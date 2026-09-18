#!/usr/bin/env python3
"""
make_reels.py — YouTube動画1本 → Instagramリール候補 2〜3本 を自動生成する

  1. transcribe : 動画 → 文字起こし JSON（faster-whisper があれば自動、無ければ Vrew の SRT を渡す）
  2. plan       : 文字起こし → Claude がフック付きの切り抜き案（plan.json）を作る
  3. render     : plan.json → 9:16 / 1080x1920 のリール動画（動きのあるテロップ・字幕・進捗バー・エンドカード）
                  + カバー画像 + Instagramキャプション + FCP用 FCPXML
  4. all        : plan → render を一気に実行

使い方の例:
  python3 make_reels.py transcribe lesson.mp4                       # → lesson.transcript.json
  python3 make_reels.py plan lesson.mp4 --transcript lesson.srt --title "Fコードが…" --count 3
  python3 make_reels.py render lesson.mp4 --plan lesson.plan.json
  python3 make_reels.py all lesson.mp4 --transcript lesson.srt --title "…"

APIキー不要で試す（手動で区間とフックを指定）:
  python3 make_reels.py render lesson.mp4 --segment "95-128|Fが押さえられない人へ|原因は指じゃない|lesson"

依存: ffmpeg（libass・libx264 入り。mac は `brew install ffmpeg`）、anthropic、pydantic
      任意: faster-whisper（自動文字起こし用）
"""
from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional
from xml.sax.saxutils import escape as xml_escape

# ───────────────────────── 定数・デフォルト ─────────────────────────

W, H = 1080, 1920           # リールのキャンバス
FPS = 30
IG_ACCOUNT = "@toshiikegawa3"
DEFAULT_MODEL = "claude-opus-5"

# リールのタイプ → 帯色（#RRGGBB）とラベル
REEL_TYPES = {
    "lesson": {"accent": "#1A66C9", "label": "ワンポイント"},   # 教える系
    "play":   {"accent": "#C91A3A", "label": "演奏"},           # 弾いてる系
    "talk":   {"accent": "#C98A1A", "label": "ギター談義"},     # 考え方・語り系
    "qa":     {"accent": "#7A1AC9", "label": "よくある質問"},   # 質問回答系
}

# Instagramキャプションの固定文（vivafla-instagram-content の規定）
FIXED_CTA = """✨✨✨✨✨
フラメンコギター、気になっている方へ。
プロフのLINEに登録すると、
豪華10大特典をまるっとプレゼントしています。
ドレミ不要・未経験から
始められるメソッドも詰まっているので、
ぜひ受け取ってみてください。
無料体験レッスンの申込みもLINEから。
👉 @toshiikegawa3"""

# 文末禁止ルール（vivafla 共通ルール）— キャプション生成後に機械チェックする
FORBIDDEN_ENDINGS = ["って。", "と。", "て。", "ですが。", "ありまして。", "んですが。", "くれて。", "ようと。", "だから。"]


# ───────────────────────── データモデル ─────────────────────────

@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Segment:
    start: float
    end: float
    text: str
    words: List[Word] = field(default_factory=list)


@dataclass
class Reel:
    id: str
    type: str                       # lesson / play / talk / qa
    start: float                    # 元動画内の秒
    end: float
    hook: str                       # 最初の0.5秒で出す一言（1行目・14文字以内）
    hook_sub: str = ""              # 2行目（任意・16文字以内）
    label: str = ""                 # 上部の小さなチップ（6文字以内）
    keywords: List[str] = field(default_factory=list)   # 字幕内で色を変える単語
    ig_caption: str = ""            # Instagramキャプション本文（固定文・ハッシュタグは後から付く）
    hashtags: List[str] = field(default_factory=list)   # 最大5
    why: str = ""                   # 選定理由（人間の確認用）

    @property
    def duration(self) -> float:
        return self.end - self.start


# ───────────────────────── 小道具 ─────────────────────────

def find_ffmpeg() -> str:
    """ffmpeg のパスを返す。PATH に無ければ imageio-ffmpeg 同梱のバイナリを使う。"""
    for name in ("ffmpeg",):
        p = shutil.which(name)
        if p:
            return p
    try:
        import imageio_ffmpeg  # type: ignore
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    sys.exit("ffmpeg が見つかりません。mac なら `brew install ffmpeg`、または `pip install imageio-ffmpeg`")


def run(cmd: List[str], quiet: bool = True) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr[-4000:])
        raise SystemExit(f"コマンド失敗: {' '.join(cmd[:3])} …")
    return r.stdout


def probe(ffmpeg: str, path: str) -> dict:
    """ffprobe が無い環境もあるので ffmpeg -i の出力から尺・解像度・fps を拾う。"""
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        out = run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                   "stream=width,height,r_frame_rate:format=duration", "-of", "json", path])
        j = json.loads(out)
        st = j["streams"][0]
        num, den = st["r_frame_rate"].split("/")
        return {"width": int(st["width"]), "height": int(st["height"]),
                "fps": int(num) / int(den), "duration": float(j["format"]["duration"])}
    r = subprocess.run([ffmpeg, "-i", path], capture_output=True, text=True)
    info = r.stderr
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", info)
    dur = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0
    m = re.search(r", (\d{2,5})x(\d{2,5})", info)
    wh = (int(m[1]), int(m[2])) if m else (1920, 1080)
    m = re.search(r"([\d.]+) fps", info)
    fps = float(m[1]) if m else 30.0
    return {"width": wh[0], "height": wh[1], "fps": fps, "duration": dur}


def default_font() -> str:
    if platform.system() == "Darwin":
        return "Hiragino Sans"
    if platform.system() == "Windows":
        return "Meiryo"
    return "IPAPGothic"


def ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def ass_color(hex_rgb: str, alpha: int = 0) -> str:
    """'#RRGGBB' → ASS の '&HAABBGGRR&'"""
    hex_rgb = hex_rgb.lstrip("#")
    r, g, b = hex_rgb[0:2], hex_rgb[2:4], hex_rgb[4:6]
    return f"&H{alpha:02X}{b}{g}{r}&"


def ass_escape(s: str) -> str:
    return s.replace("{", "｛").replace("}", "｝").replace("\n", "\\N")


def slugify(s: str, n: int = 24) -> str:
    s = re.sub(r"[\\/:*?\"<>|\s]+", "_", s.strip())
    return s[:n] or "reel"


# ───────────────────────── 文字起こしの読み込み ─────────────────────────

def parse_srt(path: str) -> List[Segment]:
    txt = Path(path).read_text(encoding="utf-8-sig")
    segs: List[Segment] = []
    for block in re.split(r"\n\s*\n", txt.strip()):
        lines = [l for l in block.splitlines() if l.strip()]
        if len(lines) < 2:
            continue
        m = re.search(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", lines[1] if "-->" in lines[1] else lines[0])
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        st = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        en = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        text_lines = lines[2:] if "-->" in lines[1] else lines[1:]
        text = re.sub(r"<[^>]+>", "", " ".join(text_lines)).strip()
        if text:
            segs.append(Segment(st, en, text))
    return segs


def parse_whisper_json(path: str) -> List[Segment]:
    j = json.loads(Path(path).read_text(encoding="utf-8"))
    segs: List[Segment] = []
    for s in j.get("segments", []):
        words = [Word(float(w["start"]), float(w["end"]), str(w.get("word", w.get("text", ""))).strip())
                 for w in s.get("words", []) if w.get("start") is not None]
        segs.append(Segment(float(s["start"]), float(s["end"]), str(s["text"]).strip(), words))
    return segs


def parse_timestamped_text(path: str) -> List[Segment]:
    """'[mm:ss] テキスト' / '12:34 テキスト' 形式（Vrew のテキストコピー等）。終了時刻は次の行の開始。"""
    rows = []
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        m = re.match(r"\s*\[?(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)\]?\s+(.*)", line)
        if m:
            h = int(m[1] or 0)
            t = h * 3600 + int(m[2]) * 60 + float(m[3])
            rows.append((t, m[4].strip()))
    segs: List[Segment] = []
    for i, (t, text) in enumerate(rows):
        en = rows[i + 1][0] if i + 1 < len(rows) else t + max(2.0, len(text) * 0.25)
        segs.append(Segment(t, en, text))
    return segs


def load_transcript(path: str) -> List[Segment]:
    ext = Path(path).suffix.lower()
    if ext == ".srt":
        return parse_srt(path)
    if ext == ".json":
        return parse_whisper_json(path)
    return parse_timestamped_text(path)


# ───────────────────────── ① transcribe ─────────────────────────

def cmd_transcribe(args) -> str:
    out = args.out or str(Path(args.video).with_suffix(".transcript.json"))
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError:
        sys.exit("faster-whisper が入っていません。`pip install faster-whisper` するか、Vrew で書き出した .srt を --transcript で渡してください。")
    model = WhisperModel(args.whisper_model, device="auto", compute_type="auto")
    segments, _ = model.transcribe(args.video, language="ja", word_timestamps=True, vad_filter=True,
                                   initial_prompt="フラメンコギター、コンパス、ブレリア、ソレア、ファルセータ、パルマ、ラスゲアード、ピカード、アルサプア、セハージョ")
    data = {"segments": []}
    for s in segments:
        data["segments"].append({
            "start": s.start, "end": s.end, "text": s.text.strip(),
            "words": [{"start": w.start, "end": w.end, "word": w.word} for w in (s.words or [])],
        })
    Path(out).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"文字起こし完了: {out}（{len(data['segments'])} セグメント）")
    return out


# ───────────────────────── ② plan（Claude で切り抜き案） ─────────────────────────

PLAN_SYSTEM = """あなたはフラメンコギター講師「トシ」（VIVAフラメンコ音楽教室）のYouTube動画から、
Instagramリール用の切り抜きを設計する編集者です。

目的: スクロール中の人が最初の0.5秒で指を止め、最後まで見て、プロフィールに飛びたくなる短尺動画を作ること。

切り抜きの原則:
- 各リールは 15〜45秒。冒頭の3秒に「一番おいしい瞬間」か「問題提起」が来る位置で切る。
- start / end は必ず文の切れ目（セグメント境界）に合わせる。話の途中で切らない。
- 自己紹介・前置き・「えー」「あの」だけの部分・繰り返しは含めない。
- タイプを混ぜる: lesson（1つのコツを教える）/ play（演奏そのもの）/ talk（考え方・本音）/ qa（質問への回答）。
  同じ動画から複数本作るときは、同じ内容の言い換えではなく違う切り口にする。

hook（1行目・全角14文字以内）の型:
- 問題提起型「Fが押さえられない人へ」/ 断言型「速弾きは練習量じゃない」/ 数字型「3日で変わる右手」
- 質問型「この1小節、弾けますか？」/ 共感型「ぼくも最初はこうでした」/ 禁止型「これだけはやらないで」
hook_sub（2行目・全角16文字以内）は答えの匂わせ。答えそのものは書かない。

label: 画面上部のチップ。6文字以内（例: ワンポイント / 演奏 / 本音 / よくある質問）。
keywords: そのリール内で字幕の色を変えて強調したい単語を3〜6個（音楽用語や核心の言葉）。

ig_caption（Instagramキャプション本文）のルール:
- 一人称は「ぼく」のみ。「私」「僕」は禁止。
- 文は必ず述語で完結させる。「〜って。」「〜と。」「〜て。」「〜ですが。」「〜だから。」など宙ぶらりんの文末は禁止。
- 説教臭さ・上から目線・関西弁・「マジで？」は禁止。自慢に見える数字は謙遜やユーモアで包む。
- ブログURLへ誘導しない。インスタ内で完結。最後は「無料体験レッスンの案内はプロフのLINEから」に自然につなぐ一文で終える。
- 生徒・一般の方の実名は書かない。
- 3〜6文。読みやすい改行。固定文とハッシュタグはこちらで付けるので書かない。
hashtags: 最大5個。#フラメンコギター は必ず入れる。

why: この区間を選んだ理由を1〜2文（人間が確認する用）。
"""


def transcript_for_prompt(segs: List[Segment]) -> str:
    return "\n".join(f"[{s.start:.1f}-{s.end:.1f}] {s.text}" for s in segs)


def cmd_plan(args, ffmpeg: str) -> str:
    segs = load_transcript(args.transcript)
    if not segs:
        sys.exit("文字起こしが空です")
    try:
        import anthropic
        from pydantic import BaseModel, Field
    except ImportError:
        sys.exit("`pip install anthropic pydantic` してください")

    class ReelPlan(BaseModel):
        id: str = Field(description="01, 02, 03 …")
        type: str = Field(description="lesson / play / talk / qa のいずれか")
        start: float = Field(description="元動画内の開始秒。セグメント境界に合わせる")
        end: float = Field(description="元動画内の終了秒。15〜45秒後")
        hook: str
        hook_sub: str = ""
        label: str = ""
        keywords: List[str] = []
        ig_caption: str
        hashtags: List[str] = []
        why: str = ""

    class Plan(BaseModel):
        reels: List[ReelPlan]

    info = probe(ffmpeg, args.video)
    user = (f"YouTube動画タイトル: {args.title or Path(args.video).stem}\n"
            f"動画の長さ: {info['duration']:.0f}秒\n"
            f"作るリールの本数: {args.count}\n"
            f"リールの長さの目安: {args.min_len}〜{args.max_len}秒\n\n"
            f"文字起こし（[開始秒-終了秒] テキスト）:\n{transcript_for_prompt(segs)}")

    client = anthropic.Anthropic()
    resp = client.messages.parse(
        model=args.model,
        max_tokens=16000,
        system=PLAN_SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_format=Plan,
    )
    if resp.stop_reason == "refusal":
        sys.exit("Claude がこのリクエストを処理できませんでした（refusal）。文字起こしの内容を確認してください。")
    plan: Plan = resp.parsed_output

    reels = []
    for r in plan.reels:
        rt = r.type if r.type in REEL_TYPES else "lesson"
        reel = Reel(id=r.id, type=rt, start=snap(r.start, segs, "start"), end=snap(r.end, segs, "end"),
                    hook=r.hook, hook_sub=r.hook_sub, label=r.label or REEL_TYPES[rt]["label"],
                    keywords=r.keywords, ig_caption=r.ig_caption, hashtags=r.hashtags[:5], why=r.why)
        reels.append(reel)

    out = args.out or str(Path(args.video).with_suffix(".plan.json"))
    save_plan(out, args.video, args.title or "", args.transcript, reels)
    print_plan(reels)
    print(f"\nplan 保存: {out}（start/end/hook はこのファイルを直接編集して調整できます）")
    return out


def snap(t: float, segs: List[Segment], which: str) -> float:
    """Claude が返した秒をいちばん近いセグメント境界に吸着させる（話の途中で切れないように）"""
    cands = [s.start for s in segs] if which == "start" else [s.end for s in segs]
    best = min(cands, key=lambda c: abs(c - t))
    return best if abs(best - t) < 2.5 else t


def save_plan(path: str, video: str, title: str, transcript: str, reels: List[Reel]) -> None:
    data = {"video": video, "title": title, "transcript": transcript,
            "reels": [asdict(r) for r in reels]}
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def load_plan(path: str) -> dict:
    j = json.loads(Path(path).read_text(encoding="utf-8"))
    j["reels"] = [Reel(**{k: v for k, v in r.items() if k in Reel.__dataclass_fields__}) for r in j["reels"]]
    return j


def print_plan(reels: List[Reel]) -> None:
    print("\n=== リール案 ===")
    for r in reels:
        print(f"[{r.id}] {r.type:6s} {r.start:7.1f}s → {r.end:7.1f}s ({r.duration:4.1f}s)")
        print(f"      フック: {r.hook} / {r.hook_sub}")
        if r.why:
            print(f"      理由: {r.why}")


# ───────────────────────── ③ render ─────────────────────────

def split_caption_chunks(segs: List[Segment], start: float, end: float, max_chars: int = 13) -> List[tuple]:
    """区間内の文字起こしを、画面に出す短いチャンク（開始, 終了, テキスト）に分割する。
    単語タイムスタンプがあればそれを使い、無ければ文字数比で時間を割る。"""
    chunks: List[tuple] = []
    for s in segs:
        if s.end <= start or s.start >= end:
            continue
        if s.words:
            buf, b_start = "", None
            for w in s.words:
                if b_start is None:
                    b_start = w.start
                buf += w.text
                if len(buf) >= max_chars or re.search(r"[。！？!?、]$", buf):
                    chunks.append((max(b_start, start), min(w.end, end), buf.strip("、")))
                    buf, b_start = "", None
            if buf and b_start is not None:
                chunks.append((max(b_start, start), min(s.words[-1].end, end), buf.strip("、")))
        else:
            parts = [p for p in re.split(r"(?<=[。！？!?、])", s.text) if p.strip()]
            # 長い部分は助詞・読点など自然な切れ目で割る
            parts2: List[str] = []
            for p in parts:
                parts2.extend(split_japanese(p, max_chars))
            total = sum(len(p) for p in parts2) or 1
            t = s.start
            for p in parts2:
                d = (s.end - s.start) * len(p) / total
                chunks.append((max(t, start), min(t + d, end), p.strip("、")))
                t += d
    # 短すぎる表示を避ける（最低0.6秒）
    fixed = []
    for i, (a, b, txt) in enumerate(chunks):
        if b - a < 0.6:
            b = a + 0.6
        if i + 1 < len(chunks):
            b = min(b, chunks[i + 1][0])
        if txt:
            fixed.append((a, b, txt))
    return fixed


_BOUNDARY = re.compile(r"[、。，,・ 　]|(?<=[てでにはがをとのもへ])")


def split_japanese(text: str, max_chars: int) -> List[str]:
    """max_chars を超える日本語テキストを、助詞・読点の直後など自然な位置で分割する。"""
    out: List[str] = []
    text = text.strip()
    while len(text) > max_chars + 2:
        window = text[max(4, max_chars - 6):max_chars + 2]
        cut = None
        for m in _BOUNDARY.finditer(window):
            cut = max(4, max_chars - 6) + m.end()
        if cut is None or cut < 4:
            cut = max_chars
        out.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        out.append(text)
    return out


def highlight(text: str, keywords: List[str], accent_ass: str) -> str:
    text = ass_escape(text)
    for kw in sorted(keywords, key=len, reverse=True):
        if kw and kw in text:
            text = text.replace(kw, f"{{\\1c{accent_ass}}}{kw}{{\\1c&HFFFFFF&}}")
    return text


def hook_lines(hook: str) -> List[str]:
    """フックを2行までに整形（\\n があれば尊重、無ければ長い場合に読点で割る）"""
    if "\n" in hook:
        return hook.split("\n")[:2]
    if len(hook) > 9:
        m = re.search(r"[、,。]", hook)
        if m and 3 <= m.end() < len(hook) - 2:
            return [hook[:m.end()], hook[m.end():]]
    return [hook]


def build_ass(reel: Reel, segs: List[Segment], dur: float, layout: str, font: str,
              hook_dur: float, end_dur: float, show_progress: bool, show_captions: bool) -> str:
    accent = REEL_TYPES[reel.type]["accent"]
    acc = ass_color(accent)
    # 座標（fit: 動画は縦中央 656〜1264 / crop: 全面）
    if layout == "fit":
        hook_y, chip_y, cap_y = 420, 200, 1470
    else:
        hook_y, chip_y, cap_y = 380, 200, 1480

    lines = hook_lines(reel.hook)
    longest = max(len(l) for l in lines)
    hook_size = 120 if longest <= 7 else 100 if longest <= 10 else 84

    styles = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,{font},{hook_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,2,0,1,9,4,5,40,40,0,1
Style: HookSub,{font},64,{ass_color('#FFD84A')},&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,6,3,5,40,40,0,1
Style: Cap,{font},70,&H00FFFFFF,&H000000FF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,1,7,3,5,60,60,0,1
Style: Chip,{font},40,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,3,0,1,0,0,5,0,0,0,1
Style: End,{font},84,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,1,0,1,7,3,5,40,40,0,1
Style: EndSmall,{font},56,{ass_color('#FFD84A')},&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,1,0,1,5,2,5,40,40,0,1
Style: Draw,{font},20,{acc},&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev: List[str] = []

    def D(layer: int, st: float, en: float, style: str, text: str) -> None:
        ev.append(f"Dialogue: {layer},{ass_time(st)},{ass_time(en)},{style},,0,0,0,,{text}")

    # 0) 0.1秒の白フラッシュ（注意を引く・控えめ）
    D(9, 0, 0.14, "Draw", "{\\an7\\pos(0,0)\\1c&HFFFFFF&\\alpha&H70&\\t(0,140,\\alpha&HFF&)\\p1}m 0 0 l 1080 0 1080 1920 0 1920{\\p0}")

    # 1) フック（0秒でポップイン → hook_dur で消える）
    hook_end = min(hook_dur, dur - end_dur - 0.2)
    text1 = "\\N".join(ass_escape(l) for l in lines)
    D(5, 0, hook_end, "Hook",
      f"{{\\an5\\pos(540,{hook_y})\\fscx20\\fscy20\\alpha&HFF&"
      f"\\t(0,160,\\fscx114\\fscy114\\alpha&H00&)\\t(160,300,\\fscx100\\fscy100)\\fad(0,220)}}{text1}")
    if reel.hook_sub:
        sub_y = hook_y + (hook_size * len(lines)) * 0.62 + 50
        D(5, 0.22, hook_end, "HookSub",
          f"{{\\an5\\move(540,{sub_y + 40:.0f},540,{sub_y:.0f},0,220)\\alpha&HFF&\\t(0,220,\\alpha&H00&)\\fad(0,220)}}"
          f"{ass_escape(reel.hook_sub)}")

    # 2) チップ（左からスライドイン、最後まで表示）
    chip = ass_escape(reel.label or REEL_TYPES[reel.type]["label"])
    cw = 44 * len(chip) + 60
    x0 = 540 - cw / 2
    D(3, 0.3, dur - end_dur, "Draw",
      f"{{\\an7\\move({x0 - 200:.0f},{chip_y - 34},{x0:.0f},{chip_y - 34},0,260)\\alpha&HFF&\\t(0,260,\\alpha&H10&)\\p1}}"
      f"m 0 0 l {cw:.0f} 0 {cw:.0f} 68 0 68{{\\p0}}")
    D(4, 0.3, dur - end_dur, "Chip",
      f"{{\\an5\\move(340,{chip_y},540,{chip_y},0,260)\\alpha&HFF&\\t(0,260,\\alpha&H00&)}}{chip}")

    # 3) 字幕チャンク（ポップイン＋キーワード色）
    if show_captions:
        for (a, b, txt) in split_caption_chunks(segs, reel.start, reel.end):
            ra, rb = a - reel.start, b - reel.start
            if rb <= 0.05 or ra >= dur - end_dur:
                continue
            rb = min(rb, dur - end_dur)
            D(2, ra, rb, "Cap",
              f"{{\\an5\\pos(540,{cap_y})\\fscx88\\fscy88\\t(0,90,\\fscx100\\fscy100)}}{highlight(txt, reel.keywords, acc)}")

    # 4) 進捗バー（画面最下部）
    if show_progress:
        D(1, 0, dur, "Draw",
          f"{{\\an7\\pos(0,{H - 14})\\alpha&H30&\\p1\\fscx0\\t(0,{int(dur * 1000)},\\fscx100)}}m 0 0 l 1080 0 1080 14 0 14{{\\p0}}")

    # 5) エンドカード（暗転 → 文字ポップ）
    es = dur - end_dur
    D(6, es, dur, "Draw", "{\\an7\\pos(0,0)\\1c&H000000&\\alpha&HFF&\\t(0,250,\\alpha&H60&)\\p1}m 0 0 l 1080 0 1080 1920 0 1920{\\p0}")
    D(7, es + 0.1, dur, "End",
      "{\\an5\\pos(540,860)\\fscx30\\fscy30\\alpha&HFF&\\t(0,180,\\fscx108\\fscy108\\alpha&H00&)\\t(180,300,\\fscx100\\fscy100)}"
      "フル動画は\\NYouTubeで")
    D(7, es + 0.35, dur, "EndSmall",
      f"{{\\an5\\move(540,1120,540,1080,0,220)\\alpha&HFF&\\t(0,220,\\alpha&H00&)}}{IG_ACCOUNT}")

    return styles + "\n".join(ev) + "\n"


def zoom_expr(motion: str, frames: int) -> str:
    if motion == "none":
        return "1"
    if motion == "punch":
        # 3秒ごとに 1.0 ⇄ 1.12 のジャンプカット風 + ごくゆっくり寄る
        return f"1+0.12*mod(floor(on/{FPS * 3}),2)+0.03*on/{frames}"
    # slowzoom（既定）: 全体でゆっくり 1.0 → 1.18
    return f"1+0.18*on/{frames}"


def render_reel(ffmpeg: str, video: str, reel: Reel, segs: List[Segment], outdir: Path, args, info: dict) -> Path:
    dur = reel.duration
    frames = int(dur * FPS) + 1
    ass_path = outdir / f"{reel.id}.ass"
    ass_path.write_text(build_ass(reel, segs, dur, args.layout, args.font, args.hook_dur, args.end_dur,
                                  not args.no_progress, not args.no_captions), encoding="utf-8")
    z = zoom_expr(args.motion, frames)
    zp = f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:fps={FPS}"

    fontsdir = args.fontsdir or ""
    ass_filter = f"ass='{ass_path.as_posix()}'" + (f":fontsdir='{fontsdir}'" if fontsdir else "")

    if args.layout == "fit":
        # 背景: ぼかし＋暗め / 前景: 16:9 をそのまま中央に（1.5倍で読み込んでから zoompan で 1080x608 に）
        fg_h = 608
        vf = (f"[0:v]fps={FPS},split[a][b];"
              f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},gblur=sigma=40,eq=brightness=-0.12:saturation=1.2[bg];"
              f"[b]scale=1620:912:force_original_aspect_ratio=decrease,pad=1620:912:(ow-iw)/2:(oh-ih)/2,{zp}:s={W}x{fg_h}[fg];"
              f"[bg][fg]overlay=(W-w)/2:(H-h)/2:shortest=1,setsar=1,{ass_filter}[v]")
    else:
        # crop: 縦いっぱいに拡大して中央（--focus で左右の位置）を切り出す
        focus = min(max(args.focus, 0.0), 1.0)
        vf = (f"[0:v]fps={FPS},scale=-2:{H},crop={W}:{H}:(iw-{W})*{focus}:0,{zp}:s={W}x{H},setsar=1,{ass_filter}[v]")

    af = f"loudnorm=I=-14:TP=-1.5:LRA=11,afade=t=out:st={max(0.0, dur - 0.6):.2f}:d=0.6"
    out = outdir / f"{reel.id}_{slugify(reel.hook)}.mp4"
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-ss", f"{reel.start:.3f}", "-t", f"{dur:.3f}", "-i", video,
           "-filter_complex", vf, "-map", "[v]", "-map", "0:a?", "-af", af,
           "-c:v", "libx264", "-preset", args.preset, "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS),
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", str(out)]
    run(cmd)

    # カバー画像（フックが最大サイズになる 0.4秒付近）
    cover = outdir / f"{reel.id}_cover.jpg"
    run([ffmpeg, "-y", "-loglevel", "error", "-ss", "0.4", "-i", str(out), "-frames:v", "1", "-q:v", "2", str(cover)])

    # Instagram キャプション
    cap = build_caption(reel)
    (outdir / f"{reel.id}_caption.txt").write_text(cap, encoding="utf-8")
    return out


def build_caption(reel: Reel) -> str:
    body = reel.ig_caption.strip() or f"{reel.hook}\n{reel.hook_sub}".strip()
    warnings = check_text_rules(body)
    tags = [t if t.startswith("#") else f"#{t}" for t in reel.hashtags][:5]
    if "#フラメンコギター" not in tags:
        tags = (["#フラメンコギター"] + tags)[:5]
    text = f"{body}\n\n{FIXED_CTA}\n\n{' '.join(tags)}\n"
    if warnings:
        text += "\n" + "\n".join(f"⚠️ 要確認: {w}" for w in warnings) + "\n"
    return text


def check_text_rules(body: str) -> List[str]:
    w = []
    if re.search(r"(^|[^ぼ])(私|僕)(?=[はがのもをにで、。])", body):
        w.append("一人称は「ぼく」のみ（私・僕が含まれています）")
    for sent in re.split(r"(?<=。)", body):
        for bad in FORBIDDEN_ENDINGS:
            if sent.strip().endswith(bad) and not sent.strip().endswith("です" + bad):
                w.append(f"文末禁止ルール: 「{sent.strip()[-12:]}」")
                break
    return w


def write_fcpxml(video: str, reels: List[Reel], info: dict, out: Path) -> None:
    """選んだ区間を縦型プロジェクトとして FCP で開けるように書き出す（微調整用）。"""
    fps = info["fps"]
    table = {23.976: ("1001/24000s", "FFVideoFormat1080p2398"), 24: ("100/2400s", "FFVideoFormat1080p24"),
             25: ("100/2500s", "FFVideoFormat1080p25"), 29.97: ("1001/30000s", "FFVideoFormat1080p2997"),
             30: ("100/3000s", "FFVideoFormat1080p30"), 59.94: ("1001/60000s", "FFVideoFormat1080p5994"),
             60: ("100/6000s", "FFVideoFormat1080p60")}
    key = min(table, key=lambda k: abs(k - fps))
    fd, _ = table[key]
    num, den = [int(x) for x in fd.rstrip("s").split("/")]

    def tc(sec: float) -> str:
        n = round(sec * den / num)
        return f"{n * num}/{den}s"

    src = Path(video).resolve()
    name = xml_escape(src.name)
    total = tc(info["duration"])
    projects = []
    for r in reels:
        d = tc(r.duration)
        projects.append(f"""      <project name="{xml_escape(f'Reel {r.id} {r.hook}')}">
        <sequence format="r1" duration="{d}" tcStart="0s" tcFormat="NDF" audioLayout="stereo" audioRate="48k">
          <spine>
            <asset-clip ref="r2" offset="0s" name="{xml_escape(r.hook)}" start="{tc(r.start)}" duration="{d}" format="r3" tcFormat="NDF" audioRole="dialogue">
              <marker start="{tc(r.start)}" duration="{fd}" value="{xml_escape(r.hook)}"/>
            </asset-clip>
          </spine>
        </sequence>
      </project>""")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.9">
  <resources>
    <format id="r1" name="CustomFormatVertical" frameDuration="{fd}" width="1080" height="1920" colorSpace="1-1-1 (Rec. 709)"/>
    <format id="r3" name="{table[key][1]}" frameDuration="{fd}" width="{info['width']}" height="{info['height']}" colorSpace="1-1-1 (Rec. 709)"/>
    <asset id="r2" name="{name}" start="0s" duration="{total}" hasVideo="1" hasAudio="1" format="r3" audioSources="1" audioChannels="2" audioRate="48000">
      <media-rep kind="original-media" src="{xml_escape(src.as_uri())}"/>
    </asset>
  </resources>
  <library>
    <event name="Reels">
{chr(10).join(projects)}
    </event>
  </library>
</fcpxml>
"""
    out.write_text(xml, encoding="utf-8")


def parse_manual_segments(specs: List[str]) -> List[Reel]:
    """--segment "START-END|フック|サブ|type" を Reel に変換"""
    reels = []
    for i, spec in enumerate(specs, 1):
        parts = spec.split("|")
        a, b = parts[0].split("-")
        typ = parts[3].strip() if len(parts) > 3 and parts[3].strip() in REEL_TYPES else "lesson"
        reels.append(Reel(id=f"{i:02d}", type=typ, start=to_sec(a), end=to_sec(b),
                          hook=parts[1].strip() if len(parts) > 1 else "この1小節、弾けますか？",
                          hook_sub=parts[2].strip() if len(parts) > 2 else "",
                          label=REEL_TYPES[typ]["label"]))
    return reels


def to_sec(s: str) -> float:
    s = s.strip()
    if ":" in s:
        parts = [float(x) for x in s.split(":")]
        return sum(p * 60 ** (len(parts) - 1 - i) for i, p in enumerate(parts))
    return float(s)


def cmd_render(args, ffmpeg: str) -> None:
    info = probe(ffmpeg, args.video)
    if args.plan:
        plan = load_plan(args.plan)
        reels: List[Reel] = plan["reels"]
        transcript = args.transcript or plan.get("transcript")
    elif args.segment:
        reels = parse_manual_segments(args.segment)
        transcript = args.transcript
    else:
        sys.exit("--plan か --segment のどちらかを指定してください")
    segs = load_transcript(transcript) if transcript and Path(transcript).exists() else []
    if not segs:
        print("※ 文字起こしが無いので字幕なしで書き出します（--transcript で .srt / .json を渡せます）")

    outdir = Path(args.outdir or (Path(args.video).with_suffix("") .as_posix() + "_reels"))
    outdir.mkdir(parents=True, exist_ok=True)
    print_plan(reels)
    outs = []
    for r in reels:
        if r.end > info["duration"] + 0.5:
            print(f"[{r.id}] 終了 {r.end}s が動画の長さ {info['duration']:.1f}s を超えています → 末尾に丸めます")
            r.end = info["duration"]
        print(f"\n[{r.id}] 書き出し中 … ({r.duration:.1f}s, {args.layout}/{args.motion})")
        outs.append(render_reel(ffmpeg, args.video, r, segs, outdir, args, info))
    write_fcpxml(args.video, reels, info, outdir / "reels_for_fcp.fcpxml")
    save_plan(str(outdir / "plan.used.json"), args.video, "", transcript or "", reels)
    print("\n=== 完了 ===")
    for o in outs:
        print(f"🎬 {o}")
    print(f"🖼  カバー: {outdir}/NN_cover.jpg")
    print(f"📝 キャプション: {outdir}/NN_caption.txt")
    print(f"🎞  FCP微調整用: {outdir}/reels_for_fcp.fcpxml（FCPで ファイル → 読み込む → XML、必要なら メディアの再リンク）")


# ───────────────────────── CLI ─────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_render_opts(sp):
        sp.add_argument("--outdir", help="出力フォルダ（既定: <動画名>_reels/）")
        sp.add_argument("--layout", choices=["fit", "crop"], default="fit",
                        help="fit: ぼかし背景＋横動画そのまま（既定・手元が見える）/ crop: 縦に切り出し（顔アップ向け）")
        sp.add_argument("--focus", type=float, default=0.5, help="crop 時の左右位置 0.0(左)〜1.0(右)")
        sp.add_argument("--motion", choices=["slowzoom", "punch", "none"], default="slowzoom",
                        help="動き: slowzoom=ゆっくり寄る / punch=3秒ごとにジャンプ寄り / none")
        sp.add_argument("--font", default=default_font(), help="テロップのフォント名（mac 既定: Hiragino Sans）")
        sp.add_argument("--fontsdir", help="フォントファイルのあるフォルダ（fontconfig で見つからないとき）")
        sp.add_argument("--hook-dur", type=float, default=3.0, help="フックの表示秒数")
        sp.add_argument("--end-dur", type=float, default=2.0, help="エンドカードの秒数")
        sp.add_argument("--no-progress", action="store_true")
        sp.add_argument("--no-captions", action="store_true")
        sp.add_argument("--preset", default="medium", help="x264 preset（試作は veryfast）")

    def add_plan_opts(sp):
        sp.add_argument("--transcript", required=True, help=".srt（Vrew）/ .json（whisper）/ [mm:ss] テキスト")
        sp.add_argument("--title", help="YouTube動画のタイトル（切り口のヒント）")
        sp.add_argument("--count", type=int, default=3, help="作る本数（既定 3）")
        sp.add_argument("--min-len", type=int, default=15)
        sp.add_argument("--max-len", type=int, default=45)
        sp.add_argument("--model", default=DEFAULT_MODEL)
        sp.add_argument("--out", help="plan.json の保存先")

    t = sub.add_parser("transcribe", help="faster-whisper で文字起こし（任意）")
    t.add_argument("video")
    t.add_argument("--whisper-model", default="large-v3")
    t.add_argument("--out")

    pl = sub.add_parser("plan", help="Claude で切り抜き案を作る")
    pl.add_argument("video")
    add_plan_opts(pl)

    r = sub.add_parser("render", help="plan.json（または --segment）からリールを書き出す")
    r.add_argument("video")
    r.add_argument("--plan")
    r.add_argument("--segment", action="append", help='"START-END|フック|サブ|type" を繰り返し指定（APIキー不要）')
    r.add_argument("--transcript", help="字幕用の文字起こし（plan に無いとき）")
    add_render_opts(r)

    a = sub.add_parser("all", help="plan → render")
    a.add_argument("video")
    add_plan_opts(a)
    add_render_opts(a)

    args = p.parse_args()
    ffmpeg = find_ffmpeg()

    if args.cmd == "transcribe":
        cmd_transcribe(args)
    elif args.cmd == "plan":
        cmd_plan(args, ffmpeg)
    elif args.cmd == "render":
        cmd_render(args, ffmpeg)
    elif args.cmd == "all":
        args.plan = cmd_plan(args, ffmpeg)
        args.segment = None
        cmd_render(args, ffmpeg)


if __name__ == "__main__":
    main()

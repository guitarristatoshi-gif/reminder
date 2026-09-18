# reels — YouTube動画からInstagramリールを自動生成

YouTube用に書き出した動画1本と文字起こしから、**縦型 1080×1920 のリール候補を2〜3本**、
カバー画像・キャプション・FCP用XMLと一緒に書き出す。
戦略（なぜ3本作って1本出すのか、動きの設計）は `STRATEGY.md`。

## できるもの

- `NN_フック.mp4` … 0.5秒でポップインするフック、スライドインするチップ、ポップする字幕、進捗バー、エンドカード入り
- `NN_cover.jpg` … カバー画像（フック表示中のフレーム）
- `NN_caption.txt` … Instagramキャプション（本文＋固定CTA＋ハッシュタグ≤5、「ぼく」・文末ルールを自動チェック）
- `reels_for_fcp.fcpxml` … 選んだ区間の縦型プロジェクト（Final Cut Pro で追い込みたいとき用）
- `plan.used.json` … 使った切り抜き案（start/end/hook を直して再レンダリングできる）

## セットアップ（mac）

```bash
brew install ffmpeg            # libass・libx264 入りの通常ビルドでOK
pip3 install anthropic pydantic
pip3 install faster-whisper    # 任意: 自動文字起こしをしたいときだけ
export ANTHROPIC_API_KEY=sk-ant-...   # plan / all を使うときだけ
```

## 使い方

### 一気に（おすすめ）

```bash
python3 reels/make_reels.py all lesson.mp4 --transcript lesson.srt --title "Fコードが押さえられない本当の理由" --count 3
```

`lesson.srt` は Vrew の「エクスポート → 字幕（SRT）」で出したもの。
Whisper の JSON（`segments[].words[]` 付き）や `[mm:ss] テキスト` 形式でも読める。
単語タイムスタンプがあると字幕の切り替えが正確になる。

### 段階的に

```bash
# 1. 文字起こし（Vrew を使わないとき。faster-whisper が必要）
python3 reels/make_reels.py transcribe lesson.mp4          # → lesson.transcript.json

# 2. 切り抜き案（Claude が候補を返す → lesson.plan.json）
python3 reels/make_reels.py plan lesson.mp4 --transcript lesson.transcript.json --title "…" --count 3

#    ここで lesson.plan.json を開いて start / end / hook を好みに直してよい

# 3. 書き出し
python3 reels/make_reels.py render lesson.mp4 --plan lesson.plan.json
```

### APIキーなしで試す（区間とフックを手で指定）

```bash
python3 reels/make_reels.py render lesson.mp4 --transcript lesson.srt \
  --segment "1:35-2:08|Fが押さえられない人へ|原因は指の力じゃない|lesson" \
  --segment "5:10-5:28|この1小節、弾けますか？|ブレリアのコンパス|play"
```

`"開始-終了|フック1行目|フック2行目|type"`。type は `lesson` / `play` / `talk` / `qa`。

## よく使うオプション（render / all）

| オプション | 既定 | 意味 |
|---|---|---|
| `--layout fit\|crop` | fit | fit: 横動画を中央に＋ぼかし背景（手元が見える）/ crop: 縦に切り出し（顔・演奏アップ） |
| `--focus 0.0〜1.0` | 0.5 | crop のときの左右位置 |
| `--motion slowzoom\|punch\|none` | slowzoom | ゆっくり寄る / 3秒ごとにジャンプ寄り / なし |
| `--font "Hiragino Sans"` | OS依存 | テロップのフォント。mac は Hiragino Sans、太さを変えたいなら `--font "Hiragino Sans W8"` |
| `--hook-dur 3` | 3 | フックを出す秒数 |
| `--end-dur 2` | 2 | エンドカードの秒数 |
| `--preset veryfast` | medium | 試作を速くしたいとき |
| `--no-captions` / `--no-progress` | | 字幕 / 進捗バーを消す |
| `--count 3` (plan/all) | 3 | 作る本数 |
| `--model` (plan/all) | claude-opus-5 | 切り抜き案に使うモデル |

## 出力の使い方

1. `NN_*.mp4` をそのまま Instagram にアップ（Meta Business Suite の予約投稿でも可）。
2. `NN_caption.txt` をキャプションに貼る。末尾に `⚠️ 要確認` があれば、その文だけ直す。
3. カバーは `NN_cover.jpg` を指定。
4. 手で追い込むなら FCP で `reels_for_fcp.fcpxml` を **ファイル → 読み込む → XML**。
   素材パスが違うと「メディアの再リンク」を求められるので元動画を指定する。

## 仕組み（ざっくり）

- テロップ・字幕・バー・エンドカードは全部 **ASS 字幕**として生成し、ffmpeg の `ass` フィルタ（libass）で焼き込む。
  `\t` によるスケール／位置アニメーションでポップイン・スライドインを作っている。
- 画面の動きは `zoompan`。背景ぼかしは `gblur`。音量は `loudnorm` で -14 LUFS に揃える。
- 切り抜き案は Claude の構造化出力（`messages.parse`）で JSON を受け取り、開始・終了を文字起こしのセグメント境界に吸着させる。

## トラブル

| 症状 | 対処 |
|---|---|
| 日本語が豆腐（□）になる | `--font` で入っているフォント名を指定。`fc-list :lang=ja` で確認。または `--fontsdir ~/Library/Fonts` |
| `ffmpeg が見つかりません` | `brew install ffmpeg` か `pip3 install imageio-ffmpeg` |
| 字幕が出ない | `--transcript` を渡しているか確認。plan.json 内の `transcript` パスも見る |
| 話の途中で切れる | plan.json の `start`/`end` を文の切れ目に手で直して `render --plan` |

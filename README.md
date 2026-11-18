# ClipForge

Turn a long video into scroll-stopping vertical clips (Shorts / TikTok / Reels) — locally, on your own PC.

Paste a YouTube link or upload a video → pick a layout and caption style → AI finds the best moments → you get polished 1080×1920 clips with word-synced captions, plus a ready-to-paste title, description and hashtags for each.

## Features

- **Sources:** YouTube link (via yt-dlp) or file upload.
- **Layouts**
  - **Split + gameplay** — speaker on top, muted gameplay from your library on the bottom.
  - **Streamer cam** — for stream VODs: the streamer's facecam is auto-detected and placed on top, the stream's own gameplay on the bottom. Drag-to-adjust framing editor.
  - **Full vertical** — center-cropped 9:16.
  - **Fit + blur** — whole frame on a blurred fill.
- **Captions:** word-by-word timing (whisper.cpp) rendered with libass. Six presets — Pop Yellow, Highlight Box, Clean Premium, Word Reveal, One Word Punch, Neon Glow — with font, size, colors, outline, words-per-line, pop size and position controls. Live preview plus an exact rendered preview.
- **AI moment picker:** Claude reads the transcript and picks self-contained moments with a hook and a payoff (falls back to a built-in scorer).
- **Review step:** rename clips, nudge start/end, preview a frame, swap style/layout before rendering.
- **Upload metadata:** after rendering, each clip gets a title, description and hashtags with copy buttons.
- **Low-RAM friendly:** one small ffmpeg call per clip, jobs run one at a time.

## Requirements

- Windows, Python 3.11+ (standard library only for the server)
- [FFmpeg](https://ffmpeg.org/) with libass, on PATH
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) on PATH (for YouTube links)
- [whisper.cpp](https://github.com/ggerganov/whisper.cpp) `whisper-cli` + a model (e.g. `ggml-large-v3-turbo-q5_0.bin`)
- Optional: [Claude Code](https://claude.com/claude-code) CLI (`claude`) on PATH for AI moment picking and titles
- Optional: `opencv-python` for Streamer-cam face detection (uses the bundled YuNet model in `models/`)

Tool locations can be set with environment variables:

| Variable | Default |
|---|---|
| `WHISPER_CLI` | `~/tools/whisper.cpp/bin/Release/whisper-cli.exe` |
| `WHISPER_MODEL` | `~/tools/whisper.cpp/models/ggml-large-v3-turbo-q5_0.bin` |
| `PORT` | `8765` |

## Run

```bat
start.bat
```

or `python server.py`, then open http://localhost:8765.

Gameplay videos live in a `gameplay/` folder next to this one (or add them in the app). Rendered clips go to `export/` next to this folder.

**YouTube "confirm you're not a bot":** if YouTube blocks downloads on your connection, upload the file instead, or put a Netscape-format `cookies.txt` in this folder (it's git-ignored).

## Project layout

```
server.py     HTTP server, job queue, API
pipeline.py   download, transcription, moment picking, titles/descriptions
captions.py   ASS caption engine + style presets
render.py     ffmpeg render graphs for each layout
facecam.py    facecam detection + crop boxes for Streamer cam
config.py     paths and tool locations
static/       front-end (vanilla HTML/CSS/JS)
fonts/        caption fonts (Impact / Segoe UI Black are copied from Windows at startup)
models/       YuNet face detector (OpenCV Zoo, MIT)
```

## Credits

- Fonts: Poppins, Lilita One, Inter Tight, Space Mono, Instrument Serif — SIL Open Font License.
- Face detection: [YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) from OpenCV Zoo.

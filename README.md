# KlipCut

AI-powered platform that turns long-form videos into short, viral-ready clips with burned-in captions, titles, and customizable layouts.

## How It Works

1. **Paste a URL or upload a video** — supports YouTube, TikTok, Twitter, and direct uploads
2. **AI analyzes the content** — transcribes with word-level timestamps, then identifies the most engaging moments using an LLM scoring rubric (hook strength, standalone value, emotion, shareability)
3. **Edit in the browser** — trim, restyle captions, change layouts, add overlays, adjust pacing — all with a live preview that matches the final export
4. **Export** — renders polished vertical clips ready for social media

## Architecture

```
React frontend  ──HTTP──▶  FastAPI backend  ──▶  Pipeline
                                │
                    ┌───────────┼────────────┐
                    ▼           ▼            ▼
                SQLite      storage/     External APIs
              (jobs, clips,  (source,    (Deepgram, Claude,
               users)       renders)     yt-dlp, FFmpeg)
```

For a project-specific walkthrough of evolving this design into separate
workers, Kubernetes autoscaling, and eventually Kafka—including diagrams,
failure modes, example manifests, and a staged learning path—see
[`docs/SCALING_KUBERNETES_KAFKA.md`](docs/SCALING_KUBERNETES_KAFKA.md).
The more detailed Kubernetes walkthrough and split example files are in
[`docs/KUBERNETES_DEEP_DIVE.md`](docs/KUBERNETES_DEEP_DIVE.md) and
[`deploy/kubernetes-learning/`](deploy/kubernetes-learning/README.md).

### Pipeline stages

| Stage | Module | What it does |
|-------|--------|-------------|
| Download | `pipeline/downloader.py` | Fetches source video via yt-dlp or accepts uploads |
| Transcribe | `pipeline/transcriber.py` | Word-level speech-to-text via Deepgram |
| Analyze | `pipeline/analyzer.py` | LLM selects clip moments with structured JSON output |
| Render | `pipeline/render.py` | FFmpeg compositing — blur/fit/gameplay/podcast frames, ASS subtitles |

### Key design decisions

- **Utterance-indexed clip selection** — the LLM picks which utterances to include, never raw timestamps. Start/end times are looked up from the transcript afterward, so clips always land on real speech boundaries.
- **ASS subtitles** — word-level karaoke captions with multiple highlight styles, generated from Deepgram timestamps. Titles live in the same subtitle file for independent styling.
- **Atomic file safety** — renders write to a temp file, validate with ffprobe, then atomically replace. A failed render never corrupts the original.
- **Recipe-based editing** — the editor saves a JSON recipe (trim points, styles, overlays). Export replays the recipe through FFmpeg. Editing and rendering are fully decoupled.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | React, Vite |
| Backend | Python, FastAPI, Pydantic |
| Database | SQLite (JSON columns for flexible fields) |
| Auth | JWT + bcrypt |
| Billing | Polar (subscriptions, credits, webhooks) |
| Storage | Local filesystem / Cloudflare R2 |
| Transcription | Deepgram (Nova-2) |
| LLM | Anthropic Claude |
| Video | FFmpeg, yt-dlp |

## Setup

### Backend

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Set API keys in .env
cp .env.example .env

uvicorn app.main:app --reload --port 8000
```

Requires `yt-dlp` and `ffmpeg` on PATH (`brew install yt-dlp ffmpeg`).

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Opens at http://localhost:5173 — proxies `/api/*` to the backend at port 8000.

## Author

**Vinay Joshi** — [vinaycjoshi310@gmail.com](mailto:vinaycjoshi310@gmail.com)

# Interview Concepts — Everything on the Resume, Explained

Every technical term, architecture decision, and keyword from the resume, explained from basics so you can talk about them confidently in an interview.

---

## Table of Contents

1. [KlipCut Concepts](#1-klipcut-concepts)
   - [Deepgram — Word-Level Transcription](#deepgram--word-level-transcription)
   - [Three-Pass Windowed LLM Highlight Analysis](#three-pass-windowed-llm-highlight-analysis)
   - [ASS Captions with Karaoke Highlighting](#ass-captions-with-karaoke-highlighting)
   - [FFmpeg Rendering Pipeline](#ffmpeg-rendering-pipeline)
   - [Hardware Encoder Detection (VideoToolbox / NVENC)](#hardware-encoder-detection-videotoolbox--nvenc)
   - [ThreadPoolExecutor — Parallel Rendering](#threadpoolexecutor--parallel-rendering)
   - [PostgreSQL Job Queue with FOR UPDATE SKIP LOCKED](#postgresql-job-queue-with-for-update-skip-locked)
   - [Stale Claim Recovery and Automatic Retries](#stale-claim-recovery-and-automatic-retries)
   - [Idempotent Webhook Fulfillment](#idempotent-webhook-fulfillment)
   - [Presigned URLs (Cloudflare R2 / S3)](#presigned-urls-cloudflare-r2--s3)
   - [Docker Multi-Service Containers](#docker-multi-service-containers)
2. [Suby Concepts](#2-suby-concepts)
   - [Real-Time Inbox and Cross-Platform Sync](#real-time-inbox-and-cross-platform-sync)
   - [OpenAI Whisper — Speech-to-Text](#openai-whisper--speech-to-text)
   - [GPT-Based Entity Extraction](#gpt-based-entity-extraction)
   - [Asynchronous Job Queues](#asynchronous-job-queues)
   - [Prisma ORM](#prisma-orm)
   - [Google OAuth](#google-oauth)
3. [Reimburser Concepts](#3-reimburser-concepts)
   - [Allowlisted DTOs](#allowlisted-dtos)
   - [Salted IP-Hash Identities](#salted-ip-hash-identities)
   - [JWT Sessions](#jwt-sessions)
   - [TTFB and Regional Co-Location](#ttfb-and-regional-co-location)
4. [CacheTray Concepts](#4-cachetray-concepts)
   - [Chrome Extension Manifest V3 (MV3)](#chrome-extension-manifest-v3-mv3)
5. [Skills Section — What Each Thing Actually Is](#5-skills-section--what-each-thing-actually-is)
   - [REST APIs](#rest-apis)
   - [gRPC](#grpc)
   - [GraphQL](#graphql)
   - [WebSockets](#websockets)
   - [Redis](#redis)
   - [Celery](#celery)
   - [FastAPI](#fastapi)
   - [Docker](#docker)
   - [AWS EC2 / S3 / Lambda](#aws-ec2--s3--lambda)

---

## 1. KlipCut Concepts

### Deepgram — Word-Level Transcription

**What it is:**
Deepgram is a speech-to-text API. You send it an audio file, it returns the transcript with timestamps for every single word.

**Why word-level matters:**
Most transcription services give you sentence-level timestamps ("this sentence was spoken between 3.2s and 7.8s"). Deepgram gives you WORD-level: "hello" at 3.2–3.5s, "world" at 3.6–3.9s. This is critical for two things:

1. **Clip boundaries** — when the LLM picks a clip, we snap the start/end to exact word boundaries. The clip never starts mid-word.
2. **Captions** — each word gets its own timing, so we can highlight the active word as it's spoken (karaoke style).

**How it works in KlipCut:**
```
Audio file (MP3) → POST to api.deepgram.com/v1/listen → JSON response
```

The response looks like:
```json
{
  "results": {
    "utterances": [
      {
        "transcript": "so the thing about startups is",
        "start": 14.2,
        "end": 16.8,
        "words": [
          {"word": "so",       "start": 14.20, "end": 14.35},
          {"word": "the",      "start": 14.40, "end": 14.50},
          {"word": "thing",    "start": 14.55, "end": 14.80},
          {"word": "about",    "start": 14.85, "end": 15.10},
          {"word": "startups", "start": 15.15, "end": 15.60},
          {"word": "is",       "start": 15.65, "end": 15.75}
        ]
      }
    ]
  }
}
```

**Two models:**
- `nova-2` (default) — detects one language for the whole file. Cheap, accurate for single-language content.
- `nova-3` with `language=multi` (opt-in) — handles code-switching (e.g., Hindi ↔ English mid-sentence). More expensive, only used when user enables "multilingual."

**Interview-ready answer:**
> "We use Deepgram's nova-2 model for speech-to-text. The key is word-level timestamps — every word comes back with a precise start and end time in seconds. This lets us snap clip boundaries to actual speech edges instead of relying on the LLM to guess timestamps, and it powers the per-word karaoke captions. For multilingual content like Hindi-English code-switching, we switch to nova-3's multi-language mode."

---

### Three-Pass Windowed LLM Highlight Analysis

**The problem with "find the best clips":**
If you send a 1-hour transcript to GPT and ask "find the 5 best moments," you get garbage. The model anchors on whatever it read first and skims the rest. The back half of the transcript is essentially ignored.

**The solution — windowed analysis:**

**Pass 1 — Nomination (per window):**
The transcript is split into overlapping windows of ~110 utterances each, with 20 utterances of overlap between windows. Each window is sent to the LLM independently with a scoring rubric.

```
Transcript: [utterances 1-110] [utterances 91-200] [utterances 181-290] ...
                                ↑ overlap ↑           ↑ overlap ↑
```

Why overlap? A great moment might straddle the boundary between two windows. With 20 utterances of overlap, at least one window will see it whole.

Each window returns candidates like:
```json
{"start_index": 45, "end_index": 62, "score": 8.5, "title": "Why most startups fail"}
```

The LLM outputs **utterance index ranges**, NOT timestamps. This is the crucial design decision — the LLM picks WHAT to clip, but Deepgram's word data determines WHERE exactly to cut.

**Pass 2 — Comparison:**
All candidates from all windows are pooled and compared head-to-head. The LLM ranks them globally, not just within their window.

**Pass 3 — Refinement:**
Each clip's in/out points are fine-tuned. Weak openings are trimmed ("so...", "um...", "yeah..."). The clip starts on the hook, not the throat-clearing.

**Then `resolve_clip_times()` maps utterance indices back to real timestamps:**
```python
clip["start"] = utterances[start_index]["words"][0]["start"]  # from Deepgram
clip["end"]   = utterances[end_index]["words"][-1]["end"]
```

**Why this is better than single-prompt:**
- Every part of the transcript gets close, focused reading
- Scoring is comparable across the whole video (same rubric everywhere)
- The LLM never needs to hold 1 hour of text in context
- Moments are de-overlapped after pooling so you don't get near-duplicate clips

**Interview-ready answer:**
> "A single prompt can't analyze a long transcript well — the model skims. So we split it into overlapping windows of about 110 utterances. Each window nominates candidates with scores. Then we pool all candidates across windows, compare them head-to-head, and refine the in/out points. The LLM outputs utterance index ranges, not raw timestamps — the actual cut points come from Deepgram's word-level data, so a clip never starts or ends mid-word."

---

### ASS Captions with Karaoke Highlighting

**What is ASS?**
ASS stands for Advanced SubStation Alpha. It's a subtitle format — like SRT, but with full styling control.

**Why not SRT?**
SRT is plain text with timestamps. No fonts, no colors, no positioning. When ffmpeg renders SRT subtitles, it uses defaults that look terrible — wrong size, wrong position, captions landing on people's faces.

ASS lets you control:
- Exact font, size, color, outline, shadow
- Pixel-perfect positioning (MarginV, MarginL, Alignment)
- Animation (fade, pop, bounce via override tags)
- Per-word timing for karaoke highlighting

**How karaoke highlighting works:**

A regular subtitle shows the whole line at once:
```
"Why most startups fail in the first year"
```

Karaoke highlighting shows the same line but the ACTIVE WORD is a different color, changing as each word is spoken:

```
Frame at 14.2s:  "WHY most startups fail in the first year"
                   ^^^  (highlighted)
Frame at 14.5s:  "Why MOST startups fail in the first year"
                       ^^^^  (highlighted)
Frame at 14.8s:  "Why most STARTUPS fail in the first year"
                            ^^^^^^^^  (highlighted)
```

**How it's implemented:**
Each word gets its own Dialogue event in the ASS file with the exact start/end time from Deepgram:

```ass
[Events]
Dialogue: 0,0:00:14.20,0:00:16.80,Highlight,,0,0,0,,Why
Dialogue: 0,0:00:14.20,0:00:16.80,Default,,0,0,0,,Why
Dialogue: 0,0:00:14.40,0:00:16.80,Highlight,,0,0,0,,most
Dialogue: 0,0:00:14.40,0:00:16.80,Default,,0,0,0,,most
```

The "Highlight" style has the accent color and shows only during that word's timing. The "Default" style stays visible for the whole cue. They overlap on screen, but the highlighted word draws on top.

**PlayResX / PlayResY:**
```ass
PlayResX: 1080
PlayResY: 1920
```
This tells the renderer "all pixel values are in a 1080×1920 coordinate space." Without it, ASS defaults to 288p and scales everything, making MarginV=120 land 800px off from where you intended.

**Animation override tags:**
```ass
{\fad(150,0)}          → fade in over 150ms
{\fscx80\fscy80\t(0,180,\fscx100\fscy100)} → start at 80% size, scale to 100% (pop effect)
```

**Interview-ready answer:**
> "We generate ASS subtitle files instead of SRT because ASS gives us full styling control — fonts, colors, positioning, and animations. The key feature is per-word karaoke highlighting: each word gets its own Dialogue event timed to Deepgram's word-level timestamps, so the active word highlights as it's spoken. We set PlayResX/PlayResY to 1080×1920 so all positioning is in real output pixels, no scaling surprises."

---

### FFmpeg Rendering Pipeline

**What FFmpeg is:**
FFmpeg is a command-line tool for video/audio processing. It can cut, encode, resize, overlay, add subtitles, adjust speed — basically anything you'd do in a video editor, but programmatically.

**What a "filter graph" is:**
FFmpeg processes video through a chain of filters. You describe the chain as a text string (the filter graph), and ffmpeg builds a processing pipeline from it.

Simple example:
```
ffmpeg -i input.mp4 -vf "scale=1080:1920" output.mp4
```

KlipCut's filter graph is complex because it does multiple things in one pass:

```
Source video → [1] Scale to fit width
             → [2] Create blurred background (scale up + gaussian blur)
             → [3] Overlay the original centered on the blurred background
             → [4] Burn in ASS subtitles
             → [5] Apply speed changes (if dead air was removed)
             → [6] Encode to H.264
             → Output MP4
```

**The "blurred background fill" technique:**
Short-form video is 9:16 (vertical). Most source video is 16:9 (horizontal). Rather than black bars (ugly) or cropping (loses content), KlipCut:
1. Takes the source frame
2. Scales it up to fill the 1080×1920 canvas (it overflows, but that's intentional)
3. Applies a heavy gaussian blur to this enlarged version
4. Overlays the original video, properly scaled, centered on top

Result: the original video is fully visible in the center, and the empty space is filled with a blurred version of itself. Looks professional.

**The ffmpeg command structure:**
```bash
ffmpeg
  -ss 14.2 -to 45.8          # seek to clip start/end
  -i source.mp4               # input video
  -filter_complex "
    [0:v]scale=1080:-2[fg];   # scale source to 1080px wide
    [0:v]scale=1080:1920,     # scale source to fill canvas
         crop=1080:1920,      # crop overflow
         gblur=sigma=40[bg];  # blur it
    [bg][fg]overlay=...       # place original on blurred background
  "
  -c:v libx264 -crf 18       # encode with H.264
  -c:a aac                    # encode audio as AAC
  output.mp4
```

**Interview-ready answer:**
> "The rendering uses ffmpeg with a complex filter graph. For the classic layout, we create a blurred, scaled-up copy of the source video as background fill to avoid black bars on the 9:16 canvas, then overlay the properly scaled original in the center. Subtitles are burned in using ffmpeg's subtitles filter with our ASS file. If dead-air removal is on, we use select/aselect filters to skip silent segments. The whole thing runs as one ffmpeg process per clip."

---

### Hardware Encoder Detection (VideoToolbox / NVENC)

**Software encoding (libx264):**
The CPU does all the work of compressing each video frame. High quality, but slow — a single core encoding 1080p video runs at maybe 30-60 fps. A 60-second clip takes 1-2 seconds per second of video.

**Hardware encoding:**
Your GPU (or a dedicated chip) has specialized circuits for video encoding. It's 5-10x faster because the silicon is purpose-built for this operation.

- **VideoToolbox** — Apple's hardware encoder on macOS (uses the M-series chip's media engine)
- **NVENC** — NVIDIA's hardware encoder on GPUs
- **VAAPI** — Linux hardware encoding (Intel/AMD)

**How KlipCut detects it at startup:**
```python
def _detect_hw_encoder():
    proc = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                          capture_output=True, text=True, timeout=5)
    if "h264_videotoolbox" in proc.stdout:
        return "h264_videotoolbox"
    return None

HW_ENCODER = _detect_hw_encoder()  # checked once at import time
```

It literally asks ffmpeg "what encoders do you have?" and checks if hardware ones are listed.

**Quality tradeoff:**
Hardware encoders use different quality parameters:
- libx264: `CRF` (Constant Rate Factor) — lower = better. CRF 18 is visually lossless.
- VideoToolbox: `q:v` (quality) — scale is different, lower = better. Quality 55 ≈ CRF 17-18.

Hardware encoding produces slightly larger files at equivalent visual quality, but it's so much faster that it's worth it for a clipping platform where turnaround time matters more than a 5% size difference.

**Interview-ready answer:**
> "At import time we probe ffmpeg for available hardware encoders. On macOS we use VideoToolbox which leverages the M-series media engine — it's about 5x faster than software x264 at equivalent quality. On NVIDIA GPUs we'd use NVENC. If neither is available, we fall back to libx264 with CRF-based quality control. The detection happens once at startup so there's no per-clip overhead."

---

### ThreadPoolExecutor — Parallel Rendering

**The problem:**
6 clips rendered sequentially = 30 minutes. Each ffmpeg process uses ~1 CPU core. Your Mac has 8+ cores sitting idle.

**The fix:**
```python
from concurrent.futures import ThreadPoolExecutor

max_parallel = 3  # configurable via CLIPPER_PARALLEL_RENDERS

with ThreadPoolExecutor(max_workers=max_parallel) as pool:
    futures = {pool.submit(render_one, clip): clip for clip in clips}
    for future in as_completed(futures):
        future.result()  # raises if the render crashed
```

**Why ThreadPoolExecutor and not ProcessPoolExecutor?**
Each "render" spawns an ffmpeg subprocess (a separate process). The Python thread is just waiting for that subprocess to finish (`subprocess.run`). It's I/O-bound waiting, not CPU-bound Python work. Threads are perfect for this — lightweight, share memory, no pickling overhead.

**Why not unlimited parallelism?**
On a Mac with 10 cores, you could run 10 ffmpeg processes. But:
- Each ffmpeg process uses ~500MB–1GB RAM (decoder buffers, frame buffers, encoder state)
- On a 2GB Railway container, 3 parallel renders would OOM
- Configurable via `CLIPPER_PARALLEL_RENDERS` environment variable

**The progress tracking:**
```python
done_count = 0
done_lock = threading.Lock()

def render_one(item):
    nonlocal done_count
    render.render_clip(**item["render_kwargs"])
    with done_lock:
        done_count += 1
        update_job(job_id, progress=f"{done_count} of {total} done")
```

`threading.Lock()` prevents two threads from incrementing `done_count` simultaneously (race condition).

**Interview-ready answer:**
> "Clip renders are independent — they read the same source but write separate outputs. So we parallelize them with ThreadPoolExecutor. Threads, not processes, because the actual work is an ffmpeg subprocess — Python is just waiting on I/O. We cap parallelism based on available memory, and use a Lock for thread-safe progress updates. On a Mac with hardware encoding, 6 clips finish in under 5 minutes instead of 30."

---

### PostgreSQL Job Queue with FOR UPDATE SKIP LOCKED

**The problem:**
Work used to run inside the same process as the API (`BackgroundTasks` in FastAPI). Three production consequences:
1. A redeploy kills every job in flight — user is charged, gets nothing
2. Renders compete with API requests for the same CPU/memory
3. Can't scale workers independently of the API

**The solution: a database-backed queue.**

A `job_queue` table:
```sql
CREATE TABLE job_queue (
    id          SERIAL PRIMARY KEY,
    job_id      TEXT UNIQUE NOT NULL,
    priority    INTEGER DEFAULT 0,
    claimed_at  TIMESTAMP,
    claimed_by  TEXT,
    attempts    INTEGER DEFAULT 0,
    available_at TIMESTAMP DEFAULT NOW(),
    created_at  TIMESTAMP DEFAULT NOW()
);
```

**How claiming works:**
```sql
SELECT id FROM job_queue
 WHERE claimed_at IS NULL
   AND available_at <= NOW()
 ORDER BY priority DESC, created_at ASC
 LIMIT 1
 FOR UPDATE SKIP LOCKED
```

- `WHERE claimed_at IS NULL` — only unclaimed jobs
- `ORDER BY priority DESC, created_at ASC` — Pro jobs first, then FIFO within same priority
- `LIMIT 1` — one job at a time
- `FOR UPDATE` — lock this row so nobody else can claim it
- `SKIP LOCKED` — if another worker already locked a row, don't wait, skip to the next one

**Without SKIP LOCKED (just FOR UPDATE):**
Worker A locks row 1. Worker B asks for a job, finds row 1, tries to lock it → **blocks**. Waits until Worker A commits. All workers serialize behind one row.

**With SKIP LOCKED:**
Worker A locks row 1. Worker B asks for a job, sees row 1 is locked, **skips it**, grabs row 2 instead. Zero contention. N workers claim N different jobs concurrently.

**SQLite fallback (optimistic claiming):**
SQLite has no SKIP LOCKED. Instead:
1. Read a candidate row
2. Try to UPDATE it, but only if `claimed_at IS STILL NULL`
3. Check `rowcount` — if 1, you got it. If 0, someone else beat you. Retry with the next row.

Works, but wastes retries under contention. SKIP LOCKED avoids that entirely.

**Interview-ready answer:**
> "We use a PostgreSQL table as a job queue. Workers claim jobs using SELECT FOR UPDATE SKIP LOCKED — this atomically locks the next unclaimed row without blocking other workers. SKIP LOCKED means if one worker has a row locked, others skip past it to the next available job rather than waiting. So N workers can claim N different jobs concurrently with zero contention. On SQLite we fall back to optimistic locking — UPDATE with a WHERE clause that ensures nobody else claimed it between our read and write."

---

### Stale Claim Recovery and Automatic Retries

**The problem:**
A worker grabs a job, starts rendering, then dies — OOM kill, redeploy, machine crash. The row stays "claimed" forever. The user sees "Rendering... 40%" for eternity.

**Stale claim detection:**
```python
STALE_CLAIM_MINUTES = 45  # generous: long renders are normal

def release_stale():
    cutoff = now() - timedelta(minutes=STALE_CLAIM_MINUTES)
    stale = query(QueuedJob).filter(
        claimed_at IS NOT NULL,
        claimed_at < cutoff      # claimed more than 45 min ago
    )
    for entry in stale:
        if entry.attempts >= MAX_ATTEMPTS:  # tried 3 times, give up
            mark_job_failed()
            refund_credits()
            delete(entry)
        else:
            entry.claimed_at = None         # put it back
            entry.available_at = now() + timedelta(minutes=entry.attempts)  # back off
```

**Exponential backoff:**
A job that just killed its worker probably shouldn't immediately kill the next one. After attempt 1, it waits 1 minute. After attempt 2, it waits 2 minutes. After attempt 3, it's permanently failed.

**Credit refunds:**
Credits are charged BEFORE rendering (so the user can't spend them elsewhere while we work). If the render fails, credits are given back. This is tracked per-job: `charged_credits` is set at billing time and refunded in the error handler.

**Interview-ready answer:**
> "If a worker dies mid-render, the claim goes stale after 45 minutes. A periodic sweep detects these, unclaims the row, and another worker picks it up. Retries back off by attempt count — 1 minute, 2 minutes — so a poison job doesn't rapidly kill every worker. After 3 failed attempts, the job is permanently marked failed and credits are refunded to the user."

---

### Idempotent Webhook Fulfillment

**The problem:**
When a user pays, Polar sends a webhook to your server: "order.paid, product: Creator plan, user: abc-123." Your server grants 500 credits. But Polar might send the SAME webhook twice (network retry, duplicate delivery). Without protection, the user gets 1000 credits for one payment.

**Idempotency = "doing it twice has the same result as doing it once."**

**How KlipCut implements it:**

Layer 1 — Webhook event deduplication:
```python
# Every webhook has a unique delivery ID
if session.get(PolarWebhookEvent, event_id):
    return {"status": "duplicate"}  # already processed
session.add(PolarWebhookEvent(id=event_id))
```

Layer 2 — Order-level deduplication:
```python
# Even if the webhook ID is different, the ORDER ID is the same
if session.get(PolarOrderGrant, order_id):
    return {"status": "duplicate"}  # already granted for this order
session.add(PolarOrderGrant(order_id=order_id, credits=500))
user.credits += 500
```

Two layers because:
- Polar might retry with the same webhook ID → caught by layer 1
- Polar might send a different event (e.g., `order.updated`) with the same order → caught by layer 2

**The checkout confirmation endpoint:**
Webhooks can't reach localhost during development. So we also have `POST /billing/confirm` — the frontend sends the `checkout_id` after payment, the backend calls Polar's API directly to verify, and grants credits using the same idempotent `PolarOrderGrant` mechanism. Same result whether the webhook or the confirm endpoint fires first.

**Interview-ready answer:**
> "Webhook fulfillment is idempotent at two levels. First, each webhook delivery ID is stored — duplicates are rejected. Second, each order ID gets a unique grant record — even if a different webhook arrives for the same order, credits are only granted once. We also have a direct confirmation endpoint for development where webhooks can't reach localhost — it calls Polar's API to verify the checkout and uses the same idempotent grant mechanism."

---

### Presigned URLs (Cloudflare R2 / S3)

**The problem:**
Your clips are stored in Cloudflare R2 (S3-compatible object storage). You can't make the bucket public — anyone could download everything. But you need to let users download their specific clips.

**Presigned URLs:**
A presigned URL is a regular URL with a cryptographic signature baked into the query string. It says "the owner of this bucket authorizes downloading this specific file, until this expiry time."

```
https://bucket.r2.cloudflarestorage.com/jobs/abc-123/clip_1.mp4
  ?X-Amz-Algorithm=AWS4-HMAC-SHA256
  &X-Amz-Credential=...
  &X-Amz-Date=20260812T000000Z
  &X-Amz-Expires=3600
  &X-Amz-Signature=a8b3c4d5e6...
```

The server generates these using boto3:
```python
url = s3_client.generate_presigned_url(
    "get_object",
    Params={"Bucket": "klipcut-test", "Key": "jobs/abc-123/clip_1.mp4"},
    ExpiresIn=3600  # valid for 1 hour
)
```

**Why this is better than proxying through your server:**
- Without presigned URLs: User → Your API → R2 → Your API → User (double bandwidth, your server is the bottleneck)
- With presigned URLs: User → R2 directly (your server just generates the URL, R2 serves the file)

**Cloudflare R2 vs AWS S3:**
R2 is S3-compatible (same API, same boto3 client) but has zero egress fees. S3 charges $0.09/GB for data leaving AWS. For a video platform serving hundreds of MB per user, R2 saves significant money.

**Interview-ready answer:**
> "Clips are stored in Cloudflare R2, which is S3-compatible. We serve them via presigned URLs — the API generates a time-limited, signed URL that lets the user download directly from R2 without proxying through our server. This offloads bandwidth from the API and R2 has zero egress fees unlike S3, which matters for a video platform."

---

### Docker Multi-Service Containers

**The architecture:**
KlipCut runs as two separate services:
1. **API** — FastAPI web server, handles HTTP requests, serves the frontend
2. **Worker** — polls the job queue, runs the rendering pipeline

They share the same codebase and the same PostgreSQL database, but run as separate containers with separate resource limits.

**Why separate?**
- The API needs to be fast and responsive (low memory, low CPU)
- The worker needs lots of RAM (ffmpeg buffers) and CPU (encoding)
- A render that OOM-crashes the worker doesn't take down the API
- You can scale workers independently: 1 API + 3 workers for high demand

**Docker Compose (simplified):**
```yaml
services:
  api:
    build: ./backend
    command: uvicorn app.main:app --host 0.0.0.0
    ports: ["8000:8000"]
    environment:
      - DATABASE_URL=postgresql://...

  worker:
    build: ./backend
    command: python -m app.worker
    deploy:
      resources:
        limits:
          memory: 2G
    environment:
      - DATABASE_URL=postgresql://...  # same database
      - CLIPPER_PARALLEL_RENDERS=2
```

**Interview-ready answer:**
> "The API and worker run as separate Docker containers sharing the same PostgreSQL database. This decouples serving from processing — a render that OOM-crashes the worker doesn't take down the API, and workers can be scaled independently based on queue depth."

---

## 2. Suby Concepts

### Real-Time Inbox and Cross-Platform Sync

**What it means:**
Messages from WhatsApp, Telegram, Gmail, Slack, Discord, and Google Calendar all appear in one unified inbox. When a new message arrives on any platform, it shows up in real time — no refresh needed.

**How real-time works (WebSockets):**
Normal HTTP: client asks, server responds. For real-time, you need the server to PUSH data to the client without being asked.

WebSocket: a persistent, bidirectional connection. Once opened, either side can send messages at any time.

```
Client                    Server
  |--- HTTP Upgrade ------→|
  |←-- 101 Switching ------|
  |                         |
  |←-- new WhatsApp msg ---|  (server pushes)
  |←-- new Gmail thread ---|  (server pushes)
  |--- mark as read ------→|  (client sends)
```

**Cross-platform sync:**
Each platform has its own API:
- WhatsApp: WhatsApp Business API webhooks
- Telegram: Bot API with `getUpdates` or webhooks
- Gmail: Google API with push notifications or polling
- Slack: Slack Events API (webhooks)
- Discord: Discord Gateway (WebSocket)

A webhook handler for each platform normalizes messages into a common format, saves to the database, and pushes to the client via WebSocket.

---

### OpenAI Whisper — Speech-to-Text

**What it is:**
Whisper is OpenAI's speech recognition model. You send it audio, it returns text. Unlike Deepgram (cloud API), Whisper can run locally or via OpenAI's API.

**How it was used at Suby:**
Voice notes sent to a Telegram bot → audio extracted → sent to Whisper → text returned → GPT extracts contact details from the text → CRM record created/updated.

**Whisper vs Deepgram:**
- Whisper: general-purpose, great accuracy, runs locally or via API
- Deepgram: optimized for real-time, gives word-level timestamps, streaming support
- KlipCut uses Deepgram because word-level timing is critical for captions
- Suby used Whisper because it just needed the text, not per-word timing

---

### GPT-Based Entity Extraction

**What it is:**
Taking unstructured text and pulling out structured data using an LLM.

**Example:**
Input (from a voice note):
> "Hey I just talked to Rahul Sharma from Flipkart, his email is rahul@flipkart.com, we discussed the integration timeline for Q3"

GPT extracts:
```json
{
  "name": "Rahul Sharma",
  "company": "Flipkart",
  "email": "rahul@flipkart.com",
  "context": "integration timeline for Q3"
}
```

**Why GPT instead of regex/NLP?**
Regex breaks on variations: "Rahul from Flipkart," "Flipkart's Rahul," "talked to the Flipkart team, Rahul was there." GPT handles all of these naturally. For a CRM where data quality matters but input is messy voice notes, LLM extraction is dramatically more reliable.

---

### Asynchronous Job Queues

**What it is:**
Instead of doing slow work during a request (which blocks the server), you put it on a queue and a background worker processes it later.

**Synchronous (bad for slow tasks):**
```
User request → Process 30 seconds of work → Response (user waited 30 seconds)
```

**Asynchronous (what we do):**
```
User request → Add to queue → Response "got it!" (instant)
                                    ↓
                              Worker picks it up
                              Processes in background
                              Notifies user when done
```

**Common implementations:**
- Redis + Celery (Python) — most common
- PostgreSQL table (what KlipCut uses) — simpler, no extra infrastructure
- RabbitMQ, AWS SQS — dedicated message brokers

---

### Prisma ORM

**What an ORM is:**
Object-Relational Mapping. Instead of writing SQL strings, you write code in your programming language and the ORM generates SQL.

**Without ORM:**
```javascript
const result = await db.query(
  "SELECT * FROM users WHERE email = $1", [email]
);
```

**With Prisma:**
```javascript
const user = await prisma.user.findUnique({
  where: { email: email }
});
```

**What makes Prisma special:**
1. **Schema-first** — you define your database schema in a `.prisma` file:
   ```prisma
   model User {
     id    String @id @default(uuid())
     email String @unique
     name  String
     contacts Contact[]
   }
   ```
2. **Auto-generated types** — TypeScript knows the shape of every query result
3. **Migrations** — `prisma migrate dev` generates SQL migration files from schema changes
4. **Type-safe queries** — you can't query a field that doesn't exist; the compiler catches it

---

### Google OAuth

**What it is:**
Letting users sign in with their Google account instead of creating a username/password.

**The flow:**
```
1. User clicks "Sign in with Google"
2. Browser redirects to accounts.google.com
3. User approves access
4. Google redirects back to your app with an authorization CODE
5. Your server exchanges the code for an ACCESS TOKEN (server-to-server)
6. Your server uses the token to fetch user's profile (name, email, photo)
7. Create or find the user in your database
8. Issue your own session token (JWT)
```

**Why server-side exchange matters:**
The authorization code → access token exchange happens server-to-server, not in the browser. This prevents someone from stealing the token by inspecting browser traffic. The code is single-use and expires in minutes.

---

## 3. Reimburser Concepts

### Allowlisted DTOs

**DTO = Data Transfer Object.** A defined shape for data going in or out of an API.

**The security problem:**
Without DTOs, an API might accept any fields:
```json
POST /api/user/update
{"name": "Vinay", "role": "admin", "credits": 99999}
```

If the server blindly spreads this into the database, the user just made themselves admin with infinite credits. This is called **mass assignment**.

**Allowlisted DTO:**
```typescript
// Only these fields are accepted. Everything else is silently dropped.
class UpdateUserDto {
  name: string;     // allowed
  bio: string;      // allowed
  // role: NOT HERE  — can't be set via API
  // credits: NOT HERE
}
```

The API validates the incoming request against this shape. Even if someone sends `role: "admin"`, it's ignored because it's not in the allowlist.

**Interview-ready answer:**
> "Every API endpoint accepts input through an allowlisted DTO — a strict schema that defines exactly which fields are writable. Anything not in the DTO is dropped, preventing mass assignment attacks where a user could set fields like role or credits by adding them to the request body."

---

### Salted IP-Hash Identities

**The problem:**
You want to identify repeat visitors (for trust scoring), but you can't store their IP address — that's PII (personally identifiable information) and creates legal/privacy liability.

**The solution:**
```python
import hashlib

SALT = "random-secret-per-deployment"

def identity(ip_address):
    return hashlib.sha256(f"{SALT}:{ip_address}".encode()).hexdigest()

# "192.168.1.42" → "a8b3c4d5e6f7..."
# Same IP always produces the same hash
# But you can NEVER reverse the hash back to the IP
```

**Why the salt?**
Without it, someone with the hash could brute-force all 4 billion IPv4 addresses and find which IP produced that hash. The salt makes this infeasible — they'd need to know your secret salt first.

**How it's used for trust:**
- First visit: hash is new → trust level 0 (limited actions)
- After 3 verified payments: trust level 1 (more features unlocked)
- Same person, same hash, building reputation — without ever storing who they are

---

### JWT Sessions

**JWT = JSON Web Token.** A signed token the server gives the client after login.

**Structure (three parts, base64-encoded, dot-separated):**
```
eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoiYWJjLTEyMyIsImV4cCI6MTcyMzQ1Njc4OX0.signature
   ↑ Header              ↑ Payload                                                  ↑ Signature
```

**Payload (decoded):**
```json
{"user_id": "abc-123", "exp": 1723456789}
```

**How it works:**
1. User logs in (Google OAuth) → server creates a JWT with the user's ID, signs it with a secret key
2. Client stores the JWT (localStorage)
3. Every request sends it: `Authorization: Bearer eyJ...`
4. Server verifies the signature — if valid, it trusts the `user_id` inside without a database lookup

**Why JWT instead of server-side sessions?**
- No session store needed (no Redis, no database table for sessions)
- Stateless — any server can verify it, good for horizontal scaling
- The token IS the session

**The tradeoff:**
You can't "log out" server-side — the token is valid until it expires. Workaround: short expiry (1 hour) + refresh tokens, or a blocklist for revoked tokens.

---

### TTFB and Regional Co-Location

**TTFB = Time to First Byte.** How long from the browser sending a request to receiving the first byte of the response. It measures server-side latency.

**What makes TTFB slow:**
```
User (Mumbai) → Server (US-East) → Database (US-West) → Server → User
     40ms             80ms              80ms            40ms
Total: ~240ms
```

**Regional co-location:**
Put the server and database in the same region, close to your users:
```
User (Mumbai) → Server (Mumbai) → Database (Mumbai) → Server → User
      5ms             1ms              1ms             5ms
Total: ~12ms
```

**~40% improvement** comes from eliminating the cross-region round trips. The server↔database hop drops from 80ms to 1ms, and the user↔server hop drops from 40ms to 5ms.

**Interview-ready answer:**
> "TTFB was high because the server and database were in different regions, adding 80ms per database round trip. Moving them to the same region — co-located near our primary user base — cut TTFB by about 40%. The server-to-database hop went from cross-continent to same-datacenter."

---

## 4. CacheTray Concepts

### Chrome Extension Manifest V3 (MV3)

**What Manifest V3 is:**
Chrome extensions declare their capabilities in a `manifest.json` file. V3 is the latest version (replacing V2), required for all new extensions since 2023.

**Key changes from V2 to V3:**
1. **Service workers instead of background pages** — V2 had a persistent background page (always running, eating RAM). V3 uses a service worker that wakes up on events and goes back to sleep. More efficient, but you can't keep state in memory.

2. **declarativeNetRequest instead of webRequest** — V2 could intercept and modify any network request (powerful but dangerous). V3 uses declarative rules (you describe what to block/modify, Chrome enforces it). Limits what extensions can do but improves security.

3. **Stricter CSP** — can't load remote scripts. All code must be bundled in the extension.

**Why it matters for the resume:**
MV3 is what Google requires now. Saying "MV3" signals you built something current, not a legacy V2 extension.

---

## 5. Skills Section — What Each Thing Actually Is

### REST APIs

**RE**presentational **S**tate **T**ransfer. An API design pattern using HTTP methods on resources:
- `GET /users/123` — read user 123
- `POST /users` — create a user
- `PUT /users/123` — replace user 123
- `PATCH /users/123` — partially update user 123
- `DELETE /users/123` — delete user 123

Stateless: each request carries everything the server needs (no session memory between requests).

### gRPC

**g**oogle **R**emote **P**rocedure **C**all. A way for services to call functions on each other as if they were local.

**REST vs gRPC:**
- REST: text-based (JSON), human-readable, HTTP/1.1
- gRPC: binary (Protocol Buffers), compact, HTTP/2, supports streaming

```protobuf
// Define the service contract
service UserService {
  rpc GetUser (UserRequest) returns (UserResponse);
}
message UserRequest { string id = 1; }
message UserResponse { string name = 1; string email = 2; }
```

**When to use gRPC:** microservice-to-microservice communication where speed matters and humans don't need to read the payloads. Typically 5-10x faster than REST+JSON for the same data.

### GraphQL

A query language for APIs. Instead of the server deciding what data each endpoint returns, the CLIENT specifies exactly what fields it wants.

**REST:** Server decides the shape
```
GET /users/123 → {"id": 123, "name": "Vinay", "email": "...", "bio": "...", "avatar": "...", ...}
// Client only needed name and email but got everything
```

**GraphQL:** Client decides the shape
```graphql
query {
  user(id: 123) {
    name
    email
  }
}
→ {"name": "Vinay", "email": "..."}
```

**Two main benefits:**
1. No over-fetching (only get what you need)
2. One endpoint for everything (no `/users`, `/posts`, `/comments` — just `/graphql`)

### WebSockets

A persistent, bidirectional connection between client and server. Unlike HTTP (request → response → done), a WebSocket stays open and either side can send messages at any time.

**Used for:** real-time features — chat, live notifications, collaborative editing, stock tickers, game state.

```javascript
const ws = new WebSocket("wss://api.example.com/ws");
ws.onmessage = (event) => console.log("Server says:", event.data);
ws.send("Hello from client");
```

### Redis

An in-memory key-value store. Data lives in RAM, so reads/writes are microseconds (vs milliseconds for PostgreSQL).

**Common uses:**
- **Caching:** store frequently-accessed data to avoid database hits
- **Session storage:** JWT tokens, user sessions
- **Rate limiting:** count requests per IP per minute
- **Pub/sub:** real-time message broadcasting
- **Job queues:** Celery uses Redis as its message broker

```
SET user:123:name "Vinay"    → stored in RAM
GET user:123:name            → "Vinay" (microseconds)
EXPIRE user:123:name 3600    → auto-delete after 1 hour
```

### Celery

A distributed task queue for Python. You define tasks as functions, and Celery runs them in background workers.

```python
@celery.task
def send_email(to, subject, body):
    # This runs in a worker process, not the web server
    smtp.send(to, subject, body)

# In your API handler:
send_email.delay("user@example.com", "Welcome!", "...")  # returns immediately
```

**Components:**
- **Broker** (Redis or RabbitMQ) — holds the queue of tasks
- **Worker** — pulls tasks from the broker and executes them
- **Result backend** (optional, Redis/DB) — stores return values

KlipCut uses a PostgreSQL-based queue instead of Celery because it's simpler (no Redis dependency) and the job queue needs features Celery doesn't have (credit refunds, stale claim recovery, priority ordering).

### FastAPI

A modern Python web framework. The "Fast" is both performance (built on ASGI/uvicorn, async-capable) and development speed (auto-generated docs, type validation).

```python
@app.post("/jobs")
def create_job(url: str, n_clips: int = 3):
    # FastAPI auto-validates: url must be a string, n_clips must be an int
    # Auto-generates OpenAPI docs at /docs
    return {"job_id": "abc-123"}
```

**Why FastAPI over Flask/Django:**
- Async support (critical for I/O-heavy work like calling Deepgram/OpenAI)
- Pydantic validation (type-safe request/response models)
- Auto-generated Swagger docs
- Performance comparable to Node.js/Go

### Docker

Packages your application + all dependencies into a container that runs identically everywhere.

**Without Docker:** "It works on my machine" — different Python versions, missing ffmpeg, wrong library versions.

**With Docker:**
```dockerfile
FROM python:3.14
RUN apt-get install -y ffmpeg
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["uvicorn", "app.main:app"]
```

This Dockerfile IS the environment. Anyone running `docker build && docker run` gets the exact same setup.

### AWS EC2 / S3 / Lambda

**EC2 (Elastic Compute Cloud):** Virtual machines in the cloud. You pick CPU, RAM, storage, and run whatever you want. KlipCut's API and workers run on EC2 instances.

**S3 (Simple Storage Service):** Object storage. Upload files (videos, images, anything), get a URL. Scales infinitely. KlipCut uses the S3-compatible Cloudflare R2 instead (same API, zero egress fees).

**Lambda:** Serverless functions. Upload a function, AWS runs it on demand. You pay per invocation, not per hour. Good for small, event-driven tasks (resize an image, process a webhook). Not suitable for KlipCut's renders (too long, too much memory).

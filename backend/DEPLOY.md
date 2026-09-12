# Deploying KlipCut

Three services and a bucket. The API and the worker run the same image from the
same repo; what differs is the start command and two environment variables.

```
  Vercel            Railway: API           Railway: worker(s)
  frontend  ──────▶ FastAPI          ┌───▶ worker.py
                    enqueues only    │     claims + renders
                          │          │
                          ▼          │
                    Postgres  ───────┘     Cloudflare R2
                    jobs + job_queue        clips, proxies, sources
```

## Why they are separate

The API used to render inside the request process, via FastAPI's
`BackgroundTasks`. Three things followed, all of which only appear under real
use:

- **A redeploy lost work.** Jobs in flight died with the container, with no
  record they had started. The user had already been charged.
- **Renders starved the API.** ffmpeg saturates every core it is given, so
  submitting a job made the whole site slow for everyone else.
- **Capacity was one container.** You could not add render throughput without
  also multiplying the web server.

The queue fixes all three. A claimed job can be released and retried, workers
are sized independently, and capacity is now "how many workers" rather than a
property of the web server.

## Service 1 — API

| | |
|---|---|
| Start | `uvicorn server:app --host 0.0.0.0 --port $PORT` |
| Size | 0.5 vCPU / 512 MB is plenty — it does no video work |
| Scale | Horizontally. Replicas are stateless. |

```bash
CLIPPER_INLINE_WORKER=0     # REQUIRED. Otherwise the API renders too, and
                            # you have rebuilt the problem you just fixed.
```

## Service 2 — Worker

Same repo, same build, different command.

| | |
|---|---|
| Start | `python worker.py` |
| Size | **2 vCPU / 2 GB each.** Peak usage is ~800 MB for one render; 1 GB leaves no headroom. |
| Scale | Add replicas. Each runs one job at a time. |

```bash
CLIPPER_INLINE_WORKER=0     # irrelevant here, but harmless
CLIPPER_MAX_CONCURRENT_RENDERS=1
```

One job per worker is deliberate. ffmpeg already uses every core it is given,
so a second concurrent render on the same box buys no throughput and only
competes for the memory ceiling. Add workers, not concurrency.

`CLIPPER_FFMPEG_THREADS` is normally left unset — the encoder reads the
container's actual CPU quota from cgroups. Set it only to override that.

## Shared environment

Both services need identical values for all of these. The worker is not
optional infrastructure; it runs the whole pipeline and needs every key.

```bash
DATABASE_URL=postgresql://...        # the same database. This IS the queue.
OPENAI_API_KEY=...
DEEPGRAM_API_KEY=...

CLIPPER_MODEL_STANDARD=...           # analysis model, every job
CLIPPER_MODEL_ADVANCED=...           # jobs that pay the surcharge

R2_BUCKET=...                        # without these the worker writes to its
R2_ENDPOINT=...                      # own ephemeral disk and the API serves
R2_ACCESS_KEY_ID=...                 # 404s for files it cannot see
R2_SECRET_ACCESS_KEY=...

CLIPPER_APP_ORIGIN=https://your-frontend  # payment return URL
POLAR_ACCESS_TOKEN=...
POLAR_WEBHOOK_SECRET=...
```

**R2 is what makes the split possible.** The worker renders on its own disk and
publishes; the API serves signed URLs from the bucket. Without R2 the two
processes cannot see each other's files at all.

## Scaling

`GET /health` reports queue depth without authentication:

```json
{"ok": true, "queue": {"waiting": 12, "running": 2, "total": 14}}
```

`waiting` climbing while `running` stays flat means the workers are saturated —
add a worker. No amount of API capacity changes that number.

Roughly: one worker finishes a typical job in a few minutes, so sustained
`waiting > 3 × workers` is the point to add one.

## Failure handling

- **Worker dies mid-render** (redeploy, OOM, machine loss). The claim goes
  stale and is released after `CLIPPER_STALE_CLAIM_MINUTES` (default 45), then
  another worker picks the job up. Nothing is lost.
- **Job repeatedly kills its worker.** After `CLIPPER_MAX_ATTEMPTS` (default 3)
  it is marked failed and the credits are refunded, rather than cycling
  forever and occupying the queue.
- **Job fails cleanly** (private video, plan limit, unreadable file). Not
  retried — it would fail identically every time. The error reaches the user
  and `run_pipeline` refunds anything already charged.

The stale-claim window is a ceiling on how long a genuinely slow job may run
before a second worker assumes it is abandoned. Raise it if you legitimately
render for longer than 45 minutes; a duplicate render costs real money.

## Local development

Nothing above is required. `CLIPPER_INLINE_WORKER` defaults to **on**, so the
API starts a worker thread and jobs process with one command and no Postgres —
SQLite is the default and the queue works on it, using an optimistic claim
instead of `SELECT ... FOR UPDATE SKIP LOCKED`.

To rehearse the production shape locally, run two terminals:

```bash
CLIPPER_INLINE_WORKER=0 uvicorn app.main:app --reload
python worker.py
```

Note that `--reload` restarts on every file save, which kills a running job.
Use plain `uvicorn app.main:app` while processing video.

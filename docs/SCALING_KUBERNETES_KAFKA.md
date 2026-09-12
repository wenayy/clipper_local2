# Scaling KlipCut: from one process to Kubernetes and Kafka

This is a learning guide and an architecture proposal for **this repository**. It is not a claim that KlipCut needs Kubernetes or Kafka today. The safest path is to add each piece only when a measured bottleneck justifies it.

For the line-by-line Kubernetes explanation and a repository-shaped set of
ConfigMap, Secret, Deployment, Service, Ingress, HPA, KEDA, and PDB examples,
continue with [`KUBERNETES_DEEP_DIVE.md`](KUBERNETES_DEEP_DIVE.md) and
[`deploy/kubernetes-learning/`](../deploy/kubernetes-learning/README.md).

The short version is:

1. Keep the React frontend static.
2. Run several lightweight FastAPI API replicas.
3. Run video workers separately because FFmpeg is CPU- and memory-heavy.
4. Put durable records in PostgreSQL and media in R2/S3, never on a pod's permanent filesystem.
5. Initially keep the existing PostgreSQL-backed `job_queue`.
6. Scale workers from queue depth (KEDA is a good Kubernetes tool for this).
7. Add Kafka later if KlipCut needs multiple independent consumers, an event history, or separately scalable pipeline stages.

---

## 1. Start with the system that exists

The current important code paths are:

| Concern | Code in this repository | What it owns |
|---|---|---|
| HTTP API | `backend/app/main.py` | Auth, billing, uploads, job submission, polling, editing |
| Durable job queue | `backend/app/queue.py` and `QueuedJob` in `backend/app/models.py` | Waiting jobs, priority, claims, attempts |
| Worker loop | `backend/worker.py` | Poll, claim, run, retry, stale-claim cleanup |
| Claim/run contract | `backend/app/runner.py` | Complete versus retry behavior |
| Video pipeline | `backend/app/main.py::run_pipeline` and `backend/app/pipeline/*` | Download, transcribe, analyze, render, publish |
| Database | `backend/app/db.py` | SQLite locally or PostgreSQL in production |
| Media | `backend/app/storage.py` | Local disk locally or Cloudflare R2 in production |
| Browser client | `frontend/` | Submits work and polls job status |

### Local-development shape

```mermaid
flowchart LR
    U[Browser] -->|HTTP| A[FastAPI]
    A --> DB[(SQLite)]
    A --> Q[Inline worker thread]
    Q --> P[Download / transcribe / analyze / FFmpeg]
    P --> FS[(backend/storage)]
    U -->|poll status / stream clip| A
```

This is convenient because one command runs everything. It is not a production scaling shape: API traffic and FFmpeg compete inside one process, SQLite is a single local file, and local media is visible only to the machine that created it.

### Production shape already supported by the code

```mermaid
flowchart LR
    U[Browser] --> FE[Static frontend / CDN]
    FE --> LB[Load balancer]
    LB --> A1[API replica 1]
    LB --> A2[API replica 2]
    A1 --> PG[(PostgreSQL)]
    A2 --> PG
    PG --> W1[Worker 1]
    PG --> W2[Worker 2]
    W1 --> R2[(R2 object storage)]
    W2 --> R2
    A1 --> R2
    A2 --> R2
    R2 -->|signed media URL| U
```

The API and worker may use the **same container image** with different commands:

- API: `uvicorn server:app --host 0.0.0.0 --port 8000`
- Worker: `python worker.py`

They share no process memory or filesystem. PostgreSQL coordinates work; R2 shares media.

### What happens when a user submits a job today

```mermaid
sequenceDiagram
    autonumber
    actor Browser
    participant API as FastAPI API
    participant DB as PostgreSQL
    participant Worker
    participant Providers as yt-dlp / Deepgram / LLM
    participant FFmpeg
    participant R2

    Browser->>API: POST /api/jobs
    API->>API: Validate plan, options and source duration
    API->>DB: INSERT jobs (status=queued)
    API->>DB: INSERT job_queue
    API-->>Browser: 200 {job_id}

    loop Every ~2 seconds
        Worker->>DB: SELECT ... FOR UPDATE SKIP LOCKED
    end
    Worker->>DB: Claim row and increment attempts
    Worker->>Providers: Inspect/download/transcribe/analyze
    Worker->>DB: Update status and percentage
    Worker->>FFmpeg: Render clip files to pod scratch disk
    Worker->>R2: Upload clips and source
    Worker->>DB: Mark job done and delete queue row

    loop While processing
        Browser->>API: GET /api/jobs/{id}
        API->>DB: Read status and queue position
        API-->>Browser: queued / transcribing / rendering / done
    end
    Browser->>API: GET /api/clips/{job}/{file}
    API-->>Browser: 302 signed R2 URL
    Browser->>R2: Stream media directly (Range requests)
```

For uploads, the ordering is deliberately different: the API streams the source to scratch space, publishes `source.mp4` to R2, verifies it exists, and **only then** inserts the queue item. This prevents an idle worker from claiming a job whose source is not available yet.

---

## 2. State: the most important distributed-systems concept here

Pods can be killed, replaced, or moved at any time. A scalable service must make every important fact live outside a pod.

| State | Correct home | Why |
|---|---|---|
| User, credits, job state, clips, queue claims | PostgreSQL | Transactional and shared by all replicas |
| Source videos, previews, rendered clips | R2/S3 | Large, durable, shared, supports direct downloads |
| Current FFmpeg files | Pod scratch volume (`emptyDir`) | Temporary working data only |
| API keys and DB/R2 credentials | Kubernetes Secret or external secret manager | Must not be committed to Git or baked into images |
| Non-secret settings | ConfigMap | Same deployable image, environment-specific configuration |
| Logs, metrics, traces | External observability system | Pod logs disappear with the pod |

The rule of thumb is: **if losing a pod loses the only copy, that data is in the wrong place.**

R2 is not optional once API and workers are separate. A worker cannot read an upload from the API pod's disk, and one API replica cannot serve a render created on another worker's disk.

---

## 3. Scale in phases

Do not jump from a laptop directly to a ten-service Kafka architecture.

### Phase 0 — one-machine development

- SQLite
- local storage
- `CLIPPER_INLINE_WORKER=1`
- one FastAPI process

Use this for development and tests.

### Phase 1 — durable production, before Kubernetes

- Managed PostgreSQL
- Cloudflare R2
- one API service with `CLIPPER_INLINE_WORKER=0`
- one standalone worker
- static frontend/CDN
- backups, error tracking, structured logs

This proves the distributed boundaries without adding cluster operations.

### Phase 2 — Kubernetes with the existing database queue

- 2+ API pods behind one Service/Ingress
- independent worker Deployment
- PostgreSQL and R2 remain managed outside the cluster
- API HPA based on HTTP/CPU
- worker autoscaling based on `job_queue` depth
- CronJob for retention/reconciliation

This is probably enough for a surprisingly large KlipCut workload.

### Phase 3 — Kafka event backbone

Add Kafka only when one or more of these are true:

- Several independent systems need the same job event (rendering, notifications, analytics, moderation, audit).
- The monolithic pipeline needs separately scalable stages.
- A replayable history of events is valuable.
- Database polling/locking is a measured bottleneck.
- Event throughput is high enough to justify operating Kafka.

Kafka does **not** replace PostgreSQL or R2. PostgreSQL remains the source of truth for user-visible state and billing. Kafka transports facts and work between services. R2 stores bytes.

---

## 4. How Kubernetes maps onto KlipCut

```mermaid
flowchart TB
    Internet((Internet)) --> Ingress[Ingress / cloud load balancer]
    Ingress --> APIService[Service: clipper-api]
    APIService --> API1[API pod]
    APIService --> API2[API pod]

    API1 --> PG[(Managed PostgreSQL)]
    API2 --> PG
    API1 --> R2[(Cloudflare R2)]
    API2 --> R2

    WorkerDeployment[Worker Deployment] --> W1[Worker pod]
    WorkerDeployment --> W2[Worker pod]
    PG --> W1
    PG --> W2
    W1 --> R2
    W2 --> R2
    W1 --> External[Deepgram / LLM / video sites]
    W2 --> External

    HPA[HPA] -. adjusts .-> API1
    KEDA[KEDA / queue scaler] -. adjusts .-> WorkerDeployment
    Secrets[Secrets] -. inject .-> API1
    Secrets -. inject .-> W1
```

### Kubernetes object → meaning in this project

| Kubernetes object | KlipCut use |
|---|---|
| `Deployment/clipper-api` | Stateless FastAPI replicas |
| `Service/clipper-api` | Stable internal address and load balancing for API pods |
| `Ingress` | TLS and `/api` routing from the public domain |
| `Deployment/clipper-worker` | Long-running FFmpeg workers |
| `HorizontalPodAutoscaler` | Scale API replicas independently |
| KEDA `ScaledObject` | Scale workers using backlog rather than CPU alone |
| `ConfigMap` | Non-secret environment values |
| `Secret` | Database, R2, provider, auth and payment secrets |
| `emptyDir` volume | Per-pod download/render scratch space |
| `PodDisruptionBudget` | Keep API capacity during voluntary maintenance |
| `CronJob` | Retention cleanup and reconciliation, once exposed as safe commands |
| `Job` | One-off migration or administrative task; not every video job |

Do not create one Kubernetes `Job` object for every clip unless there is a strong operational reason. The application already has job identity, progress, retries and billing rules. Creating cluster objects per user request adds Kubernetes API pressure and makes application state harder to reason about.

---

## 5. Container requirements

There is currently no Dockerfile in this repository; `backend/railpack.json`/`nixpacks.toml` describe the present hosting build. Before deploying to Kubernetes, build one immutable image that contains:

- Python dependencies from `backend/requirements.txt`
- FFmpeg and ffprobe
- yt-dlp (also installed by the Python requirements)
- fonts and the YuNet model under `backend/assets/`
- the application source
- a non-root runtime user

Use the same image digest for API and worker so a queued job cannot be accepted by one application version and interpreted by an unexpectedly different worker version.

An illustrative Dockerfile shape (package names vary by base image) is:

```dockerfile
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ .
RUN useradd --system --uid 10001 clipper \
 && mkdir -p /app/storage \
 && chown -R clipper:clipper /app

USER 10001
ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
```

The worker overrides only `command`/`args`. Do not put secrets in the Dockerfile.

---

## 6. Illustrative Kubernetes manifest

This is a teaching example, not a copy-paste production release. Replace image names, hosts, storage sizing and secret management for the chosen cloud. Managed PostgreSQL and R2 are intentionally outside this manifest.

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: clipper
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: clipper-config
  namespace: clipper
data:
  CLIPPER_INLINE_WORKER: "0"
  STORAGE_BACKEND: "r2"
  CLIPPER_MAX_CONCURRENT_RENDERS: "1"
  CLIPPER_PARALLEL_RENDERS: "1"
  CLIPPER_STALE_CLAIM_MINUTES: "45"
  CLIPPER_MAX_ATTEMPTS: "3"
  CLIPPER_APP_ORIGIN: "https://klipcut.example.com"
  CLIPPER_CORS_ORIGINS: "https://klipcut.example.com"
---
# Demonstration only. In production, sync these from a cloud secret manager.
apiVersion: v1
kind: Secret
metadata:
  name: clipper-secrets
  namespace: clipper
type: Opaque
stringData:
  DATABASE_URL: "postgresql+psycopg2://USER:PASSWORD@HOST:5432/clipper"
  R2_BUCKET: "klipcut"
  R2_ENDPOINT: "https://ACCOUNT.r2.cloudflarestorage.com"
  R2_ACCESS_KEY_ID: "replace-me"
  R2_SECRET_ACCESS_KEY: "replace-me"
  DEEPGRAM_API_KEY: "replace-me"
  OPENAI_API_KEY: "replace-me"
  CLIPPER_SECRET_KEY: "replace-me"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: clipper-api
  namespace: clipper
spec:
  replicas: 2
  selector:
    matchLabels:
      app: clipper-api
  template:
    metadata:
      labels:
        app: clipper-api
    spec:
      terminationGracePeriodSeconds: 30
      containers:
        - name: api
          image: ghcr.io/your-org/clipper:IMAGE_DIGEST
          args: ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
          ports:
            - name: http
              containerPort: 8000
          envFrom:
            - configMapRef:
                name: clipper-config
            - secretRef:
                name: clipper-secrets
          resources:
            requests:
              cpu: 250m
              memory: 384Mi
            limits:
              cpu: "1"
              memory: 1Gi
          readinessProbe:
            httpGet:
              # Add this endpoint before using the manifest; see section 8.5.
              path: /readyz
              port: http
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /healthz
              port: http
            initialDelaySeconds: 15
            periodSeconds: 20
          volumeMounts:
            - name: scratch
              mountPath: /app/storage
      volumes:
        - name: scratch
          emptyDir:
            sizeLimit: 5Gi
---
apiVersion: v1
kind: Service
metadata:
  name: clipper-api
  namespace: clipper
spec:
  selector:
    app: clipper-api
  ports:
    - name: http
      port: 80
      targetPort: http
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: clipper-worker
  namespace: clipper
spec:
  replicas: 2
  selector:
    matchLabels:
      app: clipper-worker
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  template:
    metadata:
      labels:
        app: clipper-worker
    spec:
      # Must be longer than a normal job if graceful completion is expected.
      terminationGracePeriodSeconds: 1800
      containers:
        - name: worker
          image: ghcr.io/your-org/clipper:IMAGE_DIGEST
          args: ["python", "worker.py"]
          envFrom:
            - configMapRef:
                name: clipper-config
            - secretRef:
                name: clipper-secrets
          resources:
            requests:
              cpu: "2"
              memory: 2Gi
              ephemeral-storage: 5Gi
            limits:
              cpu: "2"
              memory: 3Gi
              ephemeral-storage: 10Gi
          volumeMounts:
            - name: scratch
              mountPath: /app/storage
      volumes:
        - name: scratch
          emptyDir:
            sizeLimit: 10Gi
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: clipper-api
  namespace: clipper
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: clipper-api
  minReplicas: 2
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 65
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: clipper-api
  namespace: clipper
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: clipper-api
```

Important details in that example:

- API and worker limits differ because they have different workloads.
- `requests` drive scheduling and autoscaling; `limits` cap a runaway process.
- One worker pod processes one application job. FFmpeg already consumes its allocated CPU.
- `CLIPPER_PARALLEL_RENDERS=1` is conservative in a 2-vCPU container. The current default is `3`; load-test before increasing it.
- Scratch space is explicitly bounded. A large input plus source, audio, previews and outputs can otherwise evict the pod.
- The worker gets a long termination grace period. On SIGTERM, `worker.py` stops claiming new work and tries to finish its current job.
- If Kubernetes kills it after the grace period, its database claim becomes stale and another worker retries it.

Ingress controllers often default to request body and timeout limits that are too small for multi-GB video uploads. The better long-term design is browser → presigned multipart upload → R2, followed by a small API call containing the object key. Until then, configure the selected ingress for the current upload limits and do not assume all controllers use the same annotations.

---

## 7. Autoscaling: API and worker are different problems

### API scaling

API pods do short HTTP/database work, so useful signals include:

- requests per second
- p95/p99 latency
- CPU
- active connections
- PostgreSQL connection-pool saturation

An HPA based on CPU is a reasonable start. Do not add API replicas blindly: every replica owns a database connection pool, so PostgreSQL/PgBouncer must be sized for the total.

### Worker scaling

Worker CPU is high whenever a render is active, but CPU does not describe **backlog**. The important values already exist at `GET /api/health`:

```json
{"queue": {"waiting": 12, "running": 2, "total": 14}}
```

A useful first calculation is:

```text
arrival rate  λ = new jobs per minute
service rate  μ = jobs completed by one worker per minute
workers needed ≈ ceil(λ / μ) + safety headroom
```

Example: if a job averages 4 minutes, one worker provides `μ = 0.25 jobs/minute`. At 1 new job/minute, 4 workers merely keep up; 5–6 provide headroom.

For the current PostgreSQL queue, KEDA can scale from a SQL query such as `SELECT count(*) FROM job_queue WHERE claimed_at IS NULL AND available_at <= now()`. Treat the following as pseudocode because KEDA trigger fields depend on the installed scaler/version:

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: clipper-worker
  namespace: clipper
spec:
  scaleTargetRef:
    name: clipper-worker
  minReplicaCount: 1
  maxReplicaCount: 20
  cooldownPeriod: 300
  triggers:
    - type: postgresql
      metadata:
        connectionFromEnv: DATABASE_URL
        query: >-
          SELECT count(*) FROM job_queue
          WHERE claimed_at IS NULL AND available_at <= now()
        targetQueryValue: "2"
```

Keep one worker minimum with the current design because workers perform stale-claim reaping. If scaling to zero is desired, move reaping/reconciliation into a CronJob or dedicated controller first.

Avoid rapid scale up/down. A worker that is processing a 20-minute video should not be killed because the *waiting* count briefly reached zero. Use a cooldown, long termination grace, and stabilization window.

---

## 8. Changes required before safely running several API pods

These are more important than writing YAML.

### 8.1 Fix startup recovery semantics

`backend/app/main.py::_recover_orphaned_jobs()` currently marks all jobs in `queued`, `downloading`, `transcribing`, `analyzing`, or `rendering` as failed whenever an API process starts.

That assumption is valid only for the local inline-worker model. In Kubernetes, an API pod can restart while a separate healthy worker still owns the job. A new API replica must not fail global work during startup.

Before horizontal API scaling:

- Run that recovery only when `CLIPPER_INLINE_WORKER=1`, or remove it in split mode.
- In split mode, let queue claims, claim heartbeats/leases and a reconciler decide whether work was abandoned.
- Make the reconciliation operation idempotent and observable.

### 8.2 Move editor export and rerender onto the queue

`POST /jobs/{job_id}/clips/{index}/export` and `/rerender` still execute FFmpeg on the API request thread. This causes long HTTP requests, competes with API traffic, and makes API resource sizing unpredictable.

Model exports as durable work items, return `202 Accepted` plus an operation ID, and let the UI poll or receive progress. The worker should fetch `source.mp4` from R2, render, publish, and atomically update the clip record.

### 8.3 Make proxy creation durable

Preview proxies are currently produced by a daemon thread launched inside the pipeline. If the worker exits after publishing clips but before that thread finishes, proxies can be missing forever. Either make proxy generation a recorded work item or allow an idempotent on-demand rebuild.

### 8.4 Use real schema migrations

`init_db()` calls SQLAlchemy `create_all()`. That creates missing tables but cannot safely evolve existing columns and constraints. Introduce Alembic migrations and run them once per release with a Kubernetes Job or CI/CD step—not concurrently in every API pod.

### 8.5 Separate liveness, readiness and diagnostics

- **Liveness:** is the process/event loop alive? It should not depend on PostgreSQL or R2, otherwise a database outage restarts every pod and makes recovery worse. `/healthz` is close to this.
- **Readiness:** can this pod serve real traffic? Add `/readyz`, check required local initialization and, cautiously, dependencies, and return a non-2xx response when not ready. Kubernetes checks the HTTP status, not an `{"ok": false}` JSON field.
- **Diagnostics:** queue depth, storage backend, version, dependency state. `/api/health` currently mixes some of these and queries the queue.

### 8.6 Schedule maintenance explicitly

Retention expiry currently runs on startup and when job lists are read. In a cluster, make retention cleanup and job reconciliation explicit scheduled commands invoked by CronJobs. Use a database advisory lock or another lease so overlapping runs remain safe.

### 8.7 Treat billing and publication as idempotent

Retries are normal. Charging, refunding, publishing and status transitions must be safe when repeated. Use unique idempotency keys/constraints such as `(job_id, operation_type)` and transactions around related database changes. Never rely on “this function normally runs once.”

### 8.8 Version job payloads

Jobs store frozen options, which is good. Add a `job_schema_version`/`pipeline_version`, and keep workers backward compatible during rolling deploys. Old queued jobs can otherwise be interpreted using new defaults or removed fields.

---

## 9. Kafka: the mental model

Kafka is a durable, ordered, partitioned log. Producers append records; consumers read them using offsets.

```mermaid
flowchart LR
    P1[API producer] --> T{{Topic}}
    T --> P0[Partition 0]
    T --> P1X[Partition 1]
    T --> P2[Partition 2]
    P0 --> CG1[render consumer group]
    P1X --> CG1
    P2 --> CG1
    P0 --> CG2[analytics consumer group]
    P1X --> CG2
    P2 --> CG2
```

Core concepts mapped to KlipCut:

| Kafka concept | Meaning for KlipCut |
|---|---|
| Topic | A named stream such as `clipper.jobs.requested.v1` |
| Record/event | A small JSON/Avro/Protobuf message; never the video bytes |
| Key | Usually `job_id`; events for one job land on the same partition |
| Partition | Unit of ordering and parallelism |
| Offset | Consumer's position in one partition |
| Consumer group | Replicas sharing one logical job, e.g. render workers |
| Different consumer groups | Each receives the event independently, e.g. renderer and analytics |
| Retention | Events remain replayable for a configured period even after consumption |
| At-least-once delivery | A consumer may process an event again after failure |
| DLQ | A separate topic for messages that exhaust retry policy |

With 12 partitions and one render consumer group, at most 12 consumers can actively read one partition each at the same moment. The 13th consumer is idle. Partitions therefore set the parallelism ceiling, but adding too many partitions also adds operational overhead.

Never place videos, transcripts or large recipes directly in Kafka. Put them in R2/PostgreSQL and publish identifiers plus metadata.

---

## 10. The safe Kafka design: database + outbox + events

A dangerous implementation is:

1. Insert job in PostgreSQL.
2. Publish Kafka message.

If the process dies between those steps, the database has a job that no worker hears about. Reversing the order produces a message for a job that may not exist.

Use the **transactional outbox pattern**:

```mermaid
sequenceDiagram
    actor Browser
    participant API
    participant PG as PostgreSQL
    participant Relay as Outbox relay / CDC
    participant K as Kafka
    participant Worker
    participant R2

    Browser->>API: Submit job
    API->>PG: BEGIN
    API->>PG: INSERT jobs
    API->>PG: INSERT outbox(job.requested)
    API->>PG: COMMIT
    API-->>Browser: {job_id}

    Relay->>PG: Read unpublished outbox rows
    Relay->>K: Produce job.requested
    Relay->>PG: Mark outbox row published

    Worker->>K: Poll event
    Worker->>PG: Claim/idempotency check
    Worker->>R2: Read source, publish outputs
    Worker->>PG: Transactionally mark done + add completion outbox event
    Worker->>K: Commit consumer offset
```

The database transaction makes “job exists” and “event must eventually be published” one atomic decision. The relay can publish an event twice if it crashes before marking the outbox row, so consumers still must be idempotent.

Kafka's exactly-once features do not make Deepgram calls, FFmpeg work, R2 uploads or PostgreSQL billing magically exactly once. Design for **at-least-once delivery plus idempotent effects**.

### Suggested event envelope

```json
{
  "event_id": "01J...",
  "event_type": "clipper.job.requested.v1",
  "occurred_at": "2026-08-27T12:34:56Z",
  "job_id": "uuid",
  "user_id": "uuid",
  "correlation_id": "uuid",
  "causation_id": null,
  "payload": {
    "input_type": "upload",
    "source_key": "uuid/source.mp4",
    "pipeline_version": 1,
    "priority": 10
  }
}
```

- `event_id` is the deduplication key.
- `job_id` is the Kafka record key and trace correlation key.
- `event_type` includes a schema version.
- The database remains authoritative for the complete frozen job options; the event can carry only routing information.
- Do not include provider secrets or signed R2 URLs. Signed URLs expire; object keys do not.

---

## 11. Kafka architecture choices

### Choice A — Kafka only replaces queue notification

```mermaid
flowchart LR
    API -->|job + outbox| PG[(PostgreSQL)]
    Relay --> Kafka{{jobs.requested}}
    PG --> Relay
    Kafka --> Workers[Monolithic pipeline workers]
    Workers --> Providers[Deepgram / LLM / FFmpeg]
    Workers --> R2[(R2)]
    Workers --> PG
```

This is the smallest migration. The existing `run_queued_job` remains mostly intact, while Kafka wakes workers instead of a two-second database poll. It adds operational cost but not much new product capability, so use it only when polling/locking or fan-out is a real issue.

### Choice B — split the pipeline into stage consumers

```mermaid
flowchart LR
    A[API] --> JR{{job.requested}}
    JR --> D[Download service]
    D -->|source in R2| SR{{source.ready}}
    SR --> T[Transcription service]
    T -->|transcript in DB/R2| TR{{transcript.ready}}
    TR --> N[Analysis service]
    N --> CR{{clips.planned}}
    CR --> R[Render workers]
    R -->|MP4s| R2[(R2)]
    R --> CD{{clips.completed}}
    CD --> Notify[Notification service]

    A --> PG[(PostgreSQL)]
    D --> PG
    T --> PG
    N --> PG
    R --> PG
```

Benefits:

- Transcription and rendering scale independently.
- A provider rate limit does not consume render capacity.
- Each stage can retry and expose its own latency.
- Analytics/notifications can subscribe without changing the renderer.

Costs:

- More services, schemas, deployments, dashboards and failure states.
- A workflow is no longer one Python call stack; state transitions must be explicit.
- Cancellation, timeout, billing/refund and partial completion become a distributed workflow (often called a saga).
- Debugging requires correlation IDs and distributed traces.

For KlipCut, use stage splitting only after measurements show the monolithic worker wastes capacity or cannot meet provider/compute limits.

### Possible topics

| Topic | Producer | Primary consumer group | Key |
|---|---|---|---|
| `clipper.jobs.requested.v1` | Outbox relay | pipeline orchestrators | `job_id` |
| `clipper.sources.ready.v1` | downloader | transcribers | `job_id` |
| `clipper.transcripts.ready.v1` | transcriber | analyzers | `job_id` |
| `clipper.clips.planned.v1` | analyzer | renderers | `job_id` |
| `clipper.clips.completed.v1` | renderer | notifications | `job_id` |
| `clipper.jobs.failed.v1` | any stage/outbox | refund + notification workflows | `job_id` |
| `clipper.jobs.retry.v1` | retry scheduler | relevant stage | `job_id` |
| `clipper.jobs.dlq.v1` | retry handler | operators/replay tool | `job_id` |

Keep domain events (“clip completed”) separate from retry mechanics (“try render again in five minutes”).

---

## 12. Kafka processing rules that prevent expensive mistakes

### Commit offsets only after durable effects

For a render event:

1. Read event.
2. Check whether this stage/event is already completed.
3. Acquire an idempotent database claim/lease.
4. Download source to scratch.
5. Render to a temporary filename.
6. Validate with ffprobe.
7. Upload to a deterministic R2 key.
8. Transactionally update PostgreSQL and insert completion event in outbox.
9. Commit the Kafka offset.

If the worker dies after step 7 but before step 9, Kafka redelivers. The second run should detect the completed database operation or safely overwrite the deterministic object key—not charge the user twice.

### Long-running consumers need special care

A render may take longer than Kafka's consumer poll interval. If the client stops polling for too long, Kafka can remove it from the group and assign the partition to another worker, producing duplicate work.

Options include:

- Configure the client poll/processing interval above the worst expected duration and keep heartbeats healthy.
- Poll on one thread and process on a controlled executor while pausing assigned partitions.
- Use Kafka to start a durable workflow and let a workflow engine/worker lease own the long task.
- Split long work into smaller idempotent stage messages.

Do not solve this only by setting a huge timeout without monitoring stuck jobs; that delays failure recovery.

### Retry categories

| Failure | Example | Action |
|---|---|---|
| Permanent input error | Private/unreadable video, no speech | Mark failed; do not retry |
| Temporary provider error | 429/503, network timeout | Exponential backoff + jitter |
| Worker/infrastructure loss | OOM, node termination | Lease expires/redelivery |
| Poison message/code bug | Same event crashes repeatedly | DLQ after bounded attempts |
| Partial side effect | R2 upload succeeded, DB write failed | Idempotent replay/reconcile |

Avoid retrying immediately on the main topic in a tight loop. Use retry topics with increasing delays or a retry scheduler, then DLQ after the limit. A DLQ is not a trash can: alert on it and provide a controlled inspect/replay tool.

### Ordering and priority

Kafka guarantees ordering only within one partition. Keying by `job_id` orders events for that job but does not create global FIFO.

The current database queue supports Pro priority with `ORDER BY priority DESC`. Kafka does not natively reorder records within a partition. Common options are separate `jobs.priority` and `jobs.standard` topics with reserved worker capacity, or a scheduler that dispatches fairly. Prevent permanent starvation of standard jobs.

---

## 13. Scaling Kafka consumers with KEDA

Kafka worker autoscaling uses **consumer lag**: produced records minus the consumer group's committed offsets.

```mermaid
flowchart LR
    K[Kafka partition end offsets] --> L[Consumer lag]
    C[Committed group offsets] --> L
    L --> KEDA[KEDA]
    KEDA -->|desired replicas| D[Render Deployment]
```

Illustrative shape:

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: clipper-render-worker
  namespace: clipper
spec:
  scaleTargetRef:
    name: clipper-render-worker
  minReplicaCount: 0
  maxReplicaCount: 24        # no more useful than topic partitions
  pollingInterval: 15
  cooldownPeriod: 600
  triggers:
    - type: kafka
      metadata:
        bootstrapServers: kafka.example.internal:9092
        consumerGroup: clipper-render-v1
        topic: clipper.clips.planned.v1
        lagThreshold: "1"
        offsetResetPolicy: earliest
      authenticationRef:
        name: clipper-kafka-auth
```

Exact authentication settings depend on the Kafka provider (SASL/SSL, IAM, certificates). Keep brokers private, use TLS, grant each producer/consumer only its required topic permissions, and rotate credentials.

---

## 14. Failure behavior in Kubernetes

| Event | Expected behavior |
|---|---|
| API pod dies after returning `job_id` | Job remains in PostgreSQL; worker still processes it |
| API pod dies during direct upload | Client retries multipart parts; no job is queued until object is complete |
| Worker gets SIGTERM | Stops claiming, finishes current job within grace period |
| Worker is OOM-killed | Claim/lease expires; another worker retries |
| Worker uploads clip then dies | Replay sees deterministic key/idempotency record and completes state transition |
| PostgreSQL unavailable | API becomes unready; workers back off without losing queued rows |
| R2 unavailable | Do not mark job `done`; retry publication safely |
| Deepgram/LLM rate-limits | Backoff; cap concurrency separately from CPU scaling |
| Kafka unavailable | Outbox accumulates safely in PostgreSQL; relay catches up later |
| Kafka sends duplicate | Consumer deduplicates using `event_id`/stage state |
| Node disk fills | Worker fails clearly; pod scratch request/limit and alert prevent silent corruption |

The desired state machine should reject impossible or backward transitions. For example, only a worker holding the active lease should move `rendering → done`, and `done` should only be written after R2 objects are confirmed.

---

## 15. Observability: how to know what to scale

Use a `job_id`/`correlation_id` on every log, metric and trace.

### Metrics

- API request rate, errors and latency by route/status
- PostgreSQL pool usage and query latency
- waiting/running queue depth or Kafka consumer lag
- oldest waiting job age (often more useful than count)
- jobs started/completed/failed/retried by reason
- duration of download, transcription, analysis, render and publish stages
- FFmpeg CPU, peak memory, scratch disk and output size
- provider latency, 429s and spend
- credits charged/refunded and reconciliation mismatches
- worker desired/ready/busy replicas

### Logs

Prefer structured JSON with fields such as:

```json
{"level":"info","event":"render.completed","job_id":"...","clip_index":1,"duration_ms":84213,"worker":"pod-name","attempt":2}
```

Never log API keys, bearer tokens, full signed URLs or unnecessary transcript/user content.

### Traces

Propagate trace/correlation context from HTTP submission → outbox → Kafka headers → each worker stage. A distributed trace should explain where a job spent its time without searching five unrelated log streams.

### Alerts

Alert on user impact, not merely pod restarts:

- oldest queued job above the target wait time
- failure/DLQ rate above baseline
- no completions while backlog is nonzero
- PostgreSQL/R2/provider errors
- scratch disk or memory near limits
- billing reconciliation differences

---

## 16. Security and operations checklist

- Keep PostgreSQL and Kafka on private networking where possible.
- Require TLS for public ingress, PostgreSQL, Kafka and object storage.
- Store secrets outside Git; use an external secret manager in production.
- Give API and worker separate service accounts and minimum permissions.
- Use presigned R2 uploads/downloads with short expiry and scoped object keys.
- Validate upload type, size and ownership server-side; filenames are not trust boundaries.
- Apply network policies after mapping required egress to R2, PostgreSQL and providers.
- Scan the image and pin the deployed image by digest.
- Back up PostgreSQL and regularly test restore, not only backup creation.
- Set object lifecycle/retention rules as defense in depth, while retaining application cleanup.
- Use rolling/canary deploys and ensure old/new workers understand queued payload versions.
- Load-test with realistic video sizes; a tiny HTTP benchmark says little about FFmpeg capacity.

---

## 17. A practical implementation order for this repository

```mermaid
flowchart TD
    A[1. Managed PostgreSQL + R2] --> B[2. Separate API and worker]
    B --> C[3. Container image + local container test]
    C --> D[4. Fix startup recovery and move exports]
    D --> E[5. Add migrations, probes, metrics, scheduled maintenance]
    E --> F[6. Deploy API + fixed worker count to Kubernetes]
    F --> G[7. Load test and measure stage time / queue age]
    G --> H[8. Add HPA + KEDA for DB queue]
    H --> I{Measured need for event backbone?}
    I -->|No| J[Keep the simpler database queue]
    I -->|Yes| K[9. Add outbox + Kafka notification]
    K --> L[10. Split stages only where measurements justify it]
```

Concrete milestones:

1. **Prove durability:** kill a worker mid-render and show the job is retried without a second charge.
2. **Prove stateless API:** run two API replicas, upload through one, poll/download through the other.
3. **Prove rollout safety:** deploy while jobs are queued and running; none should be incorrectly failed.
4. **Prove capacity model:** record average and p95 job duration, memory and scratch use for representative inputs.
5. **Add backlog scaling:** scale workers from oldest-job age/queue depth, with a strict maximum based on provider and budget limits.
6. **Only then evaluate Kafka:** add an outbox and one consumer path behind a feature flag; compare reliability and operational complexity.

---

## 18. Learning exercises

These experiments make the concepts concrete without requiring Kafka first.

### Exercise A — see the existing queue work

Run API and worker separately with PostgreSQL/R2-compatible settings:

```bash
cd backend
CLIPPER_INLINE_WORKER=0 uvicorn app.main:app --port 8000
```

In another terminal:

```bash
cd backend
python worker.py
```

Submit multiple jobs, inspect `/health`, stop a worker during one job, and observe the claim/retry rules. For a quick exercise, lower the stale window only in a disposable environment.

### Exercise B — prove filesystems are not shared

Run API and worker in separate containers with local storage. An upload will be present only in the API container. Then configure R2 and repeat: the worker can fetch `job_id/source.mp4` regardless of which pod accepted it. This demonstrates why object storage is a scaling requirement, not a convenience.

### Exercise C — consumer groups with toy events

Before integrating the real pipeline, create a small local Kafka topic with three partitions. Run two consumers with the same group and observe that they divide records; run a third consumer with a different group and observe that it receives every record independently. Key messages by a fake `job_id` and confirm per-job order.

### Exercise D — duplicate delivery

Have a toy consumer perform a deterministic database insert, then crash before committing its offset. On restart the event arrives again. Add a unique `event_id` constraint and show that the second delivery becomes a no-op. This is the exact idea needed to protect credits and R2 publication.

### Exercise E — autoscaling math

Record 20 representative job durations. Calculate arrival rate, service rate and required workers. Then compare the calculation with queue growth during a controlled burst. Autoscaling should be based on this data, not a guessed replica count.

---

## Final architecture recommendation

For the next real scaling step, use:

```text
Static React/CDN
      ↓
Ingress → 2+ FastAPI pods ──────────┐
               ↓                    │
        Managed PostgreSQL          │
          (jobs + queue)            │
               ↓                    │
       KEDA-scaled workers ─────────┤
               ↓                    ↓
        Deepgram / LLM / FFmpeg → Cloudflare R2 → browser
```

That architecture matches the code already present and keeps the number of moving parts understandable. Kafka is the next tool when KlipCut has a measured need for event fan-out, replay, or independently scaled stages. When that time comes, introduce it through a transactional outbox and preserve PostgreSQL as the source of truth.

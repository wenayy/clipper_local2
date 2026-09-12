# Deploying KlipCut on AWS ($200-credit friendly)

The whole app runs on **one EC2 instance** with Docker Compose: Postgres, the
API, and one or more render workers. Video files go to **Cloudflare R2**, and
**Cloudflare** sits in front for DNS, TLS, and Turnstile. The React frontend is
built once and hosted free on **Cloudflare Pages**.

```
Cloudflare Pages          Cloudflare (DNS + TLS + Turnstile)
  frontend  ───────────────────────▶  EC2 instance
                                        ├── api      (FastAPI, port 8000)
                                        ├── worker×N (python worker.py)
                                        └── db        (Postgres)
                                              │
                                        Cloudflare R2  (clips, sources, proxies)
```

Why one box: with $200 of credits, a single `c7i.xlarge` (4 vCPU / 8 GB, ~$0.17/hr)
runs the API plus two workers comfortably and lasts ~7 weeks on credits alone.
When you outgrow it, the same images move to ECS or the k8s manifests in
`deploy/kubernetes-learning/` unchanged.

---

## 0. One-time accounts

- **Cloudflare**: a domain (or move DNS to Cloudflare), an R2 bucket, a Turnstile site.
- **Polar**: your one product's Checkout Link + Product ID (already wired in
  `backend/app/polar_catalog.py`), an access token, and a webhook secret.
- **AWS**: credits applied to the account.

## 1. Cloudflare R2 (object storage)

1. Cloudflare dashboard → **R2** → *Create bucket* → e.g. `klipcut`.
2. **R2 → Manage API Tokens → Create API Token** (Object Read & Write). Copy the
   Access Key ID and Secret.
3. Your endpoint is `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`.

Fill these into `.env` (next step): `R2_BUCKET`, `R2_ENDPOINT`, `R2_ACCESS_KEY_ID`,
`R2_SECRET_ACCESS_KEY`, and `STORAGE_BACKEND=r2`.

## 2. Cloudflare Turnstile

1. Cloudflare → **Turnstile → Add site** → add your domain (and `localhost` for testing).
2. Copy the **Site Key** → `TURNSTILE_SITE_KEY`, **Secret Key** → `TURNSTILE_SECRET_KEY`.

The backend serves the site key at `/api/turnstile-site-key`; the frontend picks
it up automatically. Leave both blank to disable the challenge entirely.

## 3. Dodo Payments billing

Billing runs on **Dodo Payments**. Credit top-ups use ONE dynamic
"Pay What You Want" product (the price is passed per checkout); Creator/Pro
plans are fixed-price subscription products, one per tier.

1. Create a **one-time product** in Dodo with **Pay What You Want** enabled and a
   min/max covering your smallest and largest credit pack. Copy its product id
   into `DODO_TOPUP_PRODUCT_ID`.
2. Create one **subscription product per plan tier** (prices are listed as
   comments in `backend/app/dodo_catalog.py`) and paste each product id into the
   matching row of `SUBSCRIPTIONS` there. A blank row shows "not available yet"
   instead of charging a wrong price.
3. Set on the server:
   - `DODO_PAYMENTS_API_KEY` — Dodo → Developer → API Keys.
   - `DODO_PAYMENTS_WEBHOOK_KEY` — from the webhook you create in step 7.
   - `DODO_PAYMENTS_ENVIRONMENT` — `live_mode` (or `test_mode` while testing).
   - `CLIPPER_APP_ORIGIN` — your frontend URL, so buyers return after paying.

## 4. Launch the EC2 instance

1. EC2 → Launch instance → **Ubuntu 24.04**, type **c7i.xlarge**, 30 GB gp3 disk.
2. Security group inbound: **22** (your IP), **80**, **443**. (App port 8000 stays
   internal; Cloudflare talks to it over 443 via the reverse proxy in step 5.)
3. Allocate an **Elastic IP** and associate it, so the address survives reboots.
4. SSH in and install Docker:

   ```bash
   curl -fsSL https://get.docker.com | sudo sh
   sudo usermod -aG docker $USER && newgrp docker
   git clone <your-repo-url> klipcut && cd klipcut/deploy/aws
   cp .env.example .env
   nano .env        # fill every value (see .env.example)
   ```

   Generate secrets: `openssl rand -hex 32` for `CLIPPER_SECRET_KEY`, and a long
   random `POSTGRES_PASSWORD`.

5. Bring it up with two workers:

   ```bash
   docker compose --env-file .env up -d --build --scale worker=2
   docker compose logs -f api worker      # watch it boot; Ctrl-C to stop watching
   curl localhost:8000/healthz            # {"ok": true}
   curl localhost:8000/api/health         # queue depth
   ```

   The API log should print the storage driver as **r2** (not local). A worker
   that prints "storage is LOCAL" can't see the API's files — fix its R2 vars.

## 5. TLS + domain via Cloudflare

Simplest path (Cloudflare Tunnel — no open ports, no certs to manage):

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared
sudo chmod +x /usr/local/bin/cloudflared
cloudflared tunnel login
cloudflared tunnel create klipcut
# Route your API hostname to the local API:
cloudflared tunnel route dns klipcut api.yourdomain.com
# Point the tunnel at localhost:8000 in ~/.cloudflared/config.yml, then:
sudo cloudflared service install
```

Alternative (Caddy on the box): put Caddy in front of `localhost:8000`, point an
A record at your Elastic IP through Cloudflare (proxied, orange cloud), and Caddy
auto-issues Let's Encrypt certs.

Either way, `https://api.yourdomain.com/api/health` should now answer.

## 6. Frontend on Cloudflare Pages

1. Cloudflare → **Workers & Pages → Create → Pages → Connect to Git** → pick this repo.
2. Build settings:
   - **Build command**: `npm run build`
   - **Build output directory**: `frontend/dist`
   - **Root directory**: `frontend`
   - **Environment variable**: `VITE_API_BASE = https://api.yourdomain.com/api`
3. Deploy, then map your app domain (e.g. `klipcut.yourdomain.com`) to the Pages project.
4. Set `CLIPPER_APP_ORIGIN` in `.env` to that URL and `docker compose up -d` again.

## 7. Wire the Dodo webhook

1. Dodo → **Developer → Webhooks → Add endpoint**:
   `https://api.yourdomain.com/api/billing/dodo/webhook`
2. Subscribe to `payment.succeeded` and the `subscription.*` events
   (`subscription.active`, `subscription.renewed`, `subscription.cancelled`,
   `subscription.expired`, `subscription.failed`, `subscription.on_hold`).
3. Copy the webhook signing key into `DODO_PAYMENTS_WEBHOOK_KEY`, then restart.
4. Test a real purchase with `DODO_PAYMENTS_ENVIRONMENT=test_mode` first; watch
   `docker compose logs -f api` for the event → credits granted.

---

## How checkout maps to Dodo

- **Credit top-ups** are one-time payments through the single
  `DODO_TOPUP_PRODUCT_ID` product; the exact price is passed as `amount` and the
  credits to grant travel in the checkout metadata. Add a pack in
  `billing.TOPUPS` and it works with no new product.
- **Creator/Pro plans** are fixed subscription products, one per tier, pasted
  into `SUBSCRIPTIONS` in `backend/app/dodo_catalog.py`. Dodo (like every
  processor) only allows dynamic amounts on one-time products, so recurring
  plans must be fixed-price products.
- Fulfillment is exactly-once: every charge is recorded in `dodo_grants` before
  the balance moves, so a webhook redelivery — or the confirm endpoint racing
  the webhook — can never double-credit.

## Scaling & operations

- **Add render capacity**: `docker compose up -d --scale worker=3`. Watch
  `/api/health` — `waiting` climbing while `running` is flat means add a worker.
- **Deploy an update**: `git pull && docker compose up -d --build`.
- **Backups**: `docker compose exec db pg_dump -U klipcut klipcut > backup.sql`.
  R2 holds the media; Postgres holds jobs, users, and the credit ledger.
- **Costs on credits**: EC2 `c7i.xlarge` ≈ $125/mo, R2 storage ≈ $0.015/GB/mo
  with no egress fees, Cloudflare Pages + Turnstile free. Stop the instance when
  idle to stretch the $200.

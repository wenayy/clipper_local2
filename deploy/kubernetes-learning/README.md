# KlipCut Kubernetes learning deployment

This directory is a **design you can study and adapt**, not a claim that the
current application is production-ready for Kubernetes. Read
[`../../docs/KUBERNETES_DEEP_DIVE.md`](../../docs/KUBERNETES_DEEP_DIVE.md)
before applying it. That guide explains every object, the traffic flow, scaling,
rollouts, failures, and the application changes still required.

## File map

```text
deploy/kubernetes-learning/
├── 00-namespace.yaml       isolated name scope
├── 01-configmap.yaml       non-secret runtime settings
├── 02-secret.example.yaml  placeholders only; never commit real values
├── 03-service-accounts.yaml
├── 10-api.yaml             FastAPI Deployment + internal Service
├── 11-worker.yaml          FFmpeg worker Deployment (no Service)
├── 12-frontend.yaml        static React/Nginx Deployment + Service
├── 20-ingress.yaml         public host, /api and / routing, TLS
├── 30-api-hpa.yaml         API CPU autoscaling
├── 31-worker-keda.yaml     optional PostgreSQL-queue autoscaling
├── 40-pdb.yaml             voluntary-disruption protection
├── images/                 backend/frontend image build examples
└── kustomization.yaml      applies the non-optional resources together
```

`31-worker-keda.yaml` is intentionally not in `kustomization.yaml`: it requires
KEDA to be installed, and the current worker's stale-claim reaper means scaling
workers to zero needs a separate reconciler first.

## Before applying

1. Build backend and frontend container images and replace both `IMAGE_DIGEST`
   placeholders. The backend image must include FFmpeg/ffprobe and all assets.
2. Provision PostgreSQL and R2 outside the cluster.
3. Copy `02-secret.example.yaml` to a temporary ignored location, substitute
   real values, and apply it—or preferably sync secrets from a cloud secret
   manager. Never commit the populated file.
4. Replace `klipcut.example.com`, R2 values, model names, and the TLS issuer.
5. Install an Ingress controller. The included annotations are specifically
   for ingress-nginx.
6. Install cert-manager only if using its annotation in `20-ingress.yaml`.
7. Fix the multi-pod application blockers documented in the deep dive,
   especially startup recovery and synchronous editor exports.

Example local image builds from the repository root:

```bash
docker build -f deploy/kubernetes-learning/images/Dockerfile.backend -t klipcut-backend:dev .
docker build -f deploy/kubernetes-learning/images/Dockerfile.frontend -t klipcut-frontend:dev .
```

## Render the combined configuration without changing a cluster

```bash
kubectl kustomize deploy/kubernetes-learning
```

## Apply after prerequisites are ready

```bash
kubectl apply -f /safe/local/path/clipper-secrets.yaml
kubectl apply -k deploy/kubernetes-learning
```

## Observe it

```bash
kubectl -n clipper get deploy,pods,svc,ingress,hpa,pdb
kubectl -n clipper rollout status deployment/clipper-api
kubectl -n clipper logs deployment/clipper-api --all-pods=true --tail=100
kubectl -n clipper logs deployment/clipper-worker --all-pods=true --tail=100
kubectl -n clipper describe pod POD_NAME
```

# Local testing plans

Three tiers, cheapest first. Each tier catches a different class of problem, so
run them in order and stop at the tier that covers your change:

| Tier | What it proves | Cost | Time |
|---|---|---|---|
| 1. Unit tests | App logic: rate limits, degraded mode, headers, metrics | venv only | ~1 min |
| 2. Docker Compose | Real image + gunicorn + real Redis, incl. outage behaviour | ~100 MB RAM | ~5 min |
| 3. kind cluster | Full k8s stack: probes, nginx→HAProxy→app, NetworkPolicies, Prometheus/Grafana | ~1.5–2 GB RAM | ~20 min |

Host-specific notes (this workstation):

- Ports 5000, 8080, 3000 and 9090 are taken on the host. This plan uses
  **5055** (app), **18080** (nginx), **13000** (Grafana), **19090** (Prometheus),
  **56379** (Redis), all bound to `127.0.0.1`.
- Two Docker daemons exist (apt `docker` and `snap.docker`, which hosts the
  shared `fleet` kind cluster). The `docker` CLI here talks to the apt daemon.
  Never stop `snap.docker`, and never point commands at `kind-fleet` or any
  other existing kube context — always pass `--context kind-sm-gitops`.
- Tiers 2 and 3 count as Docker work under the workspace contract: lease
  `docker` in `~/coordination/LOCKS.md`, post a note to
  `~/coordination/messages.jsonl`, and defer tier 3 if swap is above ~20 GB
  or load is above 3.

---

## Tier 1 — Unit tests

**Goal:** fast check of `src/server/app.py` against `fakeredis`.

1. Create a venv and install dev deps:
   ```bash
   cd src/server && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
   ```
2. Run the suite:
   ```bash
   cd src/server && .venv/bin/pytest -q
   ```
3. **Pass criteria:** all tests green, no warnings about unclosed Redis clients.

`.venv/` must stay out of git (check `.gitignore` covers it before committing).

---

## Tier 2 — Docker Compose smoke stack

**Goal:** run the production image exactly as CI builds it, against a real
password-protected Redis, and exercise the degraded paths.

### Setup

1. Lease `docker` (see host notes).
2. Extend `docker-compose.test.yml` with an `app` service built from the repo
   `Dockerfile`, on the same compose network as `redis`:
   - `build: .`, `image: sm-app:local`
   - `ports: ["127.0.0.1:5055:5000"]`
   - env: `REDIS_HOST=redis`, `REDIS_PORT=6379`, `REDIS_PASSWORD=${SM_REDIS_PASSWORD}`,
     `APP_VERSION=local`, `VISITS_RATE_LIMIT=5` (low so the limiter is easy to hit)
   - `mem_limit: 256m`, `cpus: 0.5` (mirrors the k8s limits)
   - `depends_on: [redis]` — no health condition, so the app also boots with Redis down
3. Give `redis` a password to mirror prod: add
   `--requirepass ${SM_REDIS_PASSWORD}` to its `command`.
4. Generate a throwaway password into an untracked `.env` (compose reads it
   automatically):
   ```bash
   echo "SM_REDIS_PASSWORD=$(openssl rand -hex 16)" > .env && chmod 600 .env
   ```
   Confirm `.env` is in `.gitignore`.

### Run

```bash
docker compose -f docker-compose.test.yml up -d --build
```

### Test checklist

| # | Action | Expected |
|---|---|---|
| 1 | `curl -s 127.0.0.1:5055/live` | `200 {"status":"ok"}` |
| 2 | `curl -s 127.0.0.1:5055/health` | `200`, `redis: ok` |
| 3 | `curl -s 127.0.0.1:5055/api/status` | `version: local`, `redis.connected: true` |
| 4 | `curl -s -X POST 127.0.0.1:5055/api/visits` ×6 | first 5 return counts, 6th `429` with `Retry-After` |
| 5 | Open `http://127.0.0.1:5055/` | UI loads, static assets 200 |
| 6 | `curl -sI 127.0.0.1:5055/` | security headers present |
| 7 | `curl -s 127.0.0.1:5055/static/../app.py` | `404` (no traversal) |
| 8 | `curl -s 127.0.0.1:5055/metrics` | `redis_up 1`, `http_requests_total` series |
| 9 | `docker compose -f docker-compose.test.yml stop redis` | — |
| 10 | Repeat 1–4 | `/live` 200; `/health` 503; `/api/status` 200 with `connected: false`; POST visits 503 |
| 11 | `/metrics` | `redis_up 0` |
| 12 | Start Redis again, repeat 2–3 | recovers without restarting the app |
| 13 | Wrong password: set a different `REDIS_PASSWORD` on app, recreate | behaves like checklist row 10 (degraded, not crashing) |

### Teardown

```bash
docker compose -f docker-compose.test.yml down
```
Release the `docker` lease.

---

## Tier 3 — Full stack on a dedicated kind cluster

**Goal:** apply `k8s/` as Argo CD would, minus Argo CD itself, and verify the
infrastructure changes (probes, NetworkPolicies, proxy chain, monitoring auth).

Argo CD is deliberately skipped: it adds ~1 GB and only tests GitOps sync, which
production already covers. Optional step 10 adds it if needed.

### Prerequisites

- Lease `docker`; check `~/coordination/RESOURCES.md` load/swap first.
- Run heavy steps with `nice -n 10 ionice -c3`.

### Steps

1. **Cluster** — dedicated, single node:
   ```bash
   nice -n 10 ionice -c3 kind create cluster --name sm-gitops
   ```
   kind's built-in CNI (kindnet, kind ≥ 0.24) enforces NetworkPolicy, so
   `k8s/network-policies.yaml` is actually tested.
2. **Image** — build locally and side-load (avoids GHCR auth and tests the
   working tree, not the last CI build):
   ```bash
   docker build -t sm-app:local . && kind load docker-image sm-app:local --name sm-gitops
   ```
3. **Secrets** — the ones kept out of git on purpose:
   ```bash
   kubectl --context kind-sm-gitops create secret generic redis-auth --from-literal=password="$(openssl rand -hex 16)"
   ```
   ```bash
   kubectl --context kind-sm-gitops create secret generic grafana-admin --from-literal=admin-user=admin --from-literal=admin-password="$(openssl rand -hex 12)"
   ```
4. **Apply** the manifests unchanged:
   ```bash
   kubectl --context kind-sm-gitops apply -f k8s/
   ```
5. **Local overrides** (cluster-side only, never committed):
   ```bash
   kubectl --context kind-sm-gitops set image deployment/app app=sm-app:local
   ```
   ```bash
   kubectl --context kind-sm-gitops patch deployment app --type=json -p='[{"op":"replace","path":"/spec/template/spec/containers/0/imagePullPolicy","value":"IfNotPresent"},{"op":"replace","path":"/spec/replicas","value":1}]'
   ```
   Keep `replicas: 3` instead if you want to watch HAProxy/Service balancing.
6. **Wait** for rollouts:
   ```bash
   kubectl --context kind-sm-gitops wait --for=condition=Available deployment --all --timeout=180s
   ```
7. **Port-forwards.** nginx is a `NodePort` on 30080, so it is also reachable
   directly at `http://<node-ip>:30080` (node IP from `docker inspect sm-gitops-control-plane`).
   The port-forwards below keep the checklist addresses stable:
   ```bash
   kubectl --context kind-sm-gitops port-forward svc/nginx 18080:80
   ```
   ```bash
   kubectl --context kind-sm-gitops port-forward svc/grafana 13000:3000
   ```
   ```bash
   kubectl --context kind-sm-gitops port-forward svc/prometheus 19090:9090
   ```

### Test checklist

| # | Area | Action | Expected |
|---|---|---|---|
| 1 | Proxy chain | `curl -s 127.0.0.1:18080/api/status` | 200 via nginx → HAProxy → app |
| 2 | Metrics hidden | `curl -s -o /dev/null -w '%{http_code}' 127.0.0.1:18080/metrics` | `404` |
| 3 | Client IP | POST `/api/visits` past the limit (default 30/60 s) | `429`; the app keys on `X-Real-IP` set by nginx |
| 4 | IP spoofing | Repeat 3 with `-H 'X-Real-IP: 1.2.3.4'` | still limited (nginx overwrites the header) |
| 5 | Probes | `kubectl get pods` | app `1/1 Ready`, 0 restarts |
| 6 | Redis outage | `kubectl scale deploy/redis --replicas=0`, wait 60 s | app pods stay Ready, no restarts; `/api/status` shows `connected: false` |
| 7 | Recovery | scale Redis back to 1 | `connected: true` without app restart |
| 8 | NetPol: Redis | `kubectl run np-test --rm -it --image=redis:7-alpine --restart=Never -- redis-cli -h redis ping` | times out (only `app=app` pods may reach Redis) |
| 9 | NetPol: app | `kubectl run np-test --rm -it --image=curlimages/curl --restart=Never -- curl -m5 app-service/live` | times out (only HAProxy/Prometheus allowed) |
| 10 | Prometheus | `127.0.0.1:19090/targets` | `app` job up; see known gaps for exporters |
| 11 | Grafana auth | `127.0.0.1:13000` | login required; secret creds work; sign-up disabled |
| 12 | Redis auth | `kubectl exec deploy/redis -- redis-cli ping` | `NOAUTH` error |

### Optional

- **Argo CD (step 10):** `kubectl create namespace argocd`, install the stable
  manifest, apply `argocd-app.yaml`. Note it syncs `HEAD` of the GitHub repo,
  not your working tree, and self-heal will undo the step 5 overrides.

### Teardown

```bash
kind delete cluster --name sm-gitops
```
Stop the port-forwards, then release the `docker` lease.
`kind create` switches the current kube context; switch it back if other
tooling relies on it (`kubectl config use-context <previous>`).

---

## Known gaps found while planning

1. **Missing exporters** — `k8s/monitoring.yaml` scrapes `node-exporter:9100`
   and `redis-exporter:9121`, but neither is defined in `k8s/`. Those targets
   show `down` locally and in production. Either add the exporters (Redis
   exporter needs the `redis-auth` secret and a NetworkPolicy rule) or remove
   the scrape jobs.
2. **No Grafana provisioning** — no datasource or dashboard is provisioned, so
   in tier 3 add Prometheus (`http://prometheus:9090`) and Loki
   (`http://loki:3100`) by hand. Loki has no log shipper feeding it.
3. **Secrets setup is undocumented in the repo** — the commands in tier 3
   step 3 are the reference until a README exists.

# Installation

This page covers installing self.ai on your own infrastructure. For what the
pieces are and how they fit together, see [Architecture](architecture.md);
for the full variable list, see [Configuration](configuration.md).

## What you are installing

self.ai is two required components plus optional satellites:

| Component | Role | Required? |
|---|---|---|
| API server (`selfai_ui`, FastAPI) | OpenAI-compatible chat/completions, knowledge base (RAG), web search, files, multi-user auth | yes |
| `self.chat` | SvelteKit web client, served as a static SPA | yes (for a UI) |
| `self.llamolotl` | llama.cpp inference + training | optional |
| `self.curator` | data curation | optional |
| `self.language-eval`, `self.code-eval` | evaluation harnesses | optional |

Nothing in this repo bundles a database, cache, or ingress — self.ai consumes
those. Neither compose file runs local inference; the API server talks to any
OpenAI-compatible endpoint you point it at.

Three deployment shapes are supported: a **combined container** (one image
serving API and SPA together), **split compose** (`api` + `chat` behind an nginx
reverse proxy serving both same-origin), and **Kubernetes via Flux** (the
`manifests/` tree).

## Prerequisites

- **Docker with Compose v2** for Path A/B, or a Kubernetes cluster with Flux for
  Path C.
- **Hardware**: self.ai itself is light — the API server is a small Python
  service and the client is static. Model hardware requirements are inherited
  from whichever inference engine and model you choose. self.ai imposes no
  hardware floor of its own, and can front models running on another host.
- **Postgres**: optional. With `DATABASE_URL` unset, the API server uses local
  SQLite under its data volume. Set a Postgres DSN to use Postgres; set
  `VECTOR_DB=pgvector` (plus `PGVECTOR_DB_URL`, which falls back to
  `DATABASE_URL`) to put vectors there too. The default vector store is local
  chroma on the data directory.
- **An embeddings endpoint**: optional, but required if you want the knowledge
  base. The published API image is torch-free and ships no local embedding
  model. See [Knowledge base](#knowledge-base-rag) below.
- **Inference endpoint**: optional. Nothing is enabled by default —
  `ENABLE_OLLAMA_API` and `ENABLE_OPENAI_API` are both `False` in the image.

## Path A — Docker Compose, combined

```bash
cp .env.example .env
# edit .env — set WEBUI_SECRET_KEY, e.g. openssl rand -hex 32
docker compose -f docker-compose.combined.yml up
```

Open `http://localhost:3000`. The container listens on `8080` internally and is
published on host port `3000`; data persists in the `selfai-data` volume mounted
at `/app/backend/data`.

Required and commonly set environment:

| Variable | Needed? | Notes |
|---|---|---|
| `WEBUI_SECRET_KEY` | **required** | Long random string. Boot fails on the well-known default (`t0p-s3cr3t`) when auth is on. |
| `WEBUI_URL` | recommended | Externally reachable base URL. Defaults to `http://localhost:3000`. |
| `DATABASE_URL` | optional | Unset → local SQLite. Set a Postgres DSN for Postgres. |
| `OPENAI_API_BASE_URL(S)` / `OPENAI_API_KEY(S)` | optional | Any OpenAI-compatible endpoint. Semicolon-separate both lists, same order and count, to register several backends. |
| `OLLAMA_BASE_URL` / `LLAMOLOTL_BASE_URLS` | optional | Local model-serving endpoints. |
| `CORS_ALLOW_ORIGIN` | leave empty | `*` is not recommended. |

To build the combined image locally instead of pulling, uncomment the `build:`
block in `docker-compose.combined.yml` (context `.`, `Dockerfile.combined`). It
adds no app code: it copies the built SPA out of the `self.chat` image into
`/app/build` on top of the API image, which makes the API server serve the UI at
`/` on the same origin.

## Path B — Docker Compose, split

Pick this when you want the API and the client as separate services — separate
scaling, separate image updates, or a topology closer to what you will run in
production.

```bash
cp .env.example .env
# edit .env — set WEBUI_SECRET_KEY
docker compose -f docker-compose.split.yml up
```

Also `http://localhost:3000`. Three services: `api` and `chat` (each exposing
`8080` internally, not published) and `proxy` (`nginx:1.27-alpine`, publishing
`3000`).

The reverse proxy is not optional. The SPA makes relative API calls, so the
browser must reach both halves on **one origin**. `deploy/proxy/nginx.conf`
routes these path prefixes to the `api` service and everything else to `chat`:

```text
/api  /ws  /ollama  /openai  /curator  /language-eval  /code-eval
/llamolotl  /static  /cache  /health
```

It also handles the WebSocket upgrade on `/ws`, sets `X-Forwarded-*`, and allows
a 100 MB request body. Substitute traefik, Caddy, or your own ingress if you
prefer — the only requirement is same-origin routing on those prefixes.

## Path C — Kubernetes via Flux

`manifests/` is a Kustomize tree that Flux reconciles into a `self-ai`
namespace. It deploys the API server (Deployment, Service, data PVC), the chat
client (Deployment, Service), a single path-routing Ingress, and — depending on
what you keep in `kustomization.yaml` — llamolotl, curator, the two eval
harnesses, and the STT/TTS pods. Single replica, no HA.

External dependencies the manifests assume you already run:

- **Postgres with pgvector** — `DATABASE_URL` and `VECTOR_DB=pgvector`.
- **Valkey/Redis** — `WEBSOCKET_MANAGER=redis`, `REDIS_URL` /
  `WEBSOCKET_REDIS_URL`.
- **An OIDC provider** — confidential client, redirect
  `https://chat.example.com/oauth/oidc/callback`. Local signup stays available
  alongside OAuth.
- **A secrets backend reachable by External Secrets Operator** — every
  credential arrives via `ExternalSecret` resources in
  `manifests/external-secrets/`: `WEBUI_SECRET_KEY`, database DSN, cache URLs,
  OIDC client credentials, provider keys, and a registry pull secret.
- **An ingress controller and cert issuer** — the Ingress is written for traefik
  with a cert-manager cluster issuer.
- **A container registry** — images are hand-pinned by tag in
  `kustomization.yaml`. A working pull secret is a hard first-boot dependency.

These manifests are written against a specific reference cluster. Hostnames,
storage classes, ingress class, issuer name, secret names, and secret store
references all need adapting to yours before they will reconcile. Object storage
is not wired up here: uploads and caches live on the API server's data PVC.

## Getting the images

Per `docs/image-publish-policy.md` (alpha):

| Image | How you get it |
|---|---|
| `api` | published (torch-free Python slim, ~1–2 GB) |
| `self.chat` | published (static SPA on nginx, ~0.1–0.3 GB) |
| `combined` | published — `api` + `self.chat` in one container, built after `api` on every publish |
| `language-eval` | published (Python slim, no torch) |
| `code-eval` | published (larger, CPU-only) |
| `self.transcribe` (STT) | published (CUDA runtime base, models mounted) |
| `self.speak` (TTS) | published (CPU, voice model baked in, ~327 MB) |
| `curator` | **build from source** — 10 GB+ (RAPIDS + torch/CUDA) |
| `llamolotl` | **build from source** — 8–12 GB, CUDA base |

Expect a large local build, not a pull, for curator and llamolotl. The compose
files reference `ghcr.io/selfdothost/self.ai/api`, `ghcr.io/selfdothost/self.chat`,
and `ghcr.io/selfdothost/self.ai/combined`.

The `combined` image is the single-container path for Docker users. A Kubernetes
deployment does not use it — it runs the split per-service images instead.

### Knowledge base (RAG)

Core performs no inference: the API image — and therefore the combined image
built on top of it — carries no embedding model, by design. There is no "full"
image variant that does. Consequences:

- Set `RAG_EMBEDDING_ENGINE=openai` (or `ollama`) and point
  `RAG_OPENAI_API_BASE_URL` / `RAG_OPENAI_API_KEY` at an OpenAI-compatible
  embeddings server, with `RAG_EMBEDDING_MODEL` set to a model it serves. Left
  on the default (local) engine, uploading or querying a knowledge base returns
  a "no embedding backend configured" error.
- Leave `RAG_RERANKING_MODEL` empty. Local reranking loads a cross-encoder that
  this image does not carry; setting it raises a clear error.

## First-run checklist

1. `WEBUI_SECRET_KEY` set to a real random value in `.env`.
2. `WEBUI_URL` set to the URL users will actually visit.
3. Database decided — SQLite (default) or `DATABASE_URL` pointed at Postgres.
4. At least one inference backend configured, or expect no chat models.
5. Embeddings endpoint configured if you intend to use knowledge bases.
6. Bring the stack up and check health:

    ```bash
    curl -f http://localhost:3000/health      # {"status": true}
    curl -f http://localhost:3000/health/db   # {"status": true} — runs SELECT 1
    ```

    The API image carries a `HEALTHCHECK` on `/health`, and the Kubernetes
    Deployment uses `/health` for readiness and liveness probes.

7. Open the UI and register. **The first account created becomes the admin**,
   and local signup is disabled automatically once it exists.

## Troubleshooting

**Container exits at startup complaining about the secret key.** The app
hard-fails when `WEBUI_SECRET_KEY` is unset or left at the well-known default
while auth is enabled. Generate one: `openssl rand -hex 32`.

**"No embedding backend configured" when uploading to a knowledge base.** The
image is API-only. See [Knowledge base](#knowledge-base-rag).

**An error when setting `RAG_RERANKING_MODEL`.** Same cause — local reranking
needs a cross-encoder this image does not ship. Leave it empty.

**API calls fail from the browser in split mode.** The SPA calls the API with
relative paths. If the proxy is missing or the path prefixes are not routed to
the `api` service, those requests hit the SPA container instead. Do not work
around this with `CORS_ALLOW_ORIGIN=*`; fix the routing.

**Pods stuck on `ImagePullBackOff` in the Flux deploy.** The registry pull
secret is a hard first-boot dependency; check that its `ExternalSecret` synced
and that the resulting `dockerconfigjson` is valid.

**Config changes appear ignored.** The reference manifests set
`ENABLE_PERSISTENT_CONFIG=False` so environment values are authoritative on
every boot. With persistent config left on, stale database toggles can win over
your environment.

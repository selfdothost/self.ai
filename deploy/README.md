# Deploying self.ai

Two compose files, both at the repo root:

- **`docker-compose.combined.yml`** — one container (API + web UI together). The
  simplest way to try self.ai.
- **`docker-compose.split.yml`** — `api` + `self.chat` as separate services
  behind a small nginx reverse proxy (`deploy/proxy/nginx.conf`) that serves them
  same-origin. Closer to a production topology.

Both listen on `http://localhost:3000`.

**Neither compose file bundles local inference.** If you don't want to
run/build [`self.llamolotl`](https://github.com/selfdothost/self.llamolotl),
you don't have to — see "Bring your own inference backend" below.

## Steps

```bash
cp .env.example .env
# edit .env — at minimum set WEBUI_SECRET_KEY (openssl rand -hex 32)
docker compose -f docker-compose.combined.yml up   # or -f docker-compose.split.yml
```

## Required / common environment

| Variable | Needed? | Notes |
|----------|---------|-------|
| `WEBUI_SECRET_KEY` | **required** | Long random string. The app refuses the well-known default. |
| `DATABASE_URL` | optional | Unset → local SQLite. Set a Postgres DSN to use Postgres. |
| `OPENAI_API_KEY` / `OPENAI_API_BASE_URL` | optional | Point at any OpenAI-compatible endpoint. |
| `OLLAMA_BASE_URL` / `LLAMOLOTL_BASE_URLS` | optional | Local model-serving endpoints. |

Full list in [`docs/config-reference.md`](../docs/config-reference.md).

## Bring your own inference backend

self.ai's API server speaks to **any OpenAI-compatible endpoint** —
`self.llamolotl` is one option, not a requirement. If you already have your
own token factory (a hosted OpenAI-compatible API, an existing vLLM/SGLang
deployment, your own llama.cpp/Ollama setup), point at it and skip
`self.llamolotl` entirely:

```bash
OPENAI_API_BASE_URLS=https://your-endpoint/v1
OPENAI_API_KEYS=sk-your-key
```

Both vars accept a semicolon-separated list (same order, same count) to
register more than one backend at once. This is the natural fit for
`docker-compose.split.yml` in particular — you're already running `api` +
`chat` + `proxy` as separate services, so there's no local model server to
add. See [`docs/config-reference.md`](../docs/config-reference.md) for the
full variable reference.

## Reverse proxy (split mode)

`deploy/proxy/nginx.conf` routes the API path-prefixes (`/api`, `/ws`, `/ollama`,
`/openai`, `/curator`, `/language-eval`, `/code-eval`, `/llamolotl`, `/static`,
`/cache`, `/health`) to the `api` service and everything else to the `chat`
service. Adapt it to traefik/caddy/your ingress as needed — the only requirement
is that the browser reaches both halves on one origin.

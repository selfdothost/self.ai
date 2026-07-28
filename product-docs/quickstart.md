# Quickstart

Fast installation is just a `docker compose up` away.

```bash
cp .env.example .env
# edit .env — set WEBUI_SECRET_KEY, e.g. openssl rand -hex 32
docker compose -f docker-compose.combined.yml up
```

Open `http://localhost:3000`. The container listens on `8080` internally and is published
on host port `3000`; data persists in the `selfai-data` volume mounted at
`/app/backend/data`.

This is the **combined** deployment shape — one image serving the API and the chat client
together, same-origin. Nothing is enabled by default: no inference endpoint, no knowledge
base embeddings backend. Point `OPENAI_API_BASE_URL`/`OPENAI_API_KEY` (or
`OLLAMA_BASE_URL`/`LLAMOLOTL_BASE_URLS`) at a model, and you have a working chat client.

For the full variable list, the split-compose and Kubernetes deployment shapes,
prerequisites, and troubleshooting, see the [Installation guide](installation.md).

## See also

- [Start Here](start-here.md) — a more in-depth walkthrough
- [Installation](installation.md) — the full guide
- [Configuration](configuration.md) — every setting, in detail

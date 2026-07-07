# self.ai

self.ai is a self-hosted, open-source AI-serving stack: an OpenAI-compatible API
server plus a web chat client, with optional inference, evaluation, speech, and
data-curation components. Bring your own infrastructure — models, database, and
inference endpoints are all yours to point at.

This repo (`self.ai`) is the **API server** (FastAPI, `selfai_ui`) and the deploy
surface. The web client lives in [`self.chat`](../self.chat).

> **Alpha.** This is the first public release. Expect rough edges. See
> `DIVERGENCE.md` for the fork history and `docs/` for configuration.

## Quick start

The easiest way to try self.ai is the combined single-container image:

```bash
cp .env.example .env
# edit .env — set WEBUI_SECRET_KEY (e.g. openssl rand -hex 32)
docker compose -f docker-compose.combined.yml up
```

Open **http://localhost:3000**.

Prefer separate api + web containers? Use the split topology instead:

```bash
docker compose -f docker-compose.split.yml up
```

More detail — including the reverse-proxy setup and every environment variable —
is in [`deploy/README.md`](deploy/README.md) and
[`docs/config-reference.md`](docs/config-reference.md).

## What's in the box

- **API server** — OpenAI-compatible chat/completions, knowledge base (RAG),
  web search, files, multi-user auth (OAuth/OIDC optional). Defaults to local
  SQLite; point `DATABASE_URL` at Postgres (with pgvector) when you want it. The
  knowledge base needs an embeddings endpoint — the image is torch-free, so set
  `RAG_EMBEDDING_ENGINE=openai` at an OpenAI-compatible embeddings server (see
  [`docs/config-reference.md`](docs/config-reference.md)).
- **Web client** ([`self.chat`](../self.chat)) — SvelteKit SPA, same-origin API.
- **Optional components** (separate images) — llama.cpp inference + training
  (`self.llamolotl`), STT (`self.faster-whisper`), TTS (`self.kokoro-fastapi`),
  eval harnesses (`self.language-eval`, `self.code-eval`), and data
  curation (`self.curator`). See [`docs/image-publish-policy.md`](docs/image-publish-policy.md)
  for which ship as images vs. build-from-source.

Nothing here bundles a database, cache, or ingress — self.ai consumes those,
it doesn't ship them.

## Hardware

self.ai itself is light — the API server is a small Python service and the web
client is static. **What it takes to run *models* is inherited from whatever
inference engine and model you choose, not from self.ai.** A small quantized
model runs comfortably on modest hardware; a large one needs a capable GPU.

Size your machine against your chosen engine and model/quant, using that
project's own guidance (e.g. llama.cpp / Ollama for GGUF quant levels). self.ai
does not impose a hardware floor of its own, and — because you bring your own
inference endpoint — it can front models running anywhere, not just on the box
serving the API.

## License

self.ai is licensed under **GPL-3.0** (see `LICENSE`). It is a hard fork of
Open-WebUI v0.5.4 (the last MIT-licensed release); the retained MIT-licensed
upstream portions are preserved in `NOTICE`, and the combined work plus all
self.ai additions are GPLv3. self.ai is **not affiliated with or endorsed by
Open-WebUI** — it was forked from Open-WebUI's MIT v0.5.4 for its multi-user
focus. See `DIVERGENCE.md` for the full record.

Vendored eval/tooling components keep their own upstream licenses (noted in
their subdirectories).

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Contributions are welcome; every commit
needs a DCO `Signed-off-by`, and AI-assisted work is disclosed with an
`Assisted-by:` trailer — a person stands behind each contribution.

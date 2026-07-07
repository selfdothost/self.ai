# self.ai

self.ai is a self-hosted, open-source AI-serving stack: a chat WebUI, an
OpenAI-compatible API, local inference and fine-tuning, dataset curation, and
evaluation harnesses. Bring your own infrastructure — models, database, and
inference endpoints are all yours to point at.

**This repo is the flagship** of the stack — the API server (FastAPI,
`selfai_ui`) and the docker files for the whole thing. Start here; the
other pieces are [`self.chat`](https://github.com/selfdothost/self.chat) (the
web client), [`self.llamolotl`](https://github.com/selfdothost/self.llamolotl)
(inference + training), [`self.curator`](https://github.com/selfdothost/self.curator)
(data curation), and [`self.language-eval`](https://github.com/selfdothost/self.language-eval) /
[`self.code-eval`](https://github.com/selfdothost/self.code-eval) (evaluation).

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
- **Web client** ([`self.chat`](https://github.com/selfdothost/self.chat)) — SvelteKit SPA, same-origin API.
- **Inference + training** ([`self.llamolotl`](https://github.com/selfdothost/self.llamolotl),
  separate image) — llama.cpp serving over an OpenAI-compatible endpoint,
  swappable for chat like any other inference backend. It's currently the
  only backend wired to self.ai's training pipelines and qLoRA deployment.
- **Data curation** ([`self.curator`](https://github.com/selfdothost/self.curator),
  separate image) — the curation pipelines are built against self.curator's
  own bespoke API today.
- **Evaluation** ([`self.language-eval`](https://github.com/selfdothost/self.language-eval),
  [`self.code-eval`](https://github.com/selfdothost/self.code-eval), separate
  images) — same story: the bundled harnesses' bespoke API is what the eval
  pipelines are built against today.

None of this is a hard technical wall — if you know of (or build) another
backend that could plug into these pipelines, we'd love to hear about it.
Open an issue or a PR.

See [`docs/image-publish-policy.md`](docs/image-publish-policy.md) for which
ship as images vs. build-from-source.

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

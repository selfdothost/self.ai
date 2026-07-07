# Configuration reference

Every deploy value a self.ai operator may need to set. All are read from the
environment with a safe default; nothing below hard-codes a host, IP, or secret.
Only `WEBUI_SECRET_KEY` must be set explicitly (the app refuses the well-known
default). Copy `.env.example` to `.env` to get started.

Read-at citations point into `api/selfai_ui/env.py` and `api/selfai_ui/config.py`.

| Value | Env var | Default | Read at |
|-------|---------|---------|---------|
| Secret key (JWT signing) | `WEBUI_SECRET_KEY` | *required* — boot fails on `t0p-s3cr3t` when auth is on | env.py (`WEBUI_SECRET_KEY`) |
| Public base URL | `WEBUI_URL` | `http://localhost:3000` | config.py (`WEBUI_URL`) |
| CORS origins | `CORS_ALLOW_ORIGIN` | empty (`[]`); `*` warns, not recommended | config.py (`CORS_ALLOW_ORIGIN`) |
| Primary database | `DATABASE_URL` | `sqlite:///…/webui.db` (local) | env.py (`DATABASE_URL`) |
| Vector store selector | `VECTOR_DB` | `chroma` (local) | config.py (`VECTOR_DB`) |
| pgvector DSN | `PGVECTOR_DB_URL` | falls back to `DATABASE_URL` | config.py (`PGVECTOR_DB_URL`) |
| RAG embedding engine | `RAG_EMBEDDING_ENGINE` | empty (local model) — set `openai`/`ollama` on the API-only image | config.py (`RAG_EMBEDDING_ENGINE`) |
| RAG embedding endpoint / key | `RAG_OPENAI_API_BASE_URL`, `RAG_OPENAI_API_KEY` | fall back to `OPENAI_API_*` | config.py (`RAG_OPENAI_API_*`) |
| RAG embedding model | `RAG_EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` (local); use e.g. `text-embedding-3-small` with the `openai` engine | config.py (`RAG_EMBEDDING_MODEL`) |
| Web loader engine | `RAG_WEB_LOADER_ENGINE` | empty (safe built-in fetch); `firecrawl` to use a crawler | config.py (`RAG_WEB_LOADER_ENGINE`) |
| Firecrawl endpoint / key | `FIRECRAWL_API_BASE_URL`, `FIRECRAWL_API_KEY` | empty | config.py (`FIRECRAWL_API_*`) |
| Web search engine | `RAG_WEB_SEARCH_ENGINE` | empty (off); e.g. `searxng` | config.py (`RAG_WEB_SEARCH_ENGINE`) |
| OpenAI-compatible base URL | `OPENAI_API_BASE_URL(S)` | `https://api.openai.com/v1` | config.py (`OPENAI_API_BASE_URL`) |
| OpenAI key(s) | `OPENAI_API_KEY(S)` | empty (disabled) | config.py (`OPENAI_API_KEY`) |
| Ollama endpoint(s) | `OLLAMA_BASE_URL(S)` | empty (disabled) | config.py (`OLLAMA_BASE_URL`) |
| llama.cpp / llamolotl endpoint(s) | `LLAMOLOTL_BASE_URLS` | empty (disabled) | config.py (`LLAMOLOTL_BASE_URLS`) |
| OIDC client id | `OAUTH_CLIENT_ID` | empty (off) | config.py (`OAUTH_CLIENT_ID`) |
| OIDC client secret | `OAUTH_CLIENT_SECRET` | empty (off) | config.py (`OAUTH_CLIENT_SECRET`) |
| OIDC provider URL | `OPENID_PROVIDER_URL` | empty (off) | config.py (`OPENID_PROVIDER_URL`) |
| OIDC redirect URI | `OPENID_REDIRECT_URI` | empty | config.py (`OPENID_REDIRECT_URI`) |
| Object storage provider | `STORAGE_PROVIDER` | empty (local disk) | config.py (`STORAGE_PROVIDER`) |
| S3 endpoint / creds / bucket / region | `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_BUCKET_NAME`, `S3_REGION_NAME` | `None` | config.py (`S3_*`) |
| STT endpoint | `AUDIO_STT_OPENAI_API_BASE_URL` | falls back to `OPENAI_API_BASE_URL` | config.py (`AUDIO_STT_OPENAI_API_BASE_URL`) |
| TTS endpoint | `AUDIO_TTS_OPENAI_API_BASE_URL` | falls back to `OPENAI_API_BASE_URL` | config.py (`AUDIO_TTS_OPENAI_API_BASE_URL`) |
| Redis/Valkey | `REDIS_URL` | `redis://localhost:6379/0` | env.py (`REDIS_URL`) |

Notes:

- **Defaults are cluster-free.** With nothing set, self.ai boots on local SQLite
  with all inference providers disabled — you add exactly the providers you have.
- **`WEBUI_SECRET_KEY`** must be a real random value. The app intentionally
  hard-fails at startup if it is unset or left at the well-known default while
  auth is enabled.
- The endpoint values above are examples — point them at *your* infrastructure.
- **Knowledge base / RAG embeddings.** The published image is API-only (no bundled
  torch / embedding model), so the knowledge base needs an external embeddings
  endpoint: set `RAG_EMBEDDING_ENGINE=openai` (or `ollama`) and point
  `RAG_OPENAI_API_BASE_URL` / `RAG_OPENAI_API_KEY` at an OpenAI-compatible
  embeddings server — typically the same provider you use for chat. With the
  default (local) engine on this image, uploading or querying a knowledge base
  returns a clear *"no embedding backend configured"* error. To embed locally
  instead, run the full image and leave `RAG_EMBEDDING_ENGINE` unset.
- **Local reranking / hybrid rerank** (`RAG_RERANKING_MODEL`) also requires the
  full image — it loads a local cross-encoder. Leave it empty on the API-only
  image (the default); setting it there raises a clear error.
- **Web loader (Firecrawl).** `RAG_WEB_LOADER_ENGINE=firecrawl` points the loader
  at any Firecrawl-compatible endpoint. To verify a connection before wiring it
  into the app, run `scripts/verify-firecrawl.py` (inside the api image) with
  `FIRECRAWL_API_BASE_URL`/`FIRECRAWL_API_KEY` set — it runs the same scrape path
  the loader uses and reports pass/fail. Crawled pages are embedded, so an
  embeddings endpoint must be configured too.

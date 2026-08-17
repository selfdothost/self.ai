# Configuration

self.ai is configured entirely from the environment, with most settings also
editable at runtime from the admin panel. This page is the operator reference:
how the layers interact, what must be set, and the settings you are most likely
to touch.

See [Installation](installation.md) for getting a deployment up, and
[Architecture](architecture.md) for what the components are.

## The three configuration layers

| Layer | Where it lives | Who changes it |
|-------|----------------|----------------|
| Environment variables | Container env, `.env`, Kubernetes manifest, secret | Operator, at deploy time |
| Persisted config | The `config` table in the application database (a single JSON blob) | The application, when an admin saves a setting |
| Runtime admin settings | Admin panel, backed by the `GET /config` and `POST /config/update` endpoints on each router | Admins, at runtime |

Settings declared as `PersistentConfig` in `api/selfai_ui/config.py` participate
in all three layers. On boot, each one reads its environment variable to produce
an *env value*, then — depending on `ENABLE_PERSISTENT_CONFIG` — either keeps
that value or replaces it with the value stored in the database. Admin-panel
saves write back to the database blob.

Settings that are **not** `PersistentConfig` (plain `os.environ.get` in
`api/selfai_ui/env.py`, for example `DATABASE_URL`, `REDIS_URL`, `VECTOR_DB`)
are environment-only. They are never stored in the database and never editable
from the admin panel.

### ENABLE_PERSISTENT_CONFIG

!!! warning "This is the single most common source of confusion"
    When `ENABLE_PERSISTENT_CONFIG=False`, the database-stored config is
    **ignored on boot**. Every `PersistentConfig` setting takes its environment
    value on every restart. Admin-panel changes still write to the database and
    still take effect for the life of the process — but they are silently
    discarded at the next restart.

| Value | Boot behaviour | Admin-panel changes |
|-------|----------------|---------------------|
| `True` (default) | DB value wins when present; env value is only a seed for settings never saved | Persist across restarts |
| `False` | Env is authoritative every boot | Apply until the pod restarts, then revert to env |

Set `False` for GitOps-style deployments where configuration belongs in
manifests and a secret store, not in application state. That is what the
reference Kubernetes deployment in `manifests/api/10-deployment.yaml` does.
Under that mode, "the toggle didn't stick" is expected behaviour: change the
manifest, not the admin panel.

Two related switches:

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_PERSISTENT_CONFIG` | `True` | See above. |
| `RESET_CONFIG_ON_START` | `False` | Deletes the persisted config row at startup. |

## Required settings

### WEBUI_SECRET_KEY

The HMAC secret used to sign user session tokens. There is no usable default:
when `WEBUI_AUTH` is on (the default), `api/selfai_ui/env.py` raises at import
time if `WEBUI_SECRET_KEY` is empty, and raises with an explicit security error
if it is left at the well-known upstream default `t0p-s3cr3t`. The process will
not boot in either case.

```bash
openssl rand -hex 32
```

Rotating this value invalidates every existing session.

### DATABASE_URL

```bash
DATABASE_URL=postgresql://selfai:REDACTED@postgres.example.internal:5432/selfai
```

Defaults to SQLite under `DATA_DIR` (`sqlite:///<DATA_DIR>/webui.db`), which is
fine for a laptop and not fine for a real deployment. A `postgres://` scheme is
rewritten to `postgresql://` automatically.

## Reference

Only operator-facing settings are listed. `api/selfai_ui/config.py` and
`api/selfai_ui/env.py` are the exhaustive source; anything not here can be found
there.

### Core and server

| Variable | Default | Description |
|----------|---------|-------------|
| `WEBUI_SECRET_KEY` | *required* | Session token signing secret. Boot fails on empty or `t0p-s3cr3t` with auth on. |
| `WEBUI_URL` | `http://localhost:3000` | Externally reachable base URL. |
| `WEBUI_NAME` | `Self.AI` | Display name. |
| `PORT` | `8080` | Listen port (read by `api/start.sh`). |
| `ENV` | `dev` | `dev` or `prod`. Also decides the default for secure session cookies. |
| `CORS_ALLOW_ORIGIN` | empty | Semicolon-separated origins. `*` is accepted but logs a warning; only `http`/`https` schemes validate. |
| `DATA_DIR` | `<backend>/data` | Uploads, caches, local vector store. Created at startup if missing, and checked for writability — a path that cannot be created or written to fails immediately with a message naming `DATA_DIR`, rather than surfacing later as a SQLite error. |
| `GLOBAL_LOG_LEVEL` | `INFO` | Falls back to `INFO` on an unrecognised value. Per-source overrides exist as `<SOURCE>_LOG_LEVEL` (e.g. `RAG_LOG_LEVEL`). |
| `SAFE_MODE` | `False` | Disables user-supplied functions/pipelines. |
| `OFFLINE_MODE` | `false` | Sets `HF_HUB_OFFLINE=1`; disables model auto-update. |
| `ENABLE_ADMIN_CHAT_ACCESS` | `True` | Allows admins to read user chats. |

### Database and vector store

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `sqlite:///<DATA_DIR>/webui.db` | Primary database DSN. |
| `DATABASE_POOL_SIZE` | `0` | SQLAlchemy pool size; `0` disables pooling. |
| `DATABASE_POOL_MAX_OVERFLOW` | `0` | Extra connections above the pool size. |
| `DATABASE_POOL_TIMEOUT` | `30` | Seconds to wait for a connection. |
| `DATABASE_POOL_RECYCLE` | `3600` | Seconds before a connection is recycled. |
| `VECTOR_DB` | `sqlite-vec` | One of `sqlite-vec`, `pgvector`, `milvus`, `qdrant`, `opensearch`. Environment-only. The `chroma` backend was removed; setting it now fails with an explanatory error rather than starting. |
| `PGVECTOR_DB_URL` | falls back to `DATABASE_URL` | DSN for the pgvector store. |
| `PGVECTOR_INITIALIZE_MAX_VECTOR_LENGTH` | `1536` | Column width created for embeddings. |
| `SQLITE_VEC_PATH` | `<DATA_DIR>/vector_db/sqlite_vec.db` | Where the sqlite-vec store file lives. |
| `SQLITE_VEC_VECTOR_LENGTH` | `1536` | Vector width, fixed once the store is created. Shorter embeddings are zero-padded; changing it requires a re-index. |

Other stores have their own variables: `QDRANT_URI`/`QDRANT_API_KEY`,
`MILVUS_URI` (default `<DATA_DIR>/vector_db/milvus.db`), and `OPENSEARCH_URI`
(default `https://localhost:9200`) with `OPENSEARCH_USERNAME`/`OPENSEARCH_PASSWORD`.

### Authentication

| Variable | Default | Description |
|----------|---------|-------------|
| `WEBUI_AUTH` | `True` | Master auth switch. Turning it off also forces `ENABLE_SIGNUP` off. |
| `ENABLE_SIGNUP` | `True` | Local account self-registration. |
| `ENABLE_LOGIN_FORM` | `True` | Show the username/password form. Set `False` for SSO-only. |
| `DEFAULT_USER_ROLE` | `pending` | Role assigned to new users; `pending` requires admin approval. |
| `JWT_EXPIRES_IN` | `-1` | Session token lifetime; `-1` is no expiry. |
| `WEBUI_SESSION_COOKIE_SAME_SITE` | `lax` | Session cookie `SameSite`. |
| `WEBUI_SESSION_COOKIE_SECURE` | `True` unless `ENV=dev` | Session cookie `Secure` flag. |
| `WEBUI_AUTH_TRUSTED_EMAIL_HEADER` | unset | Header carrying a pre-authenticated email (reverse-proxy auth). Only set behind a proxy that strips it from client requests. |
| `ENABLE_API_KEY` | `True` | Allow users to mint API keys. |
| `ENABLE_API_KEY_ENDPOINT_RESTRICTIONS` | `False` | Restrict API keys to an allowlist. |
| `API_KEY_ALLOWED_ENDPOINTS` | empty | Comma-separated endpoint allowlist. |
| `BYPASS_MODEL_ACCESS_CONTROL` | `False` | Skip per-model access checks. |

#### OIDC / OAuth

| Variable | Default | Description |
|----------|---------|-------------|
| `OAUTH_CLIENT_ID` | empty | OIDC client id. |
| `OAUTH_CLIENT_SECRET` | empty | OIDC client secret. |
| `OPENID_PROVIDER_URL` | empty | Discovery document URL. All three of id, secret and provider URL must be set before the `oidc` provider is registered. |
| `OPENID_REDIRECT_URI` | empty | Redirect URI. |
| `OAUTH_PROVIDER_NAME` | `SSO` | Label on the login button. |
| `OAUTH_SCOPES` | `openid email profile` | Requested scopes. |
| `ENABLE_OAUTH_SIGNUP` | `False` | Create accounts on first SSO login. |
| `OAUTH_MERGE_ACCOUNTS_BY_EMAIL` | `False` | Link an SSO login to an existing local account with the same email. Only enable if the IdP verifies email. |
| `ENABLE_OAUTH_ROLE_MANAGEMENT` | `False` | Map IdP roles to application roles. |
| `OAUTH_ALLOWED_ROLES` | `user,admin` | Roles permitted to sign in. |
| `OAUTH_ADMIN_ROLES` | `admin` | Roles granted admin. |
| `ENABLE_OAUTH_GROUP_MANAGEMENT` | `False` | Sync IdP groups. |
| `OAUTH_ALLOWED_DOMAINS` | `*` | Comma-separated email domain allowlist. |

Claim names are overridable when the IdP uses non-standard ones:
`OAUTH_USERNAME_CLAIM` (`name`), `OAUTH_EMAIL_CLAIM` (`email`),
`OAUTH_PICTURE_CLAIM` (`picture`), `OAUTH_ROLES_CLAIM` (`roles`),
`OAUTH_GROUP_CLAIM` (`groups`). Google and Microsoft providers are configured
separately via `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` and
`MICROSOFT_CLIENT_ID`/`MICROSOFT_CLIENT_SECRET`/`MICROSOFT_CLIENT_TENANT_ID`.

#### LDAP

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_LDAP` | `false` | Enable LDAP authentication. |
| `LDAP_SERVER_HOST` | `localhost` | Server host. |
| `LDAP_SERVER_PORT` | `389` | Server port. |
| `LDAP_APP_DN` / `LDAP_APP_PASSWORD` | empty | Bind DN and password. |
| `LDAP_SEARCH_BASE` | empty | User search base DN. |
| `LDAP_SEARCH_FILTER` | empty | Additional user filter. |
| `LDAP_ATTRIBUTE_FOR_USERNAME` | `uid` | Username attribute. |
| `LDAP_USE_TLS` | `True` | Use TLS. |

Also available: `LDAP_SERVER_LABEL` (default `LDAP Server`), `LDAP_CA_CERT_FILE`
(empty), `LDAP_CIPHERS` (default `ALL`).

### Inference connections

Base-URL and key lists are semicolon-separated (`;`) and positional — the *n*th
key belongs to the *n*th URL.

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_OPENAI_API` | `True` | Enable the OpenAI-compatible connection type. |
| `OPENAI_API_BASE_URL` | `https://api.openai.com/v1` | Single OpenAI-compatible endpoint. |
| `OPENAI_API_BASE_URLS` | falls back to `OPENAI_API_BASE_URL` | Multiple endpoints, `;`-separated. |
| `OPENAI_API_KEY` / `OPENAI_API_KEYS` | empty | Key, or `;`-separated keys aligned to the URL list. |
| `OPENAI_API_CONFIGS` | `{}` | JSON object keyed by base URL. Per-connection `enable`, `model_ids`, `prefix_id`. Use `prefix_id` when two connections expose the same model ids, or the merged model list will contain duplicates. |
| `ENABLE_OLLAMA_API` | `True` | Enable the Ollama connection type. |
| `OLLAMA_BASE_URL` / `OLLAMA_BASE_URLS` | empty | Ollama endpoint(s), `;`-separated. |
| `ENABLE_LLAMOLOTL_API` | `True` | Enable the self.llamolotl connection type. |
| `LLAMOLOTL_BASE_URLS` | `http://self-llamolotl:8080` | Inference endpoint(s). |
| `LLAMOLOTL_CONTROL_BASE_URLS` | `http://self-llamolotl:8093` | Model-management control endpoint(s). |
| `AIOHTTP_CLIENT_TIMEOUT` | unset (no timeout) | Seconds for outbound provider calls. |
| `AIOHTTP_CLIENT_TIMEOUT_OPENAI_MODEL_LIST` | unset (no timeout) | Seconds for the model-list call specifically. |

Companion services follow the same shape: `ENABLE_CURATOR_API` /
`CURATOR_BASE_URLS`, `ENABLE_CODE_EVAL_API` / `CODE_EVAL_BASE_URLS`,
`ENABLE_LANGUAGE_EVAL_API` / `LANGUAGE_EVAL_BASE_URLS`, `ENABLE_SELF_CORPUS` /
`SELF_CORPUS_LAKEFS_ENDPOINT`, and `ENABLE_PISTON_EXECUTION` /
`PISTON_BASE_URL`.

### RAG and embeddings

| Variable | Default | Description |
|----------|---------|-------------|
| `RAG_EMBEDDING_ENGINE` | empty (local model) | `openai`, `ollama`, or empty for a local sentence-transformers model. Must be set — see [Core runs no models in-process](#core-runs-no-models-in-process). |
| `RAG_EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | Embedding model id. Use a served model id (e.g. `text-embedding-3-small`) with the `openai` engine. |
| `RAG_OPENAI_API_BASE_URL` | falls back to `OPENAI_API_BASE_URL` | Embeddings endpoint, independent of the chat provider. |
| `RAG_OPENAI_API_KEY` | falls back to `OPENAI_API_KEY` | Embeddings key. |
| `RAG_OLLAMA_BASE_URL` / `RAG_OLLAMA_API_KEY` | fall back to `OLLAMA_BASE_URL` / empty | Ollama embeddings connection. |
| `RAG_EMBEDDING_BATCH_SIZE` | `1` | Texts per embedding request. |
| `RAG_RERANKING_MODEL` | empty | Local cross-encoder reranker. Must stay empty on the API-only image. |
| `ENABLE_RAG_HYBRID_SEARCH` | `False` (unset) | BM25 + vector hybrid retrieval; requires a reranking model. |
| `RAG_TOP_K` | `3` | Chunks retrieved per query. |
| `RAG_RELEVANCE_THRESHOLD` | `0.0` | Minimum score to keep a chunk. |
| `CHUNK_SIZE` | `1000` | Characters per chunk. |
| `CHUNK_OVERLAP` | `100` | Overlap between chunks. |
| `RAG_FILE_MAX_SIZE` | unset (unlimited) | Max upload size in MB. |
| `RAG_FILE_MAX_COUNT` | unset (unlimited) | Max files per upload. |
| `FILE_UPLOAD_MIME_ALLOWLIST` | empty (all allowed) | Comma-separated MIME allowlist. |
| `CONTENT_EXTRACTION_ENGINE` | empty | External document extraction engine. |

### Web search and web loading

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_RAG_WEB_SEARCH` | `False` | Enable web search in chat. |
| `RAG_WEB_SEARCH_ENGINE` | empty | e.g. `searxng`, `google_pse`, `bing`. |
| `SEARXNG_QUERY_URL` | empty | SearXNG URL including the `<query>` placeholder, e.g. `https://search.example.com/search?q=<query>`. |
| `RAG_WEB_SEARCH_RESULT_COUNT` | `3` | Results fetched per search. |
| `RAG_WEB_SEARCH_CONCURRENT_REQUESTS` | `10` | Parallel page fetches. |
| `RAG_WEB_LOADER_ENGINE` | empty (built-in fetch) | `firecrawl` to use a Firecrawl-compatible crawler. |
| `FIRECRAWL_API_BASE_URL` / `FIRECRAWL_API_KEY` | empty | Crawler endpoint and key. |
| `BROWSE_PLAYWRIGHT_SERVICE_URL` / `BROWSE_PLAYWRIGHT_API_KEY` | empty | Playwright fetch service used by the in-chat browse tool. Unset means the tool no-ops. |
| `ENABLE_RAG_WEB_LOADER_SSL_VERIFICATION` | `True` | Verify TLS when fetching pages. |

Crawled and searched pages are embedded, so web search only works once an
embeddings backend is configured.

### Audio (STT / TTS)

| Variable | Default | Description |
|----------|---------|-------------|
| `AUDIO_STT_ENGINE` | empty (local faster-whisper) | Set to `openai` to use an external OpenAI-compatible transcription service. |
| `AUDIO_STT_OPENAI_API_BASE_URL` | falls back to `OPENAI_API_BASE_URL` | Transcription endpoint. |
| `AUDIO_STT_OPENAI_API_KEY` | falls back to `OPENAI_API_KEY` | Transcription key. |
| `AUDIO_STT_MODEL` | empty | Model id sent to the transcription service. |
| `WHISPER_MODEL` | `base` | Local faster-whisper model (local engine only). |
| `AUDIO_TTS_ENGINE` | empty | `openai`, `elevenlabs`, `azure`, or `transformers`. |
| `AUDIO_TTS_OPENAI_API_BASE_URL` | falls back to `OPENAI_API_BASE_URL` | Speech endpoint. |
| `AUDIO_TTS_OPENAI_API_KEY` | falls back to `OPENAI_API_KEY` | Speech key. |
| `AUDIO_TTS_MODEL` | `tts-1` | Model id. |
| `AUDIO_TTS_VOICE` | `alloy` | Voice id. |
| `AUDIO_TTS_SPLIT_ON` | `punctuation` | How long text is chunked before synthesis. |

!!! warning "Setting a base URL is not enough"
    The audio router dispatches on `AUDIO_STT_ENGINE` / `AUDIO_TTS_ENGINE`
    only. If the engine is empty, the OpenAI base-URL and key values are never
    read: STT falls through to the local faster-whisper path, and TTS returns
    `400 TTS engine is not configured`. Always set the engine to `openai`
    alongside the base URL.

### Storage

| Variable | Default | Description |
|----------|---------|-------------|
| `STORAGE_PROVIDER` | empty (local disk under `DATA_DIR`) | Set to `s3` for object storage. |
| `S3_ENDPOINT_URL` | `None` | S3-compatible endpoint. |
| `S3_BUCKET_NAME` | `None` | Bucket. |
| `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | `None` | Credentials. |
| `S3_REGION_NAME` | `None` | Region. |

### Websocket and cache

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_WEBSOCKET_SUPPORT` | `True` | Enable socket.io. |
| `WEBSOCKET_MANAGER` | empty (in-memory) | Set to `redis` for a shared pub/sub backend. Required for more than one replica. |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis/Valkey URL for the application cache. |
| `WEBSOCKET_REDIS_URL` | falls back to `REDIS_URL` | Separate URL for socket.io pub/sub. |
| `REDIS_KEY_PREFIX` | `open-webui` | Key namespace. Must match the ACL pattern if the server restricts the keyspace, or every operation returns `NOPERM`. |

## Core runs no models in-process

self.ai's core — the API server and the chat client — does not perform inference.
Serving models is the job of the separate components behind it, or of whatever
external OpenAI-compatible endpoint you point it at. The API image is therefore
built without torch, `sentence-transformers`, `faster-whisper`, or the rest of the
local-model stack, and that is a design decision rather than a packaging
limitation: **there is no "full" image variant to switch to.**

Everything that would run a model in-process is consequently unavailable, and the
code raises an explicit error rather than an import failure:

- **Embeddings.** `RAG_EMBEDDING_ENGINE` must be `openai` or `ollama`. Leaving
  it empty selects the local sentence-transformers path; on this image, any
  knowledge-base upload or query then fails with *"No embedding backend is
  configured."*
- **Reranking.** `RAG_RERANKING_MODEL` must stay empty. Setting it raises
  *"Local reranking requires the full image"* — the message is an Open-WebUI
  holdover that outlived the image it named; read it as "not available in core".
  This also rules out `ENABLE_RAG_HYBRID_SEARCH`, which depends on a reranker.
- **Local speech-to-text.** With `AUDIO_STT_ENGINE` empty, transcription
  returns `501 Not Implemented`. Point STT at an external service instead.

In every case the fix is to point the setting at a service, not to change images.

## Exporting and importing configuration

The persisted config blob can be moved between deployments. Both endpoints
require an admin account. (`ENABLE_ADMIN_EXPORT`, default `True`, is a separate
front-end feature flag and does not gate these endpoints.)

```bash
# Export
curl -H "Authorization: Bearer $TOKEN" \
  https://chat.example.com/api/v1/configs/export > config.json

# Import
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"config\": $(cat config.json)}" \
  https://chat.example.com/api/v1/configs/import
```

Import replaces the whole blob and refreshes every registered `PersistentConfig`
in the running process. Two caveats:

- The export contains provider API keys and OAuth secrets in plaintext. Treat
  the file as a secret.
- With `ENABLE_PERSISTENT_CONFIG=False`, an import applies to the running
  process but is discarded at the next restart, because env is re-read as
  authoritative. Import into a deployment configured that way only as a
  temporary measure — the durable change belongs in the manifest.

Back to [Home](index.md).

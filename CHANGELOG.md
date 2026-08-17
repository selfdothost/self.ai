# Changelog

All notable changes to self.ai are documented here. This is a curated, human-readable summary of what shipped each week — not a raw commit or merge log — organized by Security, Added, Removed, and Fixed. self.ai publishes weekly on Tuesdays.

## self.ai — 2026-08-18

Covers one week of changes, but this is the first push to reach the public mirror since 2026-07-30: the 2026-08-04 and 2026-08-11 syncs were stopped by the publish pipeline's own safety scan (internal hostnames in two comments and a test fixture that looked like a cloud key), and a mirror credential lapsed on the companion repos. Both fixed; the 2026-08-11 entry below arrives with this one.

### Security
- A session lookup that fails to deliver no longer deletes the caller's token — a transient failure could previously log a user out. (self.chat!175)
- The vision guard now actually fires: a request that sends an image to a model that cannot see is refused up front, and a turn that fails is persisted as failed rather than vanishing from the conversation. (self.chat#51, #52)

### Added
- **Studio.** The Workspace is now the Studio, in the API's permission group and in the client. Permission names move from `workspace.*` to `studio.*`. Stored group grants are rekeyed by a migration (`studio` wins whole; nothing is merged per-child, so a permission an admin had turned off stays off), and permission checks read the old key transitionally so no group loses Studio access if the two halves of the rename land in either order. (self.ai#134)
- **The Tokenization Studio, first cut.** A Studio session that shows a prompt the way the model sees it: its own permission and session kind, a model gallery limited to models that expose a tokenizer, a shell with a pinned controls rail, prompt selection from Studio › Prompts and the sampler in use, per-token logprobs on request, re-scoring a single position, an explicit "this model must be loaded first" signal instead of a stall, and every token stream saying whose tokenizer its ids belong to. (self.ai#134, T-201–T-307, T-401)
- **Model versioning and publishing.** Publishing a model is its own job kind under `studio.publish`: it queues behind a job window and holds a VRAM lease like any other GPU consumer, the admin's job-window editor offers `publish` as a slot type, and the queue shows a publish as a first-class row with its window. (self.ai#131, #136)
- **Model weights on self.corpus.** Every model gets a repository on the data-versioning service; a download lands in it, and the catalogue links to it, so a served weight has a provenance trail alongside the knowledge it was trained on. (self.ai!463)
- A model's vision capability is derived from what its serving backend actually reports about its input modalities, not from a hand-kept flag. (self.ai#139)
- A client can declare which surface owns a conversation, so a Studio session and a chat no longer collide in the sidebar. (self.ai#134)
- The curation pipeline canvas is rebuilt on a maintained flow library; loading a saved pipeline repopulates it. (self.chat!146, #47)
- LoRA routes address the right model when the inference server runs in router mode. (self.llamolotl#44)

### Removed
- The last `createEventDispatcher` in the client. The Svelte 5 conversion is complete; every component uses callback props. (self.chat#31)
- `/debug/session_pools` on the speech service — it could only ever return a 500. (self.speak#2)

### Fixed
- Versioning tables declared their JSON columns with a type that breaks on Postgres; corrected, and a schema sweep now fails the test suite on any column where the model and the migration disagree. (self.ai!456, !465)
- Migration bookkeeping rows that resolve to no migration are pruned instead of blocking upgrades. (self.ai#85)
- An over-large upload to the transcription service is reported as 413, not 500. (self.transcribe#4)
- CI now smoke-tests the chat client against a running API — nothing did before. (self.ai#134)
- The public-mirror pipeline no longer depends on the repository's own end-to-end tests: a flaky browser test cannot skip a week's publish. The scan that gates the mirror is unchanged and remains the gate.

## self.ai — 2026-08-11

Covers two weeks: no entry was published on 2026-08-04.

### Security
- The MCP proxy no longer forwards the caller's self.ai credential to a registered backend. A backend now authenticates on its own seam, so adding one does not hand it the credentials of everyone who uses it. (self.ai#25)
- Configuration export no longer includes live secret values. Exported config is meant to be shareable; it was carrying the real values of secret-typed settings. (self.ai#95)
- Socket.IO connections accept API keys on attach, so a programmatic client no longer needs a session cookie to use the realtime surface. (self.ai#81)
- Automated clients can hold their own accounts instead of sharing an administrator's API key. Sharing it did not just blur the audit trail: a caller using it authenticated as an administrator, which silently bypassed every model access-control gate. Accounts that already exist under a matching email are now merged onto their OIDC identity rather than becoming a second account, and the link is recorded when it happens.

### Added
- **The Voice Workshop.** Voices are a first-class Workspace object with their own CRUD API and permission, built and previewed on a node graph rather than a form. Multiple samples can be blended with a slider, previews play from the real synthesis backend, and a crafted voice can be attached to a model so that model answers in it. Deliberately framed as a workshop for building voices, not as cloning.
- **Anthropic's Messages API, inbound.** self.ai now serves `POST /v1/messages`, so tools that speak Anthropic's API natively — including Claude Code — can point at a self.ai deployment directly. This is the inbound direction; the existing outbound Anthropic provider is unchanged.
- **An MCP front door.** An auth-gated proxy for Model Context Protocol backends, with dynamic registration so an admin can add a backend without a redeploy, and a browser-automation backend registered behind it.
- **Backup and restore.** A real Backup admin page that produces a restorable archive of the deployment's own data, with the Database page narrowed to actually being about the database — connection, pool, schema revision and size.
- **Evaluations you can extend.** A catalog for evaluations added to a deployment, live task discovery from the harness instead of a hardcoded list, the MultiPL-E language set read from the harness, and sandboxed code execution turned on for code evaluations.
- **Cooperative GPU sharing, tightened.** Model loads now take a broker lease rather than loading and hoping, an admin can force-release every GPU consumer system-wide from one control, the queue explains why a job is sitting where it is, and an admin panel shows what is resident on the card. Model footprints are declared per model, so a load that will not fit is refused with the reason instead of failing as an out-of-memory crash.
- `/api/models` reports the context window a model is actually served with, so a client no longer has to guess or load a model to find out.
- The Admin System page reports the pod's own CPU/memory quota and the shared GPU, rather than the underlying host's hardware, which was never what the deployment could use.
- Tools supplied by an API client are merged with the deployment's own tools instead of replacing them, and client tool names are never rewritten.

### Removed
- **ChromaDB.** The default vector store is now sqlite-vec — a single file needing no extra service, which is what the single-container quickstart always wanted. That removed 41 packages from the API image, among them an ONNX runtime, a tokenizer stack, an OpenTelemetry stack and a Kubernetes client — none of which a self-hosted install was using. **This is a breaking change for existing installs.** `VECTOR_DB=chroma` now refuses to start and says why; the two storage formats are not interchangeable, so **knowledge bases must be re-indexed** — re-upload the files, or rebuild the knowledge base from its sources. An install that starts with an orphaned Chroma store beside an empty one logs an explicit warning rather than silently retrieving nothing.
- pydub and its `audioop` compatibility shim. Audio conversion calls ffmpeg directly, which is what pydub was wrapping.
- The inherited upstream attribution on the About page, which credited a project self.ai is a hard fork of rather than describing self.ai.

### Fixed
- **The chat route could go blank in production.** A model-selector effect that rewrote the state it depended on re-entered itself indefinitely. Two more of the same shape are fixed here: chat message pagination whose loader fired forever, and a chat load effect that could re-enter itself.
- **Any chat could grow to several gigabytes of memory.** A loader interval was never cleared, leaking on every message.
- **HTML and SVG artifacts render again**, and the artifact rail sizes itself correctly instead of collapsing, resizing in a loop, or tearing down the surrounding view.
- **Mod API routes were unreachable in the combined single-container image** — the deployment we recommend to new users. The single-page-app catch-all was registered before mod routes and shadowed all of them, so a mod's `POST` returned "method not allowed" and its `GET` returned the web UI instead of data. The split deployment was never affected, which is why nothing caught it. (self.ai#119)
- **Pointing `DATA_DIR` at a directory that does not exist now works**, instead of failing several frames later as an unrelated-looking database error. The directory is created at startup and checked for writability, and a path that cannot be created or written to says so by name. This is the exact advice given for the combined image — mount a volume, point `DATA_DIR` at it. (self.ai#120)
- **A model that does not fit reads as a model that does not fit.** A capacity refusal from the inference router used to reach the caller as a bare "Service Unavailable"; the upstream explanation was being discarded before it could be read, and the status was flattened so a client could not tell a retryable failure from a permanent one. Structured API errors now render as text rather than `[object Object]`. (self.ai#103, self.ai#35)
- **The deployment refuses to serve on a schema that is not at head**, rather than starting against a half-migrated database and failing later in ways that look like data corruption. (self.ai#82)
- A range of GPU-memory accounting faults: a release could under-report what it freed, a consumer that had never checked in could be force-reclaimed, a grant reservation could be double-counted, and a model swap was accounted as an addition — which made switching models look impossible once the card was nearly full.
- Stopping a generation is now an explicit stop signal to the inference server, not just a client-side disconnect.
- Workspace routes showed a blank screen instead of a loading state, sidebar chat search hung forever on a failed request, and code-preview artifacts threw on relative script and link URLs.
- The API stack runs on Python 3.13, and the image no longer compiles numpy from source on every build. (self.ai#60, self.ai#108)

## self.ai — 2026-07-28

### Security
- Closed a gap in the route authorization audit that had been hiding almost the entire API from it. The audit filtered the route table in a way that, on the pinned FastAPI version, skipped every route registered through an included router — it was inspecting 21 routes out of 478 and passing. It now walks the real route tree, and auth dependencies identify themselves explicitly instead of being recognized by function name. Seven unrecognized routes surfaced; three were real and are fixed here. (self.ai#51)
- A mod's frontend-manifest endpoint was anonymous, giving an unauthenticated caller a 404-vs-200 oracle over any mod id — which mods an instance runs, their custom-element tags, and their current bundle hashes. It now requires a verified user. (self.ai#70)
- The static route serving a mod's frontend assets served that mod's entire install directory, not just its built assets — Python sources, manifests, tests, and build inputs were all anonymously readable. It now serves web asset types only, checked against the resolved target, so a symlink named `logo.svg` pointing at a config file is refused for what it is. (self.ai#70)
- Service-to-service calls are authenticated end to end. Speech synthesis and transcription requests carry a scoped service ticket, and the curation harness and both evaluation harnesses now validate one on every control endpoint. These previously accepted unauthenticated calls from anywhere inside the deployment network. (self.ai#25)
- Markdown link targets are sanitized before rendering. A `javascript:` or `data:` URL arriving from model output, a shared chat, or a knowledge-base document can no longer become a clickable script payload.
- Tools and Functions that execute admin-supplied Python now carry an explicit warning stating that the code runs unsandboxed with full server privileges. The behavior is unchanged; the risk is no longer implicit.
- Admins can see and control what an installed mod is allowed to do. A new admin-only endpoint lists every scope a mod declares regardless of grant state, and the permissions editor renders one toggle per scope with its description. Previously a mod's scope was only visible once it had already been granted. (self.ai#69)

### Added
- **Anthropic models, natively.** A first-class Anthropic Messages API provider with its own Connections entry, rather than routing through an OpenAI-compatibility shim. Extended thinking is preserved and rendered in a separate reasoning panel instead of being inlined into the answer, and tool calls round-trip correctly. (self.ai#59)
- **Web access for models.** Two new tools: `web_fetch` reads a single page you name, and `deep_research` searches and then follows links across sources. Deep Research is a separate toggle from Web Search and ships off by default; admins control whether it is offered at all and cap how many pages one run may visit (default 10). Both honor `robots.txt`, including `Crawl-delay`.
- **Web Crawl.** A model can crawl a domain directly into a knowledge base you choose, with an in-chat toggle, a Configure modal for scope, and an admin surface for the defaults. Firecrawl-backed knowledge-base crawls now honor `Crawl-delay` as well.
- **Image generation in chat.** A self-hosted ComfyUI server backs image generation, running SDXL on the same GPU as inference — roughly 20 seconds for a 1024×1024 image. The inherited connector was hardened in the process: a failed or interrupted generation used to hang the request indefinitely, and the workflow node map is now pinned in configuration so it survives a restart.
- **Local vision.** Qwen2.5-VL is available as a local model, so images can be sent to a model running on your own hardware.
- **Cooperative GPU sharing.** Inference, speech, and image generation now negotiate for a single GPU's memory instead of competing for it. Consumers register what they hold, are asked to release when another service needs room, and are granted memory only after a release is actually confirmed — never speculatively. A consumer that stops responding can be reclaimed, and a stale hold is cleared only by an explicit admin action rather than being guessed at.
- **Mods, end to end.** The extension system is complete across backend and frontend: a YAML manifest contract, a permanent reference mod exercising every registration hook (its own route, Socket.IO namespace, model-callable tool, permission scope, and database table), and frontend loading — a mod ships its UI as a Svelte custom element served same-origin and loaded by native dynamic import, with registry-driven navigation. A mod declares its configuration in its manifest and core attaches it at boot, namespaced per mod, with secret values sourced only from the environment — so an operator configures a mod the same way they configure core, without the mod reading the environment itself.
- **Chat folders as retrieval presets.** A folder can carry its own model, tools, and knowledge bases, and chats created inside it inherit that setup. Web Search is available as a folder preset tool.
- **Self-hosted speech, configurable.** Connections gains a typed Audio section for self-hosted STT and TTS backends. Voices are discovered from the engine rather than hardcoded: admins pick a default voice and curate which voices are offered, and an individual model can override the default voice for its own replies. Transcription can route across multiple loaded models.
- **Speech on the GPU.** The speech service now ships a GPU image and runs on the shared card.
- **Documentation site.** Product documentation is published, covering Home, Getting Started, Models, Guides, and Contributing, plus a generated API reference served without any external CDN.

### Removed
- The inherited OpenWebUI Pipelines tab is gone. Nothing was behind it, and it advertised a capability self.ai does not have.
- Leftover single-model-mode assumptions in the inference server, which no longer matched how it actually loads models, were removed after an audit.

### Fixed
- Test Mode evaluation jobs were queued behind the GPU scheduling window even though they never touch the GPU. (self.ai#64)
- A training run rejected at dataset approval now names which dataset failed and why, instead of failing generically. (self.ai#66)
- An evaluation job that failed showed an empty results page with no explanation; the real failure reason is now surfaced.
- Transcription crashed during CUDA initialization on CPU-only nodes and could return a 502 on model load. Device detection is now deterministic and resolved once.
- The code evaluation harness sized its worker pool from the host's CPU count rather than its own container limit, oversubscribing under load.
- GPU memory over-grant hazards closed: a release could under-report what it actually freed, consumers that had not checked in were invisible to the broker, and a broker restart forgot what was already held. Reclamation is also priority-gated now — it can only target holders of strictly lower priority, and a requester's priority is read from the registry rather than taken from its own request.
- Mod tools could collide with built-in tool names, producing a request that was invalid at the wire; mod tools now open the tool-calling path correctly.
- Self-hosted audio connections did not survive a restart, because a value set only in the admin UI has no durable backing. They are now declared in configuration.
- A batch of chat interface fixes: dropdowns rendering behind modals, invisible text in dark and OLED themes across several settings surfaces, a folder's Configure dialog silently failing to save and then showing stale state when reopened, "New Chat" from inside a folder landing on a 404, and the input menu clipping longer tool names.

## self.ai — 2026-07-21

### Security
- Fixed a stored XSS vulnerability in file preview/inline-content serving — an uploaded file with a spoofed content-type could execute script when previewed. Hardened with strict server-side content-type detection (never trusts client-supplied metadata) plus CSP sandboxing on all file-content routes. (critical, self.ai#55)
- Outbound calls from the curator and eval workers (self.code-eval / self.language-eval) now authenticate with narrowly-scoped service tickets instead of broader shared credentials, reducing blast radius if a worker is ever compromised.

### Added
- New unauthenticated `/api/models/public` endpoint for read-only model-catalog access.
- Periodic model-integrity self-check — the platform now verifies loaded models on a schedule instead of only at load time.
- New local model presets available: Gemma 4, GLM-4.5-Air, Qwen3-Coder-Next.

### Removed
- Nothing removed this week.

### Fixed
- Reranker was returning near-zero garbage scores due to a missing classifier head.
- Model-list cache could be silently defeated, serving stale results.
- Cancelling a model pull could fail due to route-registration order.
- Knowledge-base uploads/deletes weren't always committing their version-control branch, risking inconsistent state.
- Async cleanup path for connection pooling used a blocking client, risking stalls under load.
- Dispatching an eval job to a different model without unloading the prior one could exhaust GPU memory.
- General GPU/CPU offload tuning across local models for stability under load, plus a memory-limit fix to prevent out-of-memory crashes.

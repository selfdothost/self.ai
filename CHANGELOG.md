# Changelog

All notable changes to self.ai are documented here. This is a curated, human-readable summary of what shipped each week — not a raw commit or merge log — organized by Security, Added, Removed, and Fixed. self.ai publishes weekly on Tuesdays.

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

# Architecture

How self.ai is put together: the components, what talks to what, and which decisions are
load-bearing.

## Shape

self.ai is not a single application. It is an API server plus a separate client, with a
set of optional satellite services behind it.

```text
                         ┌──────────────────────┐
      browser ───────────│   ingress / proxy    │
                         └──────────┬───────────┘
                        /                       \
             (static assets)                 (/api, /ws, …)
                   │                              │
        ┌──────────▼──────────┐        ┌──────────▼──────────┐
        │    chat client      │        │     API server      │
        │    (SvelteKit)      │        │     (FastAPI)       │
        └─────────────────────┘        └──────────┬──────────┘
                                                  │
        ┌──────────────┬──────────────┬───────────┼───────────┬──────────────┐
        │              │              │           │           │              │
   ┌────▼────┐   ┌─────▼─────┐  ┌─────▼─────┐ ┌───▼────┐ ┌────▼────┐  ┌──────▼──────┐
   │inference│   │   evals   │  │  curator  │ │  STT   │ │   TTS   │  │  external   │
   │(llama.  │   │ language  │  │           │ │        │ │         │  │  OpenAI-    │
   │  cpp)   │   │ + code    │  │           │ │        │ │         │  │  compatible │
   └─────────┘   └───────────┘  └───────────┘ └────────┘ └─────────┘  └─────────────┘

        shared infrastructure the API server consumes, not bundles:
        Postgres (+ pgvector) · cache/pubsub · object store · identity provider
```

## Components

| Component | Language | Role |
| --- | --- | --- |
| API server | Python / FastAPI | Every API route, the chat pipeline, retrieval, auth, admin config, and the proxies to all satellites |
| Chat client | SvelteKit | The browser client — conversations, workspace, admin panel |
| Inference | llama.cpp | Serves local GGUF models; model residency control, LoRA management, training-side control port |
| Language evaluation | Python | Language-understanding benchmark harness with its own control plane |
| Code evaluation | Python + many runtimes | Sandboxed multi-language code-execution benchmark harness |
| Curation | Python | Data-curation pipelines over knowledge bases |
| Transcription (STT) | Python | Speech-to-text behind an OpenAI-compatible endpoint |
| Voice (TTS) | Python | Text-to-speech behind an OpenAI-compatible endpoint |
| Image generation | Python (ComfyUI/AUTOMATIC1111 client) | Talks to a configured image-generation backend; inherited from the Open-WebUI fork, being hardened into self.ai's own |
| Vision (multimodal chat) | Python | Not a separate deployed component — multimodal message handling inherited from the Open-WebUI fork, within the API server |

Only the API server and the chat client are required. Everything else is optional and can
be replaced by an external service that speaks the same protocol.

## Same-origin by design

The client and the API are served under **one origin**, path-routed by the ingress or
reverse proxy: API paths (`/api`, `/ws`, `/oauth`, and the per-service proxy prefixes) go
to the API server; everything else serves the client.

The consequence is that **there is no CORS configuration**, because there is no
cross-origin request. If you split the client and API onto different hostnames you are
off the supported path and will have to solve authentication and websocket origin
yourself.

## Request path for a chat completion

Understanding this order matters if you write extensions.

1. The client posts to the chat completions endpoint over the same origin.
2. **Inlet filters** run, in ascending priority order. Each may rewrite the request body.
3. Retrieval runs if the turn references a knowledge base — the query is embedded, the
   vector store is searched, and results are injected into the context with citations.
4. Tool specifications are assembled from the enabled toolkits and offered to the model.
5. The request is dispatched — to a locally served model, an external OpenAI-compatible
   endpoint, or a pipe function presenting itself as a model.
6. Tool calls, if any, are executed server-side and fed back.
7. The response streams to the client over websocket.
8. **Outlet filters** run on the assembled response.

Filters and tools are covered in [Extending self.ai](extending.md).

## Data and state

| What | Where it lives |
| --- | --- |
| Users, chats, knowledge metadata, tools, functions, config | Postgres |
| Vector embeddings | pgvector in Postgres (Chroma and Milvus backends also exist) |
| Uploaded files and cache | A local volume on the API server |
| Session state, websocket pub/sub | The configured cache backend |
| Model weights | A volume mounted into the inference service |

!!! note "Object storage is configurable but not the deployed path"
    An S3-compatible storage provider exists in configuration, but the reference
    deployment keeps uploads and cache on a local volume. Moving knowledge-base storage
    to object storage is a rewrite, not a configuration flip.

## Service-to-service authentication

Calls from the API server to satellite services can be authenticated with short-lived
signed tickets — a JWT carrying issuer, audience, scope, and a unique id, valid for
minutes, minted per call from a shared secret.

!!! warning "Not every service validates tickets yet"
    Ticket auth is wired for the inference and curation services. The evaluation harnesses
    and the speech services do not yet validate them. Do not assume a satellite service is
    authenticated because the mechanism exists — verify per service, and keep satellites
    on a network that is not reachable from outside your cluster.

## Inference

The inference service runs llama.cpp in router mode with a models directory and a preset
file, holding a bounded number of models with **one resident at a time**. The API server's
inference router covers considerably more than proxying completions: model listing,
inspection, pull, and delete; explicit load/unload and slot inspection; LoRA application
and listing; pipeline bake; and training job submission with an approval step.

A separate control port handles training-side operations.

self.ai does not require this service. Any OpenAI-compatible endpoint can back chat
instead, and many deployments use one — local inference is an option, not a dependency.

## GPU arbitration

Inference, curation, evaluation, and training all want the same GPU. Rather than letting
them contend, self.ai schedules them: an operator defines **job windows**, and a
window-aware dispatcher with priority tiers releases queued jobs into those windows,
serialized by a distributed lock.

This is why a submitted training or curation job may sit queued rather than starting
immediately — it is waiting for its window. See
[Evaluation and training](evaluation-and-training.md).

## Fork provenance

self.ai is a **hard fork of Open-WebUI v0.5.4**, rebranded to the `selfai_ui` Python
package. Two things follow from that, and both are deliberate:

- **No upstream ingestion.** Code written upstream after v0.5.4 is not merged in. The fork
  does not track upstream and does not intend to.
- **GPL-3.0.** The fork point was MIT-licensed; attribution is retained in `NOTICE`. The
  project as it stands is GPL-3.0.

Substantial parts of the stack — the evaluation harnesses, curation, training, GPU
scheduling, the inference control surface, and the browse layer — are not from upstream at
all. See `DIVERGENCE.md` in the repository for the full record.

## Deliberate departures from the monolith

The compose-file origin bundled its own database, cache, and proxy. The current direction
is the opposite: **consume shared infrastructure, do not bundle it**. Postgres, cache,
object storage, identity, and ingress are all brought by the operator. If you are reading
old material that assumes a bundled stack, that assumption is retired.

## What is not built

- **Server-side code sandboxing** — chat code execution is browser-side WASM, and Tools
  and Functions run in-process with no isolation. Configuration for a sandboxed execution
  backend is scaffolded and off by default; nothing consumes it yet.
- **Browse integration into web search** — the browse profile layer exists but is not
  wired into the web search path.
- **Table-format integration for knowledge bases** — stubbed.

## See also

- [Installation](installation.md) — deploying the shapes described here
- [Configuration](configuration.md) — the settings that wire them together
- [Extending self.ai](extending.md) — where extension code runs in this picture

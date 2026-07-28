# self.ai

**Your selfhosted AI Provider. Build, Train, and Run inference models for you, your
family, your team, and more.**

Self.AI is a selfhosted AI Provider, offering the tools to run, train, and evaluate
inference models: [Image](image.md), [Language](language.md),
[Transcription](transcription.md), [Voice](voice.md), and [Vision](vision.md); collect and
curate the data needed to create and train these models; and finally, a documented
'[mod](mods.md)' surface that allows operators to add capabilities without rebuilding the
source.

![A conversation in self.ai, answered by a locally served model](assets/chat-conversation.png)

## Design principles

- **FOSS, GPL-3 licensed.** If you deploy it, it's yours.
- **Everything is spec-driven and modular.** Need more horsepower than SQLite can offer?
  Swap in Postgres. Need better datalake storage? Connect your LakeFS-compliant API and
  the core will automatically create repos in LakeFS for you when you create a knowledge
  base.
- **Open specs over invented protocols.** Inference is OpenAI-compatible. New protocols
  only where no open spec fits. See the [API Reference](api-reference.md) for the whole
  surface, generated straight from the code.
- **This is an alpha project.** If you find issues, please
  [submit an issue on GitHub](https://github.com/selfdothost/self.ai/issues). If you'd
  like to contribute, please see our [guidelines](contribute.md).

## Getting Started

Fast installation is just a `docker compose up` away — see our
[Quickstart](quickstart.md) guide for details. For a more in-depth walkthrough, see our
[Getting Started](start-here.md) page.

## What's in the Box?

| Component | What it does |
| --- | --- |
| [API server](architecture.md#components) | FastAPI application: chat completions, retrieval, files, users and auth, admin configuration, and the proxies to every other component |
| [Chat client](architecture.md#components) | The web client — conversations, folders, workspace, and admin panel |
| [Language](architecture.md#components) | A llama.cpp-based server for locally served GGUF models, including model residency control and LoRA management |
| [Image](architecture.md#components) | Image generation via a configured ComfyUI, AUTOMATIC1111, or OpenAI-compatible backend — inherited from the Open-WebUI fork, being hardened into self.ai's own |
| [Transcription](architecture.md#components) | Speech-to-text behind an OpenAI-compatible endpoint. Whisper only today; training and a Workspace panel are in progress |
| [Voice](architecture.md#components) | Text-to-speech behind an OpenAI-compatible endpoint. Training and a new Workspace panel are in progress |
| [Vision](architecture.md#components) | Multimodal chat, inherited from the Open-WebUI fork — being exercised and hardened into something stable |
| [Retrieval](architecture.md#components) | Document ingestion, chunking, embedding, and vector search over your own knowledge bases |
| [Evaluation](architecture.md#components) | Language-understanding benchmarks and a multi-language code-execution harness |
| [Curation](architecture.md#components) | A data-curation service with a node-graph pipeline editor attached to a knowledge base |

Each of those beyond the API server and client is optional. self.ai runs as a chat client
against any OpenAI-compatible endpoint without deploying any of them.

## Project status

self.ai is **pre-v1.0 and under active development**. It began as a hard fork of
Open-WebUI v0.5.4 and is being deliberately separated from that origin — see
[DIVERGENCE.md](https://github.com/selfdothost/self.ai/blob/main/DIVERGENCE.md) in the
repository for the fork's provenance, the no-upstream-ingestion rule, and the licensing
posture that follows from it.

Things worth knowing before you evaluate it:

- Some subsystems are shipped-but-partial. Each page in these docs carries a section
  saying what in that area is not yet built.
- The **mods** system — operator-installed, instance-wide extensions — is shipped
  (core hooks, a reference mod, and native client-surface loading). See [Mods](mods.md).
  The first real mod is being built against it now. For extensions authored by an
  individual user rather than installed by an operator, see
  [Tools and Functions](extending.md).
- Not every component image is published; some are build-from-source. See
  [Installation](installation.md).

## License

GPL-3.0. The project retains attribution for the MIT-licensed Open-WebUI code it forked
from; see `NOTICE` in the repository.

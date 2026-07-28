# Voice models

self.speak serves text-to-speech behind an OpenAI-compatible endpoint, so any client
speaking that API — including self.ai's own chat client — can use it without knowing it's
self-hosted.

## Serving

Deployed as its own [component](architecture.md#components); self.ai's API server proxies
to it rather than running voice synthesis in-process.

## In progress

Voice is being updated to include training, alongside a new **Voices** panel in the
[Workspace](using-selfai.md#workspace) — [Transcription](transcription.md) is getting the
same treatment, starting from further behind.

## Not shipped yet

There is no voice-model evaluation harness. Benchmarking coverage today is
[Language](language.md)-only; see
[Evaluation, Curation, and Training](evaluation-and-training.md).

## See also

- [Transcription](transcription.md) — the speech-to-text counterpart
- [Configuration](configuration.md) — the reference settings list

# Transcription models

self.transcribe serves speech-to-text behind an OpenAI-compatible endpoint, so any client
speaking that API — including self.ai's own chat client, for voice input — can use it
without knowing it's self-hosted.

## Serving

Deployed as its own [component](architecture.md#components); self.ai's API server proxies
to it rather than running transcription in-process. Whisper is the only real option out of
the box right now — offering anything else needs significantly more work than
[Voice](voice.md)'s equivalent backend swap does.

## In progress

Transcription is getting the same treatment as Voice — training, and a panel of its own in
the [Workspace](using-selfai.md#workspace) — starting from further behind.

## Not shipped yet

There is no transcription-model evaluation harness. Benchmarking coverage today is
[Language](language.md)-only; see
[Evaluation, Curation, and Training](evaluation-and-training.md).

## See also

- [Voice](voice.md) — the text-to-speech counterpart
- [Configuration](configuration.md) — the reference settings list

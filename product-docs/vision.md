# Vision models

Like [Image](image.md) generation, multimodal (image-input) chat is inherited from the
Open-WebUI fork — the capability is believed to be there, but it hasn't been exercised or
hardened as self.ai's own yet.

## In progress

Vision needs the same treatment as the ComfyUI/AUTOMATIC1111 connections: real, inherited
code that needs to be deliberately exercised and worked into something stable, rather than
trusted as-is because it happened to come along with the fork.

## Not shipped yet

There is no vision-model evaluation or training harness. Benchmarking and training
coverage today is [Language](language.md)-only; see
[Evaluation, Curation, and Training](evaluation-and-training.md).

## See also

- [Image](image.md) — the other capability inherited from the fork in the same state
- [Language](language.md) — the text-model serving self.ai does build for

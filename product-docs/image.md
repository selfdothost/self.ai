# Image models

self.ai's chat client can generate images from a configured backend — self-hosted or
remote — through the same admin-brings-own-infrastructure posture as everything else.

## Serving

Image generation is off by default (`ENABLE_IMAGE_GENERATION`). When enabled, an admin
picks the engine (`IMAGE_GENERATION_ENGINE`): an OpenAI-compatible image endpoint (the
default), or a self-hosted
[AUTOMATIC1111](https://github.com/AUTOMATIC1111/stable-diffusion-webui) or
[ComfyUI](https://github.com/comfyanonymous/ComfyUI) backend, each configured with its own
base URL. self.ai does not ship or run any of these — it talks to whichever one you point
it at.

## In progress

The ComfyUI and AUTOMATIC1111 connections are inherited from the Open-WebUI fork — real
and wired, but not yet exercised and hardened as self.ai's own, rather than code that just
came along with the fork's origins.

## Not shipped yet

There is no image-model evaluation or training harness. Benchmarking and training
coverage today is [Language](language.md)-only; see
[Evaluation, Curation, and Training](evaluation-and-training.md).

## See also

- [Configuration](configuration.md) — the reference settings list

# Language models

self.ai serves language models — the models behind chat completions, tool calling, and
retrieval-augmented generation — through an OpenAI-compatible API. Point it at any
OpenAI-compatible endpoint, or run models yourself.

## Local serving

[self.llamolotl](architecture.md#inference), a llama.cpp-based server, serves locally
hosted GGUF models. It supports model residency control (which model is loaded and for
how long) and LoRA management, both administered through its own control plane. See
[Architecture](architecture.md#inference) for where it sits in the request path.

## Context windows

`GET /api/models` reports the window each model is actually served with, so a client can
budget its own context instead of assuming one. Two optional integer fields per entry:

| Field | Meaning |
| --- | --- |
| `context_length` | Tokens one request may use — prompt plus generation. |
| `max_output_tokens` | Cap on generated tokens, when the backend sets one below the window. |

Three properties are worth relying on:

- **It is the served window, not the trained ceiling.** A model trained to 262144 tokens
  but served with `ctx-size = 16384` reports `16384`. The trained number is never
  published in its place: a caller that budgets against it fails exactly as hard as one
  that guessed.
- **It is populated for unloaded models.** Locally served models sit unloaded most of the
  time, and clients build their model table at startup. For those the value comes from the
  effective self.llamolotl preset — the `[*]` section plus any per-model override — so no
  model has to be loaded to answer. A loaded model reports the window its running process
  allocated, which takes precedence.
- **A missing field means unknown, not unlimited.** Models reached through an external API
  carry these fields only when that API publishes them. With no value, apply your own
  policy — self.ai will not invent one.

Admins set the local window in the self.llamolotl models preset; raising `ctx-size` for a
model raises what its entry reports, at the cost of KV cache VRAM.

## Benchmarking

self.language-eval runs language-understanding benchmarks against any configured model —
local or remote — with its own control plane and result storage. See
[Language evaluations](evaluation-and-training.md#language-evaluations).

## Training

Training job orchestration exists for language models today. See
[Training](evaluation-and-training.md#training) for scope and current limitations.

## See also

- [Architecture](architecture.md) — inference in the request path
- [Evaluation, Curation, and Training](evaluation-and-training.md) — the full harness
- [Configuration](configuration.md) — wiring an inference backend

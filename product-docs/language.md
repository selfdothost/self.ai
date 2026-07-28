# Language models

self.ai serves language models — the models behind chat completions, tool calling, and
retrieval-augmented generation — through an OpenAI-compatible API. Point it at any
OpenAI-compatible endpoint, or run models yourself.

## Local serving

[self.llamolotl](architecture.md#inference), a llama.cpp-based server, serves locally
hosted GGUF models. It supports model residency control (which model is loaded and for
how long) and LoRA management, both administered through its own control plane. See
[Architecture](architecture.md#inference) for where it sits in the request path.

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

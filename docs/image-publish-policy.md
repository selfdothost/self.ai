# Image publish policy (alpha)

Which self.ai container images are published to GHCR under `selfdothost`, and
which are build-from-source only for the alpha.

Public GHCR storage and bandwidth are free for public images, but two of our
images are large enough to be impractical for a first public push. Those ship as
build-from-source instead.

| Image | Disposition | Rationale |
|-------|-------------|-----------|
| `api` | **push to GHCR** | Torch-free Python slim, ~1–2 GB. |
| `self.chat` | **push to GHCR** | Static SPA served by nginx, ~0.1–0.3 GB. |
| `language-eval` | **push to GHCR** | Python slim, API-only extras, no torch. |
| `code-eval` | **push to GHCR** | Larger (language toolchains) but CPU-only, acceptable. |
| `curator` | **build from source** | 10 GB+ (RAPIDS + torch/cu128). A prior single-layer build exceeded the registry's blob-ingest limit. Do not push until slimmed (drop the RAPIDS dedup extra). |
| `llamolotl` | **build from source** | 8–12 GB; currently ships a CUDA `-devel-` base in its runtime stage. Do not push until the runtime stage is switched to `-runtime-` (see its own `Dockerfile.llama-only` for the pattern). |

The "build from source" images carry a size/cost warning in their own READMEs so
a public user knows to expect a large local build rather than a pull.

## Stretch (post-alpha)

`self.transcribe` (STT) and `self.speak` (TTS) are deferred out of the alpha. They
build cleanly (CUDA `-runtime-` base with mounted models; CPU image baking a
~327 MB voice model, respectively) and are push-ready, but speech is not part of
the first public cut — revisit when STT/TTS re-enter scope.

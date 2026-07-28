# Image publish policy (alpha)

Which self.ai container images are published to GHCR under `selfdothost`, and
which are build-from-source only for the alpha.

Public GHCR storage and bandwidth are free for public images, but two of our
images are large enough to be impractical for a public push. Those ship as
build-from-source instead.

| Image | Disposition | Rationale |
|-------|-------------|-----------|
| `api` | **push to GHCR** | Torch-free Python slim, ~1–2 GB. |
| `self.chat` | **push to GHCR** | Static SPA served by nginx, ~0.1–0.3 GB. |
| `combined` | **push to GHCR** | api + self.chat assembled into one container (`Dockerfile.combined`), built after `api` by `.github/workflows/publish.yml`. This is the single-container path public users get from `docker-compose.combined.yml`. **Not used in the yard** — the cluster runs the split services under k3s and never pulls this image. |
| `language-eval` | **push to GHCR** | Python slim, API-only extras, no torch. |
| `code-eval` | **push to GHCR** | Larger (language toolchains) but CPU-only, acceptable. |
| `self.transcribe` (STT) | **push to GHCR** | Cleanup landed; CUDA `-runtime-` base with models mounted rather than baked. |
| `self.speak` (TTS) | **push to GHCR** | Cleanup landed; CPU image baking a ~327 MB voice model. |
| `curator` | **build from source** | 10 GB+ (RAPIDS + torch/cu128). A prior single-layer build exceeded the registry's blob-ingest limit. Do not push until slimmed (drop the RAPIDS dedup extra). |
| `llamolotl` | **build from source** | 8–12 GB; still ships a CUDA `-devel-` base in its runtime stage. Do not push until the runtime stage is switched to `-runtime-` (see its own `Dockerfile.llama-only` for the pattern). |

The "build from source" images carry a size/cost warning in their own READMEs so
a public user knows to expect a large local build rather than a pull.

## Docker images vs the yard

Everything above is about the **public** distribution. Internally the yard runs
k3s and pulls the split per-service images from the private registry, pinned by
the Flux manifests. The `combined` image exists for public single-container
users and has no internal consumer.

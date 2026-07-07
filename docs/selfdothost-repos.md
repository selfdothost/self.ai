# selfdothost repository set

self.ai publishes to the GitHub organization **`selfdothost`**. GitHub has no
subgroups, so the private GitLab hierarchy (`selfshipyard/selfai/*`) is flattened
into one org. Each public repo keeps the same `self.<name>` name it has as a
submodule path, so `git clone --recursive` resolves cleanly (the relative
`../self.X.git` submodule URLs collapse correctly against a flat org).

| Repo | Role | Relationship to self.ai |
|------|------|-------------------------|
| `self.ai` | **Flagship** — FastAPI API server + deploy | this repo |
| `self.chat` | Web client (SvelteKit) | sibling repo |
| `self.code-eval` | Code-generation eval harness | submodule |
| `self.language-eval` | General LLM eval harness | submodule |
| `self.curator` | Data curation | submodule |
| `self.llamolotl` | LLM inference + training (llama.cpp + DeepSpeed) | submodule |

**Excluded:** `crew-code` (and everything under self.crew) is **not** part of the
`selfdothost` public set.

## Stretch (post-alpha) — not in the first mirror

| Repo | Role | Relationship to self.ai |
|------|------|-------------------------|
| `self.kokoro-fastapi` | TTS service | submodule |
| `self.faster-whisper` | STT service | submodule |

Speech (STT/TTS) is deferred out of the alpha public set. These two remain
submodules for the yard deployment, but their public repos are **not** created
in the alpha and their submodule entries are **mirror-excluded** (like `context/`
and `manifests/`).

> **Recursive-clone caveat.** Because `.gitmodules` still lists the two speech
> submodules, a `git clone --recursive` of the public `selfdothost/self.ai` would
> try to resolve `selfdothost/self.faster-whisper` / `self.kokoro-fastapi`, which
> won't exist. The mirror step must drop those two `.gitmodules` entries (or the
> public README documents cloning them from the forge). Tracked with the
> `manifests/` mirror-exclusion blocker on self.ai#10.

Every published repo name matches its submodule path name exactly; a
`git clone --recursive` of `selfdothost/self.ai` resolves the alpha submodules to
their `selfdothost/self.<name>` counterparts.

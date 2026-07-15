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
| `self.speak` | TTS service | submodule |
| `self.transcribe` | STT service | submodule |

**Excluded:** `crew-code` (and everything under self.crew) is **not** part of the
`selfdothost` public set.

Every published repo name matches its submodule path name exactly; a
`git clone --recursive` of `selfdothost/self.ai` resolves the alpha submodules to
their `selfdothost/self.<name>` counterparts.

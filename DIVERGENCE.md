# DIVERGENCE — self.ai vs Open-WebUI

**Read this before pulling, merging, or referencing any Open-WebUI code.**

## Provenance

self.ai's API + WebUI is forked from **Open-WebUI v0.5.4** (released
2024-01-05) — the **last MIT-licensed release**. The fork point is pinned
in our history at commit **`0382c9f`** ("Forking Open-WebUI from v.0.5.4
the last MIT ver"). The MIT baseline is retained verbatim in
[`NOTICE`](NOTICE) (© 2023 Timothy Jaeryang Baek).

self.ai is **not affiliated with, endorsed by, or connected to** the Open-WebUI
project. This is a factual record of a fork made from Open-WebUI's MIT-licensed
v0.5.4 for its multi-user focus — nothing more.

**License of record:** self.ai is **GPLv3** (root `LICENSE`). MIT→GPLv3
for the combined work is lawful and deliberate.

## The firewall (hard rule)

After v0.5.4, Open-WebUI relicensed away from MIT to terms that forbid the
branding and structural changes we have already made. We are legally safe
**only** by staying on the MIT lineage. Therefore:

1. **No code ingestion from post-v0.5.4 Open-WebUI. Ever.** No merges, no
   cherry-picks, no copy-paste, no "reference their file." Not one line.
2. **No upstream git remote.** `origin` is ours; there is nothing to pull.
3. **Parity is clean-room only.** If Open-WebUI ships a fix or feature we
   want, read the *advisory / CVE text* for awareness, then reimplement
   from scratch against our own code. Never read or copy their post-MIT
   source.

## What we've diverged (high level)

The fork is substantially our own, not lightly-patched Open-WebUI:

- **Rebrand** to self.ai (best-effort complete; package is `selfai_ui`).
  Spot a leftover Open-WebUI reference we missed? Comment on
  [selfdothost/self.ai#1](https://github.com/selfdothost/self.ai/issues/1).
- **GPU job orchestration** — a window-aware, RedisLock-guarded dispatcher
  (`api/selfai_ui/utils/gpu_queue.py`).
- **Training / eval / curation control planes** + forwarding routers
  (llamolotl, curator, language-eval, code-eval, images, benchmarks, windows).
- **Job state machines** (7-state vocabulary) + 9 dedicated Alembic
  migrations + a bespoke **node-graph curation UX**.

The original Open-WebUI subsystems (auth, chat, RAG, retrieval, files,
channels) remain under the retained MIT notice for now — not because they're
finished, but because effort so far has gone into the curation, training, and
eval stack first. A full chat-system overhaul is planned before final
release: Knowledge Bases integrated directly into chats, and chat folders
that carry a default model plus included KBs and datasets. Once that UI work
lands, the project moves into beta, centered on a **mods** system —
self.crew and crew-code will be the first mod built on it.

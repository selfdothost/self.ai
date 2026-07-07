# self.ai ROADMAP

self.ai is the yard's **AI platform** — a GPLv3 product we own, forked from
Open-WebUI v0.5.4 (the last MIT release) and diverged into our own thing.
Captain: **Data**. Planning surface: OpenProject project `self.ai`
(`OP#36`).

- **Product vision (the destination):** `context/treasuremaps/2026-06-20-product-offering.md` — the 9 pillars.
- **Legal floor:** `context/treasuremaps/2026-06-19-fork-and-license-posture.md` — MIT-baseline hard fork, upstream code firewall, GPLv3.
- **Deployment teardown:** `context/treasuremaps/2026-06-19-self-ai-teardown.md` — monolith → lean k8s.

## How this ladders into the shipyard

The matey-harness is live (H0–H4, 2026-06-12); the yard's active phase is
the **crew multiplier**, and the standing doctrine is *"build-from-source
is the crew's first real body of work."* self.ai is the flagship of that
phase: a from-source product we own, captained by Data, built by the crew.
**Matey's Marsh** is the reference deployment; the same component then
ships inside every replicated self.shipyard. self.ai's waves land **inside**
the shipyard capability map, not beside it.

## The four waves

### Wave 0 — Foundation: own it, lean it, make it buildable
*Unblocks everything. Nothing else starts until this lands.*
- De-upstream (`OP#222`): rebrand, `DIVERGENCE.md`, GPLv3, strip GitHub residue
- Strip to lean k8s, M1 durable single-replica (`OP#203`)
- CI/CD replacing the dead GitHub Actions + pull-from-registry
- **Cavekit** (selftools#10) + **Playwright** (selftools#11) so the crew builds *and tests* autonomously

→ *self.ai stands up clean in-cluster; the crew can iterate on it.*

### Wave 1 — Baseline product (commodity pillars, solid not fancy)
- ① **Serve** — OpenAI-compatible API, Keycloak-federated, external API keys
- ③ **Inference** — vLLM + llama.cpp, externalized (extraction `OP#217`)
- ② **UX** — WebUI + Audio I/O + shipped default TUI
- ⑥ **Knowledge** — qdrant RAG + native **self.cloud** document integration

→ *a working, owned AI platform — "every other frontend," but ours.*

### Wave 2 — The moat (the differentiators — this is the product)
- ⑧ **Storage/VCS** *(first — the iterate enabler)* — LakeFS + Nessie over Garage S3
- ④ **Evals** — exit exams + user-built test suites for character/model targets
- ⑤ **Train** — baseline → fine-tune existing → train from scratch
- ⑦ **Curation** — pipelines **+ node-graph programming UX**
- ⑨ **Job Window control** — GPU arbitration → WebUI "no local gen" mode

→ *the reason a self.shipyard is worth running.*

### Wave 3 — Productize (Admin + multi-tenant → client-replicable)
- ⑨ **Admin console** — user admin, model perms, storage quotas, log routing
- Per-tenant isolation; per-client deployability

→ *Matey's Marsh "finished"; self.shipyard replicable to clients.*

## Future considerations (flagged, not scheduled)

- **Split the Svelte UI into its own repo** (`OP#236`). The WebUI is
  already a client-side Svelte app on the API. A dedicated UI repo eases
  the Node-free API-only image (`OP#221`) and reuse of the Svelte codebase
  as an **Android/iOS app**. Admiral flag: *much later* — a decision-point
  to time, not act on now.
- **Knowledge storage is a rewrite, not a config flip** (`OP#235`). The
  KB/dataset/upload storage subsystem gets rewritten on Garage S3,
  untangling the dataset↔KB coupling that damaged the Knowledge system
  (current stopgap: per-KB UUID subfolder). Distinct from the pgvector
  vector-path move (Wave 1 ⑥, which *is* a flip).

## Sprint model

Autonomous **micro-sprints — hours up to 24h.** Data decomposes → `Task`
CRs → mateys build → Cavekit-shaped Draft MRs. Admiral's loop per sprint:
**review → request refinements (reshapes next sprint)**, or **approve →
merge → watch deploy.**

**Current: Sprint 1 — State-of-self.ai survey** (reconnaissance; Data's
captain report on where things stand, what's ground-up, what needs
redesign to fit the yard, what's salvageable). OpenProject version
"Sprint 1".

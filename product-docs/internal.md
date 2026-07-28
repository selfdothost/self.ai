# Internal notes

Everything above this page is product documentation — written for someone running or
extending self.ai. This page is the door to the material that is **not** product
documentation but is built into this site anyway, because it is where the reasoning lives.

None of it is nav'd individually. All of it is full-text searchable from the search box.

!!! note "Not present in public mirrors"
    The `context/` tree described below is excluded from the public repository mirror. If
    you are reading this page from a public checkout rather than the built docs site, the
    directories it points at will not be there.

## Operator handoffs

`handoffs/` carries operational notes that are more specific than the product docs:

- **Config reference** — the curated operator settings table, with citations into the
  source. [Configuration](configuration.md) supersedes it for reading; this is the
  working document it was built from.
- **Image publish policy** — which component images are published and which are
  build-from-source, and why.
- **Public repository map** — how the published repositories relate to each other and to
  the submodules in this tree.

The release-engineering credential inventory is deliberately **not** included in this
site. It lists vault paths and token scopes rather than secret values, but it is not
something a browsable docs site should carry.

## Decision records

`context/treasuremaps/` holds forward-looking decision records — what was decided, why,
and which alternatives were rejected. If you are asking "why does self.ai do X this way",
that directory is the answer, and it is usually more current than any prose summary.

`context/plans/`, `context/kits/`, and `context/impl/` hold the specification and
implementation-tracking material behind in-flight work. `context/impl/` in particular is
the honest record of what is actually finished versus what is scaffolded — when this
site's product pages and that directory disagree, the tracking documents are more likely
to be right, and the product page is a bug.

## Contributing

The repository root carries the contributor-facing files:

- `CONTRIBUTING.md` — the DCO sign-off requirement, the AI-assistance disclosure trailer,
  and the merge request flow.
- `DIVERGENCE.md` — the fork's provenance from Open-WebUI v0.5.4, the rule against
  ingesting upstream code written after that point, and the licensing posture.
- `SECURITY.md` — how to report a vulnerability.
- `SECURITY_HARDENING.md` — the hardening posture of a deployed instance.
- `CODE_OF_CONDUCT.md`, `NOTICE`, `LICENSE`.

## Keeping this site honest

Two rules this documentation is written under, stated so they can be held to:

1. **Unshipped is marked unshipped.** Every page carries a section naming what in its area
   does not work yet. A feature described without such a caveat is a claim that it works.
2. **Claims are traceable.** Behavior described here should be checkable against the
   source. If you find a page asserting something the code does not do, that is a
   documentation defect worth filing.

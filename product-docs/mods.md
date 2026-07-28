# Mods

Status: **Phases 0-2 are shipped.** Core hooks, the reference mod, and native
client-surface loading are all built, tested, and running. This page documents what
is actually there — how to author a mod, how to install one, and the contract it
must satisfy — not a draft.

The first real mod (self.crew's crew mod, tracked in `selfshipyard/self.crew#141`)
is being built against this contract now. Gaps it finds get fixed here, the same
way the reference mod's own construction found and closed several (see
[Definition of done](#definition-of-done) and [Known gaps closed post-launch]
(#known-gaps-closed-post-launch)).

If you want to extend a self.ai instance with something scoped to one user's own
chat experience, read [Extending self.ai](extending.md) instead — Tools, Functions,
and Pipelines are the shipped, user-authored surface. Mods are the operator-installed
tier.

---

## Why mods exist

self.ai today is a chat client with an extension surface inherited from its Open-WebUI
origins. Mods are the tier that turns it into a **platform other systems plug into**: an
operator installs a unit that brings its own API, its own live surfaces, its own
model-callable capabilities, and its own permission boundaries, without rebuilding either
the API image or the client.

That is a different job from Functions, and the split is by **trust**, not capability
alone.

| | Functions / Tools (shipped) | Mods (shipped) |
| --- | --- | --- |
| Who authors | An end user, in the UI | An operator, who installs it |
| Trust model | Scoped to the authoring user | Operator-vetted, trusted like any admin-installed component |
| Reach | The author's own chat experience | The whole instance — every user |
| Can do | Tools, filters, and pipes on the chat path | Mount API routes, live event namespaces, client surfaces, model-callable tools, background services |
| Permissions | Bound to the authoring user | Declares its own scopes; the admin grants them |
| Distribution | Authored in-UI, stored in the database | Installed as a package, enabled via config |

The short version: *a Function is something a user writes to improve their own chat; a Mod
is something an operator installs to extend the instance, and it carries both its
capabilities and its permission boundaries with it.*

## What a mod contributes

1. **API surface** — FastAPI routers mounted under the mod's own prefix.
2. **Live surface** — a Socket.IO namespace for streaming events to the client.
3. **Tool surface** — model-callable tools, so the mod's capabilities are reachable by the
   model and not only by a human clicking.
4. **Client surface** — a native view in the chat client's own navigation, shown only to
   users holding the relevant scopes.
5. **Permissions** — the scopes it introduces, which the admin grants to groups.
6. **Sidecar** — optionally, a companion container for a runtime that cannot live
   in-process. Most mods have none.

A mod may contribute any subset. A mod contributing only a tool surface is valid and
useful — the reference mod itself is proof: it ships all six, but a real mod is free to
ship fewer.

## The manifest

Every mod ships a `mod.yaml`:

```yaml
id: example
name: Example
version: 0.1.0
min_core_version: "0.5.0"
entrypoint: example_mod:Mod              # Python entrypoint object in the API process

api:
  prefix: /example                       # routers mount here

ws:
  namespace: /example                    # Socket.IO namespace on core's server

frontend:
  bundle_url: /static/mods/example/entry.<hash>.js  # served by the per-mod asset surface
  view: example                          # the id this mod's nav entry resolves through
  label: Example                         # nav label
  icon: puzzle                           # nav icon
  add_to_nav: true                       # whether this view gets a primary-nav entry
  scopes:                                # gating scopes -- must be this mod's own
    - mods.example.thing.read
  # tag: mod-example                     # optional; derived from `id` when omitted

db:
  table_prefix: mod_example_             # every table this mod owns starts with this

# Scopes the mod introduces. Dot-separated, always rooted at `mods.<id>`.
scopes:
  - id: mods.example.thing.read
    desc: View things
  - id: mods.example.thing.write
    desc: Create and modify things

# Operator-settable configuration this mod needs. Optional.
config:
  - key: api_key
    desc: Upstream API credential
    secret: true
```

YAML rather than TOML: a mod is deployed alongside a stack whose configuration is
already YAML throughout, and mod manifests are read by the same people writing those.
One format to rule them all; and in the core, bind them.

A mod declaring no `frontend` block is valid — it contributes no client UI, only
whatever subset of API/WS/tool surface it needs. `bundle_url` in the `frontend` block is
what the manifest declares at author time; what a client actually fetches at runtime is
a *resolved*, content-hashed URL served fresh on every view-entry — see
[Client-surface loading](#client-surface-loading).

### Why scopes are dot-separated

Core already has a permission checker: `has_permission(user_id, key, defaults)` in
`api/selfai_ui/utils/access_control.py`. It splits a dot-separated key, walks a nested
boolean dict of group permissions, and falls back to instance defaults. Group permissions
are stored as an arbitrary JSON dict on the group row.

A scope named `mods.example.thing.read` therefore works with **no changes to core's
permission logic at all** — it is checked exactly the way `workspace.models` is:

```json
{
  "workspace": { "models": true, "knowledge": true },
  "mods": {
    "example": { "thing": { "read": true, "write": false } }
  }
}
```

This is why the contract uses dot-namespacing rather than colon-style scope ids. A mod's
permissions become literally the same kind of object as a built-in one, which is what
"indistinguishable from a built-in" has to mean in practice.

Rooting every mod scope at `mods.<id>` also guarantees no mod can collide with a core
permission key or with another mod's.

## The core contract

What core self.ai provides so an installed mod can register itself.

### 1. Discovery

At boot the API scans every directory in `MODS_INSTALL_DIRS` — the built-in `MODS_DIR`
(env `MODS_DIR`, defaulting to a `mods/` directory inside the image) plus any extra
locations named in `MODS_EXTRA_DIRS` (semicolon-separated) — for a subdirectory
containing a `mod.yaml`. Whichever of those a mod's id appears in the `ENABLED_MODS`
list gets loaded; everything else is present but inert. A disabled mod is not loaded and
contributes zero surface. `ENABLED_MODS` is a `PersistentConfig` value, so it is
admin-settable, but changes take effect on the *next* boot — there is deliberately no
hot-reload, so the surface an operator reviewed is always the surface that is serving.

**Installing a mod is drop-a-directory, flip `ENABLED_MODS`, restart — never a rebuild.**
That includes making the mod's own Python code importable: discovery finds a mod's
`mod.yaml` by reading it directly off disk, but the loader also adds that mod's own
install directory to `sys.path` before importing its entrypoint, so a mod dropped via
`MODS_EXTRA_DIRS` needs no `PYTHONPATH` wiring in the deployment manifest. This is a real
gap that was open for a while (found while wiring self.crew's crew mod — see
[Known gaps closed post-launch](#known-gaps-closed-post-launch)); it is closed now and a
mod author should never need to think about it.

### 2. Registration hooks

Each mod's entrypoint exposes a `Mod` object:

| Hook | Purpose |
| --- | --- |
| `register_routers(app)` | Mount the mod's API routers under its prefix |
| `register_ws(sio)` | Register a Socket.IO namespace on core's existing server |
| `register_tools()` | Return model-callable tool specifications |
| `register_lifecycle()` | Startup and shutdown hooks for background work |

`register_routers` is the same `app.include_router(...)` shape every first-party router
already uses, so a mod router is structurally identical to a built-in one — it is simply
mounted by the loader rather than hand-written into the application module.

An object exposing none of these four hooks is refused at load time — it is not a mod,
and the loader says so rather than loading a no-op.

### 3. Live events are a namespace, not a second stack

Core mounts a Socket.IO server as an ASGI sub-app at `/ws` (`api/selfai_ui/main.py`),
authenticating each connection by decoding the user's token on connect. It can be backed
by the cache for cross-replica delivery.

A mod therefore registers a **namespace on that server** rather than mounting websocket
endpoints of its own. It inherits, for free:

- token authentication on connect, with the same identity core sees
- reconnection and buffering behavior the client already implements
- cross-replica fan-out when the cache-backed manager is enabled

A mod that mounts its own websocket stack gets none of those and must rebuild all three.
The reference mod proves the namespace path end to end: it streams state changes over
its own namespace to `emit_to_user` — the facade helper that targets a specific user
without a mod needing to know the user-to-session mapping itself (see
[What a mod may import from core](#what-a-mod-may-import-from-core)).

### 4. Tools

`register_tools()` returns a list of **tool declarations** — one per model-callable tool
the mod contributes. Core turns each into exactly the shape it already builds for
Functions-authored toolkits, so the model sees no difference between a mod-provided tool
and a user-authored one.

A declaration is a plain dict (or a `ModTool`; core coerces a dict into one for you):

```python
def register_tools(self):
    return [
        {
            "name": "submit",                        # required; [A-Za-z0-9_-]{1,64}
            "description": "Submit a unit of work.", # required; the model reads this
            "handler": submit_task,                  # required; sync or async
            "scope": "mods.reference.use",           # required; see below
            "parameters": None,                      # JSON Schema, or None for no args
            "output_schema": TOOL_OUTPUT_SCHEMA,     # optional
        }
    ]
```

**Return dicts, not `ToolSpec`.** `ToolSpec` is what core builds *from* your declaration
further down the pipeline; it carries no `handler` and no `scope`, so it cannot express a
mod tool at all. `ModTool` is the equivalent typed form, but it lives in
`selfai_ui.mods.tools` — outside the facade — so importing it would break a mod's import
boundary. A dict is the sanctioned shape, and the one the reference mod uses.

Bad `name` or missing `scope` is refused at **boot**, not at call time, and the whole
mod's tool set is refused with it: a tool with no gate is a capability the operator never
agreed to expose. The charset is the intersection of what OpenAI and Anthropic accept, so
a name that validates here reaches either provider.

Each tool declares the scope it requires. A user without that scope is never offered the
tool, and a call made anyway is refused server-side — the scope check is authoritative at
the call site, not merely a UI filter.

This is the hook that makes a mod a **capability provider** rather than a set of extra
pages: whatever the mod can do becomes something the model can be asked to do, bounded by
what the operator granted.

A tool may also declare `output_schema`, a JSON Schema describing what its handler
returns. Nothing on the wire changes when you do — no provider self.ai talks to today
defines a result-schema field, so it is carried and inert. It exists so a
handle-returning tool (the async `{task_id, status}` pattern — a tool that starts
background work and returns immediately rather than blocking until it finishes, streaming
the eventual result over the mod's own WS namespace instead) can describe its result
without waiting on a model change. The reference mod's own `submit` tool is exactly this
shape: it returns a `{task_id, status}` handle and the caller observes completion over
the namespace.

!!! warning "For Function authors: `__tools__[...]["spec"]` is a typed model now"

    This one is about the tool set core *hands out*, not the declarations a mod returns
    — if you are writing a mod, the shape above is all you need.

    Once core has assembled a tool, its specification is a `ToolSpec`: a pydantic model
    exported from `selfai_ui.modapi`, carrying `name`, `description`, `input_schema` and
    the optional `title` and `output_schema`, using MCP's field names. Serialization to a
    provider's wire format happens at the edge — `to_openai()`, `to_anthropic()`,
    `to_mcp()` — so nothing upstream ever picks a dialect.

    **This replaced an untyped `dict[str, Any]`.** If you have a Function plugin that
    reads the tool set core hands it as `__tools__`, its `["spec"]` entry is now a
    `ToolSpec`, and subscripting it raises `TypeError`:

    ```python
    name = tool["spec"]["name"]       # was
    name = tool["spec"].name          # now
    spec = tool["spec"].to_openai()   # or, for the whole old dict shape
    ```

    A compatibility shim was considered and rejected: making `ToolSpec` half-dict-like
    would let the first access succeed and break later, further from the cause. It fails
    immediately instead, at the plugin's own line.

    Stored tool specifications are unaffected — the database shape did not change and no
    migration is involved.

### 5. Scope ingestion

Core reads the mod's declared scopes and seeds them, deny-by-default, into instance
permission defaults on every boot a mod loads — the admin-UI-visibility and
sensible-default half of the dot-namespacing story above. Seeding never overwrites an
existing value, so an admin's prior grant survives a restart.

### 6. State and migrations

A mod with state owns tables. Core already runs Alembic
(`api/selfai_ui/alembic.ini`, with versions under `api/selfai_ui/migrations/versions/`),
so mods do not need a new migration story — they need a disciplined slice of the existing
one:

- Every table a mod creates is prefixed per its manifest (`mod_example_`).
- The mod ships its own migration revisions and they run as part of the normal upgrade,
  gated behind the mod being enabled.
- A mod never alters a core table. If it needs to relate to one, it does so by id from its
  own table.

The reference mod's own migration (`mod_reference_handles`, gated on the mod being
enabled) is the executable proof of this shape.

### 7. Configuration

A mod needing operator configuration — endpoints, credentials, limits — declares the keys
it needs in its manifest's `config` block; it must not invent its own mechanism. Core
turns each into a `PersistentConfig` entry on `AppConfig`, persisted at
`mods.<id>.config.<key>` — the same object every first-party setting already is, so mod
config exports and imports exactly the way core settings do. No new storage, no parallel
admin surface.

A key marked `secret: true` never takes its value from the manifest (a file in an image,
readable by anyone who can read the image, and it lands in version control). Its value
comes from the deployment's existing secret path instead — the environment variable
`MOD_<ID>_<KEY>` (upper-cased, id and key both namespaced the same way, so two mods
wanting the same key name never collide).

**Reading a value back.** Attachment happens at boot, immediately after the mod loads. A
mod reads its resolved value off the application config under that same derived attribute
name — it does not read the environment itself:

```python
@router.get("/config")
def read_config(request: Request):
    api_key = getattr(request.app.state.config, "MOD_EXAMPLE_API_KEY", None)
```

`getattr` with a default rather than a bare attribute access: an unattached key raises
`KeyError` off `AppConfig`, and a mod reporting its own configuration should say "absent"
plainly. The reference mod's `GET /reference/config` is the worked example — it covers
both an ordinary key with a manifest default and a secret sourced only from the
environment, and reports the secret as set-or-not rather than echoing it.

A mod whose configuration cannot be attached is recorded as an error and skipped; its
other surfaces still load and the instance still boots, matching
[Failure isolation](#8-failure-isolation).

### 8. Failure isolation

A mod that throws during registration is disabled with a logged error; it does not prevent
core or other mods from booting. Mod routers run behind error isolation, so an exception
in a mod route does not take down the API. An unhealthy sidecar degrades that mod's
surface, not the instance.

Each mod logs under its own namespace (`selfai_ui.mods.<id>`) so its failures are
attributable without reading core's logs sideways.

### 9. The registry endpoint

`GET /api/v1/mods/enabled` returns every enabled, loaded mod the caller may see:

- An **admin** sees every loaded mod, full stop — an admin surface that hid one would
  lie about instance state.
- A **non-admin** sees a mod only if they hold at least one of its declared scopes; an
  unheld mod is absent from the response entirely, never flagged or nulled.

Each entry carries `id`, `name`, and `scopes` (the ones *this* caller holds — evaluated
through the same `has_permission` enforcement uses, so the report can never disagree with
what is enforced). If the mod declares a `frontend` block, the entry also carries
`bundle_url`, `view`, `label`, `icon`, and `add_to_nav` — everything the client needs to
render nav without a second request. A mod with no `frontend` block omits all five.

This is the single source of truth the client consumes to decide what mod nav to render.

!!! note "Accepted Phase 0 limitation"
    The registry response is scope-filtered, but a mod's *routes* are mounted for every
    caller regardless — probing a guessed route prefix still discloses that a mod exists,
    even to someone the registry would never list it for. This is a recorded, accepted
    limitation, not something believed fixed.

    Narrower than it was: the per-mod `frontend-manifest` endpoint used to answer
    anonymously, which made it an enumeration oracle over any mod id rather than something
    a prober had to already guess. It now requires an authenticated user. It is still not
    scope-filtered — an authenticated caller holding none of a mod's scopes can still
    resolve it — so the limitation stands for logged-in callers and only the anonymous
    case is closed.

## What a mod may import from core

A mod imports **only from `selfai_ui.modapi`** and from nowhere else in the package.
Everything else under `selfai_ui` is internal and may be renamed, moved, or rewritten
without notice.

The facade's current contents:

| Name | What it's for |
| --- | --- |
| `get_verified_user` | Identity/auth, usable as a route dependency by a mod router |
| `has_permission` | The same permission checker core uses for enforcement |
| `permission_defaults` | The instance default permission tree `has_permission` falls back to. Needed wherever there is no request to read it off app state — a Socket.IO handler, a watch loop |
| `get_user_id_from_session_pool` | The authenticated user behind a Socket.IO connection (sid → user id), for an ownership check inside a `register_ws` handler |
| `get_db` | Database session access |
| `sio` | Core's Socket.IO server — mods register a namespace on it |
| `emit_to_user` | Emit to a specific user from outside a request context (a watch loop, a background task) without knowing session ids — raw `sio` cannot target a user, since the user-to-session mapping is internal |
| `ToolSpec`, `get_tools_specs` | Tool specification types, in the shape core builds for user-authored toolkits. Not the shape `register_tools()` returns — see [Tools](#4-tools) |
| `CORE_VERSION` | The running core version, compared against a mod's `min_core_version` |

Adding a name here is a decision, not a convenience — `tests/test_mods_facade_boundary.py`
fails the build if a name is removed or renamed without a major-version bump. Names may be
*added* in a minor release.

!!! warning "Always pass the defaults to `has_permission`"

    `has_permission(user_id, key, default_permissions)` resolves a scope against the
    caller's groups and *then* falls back to the instance defaults. The third argument
    defaults to `{}`, so a two-argument call **denies every scope an admin granted through
    instance defaults rather than through a group** — silently, and in the direction that
    looks like a correct refusal.

    ```python
    has_permission(uid, "mods.example.thing.read")                        # denies
    has_permission(uid, "mods.example.thing.read", permission_defaults()) # correct
    ```

    On a route you may read the same tree off the request
    (`request.app.state.config.USER_PERMISSIONS`); they are the same object. Off the
    request path — a namespace handler, a watch loop — `permission_defaults()` is the
    supported source.

The reason to pay for this upfront is narrow and important: **the moment a real mod ships
against core's internals, core stops being able to refactor them.** self.ai is mid-way
through separating itself from its Open-WebUI origins, and a mod ecosystem pinned to the
internals of that origin would weld the fork's worst inheritance permanently in place. A
facade keeps the seam movable.

A mod declares `min_core_version`. Core refuses to load a mod whose requirement it does not
satisfy, rather than failing later in an unrelated place.

!!! warning "Not enforced for third-party mods"
    This boundary is checked automatically only for mods *in this repository* (the
    reference mod). A mod is trusted code running in the API process with the API's
    privileges — it can import whatever it likes and nothing at runtime stops it. For a
    mod you did not write, the facade is an install-time review obligation: read its
    imports before you enable it.

## Client-surface loading

A mod's UI is a **Svelte 5 custom element**, compiled to a standalone bundle, served
same-origin — never compiled into the chat client at build time, and never an iframe.

There is exactly **one generic, id-parameterized route** in the client (`/mods/[id]`)
that every mod's nav entry resolves through. Adding a mod never touches the client's
route tree and never triggers a client rebuild. What happens when a user opens it:

1. The client fetches `GET /api/v1/mods/{id}/frontend-manifest` — a small, always-fresh
   endpoint (`no-cache`/revalidating headers) that resolves the mod's *current*
   content-hashed bundle URL and custom-element tag by reading the mod's directory anew
   on every request. This is fetched on **every view-entry**, not once — so a rebuilt mod
   is picked up on the next visit without a client redeploy. It **requires an
   authenticated user**; the client sends the caller's bearer token with it.
2. The client dynamically `import()`s the resolved bundle. The bundle's own top-level
   `customElements.define(tag, ...)` call is what registers the element — the host never
   calls `define()` itself and never reaches into the mod's internal Svelte component
   tree. Concurrent view-entries for the same mod share one in-flight load (an in-flight
   `Map<modId, Promise>`, not a check-then-`define()` race).
3. The host `document.createElement(tag)`s the mod's element and assigns `authToken`,
   `apiBase`, and `currentUser` as **instance properties**, set *before* the element is
   inserted into the DOM — never as HTML attributes, which are string-only and would
   serialize the token into the page. The mod's root component reads them through
   Svelte's `$props()`. Context is per-instance: mounting the same mod twice yields
   independent state.
4. Teardown is the standard Web Components lifecycle: navigating away removes the element
   from the DOM, firing its `disconnectedCallback`, and Svelte destroys the inner
   component on the next tick. No bespoke teardown code. Non-Svelte side effects a mod
   opens itself (a raw WebSocket, a `setInterval`, a `ResizeObserver`) are the mod
   author's own responsibility to close in its own teardown — this mechanism guarantees
   the element is removed and the inner component destroyed, nothing more.

### What the asset surface will serve

`GET /static/mods/{id}/{path}` serves a mod's built assets off its own install directory,
with a real traversal guard: the requested path is resolved and confirmed to lie inside
that directory, which refuses `..` escape, absolute paths, and symlinks pointing outside.

That answers "is this file inside the mod's directory". It does **not** answer "should this
file be public", so the surface additionally serves **only web-asset file types** — scripts,
styles, source maps, markup, JSON, images, fonts. Everything else is a 404, checked on the
resolved target rather than the requested name, so a symlink called `logo.svg` pointing at
`secrets.yaml` is refused for what it is.

This matters because installing a mod means dropping a directory onto a volume. Without the
restriction, everything dropped there was world-readable: a mod's Python source, its
`mod.yaml`, its tests, its build inputs. An allowlist rather than a blocklist of known-bad
extensions — a blocklist only protects against the file types somebody thought of, and the
cost of getting it wrong is publishing a credential.

The surface itself is deliberately **unauthenticated**: the client loads a bundle with
dynamic `import()`, which cannot carry an `Authorization` header. Narrowing *what* it serves
is the control; requiring auth is not available to it.

!!! warning "Do not put anything private in a mod's install directory"
    The type restriction is a backstop, not a licence. A mod's directory is served to
    anyone who can reach the instance. Credentials belong in the manifest's `config` block
    with `secret: true`, sourced from the deployment's secret path — never in a file beside
    the bundle.

### Containment

Two mechanisms, for two different error classes, because one is not a superset of the
other:

- **`<svelte:boundary>`** wraps the mount point. It catches errors thrown during
  rendering and while running effects *within it* — which covers a throw from the host's
  own mount action.
- **A scoped `window` `'error'` listener**, registered for exactly the lifetime a mod is
  mounted, catches the class the boundary cannot: per the Custom Elements spec, an
  exception thrown inside a custom element reaction (`connectedCallback` and friends) is
  *reported* to the global scope, not synchronously propagated back through the DOM call
  (`appendChild`) that triggered it — so it never enters the boundary's effect stack at
  all. This was originally believed to route through the boundary; a real CI run of the
  containment tests proved otherwise, which is why it's called out here rather than left
  implicit. See [Known gaps closed post-launch](#known-gaps-closed-post-launch).

Both paths render the same contained "this mod stopped responding" state in the mod's own
view slot, leaving the shell, the nav, and every other mod untouched.

**Named residual risks, accepted rather than solved:**

- A genuinely runaway mod (an infinite loop, not a thrown error) has no in-browser
  containment short of a Worker or an iframe. Accepted as part of the same-origin,
  same-privilege, operator-trusted mod model — see [Security posture](#security-posture).
- A mod's own *async* reactive-effect errors (its internal Svelte runtime is a separate
  bundle with its own scheduler) and event-handler/`setTimeout` throws run outside both
  the boundary's effect frame and a reported custom-element reaction. Neither mechanism
  catches those. The mod author's own responsibility, consistent with the trust model.

### Rejected loading mechanisms

- **An iframe.** Adds an isolation boundary that does not actually isolate anything here
  — tokens ride in a cookie, so a same-origin bundle inherits the user's credentials
  regardless of the frame. It looked like a boundary without being one.
- **Module Federation.** Immature tooling at this scale for a v0.
- **A raw dynamically-imported Svelte component** (rather than a custom element). Hits a
  real, open Svelte bug (sveltejs/svelte#13186) — a cross-build Svelte runtime instance
  mismatch between the host's own Svelte and a separately-bundled component's copy. A
  custom element sidesteps it entirely: the host only ever touches one DOM element and
  never reaches into the mod's internal component tree.

## Security posture

Two things to be explicit about, because the word "sandbox" tends to imply protections that
are not present:

- **A mod is trusted code.** It runs in the API process with the API's privileges. Installing
  a mod is a decision of the same weight as deploying any other service into your stack.
  Scopes bound what *users* may reach through a mod; they do not bound the mod itself.
- **A mod's client surface is as trusted as core's.** It runs as a native route in the
  client, authenticated with the same token, on the same origin. There is no isolation
  boundary between a mod's frontend and core's. Nothing about a mod's frontend should be
  read as sandboxed.

Per-object authorization is separate from scopes. A scope answers "may this user use the
session feature at all"; it does not answer "may this user drive *that* session". Mods reuse
core's existing per-object access-control helpers for the second question rather than
inventing a parallel one.

## Phasing

| Phase | Deliverable | Status |
| --- | --- | --- |
| 0 | Core hooks: discovery, manifest schema, the four registration hooks, scope ingestion, config surface, the stable facade, registry endpoint. No frontend loading. | **Done** |
| 1 | A reference mod: one router, one namespace, one tool, one scope, one table. The executable spec and conformance test. It stays in the tree. | **Done** |
| 2 | Client-side registry consumer and the native mount loader described above — a mod's surface appears in the client's own navigation, not embedded in a frame. | **Done** |
| 3+ | The first real mod, built against the validated contract. | **In progress** — self.crew's crew mod (`self.crew#141`) |

Phase 1 existed specifically to **test the contract with a throwaway** before betting a real
mod on it. A reference mod that exercises all four hooks is worth more than one that only
mounts a route, because the hooks that are hardest to get right — namespace auth and tool
scope enforcement — are exactly the ones a trivial mod would skip. It found real gaps
(see below), which is exactly what it was for.

## Definition of done

Proven end to end, without hand-editing the application module and without rebuilding
either image to install the mod, by the reference mod (Phase 1) and its frontend surface
(Phase 2):

- The mod mounts a router under its prefix.
- It streams events over its namespace to an authenticated client (`emit_to_user`).
- It exposes a tool the model can call, and the call is refused for a user lacking the
  scope.
- An admin grants the scope; a user with the grant sees the mod's surface, a user without
  it does not and cannot reach the mod's API.
- Its table is created and migrated under its own prefix.
- Disabling the mod removes all of it cleanly on the next boot, leaving its data intact.
- Its client UI mounts as a native view in the client's own navigation, fetched fresh on
  each view-entry.
- A crash after mount is contained to the mod's own view slot; the shell and every other
  mod's nav entry are untouched.
- Navigating away is a genuine element removal (a real `disconnectedCallback`), not an
  optimized-away same-tick reattach.

## Known gaps closed post-launch

Real gaps the contract's own definition-of-done did not catch, found by actually building
against it — recorded rather than quietly fixed, because a contract that looks
finished from its own conformance test is not the same claim as a contract that has
survived a second, independent build.

- **The `sys.path` gap** (found reviewing the contract for self.crew's crew mod,
  `self.crew#141`). Discovery finds a mod's `mod.yaml` by reading it directly off disk —
  it never touches `sys.path`. But resolving the entrypoint imports the mod's *code* with
  a bare `import_module(module_name)`, and Python's import system only ever consults
  `sys.path`. Nothing wired a mod's install directory onto it, so a mod dropped via
  `MODS_EXTRA_DIRS` had a discoverable manifest but genuinely unimportable code unless the
  deploy environment happened to also set `PYTHONPATH` — which self.ai's own deploy
  manifests didn't. Fixed in the loader (it appends a mod's directory to `sys.path` once,
  idempotently, before import), not in the deploy contract, so "drop a directory, flip
  `ENABLED_MODS`, no rebuild" stays true with zero per-mod ops coordination.
- **The client surfaces were unauthenticated, and the asset route served everything.**
  `frontend-manifest` answered anonymously — verified with a plain `curl` from outside the
  cluster, naming a live mod's tag and bundle hash — and the asset route served *any* file
  in a mod's install directory, not just its built assets. The traversal guard was correct
  throughout; the defect was scope, and the docs had described the intent
  ("built frontend assets") more narrowly than the code implemented it. Both surfaces were
  invisible to the route auth audit, which could not see `include_router`-mounted routes at
  all — so the audit reported full coverage while never inspecting them, or any mod's
  routes. Fixed together: the manifest requires a verified user, the asset route serves
  web-asset file types only, and the audit now recurses into mounted routers and recognises
  ticket auth.
- **The configuration surface was inert** (found reviewing the harness before the crew
  mod's own build, `self.crew#141`). A manifest's `config` block parsed and validated,
  and the functions that turn it into `PersistentConfig` entries existed and were
  unit-tested — but nothing called them at boot, so a declared key never reached the
  running instance. A mod author following this page got no value at runtime; the crew
  mod's author hit exactly that and fell back to reading raw environment variables, which
  is the "invent your own mechanism" this section forbids. Fixed by attaching each loaded
  mod's config in the boot path, and covered by a real-boot test rather than only a unit
  test — the same failure class the Phase 0 review caught in the discovery path (unit
  proven, assembly unwired), recurring in the one hook no mod had declared. The reference
  mod now declares a `config` block so the path stays covered.
- **The `connectedCallback` containment gap.** `<svelte:boundary>` was believed to catch a
  synchronous throw from a mounted mod's `connectedCallback` — Svelte runs the mount
  action as an effect, and the boundary catches effect errors. It doesn't, for this
  specific case: per the Custom Elements spec, a reaction's exception is *reported*
  globally rather than propagated back through `appendChild`, so it never reaches the
  boundary's effect stack. Found by a real CI test failure, not by review — the
  containment tests genuinely threw and the boundary genuinely didn't catch it. Fixed with
  a second, real containment mechanism (a scoped `window` `'error'` listener) rather than
  by narrowing the claim. See [Containment](#containment).

## Rejected alternatives

- **Extending the Functions model to cover mods.** Functions are user-scoped and
  chat-path-only by design — the opposite of what operator-installed code that mounts
  routes and defines permissions needs. Two tiers split cleanly by trust is the point.
- **TOML manifests.** Rejected in favour of YAML. The surrounding stack's configuration is
  YAML throughout and the same people read both; a second format buys nothing here.
- **Colon-style scope ids** (`example:thing:read`). Rejected in favour of dot-namespacing,
  which reuses core's existing permission traversal instead of requiring a parallel
  resolver. See [Why scopes are dot-separated](#why-scopes-are-dot-separated).
- **Mods mounting their own websocket stacks.** Rejected as the default: it discards auth,
  reconnection, and cross-replica delivery that core's Socket.IO server already provides.
- **Letting mods import freely from core internals.** Rejected. It would freeze the
  Open-WebUI inheritance self.ai is actively separating from.
- **Baking mods into the API image at build time.** An install that requires rebuilding the
  image is a fork with extra steps. Enablement is configuration, not a rebuild.
- **Compiling a mod's frontend into the chat client at build time.** Same reason —
  installing a mod would force a client rebuild.
- **An iframe, Module Federation, or a raw dynamically-imported Svelte component** for
  client-surface loading. See [Rejected loading mechanisms](#rejected-loading-mechanisms).
- **A mod marketplace, remote install, or sandboxing of unsigned mods.** Deferred. The v0
  posture is operator-installed and operator-trusted, the same as every other
  admin-brought component.

## Open questions

Genuinely unresolved. If you are designing against this contract, these are the parts most
likely to move:

1. **Scope federation.** How mod-declared scopes map through external SSO and role mapping.
   The scope table lives in self.ai; the federation layer is separate.
2. **Sidecar lifecycle.** Health probing and degradation are named, but who starts a sidecar
   — the mod, or the deployment — is not decided. Not yet load-bearing: the crew mod, the
   first real mod, carries no sidecar of its own.

## See also

- [Extending self.ai](extending.md) — the extension surface that exists today
- [Architecture](architecture.md) — where a mod would sit in the request path

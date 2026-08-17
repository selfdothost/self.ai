"""Mod tools: registration, per-user assembly, call-site scope enforcement.

A mod contributes model-callable tools via `register_tools()`, which returns
`ModTool` descriptors. This module turns those into exactly the dict shape
`get_tools()` already builds for user-authored toolkits -- `toolkit_id`,
`callable`, `spec`, `pydantic_model`, `file_handler`, `citation` -- so the
model cannot tell a mod tool from a user-authored one. There is no origin
marker in what the model receives, INCLUDING when a name collides: the
qualifier `collide_name` produces is built the same way for a mod tool and a
user-authored one, and never spells out `mod:` or a mod id. `toolkit_id`
itself keeps that prefix -- it is bookkeeping, read by dispatch, logs, and the
registry, never shown to the model -- so the two identities are deliberately
different: one is for operators, the other is for the model.

Three places a scope matters, and they are deliberately distinct:

* **Registration.** Every ModTool must declare a required scope. One that does
  not is rejected and the mod's tools are not registered -- a tool with no gate
  is a capability the operator never agreed to expose.
* **Assembly.** When the tool set for a model call is built, a tool is included
  only for users who hold its scope. A user who lacks it never sees the tool.
* **Call site.** The callable itself re-checks the scope before running. This
  is the authoritative check; assembly-time filtering is a convenience for the
  model, not a security boundary, because a guessed tool name could be invoked
  directly.

Collisions are resolved by a deterministic rule rather than the silent discard
that `get_tools()` historically did. The rule is stated in one place and used
by both paths.

Cavekit: cavekit-mods-surfaces.md R3, R4, R5, R6 -- T-063..T-069, T-072
"""

import inspect
import logging
import re
from dataclasses import dataclass
from typing import Callable

from selfai_ui.utils.access_control import has_permission
from selfai_ui.utils.tools import function_to_pydantic_model
from selfai_ui.utils.toolspec import ToolSpec

log = logging.getLogger(__name__)

#: The character set a tool `name` must satisfy to reach a model at all.
#:
#: Both wire formats core speaks publish this exact pattern for a function/tool
#: name: OpenAI requires `^[a-zA-Z0-9_-]{1,64}$`; Anthropic requires
#: `^[a-zA-Z0-9_-]{1,128}$` -- same charset, OpenAI's bound is the tighter one.
#: A name satisfying this is valid on both. Checked at registration (fail
#: closed, like every other manifest-adjacent validation in this package)
#: rather than left to surface as an opaque provider rejection at call time.
TOOL_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

#: Human-readable statement of the same rule, for error messages.
TOOL_NAME_RULE = (
    "must be 1-64 characters, using only letters, digits, hyphens and "
    "underscores (the charset OpenAI and Anthropic both require of a tool name)"
)


def is_valid_tool_name(name: object) -> bool:
    """True when `name` satisfies the charset every supported provider requires."""
    return isinstance(name, str) and bool(TOOL_NAME_PATTERN.match(name))


#: Characters `collide_name` must not pass through into a model-facing name.
#: Broader than `TOOL_NAME_PATTERN` needs for today's inputs -- mod ids already
#: satisfy it (`mods/naming.py`'s `MOD_ID_PATTERN`) and a user's `toolkit_id` is
#: a Python identifier (`str.isidentifier()`, checked at
#: `routers/tools.py:80`) -- but `isidentifier()` admits non-ASCII letters
#: (e.g. `café` is a valid Python identifier and not a valid tool name), so an
#: owner is sanitized rather than trusted.
_UNSAFE_OWNER_CHARS = re.compile(r"[^a-zA-Z0-9_-]")


@dataclass
class ModTool:
    """One model-callable tool a mod contributes.

    `handler` is the implementation; it receives the model's arguments (the
    `__`-prefixed injected params are consumed by the scope guard and are NOT
    passed through to the handler -- the guard captures the invoking user and
    the permission defaults at assembly, so a handler that needs the caller's
    identity should take it from the mod's own request-scoped context rather
    than a tool argument). `scope` is the `mods.<id>.*` permission required to
    use it. `parameters` is a JSON-schema dict for the function signature, or
    None for a no-argument tool.

    `output_schema` is an optional JSON-schema dict describing what the handler
    returns. It carries straight through to `ToolSpec.output_schema` -- same
    name deliberately, since that is the field a mod author already sees on the
    facade's `ToolSpec` -- and is **inert on the live path**: no provider core
    talks to today defines a result-schema field, so it appears in `to_mcp()`
    and in neither `to_openai()` nor `to_anthropic()`. It exists now so a
    handle-returning tool -- the async `{task_id, status}` pattern numberone
    confirmed on self.crew#141 -- can declare its shape without a later model
    change.
    """

    name: str
    description: str
    handler: Callable
    scope: str
    parameters: dict | None = None
    output_schema: dict | None = None


class ModToolError(Exception):
    """A mod's tools could not be registered. Never escapes the loader."""


def validate_mod_tools(declared: list) -> list[ModTool]:
    """Normalise and validate a mod's declared tools at registration time.

    Raises `ModToolError` if any tool lacks a required scope -- the whole mod's
    tools are then refused, because a tool with no gate is a capability the
    operator never agreed to expose. Coerce dict inputs to ModTool so a mod may
    return plain dicts if it prefers.

    Also raises if `name` does not satisfy `TOOL_NAME_PATTERN`. Nothing enforced
    this before ToolSpec existed -- a mod could declare a tool named `"Look
    something up"` and it would validate today, reach `assemble_mod_tools`
    unexamined, and fail only when a provider rejected it at call time, with an
    error that named neither the mod nor the tool. Checking here instead means
    a bad name is refused at boot, at the same point and with the same posture
    as every other manifest-adjacent check in this package.
    """
    tools: list[ModTool] = []
    for index, raw in enumerate(declared or []):
        tool = raw if isinstance(raw, ModTool) else ModTool(**raw)
        if not tool.scope:
            raise ModToolError(
                f"tool {tool.name!r} (index {index}) declares no required scope; "
                f"a mod tool must gate on a mods.<id>.* scope"
            )
        if not is_valid_tool_name(tool.name):
            raise ModToolError(f"tool {tool.name!r} (index {index}) is not a valid tool name: {TOOL_NAME_RULE}")
        tools.append(tool)
    return tools


def _no_param_schema() -> dict:
    return {"type": "object", "properties": {}}


def _scope_guard(tool: ModTool, *, user, defaults: dict, has_permission_fn=has_permission) -> Callable:
    """Wrap a handler so it refuses callers lacking the tool's scope.

    The identity and permission defaults are CAPTURED at assembly time, not read
    from call-time kwargs. The tool-calling dispatch path invokes a tool as
    `callable(**filtered_args)` with only the model's arguments -- it never
    injects `__user__`/`__request__` the way user-authored toolkits get them
    baked via `apply_extra_params_to_tool_function`. A guard that read those
    from kwargs therefore always saw `None` and denied every call (F-003).
    Capturing them here makes the check authoritative regardless of what the
    dispatch path passes: assembly already knows the user, so the guard is bound
    to that user for this request.

    Still a real call-site check, not just the assembly filter: `has_permission`
    is re-evaluated at invocation against the same captured live defaults, so a
    guessed tool name that reached the callable directly is refused, and a scope
    revoked between assembly and call is honoured.

    A denial is a permissions refusal, not a technical failure: it raises
    `PermissionError`, which the tool-calling path surfaces as a tool error.
    """
    scope = tool.scope
    handler = tool.handler
    user_id = getattr(user, "id", None)

    async def guarded(**kwargs):
        if not has_permission_fn(user_id, scope, defaults):
            raise PermissionError(f"this action requires the '{scope}' permission")

        call_kwargs = {k: v for k, v in kwargs.items() if not k.startswith("__")}
        result = handler(**call_kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    return guarded


def assemble_mod_tools(
    tools: list[ModTool], manifest, *, user, defaults: dict, has_permission_fn=has_permission
) -> dict[str, dict]:
    """Build the per-user tool dict for one mod, in core's shape.

    Takes the validated tool list (the loader stores it on `LoadedMod.tools`)
    rather than re-calling `register_tools()`, so what the model sees is exactly
    what was validated at registration. `defaults` is the live instance
    permission defaults (`USER_PERMISSIONS`) -- the SAME object the call-site
    guard, the registry, and core's own `has_permission` use, so a grant that
    comes from an instance default (not a group) is seen consistently
    everywhere (F-005; the old `{}` here hid such tools from the model while the
    registry reported them held). Only tools whose scope the user holds are
    included; the rest are absent. The returned dicts are structurally identical
    to those `get_tools()` produces, with `toolkit_id` set to `mod:<id>`.
    """
    assembled: dict[str, dict] = {}
    for tool in tools:
        if not has_permission_fn(getattr(user, "id", None), tool.scope, defaults):
            continue

        spec = ToolSpec(
            name=tool.name,
            description=tool.description,
            input_schema=tool.parameters or _no_param_schema(),
            output_schema=tool.output_schema,
        )
        callable_ = _scope_guard(tool, user=user, defaults=defaults, has_permission_fn=has_permission_fn)
        assembled[tool.name] = {
            "toolkit_id": f"mod:{manifest.id}",
            "callable": callable_,
            "spec": spec,
            "pydantic_model": function_to_pydantic_model(callable_),
            "file_handler": False,
            "citation": False,
        }
    return assembled


def assemble_for_user(
    load_result, user, *, defaults: dict, has_permission_fn=has_permission
) -> list[tuple[str, dict]]:
    """Every mod tool the calling user may invoke, as (name, tool) pairs.

    Returns pairs rather than a dict so cross-mod collisions are visible to the
    caller's `resolve_collisions` pass. With no mods loaded this is empty -- so
    merging it into the chat path is a no-op for any instance that has not
    enabled a mod. `defaults` is threaded through to the per-mod assembly and
    the call-site guard so every scope decision uses the live instance
    permission defaults.
    """
    pairs: list[tuple[str, dict]] = []
    if load_result is None:
        return pairs
    for mod in load_result.loaded.values():
        if not getattr(mod, "tools", None):
            continue
        for name, tool in assemble_mod_tools(
            mod.tools, mod.manifest, user=user, defaults=defaults, has_permission_fn=has_permission_fn
        ).items():
            pairs.append((name, tool))
    return pairs


def collide_name(owner: str, name: str) -> str:
    """The deterministic name given to a tool that collides with another.

    Both colliding tools keep working: each is qualified with its owner, so the
    model can still call either. This replaces the silent discard at
    `utils/tools.py` that logged a warning and dropped the entry.

    `owner` is `toolkit_id`: a plain identifier for a user-authored toolkit, or
    `mod:<id>` for a mod tool. Two things happen to it before it reaches the
    model, and both are deliberate rather than a pass-through of whatever
    `toolkit_id` happens to be:

    1. **The `mod:` scheme prefix is stripped.** Left in, it becomes a literal,
       unconditional watermark: every mod tool that ever collides would carry a
       hardcoded `mod:` tag, contradicting the "no origin marker" contract this
       module states in its own docstring.
       Stripping it makes a mod's qualifier the same *shape* as a user
       toolkit's -- a bare identifier -- so the two are structurally
       indistinguishable even under collision, which is the actual property
       "no origin marker" is meant to guarantee. `toolkit_id` itself is
       untouched by this -- the prefix stays for dispatch, logs, and the
       registry, all of which are operator-facing, not model-facing.
    2. **The result is sanitized to `TOOL_NAME_PATTERN`'s charset.** A mod id
       already satisfies it (`mods/naming.py`'s `MOD_ID_PATTERN`), but a user's
       `toolkit_id` is only checked to be a Python identifier
       (`routers/tools.py:80`), and `str.isidentifier()` admits non-ASCII
       letters that OpenAI's and Anthropic's tool-name charset does not. Before
       this, `owner` was used unsanitized, so a mod collision was invalid on
       both providers -- not a leak, a hard failure at the wire.

    Stripping `mod:` narrows the space of distinct owners: a mod id and an
    identically-spelled user toolkit id (both legal, independently chosen)
    would now qualify to the same string. `resolve_collisions` does not detect
    a collision between two *qualified* names -- the second silently overwrites
    the first in `resolved[key]`. Accepted as a narrow, pre-existing class of
    risk (an unqualified collision was already only detected, not prevented,
    by identity) rather than solved here; a mod author who names a mod the same
    as an existing user toolkit id is already an unlikely coincidence, and nothing
    in the collision path was defended against qualifier-level collisions before
    this change either.
    """
    qualifier = owner.removeprefix("mod:") if owner.startswith("mod:") else owner
    safe_qualifier = _UNSAFE_OWNER_CHARS.sub("_", qualifier)
    return f"{safe_qualifier}__{name}"


def resolve_collisions(pairs: list[tuple[str, dict]]) -> dict[str, dict]:
    """Apply the collision rule to a list of (name, tool) pairs.

    A name used by exactly one tool is kept bare. A name shared by more than one
    is qualified with each owner via `collide_name`, so EVERY holder of a shared
    name is renamed -- not just the second one. That makes the resulting set of
    names independent of order: the same inputs in any order yield the same keys.
    No tool is ever dropped.

    Used by `get_tools` (user-authored) and the middleware merge (user + mod).

    When a name is qualified, the tool's spec name MUST be rewritten to match the
    new key. The model is offered the spec's name, and dispatch looks the returned
    call up by the dict key (`if name in admin_tools`): if the two diverge, the
    model calls a name that is not a key and the call fails with "Unknown tool"
    (F-001). The rewrite is done on a copy, because the spec derives from the
    shared, cached `tools.specs` object off the DB model -- mutating it in place
    would corrupt it for every subsequent request.

    That copy is a SHALLOW copy of the tool dict, and deliberately so.
    `_spec_renamed_to` already returns a fresh spec, so the only thing needing
    protection is protected. Deep-copying the whole tool dict would also copy
    `callable`, and for an async tool that is actively harmful:
    `apply_extra_params_to_tool_function` (`utils/tools.py:18`) returns a bare
    `functools.partial` for a coroutine function, whose keywords hold the live
    `__request__`, `__event_emitter__`, `__model__`, and `__messages__`.
    `deepcopy` of a partial copies its keywords, so it would try to deep-copy a
    FastAPI `Request` -- either raising or handing the tool a detached request
    that no longer refers to the live one. (The sync path escaped it only
    because `deepcopy` of a plain function returns the same object.)

    Every collision is logged, naming the shared tool name and every owner
    holding it -- `cavekit-mods-surfaces.md` R5's "a collision produces a
    logged record" criterion, previously unimplemented: qualification ran
    silently and an operator had no way to learn a collision had happened at
    all. Also logged, as its own louder case: two owners whose *qualified*
    names collide with each other (possible since `collide_name` sanitizes --
    see its docstring), which silently drops the earlier tool rather than
    merely renaming it.
    """
    from collections import Counter

    counts = Counter(name for name, _ in pairs)
    colliding_names = {name for name, count in counts.items() if count > 1}
    if colliding_names:
        owners_by_name: dict[str, list[str]] = {name: [] for name in colliding_names}
        for name, tool in pairs:
            if name in colliding_names:
                owners_by_name[name].append(tool.get("toolkit_id", "?"))
        for name, owners in owners_by_name.items():
            log.warning(
                "tool name %r is held by %d owners (%s); each is qualified by owner",
                name,
                len(owners),
                ", ".join(owners),
            )

    resolved: dict[str, dict] = {}
    for name, tool in pairs:
        if counts[name] == 1:
            resolved[name] = tool
            continue
        owner = tool.get("toolkit_id", "?")
        key = collide_name(owner, name)
        if key in resolved:
            log.warning(
                "qualified name %r is itself shared by more than one owner after "
                "collision handling; the tool registered under it earlier is no "
                "longer reachable under this name",
                key,
            )
        renamed = dict(tool)
        if "spec" in renamed:
            renamed["spec"] = _spec_renamed_to(renamed["spec"], key)
        resolved[key] = renamed
    return resolved


def yield_names_to(tools: dict[str, dict], reserved: set[str]) -> dict[str, dict]:
    """Qualify every tool in `tools` whose name is already claimed by `reserved`.

    One-sided, and that is the whole point of it being separate from
    `resolve_collisions`. That function qualifies EVERY holder of a shared name,
    which is right when both holders are ours to rename. A caller-supplied tool
    is not ours: the client dispatches the `tool_calls` we return against its own
    registry, by name, so rewriting a client tool's name breaks the client with
    no way for it to notice (self.ai#71). So `reserved` keeps its spelling bare
    and our side yields, qualified by its owner through the same `collide_name`
    a two-sided collision would use. Nothing is dropped, and a request carrying
    no client tools passes through untouched.

    Shallow-copies the tool dict for the same reason `resolve_collisions` does --
    `callable` may be a `functools.partial` holding the live request.
    """
    if not reserved:
        return tools

    qualified: dict[str, dict] = {}
    for name, tool in tools.items():
        if name not in reserved:
            qualified[name] = tool
            continue
        key = collide_name(tool.get("toolkit_id", "?"), name)
        log.warning(
            "tool name %r is claimed by a client-supplied tool on this request; "
            "the server-side tool from %r is offered as %r instead",
            name,
            tool.get("toolkit_id", "?"),
            key,
        )
        renamed = dict(tool)
        if "spec" in renamed:
            renamed["spec"] = _spec_renamed_to(renamed["spec"], key)
        qualified[key] = renamed
    return qualified


def _spec_renamed_to(spec, key: str):
    """Return `spec` with its name set to `key`, whatever shape it arrives in.

    Both live producers -- `get_tools()` and `assemble_mod_tools()` -- build a
    `ToolSpec`, so the typed branch is the production path. The dict branch is
    kept because the outer six-key tool dict is an untyped, third-party-visible
    contract (`functions.py` hands it whole to plugin functions as `__tools__`),
    so a hand-built or legacy dict spec remains representable here.

    The unrecognized case logs rather than passing silently. A skipped rename is
    exactly F-001: the model is offered a name that is not a dispatch key and the
    call fails with "Unknown tool" -- a silent, production-only failure. That is
    what the old `isinstance(..., dict)` guard would have become the moment specs
    went typed, and it must never be reachable without a trace.
    """
    if isinstance(spec, ToolSpec):
        return spec.model_copy(deep=True, update={"name": key})
    if isinstance(spec, dict):
        return {**spec, "name": key}
    if spec is not None:
        log.warning(
            "cannot rename a %s spec for collision-qualified tool %r; "
            "the model will be offered a name that is not a dispatch key",
            type(spec).__name__,
            key,
        )
    return spec

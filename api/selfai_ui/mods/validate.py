"""Semantic validation and fail-closed loading of `mod.yaml`.

`manifest.py` answers "is this the right shape". This answers the questions that
need context the file does not contain: is the `id` safe, do the declared scopes
sit under this mod's own namespace, does the table prefix match what the id
derives, and does the running core satisfy `min_core_version`.

Fail-closed throughout. Every rejection names the file and the specific rule
that failed, because the alternative -- a mod that silently does not appear --
is the worst possible failure mode for an operator who just installed one.

Cavekit: cavekit-mods-manifest.md R2, R3, R4, R5, R6; cavekit-mods-loader.md R8
Tasks: T-009 .. T-013, T-019
"""

import logging

import yaml
from pydantic import ValidationError

from selfai_ui.mods import naming
from selfai_ui.mods.manifest import ManifestError, ModManifest

log = logging.getLogger(__name__)


def parse_version(raw: object) -> tuple[int, ...]:
    """Parse a dotted numeric version into a comparable tuple.

    Deliberately strict and dependency-free: a version is dotted integers and
    nothing else. Anything we cannot compare with confidence is a refusal
    rather than a guess, because the consequence of guessing wrong is loading a
    mod against a core that does not satisfy it.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ManifestError("version must be a non-empty string")
    parts = raw.strip().split(".")
    try:
        return tuple(int(part) for part in parts)
    except ValueError as exc:
        raise ManifestError(f"version {raw!r} is not a dotted numeric version") from exc


def _pad(a: tuple[int, ...], b: tuple[int, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Zero-pad the shorter tuple so 1.2 and 1.2.0 compare equal."""
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)), b + (0,) * (width - len(b))


def core_satisfies(min_core_version: str, core_version: str) -> bool:
    """True when `core_version` is at least `min_core_version`."""
    required, running = _pad(parse_version(min_core_version), parse_version(core_version))
    return running >= required


def validate_semantics(manifest: ModManifest, *, core_version: str) -> None:
    """Apply every rule that needs context beyond the manifest's own shape.

    Raises `ManifestError` on the first failure, naming the rule.
    """
    # R2 -- the id roots both derived namespaces, so it is checked first.
    if not naming.is_valid_mod_id(manifest.id):
        raise ManifestError(f"id {manifest.id!r} {naming.MOD_ID_RULE}", field="id")

    # R4 -- refused here, before anything resolves an entrypoint. A mod that
    # cannot run against this core must not get as far as importing its code.
    if not core_satisfies(manifest.min_core_version, core_version):
        raise ManifestError(
            f"mod {manifest.id!r} requires core >= {manifest.min_core_version}, "
            f"running core is {core_version}",
            field="min_core_version",
        )

    # R5 -- scopes rooted at this mod's own namespace. This is what makes
    # collision with a core permission key, or another mod's scopes, impossible
    # rather than merely discouraged.
    seen: set[str] = set()
    for scope in manifest.scopes:
        if not naming.is_scope_in_namespace(scope.id, manifest.id):
            raise ManifestError(
                f"scope {scope.id!r} must sit beneath {naming.scope_root_for(manifest.id)}.",
                field="scopes",
            )
        if scope.id in seen:
            raise ManifestError(f"duplicate scope {scope.id!r}", field="scopes")
        seen.add(scope.id)

    # R6 -- the table prefix is derived, never freely chosen.
    if manifest.db is not None:
        expected = naming.table_prefix_for(manifest.id)
        if manifest.db.table_prefix != expected:
            raise ManifestError(
                f"table_prefix {manifest.db.table_prefix!r} must be {expected!r}",
                field="db.table_prefix",
            )

    # Frontend-API R1 -- the frontend nav surface is gated by ordinary scopes
    # from this mod's own namespace, checked with the SAME helper the mod's
    # declared scopes use. No new gating mechanism: a foreign root or a core
    # permission key is refused exactly as it is for a declared scope.
    if manifest.frontend is not None:
        frontend = manifest.frontend
        if not frontend.scopes:
            raise ManifestError(
                "frontend block must name at least one gating scope",
                field="frontend.scopes",
            )
        for scope_id in frontend.scopes:
            if not naming.is_scope_in_namespace(scope_id, manifest.id):
                raise ManifestError(
                    f"frontend gating scope {scope_id!r} must sit beneath {naming.scope_root_for(manifest.id)}.",
                    field="frontend.scopes",
                )
        # A declared tag must be a real custom-element name; an omitted one
        # derives from the id and is valid by construction.
        if frontend.tag is not None and not naming.is_valid_custom_element_tag(frontend.tag):
            raise ManifestError(
                f"frontend tag {frontend.tag!r} is not a valid custom-element name "
                f"(lowercase, opening with a letter, and containing a hyphen)",
                field="frontend.tag",
            )

    # Loader R8 -- a key marked secret must not carry its value in the manifest.
    # The marker exists precisely so this check is expressible.
    for entry in manifest.config:
        if entry.secret and entry.default is not None:
            raise ManifestError(
                f"config key {entry.key!r} is marked secret and must not carry an inline value",
                field="config",
            )


def load_manifest(path, *, core_version: str) -> ModManifest:
    """Read, parse, and fully validate one `mod.yaml`.

    Every failure mode -- unreadable, unparseable, wrong shape, wrong semantics
    -- raises `ManifestError` naming the path. Nothing escapes as a raw YAML or
    pydantic error, so a caller can treat "this mod did not load" uniformly and
    still report something an operator can act on.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(f"cannot be read ({exc.strerror})", path=path) from exc

    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ManifestError(f"is not valid YAML ({exc.__class__.__name__})", path=path) from exc

    if data is None:
        raise ManifestError("is empty", path=path)
    if not isinstance(data, dict):
        raise ManifestError(f"must be a mapping, got {type(data).__name__}", path=path)

    # YAML 1.1 resolves bare `on`, `off`, `yes`, `no`, `y`, `n`, `true` and
    # `false` to booleans. A mod legitimately named `on` therefore arrives here
    # with id=True and fails as "Input should be a valid string", which tells
    # its author nothing about the real cause. naming.py's charset permits
    # these names, so the collision is real rather than theoretical.
    for field in ("id", "name", "version", "entrypoint", "min_core_version"):
        if isinstance(data.get(field), bool):
            literal = str(data[field]).lower()
            raise ManifestError(
                f'YAML read this as a boolean, not text -- quote it as "{literal}". '
                f"Bare on/off/yes/no/true/false are booleans in YAML",
                field=field,
                path=path,
            )

    try:
        manifest = ModManifest(**data)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first.get("loc", ())) or "?"
        raise ManifestError(first.get("msg", "invalid"), field=field, path=path) from exc

    try:
        validate_semantics(manifest, core_version=core_version)
    except ManifestError as exc:
        raise ManifestError(exc.raw, field=exc.field, path=path) from exc

    return manifest

"""The `mod.yaml` schema.

Structural validation only: what fields exist, what shape they have, and that
nothing unrecognised slipped in. The semantic checks that need context beyond
the file -- whether `min_core_version` is satisfied by the running core, whether
declared scopes sit under this mod's namespace -- live with the requirements
that own them and are applied on top of a parsed manifest.

Validation is fail-closed throughout: a manifest that does not parse cleanly
yields no mod, and the error names the offending field rather than a position
in a byte stream.

Cavekit: cavekit-mods-manifest.md R1 -- T-004
"""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from selfai_ui.mods import naming


class ManifestError(ValueError):
    """A manifest could not be accepted.

    Renders as `<path>: <field>: <message>`, omitting whichever parts are not
    known. Path leads because an operator reading a boot log is looking for
    *which mod* failed before they care which field did; `field` and `path`
    stay available as attributes for callers that want them structured.
    """

    def __init__(self, message: str, *, field: str | None = None, path: object = None):
        self.field = field
        self.path = path
        #: The message without any path/field prefix, so an error raised without
        #: a path can be re-raised with one without doubling the prefix.
        self.raw = message
        prefix = "".join(f"{part}: " for part in (path, field) if part is not None)
        super().__init__(f"{prefix}{message}")


class _Strict(BaseModel):
    """Base for every manifest block: unknown keys are an error, not a shrug.

    Fail-closed means a typo'd key is a refusal rather than a silently ignored
    setting. `mod.yaml` is small and hand-written; a mod author who writes
    `prefx:` should be told, not left wondering why their routes never mounted.
    """

    model_config = ConfigDict(extra="forbid")


class ApiBlock(_Strict):
    """Where this mod's routers mount."""

    prefix: str


class WsBlock(_Strict):
    """The Socket.IO namespace this mod registers on core's existing server.

    A namespace, not a websocket stack of its own -- see
    cavekit-mods-surfaces.md R1.
    """

    namespace: str


class FrontendBlock(_Strict):
    """How this mod declares a client UI surface, and where its bundle loads from.

    Grown from a bare loading address (Phase 0) into a declarative
    nav-registration shape informed by Grafana's `includes[]` (research brief
    §2, §5): a `view` id/path, a nav `label`, an `icon`, and an `add_to_nav`
    flag. Visibility is gated by `scopes` -- one or more ordinary scopes from
    this mod's own `mods.<id>` namespace, checked in `validate.py` against
    `naming.is_scope_in_namespace()`. This invents **no new gating mechanism**;
    the gating scope is a scope the mod already declares.

    The custom-element `tag` this mod's bundle registers is optional: when it is
    omitted the tag derives from the mod `id` via `naming.custom_element_tag_for()`,
    the single source both this validation and any later tag check read from
    (`ModManifest.custom_element_tag`).

    `bundle_url` is retained unchanged from Phase 0 (`cavekit-mods-registry.md`
    R4) -- a loading address, reported by the registry as before. It is not an
    isolation boundary.
    """

    bundle_url: str
    #: The view id/path this mod's nav entry resolves through (the client routes
    #: every mod view through one generic, id-parameterised route).
    view: str
    #: The human-readable nav label.
    label: str
    #: The nav icon name.
    icon: str
    #: Whether this view is added to the primary nav.
    add_to_nav: bool
    #: One or more gating scopes, each rooted at this mod's own `mods.<id>`
    #: namespace. Structural shape only here; the namespace rule is enforced in
    #: `validate.py` because it needs the mod id.
    scopes: list[str]
    #: The custom-element tag to register. Optional -- derived from the mod id
    #: when omitted. When declared, validated as a real custom-element name.
    tag: str | None = None


class DbBlock(_Strict):
    """The prefix every table this mod owns carries.

    Checked against the derivation in `naming.table_prefix_for()`.
    """

    table_prefix: str


class ScopeEntry(_Strict):
    """A permission scope this mod introduces.

    `id` must sit under `mods.<mod id>.` -- enforced against
    `naming.is_scope_in_namespace()`, not here, because it needs the mod id.
    """

    id: str
    desc: str


class ConfigEntry(_Strict):
    """One operator-settable configuration key this mod needs.

    `secret` marks a key whose value must come from the deployment's secret
    path. A secret-marked entry carrying an inline value is a refusal -- the
    marker is what makes that check expressible at all.
    """

    key: str
    desc: str | None = None
    secret: bool = False
    default: object | None = None


class ModManifest(_Strict):
    """A parsed, structurally valid `mod.yaml`."""

    id: str
    name: str
    version: str
    entrypoint: str
    min_core_version: str

    api: ApiBlock | None = None
    ws: WsBlock | None = None
    frontend: FrontendBlock | None = None
    db: DbBlock | None = None
    scopes: list[ScopeEntry] = Field(default_factory=list)
    config: list[ConfigEntry] = Field(default_factory=list)

    #: The absolute on-disk directory this manifest was discovered in. This is
    #: NOT a `mod.yaml` field -- it is a `PrivateAttr`, invisible to the schema,
    #: so a mod cannot declare its own directory (a `directory:` key in the file
    #: is an unknown key and refused). Discovery sets it after a successful load
    #: (`discovery.discover`); the per-mod asset and manifest surfaces (R3, R4)
    #: serve this mod's built assets off it. `None` until discovery sets it --
    #: e.g. a manifest built directly in a unit test.
    _directory: Path | None = PrivateAttr(default=None)

    @property
    def directory(self) -> Path | None:
        """The absolute on-disk directory this mod was loaded from, or `None`."""
        return self._directory

    def custom_element_tag(self) -> str | None:
        """The custom-element tag this mod's bundle registers, or `None`.

        The single resolution point: the tag the `frontend` block declares, else
        the one derived from the mod `id` by `naming.custom_element_tag_for()`.
        Returns `None` when the mod declares no `frontend` surface. Every
        tag-related check reads the derivation rule from here, never restates it.
        """
        if self.frontend is None:
            return None
        return self.frontend.tag or naming.custom_element_tag_for(self.id)

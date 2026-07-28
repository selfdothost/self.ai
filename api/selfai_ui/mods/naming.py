"""Naming rules for mods -- the single source for both derived namespaces.

A mod's `id` roots two different namespaces:

  * its permission scopes, which live under `mods.<id>.*` and are evaluated by
    `has_permission()` (`selfai_ui/utils/access_control.py`), a checker that
    splits keys on `.`
  * its database tables, which all carry a derived prefix

A character that is legal in one of those and not the other -- a dot most
obviously, which would silently graft the mod's scopes onto a different branch
of the permission tree -- is a validation failure. Both checks therefore read
their rules from here rather than restating them, so the two can never drift.

Cavekit: cavekit-mods-manifest.md R2, R6 -- T-003
"""

import re

#: The permitted character set for a mod `id`, stated once.
#:
#: Lowercase alphanumerics, hyphen and underscore, and it must open with a
#: letter. Deliberately excludes:
#:   `.` -- would traverse the scope namespace (`mods.a.b` is two levels)
#:   `/` `\` -- path separators, would traverse the install directory
#:   whitespace -- ambiguous in every serialized form we use
#:   uppercase -- table prefixes fold case in some backends; two ids differing
#:                only in case would collide at the database and not at the
#:                scope tree
MOD_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")

#: Human-readable statement of the same rule, for error messages and docs.
MOD_ID_RULE = (
    "must start with a lowercase letter and contain only lowercase letters, "
    "digits, hyphens and underscores"
)

#: Root of every mod's permission-scope namespace.
MOD_SCOPE_ROOT = "mods"

#: Prefix applied to every table a mod owns.
_TABLE_PREFIX_TEMPLATE = "mod_{id}_"

#: Prefix applied to every mod's custom-element tag, so a mod can never register
#: a bare tag that collides with a platform element.
_CUSTOM_ELEMENT_PREFIX = "mod-"

#: A usable custom-element name: lowercase, opening with a letter, and carrying
#: at least one hyphen. This is the minimal subset of the HTML custom-element
#: rule this project relies on -- a valid custom element name must contain a
#: hyphen, which the `mod-` prefix guarantees for every derived tag.
_CUSTOM_ELEMENT_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)+$")


def is_valid_mod_id(mod_id: object) -> bool:
    """True when `mod_id` satisfies the declared character set."""
    return isinstance(mod_id, str) and bool(MOD_ID_PATTERN.match(mod_id))


def scope_root_for(mod_id: str) -> str:
    """The dotted prefix every scope this mod declares must sit under.

    A scope outside this prefix is a validation failure -- that is what makes
    collision with a core permission key, or with another mod's scopes,
    structurally impossible rather than merely discouraged.
    """
    return f"{MOD_SCOPE_ROOT}.{mod_id}"


def is_scope_in_namespace(scope_id: object, mod_id: str) -> bool:
    """True when `scope_id` sits strictly beneath this mod's scope root.

    The root alone (`mods.<id>`) is not a usable scope -- it names the branch,
    not a permission -- so it is rejected along with anything outside it.
    """
    if not isinstance(scope_id, str):
        return False
    return scope_id.startswith(f"{scope_root_for(mod_id)}.")


def table_prefix_for(mod_id: str) -> str:
    """The table prefix this mod's `id` derives.

    The derivation is stated only here. `db.table_prefix` in a manifest is
    checked against this rather than against a restated pattern.
    """
    return _TABLE_PREFIX_TEMPLATE.format(id=mod_id)


def custom_element_tag_for(mod_id: str) -> str:
    """The custom-element tag a mod's frontend bundle registers, from its `id`.

    Stated **only here** -- both the manifest validation and any later
    tag-related check resolve to this when a `frontend` block does not declare
    an explicit tag (`ModManifest.custom_element_tag`). Underscores fold to
    hyphens because a custom-element name may not contain an underscore while a
    mod `id` may; the `mod-` prefix guarantees the required hyphen.
    """
    return f"{_CUSTOM_ELEMENT_PREFIX}{mod_id.replace('_', '-')}"


def is_valid_custom_element_tag(tag: object) -> bool:
    """True when `tag` is a usable custom-element name.

    A `frontend` block may declare its own tag rather than take the derived one;
    when it does, it must satisfy this -- lowercase, opening with a letter, and
    containing a hyphen -- or the mod is refused.
    """
    return isinstance(tag, str) and bool(_CUSTOM_ELEMENT_PATTERN.match(tag))

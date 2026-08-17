import json
import logging
from typing import Any, Dict, List, Optional

from selfai_ui.models.groups import Groups
from selfai_ui.models.users import UserModel, Users

log = logging.getLogger(__name__)

# Phase 0 of the Tokenization Studio programme renamed the `workspace` permission
# group to `studio`. An Alembic revision rekeys every stored group blob, but a
# group written before that migration -- or restored from an older backup -- still
# says `workspace`, and has_permission denies on a missing level of the hierarchy.
# Without this fallback every such group loses all Studio access at once, with no
# exception and no log line: the navigation simply stops rendering.
#
# TWO functions consume this, and both are needed: `has_permission` is the
# server-side gate, and `get_permissions` builds the object the CLIENT reads to
# decide what to render. Fixing only the first leaves the API permitting calls
# the UI never offers a way to make.
#
# REMOVAL: delete this constant, _resolve_permission's fallback branch, AND
# get_permissions' fold_renamed_group once
# the debug line below has stopped appearing in production for a full release
# cycle -- target is the release AFTER the one carrying the rename, i.e. the
# first release in which no unmigrated group has been observed. Do not remove it
# in the same release as the rename; the migration and this fallback are what
# make the rename safe to land in either order.
# Decision record: selfai/gitlab-profile
# context/treasuremaps/2026-08-11-tokenization-studio.md, Decision 1.
_RENAMED_PERMISSION_GROUP = ("studio", "workspace")


def get_permissions(
    user_id: str,
    default_permissions: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Get all permissions for a user by combining the permissions of all groups the user is a member of.
    If a permission is defined in multiple groups, the most permissive value is used (True > False).
    Permissions are nested in a dict with the permission key as the key and a boolean as the value.
    """

    def combine_permissions(permissions: Dict[str, Any], group_permissions: Dict[str, Any]) -> Dict[str, Any]:
        """Combine permissions from multiple groups by taking the most permissive value."""
        for key, value in group_permissions.items():
            if isinstance(value, dict):
                if key not in permissions:
                    permissions[key] = {}
                permissions[key] = combine_permissions(permissions[key], value)
            else:
                if key not in permissions:
                    permissions[key] = value
                else:
                    permissions[key] = permissions[key] or value
        return permissions

    def fold_renamed_group(group_permissions: Dict[str, Any], group_id: Optional[str] = None) -> Dict[str, Any]:
        """Move a pre-rename `workspace` block onto `studio` before combining.

        `has_permission` gets its own fallback (`_resolve_permission` below).
        This function is the SECOND traversal of the same blobs and needs its
        own, because it is the one the CLIENT reads: `routers/auths.py` calls
        `get_permissions()` on signin, signup and session to build the
        `permissions` object self.chat stores as `$user.permissions` and gates
        every Studio navigation entry on.

        Without this, the two halves disagree for a group the migration never
        reached. `combine_permissions` merges the group's blob OVER the
        defaults, and the defaults now carry a full `studio` block of `False`,
        so both keys survive side by side and the all-`False` one is the one
        with the name the client looks up:

            {"studio":    {"models": False, ...},   <- from defaults, wins
             "workspace": {"models": True, ...}}    <- the real grant, ignored

        The API would then permit a call that the UI never renders a way to
        make -- the same silent lockout the rename guards against, moved to a
        worse place to find it.

        Same rule as the Alembic revision and `_resolve_permission`: `studio`
        wins when both are present, and the stale key is dropped from the
        result so the client is never handed both.
        """
        new_name, old_name = _RENAMED_PERMISSION_GROUP
        if not isinstance(group_permissions, dict) or old_name not in group_permissions:
            return group_permissions

        folded = {key: value for key, value in group_permissions.items() if key != old_name}
        if new_name not in folded:
            folded[new_name] = group_permissions[old_name]
            log.debug(
                "group %s carries the pre-rename '%s' permission key; folding it onto '%s' "
                "for this response. The group predates the Studio rekey migration.",
                group_id if group_id is not None else "<unknown>",
                old_name,
                new_name,
            )
        return folded

    user_groups = Groups.get_groups_by_member_id(user_id)

    # deep copy default permissions to avoid modifying the original dict
    permissions = json.loads(json.dumps(default_permissions))

    for group in user_groups:
        group_permissions = fold_renamed_group(group.permissions, getattr(group, "id", None))
        permissions = combine_permissions(permissions, group_permissions)

    return permissions


def has_permission(
    user_id: str,
    permission_key: str,
    default_permissions: Dict[str, bool] = {},
) -> bool:
    """
    Check if a user has a specific permission by checking the group permissions
    and falls back to default permissions if not found in any group.

    Permission keys can be hierarchical and separated by dots ('.').
    """

    def get_permission(permissions: Dict[str, bool], keys: List[str]) -> bool:
        """Traverse permissions dict using a list of keys (from dot-split permission_key)."""
        for key in keys:
            if not isinstance(permissions, dict) or key not in permissions:
                return False  # If any part of the hierarchy is missing, deny access
            permissions = permissions[key]  # Go one level deeper

        return bool(permissions)  # Return the boolean at the final level

    def _resolve_permission(
        permissions: Dict[str, Any],
        keys: List[str],
        group_id: Optional[str] = None,
    ) -> bool:
        """
        Traverse `permissions`, retrying once under the pre-rename group name.

        The fallback is scoped to the FIRST segment and to the single literal
        `studio` -> `workspace`; `chat.*`, `features.*` and `mods.*` are
        untouched, and no key outside the renamed group can be turned from a
        deny into an allow by it.

        It fires on a MISSING key, never on a falsy one. A blob that has
        `studio` uses it and never consults `workspace`, including when the
        value there is explicitly False -- otherwise a permission an admin had
        deliberately turned off would be resurrected by the stale half of a
        half-migrated blob.
        """
        new_name, old_name = _RENAMED_PERMISSION_GROUP
        if keys and keys[0] == new_name and isinstance(permissions, dict) and new_name not in permissions:
            if old_name in permissions:
                log.debug(
                    "permission %s resolved via the pre-rename '%s' key on group %s; "
                    "this group predates the Studio rekey migration",
                    ".".join(keys),
                    old_name,
                    group_id if group_id is not None else "<default blob>",
                )
                return get_permission(permissions, [old_name, *keys[1:]])
        return get_permission(permissions, keys)

    permission_hierarchy = permission_key.split(".")

    # Retrieve user group permissions
    user_groups = Groups.get_groups_by_member_id(user_id)

    for group in user_groups:
        group_permissions = group.permissions
        if _resolve_permission(group_permissions, permission_hierarchy, group.id):
            return True

    # Check default permissions afterwards if the group permissions don't allow it
    return _resolve_permission(default_permissions, permission_hierarchy)


def has_access(
    user_id: str,
    type: str = "write",
    access_control: Optional[dict] = None,
) -> bool:
    if access_control is None:
        return type == "read"

    user_groups = Groups.get_groups_by_member_id(user_id)
    user_group_ids = [group.id for group in user_groups]
    permission_access = access_control.get(type, {})
    permitted_group_ids = permission_access.get("group_ids", [])
    permitted_user_ids = permission_access.get("user_ids", [])

    return user_id in permitted_user_ids or any(group_id in permitted_group_ids for group_id in user_group_ids)


# Get all users with access to a resource
def get_users_with_access(type: str = "write", access_control: Optional[dict] = None) -> List[UserModel]:
    if access_control is None:
        return Users.get_users()

    permission_access = access_control.get(type, {})
    permitted_group_ids = permission_access.get("group_ids", [])
    permitted_user_ids = permission_access.get("user_ids", [])

    user_ids_with_access = set(permitted_user_ids)

    for group_id in permitted_group_ids:
        group_user_ids = Groups.get_group_user_ids_by_id(group_id)
        if group_user_ids:
            user_ids_with_access.update(group_user_ids)

    return Users.get_users_by_user_ids(list(user_ids_with_access))

"""Permissions closeout: seeding, lossless round trip, survival of disablement.

These are the properties a mod author has to be able to rely on. If a grant
does not survive an admin pressing Save, or vanishes when a mod is briefly
turned off, then `mods.<id>.*` is not a real permission namespace and nothing
built on it can be trusted.

Everything here goes through the same `has_permission` core uses for
`workspace.models`. No mods-specific evaluation path exists, and T-049 exists
to have a human confirm that by reading the diff — no behavioural test can
distinguish a checker that consults load state from one that does not, given
the grant is retained either way.

Cavekit: cavekit-mods-permissions.md R2, R3, R5 — T-046, T-047, T-048
"""

import pytest

from selfai_ui.models.groups import GroupForm, Groups, GroupUpdateForm
from selfai_ui.mods.manifest import ModManifest
from selfai_ui.mods.scopes import seed_defaults, strip_mod_scopes
from selfai_ui.utils.access_control import has_permission

BASE = {"name": "Example", "version": "0.1.0", "entrypoint": "m:Mod", "min_core_version": "0.5.0"}

CREW_SCOPES = [
    {"id": "mods.crew.session.connect", "desc": "attach"},
    {"id": "mods.crew.session.drive", "desc": "drive"},
]


def _manifest(mod_id="crew", scopes=None):
    data = {**BASE, "id": mod_id}
    if scopes is not None:
        data["scopes"] = scopes
    return ModManifest(**data)


def _group_granting(db_session, owner_id, member_id, permissions, name="grantees"):
    """A group carrying `permissions`, with `member_id` in it."""
    group = Groups.insert_new_group(owner_id, GroupForm(name=name, description="test"))
    assert group is not None
    return Groups.update_group_by_id(
        group.id,
        GroupUpdateForm(
            name=group.name,
            description=group.description,
            permissions=permissions,
            user_ids=[member_id],
        ),
    )


# --- T-046: deny-by-default seeding ----------------------------------------
@pytest.mark.tier1
def test_a_user_in_no_group_fails_every_scope_immediately_after_enable(test_user):
    """Enabling a mod must not hand its capabilities to the instance."""
    defaults = seed_defaults({}, _manifest(scopes=CREW_SCOPES))

    for scope in CREW_SCOPES:
        assert not has_permission(test_user["id"], scope["id"], defaults), scope["id"]


@pytest.mark.tier0
def test_seeding_alters_no_pre_existing_key_mod_or_core():
    before = {
        "workspace": {"models": True, "knowledge": False},
        "chat": {"delete": True},
        "mods": {"other": {"thing": True}},
    }
    after = seed_defaults(before, _manifest(scopes=CREW_SCOPES))

    assert after["workspace"] == {"models": True, "knowledge": False}
    assert after["chat"] == {"delete": True}
    assert after["mods"]["other"] == {"thing": True}
    assert after["mods"]["crew"]["session"] == {"connect": False, "drive": False}


@pytest.mark.tier0
def test_re_enabling_does_not_reset_a_scope_an_admin_has_since_granted():
    granted = seed_defaults({}, _manifest(scopes=CREW_SCOPES))
    granted["mods"]["crew"]["session"]["connect"] = True

    reseeded = seed_defaults(granted, _manifest(scopes=CREW_SCOPES))

    assert reseeded["mods"]["crew"]["session"]["connect"] is True, "an admin's grant must survive re-enable"
    assert reseeded["mods"]["crew"]["session"]["drive"] is False


@pytest.mark.tier0
def test_a_mod_with_no_scopes_block_seeds_nothing_and_does_not_error():
    before = {"workspace": {"models": True}}
    assert seed_defaults(before, _manifest(scopes=None)) == before


# --- T-047: lossless round trip with mod scopes ----------------------------
def _roundtrip(client):
    before = client.get("/api/v1/users/default/permissions").json()
    response = client.post("/api/v1/users/default/permissions", json=before)
    assert response.status_code == 200, response.text
    return before, response.json()


@pytest.mark.tier1
def test_the_mods_subtree_survives_a_roundtrip_intact(authenticated_admin, test_app):
    original = test_app.state.config.USER_PERMISSIONS
    test_app.state.config.USER_PERMISSIONS = seed_defaults(dict(original), _manifest(scopes=CREW_SCOPES))
    try:
        before, after = _roundtrip(authenticated_admin)
        assert after["mods"] == before["mods"]
    finally:
        test_app.state.config.USER_PERMISSIONS = original


@pytest.mark.tier1
def test_toggling_one_scope_leaves_every_other_scope_unchanged(authenticated_admin, test_app):
    original = test_app.state.config.USER_PERMISSIONS
    seeded = seed_defaults(dict(original), _manifest(scopes=CREW_SCOPES))
    seeded = seed_defaults(seeded, _manifest("other", [{"id": "mods.other.thing.read", "desc": "x"}]))
    test_app.state.config.USER_PERMISSIONS = seeded
    try:
        payload = authenticated_admin.get("/api/v1/users/default/permissions").json()
        payload["mods"]["crew"]["session"]["connect"] = True

        response = authenticated_admin.post("/api/v1/users/default/permissions", json=payload)
        assert response.status_code == 200, response.text
        result = response.json()

        assert result["mods"]["crew"]["session"]["connect"] is True
        assert result["mods"]["crew"]["session"]["drive"] is False, "the sibling scope must not move"
        assert result["mods"]["other"]["thing"]["read"] is False, "another mod's subtree must not move"
    finally:
        test_app.state.config.USER_PERMISSIONS = original


@pytest.mark.tier1
def test_a_granted_user_passes_the_check_after_the_roundtrip(authenticated_admin, test_app, test_user, db_session):
    original = test_app.state.config.USER_PERMISSIONS
    test_app.state.config.USER_PERMISSIONS = seed_defaults(dict(original), _manifest(scopes=CREW_SCOPES))
    _group_granting(
        db_session,
        test_user["id"],
        test_user["id"],
        {"mods": {"crew": {"session": {"connect": True}}}},
        name="crew-connect",
    )
    try:
        _roundtrip(authenticated_admin)
        defaults = test_app.state.config.USER_PERMISSIONS

        assert has_permission(test_user["id"], "mods.crew.session.connect", defaults)
        assert not has_permission(test_user["id"], "mods.crew.session.drive", defaults)
    finally:
        test_app.state.config.USER_PERMISSIONS = original


# --- shape-change in _merge_permissions: pinned, not incidental -------------
# The merge recurses only where BOTH sides are dicts. When a key's type changes
# between a bool leaf and a nested group, incoming wins wholesale -- no merge,
# no error. That is the sane behaviour for a deliberate shape change, but it is
# implicit. These tests make it deliberate: if someone later adds a type guard
# that rejects the change, or a silent coercion, these fail and point here.
@pytest.mark.tier0
def test_a_bool_to_dict_shape_change_is_replaced_not_merged():
    from selfai_ui.routers.users import _merge_permissions

    current = {"workspace": {"models": True}}
    incoming = {"workspace": {"models": {"sub": True}}}

    merged = _merge_permissions(current, incoming)

    assert merged["workspace"]["models"] == {"sub": True}, "the dict replaces the bool"


@pytest.mark.tier0
def test_a_dict_to_bool_shape_change_is_replaced_not_merged():
    from selfai_ui.routers.users import _merge_permissions

    current = {"workspace": {"models": {"sub": True, "other": False}}}
    incoming = {"workspace": {"models": False}}

    merged = _merge_permissions(current, incoming)

    assert merged["workspace"]["models"] is False, "the bool replaces the whole subtree"


@pytest.mark.tier0
def test_a_shape_change_is_confined_to_its_own_key():
    """A sibling key that DID match in shape is still merged, not clobbered."""
    from selfai_ui.routers.users import _merge_permissions

    current = {"mods": {"crew": {"session": True}}, "chat": {"delete": True}}
    incoming = {"mods": {"crew": {"session": {"connect": True}}}}

    merged = _merge_permissions(current, incoming)

    assert merged["mods"]["crew"]["session"] == {"connect": True}
    assert merged["chat"]["delete"] is True, "the unrelated key is untouched"


# --- T-048: granted state survives disablement -----------------------------
@pytest.mark.tier1
def test_a_group_grant_is_still_in_storage_after_disable(test_user, db_session, test_app):
    """Disabling is not revocation. The grant is an admin's decision about a
    person; turning a mod off is an operator's decision about the instance."""
    group = _group_granting(
        db_session,
        test_user["id"],
        test_user["id"],
        {"mods": {"crew": {"session": {"connect": True}}}},
        name="crew-survives",
    )
    assert group is not None

    # "Disable": the mod's scopes leave the runtime defaults object entirely.
    defaults_while_disabled = strip_mod_scopes(seed_defaults({}, _manifest(scopes=CREW_SCOPES)), "crew")
    assert "crew" not in defaults_while_disabled.get("mods", {})

    # The stored grant is untouched, and re-enabling restores the answer with
    # no admin re-grant.
    stored = Groups.get_group_by_id(group.id)
    assert stored.permissions["mods"]["crew"]["session"]["connect"] is True

    defaults_after_reenable = seed_defaults(defaults_while_disabled, _manifest(scopes=CREW_SCOPES))
    assert has_permission(test_user["id"], "mods.crew.session.connect", defaults_after_reenable)


@pytest.mark.tier1
def test_the_checker_gives_the_same_answer_for_the_same_stored_grant(test_user, db_session):
    """Load state is not an input to the checker — and must never become one.

    A grant present in the group's stored permissions returns the same answer
    whether or not the owning mod is loaded, because the checker has no notion
    of loading. What stops a disabled mod's scope from mattering is that
    nothing calls the check: it has no route, namespace, or tool.
    """
    _group_granting(
        db_session,
        test_user["id"],
        test_user["id"],
        {"mods": {"crew": {"session": {"connect": True}}}},
        name="crew-loadstate",
    )

    loaded = seed_defaults({}, _manifest(scopes=CREW_SCOPES))
    unloaded = strip_mod_scopes(loaded, "crew")

    assert has_permission(test_user["id"], "mods.crew.session.connect", loaded) is True
    assert has_permission(test_user["id"], "mods.crew.session.connect", unloaded) is True


@pytest.mark.tier0
def test_disabling_one_mod_alters_neither_another_mods_grants_nor_core():
    defaults = seed_defaults({"workspace": {"models": True}}, _manifest(scopes=CREW_SCOPES))
    defaults = seed_defaults(defaults, _manifest("other", [{"id": "mods.other.thing.read", "desc": "x"}]))
    defaults["mods"]["other"]["thing"]["read"] = True

    after = strip_mod_scopes(defaults, "crew")

    assert "crew" not in after["mods"]
    assert after["mods"]["other"]["thing"]["read"] is True
    assert after["workspace"]["models"] is True

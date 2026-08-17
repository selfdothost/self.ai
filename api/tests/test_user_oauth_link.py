# test_user_oauth_link.py
#
# Covers the OIDC account-linking audit stamp (self.ai#113 follow-up).
#
# We provision ~45 crew persona accounts programmatically and then merge each
# onto its OIDC identity on first Keycloak login. `created_at` records when the
# row was made and `last_active_at` moves on every request, so neither answers
# the operational question: did this persona ever link, and when.

import pytest

from selfai_ui.models.users import Users

# Deliberately far in the past. Using a fixed stale value rather than reading
# the current `updated_at` avoids a same-second false pass: `int(time.time())`
# has one-second resolution, so a test that writes and re-reads inside the same
# second would see no change and pass whether or not the stamp exists.
STALE = 1_000_000_000

OAUTH_SUB = "oidc@1f6db5bb-c70a-402b-8a2b-c5fe0b91f6ac"


@pytest.mark.tier0
def test_linking_an_oauth_sub_stamps_updated_at(test_user):
    Users.update_user_by_id(test_user["id"], {"updated_at": STALE})

    before = Users.get_user_by_id(test_user["id"])
    assert before.updated_at == STALE
    assert before.oauth_sub is None, "fixture user should start unlinked"

    linked = Users.update_user_oauth_sub_by_id(test_user["id"], OAUTH_SUB)

    assert linked.oauth_sub == OAUTH_SUB
    assert linked.updated_at > STALE, "linking must stamp updated_at, or the merge is unauditable"


@pytest.mark.tier0
def test_the_stamp_survives_a_reread(test_user):
    # Guards against the stamp existing only on the returned model rather than
    # being committed -- the audit is worthless if it is not in the row.
    Users.update_user_by_id(test_user["id"], {"updated_at": STALE})
    Users.update_user_oauth_sub_by_id(test_user["id"], OAUTH_SUB)

    reread = Users.get_user_by_id(test_user["id"])
    assert reread.oauth_sub == OAUTH_SUB
    assert reread.updated_at > STALE


@pytest.mark.tier0
def test_last_active_does_not_touch_updated_at(test_user):
    """The scoping is deliberate, so it gets a test.

    Bumping `updated_at` in `update_user_last_active_by_id` would rewrite the
    column on every request and destroy the linking signal entirely. This locks
    that decision in so a later "make the updaters consistent" sweep has to
    argue with a failing test rather than quietly undo it.
    """
    Users.update_user_by_id(test_user["id"], {"updated_at": STALE})

    Users.update_user_last_active_by_id(test_user["id"])

    after = Users.get_user_by_id(test_user["id"])
    assert after.updated_at == STALE, "activity is not a modification; it must not move updated_at"
    assert after.last_active_at > STALE

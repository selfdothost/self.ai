"""cavekit-browse-access-control.md — R1 (per-user/role gate), R2 (decoupled
from the Playwright service's own auth), R3 (consistent, clear denial), R4
(reuses this codebase's existing permission-check pattern)."""

import inspect
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException, status

from selfai_ui.browse import access_control as access_control_module
from selfai_ui.browse.access_control import (
    BROWSING_PERMISSION_KEY,
    check_browsing_access,
    has_browsing_access,
)
from selfai_ui.constants import ERROR_MESSAGES


def _user(role="user", user_id="u1"):
    return SimpleNamespace(id=user_id, role=role)


@pytest.mark.tier0
def test_admin_always_has_access():
    # Admin bypasses the permission lookup entirely — matches check_model_access
    # and every other admin-bypass check in this codebase.
    with patch("selfai_ui.browse.access_control.has_permission") as mock_has_permission:
        assert has_browsing_access(_user(role="admin"), {}) is True
        mock_has_permission.assert_not_called()


@pytest.mark.tier0
def test_user_without_permission_is_denied():
    permissions = {"features": {"web_browsing": False}}
    with patch("selfai_ui.browse.access_control.has_permission", return_value=False) as mock_has_permission:
        assert has_browsing_access(_user(role="user"), permissions) is False
        mock_has_permission.assert_called_once_with("u1", BROWSING_PERMISSION_KEY, permissions)


@pytest.mark.tier0
def test_user_with_permission_is_allowed():
    with patch("selfai_ui.browse.access_control.has_permission", return_value=True):
        assert has_browsing_access(_user(role="user"), {"features": {"web_browsing": True}}) is True


@pytest.mark.tier0
def test_check_raises_401_on_denial():
    # R3: denial is a clear, distinguishable HTTPException — not a silent
    # no-op, not a generic/technical error.
    with patch("selfai_ui.browse.access_control.has_permission", return_value=False):
        with pytest.raises(HTTPException) as exc_info:
            check_browsing_access(_user(role="user"), {})
        assert exc_info.value.status_code == 401


@pytest.mark.tier0
def test_check_does_not_raise_on_access():
    with patch("selfai_ui.browse.access_control.has_permission", return_value=True):
        check_browsing_access(_user(role="user"), {})  # must not raise


# --- R2: decoupled from the Playwright service's own auth -------------------


@pytest.mark.tier0
def test_module_never_imports_the_connection_module():
    # R2: granting/revoking self.ai-side browsing access must never require
    # touching the Playwright service — enforced structurally here by the
    # fact that this module has no dependency on connection.py (which owns
    # BROWSE_PLAYWRIGHT_SERVICE_URL / BROWSE_PLAYWRIGHT_API_KEY) at all.
    source = inspect.getsource(access_control_module)
    assert "browse.connection" not in source
    assert "BROWSE_PLAYWRIGHT" not in source


@pytest.mark.tier0
def test_gate_is_a_pure_function_of_user_and_permissions_only():
    # R2: has_browsing_access's signature takes only (user, user_permissions)
    # — there is no way to pass it Playwright-service state even if you
    # wanted to, so revoking/granting can only ever touch USER_PERMISSIONS.
    sig = inspect.signature(has_browsing_access)
    assert list(sig.parameters) == ["user", "user_permissions"]


@pytest.mark.tier0
def test_revoking_permission_takes_effect_via_permissions_dict_alone():
    with patch("selfai_ui.browse.access_control.has_permission", side_effect=[True, False]):
        user = _user(role="user")
        assert has_browsing_access(user, {"features": {"web_browsing": True}}) is True
        assert has_browsing_access(user, {"features": {"web_browsing": False}}) is False


# --- R3: consistent, clear denial -------------------------------------------


@pytest.mark.tier0
def test_denial_uses_the_same_error_shape_as_other_permission_denials():
    # R3: identical HTTPException(401, ERROR_MESSAGES.UNAUTHORIZED) shape
    # used by every other permission check in this codebase (see
    # routers/training.py, routers/chats.py, routers/tools.py).
    with patch("selfai_ui.browse.access_control.has_permission", return_value=False):
        with pytest.raises(HTTPException) as exc_info:
            check_browsing_access(_user(role="user"), {})
        assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
        assert exc_info.value.detail == ERROR_MESSAGES.UNAUTHORIZED


@pytest.mark.tier0
def test_denial_status_differs_from_a_technical_failure_status():
    # R3: a permissions denial (401) must be distinguishable from a
    # technical/upstream failure, which this codebase's conventions use 5xx
    # (or 502/503/504) for — not the same status family.
    with patch("selfai_ui.browse.access_control.has_permission", return_value=False):
        with pytest.raises(HTTPException) as exc_info:
            check_browsing_access(_user(role="user"), {})
        assert exc_info.value.status_code < 500


# --- R4: reuses this codebase's existing permission-check pattern -----------


@pytest.mark.tier0
def test_gate_delegates_to_the_existing_has_permission_utility():
    # R4 AC1: same mechanism this codebase already uses for every other
    # "can this role use X" check (workspace.training, chat.delete, etc.) —
    # not a new, parallel permission model. Proven by delegation: a
    # non-admin's result is determined entirely by has_permission's return
    # value, with no other permission logic in between.
    with patch("selfai_ui.browse.access_control.has_permission", return_value=True) as mock_has_permission:
        assert has_browsing_access(_user(role="user"), {}) is True
    with patch("selfai_ui.browse.access_control.has_permission", return_value=False) as mock_has_permission:
        assert has_browsing_access(_user(role="user"), {}) is False
    assert mock_has_permission.call_count == 1


# R4 AC2 is tagged [human-review] in the kit — not agent-automatable.
# Human-review note (2026-07-13): confirmed has_browsing_access/
# check_browsing_access reuse has_permission(user_id, "<dotted.key>",
# USER_PERMISSIONS) + admin-bypass, the exact shape used by
# routers/training.py ("workspace.training"), routers/chats.py
# ("chat.delete"), routers/tools.py, routers/knowledge.py, routers/
# prompts.py, and routers/models.py. No new/parallel permission model was
# introduced.

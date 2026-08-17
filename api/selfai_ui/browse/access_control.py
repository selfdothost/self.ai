"""User-side gating for browsing-backed tools (cavekit-browse-access-control.md).

This is self.ai's own permission check — independent of, and evaluated
before, any authentication the core Playwright connection performs against
the browser-automation service itself (cavekit-browse-connection.md R1).
Revoking or granting this permission never touches that service.
"""

from fastapi import HTTPException, status

from selfai_ui.constants import ERROR_MESSAGES
from selfai_ui.utils.access_control import has_permission

BROWSING_PERMISSION_KEY = "features.web_browsing"


def has_browsing_access(user, user_permissions: dict) -> bool:
    """R1: per-user/role gate. Admins always pass; other roles are gated by
    the standard USER_PERMISSIONS tree, same shape as every other
    studio/chat permission check in this codebase."""
    if user.role == "admin":
        return True
    return has_permission(user.id, BROWSING_PERMISSION_KEY, user_permissions)


def check_browsing_access(user, user_permissions: dict) -> None:
    """Raise a 401 if the user may not invoke a browsing-backed tool.

    R1: called before the core Playwright connection is ever invoked — the
    caller is expected to call this first and only proceed to the
    connection on success.
    R3: the denial is a standard HTTPException 401, identical in shape to
    every other permission denial in this codebase (see
    routers/training.py, routers/tools.py, routers/knowledge.py, etc.) —
    distinguishable from a technical failure and identifiable as a
    permissions denial, never a silent no-op.
    """
    if not has_browsing_access(user, user_permissions):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )

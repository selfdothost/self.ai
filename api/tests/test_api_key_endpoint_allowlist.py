"""_path_is_allowed: the API_KEY_ALLOWED_ENDPOINTS matcher (selfai/gitlab-profile#30).

ENABLE_API_KEY_ENDPOINT_RESTRICTIONS defaults off, so none of this bites in
production today -- but if it were switched on, the old exact-match-only
check could never admit a parameterised route like /mcp/{server}, silently
contradicting main.py's own comment telling an operator to allowlist "/mcp".
"""

import pytest

from selfai_ui.utils.auth import _path_is_allowed


@pytest.mark.parametrize(
    "path,allowed,expected",
    [
        # Exact match, unchanged behaviour.
        ("/api/chat/completions", ["/api/chat/completions"], True),
        ("/api/chat/completions", ["/api/models"], False),
        # A bare "/mcp" entry is exact-only -- it does NOT admit /mcp/{server}.
        # This is the trap the old code fell into: the entry existed, but a
        # real MCP request would still 403.
        ("/mcp/glab", ["/mcp"], False),
        ("/mcp", ["/mcp"], True),
        # "/mcp/*" is the fix: a prefix match that actually covers every
        # backend name.
        ("/mcp/glab", ["/mcp/*"], True),
        ("/mcp/echo", ["/mcp/*"], True),
        ("/mcp/mailbox", ["/mcp/*"], True),
        # The prefix must not swallow a sibling path that merely starts with
        # the same characters.
        ("/mcp-other", ["/mcp/*"], False),
        # Exact and prefix entries coexist in one allowlist.
        ("/api/v1/mcp/servers", ["/mcp/*", "/api/v1/mcp/servers"], True),
        ("/api/v1/mcp/servers/glab", ["/mcp/*", "/api/v1/mcp/servers"], False),
        # No entries at all -> nothing is allowed.
        ("/mcp/glab", [], False),
    ],
)
def test_path_is_allowed(path, allowed, expected):
    assert _path_is_allowed(path, allowed) is expected

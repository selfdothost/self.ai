"""Socket.IO attach with an API key (self.ai#81).

A crew captain Pod is cattle -- a fresh Pod per session, a dozen-plus identities
-- so it cannot depend on a human signing in to paste a session JWT. It already
carries an `sk-` API key for inference, which mint can issue and rotate
unattended, so that key is what it attaches with. These tests pin the credential
resolution (`resolve_socket_user`) and the connect handler's refusal contract:
every rejection carries a stable code, because a bare drop is indistinguishable
from a network fault or a wrong URL.

The handler is exercised directly rather than over a real Socket.IO transport,
matching the harness in test_mods_ws.py.
"""

import asyncio
import types

import pytest

from selfai_ui.socket import main as socket_main

API_KEY = "sk-0123456789abcdef"


def _user(uid="captain-1"):
    return types.SimpleNamespace(id=uid, name=uid, model_dump=lambda: {"id": uid})


@pytest.fixture
def api_key_user(monkeypatch):
    """A user reachable by API key, with last-active writes stubbed out."""
    user = _user()
    monkeypatch.setattr(
        socket_main.Users, "get_user_by_api_key", staticmethod(lambda key: user if key == API_KEY else None)
    )
    monkeypatch.setattr(socket_main.Users, "update_user_last_active_by_id", staticmethod(lambda uid: None))
    return user


@pytest.fixture
def api_key_enabled(monkeypatch):
    """ENABLE_API_KEY on, endpoint restrictions off -- the shipped defaults."""
    monkeypatch.setattr(socket_main.ENABLE_API_KEY, "value", True)
    monkeypatch.setattr(socket_main.ENABLE_API_KEY_ENDPOINT_RESTRICTIONS, "value", False)


def _refusal_code(auth):
    """Resolve `auth` and return the refusal code, asserting it was refused."""
    user, refusal = socket_main.resolve_socket_user(auth)
    assert user is None, f"expected refusal, got user {user}"
    return refusal[0]


# --- the credential a captain actually sends ---------------------------------


@pytest.mark.tier0
def test_api_key_in_the_api_key_field_is_accepted(api_key_enabled, api_key_user):
    """crew-code sends auth={'api_key': ...} when its apiKey field is set
    (remote-attach.ts). That field was previously ignored outright -- the whole
    bug in #81."""
    user, refusal = socket_main.resolve_socket_user({"api_key": API_KEY})
    assert refusal is None
    assert user is api_key_user


@pytest.mark.tier0
def test_api_key_in_the_token_field_is_accepted(api_key_enabled, api_key_user):
    """A caller with only one credential slot puts the key in `token`. Since
    decode_token is a bare jwt.decode, an sk- string there could only ever fail;
    routing it to the API-key path is strictly more useful."""
    user, refusal = socket_main.resolve_socket_user({"token": API_KEY})
    assert refusal is None
    assert user is api_key_user


@pytest.mark.tier0
def test_an_unrecognised_api_key_is_refused_by_code(api_key_enabled, api_key_user):
    assert _refusal_code({"api_key": "sk-not-a-real-key"}) == "invalid_api_key"


# --- the JWT path is untouched -----------------------------------------------


@pytest.mark.tier0
def test_a_valid_jwt_still_resolves(monkeypatch, api_key_enabled):
    """The browser path must be unaffected by the API-key branch."""
    user = _user("admiral-1")
    monkeypatch.setattr(socket_main, "decode_token", lambda t: {"id": "admiral-1"})
    monkeypatch.setattr(socket_main.Users, "get_user_by_id", staticmethod(lambda uid: user))

    resolved, refusal = socket_main.resolve_socket_user({"token": "a.jwt.value"})
    assert refusal is None
    assert resolved is user


@pytest.mark.tier0
def test_a_jwt_naming_no_user_is_refused_by_code(monkeypatch, api_key_enabled):
    monkeypatch.setattr(socket_main, "decode_token", lambda t: {"id": "ghost"})
    monkeypatch.setattr(socket_main.Users, "get_user_by_id", staticmethod(lambda uid: None))
    assert _refusal_code({"token": "a.jwt.value"}) == "unknown_user"


@pytest.mark.tier0
def test_no_credential_is_refused_by_code(api_key_enabled):
    assert _refusal_code(None) == "auth_required"
    assert _refusal_code({}) == "auth_required"
    assert _refusal_code({"something_else": "x"}) == "auth_required"


# --- the two switches the HTTP path honours ----------------------------------


@pytest.mark.tier0
def test_api_key_attach_respects_enable_api_key(monkeypatch, api_key_user):
    """ENABLE_API_KEY=False must shut the socket API-key path too, or the
    operator's switch means less than it says."""
    monkeypatch.setattr(socket_main.ENABLE_API_KEY, "value", False)
    monkeypatch.setattr(socket_main.ENABLE_API_KEY_ENDPOINT_RESTRICTIONS, "value", False)
    assert _refusal_code({"api_key": API_KEY}) == "api_key_disabled"


@pytest.mark.tier0
def test_endpoint_restrictions_gate_the_socket_path(monkeypatch, api_key_user):
    """With restrictions on, a socket the allowlist cannot name would silently
    widen a deliberate narrowing -- so the socket path must be listed."""
    monkeypatch.setattr(socket_main.ENABLE_API_KEY, "value", True)
    monkeypatch.setattr(socket_main.ENABLE_API_KEY_ENDPOINT_RESTRICTIONS, "value", True)
    monkeypatch.setattr(socket_main.API_KEY_ALLOWED_ENDPOINTS, "value", "/api/chat/completions")
    assert _refusal_code({"api_key": API_KEY}) == "api_key_endpoint_restricted"

    monkeypatch.setattr(
        socket_main.API_KEY_ALLOWED_ENDPOINTS,
        "value",
        f"/api/chat/completions,{socket_main.SOCKET_PATH}",
    )
    user, refusal = socket_main.resolve_socket_user({"api_key": API_KEY})
    assert refusal is None
    assert user is api_key_user


@pytest.mark.tier0
def test_endpoint_restrictions_do_not_gate_the_jwt_path(monkeypatch, api_key_user):
    """Restrictions are an API-key concept on HTTP; they must not start
    rejecting browsers here."""
    user = _user("admiral-2")
    monkeypatch.setattr(socket_main.ENABLE_API_KEY, "value", False)
    monkeypatch.setattr(socket_main.ENABLE_API_KEY_ENDPOINT_RESTRICTIONS, "value", True)
    monkeypatch.setattr(socket_main.API_KEY_ALLOWED_ENDPOINTS, "value", "")
    monkeypatch.setattr(socket_main, "decode_token", lambda t: {"id": "admiral-2"})
    monkeypatch.setattr(socket_main.Users, "get_user_by_id", staticmethod(lambda uid: user))

    resolved, refusal = socket_main.resolve_socket_user({"token": "a.jwt.value"})
    assert refusal is None
    assert resolved is user


# --- end to end through the connect handler ----------------------------------


class _Pool:
    def __init__(self):
        self.data = {}

    async def aset(self, k, v):
        self.data[k] = v

    async def aget(self, k, default=None):
        return self.data.get(k, default)

    async def akeys(self):
        return list(self.data)


@pytest.fixture
def connect_env(monkeypatch):
    """Stub the pools and emits so `connect` can run without Redis or a client."""
    session_pool, user_pool = _Pool(), _Pool()
    monkeypatch.setattr(socket_main, "SESSION_POOL", session_pool)
    monkeypatch.setattr(socket_main, "USER_POOL", user_pool)

    async def _models():
        return []

    async def _noop_emit(*a, **k):
        return None

    monkeypatch.setattr(socket_main, "get_models_in_use", _models)
    monkeypatch.setattr(socket_main.sio, "emit", _noop_emit)
    return session_pool


@pytest.mark.tier0
def test_connect_accepts_an_api_key_and_records_the_session(api_key_enabled, api_key_user, connect_env):
    """The load-bearing case: a captain Pod attaches with only its API key."""
    result = asyncio.run(socket_main.connect("sid-captain", {}, {"api_key": API_KEY}))
    assert result is True
    assert connect_env.data["sid-captain"] == {"id": "captain-1"}


@pytest.mark.tier0
def test_connect_refusal_carries_a_reason_the_client_can_read(api_key_enabled, api_key_user, connect_env):
    """#81 item 3: a bad credential used to be a bare `return False`, which the
    client saw as an unexplained drop. It now raises with a human-readable
    message and a stable code in the DISCONNECT payload."""
    with pytest.raises(socket_main.SocketConnectionRefused) as exc:
        asyncio.run(socket_main.connect("sid-bad", {}, {"api_key": "sk-wrong"}))

    args = exc.value.error_args
    assert args["data"]["code"] == "invalid_api_key"
    assert "api key" in args["message"].lower()

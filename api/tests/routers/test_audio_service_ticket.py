"""The audio proxy attaches a self-hosted service ticket (self.ai#63).

self.speak / self.transcribe validate a scoped X-Selfai-Ticket on their serving
endpoints and fail-closed with 401. The proxy in routers/audio.py must mint and
attach one when the target is a self-hosted engine (marked by its CONTROL base
URL being set) — and must NOT leak an internal ticket to an external provider,
nor ever fail the request if minting can't happen.
"""

import jwt

from selfai_ui.routers.audio import _service_ticket_header
from selfai_ui.utils.service_auth import TICKET_HEADER


def test_no_ticket_when_control_url_unset(monkeypatch):
    """External provider (no self-hosted control URL) -> no internal ticket.

    Gated before minting even runs, so a configured secret must not change it.
    """
    monkeypatch.setattr("selfai_ui.utils.service_auth.SERVICE_AUTH_SECRET", "s3cr3t", raising=False)
    assert _service_ticket_header("", "self.speak", "audio:synthesize") == {}
    assert _service_ticket_header("   ", "self.speak", "audio:synthesize") == {}


def test_ticket_minted_for_self_hosted(monkeypatch):
    """Self-hosted (control URL set) -> a valid, correctly-scoped ticket."""
    secret = "shared-hs256-secret"
    monkeypatch.setattr("selfai_ui.utils.service_auth.SERVICE_AUTH_SECRET", secret, raising=False)

    header = _service_ticket_header(
        "http://self-speak:8880", "self.speak", "audio:synthesize"
    )
    assert set(header) == {TICKET_HEADER}

    # The engine will decode with the shared secret + its own audience; prove the
    # ticket we mint passes exactly that check.
    claims = jwt.decode(
        header[TICKET_HEADER],
        secret,
        algorithms=["HS256"],
        audience="self.speak",
    )
    assert claims["aud"] == "self.speak"
    assert "audio:synthesize" in claims["scope"].split()
    assert claims["exp"] > claims["iat"]  # short-lived, not standing


def test_transcribe_audience_and_scope(monkeypatch):
    secret = "shared-hs256-secret"
    monkeypatch.setattr("selfai_ui.utils.service_auth.SERVICE_AUTH_SECRET", secret, raising=False)
    header = _service_ticket_header(
        "http://self-transcribe:8890", "self.transcribe", "audio:transcribe"
    )
    claims = jwt.decode(
        header[TICKET_HEADER], secret, algorithms=["HS256"], audience="self.transcribe"
    )
    assert claims["aud"] == "self.transcribe"
    assert "audio:transcribe" in claims["scope"].split()


def test_missing_secret_never_breaks_serving(monkeypatch):
    """If the secret is unconfigured, return {} (log + proceed), never raise."""
    monkeypatch.setattr("selfai_ui.utils.service_auth.SERVICE_AUTH_SECRET", "", raising=False)
    # control URL is set, so it WOULD mint — but the secret is missing.
    assert _service_ticket_header(
        "http://self-speak:8880", "self.speak", "audio:synthesize"
    ) == {}

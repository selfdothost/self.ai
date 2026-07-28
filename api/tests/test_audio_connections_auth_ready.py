"""cavekit-audio-connections.md R8 (Mutating Operations Ship Without Blocking on
Platform-Wide Auth) — the auth-ready credential slot on mutating management
requests (T-009).

Verifies each R8 acceptance criterion individually:
  AC1: self-hosted STT management actions are usable within the trusted
       perimeter with no dedicated auth step of their own.
  AC2: those actions are not exposed on an untrusted external network.
  AC3: every mutating management request defines an optional credential slot in
       its request contract — accepted but unused/unchecked today.
  AC4: prose states plainly that these actions rely on network-perimeter
       protection until the separate platform-wide auth effort lands.

AC1/AC2/AC4 are posture/prose criteria (this repo is the API server; the network
perimeter is an infrastructure concern, and the "no dedicated auth step" state is
proven by the slot being inert). They are asserted here against the in-code
posture statement and the absence of any credential check, alongside the
concrete AC3 contract.
"""

import pytest

from selfai_ui.audio.connections import (
    CREDENTIAL_SLOT,
    PERIMETER_POSTURE,
    AudioConnectionType,
    ManagementAction,
    ManagementMutationRequest,
    NotAMutatingActionError,
    build_mutation_request,
    get_type_info,
    management_action_for_type,
    serialize_connection_type,
    serialize_management_action,
)

SELF_HOSTED = [
    AudioConnectionType.SELF_HOSTED_STT,
    AudioConnectionType.SELF_HOSTED_TTS,
]
HOSTED = [
    AudioConnectionType.HOSTED_GENERAL,
    AudioConnectionType.HOSTED_SPECIALIZED_A,
    AudioConnectionType.HOSTED_SPECIALIZED_B,
]


def _stt_action() -> ManagementAction:
    return management_action_for_type(get_type_info(AudioConnectionType.SELF_HOSTED_STT))


def _tts_action() -> ManagementAction:
    return management_action_for_type(get_type_info(AudioConnectionType.SELF_HOSTED_TTS))


# --- AC1: usable within the trusted perimeter with no dedicated auth step ------


@pytest.mark.tier0
def test_stt_mutation_usable_with_no_credential_supplied():
    # The mutating request builds and is well-formed with NO credential — i.e.
    # no dedicated auth step is required to use it within the perimeter.
    request = build_mutation_request(_stt_action(), model_id="whisper-large-v3")
    assert request.model_id == "whisper-large-v3"
    assert request.credential is None
    # Nothing is checked: the request is not "authenticated", yet it is valid.
    assert request.is_authenticated is False


# --- AC2: not exposed on an untrusted external network -------------------------


@pytest.mark.tier0
def test_credential_slot_is_inert_no_check_gates_use():
    # AC2's "not exposed externally" is a perimeter (infra) property; the code's
    # side of it is that the credential slot performs NO gating — a supplied
    # credential neither grants nor denies, and its absence blocks nothing. So
    # the module ships no auth check that could be relied on in place of the
    # perimeter.
    with_cred = build_mutation_request(_stt_action(), "m", credential="tok")
    without_cred = build_mutation_request(_stt_action(), "m")
    # Presence/absence of a credential changes nothing about authorization today.
    assert with_cred.is_authenticated is without_cred.is_authenticated is False


# --- AC3: every mutating request defines an optional credential slot -----------


@pytest.mark.tier0
def test_stt_action_declares_the_credential_slot():
    action = _stt_action()
    assert action.supports_download is True  # it is the mutating action
    assert action.accepts_credential is True
    assert action.credential_slot == CREDENTIAL_SLOT == "credential"


@pytest.mark.tier0
def test_mutation_request_carries_optional_credential_slot():
    # Slot is optional (defaulted) ...
    default = ManagementMutationRequest(model_id="m")
    assert default.credential is None
    # ... and accepts a value without being checked.
    supplied = ManagementMutationRequest(model_id="m", credential="bearer-xyz")
    assert supplied.credential == "bearer-xyz"


@pytest.mark.tier0
def test_mutation_request_serializes_the_slot_even_when_absent():
    # The slot is a STABLE part of the wire contract: present (as null) even when
    # no credential is supplied, so a future gate can read a fixed shape.
    body = ManagementMutationRequest(model_id="whisper").to_serializable()
    assert body == {"model_id": "whisper", "credential": None}
    assert CREDENTIAL_SLOT in body

    body2 = ManagementMutationRequest(model_id="whisper", credential="t").to_serializable()
    assert body2 == {"model_id": "whisper", "credential": "t"}


@pytest.mark.tier0
def test_build_mutation_request_passes_credential_through_unchecked():
    request = build_mutation_request(_stt_action(), "whisper", credential="secret")
    assert request.credential == "secret"
    # Accepted into the contract, but confers no authentication today.
    assert request.is_authenticated is False


@pytest.mark.tier0
def test_browse_only_action_has_no_mutating_request():
    # TTS is browse-only: no mutation, so no credential slot and no request.
    tts = _tts_action()
    assert tts.supports_download is False
    assert tts.accepts_credential is False
    assert tts.credential_slot is None
    with pytest.raises(NotAMutatingActionError):
        build_mutation_request(tts, "any-voice")


@pytest.mark.tier0
def test_serialized_stt_action_exposes_auth_readiness():
    payload = serialize_connection_type(get_type_info(AudioConnectionType.SELF_HOSTED_STT))
    ma = payload["management_action"]
    assert ma["accepts_credential"] is True
    assert ma["credential_slot"] == "credential"


@pytest.mark.tier0
def test_serialized_tts_action_declares_no_credential_slot():
    payload = serialize_connection_type(get_type_info(AudioConnectionType.SELF_HOSTED_TTS))
    ma = payload["management_action"]
    assert ma["accepts_credential"] is False
    assert ma["credential_slot"] is None


@pytest.mark.tier0
@pytest.mark.parametrize("connection_type", HOSTED)
def test_hosted_providers_have_no_action_and_no_slot(connection_type):
    # Hosted providers expose no self-hosted management action at all, hence no
    # mutating request and no credential slot.
    payload = serialize_connection_type(get_type_info(connection_type))
    assert payload["management_action"] is None


@pytest.mark.tier0
def test_serialize_management_action_includes_credential_keys():
    ma = serialize_management_action(_stt_action())
    assert "accepts_credential" in ma
    assert "credential_slot" in ma


# --- AC4: prose states reliance on network-perimeter protection ----------------


@pytest.mark.tier0
def test_perimeter_posture_statement_is_present_and_plain():
    text = PERIMETER_POSTURE.lower()
    # States reliance on network-perimeter protection ...
    assert "perimeter" in text
    # ... and that it is until the separate platform-wide auth effort lands ...
    assert "until" in text
    assert "auth" in text
    # ... and that the slot is accepted-but-unchecked, not an active check.
    assert "unchecked" in text

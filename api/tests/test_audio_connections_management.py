"""cavekit-audio-connections.md R4 (Management Action for Self-Hosted Types) —
the self-hosted management entry point on the Connections surface (T-005).

Verifies each R4 acceptance criterion individually:
  AC1: a self-hosted STT connection exposes a management action from its entry.
  AC2: a self-hosted TTS connection exposes a management action from its entry.
  AC3: the three hosted-provider types do NOT expose this action.
  AC4: opening the management action does not navigate away from the surface.
"""

import pytest

from selfai_ui.audio.connections import (
    AudioConnectionType,
    ManagementAction,
    ManagementActionKind,
    available_connection_types,
    get_type_info,
    has_management_action,
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


# --- AC1: self-hosted STT exposes a management action --------------------------


@pytest.mark.tier0
def test_self_hosted_stt_exposes_a_management_action():
    info = get_type_info(AudioConnectionType.SELF_HOSTED_STT)
    action = management_action_for_type(info)

    assert action is not None
    assert has_management_action(info) is True
    # STT manages its downloadable model catalog (content owned by T-020).
    assert action.kind is ManagementActionKind.STT_MODELS
    assert action.target == "/api/v1/transcribe/models"


# --- AC2: self-hosted TTS exposes a management action --------------------------


@pytest.mark.tier0
def test_self_hosted_tts_exposes_a_management_action():
    info = get_type_info(AudioConnectionType.SELF_HOSTED_TTS)
    action = management_action_for_type(info)

    assert action is not None
    assert has_management_action(info) is True
    # TTS manages its voice catalog (content owned by T-010).
    assert action.kind is ManagementActionKind.TTS_VOICES
    assert action.target  # a concrete forward-pointer target exists


# --- AC3: hosted providers expose NO self-hosted management action -------------


@pytest.mark.tier0
@pytest.mark.parametrize("connection_type", HOSTED)
def test_hosted_providers_expose_no_management_action(connection_type):
    info = get_type_info(connection_type)
    assert management_action_for_type(info) is None
    assert has_management_action(info) is False


@pytest.mark.tier0
def test_gate_matches_the_self_hosted_flag_exactly():
    # The entry point is gated solely off the existing ``self_hosted`` flag:
    # action present iff self_hosted, for all five types — one source of truth.
    for info in available_connection_types():
        assert has_management_action(info) is info.self_hosted


# --- AC4: opening the action does not navigate away from the surface -----------


@pytest.mark.tier0
@pytest.mark.parametrize("connection_type", SELF_HOSTED)
def test_management_action_opens_in_surface(connection_type):
    action = management_action_for_type(get_type_info(connection_type))
    assert isinstance(action, ManagementAction)
    # R4 AC4 encoded as a contract invariant the surface can rely on.
    assert action.opens_in_surface is True


# --- serialized representation carries the entry point -------------------------


@pytest.mark.tier0
@pytest.mark.parametrize("connection_type", SELF_HOSTED)
def test_serialized_self_hosted_type_carries_management_action(connection_type):
    payload = serialize_connection_type(get_type_info(connection_type))
    assert payload["self_hosted"] is True
    ma = payload["management_action"]
    assert ma is not None
    assert ma["opens_in_surface"] is True
    assert ma["kind"] in {"stt_models", "tts_voices"}
    assert ma["target"]


@pytest.mark.tier0
@pytest.mark.parametrize("connection_type", HOSTED)
def test_serialized_hosted_type_has_null_management_action(connection_type):
    payload = serialize_connection_type(get_type_info(connection_type))
    assert payload["self_hosted"] is False
    assert payload["management_action"] is None


@pytest.mark.tier0
def test_serialize_management_action_none_passthrough():
    assert serialize_management_action(None) is None

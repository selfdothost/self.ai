"""cavekit-audio-connections.md R7 (Management Content Differs by Backend
Capability) — the STT-download-vs-TTS-browse distinction on the shared
management entry point (T-008).

R7 makes the download capability an *explicit, discoverable* field on the
``ManagementAction`` that both self-hosted types produce via the same
``management_action_for_type`` entry point (R4/T-005), rather than something a
caller infers by knowing which ``kind`` maps to which router.

Verifies each R7 acceptance criterion individually:
  AC1: the self-hosted STT management view offers a download action (a model not
       currently present can be pulled) — ``supports_download=True`` + a
       non-empty download target naming the pull endpoint.
  AC2: the self-hosted TTS management view offers NO download action (its voice
       set is fixed at deploy time) — ``supports_download=False`` + no target.
  AC3: both views are reached through the same entry point and present their
       catalog in the same list-based form — same ``ManagementAction`` shape.
  AC4: the ONLY presentational difference between the two views is the presence
       (STT) / absence (TTS) of the download action.
"""

import pytest

from selfai_ui.audio.connections import (
    AudioConnectionType,
    ManagementAction,
    ManagementActionKind,
    get_type_info,
    management_action_for_type,
    serialize_connection_type,
    serialize_management_action,
)


def _stt_action() -> ManagementAction:
    return management_action_for_type(get_type_info(AudioConnectionType.SELF_HOSTED_STT))


def _tts_action() -> ManagementAction:
    return management_action_for_type(get_type_info(AudioConnectionType.SELF_HOSTED_TTS))


# --- AC1: STT view offers a download action -----------------------------------


@pytest.mark.tier0
def test_stt_management_view_offers_a_download_action():
    action = _stt_action()
    assert action is not None
    assert action.kind is ManagementActionKind.STT_MODELS
    # The catalog is not fixed at deploy time: a not-yet-present model can be
    # pulled on demand.
    assert action.supports_download is True
    # The download target names the concrete transcribe-router pull endpoint
    # (T-021), with a {model_id} slot the caller fills for the model to download.
    assert action.download_target
    assert action.download_target == "/api/v1/transcribe/models/{model_id}/pull"
    assert "{model_id}" in action.download_target


# --- AC2: TTS view offers NO download action ----------------------------------


@pytest.mark.tier0
def test_tts_management_view_offers_no_download_action():
    action = _tts_action()
    assert action is not None
    assert action.kind is ManagementActionKind.TTS_VOICES
    # Browse-and-curate only: the voice set is fixed at deploy time, so there is
    # nothing to download and no pull endpoint to point at.
    assert action.supports_download is False
    assert action.download_target is None


# --- AC3: both reached through the same entry point, same list-based form ------


@pytest.mark.tier0
def test_both_views_share_the_same_entry_point_and_shape():
    stt = _stt_action()
    tts = _tts_action()
    # Same entry point (management_action_for_type) yields the same dataclass
    # type for both — the same list-based management-view contract.
    assert isinstance(stt, ManagementAction)
    assert isinstance(tts, ManagementAction)
    assert type(stt) is type(tts)
    # Same serialized field set (keys), regardless of capability values.
    assert set(serialize_management_action(stt)) == set(serialize_management_action(tts))
    # Both open in-surface (browsed in place, not navigated to).
    assert stt.opens_in_surface is True
    assert tts.opens_in_surface is True


# --- AC4: the ONLY presentational difference is the download capability --------


@pytest.mark.tier0
def test_only_difference_between_views_is_the_download_capability():
    stt = serialize_management_action(_stt_action())
    tts = serialize_management_action(_tts_action())

    differing = {k for k in stt if stt[k] != tts[k]}
    # kind/label/target/download_target legitimately differ per type; the
    # capability flag is the load-bearing one. accepts_credential/credential_slot
    # (R8/T-009) track the same mutating-vs-browse-only distinction as
    # supports_download, not a new structural asymmetry — STT is the domain's
    # only mutating action, so it is also the only one with an auth-ready slot.
    assert differing <= {
        "kind",
        "label",
        "target",
        "supports_download",
        "download_target",
        "accepts_credential",
        "credential_slot",
    }
    # The capability distinction itself is present and correctly polarized.
    assert "supports_download" in differing
    assert stt["supports_download"] is True
    assert tts["supports_download"] is False
    # The auth-ready slot (R8) is polarized identically to the download
    # capability: present only on the mutating (STT) action.
    assert stt["accepts_credential"] is True
    assert tts["accepts_credential"] is False
    assert stt["credential_slot"] == "credential"
    assert tts["credential_slot"] is None
    # opens_in_surface (the in-surface invariant) is identical for both — the
    # difference is capability, not navigation behavior.
    assert "opens_in_surface" not in differing


# --- serialized contract carries the capability fields ------------------------


@pytest.mark.tier0
def test_serialized_stt_connection_type_carries_download_capability():
    payload = serialize_connection_type(get_type_info(AudioConnectionType.SELF_HOSTED_STT))
    ma = payload["management_action"]
    assert ma["supports_download"] is True
    assert ma["download_target"] == "/api/v1/transcribe/models/{model_id}/pull"


@pytest.mark.tier0
def test_serialized_tts_connection_type_has_no_download_capability():
    payload = serialize_connection_type(get_type_info(AudioConnectionType.SELF_HOSTED_TTS))
    ma = payload["management_action"]
    assert ma["supports_download"] is False
    assert ma["download_target"] is None


@pytest.mark.tier0
@pytest.mark.parametrize(
    "connection_type",
    [
        AudioConnectionType.HOSTED_GENERAL,
        AudioConnectionType.HOSTED_SPECIALIZED_A,
        AudioConnectionType.HOSTED_SPECIALIZED_B,
    ],
)
def test_hosted_providers_expose_no_management_action_at_all(connection_type):
    # Hosted providers have no self-hosted management action, hence no
    # capability fields to speak of (unchanged from R4/T-005).
    assert management_action_for_type(get_type_info(connection_type)) is None

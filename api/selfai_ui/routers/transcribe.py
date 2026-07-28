"""Transcribe router — self-hosted STT (self.transcribe) model management.

R1 (Model Listing) of ``cavekit-audio-transcribe-router.md``: report which STT
models are downloaded and ready to serve, plus which known catalog entries are
available to pull but not yet present, with the two clearly distinguished.

R2 (Model Pull): download a not-yet-present model from its source without
redeploying or rebuilding the backend, streaming incremental progress rather
than only a terminal success/failure.

R4 (Active Model Swap): change which downloaded model actually serves
transcription requests without a full service restart.

Admin curation (cavekit-audio-transcribe-picker R1/R2): each listing entry
also carries ``enabled`` — whether an admin has enabled it for end-user
selection — and a search filter, plus a toggle endpoint to set that flag.

This is the STT analog of ``routers/llamolotl.py``'s model discovery, pull,
and load. The listing, pull, and swap all talk to the self-hosted STT
backend's *control* port (``AUDIO_STT_CONTROL_BASE_URL``), separate from the
OpenAI-compatible *serving* endpoint used for transcription requests — the
same serving/control split self.llamolotl uses. Pull mirrors self.llamolotl's
streaming pull: the router proxies the control port's NDJSON progress stream,
normalizing each chunk so every emitted event carries an explicit
success/error/pulling phase.

R3 (Pull Cancellation): stop an in-flight pull before it completes. The cancel
endpoint sends a single control-port ``pull/cancel`` command (the STT analog of
self.llamolotl's ``/api/models/pull/cancel``) identifying the pull by model id;
the backend stops the download on its end, which terminates the still-open
``_stream_pull`` response — ideally with a terminal ``cancelled`` event. The UI
server holds no per-pull cancel-state, so cancelling never blocks a later re-pull
of the same model.

R5 (Model Deletion): remove a downloaded model to reclaim storage. Deletion is
gated on the active-model invariant (R7 AC2): the currently-active model may not
be deleted while it remains active — it must first be swapped (R4) to another
downloaded model. The delete endpoint resolves the R1 listing, calls
``transcribe.invariant.assert_deletable`` and rejects an active-model deletion
with HTTP 409, then issues a single control-port ``delete`` command (the STT
analog of self.llamolotl's ``/api/models/delete``). Because the backend's own
``/api/models`` view is the sole source of the downloaded-and-ready set, once the
backend removes the model it simply drops out of the R1 listing (R5 AC2/AC3).

R6 (Auth-Ready, Not Auth-Blocked): the three mutating operations (pull, cancel,
delete) ship network-perimeter-protected, without a dedicated authentication
mechanism of their own, while a separate platform-wide authenticated-access
effort proceeds independently. So a future auth gate can be added without a
breaking change, each mutating request already defines an OPTIONAL credential
slot in its request body (``MutatingActionForm.credential``, wire key
:data:`CREDENTIAL_SLOT`) — accepted but entirely unused and unchecked today, and
never forwarded to the backend control command. A future gate reads that slot
without reshaping the request. See :data:`PERIMETER_POSTURE` for the plainly
stated posture (R6 AC4); this slot reuses the same connections-domain contract
T-009 established on ``ManagementMutationRequest``.
"""

import json
import logging
from typing import AsyncGenerator, Optional

import aiohttp
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from selfai_ui.audio.connections import CREDENTIAL_SLOT, PERIMETER_POSTURE
from selfai_ui.env import (
    AIOHTTP_CLIENT_TIMEOUT,
    AIOHTTP_CLIENT_TIMEOUT_OPENAI_MODEL_LIST,
    SRC_LOG_LEVELS,
)
from selfai_ui.models.users import Users
from selfai_ui.transcribe.delete import build_delete_payload
from selfai_ui.transcribe.invariant import (
    ModelIsActiveError,
    assert_deletable,
    mark_active_model,
)
from selfai_ui.transcribe.listing import (
    build_model_listing,
    curate_listing,
)
from selfai_ui.transcribe.picker import resolve_selectable_state
from selfai_ui.transcribe.pull import (
    build_cancel_payload,
    build_pull_payload,
    error_event,
    incomplete_stream_event,
    is_terminal,
    normalize_pull_event,
)
from selfai_ui.transcribe.swap import ModelNotDownloadedError, validate_swap_target
from selfai_ui.utils.auth import get_admin_user, get_verified_user

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS.get("AUDIO", logging.INFO))

router = APIRouter()


def get_transcribe_control_url(request: Request) -> str:
    """Resolve the self-hosted STT control-port base URL, or 400 if unset.

    A 400 (not 500) because an absent control URL means "no self-hosted STT
    router backend is configured" — a client/config condition, not a server
    fault. Mirrors llamolotl's guard on an unconfigured backend.
    """
    control_url = getattr(request.app.state.config, "STT_CONTROL_BASE_URL", "") or ""
    control_url = control_url.strip()
    if not control_url:
        raise HTTPException(
            status_code=400,
            detail="No self-hosted STT router backend is configured",
        )
    return control_url.rstrip("/")


async def _send_get(url: str, key: Optional[str] = None):
    """GET a control-port view, returning parsed JSON or None on any failure.

    A missing/failed view is returned as None so the listing can degrade
    gracefully (e.g. report only downloaded models if the catalog view is
    unreachable) rather than failing the whole request.
    """
    timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_OPENAI_MODEL_LIST)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(
                url,
                headers={**({"Authorization": f"Bearer {key}"} if key else {})},
            ) as response:
                if response.status >= 400:
                    log.warning("transcribe control GET %s -> HTTP %s", url, response.status)
                    return None
                return await response.json()
    except Exception as e:
        log.warning("transcribe control GET %s failed: %s", url, e)
        return None


def _enabled_map(request: Request) -> dict:
    """Return a copy of the admin-set model-id -> enabled map from config."""
    raw = getattr(request.app.state.config, "STT_ENABLED_MODELS", None)
    return dict(raw) if isinstance(raw, dict) else {}


@router.get("/models")
async def list_transcribe_models(
    request: Request,
    search: Optional[str] = None,
    user=Depends(get_admin_user),
):
    """List the router's models: downloaded-and-ready plus pullable catalog.

    Each entry carries ``downloaded`` (bool) and ``availability``
    ("downloaded" | "pullable"), so a downloaded-and-ready model is
    unambiguously distinguishable from a merely-pullable one.

    Curation layer (cavekit-audio-transcribe-picker R1): each entry also carries
    ``enabled`` — whether an admin has enabled it for end-user selection — and an
    optional ``search`` query param filters the list by id/name/description
    (case-insensitive), consistent with the established curation-list pattern.

    Active-model indicator (cavekit-audio-transcribe-picker R3): each entry also
    carries ``active`` — True for the single model the router currently has
    resident/serving, False otherwise (``mark_active_model``). ``active`` is
    strictly narrower than ``downloaded``, so the curation list marks the active
    model as more than merely downloaded without a separate call; the marker moves
    with the router's active model on any subsequent listing.
    """
    control_url = get_transcribe_control_url(request)
    key = getattr(request.app.state.config, "STT_OPENAI_API_KEY", None) or None

    downloaded_raw = await _send_get(f"{control_url}/api/models", key)
    catalog_raw = await _send_get(f"{control_url}/api/models/available", key)

    if downloaded_raw is None and catalog_raw is None:
        raise HTTPException(
            status_code=502,
            detail="Self.AI UI: could not reach the self-hosted STT router backend",
        )

    listing = build_model_listing(downloaded_raw, catalog_raw)
    # Resolve the active model over the full listing before search narrows it, so
    # the active marker never depends on whether the active model matches a filter.
    listing = mark_active_model(listing)
    return curate_listing(listing, _enabled_map(request), search)


class ModelEnabledForm(BaseModel):
    """Body for the enable/disable toggle: the desired enabled state."""

    enabled: bool


@router.post("/models/{model_id}/enabled")
async def set_transcribe_model_enabled(
    model_id: str,
    form_data: ModelEnabledForm,
    request: Request,
    user=Depends(get_admin_user),
):
    """Set whether end users may select the given transcribe model.

    Persists an admin-set ``id -> enabled`` flag (cavekit-audio-transcribe-picker
    R1). The flag is stored by model id independent of whether the model is
    downloaded or merely pullable — an admin may enable a model before it is
    pulled — and it survives reload via PersistentConfig. The toggle does not
    require the STT backend to be reachable: it records intent against the id the
    admin is curating, so curation stays usable even when the backend is down.
    """
    config = request.app.state.config
    enabled_map = _enabled_map(request)
    enabled_map[model_id] = form_data.enabled
    # Reassign a fresh dict so PersistentConfig replaces its value and persists.
    config.STT_ENABLED_MODELS = enabled_map

    return {"id": model_id, "enabled": form_data.enabled}


# ---------------------------------------------------------------------------
# End-user model selection (cavekit-audio-transcribe-picker R4, net new)
# ---------------------------------------------------------------------------
#
# Unlike voices, there is no pre-existing end-user surface here to replace:
# today an end user can pick which STT *engine* to use, never which *model*.
# These two routes are that net-new capability — a plain (non-admin) end user
# can list the transcribe models an admin has enabled and save one as their
# personal choice. Both reuse the same control-port listing + ``_enabled_map``
# machinery the admin curation surface (T-028) built, only scoped down to the
# enabled-only set (R4 AC2/AC3). The STT engine selection itself is a separate,
# pre-existing concern these routes deliberately do not touch.


@router.get("/models/selectable")
async def list_selectable_transcribe_models(
    request: Request, user=Depends(get_verified_user)
):
    """List the transcribe models a plain end user may select (R4/R5).

    The end-user counterpart to the admin ``GET /models`` curation view: it runs
    the same control-port listing but returns ONLY the models an admin has
    enabled (``_enabled_map`` id -> True). A model an admin has disabled — or
    never enabled — is absent from this list (R4 AC2/AC3), so an end-user picker
    built on it can offer only permitted models.

    Graceful empty state (cavekit-audio-transcribe-picker R5): the response is
    resolved by ``resolve_selectable_state`` into a self-describing envelope with
    a ``mode`` field so the picker never renders a broken, empty control:

    * ``mode: "enabled"`` — ``data`` holds the admin-enabled selectable models.
    * ``mode: "fallback"`` — no model is admin-enabled but one is actively
      serving; ``active_model``/``fallback_model`` name it so the picker falls
      back to the router's active model (R5 AC1) instead of an empty control.
    * ``mode: "unavailable"`` — zero models are downloaded (the router's
      permitted no-active state); ``message`` carries a clear unavailable
      statement (R5 AC2).

    Authenticated end user (``get_verified_user``), not admin-gated: this is a
    personal-settings affordance, not a management action.
    """
    control_url = get_transcribe_control_url(request)
    key = getattr(request.app.state.config, "STT_OPENAI_API_KEY", None) or None

    downloaded_raw = await _send_get(f"{control_url}/api/models", key)
    catalog_raw = await _send_get(f"{control_url}/api/models/available", key)

    if downloaded_raw is None and catalog_raw is None:
        raise HTTPException(
            status_code=502,
            detail="Self.AI UI: could not reach the self-hosted STT router backend",
        )

    listing = build_model_listing(downloaded_raw, catalog_raw)
    return resolve_selectable_state(listing, _enabled_map(request))


class TranscribeModelSelectionForm(BaseModel):
    """Body for the end-user model-selection save: the chosen model id."""

    model_id: str

    # ``model_id`` sits in pydantic v2's protected ``model_`` namespace; opt out
    # so the field name matches the wire contract (``{"model_id": ...}``) without
    # a spurious namespace warning.
    model_config = {"protected_namespaces": ()}


def _persist_user_stt_model(user_id: str, model_id: str) -> None:
    """Save ``model_id`` as the user's personal STT model choice.

    Mirrors the existing per-user preference mechanism (``routers/users.py``'s
    ``/user/settings/update``): read the user's ``settings`` JSON, merge the
    choice under the ``ui`` sub-tree — namespaced ``ui.audio.stt.model`` to match
    the platform's existing ``audio.stt.*`` config keys — and persist the whole
    settings blob via ``Users.update_user_by_id``. Only the STT model key is
    touched; every other personal setting is carried through untouched.
    """
    db_user = Users.get_user_by_id(user_id)
    settings = (
        db_user.settings.model_dump()
        if db_user is not None and db_user.settings is not None
        else {}
    )

    ui = settings.get("ui")
    ui = dict(ui) if isinstance(ui, dict) else {}
    audio = ui.get("audio")
    audio = dict(audio) if isinstance(audio, dict) else {}
    stt = audio.get("stt")
    stt = dict(stt) if isinstance(stt, dict) else {}

    stt["model"] = model_id
    audio["stt"] = stt
    ui["audio"] = audio
    settings["ui"] = ui

    Users.update_user_by_id(user_id, {"settings": settings})


@router.post("/models/selected")
async def select_transcribe_model(
    form_data: TranscribeModelSelectionForm,
    request: Request,
    user=Depends(get_verified_user),
):
    """Save the end user's personal transcribe-model choice (R4).

    Validates that ``model_id`` is one an admin has enabled (``_enabled_map`` id
    -> True) before persisting it. If the admin has disabled the model — or never
    enabled it — the save is rejected with 403 (R4 AC3): the end-user selection
    is bounded by admin curation, never bypassing it. Validation reads the
    admin-set enabled map from config, not the live backend, so a user can save a
    choice even when the STT backend is unreachable — the same "records intent by
    id" posture as the admin toggle.

    Authenticated end user (``get_verified_user``), not admin-gated: saving a
    personal preference is not a management action. On success the choice is
    persisted as a per-user setting (``ui.audio.stt.model``).
    """
    enabled_map = _enabled_map(request)
    if enabled_map.get(form_data.model_id) is not True:
        raise HTTPException(
            status_code=403,
            detail="That transcribe model is not enabled for selection",
        )

    _persist_user_stt_model(user.id, form_data.model_id)

    return {"model_id": form_data.model_id}


async def _send_post(
    url: str,
    payload: dict,
    key: Optional[str] = None,
    action_label: str = "swap",
):
    """POST a control-port command, returning parsed JSON or raising on failure.

    Unlike :func:`_send_get` (which degrades to None so a partial listing can
    still render), a mutating command must surface failure to the caller: an
    upstream error becomes a 502 and an unreachable backend a 502 as well, so a
    command never silently no-ops while reporting success. ``action_label`` names
    the operation in the rejection detail (e.g. "swap", "pull cancellation").
    """
    timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)
    url_ = url
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(
                url_,
                json=payload,
                headers={**({"Authorization": f"Bearer {key}"} if key else {})},
            ) as response:
                if response.status >= 400:
                    log.warning("transcribe control POST %s -> HTTP %s", url_, response.status)
                    raise HTTPException(
                        status_code=502,
                        detail=f"Self.AI UI: the self-hosted STT router backend rejected the {action_label}",
                    )
                try:
                    return await response.json()
                except Exception:
                    return {}
    except HTTPException:
        raise
    except Exception as e:
        log.warning("transcribe control POST %s failed: %s", url_, e)
        raise HTTPException(
            status_code=502,
            detail="Self.AI UI: could not reach the self-hosted STT router backend",
        )


@router.post("/models/{model_id}/activate")
async def activate_transcribe_model(
    request: Request, model_id: str, user=Depends(get_admin_user)
):
    """Make ``model_id`` the active model that serves transcription (R4).

    Changes which downloaded model serves requests without restarting the UI
    server or the STT backend process: the target is validated against the R1
    downloaded-and-ready listing (R4 AC3 — only a downloaded model may be made
    active), then a single control-port ``load`` command activates it (the STT
    analog of llamolotl's ``/models/load``). The backend, being a
    single-active-model router, drops the previously active model, so after this
    call transcription requests are served by ``model_id`` (R4 AC1/AC2).
    """
    control_url = get_transcribe_control_url(request)
    key = getattr(request.app.state.config, "STT_OPENAI_API_KEY", None) or None

    # Gate on the same R1 listing the GET route exposes: a swap may target only
    # a downloaded-and-ready model.
    downloaded_raw = await _send_get(f"{control_url}/api/models", key)
    catalog_raw = await _send_get(f"{control_url}/api/models/available", key)
    if downloaded_raw is None and catalog_raw is None:
        raise HTTPException(
            status_code=502,
            detail="Self.AI UI: could not reach the self-hosted STT router backend",
        )

    try:
        validate_swap_target(downloaded_raw, catalog_raw, model_id)
    except ModelNotDownloadedError as e:
        # 404: the requested active model is not among the downloadable-ready
        # set — a pullable-only or unknown model cannot be made active.
        raise HTTPException(status_code=404, detail=str(e))

    await _send_post(f"{control_url}/api/models/load", {"model": model_id}, key)

    return {"active": model_id, "status": "active"}


# ---------------------------------------------------------------------------
# R6: Auth-ready credential slot on the mutating operations (pull, cancel, delete)
# ---------------------------------------------------------------------------
#
# cavekit-audio-transcribe-router.md R6 (Auth-Ready, Not Auth-Blocked).
#
# The three mutating operations below ship now — reachable only from within the
# trusted network perimeter, never exposed on an untrusted external network — and
# do NOT block on the separate platform-wide authenticated-access effort. To keep
# that future gate a non-breaking addition, every mutating request defines an
# OPTIONAL credential slot in its request body: present in the contract but wholly
# unused and unchecked today, and never forwarded to the backend control command.
#
# ``MutatingActionForm`` is that slot for this router. It is the transcribe-router
# endpoint wiring of the same connections-domain contract T-009 proved on
# ``ManagementMutationRequest`` (both name the slot :data:`CREDENTIAL_SLOT`). The
# slot is inert: no request is authenticated by it (there is nothing to read it),
# so until the platform-wide gate lands these operations rely on network-perimeter
# protection (:data:`PERIMETER_POSTURE`, R6 AC1/AC2/AC4). The body is OPTIONAL so
# every existing caller that omits it works exactly as before (R6 AC1) — the slot
# only *adds* an accepted-but-unchecked field to the request's shape (R6 AC3).

# R6 AC3/AC4 carried in-code (not only in prose), reusing the connections-domain
# contract T-009 established so this router and the Connections surface name ONE
# identical slot and state ONE identical posture rather than drifting copies:
#   * MUTATING_CREDENTIAL_SLOT — the wire key of the optional credential slot on
#     every mutating request body (== :data:`CREDENTIAL_SLOT`); a future auth gate
#     reads this one key across both surfaces (R6 AC3).
#   * MUTATING_OPERATION_POSTURE — the plainly-stated network-perimeter posture
#     for these operations (== :data:`PERIMETER_POSTURE`), so an auditor reads it
#     from this module itself (R6 AC4).
MUTATING_CREDENTIAL_SLOT = CREDENTIAL_SLOT
MUTATING_OPERATION_POSTURE = PERIMETER_POSTURE


class MutatingActionForm(BaseModel):
    """Optional request body for a mutating transcribe-router action (R6).

    Carries the single auth-ready credential slot every mutating operation
    (pull, cancel, delete) defines. ``credential`` — whose wire key is
    :data:`CREDENTIAL_SLOT` — is OPTIONAL and today entirely
    accepted-but-unchecked: nothing reads or verifies it, and it is never
    forwarded to the backend control command (the ``build_*_payload`` helpers
    send only the model id/name). It exists solely so a future platform-wide auth
    gate can read a caller credential without changing the request's existing
    shape (R6 AC3). Until that gate lands these mutations rely on
    network-perimeter protection (:data:`PERIMETER_POSTURE`, R6 AC1/AC2/AC4).

    The whole body is optional (the endpoints default it to ``None``), so a
    caller that sends no body at all — every existing pull/cancel/delete caller —
    is unaffected (R6 AC1).
    """

    credential: Optional[str] = None


async def _stream_pull(control_url: str, model_id: str, key: Optional[str]) -> AsyncGenerator[bytes, None]:
    """Proxy the control port's pull stream, normalizing each progress chunk.

    Yields newline-delimited JSON (``application/x-ndjson``) where every line is
    a normalized pull event (see ``transcribe.pull``): many ``pulling`` events
    followed by exactly one terminal ``success`` or ``error``. The generator
    guarantees a terminal event is always emitted:

    * an upstream HTTP >= 400 or a mid-stream exception -> a terminal ``error``;
    * a stream that ends without any terminal event -> a synthesized ``error``
      (``incomplete_stream_event``), so a truncated pull is never mistaken for a
      completed one (R2 AC5).

    Only the backend's own ``/api/models`` view (read by the R1 listing) decides
    what is downloaded-and-ready; this stream never writes into that set, so a
    failed pull cannot leak a partial model as ready.
    """
    url = f"{control_url}/api/models/pull"
    payload = json.dumps(build_pull_payload(model_id))
    timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)
    saw_terminal = False
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(
                url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    **({"Authorization": f"Bearer {key}"} if key else {}),
                },
            ) as response:
                if response.status >= 400:
                    detail = f"pull rejected by backend (HTTP {response.status})"
                    try:
                        body = await response.json()
                        if isinstance(body, dict):
                            detail = body.get("detail") or body.get("error") or detail
                    except Exception:
                        pass
                    log.warning("transcribe pull %s -> HTTP %s", model_id, response.status)
                    yield (json.dumps(error_event(model_id, detail)) + "\n").encode("utf-8")
                    return

                async for raw_line in response.content:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        # A non-JSON progress line is malformed; surface it as an
                        # error event rather than dropping it silently.
                        event = error_event(model_id, "malformed pull progress from backend")
                    else:
                        event = normalize_pull_event(chunk, model_id=model_id)
                    if is_terminal(event):
                        saw_terminal = True
                    yield (json.dumps(event) + "\n").encode("utf-8")
    except Exception as e:
        log.warning("transcribe pull %s failed mid-stream: %s", model_id, e)
        yield (json.dumps(error_event(model_id, f"pull connection error: {e}")) + "\n").encode("utf-8")
        return

    if not saw_terminal:
        yield (json.dumps(incomplete_stream_event(model_id)) + "\n").encode("utf-8")


@router.post("/models/{model_id:path}/pull")
async def pull_transcribe_model(
    model_id: str,
    request: Request,
    form_data: Optional[MutatingActionForm] = Body(default=None),
    user=Depends(get_admin_user),
):
    """Pull ``model_id`` into the backend, streaming incremental progress.

    Returns an ``application/x-ndjson`` stream of normalized pull events (R2
    AC1/AC2): the model downloads without any redeploy/rebuild, and progress is
    reported as it happens rather than only at the end. The stream's final event
    is a terminal ``success`` or ``error`` (R2 AC4). On ``success`` the model
    appears in the R1 listing's downloaded-and-ready set (R2 AC3); on any
    failure the listing continues to reflect only what the backend actually
    holds, so no partial model is presented as ready (R2 AC5).

    Mutating operation, admin-gated and network-perimeter protected (R6). The
    request body carries the OPTIONAL auth-ready credential slot
    (``form_data.credential``, wire key :data:`CREDENTIAL_SLOT`): it is accepted
    but wholly unused and unchecked today, and never forwarded to the backend
    pull command — the pull runs identically whether or not a credential is
    supplied (R6 AC3). The body itself is optional, so a caller that sends no
    body pulls exactly as before (R6 AC1). See :data:`PERIMETER_POSTURE`.
    """
    control_url = get_transcribe_control_url(request)
    key = getattr(request.app.state.config, "STT_OPENAI_API_KEY", None) or None

    return StreamingResponse(
        _stream_pull(control_url, model_id, key),
        media_type="application/x-ndjson",
    )


@router.post("/models/{model_id:path}/cancel")
async def cancel_transcribe_pull(
    model_id: str,
    request: Request,
    form_data: Optional[MutatingActionForm] = Body(default=None),
    user=Depends(get_admin_user),
):
    """Cancel an in-flight pull of ``model_id`` before it completes (R3).

    Sends a single control-port ``pull/cancel`` command (the STT analog of
    self.llamolotl's ``/api/models/pull/cancel``); the backend stops the download
    on its end, which terminates the still-open ``/pull`` stream — ideally with a
    terminal ``cancelled`` event (R3 AC1). Because the download never completes,
    the cancelled model is never added to the backend's ``/api/models`` view, so
    the R1 listing continues to report it as merely pullable, not
    downloaded-and-ready (R3 AC2) — the same no-optimistic-write-through property
    the pull relies on.

    The endpoint is stateless on the UI-server side: it records no per-pull
    cancel flag, so a subsequent NEW pull of the same ``model_id`` is unaffected
    and succeeds normally (R3 AC3). Cancelling when no pull is in flight is a
    backend no-op; an upstream rejection surfaces as 502 rather than silently
    reporting success.

    Mutating operation, admin-gated and network-perimeter protected (R6). The
    request body carries the OPTIONAL auth-ready credential slot
    (``form_data.credential``, wire key :data:`CREDENTIAL_SLOT`): accepted but
    unused and unchecked today, and never forwarded to the backend cancel
    command — cancellation behaves identically with or without it (R6 AC3). The
    body is optional, so a caller that sends none cancels exactly as before
    (R6 AC1). See :data:`PERIMETER_POSTURE`.
    """
    control_url = get_transcribe_control_url(request)
    key = getattr(request.app.state.config, "STT_OPENAI_API_KEY", None) or None

    await _send_post(
        f"{control_url}/api/models/pull/cancel",
        build_cancel_payload(model_id),
        key,
        action_label="pull cancellation",
    )

    return {"id": model_id, "status": "cancelled"}


# ---------------------------------------------------------------------------
# R5: Model deletion under the active-model invariant (DELETE /models/{id})
# ---------------------------------------------------------------------------
#
# Kept as a single self-contained endpoint (its own control-port command and
# invariant gate) so it merges cleanly alongside concurrent work on the listing
# and the active-model indicator.


@router.delete("/models/{model_id:path}")
async def delete_transcribe_model(
    model_id: str,
    request: Request,
    form_data: Optional[MutatingActionForm] = Body(default=None),
    user=Depends(get_admin_user),
):
    """Delete a downloaded transcribe model, reclaiming its storage (R5).

    Gated on the active-model invariant (R7 AC2 / R5 AC4): the currently-active
    model may NOT be deleted while it remains active — it must first be swapped
    (R4) to another downloaded model. The endpoint resolves the same R1 listing
    the GET route exposes and calls ``assert_deletable`` BEFORE issuing any
    backend delete; an attempt to delete the active model raises
    :class:`ModelIsActiveError`, surfaced here as HTTP 409 so no delete command
    ever reaches the backend for the active model.

    A non-active downloaded model is removed by a single control-port ``delete``
    command (the STT analog of self.llamolotl's ``/api/models/delete``). The
    backend's own ``/api/models`` view is the sole source of the
    downloaded-and-ready set, so once the backend drops the model it simply falls
    out of the R1 listing — it no longer appears downloaded-and-ready but may
    remain listed as pullable if it is a known catalog entry (R5 AC2). Removing
    one model touches only that model's files, leaving every other downloaded
    model's availability intact (R5 AC3).

    Mutating operation, admin-gated and network-perimeter protected (R6). The
    request body carries the OPTIONAL auth-ready credential slot
    (``form_data.credential``, wire key :data:`CREDENTIAL_SLOT`): accepted but
    unused and unchecked today, and never forwarded to the backend delete
    command — deletion (and the active-model gate) behaves identically with or
    without it (R6 AC3). The body is optional, so a caller that sends none — the
    existing ``DELETE /models/{id}`` with no body — deletes exactly as before
    (R6 AC1). See :data:`PERIMETER_POSTURE`.
    """
    control_url = get_transcribe_control_url(request)
    key = getattr(request.app.state.config, "STT_OPENAI_API_KEY", None) or None

    # Resolve the same R1 listing the GET route exposes, so the invariant gate
    # sees exactly the backend's downloaded-and-ready set and its active model.
    downloaded_raw = await _send_get(f"{control_url}/api/models", key)
    catalog_raw = await _send_get(f"{control_url}/api/models/available", key)
    if downloaded_raw is None and catalog_raw is None:
        raise HTTPException(
            status_code=502,
            detail="Self.AI UI: could not reach the self-hosted STT router backend",
        )

    listing = build_model_listing(downloaded_raw, catalog_raw)

    # R7 AC2 gate: refuse to delete the currently-active model while active.
    try:
        assert_deletable(listing, model_id)
    except ModelIsActiveError as e:
        # 409 Conflict: the request conflicts with the active-model invariant —
        # the model must be swapped away from active before it can be deleted.
        raise HTTPException(status_code=409, detail=str(e))

    await _send_post(
        f"{control_url}/api/models/delete",
        build_delete_payload(model_id),
        key,
        action_label="model deletion",
    )

    return {"id": model_id, "status": "deleted"}

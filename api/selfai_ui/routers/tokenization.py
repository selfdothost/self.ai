"""Tokenization Studio: re-score one position — self.ai#134, T-304 (LR/R3).

THE DEFAULT READ PATH FOR A CLICK. Streaming alternatives for every token is
retained only for the whole-reply heatmap: the Phase 1 spike measured 968 bytes
per token at `top_logprobs=10`, of which alternatives are 92% of the payload.
The distribution at one position is deterministic given the prefix, and
llama.cpp's prompt cache makes the re-decode cheap — so asking again when the
artist actually clicks is both smaller and fresher than carrying everything.

WHAT THIS DELIBERATELY DOES NOT DO: mutate anything (R3-AC6). No chat, no
session, no message edit record. It reads a distribution and returns it. That
is worth stating in the code because the obvious neighbouring feature — branch
from this token — DOES write, and lives behind R4/Phase 4.

A separate router rather than routes on `main.py`'s chat entry: this is a read
of a model, not a conversation turn, and nothing it returns is persisted.
"""

import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from selfai_ui.constants import ERROR_MESSAGES
from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.models.models import Models
from selfai_ui.routers.llamolotl import get_api_key, send_get_request
from selfai_ui.utils.access_control import has_permission
from selfai_ui.utils.auth import get_verified_user
from selfai_ui.utils.chat import generate_chat_completion
from selfai_ui.utils.models import get_all_models
from selfai_ui.utils.payload import apply_model_params_to_body_openai
from selfai_ui.utils.tokenization import (
    DEFAULT_POST_SAMPLING,
    MAX_TOP_LOGPROBS,
    SAMPLING_FIELD_NAMES,
    TokenizationParamError,
    TokenizationPrefixTooLong,
    TokenizationUpstreamError,
    assert_expected_model,
    assert_model_can_tokenize,
    assert_prefix_fits,
    build_identity,
    classify_upstream_error,
    extract_position_distribution,
    inspect_residency,
    resolve_rescore_sampling,
    served_model_of,
)

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])

router = APIRouter()

#: The permission admitting an artist to the Tokenization Studio.
#:
#: Named once rather than spelled at each call site: the key is HIERARCHICAL and
#: `has_permission` denies on any missing level, so a typo anywhere in the
#: dotted path fails closed and looks exactly like a user who was not granted
#: it -- no exception, no log line, just a locked door. Declared in `config.py`
#: (Phase 2) so `get_permissions` publishes it to the client too; enforcing a
#: key the default blob does not declare would gate the API on something the
#: navigation can never show.
TOKENIZATION_PERMISSION = "studio.tokenization"

#: How long the weights lookup may take before the answer is "unknown".
#:
#: Small on purpose. This is a LABEL on a distribution that has already been
#: computed; the re-score is correct with or without it, so a slow or absent
#: llamolotl must degrade the label rather than the answer.
IDENTITY_LOOKUP_TIMEOUT_SECONDS = 2.0


def require_tokenization_access(request: Request, user) -> None:
    """T-305 (R3-AC3), the deferred X-2 of the Studio shell build site.

    Mirrors `llamolotl.require_training_access` deliberately -- same order,
    same status, same error -- because a Studio permission behaving differently
    from its neighbours is a bug waiting to be written by whoever reads one and
    assumes the other.
    """
    if user.role == "admin":
        return

    if has_permission(
        user.id, TOKENIZATION_PERMISSION, request.app.state.config.USER_PERMISSIONS
    ):
        return

    raise HTTPException(status_code=401, detail=ERROR_MESSAGES.UNAUTHORIZED)


class RescoreForm(BaseModel):
    """One position of one reply.

    `prefix` is authoritative, `position` is not. The prefix is what actually
    determines the distribution; `position` is the client's index for the token
    it clicked, carried so the answer can be correlated with the view that
    asked. Deriving the prefix from `position` would need a tokenizer on this
    side of the wire, which there is not one of.
    """

    model: str
    messages: list[dict[str, Any]] = Field(default_factory=list)
    prefix: str = ""
    position: int = Field(default=0, ge=0)
    top_logprobs: int = Field(default=5, ge=0, le=MAX_TOP_LOGPROBS)
    post_sampling_probs: bool = DEFAULT_POST_SAMPLING
    sampling: Optional[dict[str, Any]] = None
    #: The model that produced the reply these ids came from, if the client
    #: knows it. Optional because an artist may re-score a fresh prefix that no
    #: stored reply corresponds to -- but when supplied it is authoritative and
    #: a disagreement is refused rather than answered (R5-AC3).
    expected_model: Optional[str] = None
    #: T-306 (R3-AC4). Opt IN to triggering a model load, defaulting to off.
    #:
    #: Off by default because the artist has not been told yet — the first
    #: request for a non-resident model is answered with what it would cost,
    #: and only a caller that has seen that answer sends this. A default of
    #: True would restore exactly the silent wait this task removes.
    #:
    #: It is not a "wait" flag and it does not poll: it drops the guard and
    #: dispatches, after which llamolotl autoloads on the chat request as it
    #: always has, subject to the window and the broker.
    allow_load: bool = False


async def _served_status(request: Request, model_id: str) -> Optional[dict[str, Any]]:
    """Read llamolotl's per-model status — the effective preset and the child's argv.

    BEST EFFORT, and deliberately so. This is the only place the served WEIGHTS
    are named: `app.state.MODELS` publishes `status` as a bare string, and the
    router's own `/model-status` reduces it to the same string, so neither can
    answer "which GGUF". The raw list can.

    A failure here must not fail the re-score — the distribution is still
    correct and useful — but it must not be silent either, which is why
    `build_identity` reports `weights_known` rather than just a null.
    """
    try:
        urls = request.app.state.config.LLAMOLOTL_BASE_URLS or []
        if not urls:
            return None
        key = get_api_key(urls[0], request.app.state.config.LLAMOLOTL_API_CONFIGS)
        # HARD BUDGET. Identity is worth a round trip but never worth stalling a
        # re-score for: `send_get_request` carries the model-list timeout, which
        # is measured in seconds, and an unreachable llamolotl would otherwise
        # add all of it to every SUCCESSFUL re-score. Measured before this bound
        # existed: ~6.15s on every 200. Timing out here costs the weights line
        # and nothing else, and `weights_known: false` says so.
        result = await asyncio.wait_for(
            send_get_request(f"{urls[0]}/v1/models", key),
            timeout=IDENTITY_LOOKUP_TIMEOUT_SECONDS,
        )
        for entry in (result or {}).get("data", []):
            if entry.get("id") == model_id:
                status_obj = entry.get("status")
                return status_obj if isinstance(status_obj, dict) else None
    except asyncio.TimeoutError:
        log.debug("llamolotl status lookup for %s exceeded its budget", model_id)
    except Exception:
        log.debug("could not read llamolotl status for %s", model_id, exc_info=True)
    return None


@router.post("/rescore")
async def rescore_position(
    request: Request,
    form_data: RescoreForm,
    user=Depends(get_verified_user),
):
    # FIRST, before the model lookup. Ordering is the whole point: a 404 for an
    # unknown model raised ahead of the gate would let an unauthorised caller
    # enumerate which models exist by reading 404 against 401.
    require_tokenization_access(request, user)

    if not request.app.state.MODELS:
        await get_all_models(request)

    model = request.app.state.MODELS.get(form_data.model)
    if model is None:
        raise HTTPException(status_code=404, detail=f"model {form_data.model!r} not found")

    # Same refusal as the streaming path (T-302): a model that cannot produce
    # per-token probabilities must say so rather than answer without them.
    try:
        # T-307 (R5-AC3), checked BEFORE dispatch: if the caller says these ids
        # came from another model, asking this one for more of them is incoherent
        # whatever the upstream would have replied — and refusing here costs no
        # generation. 400 rather than the 409 below because this is the REQUEST
        # disagreeing with itself, not the server serving something unexpected.
        assert_expected_model(form_data.model, form_data.expected_model)
        assert_model_can_tokenize(model)
        assert_prefix_fits(model, form_data.prefix)
    except TokenizationPrefixTooLong as e:
        # 413 rather than 400: the request is well-formed, it is too large.
        # R3-AC8 wants this distinguishable from an ordinary bad parameter.
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(e))
    except TokenizationParamError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # T-306 (R3-AC4). llamolotl serves ONE active model at a time, so a re-score
    # against an older session's model is a SWAP, not an addition — and that swap
    # is subject to GPU-window enforcement and the VRAM broker, either of which
    # can make it legitimately slow or refuse it outright. That is exactly why
    # the cost must be SAID rather than left to be inferred from a spinner.
    #
    # 409 CONFLICT, chosen over the alternatives on the grounds that a caller has
    # to be able to tell this apart from every other outcome this endpoint has:
    #
    #   * not 200 — a success with an empty body is how R3-AC7 got written;
    #   * not 401 — that is the permission gate above, and a refusal to serve is
    #     not a refusal to admit;
    #   * not 502 — nothing upstream failed. Nothing upstream was even asked;
    #   * not 503, which is the LEASE refusal's code and carries `gpu_locked_by`.
    #     Reusing it would put two different remedies (wait for a window to end
    #     vs. ask for a load) behind one status, and a client that maps 503 onto
    #     "the GPU is busy, retry later" would silently do the wrong thing;
    #   * not 425 Too Early — that is about replay safety, not resource state.
    #
    # 409 is "the request conflicts with the current state of the target
    # resource", which is literally the case: a different model holds the card.
    # The detail body is a dict, mirroring the lease refusal's shape, so the
    # reason is machine-readable rather than a sentence to regex.
    residency = inspect_residency(request.app.state.MODELS, form_data.model)
    if residency.resident is False and not form_data.allow_load:
        # `is False`, NOT falsy: an UNKNOWN status (None) must proceed. Refusing
        # on a signal we do not have would break every caller whose model list
        # has not been fetched yet, and it is the mistake utils/vram_admission.py
        # documents at length. Only a positive observation refuses.
        log.info(
            "re-score: %r is %r, not resident — reporting load-required (loaded=%r)",
            form_data.model,
            residency.status,
            residency.loaded_model,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "detail": (
                    f"model {form_data.model!r} is not loaded on the GPU"
                    + (
                        f" ({residency.loaded_model!r} is)"
                        if residency.loaded_model
                        else ""
                    )
                    + ". self.llamolotl serves one model at a time, so this "
                    "re-score requires a load, which is subject to the GPU "
                    "window and the VRAM broker and may take a while or be "
                    "refused. Re-send with allow_load=true to start it."
                ),
                "load_required": True,
                "model": form_data.model,
                # Verbatim, so 'a load is already underway' ("loading") is
                # distinguishable from 'nothing has started' ("unloaded").
                "model_status": residency.status,
                "loaded_model": residency.loaded_model,
                "retry_with": {"allow_load": True},
            },
        )

    sampling = resolve_rescore_sampling(form_data.sampling)

    messages = list(form_data.messages)
    if form_data.prefix:
        # Continue the assistant turn rather than starting a new one, so the
        # distribution is the one at the clicked position and not the one at
        # the start of a fresh reply.
        messages = messages + [{"role": "assistant", "content": form_data.prefix}]

    payload: dict[str, Any] = {
        "model": form_data.model,
        "messages": messages,
        "stream": False,
        "max_tokens": 1,
        "logprobs": True,
        "top_logprobs": form_data.top_logprobs,
        "post_sampling_probs": form_data.post_sampling_probs,
        "continue_final_message": bool(form_data.prefix),
        "add_generation_prompt": not form_data.prefix,
        **sampling,
    }

    # Apply the model's configured params HERE, before dispatch, so what is
    # echoed is what was actually used. `apply_model_params_to_body_openai`
    # OVERWRITES (`form_data[key] = value`), and the llamolotl router applies
    # it again downstream — so echoing the caller's requested values would
    # print numbers the model never saw whenever a model has params configured.
    # AC5 asks for an echo that reproduces what the artist saw; an echo that
    # can silently disagree with the generation is worse than none.
    model_info = Models.get_model_by_id(form_data.model)
    if model_info:
        payload = apply_model_params_to_body_openai(model_info.params.model_dump(), payload)
    effective_sampling = {k: payload[k] for k in sampling if k in payload}

    try:
        response = await generate_chat_completion(request, payload, user)
    except HTTPException:
        # Do NOT flatten this. A refusal here is usually the lease admission
        # checkpoint declining a local generation with a machine-readable body
        # (an active GPU window, for instance), and that reason is the entire
        # value of the response. See self.ai#138 for the same mistake made in
        # the task router.
        raise
    except Exception as e:
        log.exception("re-score failed upstream")
        err = classify_upstream_error(e)
        if isinstance(err, TokenizationPrefixTooLong):
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(err)
            )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(err))

    # T-307 (R5-AC3). WHICH MODEL ACTUALLY ANSWERED — not the same question as
    # which one was asked for. During an eval window the lease checkpoint serves
    # the model already resident INSTEAD of the requested one (self.ai#35), and
    # for ordinary chat that is a reasonable trade announced in the body. For a
    # tokenization re-score it is silent corruption: the ids come back
    # well-formed, in the right shape, at the right position, and mean something
    # entirely different because they belong to another vocabulary. Nothing
    # errors. Nobody notices until a bake replays them against the HF tokenizer.
    #
    # 409 rather than 200-with-a-warning: a marked-but-returned distribution
    # would still be rendered, and an artist reweighting a token they cannot
    # trust is worse than an artist told to try again.
    served = served_model_of(response, form_data.model)
    if served != form_data.model:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "model_mismatch": True,
                "requested": form_data.model,
                "served": served,
                "substitution": (response or {}).get("model_substitution"),
                "detail": (
                    f"asked {form_data.model!r} but {served!r} answered, so these "
                    f"token ids belong to a different tokenizer and cannot be "
                    f"read as {form_data.model!r}'s. This usually means an eval "
                    f"window substituted the resident model; re-score once the "
                    f"window ends."
                ),
            },
        )

    try:
        chosen, alternatives = extract_position_distribution(
            response, form_data.post_sampling_probs
        )
    except TokenizationUpstreamError as e:
        # R3-AC7. An upstream that answered without a distribution is NOT an
        # empty distribution, and a 200 with `candidates: []` is exactly how
        # those two become indistinguishable.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))

    # T-307 (R5-AC1/AC2). The ids above are only interpretable against the
    # tokenizer that produced them, so the response says whose they are. The
    # display id is not enough on its own — it is a preset section name and can
    # be renamed — so this carries the GGUF the weights were loaded from.
    identity = build_identity(
        served,
        await _served_status(request, served),
        context_length=model.get("context_length"),
    )

    return {
        "model": form_data.model,
        "identity": identity,
        "position": form_data.position,
        # Which pair of field names the values below use, sent rather than
        # inferred: reading the wrong one returns an empty list and no error.
        "post_sampling_probs": form_data.post_sampling_probs,
        "fields": SAMPLING_FIELD_NAMES[form_data.post_sampling_probs],
        "top_logprobs": form_data.top_logprobs,
        "sampling": effective_sampling,
        "chosen": chosen,
        "candidates": alternatives,
    }

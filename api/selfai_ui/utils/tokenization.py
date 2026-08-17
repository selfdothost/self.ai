"""Tokenization Studio request parameters: bound them, and say what was used.

T-301 of `context/plans/build-site-tokenization-logprobs.md` (LR/R1).

`logprobs` and `top_logprobs` already reach llamolotl untouched -- the chat
entry point takes a raw `form_data: dict` with no Pydantic model stripping
unknown fields, and the OpenAI forwarding path runs no allowlist. So the work
here is NOT plumbing. It is a bound and an echo.

WHY A BOUND. llama.cpp clamps `n_probs` to the vocabulary size
(`server-context.cpp`: `std::min(max_probs, n_probs_request)`), which is a
SILENT success: ask for 10_000 and you get a truncated set back with no
indication anything was refused. That is the same class of failure this whole
surface exists to eliminate, so an absurd value fails fast here with a reason
instead of succeeding differently upstream.

WHY AN ECHO. The view has to label what it is displaying rather than assume it.
That is not politeness -- see `SAMPLING_FIELD_NAMES` below.
"""

import logging
from dataclasses import dataclass
from typing import Any, Optional

from selfai_ui.utils.model_context import CONTEXT_LENGTH_FIELD

log = logging.getLogger(__name__)

#: The largest `top_logprobs` this API will accept.
#:
#: Bounded on PAYLOAD, not on what llama.cpp can produce. The Phase 1 spike
#: measured 968 bytes per token at `top_logprobs=10` -- about 945 KiB per 1000
#: tokens, of which alternatives are 92%. Twenty is roughly 2 MiB per 1000
#: tokens: generous for a deliberate investigation, and still an amount a
#: browser can hold. Past that the honest answer is to re-score a position on
#: demand, which is what the re-score endpoint is for.
MAX_TOP_LOGPROBS = 20

#: Which field names the upstream uses for each sampling mode.
#:
#: THIS IS THE TRAP THIS MODULE EXISTS TO MAKE VISIBLE. llama.cpp renames the
#: fields depending on `post_sampling_probs`: pre-sampling emits
#: `logprob`/`top_logprobs`, post-sampling emits `prob`/`top_probs`. A client
#: reading only one pair sees ZERO alternatives in the other mode, with no error
#: anywhere -- an empty list that looks exactly like "the model was certain".
#:
#: Echoing the mode is what lets the client read the right pair instead of
#: guessing, and lets the view say which it is showing.
SAMPLING_FIELD_NAMES = {
    False: {"token": "logprob", "alternatives": "top_logprobs"},
    True: {"token": "prob", "alternatives": "top_probs"},
}

#: Pre-sampling by default, REVERSING this kit's original recommendation.
#:
#: Measured live against gemma-4-26B at temp 0.8 / top_p 0.9: post-sampling
#: returned 2 of 5 requested candidates, because `top_p` had already truncated
#: the set and said nothing about it. Pre-sampling returned all 5, including the
#: ones the sampler excluded.
#:
#: Post-sampling answers "why did it say that", which is the right question for
#: a debugger. This is a tuning studio: an artist steering the model toward a
#: token it would not normally pick needs to see precisely the candidates the
#: sampler HIDES, because those are the ones worth reweighting.
DEFAULT_POST_SAMPLING = False


class TokenizationParamError(ValueError):
    """A tokenization request asked for something this API will not do."""


def resolve_logprobs_request(form_data: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Validate the logprobs parameters on `form_data`, and describe them.

    Returns None when the request did not ask for logprobs at all -- which is
    every ordinary chat request, and the case that must stay byte-for-byte
    unchanged. Callers therefore get an explicit "not a tokenization request"
    answer rather than an empty dict they might act on.

    Raises TokenizationParamError with a reason the client can show. Never
    silently corrects a value: a request that asked for something impossible
    should be told, not quietly given something else.
    """
    if not form_data.get("logprobs"):
        return None

    raw = form_data.get("top_logprobs", 0)

    # Reject before coercing. bool is an int in Python, and `True` arriving here
    # would otherwise become a perfectly plausible `top_logprobs=1`.
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TokenizationParamError(
            f"top_logprobs must be an integer between 0 and {MAX_TOP_LOGPROBS}, got {raw!r}"
        )
    if raw < 0 or raw > MAX_TOP_LOGPROBS:
        raise TokenizationParamError(
            f"top_logprobs must be between 0 and {MAX_TOP_LOGPROBS}, got {raw}. "
            f"Higher values are served by re-scoring a single position on demand "
            f"rather than streaming alternatives for every token."
        )

    post_sampling = form_data.get("post_sampling_probs", DEFAULT_POST_SAMPLING)
    if not isinstance(post_sampling, bool):
        raise TokenizationParamError(
            f"post_sampling_probs must be a boolean, got {post_sampling!r}"
        )

    return {
        "logprobs": True,
        "top_logprobs": raw,
        "post_sampling_probs": post_sampling,
        # The field names the client should read for this mode. Sent rather than
        # left to be inferred, because inferring it wrong produces an empty list
        # instead of an error.
        "fields": SAMPLING_FIELD_NAMES[post_sampling],
    }

#: The provider whose models can serve a tokenization session.
#:
#: `owned_by` is a real discriminator rather than an inference: the llamolotl
#: router stamps it (`routers/llamolotl.py:557`) and the client filters its
#: picker on the same value. Only llamolotl reaches a llama.cpp server, and
#: `n_probs` -- what `logprobs` maps onto -- is a llama.cpp parameter. An
#: OpenAI-compatible connection may accept the field and answer without it.
TOKENIZATION_PROVIDER = "llamolotl"


def assert_model_can_tokenize(model: dict[str, Any]) -> None:
    """Refuse a tokenization request against a model that cannot serve one.

    REFUSE, DO NOT DOWNGRADE. Silently answering without distributions is the
    dangerous outcome: the artist gets a perfectly ordinary-looking reply, the
    token view renders nothing, and the reasonable conclusion is that the
    feature is broken rather than that the model is unsupported. A refusal names
    the model and the reason, so the next action is obvious.

    The client already filters its picker on the same field, so this is defence
    in depth -- for direct API use, and for a model whose connection type
    changed underneath a session that was already open.
    """
    owned_by = (model or {}).get("owned_by")
    if owned_by != TOKENIZATION_PROVIDER:
        raise TokenizationParamError(
            f"model {(model or {}).get('id', '?')!r} is served by "
            f"{owned_by or 'an unknown provider'}, which cannot return per-token "
            f"probabilities. Tokenization sessions require a "
            f"{TOKENIZATION_PROVIDER}-backed model."
        )


# --------------------------------------------------------------------------
# T-304 (LR/R3): single-position re-score.
# --------------------------------------------------------------------------


class TokenizationPrefixTooLong(TokenizationParamError):
    """The prefix cannot fit the model's context. R3-AC8 wants this SPECIFIC,
    not folded into the generic parameter error, because the remedy differs:
    a bad parameter is fixed by asking differently, an over-long prefix is
    fixed by re-scoring a shorter span."""


class TokenizationUpstreamError(RuntimeError):
    """The upstream failed, as opposed to answering with nothing.

    R3-AC7 exists because these two are trivially confused: a re-score that
    returns no candidates and a re-score that never reached the model both
    render as an empty list. One means "the model was certain", the other
    means "we do not know". They must not arrive looking alike.
    """


#: Sampling parameters a re-score accepts and echoes (R3-AC5).
#:
#: Deliberately a fixed set rather than "whatever the client sent": these are
#: the ones that change the RETURNED DISTRIBUTION, which is the whole point of
#: echoing them. `max_tokens` is absent on purpose -- a re-score reads one
#: position, so letting a caller set it would only let them pay for tokens
#: nobody reads.
RESCORE_SAMPLING_PARAMS = (
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "typical_p",
    "repeat_penalty",
    "presence_penalty",
    "frequency_penalty",
    "seed",
)

#: A deliberately generous upper bound on how many characters one token can
#: decode to, used ONLY to prove a prefix is certainly too long.
#:
#: There is no tokenizer on this side of the wire -- llamolotl exposes no
#: `/tokenize` -- so an exact count is not available without another round
#: trip. Rather than estimate (an estimate that is wrong low truncates
#: silently, which is exactly what R3-AC8 forbids), this bounds the count from
#: BELOW: no token decodes to more than this many characters, so
#: `len(text) / MAX_CHARS_PER_TOKEN` can only ever UNDERSTATE the token count.
#: A refusal from it is therefore always correct; it simply will not catch
#: every over-long prefix. The authoritative check is the model's own
#: tokenizer upstream, whose overflow is mapped onto the same specific error --
#: see `classify_upstream_error`.
MAX_CHARS_PER_TOKEN = 16


def assert_prefix_fits(model: dict[str, Any], text: str) -> None:
    """Refuse a prefix that certainly exceeds the model's context (R3-AC8).

    Silence is the failure mode being designed against: llama.cpp will happily
    shift context and answer, and the artist would be shown a distribution for
    a position computed from a DIFFERENT prefix than the one they clicked --
    plausible, wrong, and completely unremarkable-looking.
    """
    context_length = (model or {}).get(CONTEXT_LENGTH_FIELD)
    if not isinstance(context_length, int) or context_length <= 0:
        # Unknown context is not the same as infinite context, but refusing on
        # an unknown would make the endpoint unusable against any model that
        # does not publish one. Defer to the upstream tokenizer instead.
        return

    lower_bound = len(text or "") // MAX_CHARS_PER_TOKEN
    if lower_bound > context_length:
        raise TokenizationPrefixTooLong(
            f"the prefix is at least {lower_bound} tokens, which exceeds the "
            f"{context_length}-token context of "
            f"{(model or {}).get('id', 'this model')!r}. Re-score a position "
            f"within a shorter span rather than sending the whole reply."
        )


#: Upstream phrasings that mean "the prompt did not fit", mapped to R3-AC8.
_CONTEXT_OVERFLOW_MARKERS = (
    "exceed the available context",
    "exceeds the available context",
    "exceeds the context",
    "context size exceeded",
    "prompt is too long",
    "too many tokens",
)


def classify_upstream_error(detail: Any) -> TokenizationParamError:
    """Turn an upstream failure into the most specific error we can justify.

    A context overflow reported by the model's real tokenizer is the SAME fact
    `assert_prefix_fits` guards, so it gets the same type -- otherwise whether
    the artist sees "prefix too long" or "upstream failed" would depend on
    which side happened to notice, which is not a distinction they can act on.
    """
    text = str(detail or "").lower()
    if any(marker in text for marker in _CONTEXT_OVERFLOW_MARKERS):
        return TokenizationPrefixTooLong(
            f"the model refused the prefix as too long for its context: {detail}"
        )
    return TokenizationUpstreamError(str(detail or "the model did not answer"))


# --------------------------------------------------------------------------
# T-306 (LR/R3-AC4): residency, reported rather than waited on.
# --------------------------------------------------------------------------

#: The one llama-server status value that means "this model can answer now".
#:
#: The router publishes `loaded` / `loading` / `unloaded`
#: (`routers/llamolotl.py`, from llama-server's `status.value`, defaulting to
#: `unloaded`), and `utils/models.py` copies it onto every llamolotl entry of
#: `app.state.MODELS`. `loading` is deliberately NOT resident: a load is
#: underway, so the request would still wait for it — which is the thing
#: R3-AC4 exists to make visible rather than silent.
LLAMOLOTL_RESIDENT_STATUS = "loaded"


@dataclass(frozen=True)
class ResidencyReport:
    """What the cached model list says about whether a model is on the card.

    ``resident`` is deliberately THREE-VALUED, and the third value is the
    important one:

      * ``True``  — observed loaded; dispatch costs nothing extra.
      * ``False`` — observed NOT loaded; answering requires a load, and a load
        on this yard is a *swap* subject to GPU-window enforcement and the VRAM
        broker, so it can legitimately take a long time or be refused outright.
      * ``None``  — the list published no status for this model. UNKNOWN IS NOT
        "NOT RESIDENT". Refusing on a number we do not have is the mistake
        `utils/vram_admission.py` documents at length ("Unknown never blocks");
        it is also the difference between a guard and an outage for any caller
        whose model list has not been fetched yet.

    ``loaded_model`` is whichever llamolotl model the SAME cached list reports
    as loaded, so a caller can say "gem8y holds the card" rather than only "not
    yours". It is read off the list already in hand — no round trip — and it is
    the first such entry: llamolotl serves one active model at a time, which is
    the whole reason this report exists.
    """

    resident: Optional[bool]
    status: Optional[str]
    loaded_model: Optional[str]


def inspect_residency(
    models_map: Optional[dict[str, Any]], model_id: str
) -> ResidencyReport:
    """Read residency out of the model list core already keeps.

    NO NETWORK, and the staleness that buys is worth stating exactly rather
    than glossing. `app.state.MODELS` is rewritten only when something calls
    `utils/models.py`'s `get_all_models` — `/api/models`, or one of the
    `if not ...MODELS` cold-start guards. There is NO background refresh. The
    3s cache people associate with this list is on the llamolotl leg *inside*
    that call, so it bounds how stale a refresh's own data is, not how long ago
    the last refresh was. A long-lived process whose clients have not asked for
    the model list can therefore be reading a residency observation of
    arbitrary age.

    That is acceptable here, but only because the answer is a REPORT and not a
    lock, and only because the verdict is three-valued in the direction it is:
    a stale read errs toward telling the artist a load is required when one has
    since finished, which costs a retry against the endpoint that then answers
    normally. It never silently blocks a request that would have worked — an
    unknown proceeds. Weigh that against the failure it replaces: a spinner
    that never ends and never says why.

    If a caller needs the residency answer to be fresh, the honest fix is a
    round trip to llamolotl's `/v1/models` (the router's `/model-status` route
    does exactly that, bypassing the cache), not a tighter guess here.
    """
    entry = (models_map or {}).get(model_id) or {}
    status = entry.get("status")

    if not isinstance(status, str) or not status:
        resident = None
    else:
        resident = status == LLAMOLOTL_RESIDENT_STATUS

    loaded_model = None
    for other_id, other in (models_map or {}).items():
        other = other or {}
        if other_id == model_id:
            continue
        if (
            other.get("owned_by") == TOKENIZATION_PROVIDER
            and other.get("status") == LLAMOLOTL_RESIDENT_STATUS
        ):
            loaded_model = other_id
            break

    return ResidencyReport(resident=resident, status=status, loaded_model=loaded_model)


def resolve_rescore_sampling(requested: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Take only the sampling parameters that change the distribution."""
    requested = requested or {}
    return {k: requested[k] for k in RESCORE_SAMPLING_PARAMS if requested.get(k) is not None}


def extract_position_distribution(
    response: Any, post_sampling: bool
) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
    """Pull the chosen token and its alternatives out of an upstream response.

    Returns `(chosen, alternatives)`. Reads the field pair that matches the
    mode -- reading the wrong one yields an empty list and no error, which is
    the trap `SAMPLING_FIELD_NAMES` exists to close.

    Raises TokenizationUpstreamError when the response has no logprobs
    structure at all, because that is "we do not know" rather than "no
    alternatives" (R3-AC7).
    """
    names = SAMPLING_FIELD_NAMES[bool(post_sampling)]

    choices = (response or {}).get("choices") or []
    if not choices:
        raise TokenizationUpstreamError(
            "the model returned no choices, so no distribution could be read"
        )

    logprobs = (choices[0] or {}).get("logprobs")
    if not logprobs:
        raise TokenizationUpstreamError(
            "the model returned no logprobs. The request asked for them, so "
            "this is an upstream failure rather than an empty distribution."
        )

    content = logprobs.get("content") or []
    if not content:
        raise TokenizationUpstreamError(
            "the model returned an empty logprobs.content, so no position "
            "could be read"
        )

    entry = content[0] or {}
    chosen = {
        "token": entry.get("token"),
        "id": entry.get("id"),
        names["token"]: entry.get(names["token"]),
    }
    alternatives = [
        {
            "token": alt.get("token"),
            "id": alt.get("id"),
            names["token"]: alt.get(names["token"]),
        }
        for alt in (entry.get(names["alternatives"]) or [])
    ]
    return chosen, alternatives


# --------------------------------------------------------------------------
# T-307 (LR/R5): whose tokenizer do these ids belong to?
# --------------------------------------------------------------------------


class TokenizationModelMismatch(TokenizationParamError):
    """The distribution does not belong to the model the caller thinks it does.

    THE FAILURE THIS PREVENTS IS SILENT AND TOTAL. A token id is only meaningful
    against the tokenizer that produced it: id 264 is ` a` in one vocabulary and
    something unrelated in the next. A re-score answered by a different model
    than the reply came from returns a perfectly well-formed distribution of
    ids that mean the wrong thing -- nothing errors, nothing looks odd, and the
    corruption only surfaces at bake time when those ids are replayed against
    the HF tokenizer. The treasuremap calls this out as a parity failure that
    corrupts silently; this class is how it stops being silent.
    """


def served_model_of(response: Any, requested: str) -> str:
    """Which model actually answered.

    NOT the same question as "which model was asked for", and the gap is real
    rather than theoretical: during an eval window the lease checkpoint serves
    the model already resident INSTEAD of the requested one (self.ai#35) and
    says so in the body. Reading `model` alone would miss it, because the
    substitution path rewrites the payload's model before dispatch.

    Order is deliberate -- the explicit substitution record first, then the
    arena channel it reuses, then whatever the upstream echoed, and only then
    the request itself.
    """
    if not isinstance(response, dict):
        return requested

    substitution = response.get("model_substitution")
    if isinstance(substitution, dict) and substitution.get("served"):
        return str(substitution["served"])

    selected = response.get("selected_model_id")
    if selected:
        return str(selected)

    echoed = response.get("model")
    if echoed:
        return str(echoed)

    return requested


def assert_expected_model(requested: str, expected: Optional[str]) -> None:
    """Refuse a re-score whose reply came from a different model (R5-AC3).

    Checked BEFORE dispatch: the caller is telling us the ids on screen came
    from `expected`, so asking `requested` for more of them is incoherent no
    matter what the upstream would have said. Refusing here also costs no
    generation.
    """
    if expected and expected != requested:
        raise TokenizationModelMismatch(
            f"this reply was produced by {expected!r}, but the re-score asked "
            f"{requested!r}. Token ids are only meaningful against the "
            f"tokenizer that produced them, so re-scoring across models would "
            f"return ids that mean something else. Re-score against "
            f"{expected!r}, or start a new session."
        )


#: Where the served weights are named, in preference order.
#:
#: `--model` in the child's argv is the operative fact -- it is what
#: llama-server was actually started with. The preset's `model =` line is the
#: same value one step earlier in the chain and is the fallback for a model
#: that is not currently running (no argv exists yet).
def weights_path_from_status(status_obj: Any) -> Optional[str]:
    """Pull the GGUF path out of llamolotl's per-model status.

    This is the identity that matters to a bake: not the display id (which is
    a preset section name and can be renamed freely) but the file the weights
    were loaded from, quantisation and all.
    """
    if not isinstance(status_obj, dict):
        return None

    args = status_obj.get("args")
    if isinstance(args, (list, tuple)):
        for i, arg in enumerate(args):
            if arg == "--model" and i + 1 < len(args):
                return str(args[i + 1])

    preset = status_obj.get("preset")
    if isinstance(preset, str):
        for line in preset.splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "model":
                return value.strip() or None

    return None


def build_identity(
    model_id: str,
    status_obj: Any = None,
    context_length: Optional[int] = None,
) -> dict[str, Any]:
    """Describe whose tokenizer the ids in this response belong to (R5-AC2).

    `weights` is the point. `weights_known` is stated explicitly rather than
    left to be inferred from a null, because "we could not read the preset" and
    "there is no preset" would otherwise look identical to a consumer -- and a
    bake that silently skips the parity check it was supposed to perform is the
    exact failure this requirement exists to make impossible.
    """
    weights = weights_path_from_status(status_obj)
    identity: dict[str, Any] = {
        "model": model_id,
        "weights": weights,
        "weights_known": weights is not None,
    }
    if context_length:
        identity["context_length"] = context_length
    return identity

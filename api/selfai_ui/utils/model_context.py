"""Served context window reporting for the model list (self.ai#87).

`GET /api/models` published no context-length field at all, so every consumer
that budgets a context window — crew-code's auto-compaction, an eval harness,
any agent loop — had to assume one. Assuming high fails hard rather than
degrading: a consumer that assumes 131072 against a model actually served at
16384 never fires compaction, blows the real limit mid-task, and takes its own
recovery path down with it, since that path budgets against the same wrong
number.

Two rules hold throughout this module:

1. **Served, not trained.** `n_ctx_train` is the ceiling the weights were
   trained to, not the window the server allocated — on the model in #87 the
   two differ by 16x, and that gap *is* the bug. A trained number looks
   authoritative and leaves a caller exactly as wrong as no number, so it is
   never used as a fallback.
2. **Omit rather than guess.** A missing field tells a consumer to fall back to
   its own policy. A wrong field tells it nothing is wrong.
"""

import logging
from typing import Any, Optional

from selfai_ui.env import SRC_LOG_LEVELS

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])

# The contract, named once. `context_length` rather than vLLM's `max_model_len`
# because it is the name OpenAI-compatible clients most often already read
# (OpenRouter publishes it under this name).
CONTEXT_LENGTH_FIELD = "context_length"
MAX_OUTPUT_TOKENS_FIELD = "max_output_tokens"

# Names other backends publish the same two numbers under. Anything outside
# these lists is left alone rather than guessed at.
_UPSTREAM_CONTEXT_FIELDS = (
    "context_length",  # OpenRouter and most OpenAI-compatible gateways
    "max_model_len",  # vLLM's /v1/models
    "max_context_length",
    "context_window",
    "max_input_tokens",  # Anthropic-shaped model listings
)
_UPSTREAM_OUTPUT_FIELDS = (
    "max_output_tokens",
    "max_completion_tokens",
)

# llama.cpp's own truth table (common_arg_utils::is_truthy / is_falsey /
# is_autoy in common/arg.cpp), mirrored rather than approximated so a preset
# written `kv-unified = on` reads here the way the server reads it.
_TRUTHY = frozenset({"on", "enabled", "true", "1"})
_FALSEY = frozenset({"off", "disabled", "false", "0"})
_AUTOY = frozenset({"auto", "-1"})


def _int(value: Any) -> Optional[int]:
    """Int from a preset string or a JSON number. Booleans are not ints here."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _positive_int(value: Any) -> Optional[int]:
    parsed = _int(value)
    return parsed if parsed is not None and parsed > 0 else None


def _flag(value: Any) -> Optional[bool]:
    """Tri-state read of a llama.cpp boolean preset value: on, off, or unset."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSEY:
        return False
    return None


def parse_preset_ini(ini: Any) -> dict:
    """Parse llama.cpp's `common_preset::to_ini()` rendering into `{flag: value}`.

    The router publishes the *effective* preset per model — the `[*]` global
    section cascaded onto the per-model section, with the router's own CLI args
    merged over the top (llama.cpp `server-models.cpp`: `cascade()`, then
    `merge(base_preset)`) — so no inheritance has to be re-derived here. Keys
    are long-form CLI flags with the leading dashes stripped (`ctx-size =
    16384`); values stay raw strings.
    """
    if not isinstance(ini, str) or not ini.strip():
        return {}

    options: dict = {}
    pending = ""
    for raw_line in ini.splitlines():
        line = pending + raw_line
        pending = ""
        # to_ini() escapes newlines inside a value with a trailing backslash, so
        # a multi-line value (a chat template, say) arrives as continuations.
        if line.endswith("\\"):
            pending = line[:-1] + "\n"
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";", "[")):
            continue
        key, sep, value = stripped.partition("=")
        if not sep:
            continue
        options[key.strip()] = value.strip()
    return options


def _arg_value(args: Any, *flags: str) -> Optional[str]:
    """Value following the first of `flags` in a rendered argv list."""
    if not isinstance(args, (list, tuple)):
        return None
    for idx, arg in enumerate(args):
        if arg in flags and idx + 1 < len(args):
            return args[idx + 1]
    return None


def _arg_present(args: Any, *flags: str) -> bool:
    if not isinstance(args, (list, tuple)):
        return False
    return any(arg in flags for arg in args)


def _served_window_from_preset(options: dict, args: Any) -> Optional[int]:
    """Per-request window implied by an effective llama-server preset.

    `--ctx-size` sizes the whole KV allocation, which llama.cpp then splits
    across slots (`n_ctx_seq = n_ctx / n_seq_max`) unless the KV buffer is
    unified, in which case every slot sees the full window. Returns None when
    the preset pins no size: `ctx-size = 0` means "whatever the model was
    trained to", which cannot be known without loading the GGUF — and a trained
    ceiling is not a served window anyway.
    """
    ctx = _positive_int(options.get("ctx-size"))
    if ctx is None:
        ctx = _positive_int(_arg_value(args, "--ctx-size", "-c"))
    if ctx is None:
        return None

    raw_parallel = options.get("parallel")
    if raw_parallel is None:
        raw_parallel = _arg_value(args, "--parallel", "-np")
    parallel = _int(raw_parallel)
    if parallel is None and str(raw_parallel or "").strip().lower() in _AUTOY:
        parallel = -1

    if parallel is None or parallel < 0:
        # Slot count left on auto — the deployed case. llama-server then takes 4
        # slots *and* forces a unified KV buffer (server.cpp), overriding
        # whatever kv-unified said, so ctx-size is already the per-request
        # window.
        return ctx

    kv_unified = _flag(options.get("kv-unified"))
    if kv_unified is None:
        if _arg_present(args, "--kv-unified", "-kvu"):
            kv_unified = True
        elif _arg_present(args, "--no-kv-unified", "-no-kvu"):
            kv_unified = False

    if kv_unified or parallel <= 1:
        return ctx
    return (ctx // parallel) or None


def llamolotl_context_fields(model: Any) -> dict:
    """Context fields for one entry of self.llamolotl's `/v1/models`.

    Prefers what a loaded child process reports — `meta.n_ctx` is the slot
    window the server actually allocated, already capped to the trained context
    — and falls back to the effective preset, which the router publishes for
    every model whether or not it is loaded. That fallback is the point: models
    sit `unloaded` most of the time, and a consumer builds its model table at
    startup, before anything is loaded.
    """
    if not isinstance(model, dict):
        return {}

    fields: dict = {}

    meta = model.get("meta")
    if isinstance(meta, dict):
        # Deliberately not meta["n_ctx_train"] — see the module docstring.
        live = _positive_int(meta.get("n_ctx"))
        if live is not None:
            fields[CONTEXT_LENGTH_FIELD] = live

    status = model.get("status")
    status = status if isinstance(status, dict) else {}
    options = parse_preset_ini(status.get("preset"))
    args = status.get("args")

    if CONTEXT_LENGTH_FIELD not in fields:
        served = _served_window_from_preset(options, args)
        if served is not None:
            fields[CONTEXT_LENGTH_FIELD] = served

    # llama.cpp bounds generation with --n-predict; anything <= 0 means "until
    # the context fills", which is not a separate limit worth publishing.
    predict = _positive_int(options.get("n-predict"))
    if predict is None:
        predict = _positive_int(_arg_value(args, "--n-predict", "--predict", "-n"))
    if predict is not None:
        window = fields.get(CONTEXT_LENGTH_FIELD)
        fields[MAX_OUTPUT_TOKENS_FIELD] = min(predict, window) if window else predict

    return fields


def upstream_context_fields(*payloads: Any) -> dict:
    """Context fields a non-llamolotl backend already published, normalized.

    Reads only names that unambiguously mean the served window or the output cap
    on an OpenAI-compatible listing. Backends that publish neither (Ollama's
    `/api/tags`, most gateways) get no field — which is the honest answer for an
    externally-gated model, and better than a plausible-looking guess.
    """
    fields: dict = {}
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        if CONTEXT_LENGTH_FIELD not in fields:
            for key in _UPSTREAM_CONTEXT_FIELDS:
                value = _positive_int(payload.get(key))
                if value is not None:
                    fields[CONTEXT_LENGTH_FIELD] = value
                    break
        if MAX_OUTPUT_TOKENS_FIELD not in fields:
            for key in _UPSTREAM_OUTPUT_FIELDS:
                value = _positive_int(payload.get(key))
                if value is not None:
                    fields[MAX_OUTPUT_TOKENS_FIELD] = value
                    break
    return fields


def inherited_context_fields(model: Any) -> dict:
    """The context fields already on a model entry, for copying onto a preset."""
    if not isinstance(model, dict):
        return {}
    return {
        field: model[field]
        for field in (CONTEXT_LENGTH_FIELD, MAX_OUTPUT_TOKENS_FIELD)
        if _positive_int(model.get(field)) is not None
    }

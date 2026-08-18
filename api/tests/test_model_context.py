"""Served context window reporting on the model list (self.ai#87).

The failure this guards against is silent: a consumer that is handed a window
8x larger than the one actually served never fires compaction and dies on a
hard limit. So the tests care as much about what is *not* reported (a trained
ceiling, a guess for a backend that publishes nothing) as about what is.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from selfai_ui.utils import models as models_util
from selfai_ui.utils.model_context import (
    inherited_context_fields,
    llamolotl_context_fields,
    parse_preset_ini,
    upstream_context_fields,
)

# Shape of one `/v1/models` entry as self.llamolotl's router renders it: the
# effective preset (`[*]` cascaded with the per-model section) as an INI string
# under status.preset, present whether or not the model is loaded.
GEM8Y_PRESET = """[gemma-4-26B-A4B-it-qat-UD-Q4_K_XL]
model = /models/gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf
n-gpu-layers = 999
ctx-size = 16384
flash-attn = 1
jinja = 1

"""


def _unloaded(preset=GEM8Y_PRESET, args=None):
    return {
        "id": "gem8y",
        "object": "model",
        "status": {"value": "unloaded", "args": args or [], "preset": preset},
    }


@pytest.mark.tier0
def test_preset_ini_parses_effective_options():
    options = parse_preset_ini(GEM8Y_PRESET)
    assert options["ctx-size"] == "16384"
    assert options["n-gpu-layers"] == "999"
    assert "[gemma-4-26B-A4B-it-qat-UD-Q4_K_XL]" not in options


@pytest.mark.tier0
def test_preset_ini_joins_escaped_multiline_values():
    options = parse_preset_ini("[m]\nchat-template = line one\\\nline two\nctx-size = 4096\n")
    assert options["chat-template"] == "line one\nline two"
    assert options["ctx-size"] == "4096"


@pytest.mark.tier0
def test_unloaded_model_reports_its_preset_window():
    """The whole point of #87: unloaded is the normal state, and consumers build
    their model table before anything is loaded."""
    assert llamolotl_context_fields(_unloaded())["context_length"] == 16384


@pytest.mark.tier0
def test_loaded_model_prefers_the_live_slot_window():
    """A running child knows the window it actually allocated; the preset is only
    what it was asked for."""
    model = {
        "id": "gem8y",
        "status": {"value": "ready", "preset": "[gem8y]\nctx-size = 131072\n"},
        "meta": {"n_ctx": 16384, "n_ctx_train": 262144},
    }
    assert llamolotl_context_fields(model)["context_length"] == 16384


@pytest.mark.tier0
def test_trained_ceiling_is_never_reported():
    """n_ctx_train would look authoritative and be wrong by 16x — omitting is the
    honest answer."""
    model = {"id": "gem8y", "status": {"value": "ready"}, "meta": {"n_ctx_train": 262144}}
    assert llamolotl_context_fields(model) == {}


@pytest.mark.tier0
def test_ctx_size_zero_reports_nothing():
    """`ctx-size = 0` means 'whatever the model was trained to', which is not
    knowable without loading the GGUF."""
    assert llamolotl_context_fields(_unloaded(preset="[m]\nctx-size = 0\n")) == {}


@pytest.mark.tier0
def test_no_preset_and_no_args_reports_nothing():
    assert llamolotl_context_fields({"id": "m", "status": {"value": "unloaded"}}) == {}


@pytest.mark.tier0
def test_explicit_slots_split_the_kv_pool():
    """With --parallel pinned and no unified KV, llama.cpp gives each slot
    n_ctx / n_parallel."""
    fields = llamolotl_context_fields(_unloaded(preset="[m]\nctx-size = 32768\nparallel = 4\n"))
    assert fields["context_length"] == 8192


@pytest.mark.tier0
def test_unified_kv_gives_every_slot_the_full_window():
    fields = llamolotl_context_fields(_unloaded(preset="[m]\nctx-size = 32768\nparallel = 4\nkv-unified = 1\n"))
    assert fields["context_length"] == 32768


@pytest.mark.tier0
def test_auto_slot_count_is_unified_kv():
    """Slots left on auto is the deployed case: llama-server takes 4 slots behind
    one unified KV buffer, so ctx-size is already the per-request window."""
    fields = llamolotl_context_fields(_unloaded(preset="[m]\nctx-size = 16384\nparallel = -1\n"))
    assert fields["context_length"] == 16384


@pytest.mark.tier0
def test_rendered_args_are_the_fallback_when_no_preset_is_published():
    """Older router builds publish the child's argv but no preset INI."""
    model = _unloaded(preset=None, args=["/app/llama-server", "--ctx-size", "16384", "--jinja"])
    assert llamolotl_context_fields(model)["context_length"] == 16384


@pytest.mark.tier0
def test_n_predict_becomes_max_output_tokens_and_is_capped_by_the_window():
    unlimited = llamolotl_context_fields(_unloaded(preset="[m]\nctx-size = 8192\nn-predict = -1\n"))
    assert "max_output_tokens" not in unlimited

    capped = llamolotl_context_fields(_unloaded(preset="[m]\nctx-size = 8192\nn-predict = 65536\n"))
    assert capped["max_output_tokens"] == 8192

    set_below = llamolotl_context_fields(_unloaded(preset="[m]\nctx-size = 8192\nn-predict = 2048\n"))
    assert set_below["max_output_tokens"] == 2048


@pytest.mark.tier0
@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"context_length": 262144}, 262144),
        ({"max_model_len": 32768}, 32768),
        ({"max_input_tokens": 200000}, 200000),
        ({"id": "claude-opus-5"}, None),
        ({"context_length": 0}, None),
        ({"context_length": "not a number"}, None),
    ],
)
def test_upstream_windows_are_passed_through_or_omitted(payload, expected):
    """An externally-gated model carries a correct value or no value — never a
    guessed one."""
    fields = upstream_context_fields(payload)
    assert fields.get("context_length") == expected


def _request(**flags):
    config = {
        "ENABLE_OPENAI_API": False,
        "ENABLE_OLLAMA_API": False,
        "ENABLE_LLAMOLOTL_API": False,
        "ENABLE_ANTHROPIC_API": False,
        **flags,  # merged, not passed alongside — a repeated key is an override here
    }
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(config=SimpleNamespace(**config))))


@pytest.mark.tier0
def test_model_list_carries_the_window_out_of_the_llamolotl_transport():
    """End of the plumbing gap: the number llamolotl knows reaches the entry
    /api/models hands a consumer."""
    served = {"data": [{"id": "gem8y", "status": "unloaded", "context_length": 16384}]}
    with patch.object(models_util.llamolotl, "get_all_models", AsyncMock(return_value=served)), patch.object(
        models_util, "get_function_models", AsyncMock(return_value=[])
    ):
        entries = asyncio.run(models_util.get_all_base_models(_request(ENABLE_LLAMOLOTL_API=True)))

    assert [entry["context_length"] for entry in entries] == [16384]


@pytest.mark.tier0
def test_model_list_omits_the_window_when_the_backend_publishes_none():
    served = {"data": [{"id": "arena-model", "status": "unloaded"}]}
    with patch.object(models_util.llamolotl, "get_all_models", AsyncMock(return_value=served)), patch.object(
        models_util, "get_function_models", AsyncMock(return_value=[])
    ):
        entries = asyncio.run(models_util.get_all_base_models(_request(ENABLE_LLAMOLOTL_API=True)))

    assert "context_length" not in entries[0]


@pytest.mark.tier0
def test_inherited_fields_copy_only_real_values():
    assert inherited_context_fields({"context_length": 16384, "max_output_tokens": 4096}) == {
        "context_length": 16384,
        "max_output_tokens": 4096,
    }
    assert inherited_context_fields({"context_length": None}) == {}
    assert inherited_context_fields({}) == {}


@pytest.mark.tier0
def test_get_all_base_models_fans_out_providers_concurrently():
    """All enabled backend fetches start concurrently, not sequentially.

    Proven by an asyncio.Event gate: _fetch_openai suspends on the gate and
    only unblocks once _fetch_ollama sets it.  With asyncio.gather both
    coroutines interleave on the same event loop — Ollama runs while OpenAI
    is suspended, sets the gate, and unblocks it.  With the old sequential
    ``await`` path, OpenAI's suspension would be permanent (Ollama never
    gets the event loop) and the call would raise TimeoutError, failing the
    test with a clear signal rather than hanging.
    """

    async def run():
        gate = asyncio.Event()
        started: list = []

        async def _openai_wait(request):
            started.append("openai")
            # Suspends until Ollama's coroutine releases the gate.  Sequential
            # scheduling would leave this blocked forever → TimeoutError.
            await asyncio.wait_for(gate.wait(), timeout=2.0)
            return {"data": []}

        async def _ollama_signal(request):
            started.append("ollama")
            gate.set()
            return {"models": []}

        with (
            patch.object(models_util.openai, "get_all_models", _openai_wait),
            patch.object(models_util.ollama, "get_all_models", _ollama_signal),
            patch.object(models_util, "get_function_models", AsyncMock(return_value=[])),
        ):
            req = _request(ENABLE_OPENAI_API=True, ENABLE_OLLAMA_API=True)
            await models_util.get_all_base_models(req)

        assert "openai" in started
        assert "ollama" in started

    asyncio.run(run())

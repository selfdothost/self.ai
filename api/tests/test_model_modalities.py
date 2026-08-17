"""Vision capability derived from the router's declared modalities (self.ai#139).

The failure this guards against is a wrong default, not a missing one. The
client reads `info.meta.capabilities.vision ?? true`, 97 of 101 workspace rows
carry a null `capabilities`, and llamolotl answers an image posted to a
text-only model with a bare HTTP 500 — so "we published nothing" and "yes, send
images" were the same thing to every consumer.

So these tests care as much about what is *not* written as about what is: a
model from a backend that declares no `architecture` must gain no `vision` key
at all, and an admin's explicit `vision` must survive in **both** directions.

Async cases use `asyncio.run(...)` from a sync test — this repo registers no
pytest-asyncio and runs under `--strict-markers`.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import selfai_ui.utils.models as models_util
from selfai_ui.models.models import ModelForm, ModelMeta, ModelParams, Models
from selfai_ui.routers import llamolotl as llamolotl_router
from selfai_ui.utils.model_modalities import (
    apply_derived_capabilities,
    architecture_fields,
    derived_vision,
    input_modalities,
)

VISION_MODEL = "Qwen2.5-VL-7B-Instruct-Q4_K_M"
TEXT_MODEL = "gemma-4-26B-A4B-it-qat-UD-Q4_K_XL"


def _served_entry(model_id, modalities=("text",), architecture=True):
    """One entry of self.llamolotl's `/v1/models`, in the shape it really ships.

    Every key here is on the live payload — `architecture` is published for
    loaded and unloaded models alike, because it is derived from the preset's
    `mmproj` path rather than from a running child. `architecture=False` stands
    in for a backend that publishes no modalities at all (Ollama's `/api/tags`,
    an OpenAI-compatible gateway, an Anthropic listing).
    """
    entry = {
        "id": model_id,
        "aliases": [],
        "tags": [],
        "object": "model",
        "owned_by": "llamacpp",
        "created": 1754006400,
        "status": {
            "value": "unloaded",
            "args": [],
            "preset": f"[{model_id}]\nmodel = /models/{model_id}.gguf\nctx-size = 16384\n",
        },
        "source": {"type": "local", "path": f"/models/{model_id}.gguf"},
        "can_remove": True,
        "vram_footprint": {"bytes": 5465081856, "source": "measured"},
    }
    if architecture:
        entry["architecture"] = {
            "input_modalities": list(modalities),
            "output_modalities": ["text"],
        }
    return entry


def _request(**overrides):
    config = {
        "ENABLE_OPENAI_API": False,
        "ENABLE_OLLAMA_API": False,
        "ENABLE_ANTHROPIC_API": False,
        "ENABLE_LLAMOLOTL_API": True,
        "LLAMOLOTL_BASE_URLS": ["http://llamolotl.test:8080"],
        "LLAMOLOTL_API_CONFIGS": {},
        "ENABLE_EVALUATION_ARENA_MODELS": False,
        "EVALUATION_ARENA_MODELS": [],
        "ENABLE_SELF_CORPUS": False,
        **overrides,
    }
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(config=SimpleNamespace(**config), FUNCTIONS={}, MODELS={})
        )
    )


@pytest.fixture(autouse=True)
def _drop_the_model_list_cache():
    """The transport caches `/v1/models` under a FIXED key with ttl=3.

    Without clearing it, every test after the first would assert against the
    first test's payload and pass for a reason that has nothing to do with the
    code under test.
    """
    asyncio.run(llamolotl_router.get_all_models.cache.clear())
    yield
    asyncio.run(llamolotl_router.get_all_models.cache.clear())


def _list_models(*entries, request=None):
    """The whole path: llamolotl's HTTP payload → the entries `/api/models` returns.

    Only the transport's HTTP call is stubbed. The router's reshape, the
    workspace-row merge and the derivation all run for real, which is the point
    — the field was being lost *in* the reshape, so a test that starts after it
    would prove nothing.
    """
    served = {"data": list(entries)}
    with patch.object(llamolotl_router, "send_get_request", AsyncMock(return_value=served)), patch.object(
        models_util, "get_function_models", AsyncMock(return_value=[])
    ):
        return asyncio.run(models_util.get_all_models(request or _request()))


def _by_id(entries, model_id):
    return next(entry for entry in entries if entry["id"] == model_id)


def _vision(entry):
    """`info.meta.capabilities.vision` as the client reads it — absent is not false."""
    return ((entry.get("info") or {}).get("meta") or {}).get("capabilities", {}).get("vision", "absent")


def _model_row(model_id, meta=None):
    return Models.insert_new_model(
        ModelForm(id=model_id, name=model_id, meta=ModelMeta(**(meta or {})), params=ModelParams()),
        "u1",
    )


####################
# Reading the declaration
####################


@pytest.mark.tier0
def test_image_input_modality_derives_vision():
    assert derived_vision(_served_entry(VISION_MODEL, ("text", "image"))) is True


@pytest.mark.tier0
def test_text_only_input_modalities_derive_no_vision():
    assert derived_vision(_served_entry(TEXT_MODEL, ("text",))) is False


@pytest.mark.tier0
def test_an_undeclared_backend_derives_nothing_rather_than_false():
    """None is the third state, and it is the one that keeps the key absent."""
    assert derived_vision(_served_entry(TEXT_MODEL, architecture=False)) is None
    assert derived_vision({"id": "m", "architecture": {"output_modalities": ["text"]}}) is None
    assert derived_vision({"id": "m", "architecture": "not a dict"}) is None
    assert derived_vision(None) is None


@pytest.mark.tier0
def test_modalities_are_read_from_whichever_copy_carries_them():
    """The flattened entry and the nested transport original travel together."""
    entry = {"id": "m", "llamolotl": _served_entry(VISION_MODEL, ("text", "image"))}
    assert input_modalities(entry, entry.get("llamolotl")) == ["text", "image"]
    assert derived_vision(entry, entry.get("llamolotl")) is True


@pytest.mark.tier0
def test_architecture_object_is_passed_through_whole():
    """`output_modalities` and a future `audio` input are not this module's to drop."""
    fields = architecture_fields(_served_entry(VISION_MODEL, ("text", "image", "audio")))
    assert fields["architecture"] == {
        "input_modalities": ["text", "image", "audio"],
        "output_modalities": ["text"],
    }
    assert architecture_fields(_served_entry(TEXT_MODEL, architecture=False)) == {}
    assert architecture_fields({"architecture": {}}) == {}
    assert architecture_fields("not a model") == {}


####################
# Precedence
####################


@pytest.mark.tier0
def test_an_explicit_row_value_wins_in_both_directions():
    """An admin override is a deliberate correction of what the backend says."""
    forced_on = {
        "id": "m",
        "architecture": {"input_modalities": ["text"]},
        "info": {"meta": {"capabilities": {"vision": True}}},
    }
    forced_off = {
        "id": "m",
        "architecture": {"input_modalities": ["text", "image"]},
        "info": {"meta": {"capabilities": {"vision": False}}},
    }
    apply_derived_capabilities([forced_on, forced_off])

    assert forced_on["info"]["meta"]["capabilities"]["vision"] is True
    assert forced_off["info"]["meta"]["capabilities"]["vision"] is False


@pytest.mark.tier0
def test_a_null_capabilities_block_is_silence_not_an_answer():
    """The 97-row case: the row exists, `capabilities` is JSON null."""
    entry = {"id": "m", "architecture": {"input_modalities": ["text"]}, "info": {"meta": {"capabilities": None}}}
    apply_derived_capabilities([entry])
    assert entry["info"]["meta"]["capabilities"]["vision"] is False


@pytest.mark.tier0
def test_other_capabilities_on_the_row_survive_the_merge():
    entry = {
        "id": "m",
        "architecture": {"input_modalities": ["text", "image"]},
        "info": {"meta": {"capabilities": {"usage": False, "citations": True}}},
    }
    apply_derived_capabilities([entry])
    assert entry["info"]["meta"]["capabilities"] == {"usage": False, "citations": True, "vision": True}


@pytest.mark.tier0
def test_an_undeclared_model_is_not_touched_at_all():
    """No `architecture` means no opinion — not `vision: false`, and not an
    `info` block conjured out of nothing."""
    entry = {"id": "gpt-4o", "object": "model", "owned_by": "openai"}
    apply_derived_capabilities([entry])
    assert entry == {"id": "gpt-4o", "object": "model", "owned_by": "openai"}


####################
# End to end, through the real transport and the real merge
####################


@pytest.mark.tier0
def test_the_transport_no_longer_drops_the_declaration(db_session):
    """The reshape builds an explicit dict, so anything unnamed is lost."""
    entries = _list_models(_served_entry(VISION_MODEL, ("text", "image")))
    assert _by_id(entries, VISION_MODEL)["architecture"] == {
        "input_modalities": ["text", "image"],
        "output_modalities": ["text"],
    }


@pytest.mark.tier0
def test_the_vision_model_reports_the_capability_with_no_row_involved(db_session):
    """Acceptance criterion 1: derived, not hand-edited."""
    entries = _list_models(
        _served_entry(VISION_MODEL, ("text", "image")),
        _served_entry(TEXT_MODEL, ("text",)),
    )
    assert _vision(_by_id(entries, VISION_MODEL)) is True
    assert _vision(_by_id(entries, TEXT_MODEL)) is False


@pytest.mark.tier0
def test_a_row_that_forces_vision_on_still_wins_end_to_end(db_session):
    _model_row(TEXT_MODEL, {"capabilities": {"vision": True}})
    entries = _list_models(_served_entry(TEXT_MODEL, ("text",)))
    assert _vision(_by_id(entries, TEXT_MODEL)) is True


@pytest.mark.tier0
def test_a_row_that_forces_vision_off_still_wins_end_to_end(db_session):
    _model_row(VISION_MODEL, {"capabilities": {"vision": False}})
    entries = _list_models(_served_entry(VISION_MODEL, ("text", "image")))
    assert _vision(_by_id(entries, VISION_MODEL)) is False


@pytest.mark.tier0
def test_a_row_with_null_capabilities_gets_the_derived_answer(db_session):
    """The majority case in the live database."""
    _model_row(TEXT_MODEL)
    entries = _list_models(_served_entry(TEXT_MODEL, ("text",)))
    assert _vision(_by_id(entries, TEXT_MODEL)) is False


@pytest.mark.tier0
def test_a_backend_that_declares_nothing_gains_no_vision_key(db_session):
    """Acceptance criterion 3 — the one that must not regress into `false`."""
    entries = _list_models(_served_entry("mystery-gateway-model", architecture=False))
    assert _vision(_by_id(entries, "mystery-gateway-model")) == "absent"


@pytest.mark.tier0
def test_a_workspace_preset_inherits_its_base_model_declaration(db_session):
    """A prompt/params wrapper is served by the same process as its base, so a
    wrapper over a text-only model is text-only — same reasoning as the served
    window in self.ai#87."""
    Models.insert_new_model(
        ModelForm(
            id="terse-gemma",
            name="Terse Gemma",
            base_model_id=TEXT_MODEL,
            meta=ModelMeta(),
            params=ModelParams(),
        ),
        "u1",
    )
    entries = _list_models(_served_entry(TEXT_MODEL, ("text",)))
    preset = _by_id(entries, "terse-gemma")
    assert preset["architecture"] == {"input_modalities": ["text"], "output_modalities": ["text"]}
    assert _vision(preset) is False

"""Versioning switched off, and switched on but unreachable — self.ai#131 R10.

A regression guard, not a feature. `ENABLE_SELF_CORPUS` off is a supported
configuration and the default for a fresh install, and "on but self.corpus is
down" is a different failure from "off" — both are supported, and neither may
take chat, model listing or model registration with it.

The fourth case is the quiet one: flipping the flag **on** must not manufacture
lines for models that predate it. A backfill is an explicit admin action
(`utils/self_corpus.backfill_missing_repos`'s shape), not a side effect of a
config change.

Async cases use `asyncio.run(...)` from a sync test — this repo registers no
pytest-asyncio and runs under `--strict-markers`.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import selfai_ui.utils.model_versions as service
import selfai_ui.utils.models as models_util
from selfai_ui.models.model_versions import ModelLines, ModelVersions
from selfai_ui.models.models import ModelForm, ModelMeta, ModelParams, Models
from selfai_ui.utils.self_corpus import SelfCorpusError

SERVED = [
    {
        "id": "gemma-q4.gguf",
        "name": "gemma-q4.gguf",
        "object": "model",
        "owned_by": "llamolotl",
        "created": 1,
    }
]


def _request(enable_self_corpus: bool):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                config=SimpleNamespace(
                    ENABLE_SELF_CORPUS=enable_self_corpus,
                    ENABLE_EVALUATION_ARENA_MODELS=False,
                    EVALUATION_ARENA_MODELS=[],
                ),
                FUNCTIONS={},
                MODELS={},
            )
        )
    )


def _model_row(model_id="gemma-q4.gguf", meta=None):
    return Models.insert_new_model(
        ModelForm(
            id=model_id,
            name=model_id,
            meta=ModelMeta(**(meta or {})),
            params=ModelParams(),
        ),
        "u1",
    )


def _list_models(enable_self_corpus: bool):
    with patch.object(models_util, "get_all_base_models", AsyncMock(return_value=list(SERVED))):
        return asyncio.run(models_util.get_all_models(_request(enable_self_corpus)))


@pytest.fixture
def unreachable_corpus(monkeypatch):
    """The flag is on; every self.corpus call fails."""

    async def _explode(*args, **kwargs):
        raise SelfCorpusError("self.corpus is unreachable")

    monkeypatch.setattr(service, "commit_branch", _explode)
    monkeypatch.setattr(service, "create_repository", _explode)


####################
# Model listing survives both failure modes
####################


@pytest.mark.tier0
def test_model_listing_works_with_versioning_off(db_session):
    entries = _list_models(False)
    assert [e["id"] for e in entries] == ["gemma-q4.gguf"]


@pytest.mark.tier0
def test_model_listing_carries_no_version_fields_when_off(db_session):
    """Degrades to today's flat list — not empty scaffolding."""
    entries = _list_models(False)
    assert "line" not in entries[0]


@pytest.mark.tier0
def test_model_listing_works_when_corpus_is_unreachable(db_session, unreachable_corpus):
    """The flag is on and self.corpus is down: listing is a DB read either way."""
    entries = _list_models(True)
    assert [e["id"] for e in entries] == ["gemma-q4.gguf"]


@pytest.mark.tier0
def test_a_model_with_no_line_lists_identically_either_way(db_session):
    """The byte-identical promise, asserted as whole-dict equality."""
    _model_row()
    off = _list_models(False)
    on = _list_models(True)
    assert on == off


####################
# Registration survives both failure modes
####################


@pytest.mark.tier0
def test_registration_leaves_a_servable_model_when_corpus_is_unreachable(db_session, unreachable_corpus):
    """A corpus outage must not make a model unservable."""
    from selfai_ui.routers import llamolotl as llamolotl_router

    _model_row()
    request = SimpleNamespace(app=SimpleNamespace(state=_request(True).app.state))
    request.app.state.config.SELF_CORPUS_LAKEFS_ENDPOINT = "http://terminus.test"
    request.app.state.config.SELF_CORPUS_LAKEFS_ACCESS_KEY_ID = "k"
    request.app.state.config.SELF_CORPUS_LAKEFS_SECRET_ACCESS_KEY = "s"

    # The route wraps this in its own try/except and continues; the model stays
    # servable and carries no half-written version pointer.
    with pytest.raises(SelfCorpusError):
        asyncio.run(
            llamolotl_router._attach_registration_to_line(
                request,
                llamolotl_router.HFModelRegisterForm(name="gemma-q4.gguf"),
                SimpleNamespace(id="u1"),
            )
        )

    model = Models.get_model_by_id("gemma-q4.gguf")
    assert model is not None and model.is_active
    assert model.meta.model_dump().get("version_id") is None
    assert ModelLines.get_lines() == [] or all(
        ModelVersions.get_versions_by_line(line.id) == [] for line in ModelLines.get_lines()
    )


####################
# Turning the flag on backfills nothing
####################


@pytest.mark.tier0
def test_flipping_the_flag_on_creates_no_lines_retroactively(db_session):
    """A backfill is an explicit admin action, not a config side effect."""
    _model_row("pre-existing.gguf")

    _list_models(False)
    assert ModelLines.get_lines() == []

    entries = _list_models(True)

    assert ModelLines.get_lines() == []
    assert "line" not in entries[0]


def _register_request(enable_self_corpus: bool):
    """The state register_model reads, beyond the versioning flag."""
    request = _request(enable_self_corpus)
    cfg = request.app.state.config
    cfg.LLAMOLOTL_CONTROL_BASE_URLS = ["http://llamolotl.test:8093"]
    cfg.LLAMOLOTL_BASE_URLS = ["http://llamolotl.test:8080"]
    cfg.LLAMOLOTL_API_CONFIGS = {}
    cfg.SELF_CORPUS_LAKEFS_ENDPOINT = "http://terminus.test"
    cfg.SELF_CORPUS_LAKEFS_ACCESS_KEY_ID = "k"
    cfg.SELF_CORPUS_LAKEFS_SECRET_ACCESS_KEY = "s"
    return request


@pytest.mark.tier0
@pytest.mark.parametrize("enabled,expected_attempts", [(False, 0), (True, 1)])
def test_registration_only_attaches_a_line_when_versioning_is_on(
    db_session, monkeypatch, enabled, expected_attempts
):
    """Drives the real route, with only llamolotl's transport stubbed.

    Parametrised in both directions on purpose: asserting only the off case
    would pass just as well if the attach were never wired up at all.
    """
    from selfai_ui.routers import llamolotl as llamolotl_router

    attempts = []

    async def _spy(request, form_data, user):
        attempts.append(form_data.name)

    monkeypatch.setattr(llamolotl_router, "_attach_registration_to_line", _spy)
    monkeypatch.setattr(llamolotl_router, "send_post_request", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(llamolotl_router, "send_get_request", AsyncMock(return_value=[]))

    _model_row()
    asyncio.run(
        llamolotl_router.register_model(
            _register_request(enabled),
            llamolotl_router.HFModelRegisterForm(name="gemma-q4.gguf"),
            url_idx=0,
            user=SimpleNamespace(id="u1"),
        )
    )

    assert len(attempts) == expected_attempts
    if not enabled:
        assert ModelLines.get_lines() == []

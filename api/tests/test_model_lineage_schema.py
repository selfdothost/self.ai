"""ModelMeta lineage is declared, not inferred — self.ai#140.

`ModelMeta` is `extra="allow"`, so for its whole life a lineage field could be
written without a schema change and read without a guarantee. That is the
failure named as D2 in gitlab-profile
`context/treasuremaps/2026-08-13-model-capacity-honesty.md`: `capabilities` was
`Optional[dict]`, 97 of 101 live rows carry `null`, and the client's guard
quietly became dead code. #140 declares the lineage fields so a consumer can
depend on them, and adds the `distilled` kind D9/D13 need.

Two properties are in tension and both are pinned here:

* **Strict enough to matter.** An unknown `source_type` is rejected, and a
  malformed `bake_info` / `active_loras` is rejected. A schema that accepts
  anything enforces nothing.
* **Loose enough to read what is already stored.** The shapes exercised below
  were taken off the live `model` table (101 rows), not invented: every row has
  `source_type: null`, `bake_info: null`, `active_loras: null` and no
  `line_id`/`version_id` at all; 97 have `capabilities: null`; and three carry
  the undeclared `tags`, `audio` and `suggestion_prompts` keys self.chat's
  ModelEditor writes. A stricter schema that cannot read those is worse than the
  loose one it replaces.

No migration accompanies #140: `model.meta` is a `JSONField` blob in both the
model and `7e5b5dc7342b_init`, and this change adds keys inside the blob, not
columns. Nothing here touches a column type, so the JSONField/sa.JSON hazard
pinned by test_model_versions_schema.py does not apply.
"""

import json

import pytest
from pydantic import ValidationError

from selfai_ui.internal.db import get_db
from selfai_ui.models.models import (
    ActiveLora,
    BakeInfo,
    DistillationInfo,
    Model,
    ModelForm,
    ModelMeta,
    ModelParams,
    Models,
    PruneRecipe,
)

#: Every value a live producer writes into `source_type`, read off the writers
#: rather than off the old comment, which was already stale (it named three and
#: self.llamolotl emits four).
#:
#: * `gguf`, `safetensors_converted` — self.llamolotl api/routers/models.py
#: * `baked`                         — self.llamolotl api/routers/pipeline.py
#: * `lora_gguf`                     — self.llamolotl api/state.py _record_lora_meta
#: * `distilled`                     — self.ai#140
PRODUCED_SOURCE_TYPES = ["gguf", "safetensors_converted", "baked", "lora_gguf", "distilled"]

#: One live row's meta, verbatim in shape: every declared key present and null,
#: no lineage, no `line_id`/`version_id`, plus the undeclared keys the workspace
#: editor writes. 100 of the 101 live rows are this modulo `capabilities`.
LEGACY_META = {
    "profile_image_url": "/static/favicon.png",
    "description": None,
    "capabilities": None,
    "hf_repo": None,
    "quant": None,
    "source_type": None,
    "trainable": None,
    "pulled_at": None,
    "bake_info": None,
    "active_loras": None,
    "suggestion_prompts": None,
    "tags": [],
    "audio": {"tts_voice": "craft:7d1c27df-26d6-4f8e-b27b-c8be73e1e4cb"},
}


def _write_raw_row(model_id, meta):
    """Insert a `model` row whose meta bypasses ModelMeta entirely.

    Going through `ModelForm` would validate the blob on the way in, which is
    exactly the thing a legacy row never did. This writes the dict as stored.
    """
    with get_db() as db:
        db.add(
            Model(
                id=model_id,
                user_id="u1",
                base_model_id=None,
                name=model_id,
                params={},
                meta=meta,
                access_control=None,
                is_active=True,
                created_at=0,
                updated_at=0,
            )
        )
        db.commit()


# ---------------------------------------------------------------------------
# The distilled kind
# ---------------------------------------------------------------------------


def test_a_distilled_model_round_trips_through_the_database(db_session):
    """Teacher, prune recipe, source corpus + commit, and weights location.

    The acceptance criterion in full: everything a distillation needs recorded
    survives a write and a read as typed fields, not as free-form extras.
    """
    meta = ModelMeta(
        hf_repo="google/gemma-4-26B-A4B-it",
        quant="Q4_K_M",
        source_type="distilled",
        trainable=True,
        corpus_repo="selfai-model-gemma-4-8b-distilled",
        corpus_commit_id="9f1c2d3e4a5b6c7d8e9f0a1b2c3d4e5f60718293",
        corpus_weights_present=True,
        distillation=DistillationInfo(
            teacher_model_id="gemma-4-26B-A4B-it-qat-UD-Q4_K_XL",
            teacher_hf_repo="google/gemma-4-26B-A4B-it",
            method="soft_target_kd",
            temperature=2.0,
            top_k_logits=64,
            prune_recipe=PruneRecipe(
                strategy="depth_and_width",
                layers_before=48,
                layers_after=32,
                intermediate_size_before=16384,
                intermediate_size_after=10240,
                notes="Minitron-shaped: prune then distil to recover.",
            ),
            source_corpus_repo="selfai-dataset-yard-transcripts",
            source_corpus_commit_id="a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
            distilled_at="2026-08-13T19:00:00",
        ),
    )

    Models.insert_new_model(
        ModelForm(id="gemma-4-8b-distilled", name="gemma-4-8b-distilled", meta=meta, params=ModelParams()),
        "u1",
    )

    stored = Models.get_model_by_id("gemma-4-8b-distilled")
    assert stored is not None, "the distilled row did not survive the write"

    assert stored.meta.source_type == "distilled"
    assert stored.meta.corpus_repo == "selfai-model-gemma-4-8b-distilled"
    assert stored.meta.corpus_commit_id == "9f1c2d3e4a5b6c7d8e9f0a1b2c3d4e5f60718293"
    assert stored.meta.corpus_weights_present is True

    d = stored.meta.distillation
    assert isinstance(d, DistillationInfo), "distillation came back untyped"
    assert d.teacher_model_id == "gemma-4-26B-A4B-it-qat-UD-Q4_K_XL"
    assert d.temperature == 2.0
    assert d.top_k_logits == 64
    assert d.source_corpus_repo == "selfai-dataset-yard-transcripts"
    assert d.source_corpus_commit_id == "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"

    assert d.prune_recipe.strategy == "depth_and_width"
    assert (d.prune_recipe.layers_before, d.prune_recipe.layers_after) == (48, 32)

    # And it is JSON in the column, not a repr of a Pydantic object.
    with get_db() as db:
        raw = db.execute(Model.__table__.select().where(Model.id == "gemma-4-8b-distilled")).first()
    blob = raw.meta if isinstance(raw.meta, dict) else json.loads(raw.meta)
    assert blob["distillation"]["prune_recipe"]["layers_after"] == 32


def test_the_weights_repo_and_the_training_corpus_are_different_fields():
    """The one confusion this schema exists to prevent.

    `corpus_repo` holds the weights; `distillation.source_corpus_repo` holds the
    text the teacher ran over. Collapsing them would make provenance a lie.

    self.ai#141 pins the same boundary from the other side, in
    `test_weights_repo_is_never_confused_with_a_distillation_source_corpus`: a
    distillation's `source_corpus_repo` reaches the commit metadata and the
    catalogue object, and never the row's `corpus_repo`.
    """
    meta = ModelMeta(
        source_type="distilled",
        corpus_repo="selfai-model-weights",
        distillation=DistillationInfo(source_corpus_repo="selfai-dataset-text"),
    )
    assert meta.corpus_repo == "selfai-model-weights"
    assert meta.distillation.source_corpus_repo == "selfai-dataset-text"
    assert meta.corpus_repo != meta.distillation.source_corpus_repo


def test_a_distilled_row_may_be_marked_before_its_details_are_written():
    """`distillation` is not required by `source_type == "distilled"`.

    A run records the row first and fills the lineage in when it finishes. A
    half-recorded distillation is still better read than rejected.
    """
    meta = ModelMeta(source_type="distilled")
    assert meta.distillation is None


# ---------------------------------------------------------------------------
# A link to a repo is not a claim that the bytes are in it
# ---------------------------------------------------------------------------


def test_a_linked_row_need_not_have_its_weights_in_the_repo(db_session):
    """The shape self.ai#141's backfill actually writes, and why the flag exists.

    Core has no filesystem access to `/models` — llamolotl owns that PVC — so
    the backfill creates the repo, commits a catalogue object and links both
    directions without ever uploading weights. That is a real commit of a real
    object, so a non-null `corpus_commit_id` does **not** imply the bytes are
    there. This is the normal state of a backfilled row, not an edge case.
    """
    _write_raw_row(
        "backfilled-model",
        {
            **LEGACY_META,
            "corpus_repo": "selfai-model-backfilled-model",
            "corpus_commit_id": "0f1e2d3c4b5a69788796a5b4c3d2e1f001234567",
            "corpus_weights_present": False,
        },
    )

    stored = Models.get_model_by_id("backfilled-model")
    assert stored.meta.corpus_repo == "selfai-model-backfilled-model"
    assert stored.meta.corpus_commit_id is not None
    assert stored.meta.corpus_weights_present is False, (
        "a linked-but-empty repo must read as False, not as 'has a commit so has bytes'"
    )


@pytest.mark.parametrize(
    "present,meaning",
    [
        (None, "never recorded"),
        (True, "the repo holds the bytes"),
        (False, "linked and provenanced, bytes only on the PVC"),
    ],
)
def test_corpus_weights_present_is_tri_state(db_session, present, meaning):
    """All three states occur in practice and must stay distinguishable.

    `None` and `False` are different claims — "we never looked" versus "we
    looked and they are not there". A `bool` defaulting False would fabricate
    the second out of the first, which is the D2 failure in miniature.
    """
    model_id = f"tri-state-{present}"
    _write_raw_row(model_id, {**LEGACY_META, "corpus_weights_present": present})

    stored = Models.get_model_by_id(model_id)
    assert stored.meta.corpus_weights_present is present, meaning

    # And the distinction survives serialisation back out to the column.
    assert stored.meta.model_dump()["corpus_weights_present"] is present


def test_corpus_weights_present_is_not_derivable_from_the_other_two_fields():
    """Two rows identical in repo and commit, opposite in whether bytes exist."""
    linked_only = ModelMeta(corpus_repo="selfai-model-x", corpus_commit_id="c1", corpus_weights_present=False)
    uploaded = ModelMeta(corpus_repo="selfai-model-x", corpus_commit_id="c1", corpus_weights_present=True)

    assert linked_only.corpus_repo == uploaded.corpus_repo
    assert linked_only.corpus_commit_id == uploaded.corpus_commit_id
    assert linked_only.corpus_weights_present is not uploaded.corpus_weights_present


def test_a_non_boolean_weights_flag_is_rejected():
    with pytest.raises(ValidationError):
        ModelMeta(corpus_weights_present="probably")


@pytest.mark.parametrize(
    "field,value",
    [
        ("corpus_repo", "selfai-model-x"),
        ("corpus_commit_id", "c1"),
        ("corpus_weights_present", False),
    ],
)
def test_the_corpus_link_fields_are_declared_not_extras(field, value):
    """self.ai#141 codes against these three, so they must be real fields.

    A key that lands on `model_extra` is validated by nothing and guaranteed by
    nothing — the D2 failure this whole MR is about. `corpus_weights_present`
    arrived in #141's worktree riding `extra="allow"` under a
    `# NOT YET DECLARED — requested of self.ai#140` marker; this is that request
    landing, and the marker's removal condition.
    """
    assert field in ModelMeta.model_fields, f"{field} is not declared on ModelMeta"

    meta = ModelMeta(**{field: value})
    assert getattr(meta, field) is value or getattr(meta, field) == value
    assert (meta.model_extra or {}) == {}, f"{field} was absorbed as an extra rather than declared"


# ---------------------------------------------------------------------------
# source_type is an enumeration now
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", PRODUCED_SOURCE_TYPES)
def test_every_source_type_a_producer_emits_is_accepted(kind):
    """A rejected value here is a *silent* loss, not a loud one.

    `routers/llamolotl.register_model` builds its ModelMeta inside a
    try/except that logs a warning — so a kind missing from this enumeration
    does not fail the registration, it just drops the lineage on the floor.
    """
    assert ModelMeta(source_type=kind).source_type == kind


@pytest.mark.parametrize("bad", ["safetensors", "GGUF", "distill", "", "quantized", "baked "])
def test_an_unknown_source_type_is_rejected(bad):
    with pytest.raises(ValidationError):
        ModelMeta(source_type=bad)


def test_source_type_is_still_optional():
    """All 101 live rows carry `source_type: null`. Rejecting that is a outage."""
    assert ModelMeta().source_type is None
    assert ModelMeta(source_type=None).source_type is None


def test_an_unknown_source_type_is_rejected_on_assignment_too():
    """`validate_assignment` — a declared field cannot be smuggled past by
    mutating the object after construction."""
    meta = ModelMeta(source_type="gguf")
    with pytest.raises(ValidationError):
        meta.source_type = "banana"
    assert meta.source_type == "gguf"


# ---------------------------------------------------------------------------
# bake_info / active_loras have shapes now
# ---------------------------------------------------------------------------


def test_bake_info_parses_the_shape_llamolotl_writes():
    """Verbatim from self.llamolotl api/routers/pipeline.py `_record_model_meta`."""
    meta = ModelMeta(
        source_type="baked",
        bake_info={
            "base_model": "gemma-4-26B-A4B-it",
            "adapters": [{"path": "yard-voice.gguf", "weight": 0.7}],
            "outtype": "f16",
            "quant_type": "Q4_K_M",
            "baked_at": "2026-08-13T18:00:00",
        },
    )
    assert isinstance(meta.bake_info, BakeInfo)
    assert meta.bake_info.adapters[0].path == "yard-voice.gguf"
    assert meta.bake_info.adapters[0].weight == 0.7


def test_bake_info_tolerates_a_null_quant_type():
    """`quant_type` is `req.quant_type or None` upstream — it really is nullable."""
    meta = ModelMeta(bake_info={"base_model": "b", "adapters": [], "outtype": "f16", "quant_type": None})
    assert meta.bake_info.quant_type is None
    assert meta.bake_info.adapters == []


def test_a_bake_adapter_without_a_path_is_rejected():
    """An adapter that names no file is not lineage, it is noise."""
    with pytest.raises(ValidationError):
        ModelMeta(bake_info={"base_model": "b", "adapters": [{"weight": 0.7}]})


def test_active_loras_parses_the_wire_shape_both_sides_use():
    """self.chat sends `{file, scale}`; `_ensure_loras_applied` reads `file` back."""
    meta = ModelMeta(active_loras=[{"file": "adapter.gguf", "scale": 0.7}, {"file": "b.gguf"}])
    assert all(isinstance(lora, ActiveLora) for lora in meta.active_loras)
    assert meta.active_loras[0].scale == 0.7
    assert meta.active_loras[1].scale == 1.0

    # `_ensure_loras_applied` sorts on `lora["file"]` after model_dump(); the
    # dumped form must still be plain dicts with that key.
    dumped = meta.model_dump()["active_loras"]
    assert sorted(dumped, key=lambda lora: lora.get("file", "")) == [
        {"file": "adapter.gguf", "scale": 0.7},
        {"file": "b.gguf", "scale": 1.0},
    ]


def test_an_active_lora_without_a_file_is_rejected():
    with pytest.raises(ValidationError):
        ModelMeta(active_loras=[{"scale": 0.7}])


@pytest.mark.parametrize("strategy", ["depth", "width", "depth_and_width"])
def test_prune_strategies(strategy):
    assert PruneRecipe(strategy=strategy).strategy == strategy


def test_an_unknown_prune_strategy_is_rejected():
    with pytest.raises(ValidationError):
        PruneRecipe(strategy="quantization")


# ---------------------------------------------------------------------------
# Backward compatibility against the shapes actually stored
# ---------------------------------------------------------------------------


def test_a_legacy_row_loads_unchanged(db_session):
    """The live shape: all-null lineage, no line membership, UI extras present."""
    _write_raw_row("legacy-model", dict(LEGACY_META))

    stored = Models.get_model_by_id("legacy-model")
    assert stored is not None, "a legacy row stopped loading"

    assert stored.meta.capabilities is None
    assert stored.meta.source_type is None
    assert stored.meta.bake_info is None
    assert stored.meta.active_loras is None
    assert stored.meta.distillation is None
    assert stored.meta.line_id is None
    assert stored.meta.corpus_repo is None
    assert stored.meta.corpus_commit_id is None
    assert stored.meta.corpus_weights_present is None

    # And the fields it *did* carry are untouched.
    assert stored.meta.profile_image_url == "/static/favicon.png"


def test_a_legacy_row_survives_the_list_endpoints_query(db_session):
    """`get_all_models` does not catch ValidationError.

    `get_model_by_id` swallows every exception and returns None, so it can hide
    a schema regression as a missing model. This path cannot: one unreadable row
    takes down the whole listing.
    """
    _write_raw_row("legacy-a", dict(LEGACY_META))
    _write_raw_row("legacy-b", {})
    _write_raw_row("legacy-c", {"description": "no lineage keys at all"})

    ids = {model.id for model in Models.get_all_models()}
    assert {"legacy-a", "legacy-b", "legacy-c"} <= ids


def test_undeclared_keys_survive_a_read_write_read_cycle(db_session):
    """The `extra="allow"` decision, pinned.

    self.chat's ModelEditor writes `knowledge`, `toolIds`, `filterIds`,
    `actionIds`, `audio`, `tags` and `suggestion_prompts` into this blob and
    none of them are declared. `extra="ignore"` would drop them on the next
    save; `extra="forbid"` would 422 the save outright. Both are data loss, so
    #140 keeps the extension channel and makes the *declared* fields
    authoritative instead.
    """
    extras = {
        "knowledge": [{"id": "kb-1", "name": "Yard docs"}],
        "toolIds": ["calculator"],
        "filterIds": ["f1"],
        "actionIds": ["a1"],
        "tags": [{"name": "yard"}],
        "audio": {"tts_voice": "craft:7d1c27df"},
        "suggestion_prompts": [{"content": "What is the yard?"}],
    }
    _write_raw_row("extras-model", {**LEGACY_META, **extras})

    stored = Models.get_model_by_id("extras-model")
    for key, value in extras.items():
        assert getattr(stored.meta, key) == value, f"extra key {key!r} was lost on read"

    # Now write it back the way the workspace editor does, and read it again.
    Models.update_model_by_id(
        "extras-model",
        ModelForm(
            id="extras-model",
            base_model_id=None,
            name="extras-model",
            meta=stored.meta,
            params=stored.params,
            access_control=None,
            is_active=True,
        ),
    )
    reread = Models.get_model_by_id("extras-model")
    for key, value in extras.items():
        assert getattr(reread.meta, key) == value, f"extra key {key!r} was lost on save"


def test_a_row_written_before_131_has_no_line_membership(db_session):
    """`line_id`/`version_id` are absent from every live row, not null in them.

    An added Optional field must read a row that predates it, which is the same
    guarantee the #140 fields need and the reason none of them is required.
    """
    _write_raw_row("pre-131", dict(LEGACY_META))
    stored = Models.get_model_by_id("pre-131")
    assert stored.meta.line_id is None
    assert stored.meta.version_id is None
    assert "line_id" not in LEGACY_META

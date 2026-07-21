"""cavekit-chat-folders-rag-config.md R1 (Typed Folder Preset Schema) — the
typed shape of a chat folder's preset: three optional named fields (a default
model reference, a list of tool references, a single list of knowledge
references) that tolerates and round-trips unrecognized extra keys for forward
compatibility."""

import pytest

from selfai_ui.models.folders import FolderPresetModel


@pytest.mark.tier0
def test_declares_exactly_three_named_fields():
    """R1 AC1 — the preset declares exactly three named fields: a default model
    reference, a list of tool references, and a list of knowledge references."""
    declared = set(FolderPresetModel.model_fields.keys())
    assert declared == {"default_model_id", "tool_ids", "knowledge_ids"}


@pytest.mark.tier0
def test_each_field_is_optional_empty_preset_carries_nothing():
    """R1 AC2 — each field is optional; a folder with none set carries no
    preset (all fields default to None)."""
    preset = FolderPresetModel()
    assert preset.default_model_id is None
    assert preset.tool_ids is None
    assert preset.knowledge_ids is None
    # A preset with nothing set serializes to all-None declared fields and no
    # extra keys — i.e. it carries no preset content.
    assert preset.model_dump() == {
        "default_model_id": None,
        "tool_ids": None,
        "knowledge_ids": None,
    }


@pytest.mark.tier0
def test_fields_accept_their_intended_reference_shapes():
    """R1 AC1 — the model reference is a single ref; tools and knowledge are
    lists of refs."""
    preset = FolderPresetModel(
        default_model_id="qwen2.5-coder-32b",
        tool_ids=["web_search", "code_interpreter"],
        knowledge_ids=["kb-alpha", "kb-beta"],
    )
    assert preset.default_model_id == "qwen2.5-coder-32b"
    assert preset.tool_ids == ["web_search", "code_interpreter"]
    assert preset.knowledge_ids == ["kb-alpha", "kb-beta"]


@pytest.mark.tier0
def test_knowledge_is_a_single_list_covering_bases_and_datasets():
    """R1 AC3 — attached knowledge is one list of knowledge references covering
    both knowledge bases and datasets, not two separate lists. There is no
    second dataset-specific declared field."""
    declared = set(FolderPresetModel.model_fields.keys())
    # exactly one knowledge-carrying field
    knowledge_fields = {f for f in declared if "knowledge" in f or "dataset" in f}
    assert knowledge_fields == {"knowledge_ids"}

    # A mix of knowledge-base ids and dataset ids all live in the one list.
    preset = FolderPresetModel(knowledge_ids=["kb-docs", "dataset-2026-curated"])
    assert preset.knowledge_ids == ["kb-docs", "dataset-2026-curated"]


@pytest.mark.tier0
def test_extra_keys_are_accepted_and_round_trip_intact():
    """R1 AC4 — a preset carrying keys beyond the three declared fields is
    accepted and round-trips those extra keys intact (forward compatibility),
    rather than being rejected or silently dropping them."""
    payload = {
        "default_model_id": "m1",
        "tool_ids": ["t1"],
        "knowledge_ids": ["k1"],
        # future preset attributes not yet declared:
        "system_prompt": "be terse",
        "temperature": 0.2,
        "nested_future": {"a": 1, "b": [2, 3]},
    }
    preset = FolderPresetModel(**payload)
    dumped = preset.model_dump()

    # extra keys survived the round-trip intact
    assert dumped["system_prompt"] == "be terse"
    assert dumped["temperature"] == 0.2
    assert dumped["nested_future"] == {"a": 1, "b": [2, 3]}

    # and the full payload round-trips without loss
    assert FolderPresetModel(**dumped).model_dump() == payload

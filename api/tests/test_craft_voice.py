"""Unit tests for crafted-voice → chat-TTS bridging (audio/craft_voice.py).

The embedding blend itself is GPU-only (validated live); these cover the pure
logic that decides, from a crafted voice's saved node graph, WHICH sample clips
at WHICH weights make up the voice for chat playback — plus the craft:<id>
namespacing the /speech router keys on.
"""

from types import SimpleNamespace

from selfai_ui.audio import craft_voice
from selfai_ui.audio.craft_voice import (
    craft_voice_uuid,
    is_craft_voice,
    resolve_blend_recipe,
)


def _sample_node(node_id, file_id):
    return {"id": node_id, "type": "reference-audio", "data": {"values": {"file": file_id}}}


def _shape_node(node_id, blend=None):
    values = {} if blend is None else {"blend": blend}
    return {"id": node_id, "type": "clone", "data": {"values": values}}


def _edge(source, target):
    return {"source": source, "target": target, "targetHandle": "ref"}


class TestNamespacing:
    def test_is_craft_voice(self):
        assert is_craft_voice("craft:abc-123") is True
        assert is_craft_voice("af_bella") is False
        assert is_craft_voice(None) is False
        assert is_craft_voice(123) is False

    def test_craft_voice_uuid(self):
        assert craft_voice_uuid("craft:abc-123") == "abc-123"


class TestResolveBlendRecipe:
    def test_two_samples_use_blend_slider(self):
        # Blend 0.3 → first clip weighted 0.7, second 0.3 (0=first, 1=second).
        graph = {
            "nodes": [
                _sample_node("s1", "fileA"),
                _sample_node("s2", "fileB"),
                _shape_node("shape", blend=0.3),
            ],
            "edges": [_edge("s1", "shape"), _edge("s2", "shape")],
        }
        voice = SimpleNamespace(id="v1", graph=graph)
        recipe = resolve_blend_recipe(voice)
        assert recipe == [("fileA", 0.7), ("fileB", 0.3)]

    def test_two_samples_default_blend_when_unset(self):
        graph = {
            "nodes": [
                _sample_node("s1", "fileA"),
                _sample_node("s2", "fileB"),
                _shape_node("shape"),  # no blend value
            ],
            "edges": [_edge("s1", "shape"), _edge("s2", "shape")],
        }
        recipe = resolve_blend_recipe(SimpleNamespace(id="v1", graph=graph))
        assert recipe == [("fileA", 0.5), ("fileB", 0.5)]

    def test_single_sample_is_a_clone(self):
        graph = {
            "nodes": [_sample_node("s1", "fileA"), _shape_node("shape", blend=0.9)],
            "edges": [_edge("s1", "shape")],
        }
        recipe = resolve_blend_recipe(SimpleNamespace(id="v1", graph=graph))
        assert recipe == [("fileA", 1.0)]

    def test_more_than_two_samples_equal_weights(self):
        graph = {
            "nodes": [
                _sample_node("s1", "fileA"),
                _sample_node("s2", "fileB"),
                _sample_node("s3", "fileC"),
                _shape_node("shape", blend=0.2),
            ],
            "edges": [_edge("s1", "shape"), _edge("s2", "shape"), _edge("s3", "shape")],
        }
        recipe = resolve_blend_recipe(SimpleNamespace(id="v1", graph=graph))
        assert recipe == [("fileA", 1.0), ("fileB", 1.0), ("fileC", 1.0)]

    def test_no_graph_falls_back_to_all_attached_files(self, monkeypatch):
        monkeypatch.setattr(
            craft_voice.VoiceFiles,
            "get_file_ids_by_voice_id",
            lambda vid: ["fx", "fy"],
        )
        recipe = resolve_blend_recipe(SimpleNamespace(id="v1", graph=None))
        assert recipe == [("fx", 1.0), ("fy", 1.0)]

    def test_graph_without_shape_falls_back_to_attached_files(self, monkeypatch):
        monkeypatch.setattr(
            craft_voice.VoiceFiles,
            "get_file_ids_by_voice_id",
            lambda vid: ["only"],
        )
        graph = {"nodes": [_sample_node("s1", "fileA")], "edges": []}
        recipe = resolve_blend_recipe(SimpleNamespace(id="v1", graph=graph))
        assert recipe == [("only", 1.0)]

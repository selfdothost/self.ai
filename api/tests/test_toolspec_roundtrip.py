"""ToolSpec against the shape actually stored in the DB, and the no-migration guard.

Cavekit: cavekit-toolspec-model.md R3 -- T-010 (criteria 2 and 3).

Tools are persisted as whatever ``get_tools_specs`` (``utils/tools.py:195``)
produces -- langchain's ``convert_to_openai_function`` over a pydantic model
generated from each tool function's signature and docstring. The round-trip
tests below run that real producer rather than a hand-written approximation of
its output, because the guarantee R3 needs is about the *actual* stored bytes:
``ToolSpec`` must read a stored spec back and write it out unchanged, so neither
the ``tool.specs`` column nor its contents ever migrate.

Finding, confirmed here rather than assumed: a ``__``-prefixed argument never
reaches ``properties`` at all. ``function_to_pydantic_model`` builds the schema
via pydantic v2 ``create_model``, which treats a leading-underscore name as a
private attribute, so injected arguments (``__id__``, ``__user__``,
``__event_emitter__``) are dropped before any schema exists -- required ones
included. The live ``__``-strip in ``get_tools()`` is therefore vestigial with
respect to this producer, and load-bearing only for specs that did not come from
it: hand-authored specs, mod-built specs, and rows persisted by older versions.
``test_a_dunder_prefixed_argument_never_reaches_the_stored_schema`` pins that.
"""

from pathlib import Path

import pytest

import selfai_ui
from selfai_ui.internal.db import JSONField
from selfai_ui.models.tools import Tool, ToolModel
from selfai_ui.utils.tools import get_tools_specs
from selfai_ui.utils.toolspec import ToolSpec


class FixtureTools:
    """A Tools class of the shape a user-authored toolkit has.

    Covers the four argument shapes the stored spec can take: none, required,
    optional-with-default, and ``__``-prefixed (injected by core, never shown
    to the model).
    """

    def ping(self) -> str:
        """Ping the service."""
        return "pong"

    def lookup(self, path: str) -> str:
        """
        Look something up.

        :param path: Where to look.
        """
        return path

    def search(self, query: str, limit: int = 10) -> str:
        """
        Search things.

        :param query: The search query.
        :param limit: How many results to return.
        """
        return query

    def emit(self, message: str, __id__: str, __event_emitter__: dict = None) -> str:
        """
        Emit a message.

        :param message: What to say.
        :param __id__: Injected by core; the model must never see it.
        """
        return message

    def undocumented(self, x: int) -> int:
        return x


# Produced once at import: this is the real langchain output, and every
# round-trip case below is parametrized over it so a new fixture function is
# covered automatically.
STORED_SPECS = get_tools_specs(FixtureTools())
STORED_IDS = [spec["name"] for spec in STORED_SPECS]


@pytest.mark.tier0
def test_the_fixture_actually_produced_a_spec_for_every_tool_function():
    # Guards the parametrized tests below against silently covering nothing if
    # get_tools_specs ever stops discovering these methods.
    assert set(STORED_IDS) == {"ping", "lookup", "search", "emit", "undocumented"}


@pytest.mark.tier0
@pytest.mark.parametrize("stored", STORED_SPECS, ids=STORED_IDS)
def test_from_openai_then_to_openai_returns_the_stored_dict_unchanged(stored):
    # R3 criterion 2: lossless for anything convert_to_openai_function emits.
    # This is what makes "no migration" true rather than merely intended.
    assert ToolSpec.from_openai(stored).to_openai() == stored


@pytest.mark.tier0
@pytest.mark.parametrize("stored", STORED_SPECS, ids=STORED_IDS)
def test_the_round_trip_does_not_mutate_or_alias_the_stored_dict(stored):
    # The stored dict is read straight off a cached Tools row on the live path,
    # so the round trip must neither edit it nor hand its schema body onward.
    before = {"name": stored["name"], "description": stored["description"], "parameters": stored["parameters"]}
    emitted = ToolSpec.from_openai(stored).to_openai()

    assert stored == before
    assert emitted["parameters"] is not stored["parameters"]


@pytest.mark.tier0
@pytest.mark.parametrize("stored", STORED_SPECS, ids=STORED_IDS)
def test_the_stored_shape_is_exactly_the_openai_function_field_set(stored):
    # If langchain ever starts emitting a fourth key, extra="forbid" turns
    # every stored spec into a validation error -- so pin what it emits today.
    assert set(stored) == {"name", "description", "parameters"}


@pytest.mark.tier0
def test_an_optional_argument_keeps_its_default_through_the_round_trip():
    stored = next(spec for spec in STORED_SPECS if spec["name"] == "search")

    emitted = ToolSpec.from_openai(stored).to_openai()

    assert emitted["parameters"]["properties"]["limit"]["default"] == 10
    assert emitted["parameters"]["required"] == ["query"]


@pytest.mark.tier0
def test_a_no_argument_function_round_trips_its_empty_properties_object():
    stored = next(spec for spec in STORED_SPECS if spec["name"] == "ping")

    assert stored["parameters"]["properties"] == {}
    assert ToolSpec.from_openai(stored).to_openai() == stored


@pytest.mark.tier0
def test_an_undocumented_function_round_trips_its_empty_description():
    stored = next(spec for spec in STORED_SPECS if spec["name"] == "undocumented")

    assert stored["description"] == ""
    assert ToolSpec.from_openai(stored).to_openai() == stored


@pytest.mark.tier0
def test_a_dunder_prefixed_argument_never_reaches_the_stored_schema():
    # Verified, not assumed. `emit` declares a REQUIRED __id__ and a defaulted
    # __event_emitter__; pydantic v2 treats leading-underscore names as private
    # attributes, so create_model drops both before a schema exists. Neither
    # appears in properties nor in required.
    stored = next(spec for spec in STORED_SPECS if spec["name"] == "emit")

    assert set(stored["parameters"]["properties"]) == {"message"}
    assert stored["parameters"]["required"] == ["message"]
    assert not any(key.startswith("__") for key in stored["parameters"]["properties"])

    # And so the strip is a no-op against this producer's output -- which is
    # exactly why it must stay in place for the producers that are not this one.
    assert ToolSpec.from_openai(stored).without_internal_params().to_openai() == stored


# --- R3 criterion 3: the storage contract does not move ----------------------

MIGRATIONS_DIR = Path(selfai_ui.__file__).resolve().parent / "migrations" / "versions"

# Every Alembic revision that existed before the ToolSpec work. The stored shape
# is unchanged, so this build must add none. If a revision is added here for
# unrelated work, confirm it does not touch `tool.specs` and then add it to this
# set -- the pin is a tripwire, not a freeze on the migration directory.
PINNED_REVISION_FILES = frozenset(
    {
        "1af9b942657b_migrate_tags.py",
        "242a2047eae0_update_chat_table.py",
        "3781e22d8b01_update_message_table.py",
        "3ab32c4b8f59_update_tags.py",
        # self.ai#134, the Studio permission rekey. Unrelated to ToolSpec and
        # confirmed not to touch `tool.specs`: it reads and rewrites only the
        # `permissions` JSON on the `group` table, moving a top-level
        # `workspace` key to `studio`. Added per this pin's own instruction.
        "3f7eff4c5814_rename_workspace_permission_to_studio.py",
        # Phase 2 of the Tokenization Studio programme. Unrelated to ToolSpec
        # and confirmed not to touch `tool.specs`: it is a single
        # `op.add_column("chat", "kind")`, additive and nullable, on the `chat`
        # table only. Added per this pin's own instruction.
        "d6e7f8a9b0c1_add_chat_kind.py",
        "4ace53fd72c8_update_folder_table_datetime.py",
        "57c599a3cb57_add_channel_table.py",
        "6a39f3d8e55c_add_knowledge_table.py",
        "7826ab40b532_update_file_table.py",
        "7e5b5dc7342b_init.py",
        "922e7a387820_add_group_table.py",
        "a1b2c3d4e5f6_add_training_tables.py",
        "a2b3c4d5e6f7_add_priority_to_jobs.py",
        "af906e964978_add_feedback_table.py",
        "b1c2d3e4f5a6_add_vram_consumer_device_occupancy.py",
        "b2c3d4e5f6a7_add_eval_job_table.py",
        "b3c4d5e6f7a8_create_curator_job_table.py",
        "b7e1c0ffee42_add_mod_reference_handles_table.py",
        "c0fbf31ca0db_update_file_table.py",
        "c1d2e3f4a5b6_add_voice_tables.py",
        "c2d3e4f5a6b7_add_vram_consumer_reservation.py",
        "c29facfe716b_update_file_table_path.py",
        "c3d4e5f6a7b8_add_eval_type_to_eval_job.py",
        "c4d5e6f7a8b9_create_job_window_tables.py",
        "c69f45358db4_add_folder_table.py",
        "ca81bd47c050_add_config_table.py",
        "d4e5f6a7b8c9_add_scheduled_for_to_training_job.py",
        "d5e6f7a8b9c0_create_benchmark_config_table.py",
        # Unrelated work (eval catalog, self.ai#90): renames/expands rows in
        # benchmark_config so the seeded names match what the harnesses can
        # actually schedule. Reads and writes benchmark_config only — no DDL,
        # never touches tool.specs. Pinned per this test's own escape hatch.
        "e1f2a3b4c5d6_reseed_benchmark_config_real_task_names.py",
        # Unrelated work (eval catalog Phase 2, self.ai#91): creates only the
        # custom_eval registry table, never touches tool.specs. Pinned per this
        # test's own escape hatch.
        "f2a3b4c5d6e7_add_custom_eval_table.py",
        # self.ai#93 — creates the backup_job table. Unrelated to ToolSpec: it
        # adds a new table and does not touch `tool` or `tool.specs`.
        "f3a7c2d1e8b9_add_backup_job_table.py",
        # Unrelated work (GPU VRAM-lease broker): creates only the vram_consumer
        # table, never touches tool.specs. Pinned per this test's own escape hatch.
        "e7d2a9c1f3b0_add_vram_consumer_table.py",
        # Same broker, R5 force-reap: adds two nullable k8s-identity columns to
        # vram_consumer, never touches tool.specs. Pinned per the escape hatch.
        "f7c3b1a2d4e5_add_vram_consumer_k8s_identity.py",
        # Same broker, Decision 6 R1: adds the lease_mode column to vram_consumer
        # (exclusive-lease posture), never touches tool.specs. Pinned per escape hatch.
        "f8a9b0c1d2e3_add_vram_consumer_lease_mode.py",
        # Same broker, Decision 6 R4: adds loaded-model columns to vram_consumer
        # (eval-coexist datum), never touches tool.specs. Pinned per escape hatch.
        "a9b0c1d2e3f4_add_vram_consumer_loaded_model.py",
        # Same broker, self.ai#105: adds the nullable last_observed_at column to
        # vram_consumer (never-observed force-reap guard), never touches
        # tool.specs. Pinned per escape hatch.
        "d7e8f9a0b1c2_add_vram_consumer_last_observed_at.py",
        "e5f6a7b8c9d0_move_files_to_kb_subdirs.py",
        "f6a7b8c9d0e1_add_knowledge_file_table.py",
        # Unrelated work (self.crew#141): creates only the crew mod's own
        # mod_crew_sessions table, enablement-gated, never touches tool.specs.
        # Pinned per this test's own escape hatch.
        "d2e3f4a5b6c7_add_mod_crew_sessions_table.py",
        # Unrelated work (self.ai#25): creates only the mcp_backend table for
        # dynamic MCP front-door registration. New table, no ALTER of anything,
        # never touches tool.specs. Pinned per this test's own escape hatch.
        "c9d8e7f6a5b4_add_mcp_backend_table.py",
        # Unrelated work (self.ai#131): creates only model_line and
        # model_version for model versioning. Two new tables, no ALTER of
        # anything — deliberately additive, not even to the `model` table it
        # sits beside. Never touches tool.specs. Pinned per this test's own
        # escape hatch.
        "b4c5d6e7f8a9_add_model_line_and_version.py",
        # Unrelated work (self.ai#131 R6): creates only the publish_job table.
        # New table, no ALTER of anything, never touches tool.specs. Pinned per
        # this test's own escape hatch.
        "c5d6e7f8a9b0_add_publish_job.py",
    }
)


@pytest.mark.tier0
def test_the_tool_specs_column_is_still_a_jsonfield():
    column = Tool.__table__.columns["specs"]

    assert isinstance(column.type, JSONField)


@pytest.mark.tier0
def test_the_tool_model_still_declares_specs_as_a_list_of_dicts():
    # `list[dict]`, not `list[ToolSpec]`: the persisted value stays the OpenAI
    # dict, and the typed model is constructed on read.
    assert ToolModel.model_fields["specs"].annotation == list[dict]


@pytest.mark.tier0
def test_no_alembic_revision_was_introduced_for_toolspec():
    present = {path.name for path in MIGRATIONS_DIR.glob("*.py")}

    added = present - PINNED_REVISION_FILES
    removed = PINNED_REVISION_FILES - present

    assert not added, (
        f"New Alembic revision(s) present: {sorted(added)}. The ToolSpec work "
        f"stores the same OpenAI dict in the same JSONField and must introduce "
        f"no migration (R3 criterion 3). If this revision is unrelated work, "
        f"confirm it does not touch tool.specs and add it to PINNED_REVISION_FILES."
    )
    assert not removed, f"Alembic revision(s) disappeared: {sorted(removed)}."


@pytest.mark.tier0
def test_only_the_root_migration_mentions_the_tool_specs_column():
    # A cheaper complement to the filename pin: even a revision added for
    # unrelated work must not touch this column.
    mentioning = sorted(
        path.name
        for path in MIGRATIONS_DIR.glob("*.py")
        if '"specs"' in path.read_text() or "'specs'" in path.read_text()
    )

    assert mentioning == ["7e5b5dc7342b_init.py"], (
        f"Migrations referencing a `specs` column: {mentioning}. Only the root "
        f"migration should create tool.specs; the ToolSpec work does not alter it."
    )

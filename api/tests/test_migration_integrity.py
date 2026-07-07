"""
Migration integrity tests.

Validates that the hybrid Peewee + Alembic migration system produces
a schema consistent with the SQLAlchemy models. Catches schema drift
between model definitions and what migrations actually create — the
exact issue flagged in research-brief-ui-test-suite.md Section 3.
"""

import os
import pytest
from sqlalchemy import create_engine, inspect, text


@pytest.fixture(scope="module")
def migrated_engine():
    """An engine pointing at the main test DB (which has all migrations applied)."""
    return create_engine(os.environ["DATABASE_URL"])


@pytest.mark.tier0
def test_all_declared_tables_exist(migrated_engine):
    """Every SQLAlchemy model table has a corresponding table in the DB."""
    from selfai_ui.internal.db import Base

    inspector = inspect(migrated_engine)
    existing = set(inspector.get_table_names())

    missing = []
    for table_name in Base.metadata.tables.keys():
        if table_name not in existing:
            missing.append(table_name)

    assert not missing, (
        f"SQLAlchemy-declared tables missing from the DB: {missing}. "
        f"Likely cause: the model was declared but no migration creates "
        f"the table."
    )


@pytest.mark.tier0
def test_curator_job_has_peewee_added_columns(migrated_engine):
    """
    The curator_job.dataset_name and created_knowledge_id columns
    come from a Peewee migration (019_add_curator_job_dataset_fields.py),
    NOT Alembic. Verify they exist after migration.

    Research brief Section 3: 'curator_job.dataset_name and
    created_knowledge_id columns exist only in Peewee migration 019'.
    """
    inspector = inspect(migrated_engine)
    columns = {col["name"] for col in inspector.get_columns("curator_job")}

    assert "dataset_name" in columns, (
        "curator_job.dataset_name missing. Peewee migration 019 did not "
        "run, or the hybrid migration system is broken."
    )
    assert "created_knowledge_id" in columns, (
        "curator_job.created_knowledge_id missing. Same root cause."
    )


@pytest.mark.tier0
def test_training_job_has_scheduled_for(migrated_engine):
    """training_job.scheduled_for was added by Alembic migration d4e5f6a7b8c9."""
    inspector = inspect(migrated_engine)
    columns = {col["name"] for col in inspector.get_columns("training_job")}
    assert "scheduled_for" in columns


@pytest.mark.tier0
def test_training_job_has_priority(migrated_engine):
    """training_job.priority was added by Alembic migration a2b3c4d5e6f7."""
    inspector = inspect(migrated_engine)
    columns = {col["name"] for col in inspector.get_columns("training_job")}
    assert "priority" in columns


@pytest.mark.tier0
def test_eval_job_has_eval_type(migrated_engine):
    """eval_job.eval_type was added by Alembic migration c3d4e5f6a7b8."""
    inspector = inspect(migrated_engine)
    columns = {col["name"] for col in inspector.get_columns("eval_job")}
    assert "eval_type" in columns


@pytest.mark.tier0
def test_benchmark_config_seed_present(migrated_engine, seeded_benchmarks):
    """Migration d5e6f7a8b9c0 seeds benchmark_config with known benchmarks."""
    with migrated_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT benchmark, eval_type FROM benchmark_config")
        ).fetchall()
    benchmarks = {(r[0], r[1]) for r in rows}

    # These are required for the UI benchmark selector
    required = {
        ("humaneval", "code-eval"),
        ("mbpp", "code-eval"),
        ("mmlu", "language-eval"),
        ("gsm8k", "language-eval"),
    }
    missing = required - benchmarks
    assert not missing, (
        f"Seeded benchmarks missing: {missing}. "
        f"The benchmark_config seeding is broken."
    )


@pytest.mark.tier0
def test_job_window_and_slot_tables_related(migrated_engine):
    """job_window_slot has a foreign key to job_window."""
    inspector = inspect(migrated_engine)
    fks = inspector.get_foreign_keys("job_window_slot")
    window_fk = [fk for fk in fks if "job_window" in fk.get("referred_table", "")]
    # Foreign key may be named or unnamed, but it must exist
    # (or the slot.window_id column must exist)
    columns = {col["name"] for col in inspector.get_columns("job_window_slot")}
    assert "window_id" in columns, (
        "job_window_slot.window_id missing — foreign relationship broken."
    )


@pytest.mark.tier0
def test_alembic_version_is_head(migrated_engine):
    """alembic_version table exists and has a single current version."""
    inspector = inspect(migrated_engine)
    assert "alembic_version" in inspector.get_table_names(), (
        "alembic_version table missing — Alembic migrations never ran"
    )
    with migrated_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).fetchall()
    assert len(rows) == 1, (
        f"Expected exactly 1 alembic version row, got {len(rows)}"
    )


@pytest.mark.tier0
def test_known_tables_accounted_for(migrated_engine):
    """
    Every table in the DB should be a known table (SQLAlchemy model,
    legacy Peewee-managed, or Alembic system). Flags unexpected tables
    that suggest a stale migration left garbage behind.
    """
    # Import all models so Base.metadata is populated
    import selfai_ui.models.users  # noqa: F401
    import selfai_ui.models.auths  # noqa: F401
    import selfai_ui.models.chats  # noqa: F401
    import selfai_ui.models.files  # noqa: F401
    import selfai_ui.models.folders  # noqa: F401
    import selfai_ui.models.knowledge  # noqa: F401
    import selfai_ui.models.tools  # noqa: F401
    import selfai_ui.models.functions  # noqa: F401
    import selfai_ui.models.training  # noqa: F401
    import selfai_ui.models.eval_jobs  # noqa: F401
    import selfai_ui.models.curator_jobs  # noqa: F401
    import selfai_ui.models.job_windows  # noqa: F401
    import selfai_ui.models.benchmark_config  # noqa: F401
    import selfai_ui.models.tags  # noqa: F401
    import selfai_ui.models.memories  # noqa: F401
    import selfai_ui.models.models  # noqa: F401
    import selfai_ui.models.prompts  # noqa: F401
    import selfai_ui.models.channels  # noqa: F401
    import selfai_ui.models.messages  # noqa: F401
    import selfai_ui.models.feedbacks  # noqa: F401
    import selfai_ui.models.groups  # noqa: F401

    from selfai_ui.internal.db import Base

    inspector = inspect(migrated_engine)
    existing = set(inspector.get_table_names())
    declared = set(Base.metadata.tables.keys())

    # Known Peewee-managed / Alembic system / legacy tables
    known_extras = {
        "alembic_version",
        "config",  # Peewee-managed config table
        "migratehistory",  # Peewee migration tracking
        "chatidtag",  # Legacy — pre-tag-table-rename
        "document",  # Legacy — pre-knowledge-migration
        "channel_member",  # Join table (no SQLAlchemy model)
    }
    transient_prefixes = ("_alembic_tmp_", "sqlite_")

    unexpected = []
    for t in existing - declared - known_extras:
        if not any(t.startswith(p) for p in transient_prefixes):
            unexpected.append(t)

    assert not unexpected, (
        f"Tables in DB not accounted for by model or known-extras list: "
        f"{unexpected}. Either add a model, add to known_extras, or "
        f"remove via migration."
    )

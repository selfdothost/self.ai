"""The model's column types must match the migration's — self.ai#131.

THIS FILE EXISTS BECAUSE ITS ABSENCE SHIPPED A BUG TO PRODUCTION.

`b4c5d6e7f8a9` and `c5d6e7f8a9b0` created real `sa.JSON()` columns. The models
declared `internal.db.JSONField`, which is a TypeDecorator over **Text** that
calls `json.loads()` on read. Those are not interchangeable:

* on **SQLite**, `sa.JSON` is stored as TEXT and the driver hands back a string,
  so `json.loads()` succeeds and everything looks fine;
* on **PostgreSQL**, psycopg hands back a `dict`, and `json.loads(dict)` raises
  ``the JSON object must be str, bytes or bytearray, not dict``.

Every test in this repo runs on SQLite. So the whole suite passed, CI passed,
the smoke job passed, and the first dict written to the live Postgres — a
version's `produced_by` — failed. A test comparing model declarations against
the migrated schema catches it **on SQLite**, because the reflected column type
is JSON either way while the model claimed Text.

The rule this pins is general: a JSON column's model declaration and its
migration must agree. Both spellings exist in this codebase for good historical
reasons; mixing them within one column is the defect.
"""

import pytest
from sqlalchemy import JSON, inspect

from selfai_ui.internal.db import JSONField
from selfai_ui.models.model_versions import ModelLine, ModelVersion, PublishJob

#: Every JSON-ish column these tables own, by table and column name.
JSON_COLUMNS = [
    (ModelLine, "access_control"),
    (ModelLine, "meta"),
    (ModelVersion, "produced_by"),
    (ModelVersion, "meta"),
    (PublishJob, "adapter_version_ids"),
    (PublishJob, "meta"),
]


@pytest.mark.parametrize("model,column", JSON_COLUMNS, ids=lambda v: getattr(v, "__name__", v))
def test_json_columns_are_sa_json_not_jsonfield(model, column):
    """JSONField over a real JSON column is the production failure above."""
    declared = model.__table__.columns[column].type

    assert not isinstance(declared, JSONField), (
        f"{model.__tablename__}.{column} is declared JSONField (Text + json.loads) but the "
        f"migration created a real JSON column. That works on SQLite and raises "
        f"'the JSON object must be str, bytes or bytearray, not dict' on PostgreSQL."
    )
    assert isinstance(declared, JSON)


@pytest.mark.parametrize("model,column", JSON_COLUMNS, ids=lambda v: getattr(v, "__name__", v))
def test_declared_type_matches_the_migrated_schema(db_session, model, column):
    """Reflect what Alembic actually created and compare it to the declaration.

    This is the check that would have caught the bug on SQLite: the reflected
    type is JSON either way, so a Text-backed declaration disagrees visibly.
    """
    reflected = {c["name"]: c["type"] for c in inspect(db_session.bind).get_columns(model.__tablename__)}
    assert column in reflected, f"{model.__tablename__}.{column} is missing from the migrated schema"

    declared = model.__table__.columns[column].type
    assert type(reflected[column]).__name__ == type(declared).__name__, (
        f"{model.__tablename__}.{column}: migration created "
        f"{type(reflected[column]).__name__}, model declares {type(declared).__name__}"
    )


def test_a_dict_round_trips_through_every_json_column(db_session):
    """The write that actually failed in production, exercised end to end."""
    from selfai_ui.models.model_versions import (
        ModelLineForm,
        ModelLines,
        ModelVersionForm,
        ModelVersions,
        PublishJobForm,
        PublishJobs,
    )

    line = ModelLines.insert_new_line(
        "u1", ModelLineForm(name="json-round-trip", corpus_repo="selfai-line-x", meta={"seeded": True})
    )
    assert ModelLines.get_line_by_id(line.id).meta == {"seeded": True}

    base = ModelVersions.insert_new_version(
        line.id,
        ModelVersionForm(
            kind="base",
            corpus_commit_id="c1",
            artifact_ref="base.gguf",
            produced_by={"job_kind": "smoke", "job_id": "1"},
            meta={"nested": {"deep": [1, 2, 3]}},
        ),
    )
    stored = ModelVersions.get_version_by_id(base.id)
    assert stored.produced_by == {"job_kind": "smoke", "job_id": "1"}
    assert stored.meta == {"nested": {"deep": [1, 2, 3]}}

    adapter = ModelVersions.insert_new_version(
        line.id,
        ModelVersionForm(
            kind="adapter",
            corpus_commit_id="c2",
            parent_version_id=base.id,
            base_version_id=base.id,
            artifact_ref="ada.gguf",
        ),
    )

    job = PublishJobs.insert_new_job(line.id, "u1", base.id, [adapter.id], PublishJobForm(output_name="v2.gguf"))
    assert PublishJobs.get_job_by_id(job.id).adapter_version_ids == [adapter.id]

    # The revert audit trail is a dict written into line.meta — the other write
    # that would have failed on Postgres.
    moved = ModelLines.set_current_version(line.id, base.id, moved_by="u1")
    assert moved.meta["version_moves"][-1]["to"] == base.id

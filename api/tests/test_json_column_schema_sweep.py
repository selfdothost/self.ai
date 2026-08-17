"""Every JSON column in the codebase must agree with its migration — self.ai#131.

`tests/test_model_versions_schema.py` pins this rule for the six columns that
actually broke. This file generalises it to **every table the app declares**,
so the next JSON column to get the spelling wrong fails in CI rather than on
the first dict written to the live Postgres.

The rule, restated: `internal.db.JSONField` is a `TypeDecorator` over **Text**
that calls `json.loads()` on read; `sa.JSON` is a real JSON column. A column
must use the same spelling in the model as in the migration that created it.
Mixing them survives SQLite (which hands back a string either way, so
`json.loads()` succeeds) and raises on PostgreSQL, where psycopg hands back a
`dict` and `json.loads(dict)` fails with ``the JSON object must be str, bytes
or bytearray, not dict``. Every test in this repo runs on SQLite, so the
mismatch ships green through `test:api` and `smoke:combined`.

Neither spelling is wrong on its own. Both exist here for good historical
reasons — the inherited tables are Text-backed and consistent, the newer ones
are real JSON and consistent. **The defect is disagreement within one column**,
which is what these tests detect.

Discovery is by import, not by a hand-written list of models: a new model file
joins the sweep by existing. A list someone has to remember to update is the
same failure mode as the missing test was.
"""

import importlib
import pkgutil

import pytest
from sqlalchemy import JSON, inspect
from sqlalchemy.types import TypeDecorator

import selfai_ui.models
from selfai_ui.internal.db import Base, JSONField


def _import_every_model_module():
    """Import all of `selfai_ui.models` so `Base.metadata` holds every table."""
    for module in pkgutil.iter_modules(selfai_ui.models.__path__):
        importlib.import_module(f"{selfai_ui.models.__name__}.{module.name}")


_import_every_model_module()


def _storage_type(type_):
    """Resolve a type down to what is actually stored in the database.

    `JSONField` is a TypeDecorator, so its declared type says nothing about
    storage until the decorator is unwrapped — `JSONField` stores Text. This
    is the whole trick: comparing decorator names against reflected names
    would flag every consistent legacy column, and comparing nothing would
    flag none of them.
    """
    seen = set()
    while isinstance(type_, TypeDecorator):
        if id(type_) in seen:  # pathological self-referential impl
            break
        seen.add(id(type_))
        impl = type_.impl
        type_ = impl() if isinstance(impl, type) else impl
    return type_


def _type_name(type_):
    """Comparable name for a type. Reflection yields dialect classes (`TEXT`),
    declarations yield generic ones (`Text`); only the spelling differs."""
    return type(type_).__name__.upper()


def _is_json_ish(type_):
    """True for both spellings of "this column holds JSON"."""
    return isinstance(type_, (JSON, JSONField))


def _declared_json_columns():
    """(table, column) for every JSON-ish column the models declare."""
    return [
        (table.name, column.name)
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if _is_json_ish(column.type)
    ]


DECLARED_JSON_COLUMNS = _declared_json_columns()

#: A floor, not a target. If discovery breaks — a rename, a package move, an
#: import that silently fails — the parametrized tests below collect zero cases
#: and pass while checking nothing. This is the tripwire for that.
MINIMUM_EXPECTED_JSON_COLUMNS = 20


def test_the_sweep_actually_finds_json_columns():
    """A sweep that finds nothing passes for the wrong reason."""
    assert len(DECLARED_JSON_COLUMNS) >= MINIMUM_EXPECTED_JSON_COLUMNS, (
        f"only {len(DECLARED_JSON_COLUMNS)} JSON columns discovered across "
        f"{len(Base.metadata.sorted_tables)} tables — model discovery is broken, "
        f"so the checks below are vacuous"
    )


@pytest.mark.parametrize(
    "table_name,column_name",
    DECLARED_JSON_COLUMNS,
    ids=[f"{t}.{c}" for t, c in DECLARED_JSON_COLUMNS],
)
def test_declared_json_columns_match_the_migrated_schema(test_engine, table_name, column_name):
    """The model's storage type must equal what Alembic actually created.

    This is the check that catches the #131 bug **on SQLite**: the migration
    made a real JSON column, the model declared JSONField (Text), and the
    reflected type disagrees visibly with the resolved declaration.
    """
    reflected = {c["name"]: c["type"] for c in inspect(test_engine).get_columns(table_name)}
    assert column_name in reflected, f"{table_name}.{column_name} is declared but missing from the migrated schema"

    declared = _storage_type(Base.metadata.tables[table_name].columns[column_name].type)
    assert _type_name(reflected[column_name]) == _type_name(declared), (
        f"{table_name}.{column_name}: the migration created "
        f"{_type_name(reflected[column_name])}, the model stores {_type_name(declared)}. "
        f"A JSONField (Text + json.loads) over a real JSON column works on SQLite and "
        f"raises 'the JSON object must be str, bytes or bytearray, not dict' on PostgreSQL."
    )


def test_no_migrated_json_column_is_declared_as_plain_text(test_engine):
    """The other direction: migration says JSON, model says Text and nothing else.

    Such a column is not JSON-ish by declaration, so the parametrized sweep
    above never sees it — but reading it on PostgreSQL still hands back a dict
    to code that expects a string.
    """
    inspector = inspect(test_engine)
    migrated_tables = set(inspector.get_table_names())

    undeclared_json = []
    for table in Base.metadata.sorted_tables:
        if table.name not in migrated_tables:
            continue
        reflected = {c["name"]: c["type"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name not in reflected:
                continue
            if _type_name(reflected[column.name]) == "JSON" and not _is_json_ish(column.type):
                undeclared_json.append(f"{table.name}.{column.name} (model declares {_type_name(column.type)})")

    assert not undeclared_json, "migrated as JSON but not declared as JSON in the model: " + ", ".join(undeclared_json)

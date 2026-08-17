"""The document_chunk vector index is opt-in (self.ai#62).

It used to be created unconditionally at PgvectorClient startup with a hardcoded
`lists = 100`. Measured on yard-pg 2026-07-23: 174 rows, planner chose a seq scan
every time, index was 4792 kB -- ~72% of the table's total footprint -- for a
structure nothing read and every insert maintained. Top-10 results were
byte-identical against a forced exact scan, so it was dead weight, not a
correctness bug.

The reason this needed a code change rather than a one-off `DROP INDEX`: the
CREATE ran on EVERY startup, so the next API pod restart put the index straight
back.

These test `_vector_index_ddl()` rather than PgvectorClient.__init__, which
would need a live Postgres with the vector extension. The helper exists to make
this decision testable in isolation; the __init__ side is one `if`.
"""

import pytest

from selfai_ui.retrieval.vector.dbs import pgvector as pgvector_mod


@pytest.fixture
def gate(monkeypatch):
    """Set the two flags as module globals, which is how the helper reads them."""

    def _set(enabled, lists=100):
        monkeypatch.setattr(pgvector_mod, "PGVECTOR_CREATE_VECTOR_INDEX", enabled)
        monkeypatch.setattr(pgvector_mod, "PGVECTOR_IVFFLAT_LISTS", lists)

    return _set


class TestDisabledByDefault:
    def test_returns_none_when_disabled(self, gate):
        """THE REGRESSION: this is the path that used to emit DDL regardless."""
        gate(False)

        assert pgvector_mod._vector_index_ddl() is None

    def test_lists_setting_is_irrelevant_when_disabled(self, gate):
        """A stray lists value must not resurrect the index."""
        gate(False, lists=1)

        assert pgvector_mod._vector_index_ddl() is None

    def test_shipped_default_is_off(self):
        """Guards the DEFAULT, not just the plumbing -- a gate that ships On is
        the same bug with extra steps. Read from config rather than the module
        global so this fails if the env default is flipped."""
        from selfai_ui import config

        assert config.PGVECTOR_CREATE_VECTOR_INDEX is False


class TestEnabled:
    def test_emits_ivfflat_ddl_when_enabled(self, gate):
        gate(True)
        ddl = pgvector_mod._vector_index_ddl()

        assert ddl is not None
        assert "CREATE INDEX IF NOT EXISTS idx_document_chunk_vector" in ddl
        assert "USING ivfflat (vector vector_cosine_ops)" in ddl

    def test_lists_is_configurable_not_hardcoded(self, gate):
        """The old DDL hardcoded lists = 100, which is wrong for any corpus this
        size (pgvector guidance is ~rows/1000). If it is going to exist, the
        operator must be able to size it."""
        gate(True, lists=1)
        ddl = pgvector_mod._vector_index_ddl()

        assert "WITH (lists = 1);" in ddl
        assert "100" not in ddl

    def test_ddl_is_idempotent(self, gate):
        """It runs on every startup, so it must stay IF NOT EXISTS."""
        gate(True)

        assert "IF NOT EXISTS" in pgvector_mod._vector_index_ddl()


class TestCollectionNameIndexIsUnaffected:
    def test_gate_does_not_mention_the_collection_name_index(self, gate):
        """Only the VECTOR index is gated. The btree on collection_name backs
        the equality filter every query applies and is left creating
        unconditionally -- this test exists so a future 'tidy-up' does not sweep
        it in alongside."""
        gate(True)
        ddl = pgvector_mod._vector_index_ddl()

        assert "collection_name" not in ddl

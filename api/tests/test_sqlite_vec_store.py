"""sqlite-vec vector store — the backend the zero-config quickstart runs on.

There were no vector-store tests of any kind before this file; chroma and
pgvector both shipped untested. That is the main reason this suite is thorough
rather than token: the quickstart is the publicly recommended deployment, and a
vector store that returns the *wrong* documents fails silently -- RAG just gets
quietly worse, with no error anywhere.

The test that matters most is collection isolation. vec0's KNN searches the
whole table, so without the PARTITION KEY a query against a small collection is
crowded out by a larger neighbour's vectors and returns another knowledge
base's documents. `test_search_does_not_leak_across_collections` fails loudly
if that partitioning is ever dropped.
"""

import json
import sqlite3

import pytest

from selfai_ui.retrieval.vector.dbs import sqlite_vec as sqlite_vec_module
from selfai_ui.retrieval.vector.dbs.sqlite_vec import SqliteVecClient

DIM = 4


def item(id_, text, vector, metadata=None):
    """Callers pass plain dicts, not VectorItem models -- match them exactly."""
    return {"id": id_, "text": text, "vector": vector, "metadata": metadata or {}}


@pytest.fixture
def client(tmp_path, monkeypatch):
    # The client reads these as module globals at construction/SQL-build time.
    monkeypatch.setattr(sqlite_vec_module, "SQLITE_VEC_PATH", str(tmp_path / "vec" / "store.db"))
    monkeypatch.setattr(sqlite_vec_module, "VECTOR_LENGTH", DIM)
    c = SqliteVecClient()
    yield c
    c.close()


@pytest.mark.tier0
def test_insert_and_get_round_trip(client):
    client.insert("kb", [item("a", "alpha", [1, 0, 0, 0], {"file_id": "f1"})])

    result = client.get("kb")

    assert result.ids[0] == ["a"]
    assert result.documents[0] == ["alpha"]
    assert result.metadatas[0] == [{"file_id": "f1"}]


@pytest.mark.tier0
def test_search_ranks_by_cosine_distance(client):
    client.insert(
        "kb",
        [
            item("exact", "exact match", [1, 0, 0, 0]),
            item("near", "nearly", [0.9, 0.1, 0, 0]),
            item("far", "orthogonal", [0, 1, 0, 0]),
        ],
    )

    result = client.search("kb", [[1, 0, 0, 0]], limit=3)

    assert result.ids[0] == ["exact", "near", "far"]
    assert result.distances[0][0] == pytest.approx(0.0, abs=1e-6)
    assert result.distances[0] == sorted(result.distances[0])
    assert result.documents[0][0] == "exact match"


@pytest.mark.tier0
def test_search_does_not_leak_across_collections(client):
    """The reason the vec0 table has a PARTITION KEY.

    'other' holds a vector strictly closer to the query than anything in 'kb'.
    An unpartitioned KNN returns it; a correct one never sees it. Without this
    guard the failure is invisible -- one knowledge base answers with another's
    documents and nothing raises.
    """
    client.insert("kb", [item("kb-1", "kb document", [0, 1, 0, 0])])
    client.insert("other", [item("other-1", "other document", [1, 0, 0, 0])])

    result = client.search("kb", [[1, 0, 0, 0]], limit=5)

    assert result.ids[0] == ["kb-1"]
    assert "other-1" not in result.ids[0]
    assert result.documents[0] == ["kb document"]


@pytest.mark.tier0
def test_search_handles_multiple_query_vectors(client):
    client.insert(
        "kb",
        [item("x", "x doc", [1, 0, 0, 0]), item("y", "y doc", [0, 1, 0, 0])],
    )

    result = client.search("kb", [[1, 0, 0, 0], [0, 1, 0, 0]], limit=1)

    assert len(result.ids) == 2
    assert result.ids[0] == ["x"]
    assert result.ids[1] == ["y"]


@pytest.mark.tier0
def test_search_empty_collection_returns_empty_not_none(client):
    result = client.search("nothing-here", [[1, 0, 0, 0]], limit=3)

    assert result is not None
    assert result.ids == [[]]


@pytest.mark.tier0
def test_upsert_replaces_without_duplicating(client):
    client.insert("kb", [item("a", "original", [1, 0, 0, 0])])
    client.upsert("kb", [item("a", "revised", [0, 1, 0, 0], {"v": 2})])

    result = client.get("kb")
    assert result.ids[0] == ["a"]
    assert result.documents[0] == ["revised"]
    assert result.metadatas[0] == [{"v": 2}]

    # The embedding must move too, not just the text.
    assert client.search("kb", [[0, 1, 0, 0]], limit=1).distances[0][0] == pytest.approx(0.0, abs=1e-6)


@pytest.mark.tier0
def test_upsert_inserts_when_absent(client):
    client.upsert("kb", [item("new", "fresh", [1, 0, 0, 0])])

    assert client.get("kb").ids[0] == ["new"]


@pytest.mark.tier0
def test_query_filters_on_metadata(client):
    client.insert(
        "kb",
        [
            item("a", "first", [1, 0, 0, 0], {"file_id": "f1"}),
            item("b", "second", [0, 1, 0, 0], {"file_id": "f2"}),
        ],
    )

    result = client.query("kb", filter={"file_id": "f2"})

    assert result.ids[0] == ["b"]
    assert result.documents[0] == ["second"]


@pytest.mark.tier0
def test_query_coerces_filter_values_to_text(client):
    """Matches the pgvector client, which compares `vmetadata[key].astext`.

    Caught a real bug: json_extract returns JSON *types*, so an uncast
    `json_extract(...) = '3'` matched nothing for `{"page": 3}` -- a filter that
    silently returns no rows rather than failing.
    """
    client.insert("kb", [item("a", "numeric meta", [1, 0, 0, 0], {"page": 3})])

    assert client.query("kb", filter={"page": 3}).ids[0] == ["a"]
    assert client.query("kb", filter={"page": "3"}).ids[0] == ["a"]


@pytest.mark.tier0
def test_query_filters_on_boolean_metadata(client):
    """JSON true/false surface as 1/0 through json_extract, not 'true'/'false'."""
    client.insert(
        "kb",
        [
            item("yes", "enabled", [1, 0, 0, 0], {"active": True}),
            item("no", "disabled", [0, 1, 0, 0], {"active": False}),
        ],
    )

    assert client.query("kb", filter={"active": True}).ids[0] == ["yes"]
    assert client.query("kb", filter={"active": False}).ids[0] == ["no"]


@pytest.mark.tier0
def test_delete_by_numeric_metadata_filter(client):
    """delete() and query() must coerce filter values identically.

    They are separate code paths; fixing one and not the other would leave
    deletes quietly matching nothing.
    """
    client.insert(
        "kb",
        [
            item("a", "keep", [1, 0, 0, 0], {"page": 1}),
            item("b", "drop", [0, 1, 0, 0], {"page": 2}),
        ],
    )

    client.delete("kb", filter={"page": 2})

    assert client.get("kb").ids[0] == ["a"]


@pytest.mark.tier0
def test_delete_by_ids(client):
    client.insert("kb", [item("a", "keep", [1, 0, 0, 0]), item("b", "drop", [0, 1, 0, 0])])

    client.delete("kb", ids=["b"])

    assert client.get("kb").ids[0] == ["a"]


@pytest.mark.tier0
def test_delete_by_metadata_filter(client):
    client.insert(
        "kb",
        [
            item("a", "keep", [1, 0, 0, 0], {"file_id": "f1"}),
            item("b", "drop", [0, 1, 0, 0], {"file_id": "f2"}),
        ],
    )

    client.delete("kb", filter={"file_id": "f2"})

    assert client.get("kb").ids[0] == ["a"]


@pytest.mark.tier0
def test_delete_collection_leaves_other_collections_intact(client):
    client.insert("kb", [item("a", "kb doc", [1, 0, 0, 0])])
    client.insert("other", [item("b", "other doc", [0, 1, 0, 0])])

    client.delete_collection("kb")

    assert client.has_collection("kb") is False
    assert client.has_collection("other") is True
    assert client.get("other").ids[0] == ["b"]


@pytest.mark.tier0
def test_delete_removes_the_vector_too_not_just_the_row(client):
    """Guards against orphaned embeddings.

    Deleting from vec_chunk alone would leave the vector in vec_index. `get`
    would look correct while `search` kept returning the deleted document --
    the kind of divergence that only shows up as a confusing RAG answer.
    """
    client.insert("kb", [item("a", "gone", [1, 0, 0, 0]), item("b", "stays", [0, 1, 0, 0])])

    client.delete("kb", ids=["a"])

    assert client.search("kb", [[1, 0, 0, 0]], limit=5).ids[0] == ["b"]
    orphans = client.conn.execute(
        "SELECT COUNT(*) FROM vec_index WHERE rowid NOT IN (SELECT rowid FROM vec_chunk)"
    ).fetchone()[0]
    assert orphans == 0


@pytest.mark.tier0
def test_reset_clears_every_collection(client):
    client.insert("kb", [item("a", "one", [1, 0, 0, 0])])
    client.insert("other", [item("b", "two", [0, 1, 0, 0])])

    client.reset()

    assert client.has_collection("kb") is False
    assert client.has_collection("other") is False
    assert client.conn.execute("SELECT COUNT(*) FROM vec_index").fetchone()[0] == 0


@pytest.mark.tier0
def test_has_collection(client):
    assert client.has_collection("kb") is False
    client.insert("kb", [item("a", "x", [1, 0, 0, 0])])
    assert client.has_collection("kb") is True


@pytest.mark.tier0
def test_short_vectors_are_padded_and_still_rank_correctly(client):
    """Zero-padding must not disturb cosine ordering.

    An install can switch embedding model; the vec0 column width cannot change.
    Padding both stored and query vectors leaves dot product and norms
    unchanged, so a 2-d vector indexed in a 4-d column ranks exactly as it
    would unpadded.
    """
    client.insert("kb", [item("a", "aligned", [1, 0]), item("b", "orthogonal", [0, 1])])

    result = client.search("kb", [[1, 0]], limit=2)

    assert result.ids[0] == ["a", "b"]
    assert result.distances[0][0] == pytest.approx(0.0, abs=1e-6)


@pytest.mark.tier0
def test_oversized_vector_raises_rather_than_truncating(client):
    with pytest.raises(ValueError, match="exceeds SQLITE_VEC_VECTOR_LENGTH"):
        client.insert("kb", [item("a", "too wide", [1, 0, 0, 0, 0])])


@pytest.mark.tier0
def test_store_persists_across_client_restarts(client, tmp_path, monkeypatch):
    """The quickstart mounts a volume; data has to survive a container restart."""
    client.insert("kb", [item("a", "durable", [1, 0, 0, 0])])
    client.close()

    reopened = SqliteVecClient()
    try:
        assert reopened.get("kb").documents[0] == ["durable"]
        assert reopened.search("kb", [[1, 0, 0, 0]], limit=1).ids[0] == ["a"]
    finally:
        reopened.close()


@pytest.mark.tier0
def test_metadata_survives_as_structured_json(client):
    """Metadata round-trips as objects, not as a stringified blob."""
    meta = {"file_id": "f1", "page": 7, "nested": {"a": [1, 2]}}
    client.insert("kb", [item("a", "doc", [1, 0, 0, 0], meta)])

    got = client.get("kb").metadatas[0][0]

    assert got == meta
    assert isinstance(got["nested"]["a"], list)


@pytest.mark.tier0
def test_extension_loads_on_this_sqlite_build(client):
    """A canary for the one environmental assumption this backend makes.

    sqlite-vec is a loadable extension; some Python builds compile extension
    loading out entirely. If that ever regresses, every other test here fails
    with the same opaque error -- this one names the cause.
    """
    version = client.conn.execute("SELECT vec_version()").fetchone()[0]
    assert version
    assert sqlite3.sqlite_version_info >= (3, 37), "sqlite-vec needs a modern SQLite"


@pytest.mark.tier0
def test_warns_when_a_legacy_chroma_store_is_orphaned(tmp_path, monkeypatch, caplog):
    """An upgrade must not silently strand an existing knowledge base.

    With no warning the symptom is RAG quietly retrieving nothing -- no error,
    no missing file, just answers that stopped using the knowledge base.
    """
    store_dir = tmp_path / "vector_db"
    store_dir.mkdir()
    (store_dir / "chroma.sqlite3").write_bytes(b"legacy")
    monkeypatch.setattr(sqlite_vec_module, "SQLITE_VEC_PATH", str(store_dir / "store.db"))
    monkeypatch.setattr(sqlite_vec_module, "VECTOR_LENGTH", DIM)

    with caplog.at_level("WARNING"):
        c = SqliteVecClient()
    try:
        assert any("legacy ChromaDB store" in r.getMessage() for r in caplog.records)
        assert any("re-indexed" in r.getMessage() for r in caplog.records)
    finally:
        c.close()


@pytest.mark.tier0
def test_no_legacy_warning_once_reindexed(tmp_path, monkeypatch, caplog):
    """The warning must stop after the user has done the re-index."""
    store_dir = tmp_path / "vector_db"
    store_dir.mkdir()
    (store_dir / "chroma.sqlite3").write_bytes(b"legacy")
    monkeypatch.setattr(sqlite_vec_module, "SQLITE_VEC_PATH", str(store_dir / "store.db"))
    monkeypatch.setattr(sqlite_vec_module, "VECTOR_LENGTH", DIM)

    first = SqliteVecClient()
    first.insert("kb", [item("a", "re-indexed", [1, 0, 0, 0])])
    first.close()

    caplog.clear()
    with caplog.at_level("WARNING"):
        second = SqliteVecClient()
    try:
        assert not any("legacy ChromaDB store" in r.getMessage() for r in caplog.records)
    finally:
        second.close()


@pytest.mark.tier0
def test_metadata_column_holds_json_text(client):
    """Storage-level check: json_extract-based filters depend on this."""
    client.insert("kb", [item("a", "doc", [1, 0, 0, 0], {"file_id": "f1"})])

    raw = client.conn.execute("SELECT metadata FROM vec_chunk WHERE id = 'a'").fetchone()[0]

    assert json.loads(raw) == {"file_id": "f1"}

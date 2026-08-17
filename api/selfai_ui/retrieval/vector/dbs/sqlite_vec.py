"""sqlite-vec vector store — the zero-configuration default.

This is the backend the single-container quickstart runs on. It needs nothing
but the SQLite that Python already links, so `docker compose -f
docker-compose.combined.yml up` gets working RAG with no second service. It
replaced ChromaDB, which cost 41 transitive packages (~98MB: onnxruntime,
tokenizers, the OpenTelemetry stack, a Kubernetes client) to do the same job
for a single-user local install.

Layout — two tables kept in step by rowid:

  vec_chunk   ordinary table: id, collection_name, text, metadata (JSON)
  vec_index   vec0 virtual table: collection_name PARTITION KEY, embedding

The partition key is load-bearing, not decoration. vec0's KNN searches the
whole table, so without partitioning a query against a small collection would
be crowded out by a larger neighbour's vectors and silently return another
collection's documents. `WHERE ... AND collection_name = ?` scopes the KNN
itself rather than filtering after the fact.

Verified against SQLite 3.40.1 — the version in the shipped api image, which is
older than most dev machines' (3.46 here). Both partitioned KNN and cosine
distance behave identically on the two.
"""

import json
import logging
import sqlite3
import struct
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import sqlite_vec

from selfai_ui.config import SQLITE_VEC_PATH, SQLITE_VEC_VECTOR_LENGTH
from selfai_ui.env import SRC_LOG_LEVELS
from selfai_ui.retrieval.vector.main import GetResult, SearchResult, VectorItem

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["RAG"])

VECTOR_LENGTH = SQLITE_VEC_VECTOR_LENGTH


def _serialize(vector: List[float]) -> bytes:
    """Pack a float list into the little-endian float32 blob vec0 expects."""
    return struct.pack(f"{len(vector)}f", *vector)


def _filter_text(value: Any) -> str:
    """Normalize a filter value for text comparison against json_extract().

    json_extract returns JSON *types*, so `{"page": 3}` yields the integer 3 and
    `3 = '3'` is false in SQLite -- comparisons must be made on a common footing.
    Every value is compared as TEXT (matching the pgvector client, which uses
    `vmetadata[key].astext`), with booleans mapped to the 1/0 that json_extract
    actually produces for JSON true/false.
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


class SqliteVecClient:
    def __init__(self) -> None:
        self.path = SQLITE_VEC_PATH
        # Serializes every operation. sqlite3 connections are not safe to share
        # across threads, and these methods are sync calls reached from FastAPI's
        # threadpool. One connection plus one lock is the honest version of that
        # constraint; a pool would need per-thread connections and buys nothing
        # for a single-writer local store.
        self._lock = threading.Lock()

        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        try:
            self.conn.enable_load_extension(True)
            sqlite_vec.load(self.conn)
            self.conn.enable_load_extension(False)
        except AttributeError as e:
            # Some Python builds compile out extension loading entirely. That is
            # unrecoverable for this backend, so say which backend and what to do
            # instead of surfacing a bare AttributeError from deep in startup.
            raise RuntimeError(
                "This Python build cannot load SQLite extensions, which the "
                "sqlite-vec vector store requires. Use a Python built with "
                "--enable-loadable-sqlite-extensions, or set VECTOR_DB=pgvector "
                "and point PGVECTOR_DB_URL at a Postgres with the vector extension."
            ) from e

        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()
        log.info("sqlite-vec vector store ready at %s (dim=%d)", self.path, VECTOR_LENGTH)
        self._warn_on_legacy_chroma_store()

    def _warn_on_legacy_chroma_store(self) -> None:
        """Say something when an upgrade has silently orphaned a chroma store.

        Otherwise the upgrade is invisible in the worst way: the store is empty,
        so RAG simply retrieves nothing. No error, no missing file, just answers
        that quietly stopped using the knowledge base. There is no in-place
        conversion between the two formats, so the only honest advice is to
        re-index -- but the user has to know that they need to.
        """
        try:
            legacy = Path(self.path).parent / "chroma.sqlite3"
            if not legacy.exists():
                return
            if self.conn.execute("SELECT 1 FROM vec_chunk LIMIT 1").fetchone() is not None:
                return  # already re-indexed; nothing to warn about
            log.warning(
                "Found a legacy ChromaDB store at %s but the sqlite-vec store is empty. "
                "The chroma backend has been removed and the two formats are not "
                "interchangeable, so existing knowledge bases will return no results "
                "until they are re-indexed (re-upload the files, or rebuild the "
                "knowledge base from its source documents). The old file is left "
                "untouched and can be deleted once you have re-indexed.",
                legacy,
            )
        except Exception:  # pragma: no cover - a warning must never break startup
            log.debug("legacy chroma store check failed", exc_info=True)

    def _create_schema(self) -> None:
        with self._lock:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vec_chunk (
                    id TEXT PRIMARY KEY,
                    collection_name TEXT NOT NULL,
                    text TEXT,
                    metadata TEXT
                )
                """
            )
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_vec_chunk_collection ON vec_chunk (collection_name)")
            self.conn.execute(
                f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS vec_index USING vec0(
                    collection_name TEXT PARTITION KEY,
                    embedding float[{VECTOR_LENGTH}] distance_metric=cosine
                )
                """
            )
            self.conn.commit()

    def _adjust_vector_length(self, vector: List[float]) -> List[float]:
        """Pad to the fixed column width.

        vec0 columns have one width for the life of the table, but a self.ai
        install can change embedding model. Zero-padding is safe for cosine
        specifically: appending zeros to both stored and query vectors leaves
        the dot product and both norms unchanged, so distances are identical to
        an unpadded index. (Same trick, same reasoning, as the pgvector client.)
        """
        vector = list(vector)
        if len(vector) > VECTOR_LENGTH:
            raise ValueError(
                f"Vector length {len(vector)} exceeds SQLITE_VEC_VECTOR_LENGTH "
                f"{VECTOR_LENGTH}. Raise it and re-index; it cannot change in place."
            )
        if len(vector) < VECTOR_LENGTH:
            vector = vector + [0.0] * (VECTOR_LENGTH - len(vector))
        return vector

    # -- writes ----------------------------------------------------------

    def _delete_rows(self, cur: sqlite3.Cursor, rowids: List[int]) -> None:
        if not rowids:
            return
        marks = ",".join("?" * len(rowids))
        cur.execute(f"DELETE FROM vec_index WHERE rowid IN ({marks})", rowids)
        cur.execute(f"DELETE FROM vec_chunk WHERE rowid IN ({marks})", rowids)

    def _insert_items(self, cur: sqlite3.Cursor, collection_name: str, items: List[VectorItem]) -> None:
        for item in items:
            vector = self._adjust_vector_length(item["vector"])
            cur.execute(
                "INSERT INTO vec_chunk (id, collection_name, text, metadata) VALUES (?, ?, ?, ?)",
                (
                    item["id"],
                    collection_name,
                    item["text"],
                    json.dumps(item["metadata"]) if item["metadata"] is not None else None,
                ),
            )
            cur.execute(
                "INSERT INTO vec_index (rowid, collection_name, embedding) VALUES (?, ?, ?)",
                (cur.lastrowid, collection_name, _serialize(vector)),
            )

    def insert(self, collection_name: str, items: List[VectorItem]) -> None:
        with self._lock:
            try:
                cur = self.conn.cursor()
                self._insert_items(cur, collection_name, items)
                self.conn.commit()
                log.info("inserted %d items into collection '%s'", len(items), collection_name)
            except Exception:
                self.conn.rollback()
                raise

    def upsert(self, collection_name: str, items: List[VectorItem]) -> None:
        with self._lock:
            try:
                cur = self.conn.cursor()
                ids = [item["id"] for item in items]
                if ids:
                    marks = ",".join("?" * len(ids))
                    existing = [r[0] for r in cur.execute(f"SELECT rowid FROM vec_chunk WHERE id IN ({marks})", ids)]
                    # Delete-then-insert rather than UPDATE: a vec0 row's
                    # embedding and its partition key are written together, and
                    # replacing both is simpler to keep consistent than patching
                    # two tables in place.
                    self._delete_rows(cur, existing)
                self._insert_items(cur, collection_name, items)
                self.conn.commit()
                log.info("upserted %d items into collection '%s'", len(items), collection_name)
            except Exception:
                self.conn.rollback()
                raise

    def delete(
        self,
        collection_name: str,
        ids: Optional[List[str]] = None,
        filter: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            try:
                cur = self.conn.cursor()
                sql = "SELECT rowid FROM vec_chunk WHERE collection_name = ?"
                params: List[Any] = [collection_name]
                if ids:
                    sql += f" AND id IN ({','.join('?' * len(ids))})"
                    params.extend(ids)
                if filter:
                    for key, value in filter.items():
                        sql += " AND CAST(json_extract(metadata, ?) AS TEXT) = ?"
                        params.extend([f"$.{key}", _filter_text(value)])
                rowids = [r[0] for r in cur.execute(sql, params)]
                self._delete_rows(cur, rowids)
                self.conn.commit()
                log.info("deleted %d items from collection '%s'", len(rowids), collection_name)
            except Exception:
                self.conn.rollback()
                raise

    def delete_collection(self, collection_name: str) -> None:
        self.delete(collection_name)
        log.info("collection '%s' deleted", collection_name)

    def reset(self) -> None:
        with self._lock:
            try:
                self.conn.execute("DELETE FROM vec_index")
                self.conn.execute("DELETE FROM vec_chunk")
                self.conn.commit()
                log.info("sqlite-vec store reset")
            except Exception:
                self.conn.rollback()
                raise

    # -- reads -----------------------------------------------------------

    def has_collection(self, collection_name: str) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM vec_chunk WHERE collection_name = ? LIMIT 1", (collection_name,)
            ).fetchone()
        return row is not None

    def search(
        self,
        collection_name: str,
        vectors: List[List[float | int]],
        limit: Optional[int] = None,
    ) -> Optional[SearchResult]:
        try:
            if not vectors:
                return None

            ids: List[List[str]] = []
            distances: List[List[float]] = []
            documents: List[List[str]] = []
            metadatas: List[List[Any]] = []

            with self._lock:
                # vec0 requires an explicit k; there is no "return everything"
                # form. When the caller passes no limit, fall back to the
                # collection's own size so the semantics match the other
                # backends' unbounded reads.
                if limit is None:
                    row = self.conn.execute(
                        "SELECT COUNT(*) FROM vec_chunk WHERE collection_name = ?", (collection_name,)
                    ).fetchone()
                    limit = row[0] if row else 0
                if limit <= 0:
                    return SearchResult(ids=[[] for _ in vectors], distances=[[] for _ in vectors],
                                        documents=[[] for _ in vectors], metadatas=[[] for _ in vectors])

                for vector in vectors:
                    query_vector = _serialize(self._adjust_vector_length(vector))
                    hits = self.conn.execute(
                        """
                        SELECT rowid, distance FROM vec_index
                        WHERE embedding MATCH ? AND k = ? AND collection_name = ?
                        ORDER BY distance
                        """,
                        (query_vector, limit, collection_name),
                    ).fetchall()

                    hit_ids: List[str] = []
                    hit_distances: List[float] = []
                    hit_documents: List[str] = []
                    hit_metadatas: List[Any] = []
                    for rowid, distance in hits:
                        chunk = self.conn.execute(
                            "SELECT id, text, metadata FROM vec_chunk WHERE rowid = ?", (rowid,)
                        ).fetchone()
                        if chunk is None:
                            continue
                        hit_ids.append(chunk[0])
                        hit_distances.append(distance)
                        hit_documents.append(chunk[1])
                        hit_metadatas.append(json.loads(chunk[2]) if chunk[2] else {})

                    ids.append(hit_ids)
                    distances.append(hit_distances)
                    documents.append(hit_documents)
                    metadatas.append(hit_metadatas)

            return SearchResult(ids=ids, distances=distances, documents=documents, metadatas=metadatas)
        except Exception as e:
            log.exception("error during sqlite-vec search: %s", e)
            return None

    def _rows_to_get_result(self, rows: List[Any]) -> GetResult:
        return GetResult(
            ids=[[r[0] for r in rows]],
            documents=[[r[1] for r in rows]],
            metadatas=[[json.loads(r[2]) if r[2] else {} for r in rows]],
        )

    def query(self, collection_name: str, filter: Dict[str, Any], limit: Optional[int] = None) -> Optional[GetResult]:
        try:
            sql = "SELECT id, text, metadata FROM vec_chunk WHERE collection_name = ?"
            params: List[Any] = [collection_name]
            for key, value in filter.items():
                # CAST is load-bearing: json_extract returns JSON types, so an
                # unwrapped `json_extract(...) = '3'` silently matches nothing
                # for {"page": 3}. See _filter_text.
                sql += " AND CAST(json_extract(metadata, ?) AS TEXT) = ?"
                params.extend([f"$.{key}", _filter_text(value)])
            if limit is not None:
                sql += " LIMIT ?"
                params.append(limit)
            with self._lock:
                rows = self.conn.execute(sql, params).fetchall()
            return self._rows_to_get_result(rows)
        except Exception as e:
            log.exception("error during sqlite-vec query: %s", e)
            return None

    def get(self, collection_name: str, limit: Optional[int] = None) -> Optional[GetResult]:
        try:
            sql = "SELECT id, text, metadata FROM vec_chunk WHERE collection_name = ?"
            params: List[Any] = [collection_name]
            if limit is not None:
                sql += " LIMIT ?"
                params.append(limit)
            with self._lock:
                rows = self.conn.execute(sql, params).fetchall()
            # Deliberately an empty GetResult rather than None for a missing or
            # empty collection: query_doc_with_hybrid_search reads
            # `result.documents[0]` with no None check, so returning None there
            # is an AttributeError. Chroma raised and pgvector returned None;
            # both fail that caller. Empty-but-shaped is the one that doesn't.
            return self._rows_to_get_result(rows)
        except Exception as e:
            log.exception("error during sqlite-vec get: %s", e)
            return None

    def close(self) -> None:
        with self._lock:
            self.conn.close()

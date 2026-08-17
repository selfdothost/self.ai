from selfai_ui.config import VECTOR_DB

if VECTOR_DB == "milvus":
    from selfai_ui.retrieval.vector.dbs.milvus import MilvusClient

    VECTOR_DB_CLIENT = MilvusClient()
elif VECTOR_DB == "qdrant":
    from selfai_ui.retrieval.vector.dbs.qdrant import QdrantClient

    VECTOR_DB_CLIENT = QdrantClient()
elif VECTOR_DB == "opensearch":
    from selfai_ui.retrieval.vector.dbs.opensearch import OpenSearchClient

    VECTOR_DB_CLIENT = OpenSearchClient()
elif VECTOR_DB == "pgvector":
    from selfai_ui.retrieval.vector.dbs.pgvector import PgvectorClient

    VECTOR_DB_CLIENT = PgvectorClient()
elif VECTOR_DB == "chroma":
    # Chroma was the inherited default and is gone. Fail with the reason and the
    # fix rather than the ModuleNotFoundError an absent chromadb would otherwise
    # raise from an import three frames down -- VECTOR_DB=chroma is a setting
    # someone deliberately wrote, so it deserves a deliberate answer.
    raise ValueError(
        "VECTOR_DB=chroma is no longer supported. The ChromaDB backend was "
        "removed in favour of sqlite-vec, which needs no extra packages or "
        "services. Unset VECTOR_DB (or set VECTOR_DB=sqlite-vec) to use it, or "
        "set VECTOR_DB=pgvector for Postgres. There is no in-place migration "
        "between the two stores: existing knowledge bases must be re-indexed."
    )
else:
    from selfai_ui.retrieval.vector.dbs.sqlite_vec import SqliteVecClient

    VECTOR_DB_CLIENT = SqliteVecClient()

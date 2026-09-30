"""Vector store — Epic-1 Sprint-1 Phase 3.

Encapsulates vector persistence + similarity behind a protocol so the rest of
the RAG pipeline is backend-agnostic. The default ``BruteForceVectorStore`` is
dependency-zero (packed float32 BLOB + pure-Python cosine) and works on SQLite
(the local-first default) and Postgres alike. A pgvector ``vector(dim)`` column
+ ANN index is a future optimization behind this same protocol, added only once
profiling proves brute-force insufficient.
"""

from backend.services.vector_store.bruteforce import (
    BruteForceVectorStore,
    pack_vector,
    unpack_vector,
)
from backend.services.vector_store.protocol import VectorStore


def build_vector_store() -> VectorStore:
    return BruteForceVectorStore()


__all__ = [
    "VectorStore",
    "BruteForceVectorStore",
    "build_vector_store",
    "pack_vector",
    "unpack_vector",
]

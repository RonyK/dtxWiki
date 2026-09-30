"""Composable symbolic and semantic indexing implementations."""

import logging

from backend.services.indexing.composite import CompositeIndexer
from backend.services.indexing.db_native import DbNativeIndexer
from backend.services.indexing.fts5 import Fts5Indexer
from backend.services.indexing.reindex import reindex_workspace

logger = logging.getLogger(__name__)


def build_indexer(settings, database_provider):
    """Compose Tier-B symbolic indexing with optional semantic indexing.

    Resolves the embedding provider rather than building it: a misconfiguration
    is already non-fatal for search and Ask, and it has to be non-fatal here for
    the same reason. The raising builder turned one bad env var into a
    permanently failing `embed` job — `ValueError` is in the worker's terminal
    set, so every job burned its retry budget and the queue filled up, under the
    exact configuration the read path treats as "semantic is simply off".
    """

    from backend.llm.registry import resolve_embedding_provider
    from backend.services.vector_store import build_vector_store

    indexers = [Fts5Indexer(database_provider)]
    embedder = None
    degraded_reason: str | None = None
    if settings is not None:
        embedder, degraded_reason = resolve_embedding_provider(settings)
        if embedder is None and degraded_reason:
            logger.warning("Semantic indexing disabled: %s", degraded_reason)
    if embedder is not None:
        indexers.append(
            DbNativeIndexer(
                embedder=embedder,
                vector_store=build_vector_store(),
                database_provider=database_provider,
            )
        )
    composite = CompositeIndexer(indexers)
    # An operator who configured semantic search and mistyped a setting must not
    # get the same outcome as one who configured none: the embed job would
    # otherwise index symbolically, report plain success, never be retried once
    # the setting is fixed, and leave the semantic index permanently short of
    # that document with nothing durable saying so.
    composite.degraded_reason = degraded_reason
    return composite

__all__ = [
    "CompositeIndexer",
    "DbNativeIndexer",
    "Fts5Indexer",
    "build_indexer",
    "reindex_workspace",
]

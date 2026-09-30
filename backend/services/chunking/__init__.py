"""Markdown chunking — Epic-1 Sprint-1 Phase 5."""

from backend.services.chunking.markdown import (
    DEFAULT_OVERLAP_CHARS,
    DEFAULT_TARGET_CHARS,
    Chunk,
    chunk_markdown,
    embedding_input,
)

__all__ = [
    "Chunk",
    "chunk_markdown",
    "embedding_input",
    "DEFAULT_TARGET_CHARS",
    "DEFAULT_OVERLAP_CHARS",
]

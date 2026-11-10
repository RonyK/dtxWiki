"""Composition of Context Git storage at application boundaries."""

from pathlib import Path

from backend.app.data_dir import data_dir
from backend.context_versions.adapters.git_libgit2 import GitSnapshotStore
from backend.context_versions.application import ContextVersionWriter


def build_context_storage(repositories_root: Path | None = None):
    root = (
        repositories_root
        if repositories_root is not None
        else data_dir() / "repositories"
    )
    store = GitSnapshotStore(root.resolve())
    return store, ContextVersionWriter(reader=store, writer=store)


def bind_context_storage(registry, factory) -> None:
    """Share explicit storage with configurable sessions without constructing DBs."""
    info = getattr(factory, "kw", {}).get("info", {})
    if "context_storage" not in registry.__dict__ and "context_snapshot_store" in info:
        registry.context_storage = (
            info["context_snapshot_store"],
            info["context_version_writer"],
        )
    configure = getattr(factory, "configure", None)
    if callable(configure):
        store, writer = registry.context_storage
        configure(
            info={
                **info,
                "context_snapshot_store": store,
                "context_version_writer": writer,
            }
        )

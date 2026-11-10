"""Observable storage failures that SQLite's serialized publisher cannot expose."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import pygit2
import pytest

from backend.context_versions.adapters.git_libgit2 import GitSnapshotStore
from backend.context_versions.application import ContextVersionWriter
from backend.context_versions.codecs import MarkdownCodec
from backend.context_versions.domain import ContextVersionStorageError
from backend.utils.block_identity import compute_block_map_with_ids


def test_independent_context_writes_overlap_without_shared_tree_or_ref(tmp_path):
    # Explicit ordinary-budget exception: SQLite serializes both publishers and
    # cannot expose cross-Context native-handle/TreeBuilder sharing. The pack
    # deletion case below exercises an independent file-loss mechanism.
    class RendezvousStore(GitSnapshotStore):
        rendezvous = None

        @contextmanager
        def _repository(self, workspace_id, *, create=False):
            with super()._repository(workspace_id, create=create) as repository:
                # Both writers must hold separate live handles simultaneously.
                # A repository-wide mutex held across write I/O breaks this.
                if create and self.rendezvous is not None:
                    self.rendezvous.wait(timeout=10)
                yield repository

    store = RendezvousStore(tmp_path / "repositories")
    writer = ContextVersionWriter(store, store)
    workspace, page_a, page_b = uuid4(), uuid4(), uuid4()
    codec = MarkdownCodec()
    created_at = datetime(2026, 11, 9, 0, 0, 0, 999999, tzinfo=UTC)
    seed = writer.prepare_snapshot(
        workspace_id=workspace,
        page_id=page_a,
        version_id=uuid4(),
        created_at=created_at,
        payload=codec.encode("ancestor"),
        parents=[],
    )
    payload_a, payload_b = (
        codec.encode("CRLF\r\ntrailing  \n"),
        codec.encode("e\u0301\x00"),
    )
    payload_a = replace(
        payload_a,
        block_map=compute_block_map_with_ids(
            codec.decode(payload_a.data),
            previous_body_md=None,
            previous_block_map=None,
            identity_seed="independent-context-a",
        ),
    )
    version_a, version_b = uuid4(), uuid4()
    store.rendezvous = Barrier(2)

    def save(page, version, payload, parents):
        record = writer.prepare_snapshot(
            workspace_id=workspace,
            page_id=page,
            version_id=version,
            created_at=created_at,
            payload=payload,
            parents=parents,
        )
        writer.align_head(workspace, page, record.commit_oid)
        return record

    with ThreadPoolExecutor(max_workers=2) as executor:
        a = executor.submit(save, page_a, version_a, payload_a, [seed.commit_oid])
        b = executor.submit(save, page_b, version_b, payload_b, [])
        record_a, record_b = a.result(timeout=15), b.result(timeout=15)
    store.rendezvous = None
    assert store.list_heads(workspace) == {
        page_a: record_a.commit_oid,
        page_b: record_b.commit_oid,
    }
    assert (
        store.read(
            workspace_id=workspace,
            page_id=page_a,
            commit_oid=record_a.commit_oid,
            descriptor=codec.descriptor,
        )
        == payload_a.data
    )
    assert (
        store.read(
            workspace_id=workspace,
            page_id=page_b,
            commit_oid=record_b.commit_oid,
            descriptor=codec.descriptor,
        )
        == payload_b.data
    )
    read_a = {
        "workspace_id": workspace,
        "page_id": page_a,
        "commit_oid": record_a.commit_oid,
        "descriptor": codec.descriptor,
    }
    assert store.read_block_map(**read_a) == payload_a.block_map
    assert (
        store.read_block_map(
            workspace_id=workspace,
            page_id=page_b,
            commit_oid=record_b.commit_oid,
            descriptor=codec.descriptor,
        )
        is None
    )
    replay_a = {
        "workspace_id": workspace,
        "page_id": page_a,
        "version_id": version_a,
        "created_at": created_at,
        "parents": [seed.commit_oid],
        "existing_oid": record_a.commit_oid,
    }
    assert writer.prepare_snapshot(**replay_a, payload=payload_a) == record_a
    # Independent snapshot-format failure: a matching body must not permit
    # replay with different archived identities. This adapter unit is the
    # lowest boundary that distinguishes body equality from metadata equality.
    with pytest.raises(ContextVersionStorageError, match="context_snapshot_invalid"):
        writer.prepare_snapshot(**replay_a, payload=replace(payload_a, block_map=[]))
    assert (
        writer.prepare_snapshot(
            workspace_id=workspace,
            page_id=page_b,
            version_id=version_b,
            created_at=created_at,
            payload=replace(payload_b, block_map=[]),
            parents=[],
            existing_oid=record_b.commit_oid,
        )
        == record_b
    )
    with pytest.raises(ContextVersionStorageError, match="context_snapshot_invalid"):
        store.read(
            workspace_id=workspace,
            page_id=page_a,
            commit_oid=record_b.commit_oid,
            descriptor=codec.descriptor,
        )
    repository = pygit2.Repository(store.repository_path(workspace))
    try:
        commit = repository[record_a.commit_oid]
        assert [str(oid) for oid in commit.parent_ids] == [seed.commit_oid]
        assert commit.message == f"Context snapshot {version_a}\n"
        assert (
            commit.author.email == commit.committer.email == "git@noreply.dtxwiki.com"
        )
        assert commit.author.offset == commit.committer.offset == 0
        assert commit.author.time == int(created_at.timestamp())
        context_tree = repository[commit.tree["context"].id]
        page_tree = repository[context_tree[str(page_a)].id]
        assert {entry.name for entry in page_tree} == {"content.md", "metadata.json"}
        # Malformed metadata is another independent storage failure: even a
        # body-only reader must reject it, rather than silently lose identities.
        damaged_page = repository.TreeBuilder(page_tree)
        damaged_page.insert(
            "metadata.json",
            repository.create_blob(b'{"schema_version":2,"block_map":[]}'),
            pygit2.GIT_FILEMODE_BLOB,
        )
        damaged_context = repository.TreeBuilder(context_tree)
        damaged_context.insert(
            str(page_a), damaged_page.write(), pygit2.GIT_FILEMODE_TREE
        )
        damaged_root = repository.TreeBuilder(commit.tree)
        damaged_root.insert(
            "context", damaged_context.write(), pygit2.GIT_FILEMODE_TREE
        )
        damaged_oid = str(
            repository.create_commit(
                None,
                commit.author,
                commit.committer,
                commit.message,
                damaged_root.write(),
                commit.parent_ids,
            )
        )
    finally:
        repository.free()
    with pytest.raises(ContextVersionStorageError, match="context_snapshot_invalid"):
        store.read(**{**read_a, "commit_oid": damaged_oid})
    with pytest.raises(ContextVersionStorageError, match="context_snapshot_invalid"):
        store.read_block_map(**{**read_a, "commit_oid": damaged_oid})
    other = GitSnapshotStore(tmp_path / "independent")
    assert (
        other.commit(
            workspace_id=workspace,
            page_id=page_b,
            version_id=version_b,
            created_at=created_at,
            payload=payload_b,
            parents=[],
        )
        == record_b.commit_oid
    )


def test_pack_pruning_preserves_reused_pack_and_objects_outside_captured_roots(
    tmp_path,
):
    store = GitSnapshotStore(tmp_path / "repositories")
    workspace, page = uuid4(), uuid4()
    codec = MarkdownCodec()

    def save(body, parents=()):
        return store.commit(
            workspace_id=workspace,
            page_id=page,
            version_id=uuid4(),
            created_at=datetime(2026, 11, 9, tzinfo=UTC),
            payload=replace(
                codec.encode(body),
                block_map=compute_block_map_with_ids(
                    body,
                    previous_body_md=None,
                    previous_block_map=None,
                ),
            ),
            parents=parents,
        )

    ancestor = save("old")
    tip = save("new", [ancestor])
    store.set_head(workspace, page, tip)
    orphan = save("uncommitted SQL transaction")
    root = store.repository_path(workspace)
    with store.management_guard(workspace):
        with pytest.raises(ContextVersionStorageError, match="context_repository_busy"):
            with store.management_guard(workspace):
                pytest.fail("management guard must be exclusive across handles")
        first = store.pack(workspace, [tip])
        assert first.packed_oids
        assert store.prune_packed(workspace, first).removed_files > 0
        second = store.pack(workspace, [tip])
        assert second.pack_files == first.pack_files
        assert store.prune_packed(workspace, second).removed_files == 0
        assert all((root / relative).is_file() for relative in second.pack_files)
        assert (
            store.read(
                workspace_id=workspace,
                page_id=page,
                commit_oid=orphan,
                descriptor=codec.descriptor,
            )
            == b"uncommitted SQL transaction"
        )
        assert (
            store.read(
                workspace_id=workspace,
                page_id=page,
                commit_oid=ancestor,
                descriptor=codec.descriptor,
            )
            == b"old"
        )
        # A wider replacement makes the old pack wholly redundant, safely.
        third = store.pack(workspace, [tip, orphan])
        store.prune_packed(workspace, third)
        assert all((root / relative).is_file() for relative in third.pack_files)
        assert (
            store.read(
                workspace_id=workspace,
                page_id=page,
                commit_oid=tip,
                descriptor=codec.descriptor,
            )
            == b"new"
        )
        assert store.read_block_map(
            workspace_id=workspace,
            page_id=page,
            commit_oid=tip,
            descriptor=codec.descriptor,
        ) == compute_block_map_with_ids(
            "new", previous_body_md=None, previous_block_map=None
        )
        # Native ODB iteration can report zero objects without inspecting a
        # malformed pack when its native index is empty. That is not proof
        # that the old representation is safe to delete.
        before = set((root / "objects" / "pack").glob("*.pack"))
        repository = pygit2.Repository(root)
        try:
            empty = pygit2.PackBuilder(repository)
            empty.write(root / "objects" / "pack")
            del empty
        finally:
            repository.free()
        unknown = (set((root / "objects" / "pack").glob("*.pack")) - before).pop()
        unknown.chmod(0o600)
        unknown.write_bytes(b"unknown invalid Git pack data")
        with pytest.raises(
            ContextVersionStorageError, match="context_snapshot_invalid"
        ):
            store.pack(workspace, [tip, orphan])
        assert unknown.read_bytes() == b"unknown invalid Git pack data"
        assert unknown.with_suffix(".idx").is_file()
        assert all((root / relative).is_file() for relative in third.pack_files)
    assert store.list_heads(workspace) == {page: tip}

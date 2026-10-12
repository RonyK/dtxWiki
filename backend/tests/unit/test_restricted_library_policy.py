"""Restricted Library targets must belong to the requested workspace.

``resolve_restricted_library_target`` must verify that the target's workspace
matches the request before applying its policy.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from backend.app.db import make_engine, make_session_factory
from backend.models.base import Base
from backend.models.library import (
    LibraryArtifact,
    LibraryArtifactConversionDetail,
    LibraryAttempt,
    LibraryAttemptConversionDetail,
    LibraryItem,
    LibraryItemVersion,
    LibraryRun,
    LibraryRunConversionDetail,
)
from backend.models.media import (
    BlobIncarnationRow,
    StoredBlobRow,
    WorkspaceMediaWriteIntent,
)
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.restricted_library_policy import (
    is_restricted_library_target,
    resolve_restricted_library_target,
)


def _seed_library_blob(session, workspace_id, *, salt: str) -> tuple[UUID, int]:
    """Minimal blob/incarnation plumbing a LibraryArtifact row's FKs require."""

    now = datetime.now(UTC)
    if session.get(WorkspaceMediaWriteIntent, workspace_id) is None:
        session.add(
            WorkspaceMediaWriteIntent(
                workspace_id=workspace_id,
                state="open",
                delete_generation=0,
                updated_at=now,
            )
        )
        session.flush()
    blob_id = uuid4()
    session.add(
        StoredBlobRow(
            workspace_id=workspace_id, id=blob_id, sha256=salt * 64, next_epoch=2
        )
    )
    session.flush()
    session.add(
        BlobIncarnationRow(
            workspace_id=workspace_id,
            blob_id=blob_id,
            blob_epoch=1,
            byte_size=1,
            storage_key=f"w/{workspace_id.hex}/{salt}/{blob_id.hex}",
            state="available",
            publish_generation=1,
            security_generation=0,
            gc_generation=0,
            available_at=now,
        )
    )
    session.flush()
    return blob_id, 1


def _session_factory():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _seed_item_and_version(session, *, workspace_id, trust_level):
    item = LibraryItem(
        workspace_id=workspace_id,
        connector_type="manual",
        format_hint="unknown",
        uri=f"file:///{uuid4()}",
        metadata_json={},
        trust_level=trust_level,
        lifecycle_status="registered",
    )
    session.add(item)
    session.flush()
    version = LibraryItemVersion(
        workspace_id=workspace_id,
        library_item_id=item.id,
        version_no=1,
        content_hash="0" * 64,
        fetched_at=datetime.now(UTC),
        raw_storage_ref="inline-text:0",
        processing_status="completed",
    )
    session.add(version)
    session.flush()

    run = LibraryRun(
        workspace_id=workspace_id,
        library_item_version_id=version.id,
        stage="conversion",
        run_seq=1,
        reason="initial",
        status="completed",
        provider_chain_json=[],
        policy_revision="test",
        output_schema_version="1",
        idempotency_key=f"test:{version.id}",
    )
    run.conversion_detail = LibraryRunConversionDetail(
        error_fallback_matrix_json={},
        per_provider_budget_json={},
        total_budget_json={},
    )
    session.add(run)
    session.flush()
    attempt = LibraryAttempt(
        workspace_id=workspace_id,
        run_id=run.id,
        library_item_version_id=version.id,
        stage="conversion",
        provider_id="test.provider",
        provider_version="1",
        provider_spi_version="1",
        config_hash="test",
        input_sha256=version.content_hash,
        output_schema_version="1",
        status="completed",
        chain_index=0,
    )
    attempt.conversion_detail = LibraryAttemptConversionDetail(
        manual_retry_disposition=None,
        disposition_reason=None,
    )
    session.add(attempt)
    session.flush()
    blob_id, blob_epoch = _seed_library_blob(session, workspace_id, salt="a")
    artifact = LibraryArtifact(
        workspace_id=workspace_id,
        library_item_version_id=version.id,
        attempt_id=attempt.id,
        stage="conversion",
        blob_workspace_id=workspace_id,
        blob_id=blob_id,
        blob_epoch=blob_epoch,
        role="markdown",
        kind="markdown",
        media_type="text/markdown",
        content_sha256="a" * 64,
        byte_size=1,
        ordinal=0,
    )
    artifact.conversion_detail = LibraryArtifactConversionDetail(
        is_complete=True,
        location_model={},
    )
    session.add(artifact)
    session.flush()
    return item, version, artifact


@pytest.mark.parametrize(
    ("target_type", "target_attr"),
    [
        ("library_item_version", "version"),
        ("library_artifact", "artifact"),
    ],
)
def test_resolves_a_target_that_belongs_to_the_requested_workspace(
    target_type,
    target_attr,
):
    factory = _session_factory()
    with factory() as session:
        workspace = seed_default_workspace(session)
        _item, version, artifact = _seed_item_and_version(
            session, workspace_id=workspace.id, trust_level="restricted"
        )
        target_id = {"version": version.id, "artifact": artifact.id}[target_attr]

        restricted = is_restricted_library_target(
            session=session,
            workspace_id=workspace.id,
            target_type=target_type,
            target_id=target_id,
        )

        resolved = resolve_restricted_library_target(
            session=session,
            workspace_id=workspace.id,
            target_type=target_type,
            target_id=target_id,
        )

        assert restricted is True
        assert resolved == (workspace.id, True)


@pytest.mark.parametrize(
    ("target_type", "target_attr"),
    [
        ("library_item_version", "version"),
        ("library_artifact", "artifact"),
    ],
)
def test_refuses_a_target_that_belongs_to_a_different_workspace(
    target_type,
    target_attr,
):
    """The core D-5 regression: a target real in workspace B must not
    resolve just because the caller asked about workspace A, even though a
    bare session.get() would happily find it (single shared DB today)."""
    factory = _session_factory()
    with factory() as session:
        requested = seed_default_workspace(session, slug="requested-workspace")
        actual = seed_default_workspace(session, slug="actual-owner-workspace")
        _item, version, artifact = _seed_item_and_version(
            session, workspace_id=actual.id, trust_level="restricted"
        )
        target_id = {"version": version.id, "artifact": artifact.id}[target_attr]

        # The row exists (a bare session.get() would return it) but does not
        # belong to `requested` -- this function must not leak it or its
        # trust level, and must not report `actual.id` as if that were an
        # acceptable substitute answer.
        target_model = (
            LibraryItemVersion
            if target_type == "library_item_version"
            else LibraryArtifact
        )
        assert session.get(target_model, target_id) is not None
        restricted = is_restricted_library_target(
            session=session,
            workspace_id=requested.id,
            target_type=target_type,
            target_id=target_id,
        )
        resolved = resolve_restricted_library_target(
            session=session,
            workspace_id=requested.id,
            target_type=target_type,
            target_id=target_id,
        )

        assert restricted is None
        assert resolved is None


def test_refuses_a_nonexistent_target_id():
    factory = _session_factory()
    with factory() as session:
        workspace = seed_default_workspace(session)
        target_id = uuid4()
        restricted = is_restricted_library_target(
            session=session,
            workspace_id=workspace.id,
            target_type="library_item_version",
            target_id=target_id,
        )
        resolved = resolve_restricted_library_target(
            session=session,
            workspace_id=workspace.id,
            target_type="library_item_version",
            target_id=target_id,
        )

        assert restricted is None
        assert resolved is None

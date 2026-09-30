from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

import backend.context_media.adapters.sqlalchemy as context_media_sqlalchemy
from backend.context_media.adapters.sqlalchemy import (
    SqlAlchemyContextMediaRepository,
)
from backend.context_media.application import (
    DeleteUnusedMedia,
    RegisterMediaAsset,
    ResolveContent,
    SyncVersionUsage,
)
from backend.context_media.domain import (
    ActorIdentity,
    AssociationState,
    ContextMediaAssetProjection,
    ContextMediaAssetRecord,
    ContextMediaEditingNotOpenError,
    ContextMediaNotFoundError,
    ContextMediaOwnershipError,
    ContextMediaUnavailableError,
    RegisterContextMediaAsset,
    StaleUsageTokenError,
    UsageState,
)
from backend.media.domain import BlobEpoch, BlobReference
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.context_media import (
    ContextMediaAsset,
    ContextMediaGcCandidate,
    ContextMediaLogicalCharge,
    ContextMediaLogicalQuotaUsage,
    ContextMediaUsage,
)
from backend.models.media import (
    BlobIncarnationRow,
    SecurityBlockedBlobRow,
    StoredBlobInspectionRow,
    StoredBlobRow,
    WorkspaceMediaWriteIntent,
)
from backend.models.upload import UploadAsset
from backend.models.workspace import Workspace

NOW = datetime(2026, 8, 6, 2, 0, tzinfo=UTC)


def _seed_page(session, workspace_id: UUID, actor: ActorIdentity | None = None):
    actor = actor or ActorIdentity("human", str(uuid4()))
    page = ContextPage(
        id=uuid4(),
        workspace_id=workspace_id,
        slug=f"context-media-{uuid4().hex[:8]}",
        title="Context media",
        current_version_id=None,
        page_type="overview",
        tags=[],
        aliases=[],
        lifecycle_status="draft",
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        id=uuid4(),
        workspace_id=workspace_id,
        page_id=page.id,
        version_no=1,
        body_md="",
        content_hash="0" * 64,
        actor_type="human",
        actor_id=None,
        version_status="editing",
        created_by_actor_kind=actor.kind,
        created_by_actor_id=actor.id,
    )
    session.add(version)
    session.flush()
    page.current_version_id = version.id
    session.flush()
    return page, version


def _seed_shared_image(session, workspace_id: UUID):
    if session.get(WorkspaceMediaWriteIntent, workspace_id) is None:
        session.add(
            WorkspaceMediaWriteIntent(
                workspace_id=workspace_id,
                state="open",
                delete_generation=0,
                updated_at=NOW,
            )
        )
        session.flush()
    blob_id = uuid4()
    inspection_id = uuid4()
    session.add(
        StoredBlobRow(
            workspace_id=workspace_id,
            id=blob_id,
            sha256="a" * 64,
            next_epoch=2,
        )
    )
    session.flush()
    session.add(
        BlobIncarnationRow(
            workspace_id=workspace_id,
            blob_id=blob_id,
            blob_epoch=1,
            byte_size=68,
            storage_key=f"w/{workspace_id.hex}/a/{blob_id.hex}",
            state="available",
            publish_generation=1,
            security_generation=0,
            gc_generation=0,
            available_at=NOW,
        )
    )
    session.flush()
    session.add(
        StoredBlobInspectionRow(
            workspace_id=workspace_id,
            id=inspection_id,
            blob_id=blob_id,
            blob_epoch=1,
            registry_version="registry-v1",
            detector_id="png-magic",
            detector_version="1",
            format_id="png",
            canonical_media_type="image/png",
            validation_schema_version="png-v1",
            metadata_json={},
            scanner_verdict="not_configured",
            scanner_provider=None,
            scanner_version=None,
            ordinary_available=True,
            created_at=NOW,
        )
    )
    session.flush()
    return BlobReference(workspace_id, blob_id, BlobEpoch(1)), inspection_id


def _candidate(
    workspace_id: UUID,
    page_id: UUID,
    created_for_version_id: UUID,
    blob: BlobReference,
    inspection_id: UUID,
    actor: ActorIdentity,
    *,
    legacy_upload_asset_id: UUID | None = None,
) -> RegisterContextMediaAsset:
    return RegisterContextMediaAsset(
        workspace_id=workspace_id,
        asset_id=uuid4(),
        page_id=page_id,
        created_for_version_id=created_for_version_id,
        blob=blob,
        inspection_id=inspection_id,
        original_filename="pixel.png",
        canonical_media_type="image/png",
        logical_bytes=68,
        policy_revision="context-mvp-v1",
        actor=actor,
        legacy_upload_asset_id=legacy_upload_asset_id,
    )


def test_sqlalchemy_adapter_registration_usage_content_and_unused_tombstone(
    session_factory,
) -> None:
    actor = ActorIdentity("human", str(uuid4()))
    with session_factory.begin() as session:
        workspace_id = session.scalar(select(Workspace.id).limit(1))
        assert workspace_id is not None
        page, version = _seed_page(session, workspace_id, actor)
        blob, inspection_id = _seed_shared_image(session, workspace_id)
        repository = SqlAlchemyContextMediaRepository(session)

        registered_asset = RegisterMediaAsset().execute(
            repository,
            _candidate(
                workspace_id,
                page.id,
                version.id,
                blob,
                inspection_id,
                actor,
            ),
            now=NOW,
        )
        assert registered_asset.page_id == page.id
        assert registered_asset.created_for_version_id == version.id
        unused_asset = RegisterMediaAsset().execute(
            repository,
            _candidate(
                workspace_id,
                page.id,
                version.id,
                blob,
                inspection_id,
                actor,
            ),
            now=NOW,
        )
        version.version_status = "draft"
        page.latest_draft_version_id = version.id
        session.flush()
        SyncVersionUsage().execute(
            repository,
            workspace_id=workspace_id,
            page_id=page.id,
            page_version_id=version.id,
            body_markdown=f"![pixel]({registered_asset.logical_reference})",
        )

        projection = repository.get_page_asset_usage(
            workspace_id, page.id, registered_asset.asset_id
        )
        assert projection is not None
        assert projection.state is UsageState.ACTIVE
        capability = ResolveContent().version(
            repository,
            workspace_id=workspace_id,
            page_id=page.id,
            page_version_id=version.id,
            asset_id=registered_asset.asset_id,
            actor=actor,
        )
        assert capability.storage_key.startswith("w/")
        assert capability.canonical_media_type == "image/png"

        unused = repository.get_page_asset_usage(
            workspace_id, page.id, unused_asset.asset_id
        )
        assert unused is not None and unused.state is UsageState.UNUSED
        DeleteUnusedMedia().execute(
            repository,
            workspace_id=workspace_id,
            page_id=page.id,
            asset_id=unused_asset.asset_id,
            usage_token=unused.usage_token,
            actor=actor,
        )
        row = session.get(
            ContextMediaAsset,
            {"workspace_id": workspace_id, "id": unused_asset.asset_id},
        )
        candidate = session.scalar(
            select(ContextMediaGcCandidate).where(
                ContextMediaGcCandidate.workspace_id == workspace_id,
                ContextMediaGcCandidate.asset_id == unused_asset.asset_id,
            )
        )
        assert row is not None and row.association_state == "tombstoned"
        assert candidate is not None
        assert candidate.asset_revision == row.revision
        quota = session.get(ContextMediaLogicalQuotaUsage, workspace_id)
        charges = session.scalars(
            select(ContextMediaLogicalCharge)
            .where(ContextMediaLogicalCharge.workspace_id == workspace_id)
            .order_by(ContextMediaLogicalCharge.asset_id)
        ).all()
        assert quota is not None and quota.charged_bytes == 68
        assert len(charges) == 2
        assert {charge.state for charge in charges} == {"charged", "released"}


def test_sqlalchemy_adapter_persists_and_uniquely_resolves_legacy_anchor(
    session_factory,
) -> None:
    actor = ActorIdentity("human", str(uuid4()))
    with session_factory.begin() as session:
        workspace_id = session.scalar(select(Workspace.id).limit(1))
        assert workspace_id is not None
        page, version = _seed_page(session, workspace_id, actor)
        blob, inspection_id = _seed_shared_image(session, workspace_id)
        legacy_upload = UploadAsset(
            workspace_id=workspace_id,
            filename=f"{'a' * 64}.png",
            mime="image/png",
            byte_count=68,
            sha256="a" * 64,
            marker_uuid=uuid4(),
            page_id_hint=page.id,
            created_by_actor_kind=actor.kind,
            created_by_actor_id=actor.id,
        )
        session.add(legacy_upload)
        session.flush()
        legacy_upload_asset_id = legacy_upload.id
        repository = SqlAlchemyContextMediaRepository(session)
        asset = RegisterMediaAsset().execute(
            repository,
            _candidate(
                workspace_id,
                page.id,
                version.id,
                blob,
                inspection_id,
                actor,
                legacy_upload_asset_id=legacy_upload_asset_id,
            ),
            now=NOW,
        )

        assert asset.legacy_upload_asset_id == legacy_upload_asset_id
        assert (
            repository.get_asset_by_legacy_upload(
                workspace_id,
                legacy_upload_asset_id,
                for_update=True,
            )
            == asset
        )
        row = session.get(
            ContextMediaAsset,
            {"workspace_id": workspace_id, "id": asset.asset_id},
        )
        assert row is not None
        assert row.legacy_upload_asset_id == legacy_upload_asset_id

        with pytest.raises(
            ValueError,
            match="legacy upload asset already has a Context anchor",
        ):
            RegisterMediaAsset().execute(
                repository,
                _candidate(
                    workspace_id,
                    page.id,
                    version.id,
                    blob,
                    inspection_id,
                    actor,
                    legacy_upload_asset_id=legacy_upload_asset_id,
                ),
                now=NOW,
            )

        charges = session.scalars(
            select(ContextMediaLogicalCharge).where(
                ContextMediaLogicalCharge.workspace_id == workspace_id
            )
        ).all()
        assert len(charges) == 1


def test_usage_lock_acquires_write_intent_then_shared_media_intent(
    session_factory,
    monkeypatch,
) -> None:
    with session_factory.begin() as session:
        workspace_id = session.scalar(select(Workspace.id).limit(1))
        assert workspace_id is not None
        page, version = _seed_page(session, workspace_id)
        repository = SqlAlchemyContextMediaRepository(session)
        events: list[str] = []
        original_acquire = context_media_sqlalchemy.acquire_write_intent
        original_scalar = session.scalar
        original_page_lock = context_media_sqlalchemy._lock_context_page

        def tracking_acquire(target_session):
            events.append("write_intent")
            return original_acquire(target_session)

        def tracking_scalar(statement, *args, **kwargs):
            descriptions = getattr(statement, "column_descriptions", ())
            if (
                descriptions
                and descriptions[0].get("entity") is WorkspaceMediaWriteIntent
            ):
                events.append("media_intent")
                lock = statement._for_update_arg
                assert lock is not None and lock.read is True
            return original_scalar(statement, *args, **kwargs)

        def tracking_page_lock(target_session, **kwargs):
            events.append("page")
            return original_page_lock(target_session, **kwargs)

        monkeypatch.setattr(
            context_media_sqlalchemy,
            "acquire_write_intent",
            tracking_acquire,
        )
        monkeypatch.setattr(session, "scalar", tracking_scalar)
        monkeypatch.setattr(
            context_media_sqlalchemy,
            "_lock_context_page",
            tracking_page_lock,
        )

        repository.lock_workspace_for_usage(workspace_id)
        # Page writers resolve a fresh adapter at the later Usage sync point.
        # The transaction-scoped marker must prevent that adapter from taking
        # the Workspace lock again after the Page row has been locked.
        SqlAlchemyContextMediaRepository(session).replace_version_usage(
            workspace_id=workspace_id,
            page_id=page.id,
            page_version_id=version.id,
            reference_counts={},
        )

        assert events[:3] == ["write_intent", "media_intent", "page"]
        with pytest.raises(
            ValueError,
            match="^workspace_media_intent_not_configured$",
        ):
            repository.lock_workspace_for_usage(uuid4())

        intent = session.get(WorkspaceMediaWriteIntent, workspace_id)
        assert intent is not None
        intent.state = "deleting"
        with pytest.raises(ValueError, match="^workspace_media_deleting$"):
            repository.lock_workspace_for_usage(workspace_id)


def test_security_block_closes_registration_and_usage_before_page_mutation(
    session_factory,
) -> None:
    actor = ActorIdentity("human", str(uuid4()))
    with session_factory.begin() as session:
        workspace_id = session.scalar(select(Workspace.id).limit(1))
        assert workspace_id is not None
        page, version = _seed_page(session, workspace_id, actor)
        blob, inspection_id = _seed_shared_image(session, workspace_id)
        repository = SqlAlchemyContextMediaRepository(session)
        editing_asset = RegisterMediaAsset().execute(
            repository,
            _candidate(
                workspace_id,
                page.id,
                version.id,
                blob,
                inspection_id,
                actor,
            ),
            now=NOW,
        )
        page_asset = RegisterMediaAsset().execute(
            repository,
            _candidate(
                workspace_id,
                page.id,
                version.id,
                blob,
                inspection_id,
                actor,
            ),
            now=NOW,
        )
        session.add(
            SecurityBlockedBlobRow(
                workspace_id=workspace_id,
                blob_id=blob.blob_id,
                blob_epoch=blob.epoch.value,
                generation=1,
                state="quarantined",
                reason_code="test_security_block",
                blocked_at=NOW,
                updated_at=NOW,
            )
        )
        session.flush()

        with pytest.raises(ContextMediaUnavailableError):
            with session.begin_nested():
                RegisterMediaAsset().execute(
                    repository,
                    _candidate(
                        workspace_id, page.id, version.id, blob, inspection_id, actor
                    ),
                    now=NOW,
                )
        with pytest.raises(ContextMediaUnavailableError):
            SyncVersionUsage().execute(
                repository,
                workspace_id=workspace_id,
                page_id=page.id,
                page_version_id=version.id,
                body_markdown=f"![blocked]({page_asset.logical_reference})",
            )

        unchanged_editing_asset = session.get(
            ContextMediaAsset,
            {"workspace_id": workspace_id, "id": editing_asset.asset_id},
        )
        usage_count = len(
            session.scalars(
                select(ContextMediaUsage).where(
                    ContextMediaUsage.workspace_id == workspace_id,
                    ContextMediaUsage.page_version_id == version.id,
                )
            ).all()
        )
        assert unchanged_editing_asset is not None
        assert unchanged_editing_asset.page_id == page.id
        assert unchanged_editing_asset.created_for_version_id == version.id
        assert usage_count == 0


def test_sqlite_usage_replace_fences_stale_unused_tombstone(session_factory) -> None:
    actor = ActorIdentity("human", str(uuid4()))
    with session_factory.begin() as session:
        workspace_id = session.scalar(select(Workspace.id).limit(1))
        assert workspace_id is not None
        page, version = _seed_page(session, workspace_id, actor)
        blob, inspection_id = _seed_shared_image(session, workspace_id)
        asset = RegisterMediaAsset().execute(
            SqlAlchemyContextMediaRepository(session),
            _candidate(
                workspace_id,
                page.id,
                version.id,
                blob,
                inspection_id,
                actor,
            ),
            now=NOW,
        )
        stale_unused = SqlAlchemyContextMediaRepository(session).get_page_asset_usage(
            workspace_id, page.id, asset.asset_id
        )
        assert stale_unused is not None and stale_unused.state is UsageState.UNUSED
        page_id = page.id
        version_id = version.id
        asset_id = asset.asset_id
        logical_reference = asset.logical_reference

    with session_factory.begin() as session:
        SyncVersionUsage().execute(
            SqlAlchemyContextMediaRepository(session),
            workspace_id=workspace_id,
            page_id=page_id,
            page_version_id=version_id,
            body_markdown=f"![now-used]({logical_reference})",
        )

    with session_factory.begin() as session:
        with pytest.raises(StaleUsageTokenError):
            SqlAlchemyContextMediaRepository(session).tombstone_and_enqueue_gc(
                stale_unused
            )

    with session_factory.begin() as session:
        persisted = session.get(
            ContextMediaAsset,
            {"workspace_id": workspace_id, "id": asset_id},
        )
        charge = session.scalar(
            select(ContextMediaLogicalCharge).where(
                ContextMediaLogicalCharge.workspace_id == workspace_id,
                ContextMediaLogicalCharge.asset_id == asset_id,
            )
        )
        usage = session.scalar(
            select(ContextMediaUsage).where(
                ContextMediaUsage.workspace_id == workspace_id,
                ContextMediaUsage.asset_id == asset_id,
            )
        )
        gc_candidate = session.scalar(
            select(ContextMediaGcCandidate).where(
                ContextMediaGcCandidate.workspace_id == workspace_id,
                ContextMediaGcCandidate.asset_id == asset_id,
            )
        )
        assert persisted is not None and persisted.association_state == "active"
        assert charge is not None and charge.state == "charged"
        assert usage is not None and usage.reference_count == 1
        assert gc_candidate is None


def test_sqlite_tombstone_fences_later_version_usage(session_factory) -> None:
    actor = ActorIdentity("human", str(uuid4()))
    with session_factory.begin() as session:
        workspace_id = session.scalar(select(Workspace.id).limit(1))
        assert workspace_id is not None
        page, version = _seed_page(session, workspace_id, actor)
        blob, inspection_id = _seed_shared_image(session, workspace_id)
        repository = SqlAlchemyContextMediaRepository(session)
        asset = RegisterMediaAsset().execute(
            repository,
            _candidate(
                workspace_id,
                page.id,
                version.id,
                blob,
                inspection_id,
                actor,
            ),
            now=NOW,
        )
        unused = repository.get_page_asset_usage(workspace_id, page.id, asset.asset_id)
        assert unused is not None and unused.state is UsageState.UNUSED
        page_id = page.id
        version_id = version.id
        asset_id = asset.asset_id
        logical_reference = asset.logical_reference

    with session_factory.begin() as session:
        SqlAlchemyContextMediaRepository(session).tombstone_and_enqueue_gc(unused)

    with session_factory.begin() as session:
        with pytest.raises(ContextMediaOwnershipError):
            SyncVersionUsage().execute(
                SqlAlchemyContextMediaRepository(session),
                workspace_id=workspace_id,
                page_id=page_id,
                page_version_id=version_id,
                body_markdown=f"![too-late]({logical_reference})",
            )

    with session_factory.begin() as session:
        charge = session.scalar(
            select(ContextMediaLogicalCharge).where(
                ContextMediaLogicalCharge.workspace_id == workspace_id,
                ContextMediaLogicalCharge.asset_id == asset_id,
            )
        )
        usage = session.scalar(
            select(ContextMediaUsage).where(
                ContextMediaUsage.workspace_id == workspace_id,
                ContextMediaUsage.asset_id == asset_id,
            )
        )
        assert charge is not None and charge.state == "released"
        assert usage is None


@pytest.mark.parametrize(
    "closed_status", ["draft", "published_snapshot", "superseded_snapshot"]
)
def test_sqlite_closed_editing_version_fences_late_asset_association(
    session_factory, closed_status,
) -> None:
    # Closing an editing version replaces claim as the ownership transition
    # that must fence an already prepared registration in a later transaction.
    actor = ActorIdentity("human", str(uuid4()))
    with session_factory.begin() as session:
        workspace_id = session.scalar(select(Workspace.id).limit(1))
        page, version = _seed_page(session, workspace_id, actor)
        blob, inspection_id = _seed_shared_image(session, workspace_id)
        candidate = _candidate(
            workspace_id, page.id, version.id, blob, inspection_id, actor
        )
        late_record = ContextMediaAssetRecord(
            workspace_id=workspace_id, asset_id=candidate.asset_id,
            page_id=page.id, created_for_version_id=version.id,
            blob=blob, inspection_id=inspection_id,
            original_filename=candidate.original_filename,
            canonical_media_type=candidate.canonical_media_type,
            logical_bytes=candidate.logical_bytes,
            policy_revision=candidate.policy_revision,
            created_by=actor, association_state=AssociationState.ACTIVE, revision=1,
        )
        version_id = version.id
    with session_factory.begin() as session:
        session.get(ContextPageVersion, version_id).version_status = closed_status
    with session_factory.begin() as session:
        with pytest.raises(ContextMediaEditingNotOpenError) as closed:
            SqlAlchemyContextMediaRepository(session).add_asset(late_record)
        assert closed.value.error_code == "media_editing_not_open"
    with session_factory() as session:
        assert (
            session.get(ContextMediaAsset, (workspace_id, candidate.asset_id)) is None
        )
        assert session.scalar(select(ContextMediaLogicalCharge).where(
            ContextMediaLogicalCharge.workspace_id == workspace_id,
            ContextMediaLogicalCharge.asset_id == candidate.asset_id,
        )) is None


def test_asset_add_rechecks_archived_page_under_final_lock(
    session_factory,
) -> None:
    actor = ActorIdentity("human", str(uuid4()))
    with session_factory.begin() as session:
        workspace_id = session.scalar(select(Workspace.id).limit(1))
        assert workspace_id is not None
        page, version = _seed_page(session, workspace_id, actor)
        blob, inspection_id = _seed_shared_image(session, workspace_id)
        page.lifecycle_status = "archived"
        candidate = _candidate(
            workspace_id,
            page.id,
            version.id,
            blob,
            inspection_id,
            actor,
        )

        with pytest.raises(ContextMediaNotFoundError) as raised:
            RegisterMediaAsset().execute(
                SqlAlchemyContextMediaRepository(session),
                candidate,
                now=NOW,
            )

        assert raised.value.error_code == "not_found"
        assert (
            session.get(
                ContextMediaAsset,
                {"workspace_id": workspace_id, "id": candidate.asset_id},
            )
            is None
        )
        assert (
            session.scalar(
                select(ContextMediaLogicalCharge).where(
                    ContextMediaLogicalCharge.workspace_id == workspace_id,
                    ContextMediaLogicalCharge.asset_id == candidate.asset_id,
                )
            )
            is None
        )


def test_asset_add_and_tombstone_follow_workspace_quota_page_lock_order(
    session_factory,
    monkeypatch,
) -> None:
    actor = ActorIdentity("human", str(uuid4()))
    with session_factory.begin() as session:
        workspace_id = session.scalar(select(Workspace.id).limit(1))
        assert workspace_id is not None
        page, version = _seed_page(session, workspace_id, actor)
        blob, inspection_id = _seed_shared_image(session, workspace_id)
        legacy_upload = UploadAsset(
            workspace_id=workspace_id,
            filename=f"{'b' * 64}.png",
            mime="image/png",
            byte_count=68,
            sha256="b" * 64,
            marker_uuid=uuid4(),
            page_id_hint=page.id,
            created_by_actor_kind=actor.kind,
            created_by_actor_id=actor.id,
        )
        session.add(legacy_upload)
        session.flush()
        repository = SqlAlchemyContextMediaRepository(session)
        events: list[str] = []
        original_workspace_lock = context_media_sqlalchemy._lock_context_workspace
        original_version_lock = context_media_sqlalchemy._lock_context_page_version
        original_page_lock = context_media_sqlalchemy._lock_context_page
        original_quota_lock = repository._logical_quota.lock_workspace
        original_anchor_lookup = repository.get_asset_by_legacy_upload

        def tracking_workspace_lock(target_session, target_workspace_id):
            events.append("workspace")
            return original_workspace_lock(target_session, target_workspace_id)

        def tracking_quota_lock(target_workspace_id):
            events.append("quota")
            return original_quota_lock(target_workspace_id)

        def tracking_page_lock(
            target_session,
            *,
            workspace_id,
            page_id,
        ):
            events.append("page")
            return original_page_lock(
                target_session,
                workspace_id=workspace_id,
                page_id=page_id,
            )

        def tracking_version_lock(target_session, **kwargs):
            events.append("editing_version")
            return original_version_lock(target_session, **kwargs)

        monkeypatch.setattr(
            context_media_sqlalchemy,
            "_lock_context_page_version",
            tracking_version_lock,
        )

        def tracking_anchor_lookup(*args, **kwargs):
            events.append("legacy_anchor")
            return original_anchor_lookup(*args, **kwargs)

        monkeypatch.setattr(
            context_media_sqlalchemy,
            "_lock_context_workspace",
            tracking_workspace_lock,
        )
        monkeypatch.setattr(
            repository._logical_quota,
            "lock_workspace",
            tracking_quota_lock,
        )
        monkeypatch.setattr(
            context_media_sqlalchemy,
            "_lock_context_page",
            tracking_page_lock,
        )
        monkeypatch.setattr(
            repository,
            "get_asset_by_legacy_upload",
            tracking_anchor_lookup,
        )
        asset = RegisterMediaAsset().execute(
            repository,
            _candidate(
                workspace_id,
                page.id,
                version.id,
                blob,
                inspection_id,
                actor,
                legacy_upload_asset_id=legacy_upload.id,
            ),
            now=NOW,
        )
        assert events[:5] == [
            "workspace", "quota", "legacy_anchor", "page", "editing_version"
        ]

        events.clear()
        unused = repository.get_page_asset_usage(workspace_id, page.id, asset.asset_id)
        assert unused is not None
        repository.tombstone_and_enqueue_gc(unused)
        assert events[:3] == ["workspace", "quota", "page"]


@pytest.mark.parametrize(
    "mismatch", ["missing", "actor_id", "actor_kind", "page", "workspace"]
)
@pytest.mark.parametrize("version_status", ["editing", "draft"])
def test_registration_hides_foreign_or_missing_editing_version(
    session_factory, mismatch, version_status,
) -> None:
    actor = ActorIdentity("human", str(uuid4()))
    with session_factory.begin() as session:
        workspace_id = session.scalar(select(Workspace.id).limit(1))
        page, version = _seed_page(session, workspace_id, actor)
        version.version_status = version_status
        blob, inspection_id = _seed_shared_image(session, workspace_id)
        target_version_id = version.id
        if mismatch == "missing":
            target_version_id = uuid4()
        elif mismatch == "actor_id":
            version.created_by_actor_id = "another-author"
        elif mismatch == "actor_kind":
            version.created_by_actor_kind = "mcp_client"
        elif mismatch == "page":
            _, other = _seed_page(session, workspace_id, actor)
            target_version_id = other.id
        else:
            foreign_workspace = Workspace(
                slug="foreign-editing", title="Foreign", name="Foreign"
            )
            session.add(foreign_workspace)
            session.flush()
            _, other = _seed_page(session, foreign_workspace.id, actor)
            target_version_id = other.id
        candidate = _candidate(
            workspace_id, page.id, target_version_id, blob, inspection_id, actor
        )
        with pytest.raises(ContextMediaNotFoundError) as hidden:
            RegisterMediaAsset().execute(
                SqlAlchemyContextMediaRepository(session), candidate, now=NOW
            )
        assert hidden.value.error_code == "not_found"
        assert (
            session.get(ContextMediaAsset, (workspace_id, candidate.asset_id)) is None
        )
        assert session.scalar(select(ContextMediaLogicalCharge).where(
            ContextMediaLogicalCharge.workspace_id == workspace_id,
            ContextMediaLogicalCharge.asset_id == candidate.asset_id,
        )) is None


@pytest.mark.parametrize("submitted", [False, True])
def test_version_content_usage_grant_and_author_only_uninserted_fallback(
    session_factory, submitted,
) -> None:
    owner = ActorIdentity("human", str(uuid4()))
    stranger = ActorIdentity("human", str(uuid4()))
    with session_factory.begin() as session:
        workspace_id = session.scalar(select(Workspace.id).limit(1))
        page, version = _seed_page(session, workspace_id, owner)
        other_version = ContextPageVersion(
            workspace_id=workspace_id, page_id=page.id, version_no=2,
            body_md="", content_hash="1" * 64, actor_type="human",
            created_by_actor_kind=stranger.kind, created_by_actor_id=stranger.id,
            version_status="editing",
        )
        session.add(other_version)
        session.flush()
        blob, inspection_id = _seed_shared_image(session, workspace_id)
        repository = SqlAlchemyContextMediaRepository(session)
        asset = RegisterMediaAsset().execute(
            repository,
            _candidate(workspace_id, page.id, version.id, blob, inspection_id, owner),
            now=NOW,
        )
        if submitted:
            version.version_status = "draft"
            session.flush()
        for target_workspace, target_version, actor, readable in (
            (workspace_id, version.id, owner, True),
            (workspace_id, version.id, stranger, False),
            (workspace_id, other_version.id, owner, False),
            (uuid4(), version.id, owner, False),
        ):
            capability = repository.resolve_version_content(
                workspace_id=target_workspace,
                page_id=page.id,
                page_version_id=target_version,
                asset_id=asset.asset_id,
                actor=actor,
            )
            assert (capability is not None) is readable
        SyncVersionUsage().execute(
            repository,
            workspace_id=workspace_id,
            page_id=page.id,
            page_version_id=other_version.id,
            body_markdown=f"![used]({asset.logical_reference})",
        )
        # Outer route authorization controls version visibility. A real usage
        # row permits resolution for either actor, independent of upload author.
        for actor in (owner, stranger):
            capability = repository.resolve_version_content(
                workspace_id=workspace_id,
                page_id=page.id,
                page_version_id=other_version.id,
                asset_id=asset.asset_id,
                actor=actor,
            )
            assert capability is not None


@pytest.mark.parametrize("close_version", [False, True])
def test_unused_delete_tracks_editing_owner_and_closed_version_transition(
    session_factory, close_version,
) -> None:
    owner = ActorIdentity("human", str(uuid4()))
    stranger = ActorIdentity("human", str(uuid4()))
    with session_factory.begin() as session:
        workspace_id = session.scalar(select(Workspace.id).limit(1))
        page, version = _seed_page(session, workspace_id, owner)
        blob, inspection_id = _seed_shared_image(session, workspace_id)
        repository = SqlAlchemyContextMediaRepository(session)
        asset = RegisterMediaAsset().execute(
            repository,
            _candidate(workspace_id, page.id, version.id, blob, inspection_id, owner),
            now=NOW,
        )
        protected = repository.get_page_asset_usage(
            workspace_id, page.id, asset.asset_id
        )
        assert protected.created_for_version_is_editing is True
        with pytest.raises(ContextMediaOwnershipError):
            DeleteUnusedMedia().execute(
                repository,
                workspace_id=workspace_id,
                page_id=page.id,
                asset_id=asset.asset_id,
                usage_token="stale",
                actor=stranger,
            )
        retained = repository.get_asset(workspace_id, asset.asset_id)
        assert retained.association_state is AssociationState.ACTIVE
        if close_version:
            version.version_status = "draft"
            session.flush()
        unprotected = repository.get_page_asset_usage(
            workspace_id, page.id, asset.asset_id
        )
        assert unprotected.created_for_version_is_editing is (not close_version)
        assert unprotected.usage_token == protected.usage_token
        DeleteUnusedMedia().execute(
            repository,
            workspace_id=workspace_id,
            page_id=page.id,
            asset_id=asset.asset_id,
            usage_token=unprotected.usage_token,
            actor=stranger if close_version else owner,
        )
        deleted = repository.get_asset(workspace_id, asset.asset_id)
        assert deleted.association_state is AssociationState.TOMBSTONED


def test_list_page_usage_masks_a_foreign_authors_private_draft_reference_count(
    session_factory,
) -> None:
    """P8.4: `latest_draft_reference_count` is derived from `page.latest_
    draft_version_id`'s body -- a page-global pointer that, per P2.2's
    per-author (not per-page) "one open editing row" uniqueness, can name a
    DIFFERENT author's currently-open, private editing row. Before this fix,
    any viewer calling `list_page_usage` saw that author's real, private
    reference count for a shared asset (e.g. 0 if they just removed an
    image, or 3 if they tripled it) -- a signal about their unsubmitted
    edit, even though the asset itself stays visible (it is also used by the
    published head, so the asset-level `foreign_editing_version_exists`
    filter does not hide it)."""

    author = ActorIdentity("human", str(uuid4()))
    other_author = ActorIdentity("human", str(uuid4()))
    with session_factory.begin() as session:
        workspace_id = session.scalar(select(Workspace.id).limit(1))
        assert workspace_id is not None
        page, published = _seed_page(session, workspace_id, author)
        blob, inspection_id = _seed_shared_image(session, workspace_id)
        repository = SqlAlchemyContextMediaRepository(session)

        asset = RegisterMediaAsset().execute(
            repository,
            _candidate(
                workspace_id, page.id, published.id, blob, inspection_id, author
            ),
            now=NOW,
        )
        published.version_status = "published_snapshot"
        page.current_published_version_id = published.id
        page.current_version_id = published.id
        session.flush()
        SyncVersionUsage().execute(
            repository,
            workspace_id=workspace_id,
            page_id=page.id,
            page_version_id=published.id,
            body_markdown=f"![pixel]({asset.logical_reference})",
        )

        # A DIFFERENT author's currently-open, private editing row -- not
        # `published`'s successor in any submitted sense, just a live
        # concurrent edit (P2.2's uniqueness is per page+author).
        private_editing = ContextPageVersion(
            id=uuid4(),
            workspace_id=workspace_id,
            page_id=page.id,
            version_no=2,
            parent_version_id=published.id,
            body_md="private draft body",
            content_hash="1" * 64,
            actor_type="human",
            actor_id=None,
            version_status="editing",
            created_by_actor_kind=other_author.kind,
            created_by_actor_id=other_author.id,
        )
        session.add(private_editing)
        page.latest_draft_version_id = private_editing.id
        page.current_version_id = private_editing.id
        session.flush()
        SyncVersionUsage().execute(
            repository,
            workspace_id=workspace_id,
            page_id=page.id,
            page_version_id=private_editing.id,
            body_markdown=(
                f"![pixel]({asset.logical_reference})"
                f"![pixel]({asset.logical_reference})"
                f"![pixel]({asset.logical_reference})"
            ),
        )

        # The real, unmasked snapshot (what get_page_asset_usage/the CAS
        # token always see) still has the real count -- untouched.
        unmasked = repository.get_page_asset_usage(
            workspace_id, page.id, asset.asset_id
        )
        assert unmasked is not None
        assert unmasked.latest_draft_reference_count == 3

        [foreign_snapshot] = repository.list_page_usage(
            workspace_id, page.id, viewer=author
        )
        assert foreign_snapshot.latest_draft_reference_count == 3
        assert foreign_snapshot.viewer_visible_latest_draft_reference_count == 0
        assert foreign_snapshot.usage_token == unmasked.usage_token

        projection = ContextMediaAssetProjection.from_snapshot(foreign_snapshot)
        assert projection.latest_draft_reference_count == 0
        # Published usage alone (count 1) is what keeps this ACTIVE -- the
        # masked draft count must not itself demote it to UNUSED/HISTORY_ONLY
        # nor should the stale ACTIVE-because-of-a-hidden-draft signal leak.
        assert projection.usage_state is UsageState.ACTIVE
        assert projection.usage_token == unmasked.usage_token

        [own_snapshot] = repository.list_page_usage(
            workspace_id, page.id, viewer=other_author
        )
        assert own_snapshot.viewer_visible_latest_draft_reference_count == 3
        own_projection = ContextMediaAssetProjection.from_snapshot(own_snapshot)
        assert own_projection.latest_draft_reference_count == 3

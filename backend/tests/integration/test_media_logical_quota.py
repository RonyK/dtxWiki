from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from backend.context_media.adapters.sqlalchemy import (
    ContextLogicalQuotaExceededError,
    SqlAlchemyContextLogicalQuota,
)
from backend.library_media.adapters.sqlalchemy import (
    SqlAlchemyLibraryLogicalQuota,
)
from backend.library_media.ports import LibraryLogicalQuotaExceededError
from backend.models.context_media import (
    ContextMediaLogicalCharge,
    ContextMediaLogicalQuotaUsage,
)
from backend.models.library import LibraryLogicalCharge, LibraryLogicalQuotaUsage
from backend.models.media import WorkspaceMediaWriteIntent
from backend.models.workspace import Workspace


def _workspace_with_media_intent(session):
    workspace = session.scalar(select(Workspace).limit(1))
    assert workspace is not None
    if session.get(WorkspaceMediaWriteIntent, workspace.id) is None:
        session.add(
            WorkspaceMediaWriteIntent(
                workspace_id=workspace.id,
                state="open",
                delete_generation=0,
                updated_at=datetime.now(UTC),
            )
        )
        session.flush()
    return workspace


def test_library_logical_quota_is_idempotent_and_separates_source_from_derived(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        workspace = _workspace_with_media_intent(session)
        quota = SqlAlchemyLibraryLogicalQuota(session)
        source_id = uuid4()
        derived_id = uuid4()

        quota.reserve(
            workspace.id,
            quota_kind="source",
            associations={source_id: 7},
        )
        quota.reserve(
            workspace.id,
            quota_kind="source",
            associations={source_id: 7},
        )
        quota.reserve(
            workspace.id,
            quota_kind="derived",
            associations={derived_id: 7},
        )

        source_usage = session.get(
            LibraryLogicalQuotaUsage,
            {"workspace_id": workspace.id, "quota_kind": "source"},
        )
        derived_usage = session.get(
            LibraryLogicalQuotaUsage,
            {"workspace_id": workspace.id, "quota_kind": "derived"},
        )
        assert source_usage is not None and source_usage.charged_bytes == 7
        assert derived_usage is not None and derived_usage.charged_bytes == 7
        assert session.scalar(select(func.count(LibraryLogicalCharge.id))) == 2

        source_usage.limit_bytes = 7
        with pytest.raises(LibraryLogicalQuotaExceededError) as exc_info:
            quota.reserve(
                workspace.id,
                quota_kind="source",
                associations={uuid4(): 1},
            )
        assert exc_info.value.limit_bytes == 7
        assert source_usage.charged_bytes == 7
        assert session.scalar(select(func.count(LibraryLogicalCharge.id))) == 2

        quota.release(
            workspace.id,
            quota_kind="source",
            association_ids={source_id},
        )
        quota.release(
            workspace.id,
            quota_kind="source",
            association_ids={source_id},
        )
        assert source_usage.charged_bytes == 0
        assert derived_usage.charged_bytes == 7
        source_charge = session.scalar(
            select(LibraryLogicalCharge).where(
                LibraryLogicalCharge.association_id == source_id
            )
        )
        assert source_charge is not None and source_charge.state == "released"


def test_context_logical_quota_is_idempotent_and_releases_once(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        workspace = _workspace_with_media_intent(session)
        quota = SqlAlchemyContextLogicalQuota(session)
        asset_id = uuid4()

        quota.reserve(workspace.id, asset_id=asset_id, byte_size=5)
        quota.reserve(workspace.id, asset_id=asset_id, byte_size=5)

        usage = session.get(ContextMediaLogicalQuotaUsage, workspace.id)
        assert usage is not None and usage.charged_bytes == 5
        assert session.scalar(select(func.count(ContextMediaLogicalCharge.id))) == 1

        usage.limit_bytes = 5
        with pytest.raises(ContextLogicalQuotaExceededError) as exc_info:
            quota.reserve(workspace.id, asset_id=uuid4(), byte_size=1)
        assert exc_info.value.limit_bytes == 5
        assert usage.charged_bytes == 5
        assert session.scalar(select(func.count(ContextMediaLogicalCharge.id))) == 1

        quota.release(workspace.id, asset_id=asset_id)
        quota.release(workspace.id, asset_id=asset_id)
        assert usage.charged_bytes == 0
        charge = session.scalar(
            select(ContextMediaLogicalCharge).where(
                ContextMediaLogicalCharge.asset_id == asset_id
            )
        )
        assert charge is not None and charge.state == "released"


def test_context_logical_quota_lock_provisions_one_uncharged_decision_row(
    session_factory,
) -> None:
    with session_factory.begin() as session:
        workspace = _workspace_with_media_intent(session)
        quota = SqlAlchemyContextLogicalQuota(session)

        quota.lock_workspace(workspace.id)
        quota.lock_workspace(workspace.id)

        usage = session.get(ContextMediaLogicalQuotaUsage, workspace.id)
        assert usage is not None
        assert usage.charged_bytes == 0
        assert usage.revision == 0
        assert session.scalar(select(func.count(ContextMediaLogicalCharge.id))) == 0

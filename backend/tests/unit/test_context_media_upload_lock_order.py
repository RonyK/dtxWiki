from contextlib import contextmanager
from hashlib import sha256
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, sessionmaker

from backend.media.domain import (
    AvailableBlobRecord,
    BlobEpoch,
    BlobReference,
    Sha256Digest,
)
from backend.models import Base
from backend.models.context_media import ContextMediaUploadRequest
from backend.models.media import MediaStagingLease, WorkspaceMediaWriteIntent
from backend.models.workspace import Workspace
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.context_image_validation import ValidatedContextImage
from backend.services.context_media_upload import (
    ContextMediaUploadService,
    _lock_workspace_media_uploads,
)


class _PostgresDialect:
    name = "postgresql"


class _PostgresBind:
    dialect = _PostgresDialect()


class _LockSpySession:
    def __init__(self, *, intent: object | None) -> None:
        self.intent = intent
        self.workspace = object()
        self.events: list[tuple[str, object, object]] = []

    def get_bind(self) -> _PostgresBind:
        return _PostgresBind()

    def scalar(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        self.events.append(("scalar", entity, statement))
        return self.intent

    def get(self, entity, identity, **kwargs):
        self.events.append(("get", entity, (identity, kwargs)))
        return self.workspace


def _call_lock(spy: _LockSpySession, workspace_id: UUID) -> None:
    _lock_workspace_media_uploads(cast(Session, spy), workspace_id)


def test_context_upload_locks_media_intent_before_plain_workspace_lookup() -> None:
    workspace_id = uuid4()
    spy = _LockSpySession(intent=SimpleNamespace(state="open"))

    _call_lock(spy, workspace_id)

    assert [(kind, entity) for kind, entity, _ in spy.events] == [
        ("scalar", WorkspaceMediaWriteIntent),
        ("get", Workspace),
    ]
    intent_statement = spy.events[0][2]
    intent_sql = str(intent_statement.compile(dialect=postgresql.dialect()))
    assert "FROM workspace_media_write_intent" in intent_sql
    assert intent_sql.rstrip().endswith("FOR SHARE")

    workspace_identity, workspace_get_options = spy.events[1][2]
    assert workspace_identity == workspace_id
    assert workspace_get_options == {}


def test_context_upload_missing_intent_fails_before_workspace_lookup() -> None:
    workspace_id = uuid4()
    spy = _LockSpySession(intent=None)

    with pytest.raises(ValueError, match="^workspace_media_intent_not_configured$"):
        _call_lock(spy, workspace_id)

    assert [(kind, entity) for kind, entity, _ in spy.events] == [
        ("scalar", WorkspaceMediaWriteIntent)
    ]


def test_seed_default_workspace_provisions_open_media_intent() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with factory.begin() as session:
        workspace = seed_default_workspace(
            session,
            slug="media-intent-seed",
            title="Media Intent Seed",
        )
        workspace_id = workspace.id

    with factory() as session:
        intent = session.get(WorkspaceMediaWriteIntent, workspace_id)
        assert intent is not None
        assert intent.state == "open"
        assert intent.delete_generation == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkspaceMediaWriteIntent)
                .where(WorkspaceMediaWriteIntent.workspace_id == workspace_id)
            )
            == 1
        )


class _ServiceTransaction:
    def __init__(self, *, phase: int, events: list[str]) -> None:
        self.phase = phase
        self.events = events

    def get_bind(self):
        return _PostgresBind()

    def scalar(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is WorkspaceMediaWriteIntent:
            return SimpleNamespace(state="open")
        if entity is MediaStagingLease:
            return None
        raise AssertionError(f"unexpected scalar entity: {entity}")

    def get(self, entity, identity, **kwargs):
        del identity, kwargs
        if entity is Workspace:
            return object()
        if entity is ContextMediaUploadRequest:
            if self.phase == 2:
                self.events.append("request_read")
            return None
        raise AssertionError(f"unexpected get entity: {entity}")

    def add(self, value) -> None:
        assert isinstance(value, ContextMediaUploadRequest)

    def flush(self) -> None:
        return None


class _ServiceSessionFactory:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.phase = 0

    @contextmanager
    def begin(self):
        phase = self.phase
        self.phase += 1
        yield _ServiceTransaction(phase=phase, events=self.events)


class _OrderRepository:
    def __init__(self, transaction: _ServiceTransaction) -> None:
        self.transaction = transaction

    def get_asset(self, workspace_id, asset_id):
        del workspace_id, asset_id
        if self.transaction.phase == 2:
            self.transaction.events.append("asset_read")
        return None

    def get_asset_by_legacy_upload(
        self,
        workspace_id,
        legacy_upload_asset_id,
        *,
        for_update=False,
    ):
        del workspace_id, legacy_upload_asset_id
        assert for_update is True
        self.transaction.events.append("legacy_anchor_read")
        return None

    def add_asset(self, asset):
        self.transaction.events.append("asset_add")
        return asset


class _OrderQuota:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def lock_workspace(self, workspace_id) -> None:
        del workspace_id
        self.events.append("quota_lock")

    def reserve(self, workspace_id, *, asset_id, byte_size) -> None:
        del workspace_id, asset_id, byte_size
        self.events.append("quota_reserve")


def test_legacy_anchor_is_read_after_quota_and_before_binding_reads() -> None:
    events: list[str] = []
    session_factory = _ServiceSessionFactory(events)
    workspace_id = uuid4()
    asset_id = uuid4()
    page_id = uuid4()
    content = b"validated-content"
    digest = sha256(content).hexdigest()
    available = AvailableBlobRecord(
        reference=BlobReference(workspace_id, uuid4(), BlobEpoch(1)),
        sha256=Sha256Digest(digest),
        byte_size=len(content),
        storage_key="blobs/validated-content",
        canonical_media_type="image/png",
        inspection_id=uuid4(),
    )
    installer = SimpleNamespace(execute=lambda **_kwargs: available)
    service = ContextMediaUploadService(
        media_installer_factory=lambda _factory, _workspace_id: installer,
        logical_quota_factory=lambda _transaction: _OrderQuota(events),
        context_media_repository_factory=_OrderRepository,
    )

    result = service.register(
        session_factory=session_factory,
        workspace_id=workspace_id,
        asset_id=asset_id,
        original_filename="ordered.png",
        canonical_media_type="image/png",
        content=content,
        sha256=digest,
        page_id=page_id,
        editing_version_id=uuid4(),
        actor_kind="human",
        actor_id="ordered-actor",
        max_bytes=1024,
        validated_image=ValidatedContextImage("image/png", 1, 1, 1, 1),
        legacy_upload_asset_id=uuid4(),
    )

    assert events == [
        "quota_lock",
        "legacy_anchor_read",
        "quota_lock",
        "legacy_anchor_read",
        "request_read",
        "asset_read",
        "quota_reserve",
        "asset_add",
    ]
    assert result.reused is False

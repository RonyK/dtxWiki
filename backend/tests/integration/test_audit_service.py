import pytest
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.orm import sessionmaker

from backend.models.audit import AuditEvent
from backend.models.base import Base
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.audit_service import AuditService


def test_audit_service_appends_legacy_and_design_fields():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    audit_service = AuditService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        event = audit_service.emit(
            session=session,
            workspace_id=workspace.id,
            actor_kind="human",
            actor_id="dev-local-user",
            action="page.create",
            target_kind="context_page",
            target_id=workspace.id,
            payload={"content_hash": "hash-1"},
            event_summary="Created context page",
            retention_class="standard",
        )

    assert event.actor_kind == "human"
    assert event.actor_type == "human"
    assert event.target_kind == "context_page"
    assert event.target_type == "context_page"
    assert event.event_summary == "Created context page"
    assert event.retention_class == "standard"
    assert event.payload == {"content_hash": "hash-1"}


def test_audit_event_is_append_only_for_orm_update_and_delete():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    audit_service = AuditService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        event = audit_service.emit(
            session=session,
            workspace_id=workspace.id,
            actor_kind="human",
            actor_id="dev-local-user",
            action="page.create",
            target_kind="context_page",
            target_id=workspace.id,
        )
        event_id = event.id

    with session_factory.begin() as session:
        event = session.scalar(select(AuditEvent).where(AuditEvent.id == event_id))
        event.event_summary = "mutated"
        with pytest.raises(ValueError, match="append-only"):
            session.flush()

    with session_factory.begin() as session:
        event = session.scalar(select(AuditEvent).where(AuditEvent.id == event_id))
        session.delete(event)
        with pytest.raises(ValueError, match="append-only"):
            session.flush()


def test_audit_event_is_append_only_for_bulk_core_update_and_delete():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    audit_service = AuditService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        event = audit_service.emit(
            session=session,
            workspace_id=workspace.id,
            actor_kind="human",
            actor_id="dev-local-user",
            action="page.create",
            target_kind="context_page",
            target_id=workspace.id,
        )
        event_id = event.id

    with session_factory.begin() as session:
        with pytest.raises(ValueError, match="append-only"):
            session.execute(
                update(AuditEvent)
                .where(AuditEvent.id == event_id)
                .values(event_summary="mutated")
            )

    with session_factory.begin() as session:
        with pytest.raises(ValueError, match="append-only"):
            session.execute(delete(AuditEvent).where(AuditEvent.id == event_id))

    with session_factory.begin() as session:
        with pytest.raises(ValueError, match="append-only"):
            session.execute(delete(AuditEvent.__table__))

    with session_factory() as session:
        event = session.scalar(select(AuditEvent).where(AuditEvent.id == event_id))
        assert event is not None
        assert event.event_summary != "mutated"

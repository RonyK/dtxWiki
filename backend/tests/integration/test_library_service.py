import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.models.audit import AuditEvent
from backend.models.base import Base
from backend.models.job import Job
from backend.models.library import LibraryItem, LibraryItemVersion
from backend.models.library_discovery import (
    LibraryDiscoveryCandidate,
    LibraryDiscoveryRun,
)
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.library_service import LibraryCandidateInput, LibraryService
from backend.services.library_storage import INLINE_TEXT_PREFIX
from backend.utils.content_hash import content_hash


def make_session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_list_item_references_hidden_when_archived():
    # Codex round-12: a read of an archived item's references must 404 (no
    # references oracle), while deletion still operates on archived items.
    from backend.services.read_errors import ReadNotFoundError

    session_factory = make_session_factory()
    library_service = LibraryService()
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        registration = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/ref-archived",
            title="Ref Archived",
            raw_text="# body",
        )
        # Live: references resolve.
        assert (
            library_service.list_item_references(
                session, workspace.id, registration.library_item.id
            )
            == []
        )
        registration.library_item.lifecycle_status = "archived"
        session.flush()
        with pytest.raises(ReadNotFoundError):
            library_service.list_item_references(
                session, workspace.id, registration.library_item.id
            )


def test_list_library_items_excludes_archived_by_default():
    # Codex round-12: the default library listing hides archived/hidden items;
    # viewing them requires an explicit lifecycle filter.
    from backend.services.domain_read import DomainReadService

    session_factory = make_session_factory()
    library_service = LibraryService()
    read_service = DomainReadService()
    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        live = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/live-list",
            title="Live List",
            raw_text="x",
        )
        archived = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/archived-list",
            title="Archived List",
            raw_text="y",
        )
        archived.library_item.lifecycle_status = "archived"
        session.flush()

        default_ids = {
            item.id
            for item in read_service.list_library_items(
                session=session, workspace_id=workspace.id
            ).items
        }
        archived_ids = {
            item.id
            for item in read_service.list_library_items(
                session=session, workspace_id=workspace.id, lifecycle="archived"
            ).items
        }

    assert live.library_item.id in default_ids
    assert archived.library_item.id not in default_ids
    assert archived.library_item.id in archived_ids


def test_library_discovery_manual_policy_keeps_candidates_pending():
    session_factory = make_session_factory()
    library_service = LibraryService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        run = library_service.run_discovery(
            session=session,
            workspace=workspace,
            run_token="manual-run-1",
            candidates=[
                LibraryCandidateInput(
                    canonical_uri="https://example.com/a",
                    title="A",
                    confidence="high",
                ),
                LibraryCandidateInput(
                    canonical_uri="https://example.com/b",
                    title="B",
                ),
            ],
        )

    with session_factory.begin() as session:
        stored_run = session.get(LibraryDiscoveryRun, run.id)
        candidates = session.scalars(
            select(LibraryDiscoveryCandidate).order_by(LibraryDiscoveryCandidate.title)
        ).all()
        items = session.scalars(select(LibraryItem)).all()

    assert stored_run.status == "completed"
    assert stored_run.candidate_count == 2
    assert stored_run.accepted_count == 0
    assert stored_run.excluded_count == 0
    assert len(candidates) == 2
    assert {candidate.decision_status for candidate in candidates} == {"pending_review"}
    assert {candidate.decision_reason for candidate in candidates} == {
        "manual_approval_required"
    }
    assert items == []


@pytest.mark.parametrize("invalid_value", [0, "false", None])
def test_library_discovery_normalizes_invalid_manual_review_policy_fail_closed(
    invalid_value,
):
    session_factory = make_session_factory()
    library_service = LibraryService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        workspace.library_policy = {
            **workspace.library_policy,
            "manual_approval_required": invalid_value,
        }
        run = library_service.run_discovery(
            session=session,
            workspace=workspace,
            run_token="invalid-manual-review-policy",
            candidates=[
                LibraryCandidateInput(
                    canonical_uri="https://example.com/fail-closed",
                    title="Fail closed",
                )
            ],
        )

    with session_factory.begin() as session:
        candidate = session.scalar(select(LibraryDiscoveryCandidate))
        items = session.scalars(select(LibraryItem)).all()

    assert run.accepted_count == 0
    assert candidate.decision_status == "pending_review"
    assert candidate.decision_reason == "manual_approval_required"
    assert items == []


def test_library_discovery_auto_registers_sources_and_extraction_jobs():
    session_factory = make_session_factory()
    library_service = LibraryService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        workspace.library_policy = {
            **workspace.library_policy,
            "manual_approval_required": False,
        }
        run = library_service.run_discovery(
            session=session,
            workspace=workspace,
            run_token="auto-run-1",
            candidates=[
                LibraryCandidateInput(
                    canonical_uri="https://example.com/accepted",
                    title="Accepted",
                    format_hint="html",
                    trust_level="trusted",
                    raw_text="# Accepted\n\nBody",
                )
            ],
        )

    with session_factory.begin() as session:
        stored_run = session.get(LibraryDiscoveryRun, run.id)
        candidate = session.scalar(select(LibraryDiscoveryCandidate))
        item = session.scalar(select(LibraryItem))
        version = session.scalar(select(LibraryItemVersion))
        job = session.scalar(select(Job).where(Job.job_type == "extraction"))
        register_audit = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "library.item.register")
        )

    assert stored_run.accepted_count == 1
    assert stored_run.excluded_count == 0
    assert candidate.decision_status == "accepted"
    assert candidate.accepted_library_item_id == item.id
    assert candidate.decided_at is not None
    assert item.uri == "https://example.com/accepted"
    assert item.lifecycle_status == "extract_pending"
    assert item.current_version_id == version.id
    assert version.content_hash == content_hash("# Accepted\n\nBody")
    assert version.raw_storage_ref.startswith(INLINE_TEXT_PREFIX)
    assert job.status == "queued"
    assert job.idempotency_key == str(version.id)
    assert job.target_ref == {
        "library_item_id": str(item.id),
        "library_item_version_id": str(version.id),
    }
    assert register_audit.target_id == item.id
    assert register_audit.payload["library_item_version_id"] == str(version.id)


def test_library_registration_reuses_same_content_version_and_job():
    session_factory = make_session_factory()
    library_service = LibraryService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        first = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/reused",
            title="Reused",
            raw_text="same body",
        )
        second = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/reused",
            title="Reused",
            raw_text="same body",
        )

    with session_factory.begin() as session:
        items = session.scalars(select(LibraryItem)).all()
        versions = session.scalars(select(LibraryItemVersion)).all()
        jobs = session.scalars(select(Job)).all()
        audits = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "library.item.register")
        ).all()

    assert first.library_item.id == second.library_item.id
    assert first.version.id == second.version.id
    assert first.extraction_job.id == second.extraction_job.id
    assert first.created_library_item is True
    assert first.created_version is True
    assert second.created_library_item is False
    assert second.created_version is False
    assert len(items) == 1
    assert len(versions) == 1
    assert len(jobs) == 1
    assert len(audits) == 1


def test_library_registration_converges_after_stale_unique_check(monkeypatch):
    session_factory = make_session_factory()
    library_service = LibraryService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        winner = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/concurrent",
            title="Concurrent winner",
            raw_text="winner body",
        )

        original_get = library_service.get_library_item_by_uri
        calls = 0

        def stale_then_current(*, session, workspace_id, uri):
            nonlocal calls
            calls += 1
            if calls == 1:
                return None
            return original_get(
                session=session,
                workspace_id=workspace_id,
                uri=uri,
            )

        monkeypatch.setattr(
            library_service,
            "get_library_item_by_uri",
            stale_then_current,
        )
        converged = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/concurrent",
            title="Concurrent loser",
            raw_text="loser body",
        )

    assert converged.library_item.id == winner.library_item.id
    assert converged.created_library_item is False
    assert calls >= 2


def test_library_registration_locks_existing_item_before_version_insert(monkeypatch):
    session_factory = make_session_factory()
    library_service = LibraryService()

    with session_factory.begin() as session:
        workspace = seed_default_workspace(session)
        first = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/version-lock",
            title="Version lock",
            raw_text="first body",
        )

        original_lock = library_service._lock_library_item_for_version_insert
        locked_item_ids = []

        def tracked_lock(*, session, library_item_id):
            locked_item_ids.append(library_item_id)
            return original_lock(
                session=session,
                library_item_id=library_item_id,
            )

        monkeypatch.setattr(
            library_service,
            "_lock_library_item_for_version_insert",
            tracked_lock,
        )
        second = library_service.register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/version-lock",
            title="Version lock",
            raw_text="second body",
        )

    assert locked_item_ids == [first.library_item.id]
    assert second.version.version_no == 2

"""Workspace retention persistence, owner boundary and optimistic concurrency."""

from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.audit import AuditEvent
from backend.models.workspace import Workspace
from backend.services.context_media_policy import MAX_ORPHAN_RETENTION_HOURS
from backend.tests.conftest import authenticate_test_client_in_workspace


def client_for(page_service, *, role="owner", hours=168):
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
                context_media_orphan_retention_hours=hours,
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client_in_workspace(
        client,
        page_service.session_factory,
        email=f"retention-{role}-{uuid4().hex}@example.com",
        workspace_slug="default",
        role=role,
    )
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    return client, workspace_id


def card(client, workspace_id):
    response = client.get(f"/api/v1/workspaces/{workspace_id}/settings/policies")
    assert response.status_code == 200, response.text
    return response.json()["data"]["attachments"]


def save(client, workspace_id, revision, hours):
    return client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/policies/batch",
        json={
            "group": "attachments",
            "expected_revision": revision,
            "changes": {"orphan_retention_hours": hours},
        },
    )


@pytest.mark.parametrize("default", [168, 72, 36, 12, 0])
def test_inheritance_override_restore_and_restart(page_service, default):
    client, workspace_id = client_for(page_service, hours=default)
    baseline = card(client, workspace_id)
    assert baseline["policy"]["source"] == "server"
    assert baseline["policy"]["orphan_retention_hours"] is None
    assert baseline["policy"]["effective_orphan_retention_hours"] == default
    with page_service.session_factory() as session:
        assert session.get(Workspace, UUID(workspace_id)).context_media_policy == {}
    response = save(client, workspace_id, baseline["revision"], 36)
    assert response.status_code == 200, response.text
    updated = response.json()["data"]["attachments"]
    assert updated["policy"]["effective_orphan_retention_hours"] == 36
    assert updated["policy"]["source"] == "workspace"
    restarted, _ = client_for(page_service, hours=0)
    after_restart = card(restarted, workspace_id)
    assert after_restart["policy"]["effective_orphan_retention_hours"] == 36
    # A server default change participates in CAS even while overridden.
    assert after_restart["revision"] != updated["revision"] or default == 0
    if default != 0:
        assert save(restarted, workspace_id, updated["revision"], 72).status_code == 409
    restored = save(restarted, workspace_id, after_restart["revision"], None)
    assert restored.status_code == 200
    assert (
        restored.json()["data"]["attachments"]["policy"][
            "effective_orphan_retention_hours"
        ]
        == 0
    )
    with page_service.session_factory() as session:
        assert session.get(Workspace, UUID(workspace_id)).context_media_policy == {}
        event = session.scalar(
            select(AuditEvent)
            .where(AuditEvent.action == "settings.policy.attachments.updated")
            .order_by(AuditEvent.created_at.desc())
        )
        assert event.payload["after"]["source"] == "server"


@pytest.mark.parametrize("bad", [-1, 1.5, True, "12", MAX_ORPHAN_RETENTION_HOURS + 1])
def test_invalid_values_do_not_change_policy(page_service, bad):
    client, workspace_id = client_for(page_service)
    baseline = card(client, workspace_id)
    response = save(client, workspace_id, baseline["revision"], bad)
    assert response.status_code == 422
    assert card(client, workspace_id) == baseline


def test_cas_and_legacy_bypass(page_service):
    client, workspace_id = client_for(page_service)
    baseline = card(client, workspace_id)
    assert save(client, workspace_id, baseline["revision"], 0).status_code == 200
    assert save(client, workspace_id, baseline["revision"], 72).status_code == 409
    legacy = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/policies",
        json={"group": "attachments", "key": "orphan_retention_hours", "value": 72},
    )
    assert legacy.status_code == 422
    assert card(client, workspace_id)["policy"]["effective_orphan_retention_hours"] == 0
    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/context/media/attachments"
    )
    assert (
        response.json()["data"]["retention_policy"]["revision"]
        == card(client, workspace_id)["revision"]
    )


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_members_can_read_but_only_owner_can_save(page_service, role):
    owner, workspace_id = client_for(page_service)
    member, _ = client_for(page_service, role=role)
    baseline = card(member, workspace_id)
    assert save(member, workspace_id, baseline["revision"], 0).status_code == 403
    assert card(owner, workspace_id) == baseline


def test_api_changes_apply_to_real_queued_sweeps_without_restarting_worker(
    page_service, monkeypatch
):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from backend.context_media.adapters.sqlalchemy import (
        SqlAlchemyContextMediaRepository,
    )
    from backend.models.context_media import ContextMediaAsset
    from backend.services.context_media_orphan_sweep import (
        ContextMediaOrphanSweepService,
    )
    from backend.services.job_queue import JobQueueService
    from backend.tests.integration.test_context_media_attachments_api import (
        _seed_asset,
    )
    from backend.worker.runtime import OneShotWorker

    client, workspace_id = client_for(page_service, hours=0)
    workspace_uuid = UUID(workspace_id)
    from backend.models.user import User

    with page_service.session_factory() as session:
        actor_id = str(
            session.scalar(select(User).where(User.email.like("retention-owner-%"))).id
        )
    run_at = datetime(2026, 10, 19, tzinfo=UTC)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return run_at

    monkeypatch.setattr("backend.worker.runtime.datetime", FrozenDatetime)
    queue = JobQueueService()
    worker = OneShotWorker(
        settings=Settings(context_media_orphan_retention_hours=0),
        context_media_orphan_sweep_service=ContextMediaOrphanSweepService(
            SqlAlchemyContextMediaRepository
        ),
    )
    seeded = _seed_asset(page_service, workspace_uuid, created_by_actor_id=actor_id)
    with page_service.session_factory.begin() as session:
        session.execute(
            update(ContextMediaAsset)
            .where(ContextMediaAsset.workspace_id == workspace_uuid)
            .values(created_at=run_at - timedelta(days=4))
        )
    for hours, reclaimed in ((168, 0), (336, 0), (0, 0), (72, 1)):
        # Queue first: old payload and enqueue time must never determine the cutoff.
        with page_service.session_factory.begin() as session:
            queue.enqueue(
                session=session,
                workspace_id=workspace_uuid,
                job_type="context_media_orphan_sweep",
                trigger_type="scheduled",
                idempotency_key=f"old-{hours}",
                target_ref={
                    "scheduled_for": "2000-01-01T00:00:00+00:00",
                    "retention_hours": 1,
                },
            )
        current = card(client, workspace_id)
        assert save(client, workspace_id, current["revision"], hours).status_code == 200
        result = worker.dispatch_once_with_session_factory(
            page_service.session_factory,
            workspace_id=workspace_uuid,
            job_types={"context_media_orphan_sweep"},
        )
        assert result.status == "completed", result
        assert result.detail["reclaimed_count"] == reclaimed
    with page_service.session_factory() as session:
        assert (
            session.get(ContextMediaAsset, (workspace_uuid, UUID(seeded["asset_id"])))
            is None
        )


def test_server_admin_without_membership_cannot_read_or_patch_policy(page_service):
    from backend.models.user import User
    from backend.models.workspace import WorkspaceMembership
    from backend.tests.integration.test_context_media_orphan_api import (
        make_server_admin_client,
    )

    owner, workspace_id = client_for(page_service)
    baseline = card(owner, workspace_id)
    admin = make_server_admin_client(page_service, "policy-outsider-admin@example.com")
    with page_service.session_factory.begin() as session:
        user = session.scalar(
            select(User).where(User.email == "policy-outsider-admin@example.com")
        )
        membership = session.scalar(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == UUID(workspace_id),
                WorkspaceMembership.user_id == user.id,
            )
        )
        session.delete(membership)
    assert save(admin, workspace_id, baseline["revision"], 0).status_code in (403, 404)
    assert admin.get(
        f"/api/v1/workspaces/{workspace_id}/settings/policies"
    ).status_code in (403, 404)
    assert card(owner, workspace_id) == baseline


def test_sweep_keeps_starting_snapshot_when_policy_is_saved_mid_run(page_service):
    from datetime import UTC, datetime

    from backend.context_media.adapters.sqlalchemy import (
        SqlAlchemyContextMediaRepository,
    )
    from backend.models.context_media import ContextMediaAsset
    from backend.models.user import User
    from backend.services.context_media_orphan_sweep import (
        ContextMediaOrphanSweepService,
    )
    from backend.services.job_queue import JobQueueService
    from backend.tests.integration.test_context_media_attachments_api import _seed_asset
    from backend.worker.runtime import OneShotWorker

    client, workspace_id = client_for(page_service)
    current = card(client, workspace_id)
    assert save(client, workspace_id, current["revision"], 72).status_code == 200
    with page_service.session_factory() as session:
        actor_id = str(
            session.scalar(select(User).where(User.email.like("retention-owner-%"))).id
        )
    seeded = _seed_asset(page_service, UUID(workspace_id), created_by_actor_id=actor_id)
    real_sweep = ContextMediaOrphanSweepService(SqlAlchemyContextMediaRepository)

    class SaveAfterSnapshot:
        def sweep(self, session, *, workspace_id, retention_hours, now):
            assert retention_hours == 72
            current = card(client, str(workspace_id))
            assert (
                save(client, str(workspace_id), current["revision"], 0).status_code
                == 200
            )
            return real_sweep.sweep(
                session,
                workspace_id=workspace_id,
                retention_hours=retention_hours,
                now=now,
            )

    with page_service.session_factory.begin() as session:
        session.get(
            ContextMediaAsset, (UUID(workspace_id), UUID(seeded["asset_id"]))
        ).created_at = datetime(2020, 1, 1, tzinfo=UTC)
        JobQueueService().enqueue(
            session=session,
            workspace_id=UUID(workspace_id),
            job_type="context_media_orphan_sweep",
            trigger_type="scheduled",
            idempotency_key="starting-policy-snapshot",
            target_ref={},
        )
    worker = OneShotWorker(
        settings=Settings(), context_media_orphan_sweep_service=SaveAfterSnapshot()
    )
    result = worker.dispatch_once_with_session_factory(
        page_service.session_factory,
        workspace_id=UUID(workspace_id),
        job_types={"context_media_orphan_sweep"},
    )
    assert result.status == "completed"
    assert result.detail["reclaimed_count"] == 1
    assert card(client, workspace_id)["policy"]["effective_orphan_retention_hours"] == 0
    with page_service.session_factory() as session:
        assert (
            session.get(
                ContextMediaAsset, (UUID(workspace_id), UUID(seeded["asset_id"]))
            )
            is None
        )

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.audit import AuditEvent
from backend.models.review import ReviewDecision, ReviewItem
from backend.models.user import User
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.policy.rbac import (
    RejectedByPolicyError,
    WorkspacePlaneUnavailableError,
    grant_workspace_membership,
    remove_workspace_membership,
    require_workspace_role,
)
from backend.repositories.workspace_repo import insert_workspace
from backend.schemas.context_write import WriteDraftRequest
from backend.services.context_write_service import ContextWriteService
from backend.tests.conftest import authenticate_test_client


def make_client(page_service, email: str) -> TestClient:
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client(client, email=email)
    return client


def make_peer_client(client: TestClient, email: str) -> TestClient:
    """Second actor on the same app: separate cookie jar, no extra create_app."""
    peer = TestClient(client.app)
    authenticate_test_client(peer, email=email)
    return peer


def test_workspace_list_filters_to_actor_memberships(page_service):
    client = make_client(page_service, "owner@example.com")
    with page_service.session_factory.begin() as session:
        other = insert_workspace(session, slug="private", title="Private")
        other_id = other.id

    workspaces = client.get("/api/v1/workspaces")
    assert workspaces.status_code == 200
    slugs = [
        workspace["slug"] for workspace in workspaces.json()["data"]["workspaces"]
    ]
    assert len(slugs) == 1
    assert slugs[0].startswith("personal-")

    denied = client.get(f"/api/v1/workspaces/{other_id}")
    assert denied.status_code == 404
    assert denied.json()["error_code"] == "not_found"


def test_viewer_write_is_rejected_and_editor_write_is_allowed(page_service):
    owner = make_client(page_service, "owner@example.com")
    viewer = make_peer_client(owner, "viewer@example.com")
    workspace_id = owner.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    pages_url = f"/api/v1/workspaces/{workspace_id}/context/pages"
    set_role(page_service, "viewer@example.com", workspace_id, "viewer")

    rejected = viewer.post(
        pages_url,
        json={"title": "Viewer Write", "body_md": "# Viewer Write"},
    )
    assert rejected.status_code == 403
    assert rejected.json()["error_code"] == "workspace_role_insufficient"

    set_role(page_service, "viewer@example.com", workspace_id, "editor")
    allowed = viewer.post(
        pages_url,
        json={"title": "Editor Write", "body_md": "# Editor Write"},
    )
    assert allowed.status_code == 201
    assert allowed.json()["data"]["title"] == "Editor Write"


def test_removed_workspace_member_cannot_read_direct_workspace(page_service):
    client = make_client(page_service, "member@example.com")
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    with page_service.session_factory.begin() as session:
        user = session.scalar(select(User).where(User.email == "member@example.com"))
        assert user is not None
        remove_workspace_membership(
            session,
            user_id=user.id,
            workspace_id=UUID(workspace_id),
        )

    denied = client.get(f"/api/v1/workspaces/{workspace_id}")
    assert denied.status_code == 404
    assert denied.json()["error_code"] == "not_found"


def test_owner_can_create_select_and_delete_empty_workspace(page_service):
    client = make_client(page_service, "owner-create@example.com")

    created = client.post(
        "/api/v1/workspaces",
        json={"title": "Research Workspace"},
    )
    assert created.status_code == 201
    payload = created.json()["data"]
    assert payload["slug"] == "research-workspace"
    assert payload["role"] == "owner"

    with page_service.session_factory.begin() as session:
        sentinel = uuid4()
        session.execute(
            text(
                "INSERT INTO search_doc_fts "
                "(context_header, body, workspace_id, source_kind, entity_id, "
                "version_id, visibility_scope, category) VALUES "
                "('workspace purge', 'workspace purge', :workspace_id, "
                "'agent_page', :entity_id, :version_id, 'draft', 'persona')"
            ),
            {
                "workspace_id": payload["id"],
                "entity_id": str(sentinel),
                "version_id": str(uuid4()),
            },
        )
        session.execute(
            text(
                "INSERT INTO page_name_trgm "
                "(name, workspace_id, page_id, source_version_id, "
                "visibility_scope, name_kind, name_norm) VALUES "
                "('workspace-purge', :workspace_id, :page_id, :version_id, "
                "'published', 'slug', 'workspace-purge')"
            ),
            {
                "workspace_id": payload["id"],
                "page_id": str(sentinel),
                "version_id": str(uuid4()),
            },
        )

    workspaces = client.get("/api/v1/workspaces")
    assert workspaces.status_code == 200
    workspace_slugs = {
        workspace["slug"] for workspace in workspaces.json()["data"]["workspaces"]
    }
    assert "research-workspace" in workspace_slugs
    assert len(workspace_slugs) == 2
    assert len(
        [slug for slug in workspace_slugs if slug.startswith("personal-")]
    ) == 1

    deleted = client.delete(f"/api/v1/workspaces/{payload['id']}")
    assert deleted.status_code == 200
    assert deleted.json()["data"]["deleted_workspace_id"] == payload["id"]
    with page_service.session_factory() as session:
        assert session.scalar(
            text(
                "SELECT count(*) FROM search_doc_fts WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": payload["id"]},
        ) == 0
        assert session.scalar(
            text(
                "SELECT count(*) FROM page_name_trgm WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": payload["id"]},
        ) == 0


def test_workspace_delete_detaches_audit_history_instead_of_blocking(
    page_service,
):
    # WS-02: ordinary admin actions (add/remove a member) leave audit_event
    # rows behind that a user has no way to clear, so counting them as a
    # hard-delete blocker made the workspace permanently undeletable after
    # the first such action. The fix keeps deletion a real (hard) delete:
    # ``audit_event.workspace_id`` is ``ON DELETE SET NULL`` (see
    # backend/models/audit.py and the forward 0005 repair migration), so the
    # database
    # detaches -- never blocks on, never deletes -- the append-only audit
    # rows when the workspace row itself goes away.
    with page_service.session_factory() as session:
        # SQLite disables FK enforcement by default. This fixture's engine
        # (backend/tests/conftest.py::session_factory) never turns it on, so
        # without this the ``ON DELETE SET NULL`` action would silently
        # no-op here and only ever be exercised against Postgres in
        # production -- exactly the kind of gap that let the missing
        # migration DDL stay invisible before. StaticPool backs the whole
        # factory with a single physical connection, so setting the pragma
        # once here keeps it on for every session used below, including the
        # one the app builds internally to serve the DELETE request.
        session.execute(text("PRAGMA foreign_keys=ON"))
        session.commit()

    owner = make_client(page_service, "owner-audit-delete@example.com")
    # Authenticating this second client is enough to provision its user row
    # so the owner can add it by email below.
    make_client(page_service, "member-audit-delete@example.com")

    created = owner.post(
        "/api/v1/workspaces",
        json={"title": "Audit History Workspace"},
    ).json()["data"]
    workspace_id = UUID(created["id"])
    original_slug = created["slug"]

    added = owner.post(
        f"/api/v1/workspaces/{workspace_id}/members",
        json={
            "email": "member-audit-delete@example.com",
            "role": "viewer",
            "confirm_first_share": True,
        },
    )
    assert added.status_code == 201
    member_user_id = UUID(added.json()["data"]["member"]["user_id"])

    removed = owner.delete(
        f"/api/v1/workspaces/{workspace_id}/members/{member_user_id}"
    )
    assert removed.status_code == 200

    with page_service.session_factory() as session:
        audit_ids_before = tuple(
            session.scalars(
                select(AuditEvent.id).where(AuditEvent.workspace_id == workspace_id)
            )
        )
        audit_count_before = len(audit_ids_before)
    # The add and the remove each emit a sensitive audit row.
    assert audit_count_before >= 2

    deleted = owner.delete(f"/api/v1/workspaces/{workspace_id}")
    assert deleted.status_code == 200
    assert deleted.json()["data"]["deleted_workspace_id"] == str(workspace_id)

    with page_service.session_factory() as session:
        # The workspace row is actually gone -- this is a real (hard)
        # delete, not a soft delete.
        assert session.get(Workspace, workspace_id) is None
        # Scoped to target_kind="workspace_membership" (the add/remove rows)
        # rather than just target_id -- that user id is also the target of
        # an unrelated, always-workspace-agnostic "auth.user.signed_up"
        # event (target_kind="user"), which would otherwise inflate this
        # count independent of the FK behavior under test.
        detached = session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.target_id == member_user_id,
                AuditEvent.target_kind == "workspace_membership",
                AuditEvent.workspace_id.is_(None),
            )
        )
        all_original_audits_detached = session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.id.in_(audit_ids_before),
                AuditEvent.workspace_id.is_(None),
            )
        )
        still_attached = session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.workspace_id == workspace_id)
        )
    # Every audit row that referenced the deleted workspace was detached
    # (workspace_id set to NULL by the database) -- none were deleted
    # themselves, and none are left dangling on a now-nonexistent
    # workspace_id.
    assert detached >= 2
    assert all_original_audits_detached == audit_count_before
    assert still_attached == 0

    # The human-readable slug is free for reuse immediately -- a hard
    # delete leaves nothing behind to collide with a new workspace.
    recreated = owner.post(
        "/api/v1/workspaces",
        json={"title": "Audit History Workspace"},
    )
    assert recreated.status_code == 201
    assert recreated.json()["data"]["slug"] == original_slug


def test_workspace_delete_allows_last_personal_and_rejects_non_empty_workspace(
    page_service,
):
    client = make_client(page_service, "owner-delete@example.com")
    personal_workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    personal_delete = client.delete(f"/api/v1/workspaces/{personal_workspace_id}")
    assert personal_delete.status_code == 200
    assert personal_delete.json()["data"]["deleted_workspace_id"] == (
        personal_workspace_id
    )
    with page_service.session_factory() as session:
        user = session.scalar(
            select(User).where(User.email == "owner-delete@example.com")
        )
        assert user is not None
        assert user.default_workspace_id is None
        assert user.personal_workspace_id is None
    empty_list = client.get("/api/v1/workspaces")
    assert empty_list.status_code == 200
    assert empty_list.json()["data"]["workspaces"] == []
    assert empty_list.json()["data"]["current_workspace_id"] is None

    assert client.post("/api/v1/auth/logout").status_code == 200
    relogin = client.post(
        "/api/v1/auth/login",
        json={
            "email": "owner-delete@example.com",
            "password": "correct-password",
        },
    )
    assert relogin.status_code == 200
    after_relogin = client.get("/api/v1/workspaces")
    assert after_relogin.status_code == 200
    assert after_relogin.json()["data"]["workspaces"] == []
    assert after_relogin.json()["data"]["personal_workspace_id"] is None

    created = client.post(
        "/api/v1/workspaces",
        json={"title": "Non Empty Workspace"},
    ).json()["data"]
    page = client.post(
        f"/api/v1/workspaces/{created['id']}/context/pages",
        json={"title": "Keep Me", "body_md": "# Keep Me"},
    )
    assert page.status_code == 201

    rejected = client.delete(f"/api/v1/workspaces/{created['id']}")
    assert rejected.status_code == 409
    assert rejected.json()["error_code"] == "workspace_delete_rejected"
    blockers = rejected.json()["blockers"]
    assert {"code": "context_pages", "count": 1} in blockers
    assert [blocker["code"] for blocker in blockers] == sorted(
        blocker["code"] for blocker in blockers
    )
    assert all(blocker["count"] > 0 for blocker in blockers)
    assert "context_pages" in rejected.json()["message"]


def test_purge_is_refused_before_anything_is_deleted_when_the_store_cannot_purge(
    page_service,
):
    """260912 review, F-1 (FIX_PLAN P6a).

    Before this fix, an unsupported blob store's missing `purge_workspace`
    surfaced as an AttributeError inside a broad `except Exception` -- AFTER
    the workspace row and its content were already gone, so the failure was
    a silent, permanent byte leak rather than a rejected request. The
    capability check now runs before the delete transaction opens at all.
    """

    client = make_client(page_service, "owner-purge-unavailable@example.com")
    created = client.post(
        "/api/v1/workspaces",
        json={"title": "Purge Capability Workspace"},
    ).json()["data"]
    workspace_id = created["id"]

    class _StoreWithoutPurgeSupport:
        """Deliberately has no `purge_workspace` -- the pre-fix duck type."""

    container = client.app.state.container
    container.get_services().blob_store_provider.store_for = (
        lambda _workspace_id: _StoreWithoutPurgeSupport()
    )

    response = client.delete(f"/api/v1/workspaces/{workspace_id}?purge=true")

    assert response.status_code == 503
    assert response.json()["error_code"] == "purge_unavailable"
    # Nothing was deleted: the check runs before the transaction opens.
    still_there = client.get(f"/api/v1/workspaces/{workspace_id}")
    assert still_there.status_code == 200


def test_workspace_delete_allows_personal_after_default_changes(page_service):
    client = make_client(page_service, "personal-delete@example.com")
    listed = client.get("/api/v1/workspaces").json()["data"]
    personal_id = listed["current_workspace_id"]
    created = client.post(
        "/api/v1/workspaces",
        json={"title": "Replacement Default"},
    ).json()["data"]
    changed = client.patch(
        "/api/v1/users/me/default-workspace",
        json={"workspace_id": created["id"]},
    )
    assert changed.status_code == 200

    deleted = client.delete(f"/api/v1/workspaces/{personal_id}")

    assert deleted.status_code == 200
    assert deleted.json()["data"]["deleted_workspace_id"] == personal_id
    with page_service.session_factory() as session:
        user = session.scalar(
            select(User).where(User.email == "personal-delete@example.com")
        )
        assert user is not None
        assert user.personal_workspace_id is None
        assert user.default_workspace_id == UUID(created["id"])


def test_workspace_with_default_slug_has_no_special_delete_protection(page_service):
    client = make_client(page_service, "legacy-default-delete@example.com")
    with page_service.session_factory.begin() as session:
        user = session.scalar(
            select(User).where(User.email == "legacy-default-delete@example.com")
        )
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        assert user is not None
        assert workspace is not None
        personal_workspace_id = user.personal_workspace_id
        default_workspace_id = user.default_workspace_id
        assert personal_workspace_id is not None
        grant_workspace_membership(
            session,
            user_id=user.id,
            workspace_id=workspace.id,
            role="owner",
        )
        workspace_id = workspace.id

    deleted = client.delete(f"/api/v1/workspaces/{workspace_id}")

    assert deleted.status_code == 200
    assert deleted.json()["data"]["deleted_workspace_id"] == str(workspace_id)
    with page_service.session_factory() as session:
        user = session.scalar(
            select(User).where(User.email == "legacy-default-delete@example.com")
        )
        assert user is not None
        assert session.get(Workspace, workspace_id) is None
        assert user.personal_workspace_id == personal_workspace_id
        assert user.default_workspace_id == default_workspace_id


def _seed_review_item(page_service, workspace_id: str, title: str) -> str:
    """Create a context draft so a pending review_item exists, returning its id."""
    with page_service.session_factory.begin() as session:
        draft = ContextWriteService().write_draft(
            session=session,
            workspace_id=UUID(workspace_id),
            request=WriteDraftRequest(
                title=title,
                page_type="overview",
                body_markdown=f"# {title}\n\nBody.",
                reason="seed review item",
            ),
            actor_kind="human",
            actor_id="00000000-0000-0000-0000-000000000010",
            actor_role="owner",
        )
        return str(draft.review_item_id)


def test_list_reviews_rejects_non_member(page_service):
    """T11/F1: list_reviews now gates on viewer membership; a non-member of the
    private workspace must receive the uniform 404 rather than learning that
    another workspace's review surface exists.

    Signup creates only a Personal workspace, so the gate is exercised against
    a separately created workspace the outsider never joined.
    """
    owner = make_client(page_service, "review-owner@example.com")
    outsider = make_client(page_service, "review-outsider@example.com")
    workspace_id = owner.post(
        "/api/v1/workspaces",
        json={"title": "Review Workspace"},
    ).json()["data"]["id"]
    _seed_review_item(page_service, workspace_id, title="List Review Target")

    denied = outsider.get(f"/api/v1/workspaces/{workspace_id}/reviews")
    assert denied.status_code == 404
    assert denied.json()["error_code"] == "not_found"

    allowed = owner.get(f"/api/v1/workspaces/{workspace_id}/reviews")
    assert allowed.status_code == 200
    assert len(allowed.json()["data"]["items"]) >= 1

    counts = owner.get(f"/api/v1/workspaces/{workspace_id}/reviews/counts")
    assert counts.status_code == 200
    assert counts.json()["data"]["pending"] >= 1


def test_decide_review_rejects_viewer_and_allows_editor(page_service):
    """T11/F1: decide_review now gates on editor membership as the first
    statement; a viewer is rejected (403) and an editor succeeds."""
    owner = make_client(page_service, "decide-owner@example.com")
    reviewer = make_client(page_service, "decide-reviewer@example.com")
    workspace_id = owner.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    review_item_id = _seed_review_item(
        page_service, workspace_id, title="Decide Review Target"
    )

    set_role(page_service, "decide-reviewer@example.com", workspace_id, "viewer")
    rejected = reviewer.post(
        f"/api/v1/workspaces/{workspace_id}/reviews/{review_item_id}/decision",
        json={"decision_type": "approve"},
    )
    assert rejected.status_code == 403
    assert rejected.json()["error_code"] == "workspace_role_insufficient"

    set_role(page_service, "decide-reviewer@example.com", workspace_id, "editor")
    allowed = reviewer.post(
        f"/api/v1/workspaces/{workspace_id}/reviews/{review_item_id}/decision",
        json={"decision_type": "approve"},
    )
    assert allowed.status_code == 200
    assert allowed.json()["data"]["status"] == "approved"


def test_bulk_decide_reviews_best_effort_and_editor_gated(page_service):
    owner = make_client(page_service, "bulk-owner@example.com")
    viewer = make_client(page_service, "bulk-viewer@example.com")
    workspace_id = owner.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    approve_id = _seed_review_item(
        page_service, workspace_id, title="Bulk Approve Target"
    )
    skip_id = _seed_review_item(
        page_service, workspace_id, title="Bulk Skip Target"
    )
    already_resolved = owner.post(
        f"/api/v1/workspaces/{workspace_id}/reviews/{skip_id}/decision",
        json={"decision_type": "approve"},
    )
    assert already_resolved.status_code == 200

    set_role(page_service, "bulk-viewer@example.com", workspace_id, "viewer")
    denied = viewer.post(
        f"/api/v1/workspaces/{workspace_id}/reviews/decide-bulk",
        json={
            "review_item_ids": [approve_id],
            "filters": None,
            "decision_type": "approve",
        },
    )
    assert denied.status_code == 403
    assert denied.json()["error_code"] == "workspace_role_insufficient"

    allowed = owner.post(
        f"/api/v1/workspaces/{workspace_id}/reviews/decide-bulk",
        json={
            "review_item_ids": [approve_id, skip_id],
            "filters": None,
            "decision_type": "approve",
        },
    )
    assert allowed.status_code == 200
    payload = allowed.json()["data"]
    assert payload["approved"] == [approve_id]
    assert payload["skipped"][0]["id"] == skip_id

    listed = owner.get(
        f"/api/v1/workspaces/{workspace_id}/reviews?status=resolved"
    )
    assert listed.status_code == 200
    statuses = {item["id"]: item["status"] for item in listed.json()["data"]["items"]}
    assert statuses[approve_id] == "approved"
    assert statuses[skip_id] == "approved"

    filter_id = _seed_review_item(
        page_service, workspace_id, title="Bulk Filter Target"
    )
    filtered = owner.post(
        f"/api/v1/workspaces/{workspace_id}/reviews/decide-bulk",
        json={
            "review_item_ids": None,
            "filters": {"status": "pending", "risk_level": "high", "scope": "all"},
            "decision_type": "approve",
        },
    )
    assert filtered.status_code == 200
    assert filtered.json()["data"]["approved"] == [filter_id]


def test_list_reviews_does_not_sweep_auto_approve_policy(page_service):
    owner = make_client(page_service, "auto-review-owner@example.com")
    workspace_id = owner.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    review_item_id = _seed_review_item(
        page_service, workspace_id, title="Auto Approved Target"
    )

    with page_service.session_factory.begin() as session:
        workspace = session.get(Workspace, UUID(workspace_id))
        policy = dict(workspace.review_policy)
        policy["automation"] = {
            **policy["automation"],
            "auto_approve_by_risk": {
                "low": {"enabled": True, "delay_days": 1},
                "medium": {"enabled": True, "delay_days": 1},
                "high": {"enabled": False, "delay_days": 1},
            },
        }
        workspace.review_policy = policy
        review = session.get(ReviewItem, UUID(review_item_id))
        review.risk_level = "medium"
        review.created_at = datetime.now(UTC) - timedelta(days=2)

    pending = owner.get(
        f"/api/v1/workspaces/{workspace_id}/reviews?status=pending"
    )
    assert pending.status_code == 200
    assert review_item_id in [
        item["id"] for item in pending.json()["data"]["items"]
    ]
    with page_service.session_factory.begin() as session:
        decision_count = session.scalar(
            select(func.count(ReviewDecision.id)).where(
                ReviewDecision.review_item_id == UUID(review_item_id)
            )
        )
    assert decision_count == 0


def test_admin_can_trigger_review_auto_approval_sweep(page_service):
    owner = make_client(page_service, "auto-review-admin@example.com")
    workspace_id = owner.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    review_item_id = _seed_review_item(
        page_service, workspace_id, title="Manual Sweep Target"
    )
    medium_review_item_id = _seed_review_item(
        page_service, workspace_id, title="Manual Sweep Medium Target"
    )

    with page_service.session_factory.begin() as session:
        workspace = session.get(Workspace, UUID(workspace_id))
        policy = dict(workspace.review_policy)
        policy["automation"] = {
            **policy["automation"],
            "auto_approve_by_risk": {
                "low": {"enabled": True, "delay_days": 1},
                "medium": {"enabled": False, "delay_days": 3},
                "high": {"enabled": False, "delay_days": 1},
            },
        }
        workspace.review_policy = policy
        review = session.get(ReviewItem, UUID(review_item_id))
        review.risk_level = "low"
        review.created_at = datetime.now(UTC) - timedelta(days=2)
        medium_review = session.get(ReviewItem, UUID(medium_review_item_id))
        medium_review.risk_level = "medium"
        medium_review.created_at = datetime.now(UTC) - timedelta(days=2)

    swept = owner.post(
        f"/api/v1/workspaces/{workspace_id}/reviews/sweep-auto-approvals"
    )
    assert swept.status_code == 200
    assert swept.json()["data"]["approved"] == [review_item_id]

    with page_service.session_factory.begin() as session:
        review = session.get(ReviewItem, UUID(review_item_id))
        medium_review = session.get(ReviewItem, UUID(medium_review_item_id))
        decision = session.scalar(
            select(ReviewDecision).where(
                ReviewDecision.review_item_id == UUID(review_item_id)
            )
        )
        assert review.status == "approved"
        assert decision.actor_type == "system"
        assert medium_review.status == "pending"


def test_request_changes_routes_to_me_scope(page_service):
    """T15/B4+B5: a request_changes routed to workspace_editors appears under
    scope=to_me for an editor principal and the decision carries audience_kind."""
    owner = make_client(page_service, "scope-owner@example.com")
    workspace_id = owner.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    review_item_id = _seed_review_item(
        page_service, workspace_id, title="Scope Target"
    )
    decided = owner.post(
        f"/api/v1/workspaces/{workspace_id}/reviews/{review_item_id}/decision",
        json={
            "decision_type": "request_changes",
            "decision_reason": "Please revise.",
            "audience_kind": "workspace_editors",
        },
    )
    assert decided.status_code == 200
    assert decided.json()["data"]["current_audience_kind"] == "workspace_editors"

    to_me = owner.get(
        f"/api/v1/workspaces/{workspace_id}/reviews?scope=to_me"
    )
    assert to_me.status_code == 200
    assert review_item_id in [item["id"] for item in to_me.json()["data"]["items"]]


def test_claim_and_second_claimer_conflicts(page_service):
    """T15/B6: claim succeeds for an editor; a second editor claiming the live
    claim gets 409 review_already_claimed; release frees it."""
    owner = make_client(page_service, "claim-owner@example.com")
    second = make_peer_client(owner, "claim-second@example.com")
    workspace_id = owner.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    review_item_id = _seed_review_item(
        page_service, workspace_id, title="Claim Target"
    )
    # A review must be changes_requested before it can be claimed (status guard).
    requested = owner.post(
        f"/api/v1/workspaces/{workspace_id}/reviews/{review_item_id}/decision",
        json={"decision_type": "request_changes", "decision_reason": "fix"},
    )
    assert requested.status_code == 200

    claimed = owner.post(
        f"/api/v1/workspaces/{workspace_id}/reviews/{review_item_id}/claim",
    )
    assert claimed.status_code == 200
    assert claimed.json()["data"]["claimed_by"] is not None

    set_role(page_service, "claim-second@example.com", workspace_id, "editor")

    conflict = second.post(
        f"/api/v1/workspaces/{workspace_id}/reviews/{review_item_id}/claim",
    )
    assert conflict.status_code == 409
    assert conflict.json()["error_code"] == "review_already_claimed"

    released = owner.post(
        f"/api/v1/workspaces/{workspace_id}/reviews/{review_item_id}/release",
    )
    assert released.status_code == 200
    assert released.json()["data"]["claimed_by"] is None

    retaken = second.post(
        f"/api/v1/workspaces/{workspace_id}/reviews/{review_item_id}/claim",
    )
    assert retaken.status_code == 200


def test_claim_rejects_viewer(page_service):
    """T15/B6: the claim endpoint keeps the editor gate (viewer -> 403)."""
    owner = make_client(page_service, "claimgate-owner@example.com")
    viewer = make_client(page_service, "claimgate-viewer@example.com")
    workspace_id = owner.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    review_item_id = _seed_review_item(
        page_service, workspace_id, title="Claim Gate Target"
    )
    set_role(page_service, "claimgate-viewer@example.com", workspace_id, "viewer")
    denied = viewer.post(
        f"/api/v1/workspaces/{workspace_id}/reviews/{review_item_id}/claim",
    )
    assert denied.status_code == 403
    assert denied.json()["error_code"] == "workspace_role_insufficient"


def set_role(page_service, email: str, workspace_id: str, role: str) -> None:
    with page_service.session_factory.begin() as session:
        user = session.scalar(select(User).where(User.email == email))
        workspace = session.get(Workspace, UUID(workspace_id))
        assert user is not None
        assert workspace is not None
        grant_workspace_membership(
            session,
            user_id=user.id,
            workspace_id=workspace.id,
            role=role,
        )


def _mcp_client_actor(workspace_id: UUID | None) -> ActorContext:
    return ActorContext(
        actor_kind="mcp_client",
        actor_id="fix6-mcp-client",
        scopes=("mcp:read", "mcp:draft-write"),
        workspace_role="mcp_client",
        workspace_id=workspace_id,
        role="mcp_client",
        token_kind="mcp_client",
    )


@pytest.mark.parametrize("claimed_workspace", ("target", "foreign", None))
def test_direct_mcp_client_credentials_are_rejected(
    session_factory, claimed_workspace
):
    """The retired direct-client identity cannot authorize workspace access."""
    with session_factory.begin() as session:
        target = insert_workspace(
            session,
            slug=f"retired-mcp-target-{str(claimed_workspace).lower()}",
            title="Target",
        )
        foreign = insert_workspace(
            session,
            slug=f"retired-mcp-foreign-{str(claimed_workspace).lower()}",
            title="Foreign",
        )
        target_id = target.id
        claimed_workspace_id = {
            "target": target.id,
            "foreign": foreign.id,
            None: None,
        }[claimed_workspace]

    actor = _mcp_client_actor(workspace_id=claimed_workspace_id)
    with session_factory() as session:
        with pytest.raises(RejectedByPolicyError) as excinfo:
            require_workspace_role(
                session=session,
                actor=actor,
                workspace_id=target_id,
                required_role="viewer",
                workspace_session_factory=session_factory,
            )
    assert excinfo.value.error_code == "unsupported_actor_kind"


def test_system_actor_remains_global(session_factory):
    """Fix-6 Task 1.2: system actor preserves by-design global owner role
    (internal services / cross-workspace jobs). Documented exception."""
    with session_factory.begin() as session:
        workspace = insert_workspace(
            session, slug="fix6-system-global", title="Fix-6 System Global"
        )
        target_id = workspace.id

    actor = ActorContext(
        actor_kind="system",
        actor_id="dtxwiki-system",
        scopes=(),
        workspace_role="system",
        workspace_id=None,
        role="system",
        token_kind="system",
    )
    with session_factory() as session:
        access = require_workspace_role(
            session=session,
            actor=actor,
            workspace_id=target_id,
            required_role="viewer",
            workspace_session_factory=session_factory,
        )
    assert access.workspace_id == target_id
    assert access.role == "owner"


def test_unknown_actor_kind_without_user_id_denied(session_factory):
    """Sprint_5 Phase_2 fix-round-3 Bot-B3: actors with an unknown
    ``actor_kind`` (outside ``{system, mcp_client, human}``) must fail closed.

    Pre-fix behavior fell through to the ``user_session`` branch and reported
    ``workspace_membership_missing``. Fix-round 3 raises
    ``PermissionError('unknown_actor_kind: ...')`` so the policy layer cannot
    be tricked into ambiguous fallthroughs. The wrapper converts this into a
    ``RejectedByPolicyError`` with ``error_code='unknown_actor_kind'``.
    """
    with session_factory.begin() as session:
        workspace = insert_workspace(
            session, slug="fix6-unknown", title="Fix-6 Unknown"
        )
        target_id = workspace.id

    actor = ActorContext(
        actor_kind="anonymous",
        actor_id="anon",
        scopes=(),
        workspace_role="none",
        workspace_id=None,
        role="none",
        token_kind="none",
    )
    with session_factory() as session:
        with pytest.raises(RejectedByPolicyError) as excinfo:
            require_workspace_role(
                session=session,
                actor=actor,
                workspace_id=target_id,
                required_role="viewer",
                workspace_session_factory=session_factory,
            )
    assert excinfo.value.error_code == "unknown_actor_kind"
    assert "anonymous" in str(excinfo.value)


def _failing_workspace_session_factory():
    raise OperationalError(
        "SELECT 1", {}, Exception("could not connect to workspace database")
    )


def test_workspace_plane_failure_raises_typed_error_not_a_policy_denial(
    session_factory,
):
    """T0 (D-01v3-e): a target workspace's OWN database can fail to open or
    query while the identity/control plane check above it succeeded. Before
    this fix, that SQLAlchemyError propagated raw and every one of this
    function's six callers -- none of which catch a bare SQLAlchemyError --
    would surface it as an unclassified 500, indistinguishable from a real
    application bug. It must instead be a distinct, typed error the caller
    can map to "retry me" (503 / workspace_plane_unavailable), never folded
    into RejectedByPolicyError's 403 family, which promises a considered,
    stable access decision."""

    with session_factory.begin() as session:
        workspace = insert_workspace(
            session, slug="fix-t0-plane-unavailable", title="T0 Plane Unavailable"
        )
        target_id = workspace.id

    actor = ActorContext(
        actor_kind="system",
        actor_id="dtxwiki-system",
        scopes=(),
        workspace_role="system",
        workspace_id=None,
        role="system",
        token_kind="system",
    )
    with session_factory() as session:
        with pytest.raises(WorkspacePlaneUnavailableError) as excinfo:
            require_workspace_role(
                session=session,
                actor=actor,
                workspace_id=target_id,
                required_role="viewer",
                workspace_session_factory=_failing_workspace_session_factory,
            )
    assert excinfo.value.error_code == "workspace_plane_unavailable"
    assert excinfo.value.workspace_id == target_id
    assert not isinstance(excinfo.value, RejectedByPolicyError)


def test_workspace_plane_schema_mismatch_is_not_relabeled_as_unavailable(
    session_factory,
):
    """The SAME OperationalError family also carries a genuinely different
    diagnostic (backend.app.db_errors.is_database_schema_mismatch, consumed
    by backend.app.main.unhandled_exception_handler) that this new guard
    must not shadow -- a stale dev DB missing a column is not "temporarily
    unreachable, retry later", and the top-level handler's friendlier
    database_schema_mismatch response must still fire."""

    with session_factory.begin() as session:
        workspace = insert_workspace(
            session, slug="fix-t0-schema-mismatch", title="T0 Schema Mismatch"
        )
        target_id = workspace.id

    actor = ActorContext(
        actor_kind="system",
        actor_id="dtxwiki-system",
        scopes=(),
        workspace_role="system",
        workspace_id=None,
        role="system",
        token_kind="system",
    )

    def _schema_mismatch_factory():
        raise OperationalError(
            "SELECT foo FROM mcp_client", {}, Exception("no such column: foo")
        )

    with session_factory() as session:
        with pytest.raises(OperationalError) as excinfo:
            require_workspace_role(
                session=session,
                actor=actor,
                workspace_id=target_id,
                required_role="viewer",
                workspace_session_factory=_schema_mismatch_factory,
            )
    assert not isinstance(excinfo.value, WorkspacePlaneUnavailableError)


def test_rest_workspace_access_dependency_maps_plane_failure_to_503(
    session_factory,
):
    """T0: backend.api.deps.workspace_access(...)'s dependency only caught
    RejectedByPolicyError (-> 403). A workspace-plane connectivity failure
    inside require_workspace_role would have propagated uncaught past this
    seam to the ASGI-level unhandled_exception_handler, landing on the same
    generic 500 as a real application bug -- not the retryable 503 a client
    can act on. Calls the dependency closure directly (it takes plain
    values, not Request) rather than standing up a full TestClient."""

    from backend.api.deps import workspace_access
    from backend.app.container import DatabaseProvider

    with session_factory.begin() as session:
        workspace = insert_workspace(
            session, slug="fix-t0-rest-plane-unavailable", title="T0 REST Plane"
        )
        target_id = workspace.id

    actor = ActorContext(
        actor_kind="system",
        actor_id="dtxwiki-system",
        scopes=(),
        workspace_role="system",
        workspace_id=None,
        role="system",
        token_kind="system",
    )

    class _FailingProvider(DatabaseProvider):
        def session_for(self, workspace_id):
            return _failing_workspace_session_factory

    check_workspace_access = workspace_access("viewer")
    with pytest.raises(HTTPException) as excinfo:
        check_workspace_access(
            workspace_id=target_id,
            actor=actor,
            session_factory=session_factory,
            db=_FailingProvider(None, session_factory=session_factory),
        )
    assert excinfo.value.status_code == 503
    assert excinfo.value.detail["error_code"] == "workspace_plane_unavailable"

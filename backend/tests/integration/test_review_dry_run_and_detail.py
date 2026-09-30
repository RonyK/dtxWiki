"""L8: backend integration coverage for the bulk dry-run preview branch and the
GET /reviews/{id} endpoint, which were previously only exercised via frontend
mocks. Also pins the L7 behaviour: a dry-run preview must not mutate state.
"""

from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.user import User
from backend.policy.rbac import grant_workspace_membership
from backend.schemas.context_write import WriteDraftRequest
from backend.services.context_write_service import ContextWriteService
from backend.tests.conftest import authenticate_test_client


def _make_client(page_service) -> TestClient:
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client(client)
    return client


def _current_workspace_id(client: TestClient) -> str:
    return client.get("/api/v1/workspaces").json()["data"]["current_workspace_id"]


def _seed_pending_review(page_service, workspace_id: str) -> str:
    with page_service.session_factory.begin() as session:
        author = User(
            email=f"author-{uuid4().hex}@example.com",
            password_hash="x",
            display_name="Author",
        )
        session.add(author)
        session.flush()
        grant_workspace_membership(
            session,
            user_id=author.id,
            workspace_id=UUID(workspace_id),
            role="editor",
        )
        draft = ContextWriteService().write_draft(
            session=session,
            workspace_id=UUID(workspace_id),
            request=WriteDraftRequest(
                title="Dry-run target",
                page_type="overview",
                body_markdown="# Dry-run target\n\nBody.",
                reason="seed dry-run review",
            ),
            actor_kind="human",
            actor_id=str(author.id),
            actor_role="owner",
        )
        return str(draft.review_item_id)


def test_get_review_returns_detail(page_service):
    client = _make_client(page_service)
    workspace_id = _current_workspace_id(client)
    review_id = _seed_pending_review(page_service, workspace_id)

    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/reviews/{review_id}"
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["id"] == review_id
    assert data["status"] == "pending"
    assert data["can_decide"] is True
    assert data["decision_summary"] == "context draft requires review"
    assert data["decision_summary_code"] == "context_draft_requires_review"


def test_get_review_unknown_id_returns_404(page_service):
    client = _make_client(page_service)
    workspace_id = _current_workspace_id(client)

    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/reviews/{uuid4()}"
    )

    assert response.status_code == 404


def test_dry_run_preview_marks_eligible_without_mutating(page_service):
    client = _make_client(page_service)
    workspace_id = _current_workspace_id(client)
    review_id = _seed_pending_review(page_service, workspace_id)

    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/reviews/decide-bulk",
        json={
            "review_item_ids": [review_id],
            "decision_type": "approve",
            "dry_run": True,
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert review_id in data["approved"]
    assert data["skipped"] == []

    # The preview must be side-effect free: the review is still pending and
    # decidable afterwards (L7 evaluates it in a read-only session).
    after = client.get(
        f"/api/v1/workspaces/{workspace_id}/reviews/{review_id}"
    )
    assert after.json()["data"]["status"] == "pending"
    assert after.json()["data"]["can_decide"] is True


def test_dry_run_preview_skips_already_resolved(page_service):
    client = _make_client(page_service)
    workspace_id = _current_workspace_id(client)
    review_id = _seed_pending_review(page_service, workspace_id)

    approved = client.post(
        f"/api/v1/workspaces/{workspace_id}/reviews/decide-bulk",
        json={
            "review_item_ids": [review_id],
            "decision_type": "approve",
            "dry_run": False,
        },
    )
    assert approved.status_code == 200
    assert review_id in approved.json()["data"]["approved"]

    # A resolved item is no longer decidable, so a subsequent preview skips it.
    preview = client.post(
        f"/api/v1/workspaces/{workspace_id}/reviews/decide-bulk",
        json={
            "review_item_ids": [review_id],
            "decision_type": "approve",
            "dry_run": True,
        },
    )
    assert preview.status_code == 200
    data = preview.json()["data"]
    assert data["approved"] == []
    assert any(item["id"] == review_id for item in data["skipped"])


def test_counts_payload_includes_status_by_risk(page_service):
    client = _make_client(page_service)
    workspace_id = _current_workspace_id(client)
    _seed_pending_review(page_service, workspace_id)

    response = client.get(f"/api/v1/workspaces/{workspace_id}/reviews/counts")

    assert response.status_code == 200
    data = response.json()["data"]
    # RO-1: additive status x risk matrix for the three lifecycle buckets.
    assert set(data["status_by_risk"].keys()) == {
        "pending",
        "changes_requested",
        "resolved",
    }
    pending_breakdown = data["status_by_risk"]["pending"]
    assert set(pending_breakdown.keys()) == {"high", "medium", "low"}
    # the per-risk pending counts sum to the pending total
    assert sum(pending_breakdown.values()) == data["pending"]
    assert data["pending"] >= 1


def test_recent_decisions_returns_real_decision(page_service):
    client = _make_client(page_service)
    workspace_id = _current_workspace_id(client)
    review_id = _seed_pending_review(page_service, workspace_id)

    approve = client.post(
        f"/api/v1/workspaces/{workspace_id}/reviews/decide-bulk",
        json={
            "review_item_ids": [review_id],
            "decision_type": "approve",
            "dry_run": False,
        },
    )
    assert approve.status_code == 200

    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/reviews/decisions/recent?limit=5"
    )

    assert response.status_code == 200
    decisions = response.json()["data"]
    # RD-1: a real decision row (not the resolved review item).
    assert len(decisions) >= 1
    top = decisions[0]
    assert top["review_item_id"] == review_id
    assert top["decision_type"] == "approve"
    assert top["resulting_review_status"] is not None
    assert top["target_title"] == "Dry-run target"
    assert top["review_target_title"] == top["target_title"]
    assert top["target_slug"] == "dry-run-target"
    assert top["target_version_no"] == 1
    assert top["decision_summary"] == "context draft requires review"
    assert top["decision_summary_code"] == "context_draft_requires_review"

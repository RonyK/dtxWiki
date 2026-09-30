from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.audit import AuditEvent
from backend.models.change import ChangeSet
from backend.models.context import ContextPage, ContextPageVersion, PageLink
from backend.models.lint import LintFinding
from backend.models.review import ReviewItem
from backend.models.workspace import Workspace
from backend.services import context_ingress_service as context_ingress_module
from backend.services.review_risk_policy import default_review_policy
from backend.tests.conftest import authenticate_test_client_in_workspace


def make_client(page_service) -> TestClient:
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client_in_workspace(
        client,
        page_service.session_factory,
        workspace_slug="default",
    )
    return client


def test_tree_mutation_routes_record_visible_state(page_service):
    client = make_client(page_service)
    workspace_id = current_workspace_id(client)
    ask_root = tree_node_by_slug(client, workspace_id, "ask")

    session_response = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/sessions",
        json={"title": "Movable Ask"},
    )
    assert session_response.status_code == 201
    ask_session_id = session_response.json()["data"]["id"]
    session_node = tree_node_by_target(
        client,
        workspace_id,
        "ask_session",
        ask_session_id,
    )

    renamed = client.patch(
        f"/api/v1/workspaces/{workspace_id}/tree/nodes/{session_node['id']}",
        json={"display_name": "Renamed Ask"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["data"]["title"] == "Renamed Ask"

    moved = client.post(
        f"/api/v1/workspaces/{workspace_id}/tree/move",
        json={
            "node_id": session_node["id"],
            "new_parent_id": ask_root["id"],
        },
    )
    assert moved.status_code == 200
    assert moved.json()["data"]["parent_id"] == ask_root["id"]
    with page_service.session_factory() as session:
        latest_change = session.scalar(
            select(ChangeSet).order_by(ChangeSet.created_at.desc())
        )
        assert latest_change is not None
        assert latest_change.reason == "Tree node moved"

    root_move = client.post(
        f"/api/v1/workspaces/{workspace_id}/tree/move",
        json={
            "node_id": ask_root["id"],
            "new_parent_id": ask_root["id"],
            "reason": "invalid root move",
        },
    )
    assert root_move.status_code == 403


def test_ask_turn_promote_and_accept_creates_context_draft(
    page_service,
    monkeypatch,
):
    locked_proposals = []
    original_lock = context_ingress_module.require_proposal_for_update

    def track_proposal_lock(session, workspace_id, proposal_id):
        locked_proposals.append(proposal_id)
        return original_lock(session, workspace_id, proposal_id)

    monkeypatch.setattr(
        context_ingress_module,
        "require_proposal_for_update",
        track_proposal_lock,
    )
    client = make_client(page_service)
    workspace_id = current_workspace_id(client)
    ask_captures = tree_node_by_slug(client, workspace_id, "ask_captures")
    client.post(
        f"/api/v1/workspaces/{workspace_id}/context/pages",
        json={
            "title": "Phase 9 Evidence",
            "body_md": "# Phase 9 Evidence\n\nNeedle evidence body.",
        },
    )
    session_response = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/sessions",
        json={
            "title": "Ingress Ask",
            "default_context_parent_tree_node_id": ask_captures["id"],
        },
    )
    assert session_response.status_code == 201
    ask_session_id = session_response.json()["data"]["id"]
    turn_response = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/sessions/{ask_session_id}/turns",
        json={"user_message": "Needle"},
    )
    assert turn_response.status_code == 200
    turn_id = turn_response.json()["data"]["id"]

    promoted = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/turns/{turn_id}/promote",
        json={
            "save_mode": "create_page",
            "parent_node_id": ask_captures["id"],
            "page_type": "guide",
            "title": "Saved Phase 9 Answer",
            "body_markdown": "# Saved Phase 9 Answer\n\nSee [[phase9-evidence]].",
        },
    )
    assert promoted.status_code == 201
    proposal = promoted.json()["data"]
    assert proposal["status"] == "proposed"
    assert proposal["proposed_parent_tree_node_id"] == ask_captures["id"]

    accepted = client.post(
        f"/api/v1/workspaces/{workspace_id}/context/ingress-proposals/{proposal['id']}/accept",
        json={
            "final_save_mode": "create_page",
            "final_parent_node_id": ask_captures["id"],
            "final_page_type": "guide",
            "final_title": "Saved Phase 9 Answer",
            "body_markdown": "# Saved Phase 9 Answer\n\nSee [[phase9-evidence]].",
        },
    )
    assert accepted.status_code == 200
    result = accepted.json()["data"]
    assert UUID(proposal["id"]) in locked_proposals
    assert result["context_page_id"]
    assert result["context_page_version_id"]
    assert result["tree_node_id"]

    pages = client.get(
        f"/api/v1/workspaces/{workspace_id}/context/pages",
    )
    assert any(page["title"] == "Saved Phase 9 Answer" for page in pages.json()["data"])
    with page_service.session_factory() as session:
        link = session.scalar(
            select(PageLink).where(
                PageLink.source_page_id == UUID(result["context_page_id"])
            )
        )
    assert link is not None
    assert link.source_version_id == UUID(result["context_page_version_id"])
    assert link.target_slug == "phase9-evidence"


def test_context_ingress_accept_rejects_unsafe_media_before_any_write(page_service):
    client = make_client(page_service)
    workspace_id = current_workspace_id(client)
    ask_captures = tree_node_by_slug(client, workspace_id, "ask_captures")
    ask_session = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/sessions",
        json={"title": "Unsafe media ingress"},
    ).json()["data"]
    turn = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/sessions/{ask_session['id']}/turns",
        json={"user_message": "Draft a page"},
    ).json()["data"]
    proposal = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/turns/{turn['id']}/promote",
        json={
            "save_mode": "create_page",
            "parent_node_id": ask_captures["id"],
            "page_type": "guide",
            "title": "Unsafe media proposal",
        },
    ).json()["data"]

    with page_service.session_factory() as session:
        before = tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (ContextPage, ContextPageVersion, ChangeSet)
        )

    accepted = client.post(
        f"/api/v1/workspaces/{workspace_id}/context/ingress-proposals/{proposal['id']}/accept",
        json={
            "final_save_mode": "create_page",
            "final_parent_node_id": ask_captures["id"],
            "final_page_type": "guide",
            "final_title": "Unsafe media proposal",
            "body_markdown": "![embedded](data:image/png;base64,AA==)",
        },
    )

    assert accepted.status_code == 422
    assert accepted.json()["error_code"] == "media_markdown_policy_invalid"
    with page_service.session_factory() as session:
        after = tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (ContextPage, ContextPageVersion, ChangeSet)
        )
    assert after == before
    detail = client.get(
        f"/api/v1/workspaces/{workspace_id}/context/ingress-proposals/{proposal['id']}"
    )
    assert detail.status_code == 200
    assert detail.json()["data"]["status"] == "proposed"


def test_accept_denied_when_source_ask_turn_deleted(page_service):
    # H-01 (Codex round-2): the source_ask_turn_id FK is nulled when the turn is
    # deleted, leaving the cloned answer in `alternatives` unverifiable. Accepting
    # such a proposal must fail closed so a direct-ID accept cannot launder the
    # stored answer into a page, and the read must drop the cloned body.
    from backend.models.ask import AskTurn

    client = make_client(page_service)
    workspace_id = current_workspace_id(client)
    ask_captures = tree_node_by_slug(client, workspace_id, "ask_captures")
    client.post(
        f"/api/v1/workspaces/{workspace_id}/context/pages",
        json={
            "title": "H01 Evidence",
            "body_md": "# H01 Evidence\n\nNeedle evidence body.",
        },
    )
    session_id = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/sessions",
        json={
            "title": "H01 Ask",
            "default_context_parent_tree_node_id": ask_captures["id"],
        },
    ).json()["data"]["id"]
    turn_id = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/sessions/{session_id}/turns",
        json={"user_message": "Needle"},
    ).json()["data"]["id"]
    proposal_id = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/turns/{turn_id}/promote",
        json={
            "save_mode": "create_page",
            "parent_node_id": ask_captures["id"],
            "page_type": "guide",
            "title": "H01 Saved Answer",
        },
    ).json()["data"]["id"]

    # Delete the source turn: the FK sets source_ask_turn_id = NULL.
    with page_service.session_factory.begin() as session:
        turn = session.get(AskTurn, UUID(turn_id))
        session.delete(turn)

    # The cloned answer is dropped from the proposal read.
    detail = client.get(
        f"/api/v1/workspaces/{workspace_id}/context/ingress-proposals/{proposal_id}"
    )
    assert detail.status_code == 200
    assert not any(
        isinstance(alt, dict) and alt.get("source") == "ask_turn"
        for alt in detail.json()["data"]["alternatives"]
    )
    # Accept fails closed (no body_markdown => would materialize the stored clone).
    accepted = client.post(
        f"/api/v1/workspaces/{workspace_id}/context/ingress-proposals/{proposal_id}/accept",
        json={
            "final_save_mode": "create_page",
            "final_parent_node_id": ask_captures["id"],
            "final_page_type": "guide",
            "final_title": "H01 Saved Answer",
        },
    )
    assert accepted.status_code == 404


def test_context_ingress_reject_locks_proposal(page_service, monkeypatch):
    locked_proposals = []
    original_lock = context_ingress_module.require_proposal_for_update

    def track_proposal_lock(session, workspace_id, proposal_id):
        locked_proposals.append(proposal_id)
        return original_lock(session, workspace_id, proposal_id)

    monkeypatch.setattr(
        context_ingress_module,
        "require_proposal_for_update",
        track_proposal_lock,
    )
    client = make_client(page_service)
    workspace_id = current_workspace_id(client)
    ask_captures = tree_node_by_slug(client, workspace_id, "ask_captures")
    ask_session = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/sessions",
        json={"title": "Reject ingress"},
    ).json()["data"]
    turn = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/sessions/{ask_session['id']}/turns",
        json={"user_message": "Reject this draft"},
    ).json()["data"]
    proposal = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/turns/{turn['id']}/promote",
        json={
            "save_mode": "create_page",
            "parent_node_id": ask_captures["id"],
            "page_type": "guide",
            "title": "Rejected Page",
        },
    ).json()["data"]

    rejected = client.post(
        f"/api/v1/workspaces/{workspace_id}/context/ingress-proposals/{proposal['id']}/reject",
        json={"reason": "not suitable"},
    )

    assert rejected.status_code == 200
    assert rejected.json()["data"]["status"] == "rejected"
    assert UUID(proposal["id"]) in locked_proposals


def test_context_ingress_update_locks_target_before_reading_head(
    page_service,
    monkeypatch,
):
    locked_pages = []
    original_lock = context_ingress_module.require_context_page_for_update

    def track_page_lock(session, workspace_id, page_id):
        locked_pages.append(page_id)
        return original_lock(session, workspace_id, page_id)

    monkeypatch.setattr(
        context_ingress_module,
        "require_context_page_for_update",
        track_page_lock,
    )
    client = make_client(page_service)
    workspace_id = current_workspace_id(client)
    ask_captures = tree_node_by_slug(client, workspace_id, "ask_captures")
    target = client.post(
        f"/api/v1/workspaces/{workspace_id}/context/pages",
        json={
            "title": "Ingress Target",
            "body_md": "# Ingress Target\n\nOriginal.",
        },
    ).json()["data"]
    ask_session = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/sessions",
        json={"title": "Update ingress"},
    ).json()["data"]
    turn = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/sessions/{ask_session['id']}/turns",
        json={"user_message": "Update the target"},
    ).json()["data"]
    proposal = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/turns/{turn['id']}/promote",
        json={
            "save_mode": "update_draft",
            "parent_node_id": ask_captures["id"],
            "page_type": "guide",
            "title": "Ingress Target",
            "body_markdown": "# Ingress Target\n\nUpdated.",
        },
    ).json()["data"]

    accepted = client.post(
        f"/api/v1/workspaces/{workspace_id}/context/ingress-proposals/{proposal['id']}/accept",
        json={
            "final_save_mode": "update_draft",
            "final_parent_node_id": ask_captures["id"],
            "final_page_type": "guide",
            "final_title": "Ingress Target",
            "target_page_id": target["id"],
            "body_markdown": "# Ingress Target\n\nUpdated.",
        },
    )

    assert accepted.status_code == 200
    assert UUID(target["id"]) in locked_pages


def test_context_ingress_slug_race_retries_with_next_suffix(
    page_service,
    monkeypatch,
):
    original_slug = context_ingress_module.unique_context_slug
    calls = 0

    def stale_first_slug(session, workspace_id, title):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "collision"
        return original_slug(session, workspace_id, title)

    monkeypatch.setattr(
        context_ingress_module,
        "unique_context_slug",
        stale_first_slug,
    )
    with page_service.session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        parent = session.scalar(
            select(context_ingress_module.WorkspaceTreeNode).where(
                context_ingress_module.WorkspaceTreeNode.workspace_id == workspace.id,
                context_ingress_module.WorkspaceTreeNode.slug == "ask_captures",
            )
        )
        session.add(
            context_ingress_module.ContextPage(
                workspace_id=workspace.id,
                slug="collision",
                title="Existing collision",
                page_type="guide",
            )
        )
        session.flush()

        created = context_ingress_module.insert_ingress_page_with_unique_slug(
            session=session,
            workspace_id=workspace.id,
            title="Collision",
            page_type="guide",
            parent_id=parent.id,
            placement_reason=None,
            placement_confidence="medium",
            actor_kind="human",
            actor_id="dev-local-user",
        )

    assert calls >= 2
    assert created.slug == "collision-2"


def test_ingress_accept_audit_uses_policy_resolved_review_required(page_service):
    client = make_client(page_service)
    workspace_id = current_workspace_id(client)
    ask_captures = tree_node_by_slug(client, workspace_id, "ask_captures")
    policy = default_review_policy()
    policy["defaults"]["context_ingress_auto"] = "high"
    with page_service.session_factory.begin() as session:
        workspace = session.get(Workspace, UUID(workspace_id))
        assert workspace is not None
        workspace.review_policy = policy

    session_response = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/sessions",
        json={"title": "Policy Gated Ingress"},
    )
    assert session_response.status_code == 201
    turn_response = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/sessions/{session_response.json()['data']['id']}/turns",
        json={"user_message": "Draft a policy gated page"},
    )
    assert turn_response.status_code == 200
    promoted = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/turns/{turn_response.json()['data']['id']}/promote",
        json={
            "save_mode": "create_page",
            "parent_node_id": ask_captures["id"],
            "page_type": "guide",
            "title": "Policy Gated Page",
        },
    )
    assert promoted.status_code == 201

    accepted = client.post(
        f"/api/v1/workspaces/{workspace_id}/context/ingress-proposals/{promoted.json()['data']['id']}/accept",
        json={
            "final_save_mode": "create_page",
            "final_parent_node_id": ask_captures["id"],
            "final_page_type": "guide",
            "final_title": "Policy Gated Page",
        },
    )
    assert accepted.status_code == 200
    assert accepted.json()["data"]["review_item_id"] is None

    with page_service.session_factory() as session:
        audit = (
            session.query(AuditEvent)
            .filter(AuditEvent.action == "context_ingress.proposal.accept")
            .order_by(AuditEvent.created_at.desc())
            .first()
        )
        assert audit is not None
        assert audit.payload["review_required"] is False


def test_human_ingress_explicit_review_request_creates_pending_review(page_service):
    client = make_client(page_service)
    workspace_id = current_workspace_id(client)
    ask_captures = tree_node_by_slug(client, workspace_id, "ask_captures")

    session_response = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/sessions",
        json={"title": "Explicit Review Ingress"},
    )
    assert session_response.status_code == 201
    turn_response = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/sessions/"
        f"{session_response.json()['data']['id']}/turns",
        json={"user_message": "Draft a page for another person to review"},
    )
    assert turn_response.status_code == 200
    promoted = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/turns/"
        f"{turn_response.json()['data']['id']}/promote",
        json={
            "save_mode": "create_page",
            "parent_node_id": ask_captures["id"],
            "page_type": "guide",
            "title": "Explicit Review Page",
        },
    )
    assert promoted.status_code == 201

    accepted = client.post(
        f"/api/v1/workspaces/{workspace_id}/context/ingress-proposals/"
        f"{promoted.json()['data']['id']}/accept",
        json={
            "final_save_mode": "create_page",
            "final_parent_node_id": ask_captures["id"],
            "final_page_type": "guide",
            "final_title": "Explicit Review Page",
            "review_required": True,
        },
    )

    assert accepted.status_code == 200
    review_id = accepted.json()["data"]["review_item_id"]
    assert review_id is not None
    with page_service.session_factory() as session:
        review = session.get(ReviewItem, UUID(review_id))
        assert review is not None
        assert review.status == "pending"
        assert review.risk_signals["caller_needs_review"] is True


def test_ask_turn_feedback_only_accept_creates_feedback_without_context_page(
    page_service,
):
    client = make_client(page_service)
    workspace_id = current_workspace_id(client)
    ask_captures = tree_node_by_slug(client, workspace_id, "ask_captures")
    session_response = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/sessions",
        json={
            "title": "Feedback Only Ask",
            "default_context_parent_tree_node_id": ask_captures["id"],
        },
    )
    assert session_response.status_code == 201
    ask_session_id = session_response.json()["data"]["id"]
    turn_response = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/sessions/{ask_session_id}/turns",
        json={"user_message": "Feedback-only note"},
    )
    assert turn_response.status_code == 200
    turn_id = turn_response.json()["data"]["id"]
    before_pages = client.get(
        f"/api/v1/workspaces/{workspace_id}/context/pages",
    ).json()["data"]

    promoted = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/turns/{turn_id}/promote",
        json={
            "save_mode": "feedback_only",
            "parent_node_id": ask_captures["id"],
            "page_type": "guide",
            "title": "Feedback Only Note",
        },
    )
    assert promoted.status_code == 201
    proposal = promoted.json()["data"]
    accepted = client.post(
        f"/api/v1/workspaces/{workspace_id}/context/ingress-proposals/{proposal['id']}/accept",
        json={
            "final_save_mode": "feedback_only",
            "final_parent_node_id": ask_captures["id"],
            "final_page_type": "guide",
            "final_title": "Feedback Only Note",
        },
    )

    assert accepted.status_code == 200
    result = accepted.json()["data"]
    assert result["context_page_id"] is None
    assert result["context_page_version_id"] is None
    assert result["tree_node_id"] is None
    assert result["feedback_item_id"]
    after_pages = client.get(
        f"/api/v1/workspaces/{workspace_id}/context/pages",
    ).json()["data"]
    feedback = client.get(
        f"/api/v1/workspaces/{workspace_id}/feedback?target_type=ask_turn",
    )

    assert [page["id"] for page in after_pages] == [page["id"] for page in before_pages]
    assert feedback.status_code == 200
    assert any(
        item["id"] == result["feedback_item_id"] for item in feedback.json()["data"]
    )


def test_ask_session_rejects_non_context_default_parent(page_service):
    client = make_client(page_service)
    workspace_id = current_workspace_id(client)
    ask_root = tree_node_by_slug(client, workspace_id, "ask")

    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/ask/sessions",
        json={
            "title": "Invalid Parent Ask",
            "default_context_parent_tree_node_id": ask_root["id"],
        },
    )

    assert response.status_code == 403
    assert response.json()["error_code"] == "rejected_by_policy"


def test_lint_routes_list_create_and_decide_findings(page_service):
    client = make_client(page_service)
    workspace_id = current_workspace_id(client)
    lint_run = client.post(
        f"/api/v1/workspaces/{workspace_id}/lint/runs",
        json={"lint_scope": "workspace", "trigger_type": "manual"},
    )
    assert lint_run.status_code == 202
    lint_run_id = lint_run.json()["data"]["id"]
    with page_service.session_factory.begin() as session:
        session.add(
            LintFinding(
                lint_run_id=UUID(lint_run_id),
                workspace_id=UUID(workspace_id),
                target_type="workspace",
                target_id=None,
                rule_id="missing_citation",
                severity="warning",
                status="open",
                suggested_action="Add citation",
            )
        )

    findings = client.get(
        f"/api/v1/workspaces/{workspace_id}/lint/findings?status=open",
    )
    assert findings.status_code == 200
    finding = findings.json()["data"]["findings"][0]
    assert finding["rule_id"] == "missing_citation"

    decided = client.post(
        f"/api/v1/workspaces/{workspace_id}/lint/findings/{finding['id']}/decision",
        json={"decision": "acknowledge", "reason": "triaged"},
    )
    assert decided.status_code == 200
    assert decided.json()["data"]["status"] == "acknowledged"


def current_workspace_id(client: TestClient) -> str:
    return client.get("/api/v1/workspaces").json()["data"]["current_workspace_id"]


def tree_node_by_slug(client: TestClient, workspace_id: str, slug: str) -> dict:
    tree = client.get(f"/api/v1/workspaces/{workspace_id}/tree")
    assert tree.status_code == 200
    return next(node for node in tree.json()["data"]["nodes"] if node["slug"] == slug)


def tree_node_by_target(
    client: TestClient,
    workspace_id: str,
    target_kind: str,
    target_id: str,
) -> dict:
    tree = client.get(f"/api/v1/workspaces/{workspace_id}/tree")
    assert tree.status_code == 200
    return next(
        node
        for node in tree.json()["data"]["nodes"]
        if node["target_kind"] == target_kind and node["target_id"] == target_id
    )

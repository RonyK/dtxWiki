"""L8: backend integration coverage for the bulk dry-run preview branch and the
GET /reviews/{id} endpoint, which were previously only exercised via frontend
mocks. Also pins the L7 behaviour: a dry-run preview must not mutate state.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.review import ReviewDecision, ReviewItem
from backend.models.tree import WorkspaceTreeNode
from backend.models.user import User
from backend.policy.rbac import grant_workspace_membership
from backend.schemas.context_write import UpdateDraftRequest, WriteDraftRequest
from backend.services import review_selection_service
from backend.services.context_write_service import ContextWriteService
from backend.services.read_errors import ReadValidationError
from backend.services.review_service import ReviewService
from backend.services.tree_projection import TreeProjectionService
from backend.tests.conftest import authenticate_test_client


def _make_client(page_service, *, email="test@example.com") -> TestClient:
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


def _no_section_load(*_args, **_kwargs):
    raise AssertionError("review evidence loaded a whole tree section")


def _current_workspace_id(client: TestClient) -> str:
    return client.get("/api/v1/workspaces").json()["data"]["current_workspace_id"]


def _seed_pending_review(
    page_service, workspace_id: str, *, title="Dry-run target"
) -> str:
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
                title=title,
                page_type="overview",
                body_markdown="# Dry-run target\n\nBody.",
                reason="seed dry-run review",
            ),
            actor_kind="human",
            actor_id=str(author.id),
            actor_role="owner",
        )
        return str(draft.review_item_id)


def _seed_same_candidate_reviews(page_service, workspace_id, review_id, count):
    """Independent change sets reviewing the same exact Context candidate."""
    with page_service.session_factory.begin() as session:
        source = session.get(ReviewItem, UUID(review_id))
        source_set = session.get(ChangeSet, source.change_set_id)
        operation = session.scalar(
            select(ChangeOperation).where(
                ChangeOperation.change_set_id == source.change_set_id
            )
        )
        ids = []
        for _ in range(count):
            change_set = ChangeSet(
                workspace_id=UUID(workspace_id),
                reason="same-candidate review",
                trigger_type=source_set.trigger_type,
                actor_type=source_set.actor_type,
                actor_id=source_set.actor_id,
            )
            session.add(change_set)
            session.flush()
            session.add(ChangeOperation(
                workspace_id=UUID(workspace_id),
                change_set_id=change_set.id,
                target_type=operation.target_type,
                target_id=operation.target_id,
                operation=operation.operation,
                diff_ref=operation.diff_ref,
            ))
            sibling = ReviewItem(
                workspace_id=UUID(workspace_id),
                change_set_id=change_set.id,
                review_type="context_update",
                status="pending",
                risk_level="low",
            )
            session.add(sibling)
            session.flush()
            ids.append(str(sibling.id))
        return str(operation.target_id), ids


def _selection(client, workspace_id, **selector):
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/reviews/selection", json=selector
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _execute_selection(client, workspace_id, token, *, batch_limit=500):
    return client.post(
        f"/api/v1/workspaces/{workspace_id}/reviews/decide-bulk",
        json={
            "selection_token": token,
            "decision_type": "approve",
            "batch_limit": batch_limit,
        },
    )


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
    operation = data["evidence"]["operations"][0]
    document = operation["document"]
    content = operation["content"]
    assert document["document_id"] == operation["target_id"]
    assert document["reader_href"] == (
        f"/workspaces/{workspace_id}/context/pages/{document['document_id']}"
    )
    assert document["can_open"] is True
    assert content["candidate_version_no"] == 1
    assert content["baseline_kind"] == "empty"
    assert content["can_read"] is content["can_diff"] is True
    assert content["is_current_candidate"] is True
    assert data["actions"]["approve"]["allowed"] is True
    assert data["actions"]["edit_directly"]["allowed"] is False
    assert data["reject_effect"]["effect"] == "hide_unpublished"

    # The projection identifies an actual readable version, independently of
    # the current-document destination above.
    version = client.get(
        f"/api/v1/workspaces/{workspace_id}/context/pages/"
        f"{document['document_id']}",
        params={"version_id": content["candidate_version_id"]},
    )
    assert version.status_code == 200, version.text


def test_get_review_unknown_id_returns_404(page_service):
    client = _make_client(page_service)
    workspace_id = _current_workspace_id(client)

    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/reviews/{uuid4()}"
    )

    assert response.status_code == 404


@pytest.mark.parametrize(
    "association", ["valid", "different_page", "hidden", "foreign", "ambiguous"]
)
def test_ingress_placement_evidence_requires_visible_same_change_exact_candidate(
    page_service, association, monkeypatch,
):
    client = _make_client(page_service)
    workspace_id = _current_workspace_id(client)
    review_id = _seed_pending_review(page_service, workspace_id)
    unrelated_id = _seed_pending_review(
        page_service, workspace_id, title="Unrelated placement"
    )
    with page_service.session_factory.begin() as session:
        review = session.get(ReviewItem, UUID(review_id))
        operation = session.scalar(select(ChangeOperation).where(
            ChangeOperation.change_set_id == review.change_set_id
        ))
        operation.operation = "context_ingress_accept"
        node = session.scalar(select(WorkspaceTreeNode).where(
            WorkspaceTreeNode.workspace_id == UUID(workspace_id),
            WorkspaceTreeNode.target_kind == "context_page",
            WorkspaceTreeNode.target_id == operation.target_id,
        ))
        assert node is not None
        if association == "different_page":
            unrelated = session.get(ReviewItem, UUID(unrelated_id))
            other_operation = session.scalar(select(ChangeOperation).where(
                ChangeOperation.change_set_id == unrelated.change_set_id
            ))
            node = session.scalar(select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.target_id == other_operation.target_id
            ))
        elif association == "hidden":
            session.get(ContextPage, node.target_id).lifecycle_status = "archived"
        elif association == "ambiguous":
            session.add(ChangeOperation(
                workspace_id=UUID(workspace_id),
                change_set_id=review.change_set_id,
                target_type="context_page", target_id=operation.target_id,
                operation="context_ingress_accept", diff_ref=operation.diff_ref,
            ))
        placement_target = node.id
        if association == "foreign":
            foreign = session.scalar(select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id != UUID(workspace_id)
            ).limit(1))
            assert foreign is not None
            placement_target = foreign.id
        session.add(ChangeOperation(
            workspace_id=UUID(workspace_id),
            change_set_id=review.change_set_id,
            target_type="workspace_tree_node",
            target_id=placement_target,
            operation="tree_place",
            diff_summary="placed under Inbox",
        ))
    with monkeypatch.context() as patch:
        # A placement is decided by its own ancestor chain, never a section load.
        patch.setattr(TreeProjectionService, "load", _no_section_load)
        response = client.get(f"/api/v1/workspaces/{workspace_id}/reviews/{review_id}")
    assert response.status_code == 200, response.text
    evidence = response.json()["data"]["evidence"]
    placement = next(operation for operation in evidence["operations"]
                     if operation["target_type"] == "workspace_tree_node")
    if association == "valid":
        candidate = next(operation for operation in evidence["operations"]
                         if operation["target_type"] == "context_page")
        assert placement["document"] == candidate["document"]
        assert placement["content"]["mode"] == "metadata_only"
        assert placement["content"]["candidate_version_id"] == (
            candidate["content"]["candidate_version_id"]
        )
        assert placement["content"]["can_read"] is False
        assert placement["content"]["can_diff"] is False
        assert placement["diff_summary"] == "placed under Inbox"
        assert evidence["reason"] == "seed dry-run review"
        approve = client.post(
            f"/api/v1/workspaces/{workspace_id}/reviews/{review_id}/decision",
            json={"decision_type": "approve"},
        )
        assert approve.status_code == 200, approve.text
    else:
        assert placement["document"]["document_id"] is None
        assert placement["document"]["reader_href"] is None
        assert placement["content"]["mode"] == "unavailable"
        assert placement["content"]["candidate_version_id"] is None
        assert placement["diff_summary"] is None
        assert evidence["reason"] is None


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


def test_group_selection_includes_unloaded_reviews_and_settles_exact_candidate(
    page_service,
):
    client = _make_client(page_service)
    workspace_id = _current_workspace_id(client)
    review_id = _seed_pending_review(page_service, workspace_id)
    page_id, siblings = _seed_same_candidate_reviews(
        page_service, workspace_id, review_id, 5
    )
    queue = client.get(
        f"/api/v1/workspaces/{workspace_id}/reviews",
        params={"target_page_id": page_id, "status": "pending", "limit": 2},
    ).json()["data"]
    assert len(queue["items"]) == 2
    assert queue["next_cursor"] is not None
    assert queue["total"] == 6

    selection = _selection(client, workspace_id, filters={
        "target_page_id": page_id, "status": "pending", "scope": "all"
    })
    expected = {review_id, *siblings}
    assert set(selection["review_item_ids"]) == expected
    assert selection["total"] == 6
    assert selection["eligible_count"] == 1
    assert sum(item["will_auto_settle"] for item in selection["items"]) == 5
    representative = next(item for item in selection["items"] if item["eligible"])
    assert set(representative["auto_settled_review_item_ids"]) == (
        expected - {representative["id"]}
    )
    assert representative["auto_settled_review_item_ids"] == (
        selection["auto_settled_review_item_ids"]
    )
    assert all(not item["auto_settled_review_item_ids"]
               for item in selection["items"] if not item["eligible"])

    # A new queue item on a different page is outside the fixed selection.
    later = _seed_pending_review(page_service, workspace_id, title="Later page")
    result = _execute_selection(
        client, workspace_id, selection["selection_token"], batch_limit=1
    )
    assert result.status_code == 200, result.text
    data = result.json()["data"]
    assert len(data["approved"]) == 1
    assert set(data["approved"]) | set(data["auto_settled_review_item_ids"]) == expected
    assert data["skipped"] == []
    assert data["selection_complete"] is True
    assert data["processed_count"] == 6
    assert data["remaining_count"] == 0
    assert data["next_selection_token"] is None
    assert {item["id"] for item in data["outcomes"]} == expected
    with page_service.session_factory() as session:
        assert session.get(ReviewItem, UUID(later)).status == "pending"
        assert session.scalar(select(func.count(ReviewDecision.id))) == 1
        page = session.get(ContextPage, UUID(page_id))
        assert page.current_published_version_id is not None


def test_selection_approves_newest_selected_draft_and_settles_earlier_one(
    page_service,
):
    client = _make_client(page_service)
    workspace_id = _current_workspace_id(client)
    review_id = _seed_pending_review(page_service, workspace_id)
    with page_service.session_factory.begin() as session:
        review = session.get(ReviewItem, UUID(review_id))
        operation = session.scalar(select(ChangeOperation).where(
            ChangeOperation.change_set_id == review.change_set_id
        ))
        page = session.get(ContextPage, operation.target_id)
        base = session.get(ContextPageVersion, page.latest_draft_version_id)
        newer = ContextWriteService().update_draft(
            session=session,
            workspace_id=UUID(workspace_id),
            request=UpdateDraftRequest(
                page_id=page.id,
                base_version_id=base.id,
                base_content_hash=base.content_hash,
                update_mode="full_rewrite",
                body_markdown="# Dry-run target\n\nNewer body.",
                reason="newer draft",
            ),
            actor_kind="human",
            actor_id=str(session.scalar(select(User.id).where(
                User.email == "test@example.com"
            ))),
        )
    page_id, [older] = _seed_same_candidate_reviews(
        page_service, workspace_id, review_id, 1
    )
    newest = str(newer.review_item_id)
    # Approving revives the archived page, an effect undo does not reverse, so
    # the approved row must not offer a per-document undo.
    with page_service.session_factory.begin() as session:
        session.get(ContextPage, UUID(page_id)).lifecycle_status = "archived"

    # Listed oldest first, the newest draft still becomes the approval basis.
    selection = _selection(client, workspace_id, review_item_ids=[older, newest])
    assert selection["review_item_ids"] == [newest, older]
    assert selection["eligible_count"] == 1
    # The earlier draft's review shows its own version, not the newest one.
    settled = next(item for item in selection["items"] if item["id"] == older)
    assert (settled["will_auto_settle"], settled["target_version_no"]) == (True, 1)
    result = _execute_selection(client, workspace_id, selection["selection_token"])
    assert result.status_code == 200, result.text
    data = result.json()["data"]
    assert data["approved"] == [newest]
    assert data["auto_settled_review_item_ids"] == [older]
    approved = next(item for item in data["outcomes"] if item["id"] == newest)
    assert approved["decision_id"] is None
    with page_service.session_factory() as session:
        page = session.get(ContextPage, UUID(page_id))
        assert page.current_published_version_id == newer.draft_version_id


def test_selection_continuation_keeps_explicit_ids_across_batches(page_service):
    client = _make_client(page_service)
    workspace_id = _current_workspace_id(client)
    ids = [
        _seed_pending_review(page_service, workspace_id, title=f"Batch page {index}")
        for index in range(3)
    ]
    # The explicit selection omits these same-candidate siblings. Their
    # effects must still be attributed to the right document/version rather
    # than presented only as one global count for three different candidates.
    impact_by_id = {}
    for index, review_id in enumerate(ids):
        _page_id, siblings = _seed_same_candidate_reviews(
            page_service, workspace_id, review_id, 2 - index
        )
        impact_by_id[review_id] = set(siblings)
    selection = _selection(client, workspace_id, review_item_ids=ids)
    assert selection["review_item_ids"] == ids
    for item in selection["items"]:
        assert item["eligible"] is True
        assert item["target_title"] == f"Batch page {ids.index(item['id'])}"
        assert item["target_version_no"] == 1
        assert set(item["auto_settled_review_item_ids"]) == impact_by_id[item["id"]]
    assert set(selection["auto_settled_review_item_ids"]) == set().union(
        *impact_by_id.values()
    )
    assert not set(ids).intersection(selection["auto_settled_review_item_ids"])
    first = _execute_selection(
        client, workspace_id, selection["selection_token"], batch_limit=1
    )
    assert first.status_code == 200, first.text
    first_data = first.json()["data"]
    assert first_data["approved"] == [ids[0]]
    assert set(first_data["auto_settled_review_item_ids"]) == impact_by_id[ids[0]]
    assert first_data["processed_count"] == 1
    assert first_data["remaining_count"] == 2
    assert first_data["selection_complete"] is False
    later = _seed_pending_review(page_service, workspace_id, title="Outside batches")

    second = _execute_selection(
        client, workspace_id, first_data["next_selection_token"], batch_limit=2
    )
    assert second.status_code == 200, second.text
    data = second.json()["data"]
    assert data["approved"] == ids[1:]
    assert set(data["auto_settled_review_item_ids"]) == impact_by_id[ids[1]]
    assert data["total"] == 3
    assert data["processed_count"] == 2
    assert data["remaining_count"] == 0
    assert data["selection_complete"] is True
    assert data["requires_reconfirmation"] is False
    with page_service.session_factory() as session:
        assert session.get(ReviewItem, UUID(later)).status == "pending"


@pytest.mark.parametrize("change", ["candidate", "sibling", "metadata"])
def test_selection_refuses_changed_candidate_or_approval_impact(page_service, change):
    client = _make_client(page_service)
    workspace_id = _current_workspace_id(client)
    review_id = _seed_pending_review(page_service, workspace_id)
    selection = _selection(client, workspace_id, review_item_ids=[review_id])
    if change == "sibling":
        _seed_same_candidate_reviews(page_service, workspace_id, review_id, 1)
    else:
        with page_service.session_factory.begin() as session:
            review = session.get(ReviewItem, UUID(review_id))
            operation = session.scalar(select(ChangeOperation).where(
                ChangeOperation.change_set_id == review.change_set_id
            ))
            page = session.get(ContextPage, operation.target_id)
            version = session.get(ContextPageVersion, page.latest_draft_version_id)
            if change == "metadata":
                # Same body and candidate UUID; the confirmation still binds
                # the effective metadata and editing revision.
                page.title = "Changed title after confirmation"
                version.editing_revision += 1
            else:
                ContextWriteService().update_draft(
                    session=session,
                    workspace_id=UUID(workspace_id),
                    request=UpdateDraftRequest(
                        page_id=page.id,
                        base_version_id=version.id,
                        base_content_hash=version.content_hash,
                        update_mode="full_rewrite",
                        body_markdown="# Different candidate\n\nChanged body.",
                        reason="candidate changed after selection",
                    ),
                    actor_kind="human",
                    actor_id=str(session.scalar(select(User.id).where(
                        User.email == "test@example.com"
                    ))),
                )

    result = _execute_selection(client, workspace_id, selection["selection_token"])
    assert result.status_code == 409, result.text
    assert result.json()["error_code"] == "review_selection_changed"
    with page_service.session_factory() as session:
        assert session.scalar(select(func.count(ReviewDecision.id))) == 0


@pytest.mark.parametrize("failure", ["domain_refusal", "candidate_changed"])
def test_selection_partial_results_preserve_commits_and_item_failure_reasons(
    page_service, monkeypatch, failure,
):
    client = _make_client(page_service)
    workspace_id = _current_workspace_id(client)
    ids = [
        _seed_pending_review(page_service, workspace_id, title=f"Partial page {index}")
        for index in range(3)
    ]
    selection = _selection(client, workspace_id, review_item_ids=ids)
    original = ReviewService.apply_decision

    def refuse_one(self, *args, **kwargs):
        if failure == "domain_refusal" and str(kwargs["review_item_id"]) == ids[1]:
            raise ReadValidationError("publication refused", error_code="blocked_write")
        result = original(self, *args, **kwargs)
        if failure == "candidate_changed" and str(kwargs["review_item_id"]) == ids[0]:
            # Make another candidate's metadata change visible at the first
            # commit boundary, before the next item starts its transaction.
            session = kwargs["session"]
            second = session.get(ReviewItem, UUID(ids[1]))
            operation = session.scalar(select(ChangeOperation).where(
                ChangeOperation.change_set_id == second.change_set_id
            ))
            session.get(ContextPage, operation.target_id).title = "Changed in flight"
        return result

    monkeypatch.setattr(ReviewService, "apply_decision", refuse_one)
    result = _execute_selection(client, workspace_id, selection["selection_token"])
    assert result.status_code == 200, result.text
    data = result.json()["data"]
    assert data["approved"] == [ids[0], ids[2]]
    assert data["skipped"][0]["id"] == ids[1]
    assert data["skipped"][0]["reason_code"] == (
        "blocked_write" if failure == "domain_refusal" else "review_selection_changed"
    )
    assert data["skipped"][0]["reason"]
    assert {item["id"]: item["outcome"] for item in data["outcomes"]} == {
        ids[0]: "approved", ids[1]: "failed", ids[2]: "approved",
    }
    assert data["selection_complete"] is True
    assert data["requires_reconfirmation"] is (failure == "candidate_changed")
    with page_service.session_factory() as session:
        assert [session.get(ReviewItem, UUID(value)).status for value in ids] == [
            "approved", "pending", "approved",
        ]
        assert session.scalar(select(func.count(ReviewDecision.id))) == 2


def test_selection_is_signed_actor_bound_and_expires_without_mutation(
    page_service, monkeypatch,
):
    client = _make_client(page_service)
    workspace_id = _current_workspace_id(client)
    review_id = _seed_pending_review(page_service, workspace_id)
    selection = _selection(client, workspace_id, review_item_ids=[review_id])
    token = selection["selection_token"]
    other = _make_client(page_service, email="other-reviewer@example.com")
    with page_service.session_factory.begin() as session:
        user = session.scalar(select(User).where(
            User.email == "other-reviewer@example.com"
        ))
        grant_workspace_membership(
            session, user_id=user.id, workspace_id=UUID(workspace_id), role="editor"
        )
    wrong_actor = _execute_selection(other, workspace_id, token)
    assert wrong_actor.status_code == 409, wrong_actor.text
    assert wrong_actor.json()["error_code"] == "review_selection_invalid"
    payload, signature = token.split(".")
    wrong_signature = ("A" if signature[0] != "A" else "B") + signature[1:]
    tampered = _execute_selection(
        client, workspace_id, f"{payload}.{wrong_signature}"
    )
    assert tampered.status_code == 409
    expires = datetime.fromisoformat(selection["expires_at"].replace("Z", "+00:00"))
    monkeypatch.setattr(review_selection_service, "time", SimpleNamespace(
        time=lambda: expires.astimezone(UTC).timestamp()
    ))
    expired = _execute_selection(client, workspace_id, token)
    assert expired.status_code == 409, expired.text
    assert expired.json()["error_code"] == "review_selection_expired"
    with page_service.session_factory() as session:
        assert session.get(ReviewItem, UUID(review_id)).status == "pending"
        assert session.scalar(select(func.count(ReviewDecision.id))) == 0


def test_selection_limit_reports_total_instead_of_truncating(page_service, monkeypatch):
    client = _make_client(page_service)
    workspace_id = _current_workspace_id(client)
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/reviews/selection",
        json={"review_item_ids": [str(uuid4()) for _ in range(5001)]},
    )
    assert response.status_code == 422, response.text
    data = response.json()
    assert data["error_code"] == "review_selection_limit_exceeded"
    assert data["details"] == {"total": 5001, "limit": 5000}

    # Use more than LIMIT + 1 matches so counting only the fetched rows fails.
    # A small execution limit avoids creating 5,001 database rows; the explicit
    # ID request above still pins the production limit.
    monkeypatch.setattr(review_selection_service, "SELECTION_LIMIT", 3)
    review_id = _seed_pending_review(page_service, workspace_id)
    with page_service.session_factory.begin() as session:
        source = session.get(ReviewItem, UUID(review_id))
        operation = session.scalar(select(ChangeOperation).where(
            ChangeOperation.change_set_id == source.change_set_id
        ))
        page_id = str(operation.target_id)
        session.add_all([ReviewItem(
            workspace_id=UUID(workspace_id),
            change_set_id=source.change_set_id,
            review_type="context_update",
            status="pending",
            risk_level="low",
        ) for _ in range(4)])
    group = client.post(
        f"/api/v1/workspaces/{workspace_id}/reviews/selection",
        json={"filters": {"target_page_id": page_id, "status": "pending"}},
    )
    assert group.status_code == 422, group.text
    assert group.json()["details"] == {"total": 5, "limit": 3}


def test_single_decision_receipt_retries_once_and_conflicts_on_changed_payload(
    page_service,
):
    # Receipt replay precedes decision-type dispatch. Cover the HTTP contract
    # once with a terminal approval; collaboration tests cover later routing
    # rounds, and PostgreSQL tests cover concurrent receipt collisions.
    client = _make_client(page_service)
    workspace_id = _current_workspace_id(client)
    review_id = _seed_pending_review(page_service, workspace_id)
    url = f"/api/v1/workspaces/{workspace_id}/reviews/{review_id}/decision"
    request = {
        "decision_id": str(uuid4()),
        "decision_type": "approve",
        "decision_reason": "Reason for the original decision.",
    }
    first = client.post(url, json=request)
    assert first.status_code == 200, first.text
    retried = client.post(url, json=request)
    assert retried.status_code == 200, retried.text
    assert len(retried.json()["data"]["decisions"]) == 1
    assert retried.json()["data"]["decisions"][0]["decision_id"] == (
        request["decision_id"]
    )
    conflict = client.post(url, json={**request, "decision_reason": "Changed reason"})
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error_code"] == "decision_id_conflict"
    assert conflict.json()["envelope"]["result_status"] == "conflict"
    with page_service.session_factory() as session:
        assert session.scalar(select(func.count(ReviewDecision.id))) == 1

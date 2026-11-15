"""One publication traversal covers shared drafts, stale previews and Git ancestry."""

from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models.context import ContextPageVersion
from backend.models.workspace import Workspace
from backend.services.page_service import PageConflictError
from backend.services.read_errors import StaleBaseVersionError


def test_shared_drafts_merge_independently_and_keep_closed_commits(page_service):
    # Transition-budget exception: one persisted publication/review traversal
    # proves independent shared branches, stale merge decisions and immutable
    # closed Git parents. A pure merge test cannot expose DB head changes or a
    # rejected review rewriting its original decision; the transport scenario
    # below covers concurrent saves against a different mutable-draft boundary.
    actor = str(uuid4())
    initial = page_service.write_page(
        workspace_slug="default",
        slug="shared-dag",
        title="Shared DAG",
        body_md=(
            "# Shared DAG\n\nFirst paragraph.\n\nMiddle paragraph.\n\nLast paragraph.\n"
        ),
        actor_kind="human",
        actor_id=actor,
        actor_provenance="browser_session",
    )
    with page_service.session_factory() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )

    def begin(source=None):
        return page_service.begin_context_edit(
            initial.id,
            workspace_id,
            "human",
            actor,
            "browser_session",
            str(uuid4()),
            from_version_id=source,
        )

    def save(draft, body, writer=actor):
        return page_service.autosave_page(
            page_id=initial.id,
            workspace_id=workspace_id,
            title=draft.title,
            body_md=body,
            actor_kind="human",
            actor_id=writer,
            base_version_id=draft.version_id,
            base_content_hash=draft.content_hash,
            actor_provenance="browser_session",
            idempotency_key=str(uuid4()),
            expected_editing_revision=draft.editing_revision,
            version_id=draft.version_id,
        )

    def preview(draft):
        return page_service.preview_context_merge(
            initial.id, draft.version_id, workspace_id, actor_id=actor
        )

    def publish(draft, seen, choice=None, key=None):
        candidate = seen["result"] or seen[choice]
        return page_service.publish_draft(
            page_id=initial.id,
            version_id=draft.version_id,
            reason="Reviewed merge",
            publish_despite_findings=False,
            actor_kind="human",
            actor_id=actor,
            actor_provenance="browser_session",
            expected_content_hash=draft.content_hash,
            expected_editing_revision=seen["editing_revision"],
            expected_published_version_id=seen["published_version_id"],
            result_hash=candidate["result_hash"],
            on_conflict=choice,
            idempotency_key=key,
        )

    left, right = begin(), begin()
    assert left.version_id != right.version_id
    left = save(left, left.body_md.replace("First paragraph.", "First edited by left."))
    right = save(
        right,
        right.body_md.replace("Last paragraph.", "Last edited by right."),
        str(uuid4()),
    )
    assert begin(right.version_id).version_id == right.version_id
    stale = preview(right)
    publish(left, preview(left))
    with pytest.raises(StaleBaseVersionError):
        publish(right, stale)
    final_preview = preview(right)
    combined = publish(right, final_preview, key="shared-final-publication")
    replayed = publish(right, final_preview, key="shared-final-publication")
    assert (
        replayed.page.version_id == combined.page.version_id and replayed.page._replayed
    )
    assert "First edited by left." in combined.page.body_md
    assert "Last edited by right." in combined.page.body_md
    assert combined.page.version_id != right.version_id
    with page_service.session_factory() as session:
        closed = session.get(ContextPageVersion, right.version_id)
        assert (
            closed.version_status == "closed"
            and closed.commit_oid
            and closed._body_md is None
        )
    withdrawn = begin()
    withdrawn = save(
        withdrawn,
        withdrawn.body_md.replace("Middle paragraph.", "Middle kept in a commit."),
    )
    page_service.discard_context_edit(
        initial.id, withdrawn.version_id, workspace_id, "human", actor, str(uuid4())
    )
    restarted = begin(withdrawn.version_id)
    assert restarted.parent_version_id == withdrawn.version_id
    # Several commits start one merged draft with both as parents.
    merged = begin([combined.page.version_id, withdrawn.version_id])
    assert "First edited by left." in merged.body_md
    assert "Middle kept in a commit." in merged.body_md
    assert merged.parent_version_ids == [
        combined.page.version_id,
        withdrawn.version_id,
    ]
    with pytest.raises(PageConflictError):
        save(right, right.body_md)

    conflict_left, conflict_right = begin(), begin()
    conflict_left = save(
        conflict_left,
        conflict_left.body_md.replace("Middle paragraph.", "Middle from left."),
    )
    conflict_right = save(
        conflict_right,
        conflict_right.body_md.replace("Middle paragraph.", "Middle from right."),
    )
    publish(conflict_left, preview(conflict_left))
    conflict_preview = preview(conflict_right)
    assert conflict_preview["kind"] == "conflict"
    resolved = publish(conflict_right, conflict_preview, "latest")
    assert "Middle from right." in resolved.page.body_md

    rejected = begin()
    submitted = page_service.submit_context_edit_for_review(
        initial.id,
        rejected.version_id,
        workspace_id,
        "human",
        actor,
        "browser_session",
        rejected.editing_revision,
        str(uuid4()),
    )
    from backend.schemas.context_write import ReviewDecisionRequest
    from backend.services.review_service import ReviewService

    with page_service.session_factory.begin() as session:
        ReviewService().apply_decision(
            session,
            workspace_id,
            submitted.review_item_id,
            ReviewDecisionRequest(decision_type="reject"),
            actor_kind="human",
            actor_id=actor,
            actor_provenance="browser_session",
            actor_role="owner",
        )
        closed = session.get(ContextPageVersion, rejected.version_id)
        assert closed.version_status == "closed" and closed.commit_oid
    assert begin(rejected.version_id).parent_version_id == rejected.version_id


def test_two_editors_merge_browser_baseline_but_mcp_requires_current_revision(
    page_service, mcp_default_actor
):
    # Budget exception required by the approved second PR3 scenario: a real
    # browser baseline, two authenticated contributors and the MCP handler's
    # revision refusal must preserve the same persisted draft. A body-merge
    # unit cannot detect transport omissions or a wrong-version write.
    from uuid import UUID

    from fastapi.testclient import TestClient

    from backend.mcp.router import dispatch
    from backend.tests.conftest import authenticate_test_client_in_workspace
    from backend.tests.integration._cross_transport_helpers import make_client

    owner = make_client(page_service)
    workspace_id = owner.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    owner_id = owner.get("/api/v1/auth/me").json()["data"]["user_id"]
    peer = TestClient(owner.app)
    assert (
        authenticate_test_client_in_workspace(
            peer,
            page_service.session_factory,
            email="concurrent-draft-peer@example.com",
            role="editor",
        )
        == workspace_id
    )
    pages_url = f"/api/v1/workspaces/{workspace_id}/context/pages"
    response = owner.post(
        pages_url,
        json={
            "title": "Concurrent shared draft",
            "body_md": "# Concurrent\n\nFirst paragraph.\n\nLast paragraph.\n",
        },
    )
    assert response.status_code == 201, response.text
    draft = response.json()["data"]
    baseline = {"body_md": draft["body_md"], "page_metadata": draft["page_metadata"]}
    url = f"{pages_url}/{draft['id']}/autosave"
    first = owner.post(
        url,
        json={
            "version_id": draft["version_id"],
            "expected_editing_revision": draft["editing_revision"],
            "base_snapshot": baseline,
            "body_md": draft["body_md"].replace(
                "First paragraph.", "First edited by owner."
            ),
            "idempotency_key": "concurrent-owner-save",
        },
    )
    assert first.status_code == 200, first.text
    second = peer.post(
        url,
        json={
            "version_id": draft["version_id"],
            "expected_editing_revision": draft["editing_revision"],
            "base_snapshot": baseline,
            "body_md": draft["body_md"].replace(
                "Last paragraph.", "Last edited by peer."
            ),
            "idempotency_key": "concurrent-peer-save",
        },
    )
    assert second.status_code == 200, second.text
    merged = second.json()["data"]
    assert merged["version_id"] == draft["version_id"]
    assert merged["autosave_merged"] is True
    assert merged["editing_revision"] == first.json()["data"]["editing_revision"] + 1
    assert "First edited by owner." in merged["body_md"]
    assert "Last edited by peer." in merged["body_md"]

    actor = mcp_default_actor(
        actor_id=owner_id, user_id=UUID(owner_id), scopes=("draft-write", "read")
    )
    refused = dispatch(
        "update_context_edit",
        {
            "workspace_id": workspace_id,
            "page_id": draft["id"],
            "version_id": draft["version_id"],
            "expected_editing_revision": draft["editing_revision"],
            "body_md": draft["body_md"].replace(
                "First paragraph.", "Unseen overwrite."
            ),
            "idempotency_key": "concurrent-stale-mcp-save",
        },
        actor=actor,
        page_service=page_service,
    )
    assert refused.envelope.result_status == "conflict"
    assert refused.envelope.error_code == "editing_revision_conflict"
    with page_service.session_factory() as session:
        stored = session.get(ContextPageVersion, UUID(draft["version_id"]))
        assert stored.body_md == merged["body_md"]
        assert stored.editing_revision == merged["editing_revision"]

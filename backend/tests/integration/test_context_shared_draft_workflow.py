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

    # Bringing the published version into a draft is bound to the result the
    # person saw, edits included: an unedited result's hash is refused, and the
    # edited result is saved with the commit added to the draft's parents.
    behind, ahead = begin(), begin()
    ahead = save(ahead, ahead.body_md.replace("Middle from right.", "Middle moved."))
    moved = publish(ahead, preview(ahead)).page.version_id
    edited = behind.body_md.replace("Middle from right.", "Middle moved.") + (
        "\nKept while bringing in.\n"
    )
    seen = page_service.preview_context_merge(
        initial.id,
        behind.version_id,
        workspace_id,
        actor_id=actor,
        commit_version_id=moved,
        body_md=edited,
    )

    def bring(result_hash):
        return page_service.bring_in_context_version(
            page_id=initial.id,
            version_id=behind.version_id,
            workspace_id=workspace_id,
            expected_editing_revision=behind.editing_revision,
            idempotency_key=str(uuid4()),
            actor_kind="human",
            actor_id=actor,
            actor_provenance="browser_session",
            commit_version_id=moved,
            result_hash=result_hash,
            result_body_md=edited,
        )

    with pytest.raises(StaleBaseVersionError):
        bring(preview(behind)["result"]["result_hash"])
    brought = bring(seen["result"]["result_hash"])
    assert "Kept while bringing in." in brought.body_md
    assert "Middle moved." in brought.body_md
    assert moved in brought.parent_version_ids

    # REST History appends across the draft-to-commit boundary, even when a
    # draft's merged parent is a newer commit. The default keeps version order.
    from backend.policy.auth import ActorContext
    from backend.services.domain_read import DomainReadService

    editor = ActorContext(
        actor_kind="human",
        actor_id=actor,
        token_kind="session",
        scopes=("read",),
        workspace_id=workspace_id,
        workspace_role="editor",
        role="editor",
    )
    with page_service.session_factory() as session:
        history = DomainReadService().list_changes
        options = {
            "session": session,
            "workspace_id": workspace_id,
            "target_type": "context_page",
            "target_id": initial.id,
            "actor": editor,
        }
        first = history(**options, limit=1, drafts_first=True)
        later = history(**options, limit=3, cursor=first.next_cursor, drafts_first=True)
        commits = history(
            **options, limit=50, cursor=later.next_cursor, drafts_first=True
        )
        # Every page is capped, and the second one crosses from drafts to commits.
        assert len(first.versions) == 1 and len(later.versions) == 3
        assert later.versions[0].version_status == "draft"
        assert later.versions[-1].version_status != "draft"
        assert commits.next_cursor is None

        # The drafts lead and the commits follow, each newest first by version
        # number (which never changes), so no row is repeated, lost or moved.
        paged = first.versions + later.versions + commits.versions
        drafts = [row for row in paged if row.version_status == "draft"]
        assert paged[: len(drafts)] == drafts
        assert {row.version_id for row in drafts} == {
            behind.version_id,
            merged.version_id,
            restarted.version_id,
        }
        for phase in (drafts, paged[len(drafts) :]):
            numbers = [row.version_no for row in phase]
            assert numbers == sorted(numbers, reverse=True)
        # The default (MCP) order stays newest first by version number.
        legacy = [row.version_no for row in history(**options, limit=50).versions]
        assert legacy == sorted(legacy, reverse=True)
        assert sorted(row.version_no for row in paged) == sorted(legacy)

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

    # An overlapping browser save returns both choices and the draft the merge
    # used; confirming a choice is an ordinary save on that draft, and the
    # same confirmation merges onto the draft if it moved again meanwhile.
    overlap = peer.post(
        url,
        json={
            "version_id": draft["version_id"],
            "expected_editing_revision": draft["editing_revision"],
            "base_snapshot": baseline,
            "body_md": draft["body_md"].replace(
                "First paragraph.", "First rewritten by peer."
            ),
            "idempotency_key": "concurrent-peer-overlap",
        },
    )
    assert overlap.status_code == 409, overlap.text
    conflict = overlap.json()
    assert conflict["error_code"] == "context_merge_conflict"
    choices = conflict["merge_preview"]
    assert choices["kind"] == "conflict" and choices["latest_side"] == "ours"
    assert choices["result"] is None
    assert choices["sides"]["theirs"]["version_id"] == draft["version_id"]
    assert conflict["server_editing_revision"] == merged["editing_revision"]
    preview = peer.post(
        f"{pages_url}/{draft['id']}/merge-preview",
        json={"version_id": merged["version_id"]},
    )
    assert preview.status_code == 200, preview.text
    for payload in (choices, preview.json()["data"]):
        assert payload["version_id"] == merged["version_id"]
        assert payload["content_hash"] == merged["content_hash"]
        assert payload["references_hash"] == merged["references_hash"]
    kept = choices["keep_both"]["body_md"]
    assert "First edited by owner." in kept and "First rewritten by peer." in kept
    confirm = {
        "version_id": draft["version_id"],
        "expected_editing_revision": conflict["server_editing_revision"],
        "base_content_hash": conflict["server_content_hash"],
        "base_snapshot": conflict["server_snapshot"],
        "body_md": kept,
    }
    saved = peer.post(url, json={**confirm, "idempotency_key": "concurrent-confirm"})
    assert saved.status_code == 200, saved.text
    saved = saved.json()["data"]
    assert saved["autosave_merged"] is False
    assert "First rewritten by peer." in saved["body_md"]
    moved = owner.post(
        url,
        json={
            "version_id": draft["version_id"],
            "expected_editing_revision": saved["editing_revision"],
            "base_snapshot": {
                "body_md": saved["body_md"],
                "page_metadata": saved["page_metadata"],
            },
            "body_md": saved["body_md"].replace(
                "Last edited by peer.", "Last moved by owner."
            ),
            "idempotency_key": "concurrent-owner-move",
        },
    )
    assert moved.status_code == 200, moved.text
    again = peer.post(url, json={**confirm, "idempotency_key": "concurrent-reconfirm"})
    assert again.status_code == 200, again.text
    again = again.json()["data"]
    assert again["autosave_merged"] is True
    assert "Last moved by owner." in again["body_md"]
    assert "First rewritten by peer." in again["body_md"]

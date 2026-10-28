from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.review import ReviewItem
from backend.models.workspace import Workspace
from backend.repositories.context_page_repo import insert_version
from backend.tests.integration._cross_transport_helpers import make_client
from backend.tests.utils.prepared_body import prepare_context_body


def _pages_url(client) -> str:
    """P9.1: the canonical, workspace-scoped pages collection URL for
    ``client``'s current workspace."""

    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    return f"/api/v1/workspaces/{workspace_id}/context/pages"


def _create_published(client, *, title: str, body_md: str) -> dict:
    """P9.1: ``POST context/pages`` always lands in "editing" -- publish it
    immediately with a follow-up PATCH, mirroring the retired
    ``write_page(as_draft=False)`` single-shot create-and-publish."""

    pages_url = _pages_url(client)
    created = client.post(pages_url, json={"title": title, "body_md": ""})
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    published = client.patch(
        f"{pages_url}/{data['id']}",
        json={
            "title": title,
            "body_md": body_md,
            "base_version_id": data["version_id"],
            "base_content_hash": data["content_hash"],
        },
    )
    assert published.status_code == 200, published.text
    return published.json()["data"]


@pytest.mark.parametrize(
    (
        "trigger_type",
        "reason",
        "graph_status",
        "direct_change_set",
        "review_status",
        "expected_code",
    ),
    [
        (
            "manual_edit",
            "page.autosave.draft",
            "linear",
            False,
            None,
            "draft.autosave",
        ),
        ("manual_edit", "page.create", "linear", False, None, "draft.page_create"),
        ("manual_edit", "page.update", "linear", False, None, "draft.page_update"),
        (
            "manual_edit",
            "page.restore_version",
            "linear",
            False,
            None,
            "draft.restore",
        ),
        (
            "manual_stale_recovery",
            "page.preserve_alternative",
            "branched",
            False,
            "pending",
            "draft.alternative",
        ),
        (
            "mcp_write_draft",
            "page.update",
            "linear",
            True,
            "pending",
            "draft.mcp_write",
        ),
        (
            "mcp_update_draft",
            "page.create",
            "linear",
            True,
            "pending",
            "draft.mcp_update",
        ),
        (
            "context_ingress_accept",
            "accept proposal",
            "linear",
            True,
            None,
            "draft.ingress_accept",
        ),
        (
            "agent_authenticated_write",
            "page.update",
            "linear",
            False,
            "pending",
            "draft.ai_staged",
        ),
        (
            "agent_authenticated_write",
            "page.autosave.draft",
            "linear",
            False,
            "pending",
            "draft.ai_staged",
        ),
    ],
)
def test_every_draft_origin_has_a_stable_read_reason(
    page_service,
    trigger_type,
    reason,
    graph_status,
    direct_change_set,
    review_status,
    expected_code,
):
    """Cover every draft-minting lane, including NULL version.change_set_id."""

    with page_service.session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        page = ContextPage(
            workspace_id=workspace.id,
            slug=f"draft-reason-{uuid4().hex}",
            title="Draft reason",
            lifecycle_status="draft",
        )
        session.add(page)
        session.flush()
        change_set = ChangeSet(
            workspace_id=workspace.id,
            reason=reason,
            trigger_type=trigger_type,
            actor_type="user",
            actor_id=uuid4(),
            status="pending_review" if review_status == "pending" else "applied",
            risk_level="low",
        )
        session.add(change_set)
        session.flush()
        body = "# Draft\n\nNot published yet.\n"
        fixture_actor_id = str(uuid4())
        version = insert_version(
            session,
            page_id=page.id,
            parent_version_id=None,
            body=prepare_context_body(
                session,
                page_id=page.id,
                body_md=body,
                actor_kind="user",
                actor_id=fixture_actor_id,
            ),
            actor_kind="user",
            actor_id=fixture_actor_id,
            version_status="draft",
            graph_status=graph_status,
            change_set_id=change_set.id if direct_change_set else None,
        )
        page.current_version_id = version.id
        page.latest_draft_version_id = version.id
        session.add(
            ChangeOperation(
                workspace_id=workspace.id,
                change_set_id=change_set.id,
                target_type="context_page",
                target_id=page.id,
                operation="update",
                diff_summary=reason,
                diff_ref=f"context_page_version:{version.id}",
            )
        )
        if review_status is not None:
            session.add(
                ReviewItem(
                    workspace_id=workspace.id,
                    change_set_id=change_set.id,
                    review_type="context_update",
                    status=review_status,
                    risk_level="low",
                    decision_summary="test review",
                )
            )
        page_id = page.id

    client = make_client(page_service)
    response = client.get(f"{_pages_url(client)}/{page_id}")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    codes = {item["code"] for item in data["publish_blocked_reasons"]}
    assert expected_code in codes
    assert data["version_status"] == "draft"
    assert data["current_published_version_id"] is None


def test_get_draft_uses_read_safe_warning_not_failed_save_language(page_service):
    # P9.1: `_publish_blocked_reasons` only computes anything for
    # `version_status == "draft"` -- a page-first "editing" row (the old
    # `write_page(as_draft=True)`'s replacement) reads back with zero
    # warnings, not the "unpublished" warning this test pins. Submitting
    # for review moves it from "editing" to "draft", matching what
    # `as_draft=True` used to produce directly.
    client = make_client(page_service)
    pages_url = _pages_url(client)
    created = client.post(
        pages_url,
        json={"title": "Draft", "body_md": "# Draft\n"},
    )
    assert created.status_code == 201, created.text
    page = created.json()["data"]
    page_id = page["id"]

    submitted = client.post(
        f"{pages_url}/{page_id}/submit-for-review",
        json={
            "idempotency_key": "draft-get-warning-submit",
            "version_id": page["version_id"],
            "expected_editing_revision": page["editing_revision"],
        },
    )
    assert submitted.status_code == 200, submitted.text

    response = client.get(f"{pages_url}/{page_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    # The read succeeded. It reports the page's state through the warnings
    # channel; it does not describe itself as a partial result, which is what a
    # write says when it saved without publishing.
    assert body["envelope"]["result_status"] == "succeeded"
    assert {warning["code"] for warning in body["envelope"]["warnings"]} == {
        "draft_unpublished"
    }
    assert all(
        "save" not in warning["message"].lower()
        for warning in body["envelope"]["warnings"]
    )


def test_unknown_draft_evidence_uses_nonempty_final_fallback(page_service):
    with page_service.session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        page = ContextPage(
            workspace_id=workspace.id,
            slug="draft-final-fallback",
            title="Fallback",
            lifecycle_status="draft",
        )
        session.add(page)
        session.flush()
        body = "# Draft\n"
        version = insert_version(
            session,
            page_id=page.id,
            parent_version_id=None,
            body=prepare_context_body(
                session,
                page_id=page.id,
                body_md=body,
                actor_kind="system",
                actor_id="system",
            ),
            actor_kind="system",
            actor_id="system",
            version_status="draft",
        )
        page.current_version_id = version.id
        page.latest_draft_version_id = version.id
        page_id = page.id

    client = make_client(page_service)
    response = client.get(f"{_pages_url(client)}/{page_id}")
    assert response.status_code == 200
    assert response.json()["data"]["publish_blocked_reasons"] == [
        {
            "code": "draft.never_published",
            "message": "This page has no published version yet.",
        }
    ]


def test_reader_modified_time_stays_at_published_head_when_draft_is_staged(
    page_service,
):
    client = make_client(page_service)
    pages_url = _pages_url(client)
    published = _create_published(
        client, title="Published", body_md="# Published\n\nReader body.\n"
    )
    newer = _create_published(
        client, title="Newer published", body_md="# Newer published\n"
    )
    # The list orders pages by their recorded published instants, which are
    # wall-clock readings: a clock that steps back between the two publications
    # would turn "newer" into the older one. Pin the instants this test is about.
    # The pinned instant keeps its time of day and microseconds so that a
    # response which rounds or truncates the published time is still caught.
    published_at = datetime(2000, 1, 1, 12, 34, 56, 123456, tzinfo=UTC)
    with page_service.session_factory.begin() as session:
        for data, instant in (
            (published, published_at),
            (newer, published_at + timedelta(days=1)),
        ):
            version = session.get(ContextPageVersion, UUID(data["version_id"]))
            page = session.get(ContextPage, UUID(data["id"]))
            assert version is not None and page is not None
            version.created_at = instant
            page.updated_at = instant

    autosave_response = client.post(
        f"{pages_url}/{published['id']}/autosave",
        json={"idempotency_key": "t-49",
            "title": "Published",
            "body_md": "# Published\n\nUnpublished editor body.\n",
            "base_version_id": published["version_id"],
            "base_content_hash": published["content_hash"],
            "expected_editing_revision": published["editing_revision"],
            "session_version_id": None,
        },
    )
    assert autosave_response.status_code == 200, autosave_response.text
    draft = autosave_response.json()["data"]
    assert draft["version_status"] == "draft"
    assert datetime.fromisoformat(draft["updated_at"]) == published_at

    # Stamp the draft and the page after both publications, the two wrong
    # candidates for a "modified" time, so ranking by either one is caught.
    with page_service.session_factory.begin() as session:
        version = session.get(ContextPageVersion, UUID(draft["version_id"]))
        page = session.get(ContextPage, UUID(published["id"]))
        assert version is not None and page is not None
        version.created_at = published_at + timedelta(days=2)
        page.updated_at = published_at + timedelta(days=2)

    read = client.get(f"{pages_url}/{published['id']}")
    assert read.status_code == 200
    assert datetime.fromisoformat(read.json()["data"]["updated_at"]) == published_at

    # P9.1: the old dedicated `/api/v1/pages` list and the intermediate
    # `context-pages` list are now the exact same canonical route/schema
    # (`ContextPageSummary`, which has no `version_status` field -- only
    # `lifecycle_status` and `latest_draft_version_id`) -- one list check
    # against it covers what used to be two separate assertions.
    page_list = client.get(pages_url)
    assert page_list.status_code == 200
    listed = next(
        page for page in page_list.json()["data"] if page["id"] == published["id"]
    )
    assert datetime.fromisoformat(listed["updated_at"]) == published_at
    assert listed["latest_draft_version_id"] == draft["version_id"]
    page_ids = [page["id"] for page in page_list.json()["data"]]
    assert page_ids.index(newer["id"]) < page_ids.index(published["id"])


def test_viewer_cannot_infer_a_draft_from_the_editable_head(page_service):
    """Redacting the draft pointer alone still answers the question.

    `current_version_id` follows the draft the moment a session stages one, so
    an actor comparing it against the published pointer learns both that a
    draft exists and its id. An actor who may not see drafts is told about the
    published head instead.
    """

    from backend.policy.auth import ActorContext

    client = make_client(page_service)
    page = _create_published(
        client, title="Page", body_md="# Page\n\nPublished body.\n"
    )
    published_version_id = UUID(page["version_id"])

    staged = client.post(
        f"{_pages_url(client)}/{page['id']}/autosave",
        json={"idempotency_key": "t-50",
            "title": "Page",
            "body_md": "# Page\n\nStaged, not published.\n",
            "base_version_id": page["version_id"],
            "base_content_hash": page["content_hash"],
            "expected_editing_revision": page["editing_revision"],
            "session_version_id": None,
        },
    )
    assert staged.status_code == 200, staged.text
    draft_version_id = UUID(staged.json()["data"]["version_id"])
    assert draft_version_id != published_version_id

    editor = ActorContext(
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        provenance="browser_session",
        workspace_role="editor",
    )
    viewer = ActorContext(
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000011",
        provenance="browser_session",
        workspace_role="viewer",
    )

    seen_by_editor = page_service.read_page(page_id=UUID(page["id"]), actor=editor)
    assert seen_by_editor.current_version_id == draft_version_id

    seen_by_viewer = page_service.read_page(page_id=UUID(page["id"]), actor=viewer)
    assert seen_by_viewer.version_id == published_version_id
    assert seen_by_viewer.current_version_id == published_version_id
    assert seen_by_viewer.current_published_version_id == published_version_id

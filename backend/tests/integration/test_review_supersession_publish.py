"""A revised draft must stay approvable.

Revising a reviewed draft forks an exact-content successor and supersedes the
review that spoke for the previous version. Lineage predecessors must not block
the successor's live review, while a version with only superseded reviews must
remain unpublishable.
"""

from uuid import UUID

from sqlalchemy import select

from backend.models.review import ReviewItem
from backend.tests.integration._cross_transport_helpers import (
    issue_personal_api_token,
    make_client,
)


def _pages_url(workspace_id) -> str:
    return f"/api/v1/workspaces/{workspace_id}/context/pages"


def _seed_page(client, workspace_id) -> dict:
    # P9.1: `POST context/pages` always lands in "editing" -- publish it
    # immediately with a follow-up PATCH, mirroring the retired
    # `write_page(as_draft=False)` single-shot create-and-publish. The
    # subsequent bearer-token autosaves below expect to fork a fresh
    # "draft" off an already-published head.
    pages_url = _pages_url(workspace_id)
    created = client.post(pages_url, json={"title": "Revised draft", "body_md": ""})
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    published = client.patch(
        f"{pages_url}/{data['id']}",
        json={
            "title": "Revised draft",
            "body_md": "# Revised draft\n\nPublished body.\n",
            "base_version_id": data["version_id"],
            "base_content_hash": data["content_hash"],
        },
    )
    assert published.status_code == 200, published.text
    return published.json()["data"]


def _revise(
    client,
    headers,
    workspace_id,
    page_id,
    base_version,
    base_hash,
    session_version,
    expected_editing_revision,
    n,
):
    response = client.post(
        f"{_pages_url(workspace_id)}/{page_id}/autosave",
        headers=headers,
        json={
            "idempotency_key": f"revise-{page_id}-{session_version}-{n}",
            "title": "Revised draft",
            "body_md": f"# Revised draft\n\nRevision {n}.\n",
            "base_version_id": base_version,
            "base_content_hash": base_hash,
            "expected_editing_revision": expected_editing_revision,
            "session_version_id": session_version,
        },
    )
    assert response.status_code == 200, response.text
    saved = response.json()["data"]
    return saved["version_id"], saved["content_hash"], saved["editing_revision"]


def test_approving_a_twice_revised_draft_publishes_it(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    created = _seed_page(client, workspace_id)
    token = issue_personal_api_token(client, scopes=["mcp:write"])
    session_cookies = dict(client.cookies)
    client.cookies.clear()
    headers = {"Authorization": f"Bearer {token}"}

    base_version = created["version_id"]
    base_hash = created["content_hash"]
    editing_revision = created["editing_revision"]
    for revision in range(3):
        base_version, base_hash, editing_revision = _revise(
            client,
            headers,
            workspace_id,
            created["id"],
            base_version,
            base_hash,
            base_version,
            editing_revision,
            revision,
        )

    for name, value in session_cookies.items():
        client.cookies.set(name, value)

    with page_service.session_factory() as session:
        rows = session.execute(
            select(ReviewItem.id, ReviewItem.status)
        ).all()
    statuses = [status for _, status in rows]
    # The shape that used to deadlock: one live review beside superseded ones.
    assert statuses.count("superseded") >= 2
    pending = [review_id for review_id, status in rows if status == "pending"]
    assert len(pending) == 1

    decision = client.post(
        f"/api/v1/workspaces/{workspace_id}/reviews/{pending[0]}/decision",
        json={"decision_type": "approve", "decision_reason": "reads fine"},
    )
    assert decision.status_code == 200, decision.text

    page = client.get(f"{_pages_url(workspace_id)}/{created['id']}").json()["data"]
    assert page["current_version_id"] == str(base_version)
    with page_service.session_factory() as session:
        review = session.get(ReviewItem, pending[0])
        assert review is not None
        assert review.status == "approved"


def test_a_draft_whose_only_reviews_are_superseded_still_refuses_publish(
    page_service,
):
    """A version whose exact reviews are superseded has no publish claim."""
    from backend.schemas.context_write import PublishContextPageRequest
    from backend.services.context_write_service import ContextWriteService
    from backend.services.read_errors import RejectedByPolicyError

    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    created = _seed_page(client, workspace_id)
    token = issue_personal_api_token(client, scopes=["mcp:write"])
    client.cookies.clear()
    headers = {"Authorization": f"Bearer {token}"}
    base_version, base_hash, editing_revision = _revise(
        client,
        headers,
        workspace_id,
        created["id"],
        created["version_id"],
        created["content_hash"],
        created["version_id"],
        created["editing_revision"],
        0,
    )
    latest_version, _latest_hash, _latest_editing_revision = _revise(
        client,
        headers,
        workspace_id,
        created["id"],
        base_version,
        base_hash,
        base_version,
        editing_revision,
        1,
    )

    # Mark every review superseded, then try to publish the draft directly.
    with page_service.session_factory.begin() as session:
        reviews = session.scalars(select(ReviewItem)).all()
        workspace_id = reviews[0].workspace_id
        for review in reviews:
            review.status = "superseded"

    write_service = ContextWriteService()
    with page_service.session_factory.begin() as session:
        try:
            write_service.publish_draft(
                session=session,
                workspace_id=workspace_id,
                page_id=UUID(created["id"]),
                request=PublishContextPageRequest(
                    version_id=UUID(latest_version), reason="probe"
                ),
                actor_kind="human",
                actor_id="00000000-0000-0000-0000-000000000010",
            )
        except RejectedByPolicyError as exc:
            assert "superseded" in str(exc)
        else:  # pragma: no cover - the guard must hold
            raise AssertionError("publish_draft accepted an all-superseded draft")

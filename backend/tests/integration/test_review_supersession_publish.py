"""A shared reviewed draft keeps its identity across revisions and publication."""

from sqlalchemy import select

from backend.models.review import ReviewItem
from backend.tests.integration._cross_transport_helpers import (
    issue_personal_api_token,
    make_client,
)
from backend.tests.utils.context_commands import merge_binding


def _pages_url(workspace_id) -> str:
    return f"/api/v1/workspaces/{workspace_id}/context/pages"


def _seed_page(client, workspace_id) -> dict:
    pages_url = _pages_url(workspace_id)
    created = client.post(
        pages_url,
        json={
            "title": "Revised draft",
            "body_md": "# Revised draft\n\nPublished body.\n",
        },
    )
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    published = client.post(
        f"{pages_url}/{data['id']}/versions/{data['version_id']}/publish",
        json={
            "reason": "Published fixture",
            "expected_content_hash": data["content_hash"],
            **merge_binding(client, pages_url, data["id"], data["version_id"]),
        },
    )
    assert published.status_code == 200, published.text
    draft = client.post(
        f"{pages_url}/{data['id']}/begin-edit",
        json={"idempotency_key": "begin-revision-draft"},
    )
    assert draft.status_code == 200, draft.text
    return draft.json()["data"]


def _submit(client, headers, workspace_id, page_id, version_id, revision):
    response = client.post(
        f"{_pages_url(workspace_id)}/{page_id}/submit-for-review",
        headers=headers,
        json={
            "version_id": version_id,
            "expected_editing_revision": revision,
            "idempotency_key": f"submit-{version_id}",
        },
    )
    assert response.status_code == 200, response.text


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
            "version_id": session_version,
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
        assert base_version == created["version_id"]
        if revision == 0:
            _submit(
                client,
                headers,
                workspace_id,
                created["id"],
                base_version,
                editing_revision,
            )

    for name, value in session_cookies.items():
        client.cookies.set(name, value)

    with page_service.session_factory() as session:
        rows = session.execute(select(ReviewItem.id, ReviewItem.status)).all()
    statuses = [status for _, status in rows]
    assert statuses == ["pending"]
    pending = [review_id for review_id, status in rows if status == "pending"]
    assert len(pending) == 1

    decision = client.post(
        f"/api/v1/workspaces/{workspace_id}/reviews/{pending[0]}/decision",
        json={
            "decision_type": "approve",
            "decision_reason": "reads fine",
            **{
                key: value
                for key, value in merge_binding(
                    client, _pages_url(workspace_id), created["id"], base_version
                ).items()
                if key != "idempotency_key"
            },
        },
    )
    assert decision.status_code == 200, decision.text

    page = client.get(f"{_pages_url(workspace_id)}/{created['id']}").json()["data"]
    assert page["current_version_id"] == str(base_version)
    with page_service.session_factory() as session:
        review = session.get(ReviewItem, pending[0])
        assert review is not None
        assert review.status == "approved"


def test_named_draft_can_publish_independently_of_superseded_review(
    page_service,
):
    """An old resolved review cannot block a new explicit browser publication."""

    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    created = _seed_page(client, workspace_id)
    token = issue_personal_api_token(client, scopes=["mcp:write"])
    session_cookies = dict(client.cookies)
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

    _submit(
        client,
        headers,
        workspace_id,
        created["id"],
        latest_version,
        _latest_editing_revision,
    )

    # A previously resolved review does not change this open draft's state.
    with page_service.session_factory.begin() as session:
        reviews = session.scalars(select(ReviewItem)).all()
        workspace_id = reviews[0].workspace_id
        for review in reviews:
            review.status = "superseded"

    for name, value in session_cookies.items():
        client.cookies.set(name, value)
    published = client.post(
        f"{_pages_url(workspace_id)}/{created['id']}/versions/{latest_version}/publish",
        json={
            "reason": "Explicit publication",
            "expected_content_hash": _latest_hash,
            **merge_binding(
                client, _pages_url(workspace_id), created["id"], latest_version
            ),
        },
    )
    assert published.status_code == 200, published.text
    assert published.json()["data"]["version_id"] == latest_version
    with page_service.session_factory() as session:
        assert all(
            review.status == "superseded"
            for review in session.scalars(select(ReviewItem))
        )

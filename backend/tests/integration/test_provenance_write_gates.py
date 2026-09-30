from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select

from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.review import ReviewItem
from backend.models.workspace import Workspace
from backend.schemas.agent import SENSITIVE_AGENT_PAGE_CATEGORIES
from backend.tests.integration._cross_transport_helpers import (
    issue_oauth_access_token,
    issue_personal_api_token,
    make_client,
)


def _workspace_id(client) -> UUID:
    payload = client.get("/api/v1/workspaces").json()["data"]
    return UUID(payload["current_workspace_id"])


def _bearer_headers(
    client,
    workspace_id: UUID,
    token_kind: str,
    *,
    scope: str = "mcp:write",
) -> dict[str, str]:
    if token_kind == "oauth":
        token = issue_oauth_access_token(
            client,
            workspace_id,
            scope=scope,
        )
    else:
        token = issue_personal_api_token(client, scopes=[scope])
    client.cookies.clear()
    return {"Authorization": f"Bearer {token}"}


def _pages_url(workspace_id: UUID) -> str:
    return f"/api/v1/workspaces/{workspace_id}/context/pages"


def _create_context_page(client, workspace_id: UUID, slug: str) -> dict:
    """P9.1: page-first creation always lands in "editing"; publish it
    immediately with a follow-up PATCH, mirroring the retired
    ``write_page(as_draft=False)`` single-shot create-and-publish every
    caller here relies on (a real published body to autosave/agent-write on
    top of)."""

    pages_url = _pages_url(workspace_id)
    created = client.post(pages_url, json={"title": f"Title {slug}", "body_md": ""})
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    published = client.patch(
        f"{pages_url}/{data['id']}",
        json={
            "title": f"Title {slug}",
            "body_md": f"# {slug}\n\nPublished body.",
            "base_version_id": data["version_id"],
            "base_content_hash": data["content_hash"],
        },
    )
    assert published.status_code == 200, published.text
    return published.json()["data"]


def _review_for_context_version(session, version_id: UUID) -> ReviewItem:
    review = session.scalar(
        select(ReviewItem)
        .join(ChangeSet, ReviewItem.change_set_id == ChangeSet.id)
        .join(ChangeOperation, ChangeOperation.change_set_id == ChangeSet.id)
        .where(
            ChangeOperation.diff_ref == f"context_page_version:{version_id}",
            ReviewItem.status == "pending",
        )
        .order_by(ReviewItem.created_at.desc())
    )
    assert review is not None
    return review


@pytest.mark.parametrize("token_kind", ["oauth", "pat"])
@pytest.mark.parametrize("same_author", [True, False])
def test_bearer_autosave_with_session_version_stages_reviewed_draft(
    page_service,
    token_kind,
    same_author,
):
    client = make_client(page_service)
    workspace_id = _workspace_id(client)
    created = _create_context_page(
        client,
        workspace_id,
        f"bearer-autosave-{token_kind}-{'same' if same_author else 'other'}",
    )
    published_version_id = UUID(created["version_id"])

    if not same_author:
        with page_service.session_factory.begin() as session:
            version = session.get(ContextPageVersion, published_version_id)
            assert version is not None
            version.created_by_actor_kind = "human"
            version.created_by_actor_id = "different-author"

    headers = _bearer_headers(client, workspace_id, token_kind)
    response = client.post(
        f"{_pages_url(workspace_id)}/{created['id']}/autosave",
        headers=headers,
        json={"idempotency_key": "t-51",
            "title": f"AI staged {token_kind}",
            "body_md": f"# AI staged {token_kind}\n\nUnreviewed body.",
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
            "expected_editing_revision": created["editing_revision"],
            "session_version_id": created["version_id"],
        },
    )
    assert response.status_code == 200, response.text
    saved = response.json()["data"]
    draft_version_id = UUID(saved["version_id"])
    assert draft_version_id != published_version_id

    with page_service.session_factory() as session:
        page = session.get(ContextPage, UUID(created["id"]))
        draft = session.get(ContextPageVersion, draft_version_id)
        review = _review_for_context_version(session, draft_version_id)
        assert page is not None
        assert draft is not None
        assert page.current_published_version_id == published_version_id
        assert page.latest_draft_version_id == draft_version_id
        assert draft.version_status == "draft"
        assert review.risk_signals["ai_authored"] is True
        assert review.status == "pending"
        # A routine agent draft is not a MUST-be-human decision; it is a review
        # the workspace's automation policy is allowed to release. The default
        # (balanced) preset has that automation switched off, which is why it
        # still waits here.
        assert review.risk_signals["human_review_required"] is False


@pytest.mark.parametrize("same_author", [True, False])
def test_browser_session_autosave_with_session_version_stages_draft(
    page_service,
    same_author,
):
    client = make_client(page_service)
    workspace_id = _workspace_id(client)
    created = _create_context_page(
        client,
        workspace_id,
        f"browser-autosave-{'same' if same_author else 'other'}",
    )
    original_version_id = UUID(created["version_id"])
    if not same_author:
        with page_service.session_factory.begin() as session:
            version = session.get(ContextPageVersion, original_version_id)
            assert version is not None
            version.created_by_actor_kind = "human"
            version.created_by_actor_id = "different-author"

    response = client.post(
        f"{_pages_url(workspace_id)}/{created['id']}/autosave",
        json={"idempotency_key": "t-52",
            "title": "Browser staged",
            "body_md": "# Browser staged\n\nHuman body.",
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
            "expected_editing_revision": created["editing_revision"],
            "session_version_id": created["version_id"],
        },
    )
    assert response.status_code == 200, response.text
    saved = response.json()["data"]
    with page_service.session_factory() as session:
        page = session.get(ContextPage, UUID(created["id"]))
        version = session.get(ContextPageVersion, UUID(saved["version_id"]))
        assert page is not None
        assert version is not None
        assert page.current_published_version_id == original_version_id
        assert page.current_version_id == version.id
        assert page.latest_draft_version_id == version.id
        assert version.version_status == "draft"


def test_aged_ai_context_review_is_released_by_workspace_automation(page_service):
    """Policy may decide an agent draft does not need a person.

    This is the counterpart to the gate itself: an agent write is staged and
    reviewed by default, but a workspace that has switched its automation on
    can let those reviews age out. Vetoing them purely because an AI wrote
    them left the workspace with no way to say so.
    """
    client = make_client(page_service)
    workspace_id = _workspace_id(client)
    created = _create_context_page(client, workspace_id, "aged-ai-review")
    headers = _bearer_headers(client, workspace_id, "pat")
    response = client.post(
        f"{_pages_url(workspace_id)}/{created['id']}/autosave",
        headers=headers,
        json={"idempotency_key": "t-53",
            "title": "Aged AI draft",
            "body_md": "# Aged AI draft\n\nPolicy may release this one.",
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
            "expected_editing_revision": created["editing_revision"],
            "session_version_id": created["version_id"],
        },
    )
    assert response.status_code == 200, response.text
    draft_version_id = UUID(response.json()["data"]["version_id"])

    with page_service.session_factory.begin() as session:
        workspace = session.get(Workspace, workspace_id)
        review = _review_for_context_version(session, draft_version_id)
        assert workspace is not None
        policy = dict(workspace.review_policy)
        policy["automation"] = {
            **policy["automation"],
            "auto_approve_by_risk": {
                "low": {"enabled": True, "delay_days": 0},
                "medium": {"enabled": True, "delay_days": 0},
                "high": {"enabled": True, "delay_days": 0},
            },
        }
        workspace.review_policy = policy
        review.created_at = datetime.now(UTC) - timedelta(days=30)
        review_id = review.id

    login = client.post(
        "/api/v1/auth/login",
        json={"email": "test@example.com", "password": "correct-password"},
    )
    assert login.status_code == 200, login.text
    counts = client.get(f"/api/v1/workspaces/{workspace_id}/reviews/counts")
    assert counts.status_code == 200, counts.text
    assert counts.json()["data"]["auto_approve_candidate_count"] == 1
    swept = client.post(
        f"/api/v1/workspaces/{workspace_id}/reviews/sweep-auto-approvals"
    )
    assert swept.status_code == 200, swept.text
    assert swept.json()["data"]["approved"] == [str(review_id)]
    with page_service.session_factory() as session:
        review = session.get(ReviewItem, review_id)
        assert review is not None
        assert review.status == "approved"


def test_aged_ai_context_review_needing_a_person_is_never_auto_approved(page_service):
    """The other half: automation does not outrank an objective defect.

    The same workspace automation as above, but the body fails the format lint
    floor. That is a mandatory review and no amount of ageing releases it.
    """
    client = make_client(page_service)
    workspace_id = _workspace_id(client)
    created = _create_context_page(client, workspace_id, "aged-ai-review-needs-human")
    headers = _bearer_headers(client, workspace_id, "pat")
    response = client.post(
        f"{_pages_url(workspace_id)}/{created['id']}/autosave",
        headers=headers,
        json={"idempotency_key": "t-54",
            "title": "Aged AI draft",
            # An unclosed fence is the objective lint floor, and a lint error
            # is a veto no automation policy may override.
            "body_md": "# Aged AI draft\n\n```python\nprint('unclosed')\n",
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
            "expected_editing_revision": created["editing_revision"],
            "session_version_id": created["version_id"],
        },
    )
    assert response.status_code == 200, response.text
    draft_version_id = UUID(response.json()["data"]["version_id"])

    with page_service.session_factory.begin() as session:
        workspace = session.get(Workspace, workspace_id)
        review = _review_for_context_version(session, draft_version_id)
        assert workspace is not None
        assert review.risk_signals["human_review_required"] is True
        policy = dict(workspace.review_policy)
        policy["automation"] = {
            **policy["automation"],
            "auto_approve_by_risk": {
                "low": {"enabled": True, "delay_days": 0},
                "medium": {"enabled": True, "delay_days": 0},
                "high": {"enabled": True, "delay_days": 0},
            },
        }
        workspace.review_policy = policy
        review.created_at = datetime.now(UTC) - timedelta(days=30)
        review_id = review.id

    login = client.post(
        "/api/v1/auth/login",
        json={"email": "test@example.com", "password": "correct-password"},
    )
    assert login.status_code == 200, login.text
    swept = client.post(
        f"/api/v1/workspaces/{workspace_id}/reviews/sweep-auto-approvals"
    )
    assert swept.status_code == 200, swept.text
    assert swept.json()["data"]["approved"] == []
    with page_service.session_factory() as session:
        review = session.get(ReviewItem, review_id)
        assert review is not None
        assert review.status == "pending"


def test_workspace_can_stop_requiring_a_person_for_ai_writes(page_service):
    """`always_for_ai_authored=false` is the workspace saying it trusts its
    agents. Their writes then land the way a person's do -- published, with an
    approved ReviewItem carrying the policy snapshot that allowed it, so the
    decision is on the record rather than silent."""
    client = make_client(page_service)
    workspace_id = _workspace_id(client)
    with page_service.session_factory.begin() as session:
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None
        policy = dict(workspace.review_policy)
        policy["review_required"] = {
            **policy["review_required"],
            "always_for_ai_authored": False,
        }
        workspace.review_policy = policy

    created = _create_context_page(client, workspace_id, "trusted-agent-write")
    headers = _bearer_headers(client, workspace_id, "pat")
    response = client.patch(
        f"{_pages_url(workspace_id)}/{created['id']}",
        headers=headers,
        json={
            "title": "Trusted agent write",
            "body_md": "# Trusted agent write\n\nNo person needed here.",
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
        },
    )
    assert response.status_code == 200, response.text
    version_id = UUID(response.json()["data"]["version_id"])

    with page_service.session_factory() as session:
        page = session.get(ContextPage, UUID(created["id"]))
        version = session.get(ContextPageVersion, version_id)
        review = session.scalar(
            select(ReviewItem)
            .join(ChangeSet, ReviewItem.change_set_id == ChangeSet.id)
            .join(ChangeOperation, ChangeOperation.change_set_id == ChangeSet.id)
            .where(
                ChangeOperation.diff_ref == f"context_page_version:{version_id}",
            )
        )
        assert page is not None and version is not None
        assert review is not None
        assert version.version_status == "published_snapshot"
        assert page.current_published_version_id == version_id
        assert review.status == "approved"
        assert review.risk_signals["ai_authored"] is True
        assert review.risk_signals["human_review_required"] is False
        assert review.decision_summary == (
            "agent-authored page write approved by policy"
        )


@pytest.mark.parametrize("path", ["create", "update", "autosave"])
def test_trusted_agent_write_still_stages_when_the_body_fails_lint(
    page_service,
    path,
):
    """Trusting an agent is not trusting a broken body.

    With the trust switch off the write publishes before anything reads the
    body, so a lint failure produced a review for content that was already
    live -- and rejecting that review cannot un-publish it. The objective floor
    has to be checked before the published pointer moves.
    """
    client = make_client(page_service)
    workspace_id = _workspace_id(client)
    with page_service.session_factory.begin() as session:
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None
        policy = dict(workspace.review_policy)
        policy["review_required"] = {
            **policy["review_required"],
            "always_for_ai_authored": False,
        }
        workspace.review_policy = policy

    # Seed over the browser session first: minting a bearer clears the cookie.
    created = (
        None
        if path == "create"
        else _create_context_page(client, workspace_id, "trusted-agent-broken-" + path)
    )
    headers = _bearer_headers(client, workspace_id, "pat")
    # An unclosed fence is the objective format floor.
    broken = "# Broken\n\n```python\nprint('unclosed')\n"

    if path == "create":
        # P9.1: `POST context/pages` is always page-first now -- it never
        # publishes for ANY actor, trusted or not (see
        # `ContextPageCreateRequest`'s own docstring), so the "objective
        # floor must be checked before the published pointer moves" concern
        # this test exists for is structurally moot for create specifically:
        # there is no published pointer for create to move in the first
        # place any more. This case now just pins that a broken body still
        # lands safely in private "editing", not that a review gate caught
        # it.
        response = client.post(
            _pages_url(workspace_id),
            headers=headers,
            json={"title": "Trusted agent broken", "body_md": broken},
        )
        assert response.status_code == 201, response.text
        page_id = UUID(response.json()["data"]["id"])
        version_id = UUID(response.json()["data"]["version_id"])
        with page_service.session_factory() as session:
            page = session.get(ContextPage, page_id)
            version = session.get(ContextPageVersion, version_id)
            assert page is not None and version is not None
            assert version.version_status == "editing"
            assert page.current_published_version_id is None
        return
    else:
        assert created is not None
        page_id = UUID(created["id"])
        body = {
            "title": "Trusted agent broken",
            "body_md": broken,
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
        }
        if path == "autosave":
            body["expected_editing_revision"] = created["editing_revision"]
            body["session_version_id"] = created["version_id"]
            body["idempotency_key"] = f"autosave-{page_id}"
            response = client.post(
                f"{_pages_url(workspace_id)}/{page_id}/autosave",
                headers=headers,
                json=body,
            )
        else:
            response = client.patch(
                f"{_pages_url(workspace_id)}/{page_id}", headers=headers, json=body
            )
        assert response.status_code == 200, response.text

    version_id = UUID(response.json()["data"]["version_id"])
    with page_service.session_factory() as session:
        page = session.get(ContextPage, page_id)
        version = session.get(ContextPageVersion, version_id)
        assert page is not None and version is not None
        assert version.version_status == "draft"
        assert page.current_published_version_id != version_id
        review = _review_for_context_version(session, version_id)
        assert review.status == "pending"
        assert review.risk_signals["lint_error"] is True
        assert review.risk_signals["human_review_required"] is True


def _agent_payload(category: str, slug: str, body: str) -> dict:
    return {
        "category": category,
        "title": f"Title {slug}",
        "slug": slug,
        "body_markdown": body,
        "reason": f"write {slug}",
    }


def _perform_agent_write(
    client,
    workspace_id: UUID,
    *,
    operation: str,
    category: str,
    slug: str,
    headers: dict[str, str] | None = None,
) -> dict:
    headers = headers or {}
    if operation == "create":
        response = client.post(
            f"/api/v1/workspaces/{workspace_id}/agent/pages",
            headers=headers,
            json=_agent_payload(category, slug, f"# {slug}\n\nCreated."),
        )
    else:
        first = client.post(
            f"/api/v1/workspaces/{workspace_id}/agent/pages",
            headers=headers,
            json=_agent_payload(category, slug, f"# {slug}\n\nVersion one."),
        )
        assert first.status_code == 201, first.text
        first_data = first.json()["data"]
        if operation == "update":
            response = client.post(
                f"/api/v1/workspaces/{workspace_id}/agent/pages/"
                f"{first_data['agent_page_id']}/draft",
                headers=headers,
                json={
                    **_agent_payload(
                        category,
                        slug,
                        f"# {slug}\n\nBearer update.",
                    ),
                    "base_version_id": first_data["draft_version_id"],
                },
            )
        else:
            second = client.post(
                f"/api/v1/workspaces/{workspace_id}/agent/pages/"
                f"{first_data['agent_page_id']}/draft",
                headers=headers,
                json={
                    **_agent_payload(
                        category,
                        slug,
                        f"# {slug}\n\nVersion two.",
                    ),
                    "base_version_id": first_data["draft_version_id"],
                },
            )
            assert second.status_code == 200, second.text
            response = client.post(
                f"/api/v1/workspaces/{workspace_id}/agent/pages/"
                f"{first_data['agent_page_id']}/restore",
                headers=headers,
                json={"version_id": first_data["draft_version_id"]},
            )
    assert response.status_code in {200, 201}, response.text
    return response.json()["data"]


@pytest.mark.parametrize("token_kind", ["oauth", "pat"])
@pytest.mark.parametrize("category", ["persona", "memory"])
@pytest.mark.parametrize("operation", ["create", "update", "restore"])
def test_rest_agent_page_bearers_create_reviews_for_all_draft_writes(
    page_service,
    token_kind,
    category,
    operation,
):
    client = make_client(page_service)
    workspace_id = _workspace_id(client)
    human = _perform_agent_write(
        client,
        workspace_id,
        operation=operation,
        category=category,
        slug=f"human-{operation}-{category}-{token_kind}",
    )
    assert human["review_item_id"] is None
    assert human["next_action"] == "owner_activation_required"

    headers = _bearer_headers(
        client,
        workspace_id,
        token_kind,
        scope="mcp:agent",
    )
    bearer = _perform_agent_write(
        client,
        workspace_id,
        operation=operation,
        category=category,
        slug=f"bearer-{operation}-{category}-{token_kind}",
        headers=headers,
    )
    assert bearer["review_item_id"] is not None
    assert bearer["next_action"] == "review_required"
    with page_service.session_factory() as session:
        review = session.get(ReviewItem, UUID(bearer["review_item_id"]))
        assert review is not None
        assert review.status == "pending"
        assert review.risk_signals["ai_authored"] is True
        # Sensitive categories always demand a person; the ordinary ones are
        # left to the automation policy (off by default, so both still pend).
        assert review.risk_signals["human_review_required"] is (
            category in SENSITIVE_AGENT_PAGE_CATEGORIES
        )

"""Publication is explicit: autosave stages, and Save publishes one draft."""

from itertools import count
from uuid import UUID

import pytest
from sqlalchemy import func, select

from backend.models.audit import AuditEvent
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPage, ContextPageVersion, PageLink
from backend.models.lint import LintFinding
from backend.models.review import ReviewItem
from backend.models.workspace import Workspace
from backend.schemas.context_write import ReviewDecisionRequest
from backend.schemas.lint import LintRunCreateRequest
from backend.services.lint_service import LintService
from backend.services.read_errors import RejectedByPolicyError
from backend.services.review_service import ReviewService
from backend.tests.integration._cross_transport_helpers import (
    issue_personal_api_token,
    make_client,
)
from backend.tests.utils.context_commands import merge_binding

ACTOR_ID = "00000000-0000-0000-0000-000000000010"
BROKEN = "# Page\n\n```python\nprint('unclosed')\n"


def _workspace_pages_url(client, *, headers=None) -> str:
    """P9.1: the canonical, workspace-scoped pages collection URL for
    ``client``'s current workspace. Callers that clear the session/cookies
    before their next request (e.g. to switch to a bearer-only credential)
    must pass the same bearer ``headers`` here too, since this itself makes
    a request."""

    response = client.get("/api/v1/workspaces", headers=headers)
    assert response.status_code == 200, response.text
    workspace_id = response.json()["data"]["current_workspace_id"]
    return f"/api/v1/workspaces/{workspace_id}/context/pages"


def _create(client, slug: str, body: str = "# Page\n\nPublished body.\n", **extra):
    as_draft = extra.pop("as_draft", False)
    pages_url = _workspace_pages_url(client)
    response = client.post(pages_url, json={"title": "Page", "body_md": body})
    assert response.status_code == 201, response.text
    created = response.json()["data"]
    created["_publish_binding"] = merge_binding(
        client, pages_url, created["id"], created["version_id"]
    )
    if as_draft:
        return created
    response = client.post(
        f"{pages_url}/{created['id']}/versions/{created['version_id']}/publish",
        json={
            "reason": "publish fixture",
            "expected_content_hash": created["content_hash"],
            **created["_publish_binding"],
            **extra,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


_autosave_idempotency_keys = count()


def _autosave(client, page, body: str, *, headers=None, **extra):
    pages_url = _workspace_pages_url(client, headers=headers)
    extra.pop("session_version_id", None)
    if page["version_status"] != "draft":
        response = client.post(
            f"{pages_url}/{page['id']}/begin-edit",
            headers=headers,
            json={
                "idempotency_key": f"begin-{next(_autosave_idempotency_keys)}",
                "from": page["version_id"],
            },
        )
        assert response.status_code == 200, response.text
        page = response.json()["data"]
    response = client.post(
        f"{pages_url}/{page['id']}/autosave",
        headers=headers,
        json={
            "idempotency_key": f"autosave-{next(_autosave_idempotency_keys)}",
            "title": extra.pop("title", "Page"),
            "body_md": body,
            "version_id": page["version_id"],
            "expected_editing_revision": extra.pop(
                "expected_editing_revision", page["editing_revision"]
            ),
            **extra,
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    data["_publish_binding"] = merge_binding(
        client, pages_url, data["id"], data["version_id"], headers=headers
    )
    return data


def _page_ledger_counts(session, page_id: UUID) -> tuple[int, int, int, int]:
    change_set_ids = select(ChangeOperation.change_set_id).where(
        ChangeOperation.target_type == "context_page",
        ChangeOperation.target_id == page_id,
    )
    return (
        session.scalar(
            select(func.count())
            .select_from(ChangeSet)
            .where(ChangeSet.id.in_(change_set_ids))
        )
        or 0,
        session.scalar(
            select(func.count())
            .select_from(ChangeOperation)
            .where(
                ChangeOperation.target_type == "context_page",
                ChangeOperation.target_id == page_id,
            )
        )
        or 0,
        session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.target_kind == "context_page",
                AuditEvent.target_id == page_id,
            )
        )
        or 0,
        session.scalar(
            select(func.count())
            .select_from(ReviewItem)
            .where(ReviewItem.change_set_id.in_(change_set_ids))
        )
        or 0,
    )


def test_autosave_never_rewrites_another_change_sets_ledger(page_service):
    """Adopting a draft another change set created must not erase its record.

    An editing session rewrites its OWN ledger row as the author keeps typing,
    which is how a whole session costs one change set. But the first autosave
    of a newly created page adopts the draft `page.create` made -- as do a
    restore and an accepted proposal -- and the row that explains what THEY did
    is not this session's to reuse.
    """

    client = make_client(page_service)
    created = _create(client, "adopted-draft-keeps-its-ledger", as_draft=True)
    page_id = UUID(created["id"])

    with page_service.session_factory() as session:
        before = {
            operation.id: (operation.operation, operation.diff_summary)
            for operation in session.scalars(
                select(ChangeOperation).where(
                    ChangeOperation.target_type == "context_page",
                    ChangeOperation.target_id == page_id,
                )
            )
        }

    _autosave(
        client,
        created,
        "# Page\n\nEdited on top of the created draft.\n",
        title="Renamed while editing",
        session_version_id=created["version_id"],
    )

    with page_service.session_factory() as session:
        after = {
            operation.id: (operation.operation, operation.diff_summary)
            for operation in session.scalars(
                select(ChangeOperation).where(
                    ChangeOperation.target_type == "context_page",
                    ChangeOperation.target_id == page_id,
                )
            )
        }

    for operation_id, recorded in before.items():
        assert after[operation_id] == recorded, (
            "autosave rewrote a change operation it does not own"
        )
    # The session staged a title, so it opened exactly one ledger row of its own.
    assert len(after) == len(before) + 1


def test_browser_create_as_draft_has_no_published_head(page_service):
    client = make_client(page_service)
    created = _create(client, "browser-draft-create", as_draft=True)

    with page_service.session_factory() as session:
        page = session.get(ContextPage, UUID(created["id"]))
        version = session.get(ContextPageVersion, UUID(created["version_id"]))
        assert page is not None and version is not None
        assert version.version_status == "draft"
        assert page.current_version_id == version.id
        assert page.latest_draft_version_id is None
        assert page.current_published_version_id is None
        assert page.lifecycle_status == "draft"

    pages_url = _workspace_pages_url(client)
    submitted = client.post(
        f"{pages_url}/{created['id']}/submit-for-review",
        json={
            "idempotency_key": "browser-draft-create-submit",
            "version_id": created["version_id"],
            "expected_editing_revision": created["editing_revision"],
        },
    )
    assert submitted.status_code == 200, submitted.text
    saved = client.post(
        f"{pages_url}/{created['id']}/versions/{created['version_id']}/publish",
        json={
            "reason": "first editor Save",
            "publish_despite_findings": False,
            "expected_content_hash": created["content_hash"],
            **created["_publish_binding"],
            "expected_references_hash": created["references_hash"],
        },
    )
    assert saved.status_code == 200, saved.text
    with page_service.session_factory() as session:
        page = session.get(ContextPage, UUID(created["id"]))
        assert page is not None
        assert page.current_published_version_id == UUID(created["version_id"])


def test_autosave_stages_and_only_explicit_publish_moves_pointer(page_service):
    client = make_client(page_service)
    created = _create(client, "explicit-publish-boundary")
    published_id = UUID(created["version_id"])
    draft = _autosave(
        client,
        created,
        "# Page\n\nStaged body.\n",
        session_version_id=None,
    )

    with page_service.session_factory() as session:
        page = session.get(ContextPage, UUID(created["id"]))
        assert page is not None
        assert page.current_published_version_id == published_id
        assert page.current_version_id == published_id
        assert page.latest_draft_version_id is None

    published = client.post(
        f"{_workspace_pages_url(client)}/{created['id']}/versions/{draft['version_id']}/publish",
        json={
            "reason": "editor Save",
            "publish_despite_findings": False,
            "expected_content_hash": draft["content_hash"],
            **draft["_publish_binding"],
            "expected_references_hash": draft["references_hash"],
        },
    )
    assert published.status_code == 200, published.text
    with page_service.session_factory() as session:
        page = session.get(ContextPage, UUID(created["id"]))
        assert page is not None
        assert page.current_published_version_id == UUID(draft["version_id"])
        assert page.current_version_id == UUID(draft["version_id"])
        assert page.latest_draft_version_id is None


def test_review_approval_cannot_publish_a_malformed_body(page_service):
    client = make_client(page_service)
    created = _create(client, "review-malformed-autosave")
    workspace_id = UUID(
        client.get("/api/v1/workspaces").json()["data"]["current_workspace_id"]
    )
    token = issue_personal_api_token(client, scopes=["mcp:write"])
    client.cookies.clear()
    draft = _autosave(
        client,
        created,
        BROKEN,
        headers={"Authorization": f"Bearer {token}"},
    )
    auth_headers = {"Authorization": f"Bearer {token}"}
    pages_url = _workspace_pages_url(client, headers=auth_headers)
    submitted = client.post(
        f"{pages_url}/{draft['id']}/submit-for-review",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "idempotency_key": "malformed-submit",
            "version_id": draft["version_id"],
            "expected_editing_revision": draft["editing_revision"],
        },
    )
    assert submitted.status_code == 200, submitted.text
    draft["review_item_id"] = submitted.json()["data"]["review_item_id"]
    with page_service.session_factory() as session:
        page = session.get(ContextPage, UUID(created["id"]))
        version = session.get(ContextPageVersion, UUID(draft["version_id"]))
        review = session.get(ReviewItem, UUID(draft["review_item_id"]))
        assert page is not None and version is not None and review is not None
        assert page.workspace_id == workspace_id
        assert version.version_status == "draft"
        assert page.current_published_version_id == UUID(created["version_id"])
        assert review.status == "pending"

    with pytest.raises(RejectedByPolicyError, match="format error") as refused:
        with page_service.session_factory.begin() as session:
            ReviewService().apply_decision(
                session=session,
                workspace_id=workspace_id,
                review_item_id=UUID(draft["review_item_id"]),
                request=ReviewDecisionRequest(decision_type="approve"),
                actor_kind="human",
                actor_id=ACTOR_ID,
                actor_role="owner",
                actor_provenance="browser_session",
            )

    assert refused.value.warnings
    assert all(
        warning.code == "lint_publish_blocked" for warning in refused.value.warnings
    )
    assert any("line 3" in warning.message for warning in refused.value.warnings)

    with page_service.session_factory() as session:
        page = session.get(ContextPage, UUID(created["id"]))
        assert page is not None
        assert page.current_published_version_id == UUID(created["version_id"])


def test_explicit_publish_refuses_findings_without_override(page_service):
    client = make_client(page_service)
    created = _create(client, "publish-refuses-findings")
    draft = _autosave(client, created, BROKEN, session_version_id=None)
    # A human autosave of a malformed body only stages a draft.
    with page_service.session_factory() as session:
        version = session.get(ContextPageVersion, UUID(draft["version_id"]))
        assert version is not None and version.version_status == "draft"

    response = client.post(
        f"{_workspace_pages_url(client)}/{created['id']}/versions/{draft['version_id']}/publish",
        json={
            "reason": "editor Save",
            "publish_despite_findings": False,
            "expected_content_hash": draft["content_hash"],
            **draft["_publish_binding"],
            "expected_references_hash": draft["references_hash"],
        },
    )
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["error_code"] == "rejected_by_policy"
    # The refusal has to be actionable: it names the defect in the author's own
    # editor coordinates so the editor can point at the line instead of just
    # reporting that publishing failed.
    [warning] = body["warnings"]
    assert warning["code"] == "lint_publish_blocked"
    assert "line 3" in warning["message"]
    with page_service.session_factory() as session:
        page = session.get(ContextPage, UUID(created["id"]))
        assert page is not None
        assert page.current_published_version_id == UUID(created["version_id"])


def test_override_published_malformed_body_stays_open_after_clean_draft_lint(
    page_service,
):
    client = make_client(page_service)
    created = _create(client, "published-malformed-stays-visible")
    malformed = _autosave(client, created, BROKEN, session_version_id=None)
    published = client.post(
        f"{_workspace_pages_url(client)}/{created['id']}/versions/{malformed['version_id']}/publish",
        json={
            "reason": "author override",
            "publish_despite_findings": True,
            "expected_content_hash": malformed["content_hash"],
            **malformed["_publish_binding"],
            "expected_references_hash": malformed["references_hash"],
        },
    )
    assert published.status_code == 200, published.text
    payload = published.json()
    assert payload["envelope"]["result_status"] == "partial"
    [warning] = payload["envelope"]["warnings"]
    assert warning["code"] == "published_with_findings"
    assert "line 3" in warning["message"]
    with page_service.session_factory() as session:
        page = session.get(ContextPage, UUID(created["id"]))
        assert page is not None
        assert page.current_published_version_id == UUID(malformed["version_id"])

    lint = LintService()
    with page_service.session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        lint.run_lint(
            session,
            workspace.id,
            LintRunCreateRequest(lint_scope="workspace", trigger_type="manual"),
            "human",
            ACTOR_ID,
        )
        finding = session.scalar(
            select(LintFinding).where(
                LintFinding.target_id == UUID(created["id"]),
                LintFinding.rule_id == "md.code_fence_balance",
            )
        )
        assert finding is not None
        finding_id = finding.id
        assert finding.status == "open"

    clean_draft = _autosave(
        client,
        published.json()["data"],
        "# Page\n\nThe next draft is clean.\n",
        session_version_id=None,
    )
    assert clean_draft["version_status"] == "draft"

    with page_service.session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        lint.run_lint(
            session,
            workspace.id,
            LintRunCreateRequest(lint_scope="workspace", trigger_type="scheduled"),
            "system",
            "lint-worker",
        )
        finding = session.get(LintFinding, finding_id)
        assert finding is not None
        assert finding.status == "open"
        assert finding.resolved_at is None


def test_repeated_browser_autosaves_keep_one_row_and_preserve_published_bytes(
    page_service,
):
    client = make_client(page_service)
    created = _create(
        client,
        "one-browser-session-row",
        "# Page\n\nPublished [[Original target]].\n",
    )
    page_id = UUID(created["id"])
    published_id = UUID(created["version_id"])
    with page_service.session_factory() as session:
        published = session.get(ContextPageVersion, published_id)
        assert published is not None
        original_body = published.body_md
        original_hash = published.content_hash
        original_links = [
            (link.target_slug, link.occurrence_count)
            for link in session.scalars(
                select(PageLink)
                .where(PageLink.source_version_id == published_id)
                .order_by(PageLink.target_slug)
            )
        ]
        before_ledger = _page_ledger_counts(session, page_id)

    current = created
    draft_id = None
    for tick in range(5):
        current = _autosave(
            client,
            current,
            f"# Page\n\nDraft tick {tick} [[Draft target {tick}]].\n",
            session_version_id=draft_id,
        )
        draft_id = current["version_id"]

    with page_service.session_factory() as session:
        page = session.get(ContextPage, page_id)
        versions = session.scalars(
            select(ContextPageVersion).where(ContextPageVersion.page_id == page_id)
        ).all()
        published = session.get(ContextPageVersion, published_id)
        published_links = [
            (link.target_slug, link.occurrence_count)
            for link in session.scalars(
                select(PageLink)
                .where(PageLink.source_version_id == published_id)
                .order_by(PageLink.target_slug)
            )
        ]
        after_ledger = _page_ledger_counts(session, page_id)
        assert page is not None and published is not None
        # One immutable published commit and one shared mutable draft.
        assert len(versions) == 2
        assert page.current_published_version_id == published_id
        assert page.latest_draft_version_id is None
        assert published.body_md == original_body
        assert published.content_hash == original_hash
        assert published_links == original_links
        assert tuple(
            after - before
            for after, before in zip(after_ledger, before_ledger, strict=True)
        ) == (5, 5, 6, 0)


def test_ai_semantic_noop_skips_ledger_but_material_revision_records(page_service):
    client = make_client(page_service)
    created = _create(client, "ai-noop-ledger")
    page_id = UUID(created["id"])
    workspace_id = UUID(
        client.get("/api/v1/workspaces").json()["data"]["current_workspace_id"]
    )
    token = issue_personal_api_token(client, scopes=["mcp:write"])
    client.cookies.clear()
    headers = {"Authorization": f"Bearer {token}"}
    first = _autosave(
        client,
        created,
        "# Page\n\nAI material one.\n",
        headers=headers,
    )
    with page_service.session_factory() as session:
        page = session.get(ContextPage, page_id)
        assert page is not None and page.workspace_id == workspace_id
        after_first = _page_ledger_counts(session, page_id)

    identical = _autosave(
        client,
        first,
        first["body_md"],
        headers=headers,
        session_version_id=first["version_id"],
    )
    with page_service.session_factory() as session:
        after_noop = _page_ledger_counts(session, page_id)
    assert identical["version_id"] == first["version_id"]
    assert after_noop == after_first

    revised = _autosave(
        client,
        identical,
        "# Page\n\nAI material two.\n",
        headers=headers,
        session_version_id=identical["version_id"],
    )
    with page_service.session_factory() as session:
        after_revision = _page_ledger_counts(session, page_id)
    assert revised["version_id"] == first["version_id"]
    assert tuple(
        after - before for after, before in zip(after_revision, after_noop, strict=True)
    ) == (1, 1, 1, 0)


def test_floor_waiver_requires_an_interactive_session(page_service):
    """Only a person who saw the warning may publish over the floor.

    Every channel holds the same draft-write scope and reaches the same publish
    route, so the gate cannot be the route. A bearer client asking to waive the
    floor is refused rather than quietly published without the waiver, because
    a caller told it published cleanly would believe the page is well formed.
    """

    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    created = _create(client, "waiver-needs-a-person")
    draft = _autosave(client, created, BROKEN, session_version_id=None)

    # P9.1 retired the waiver-blind intermediate publish route; it must stay
    # deleted rather than resurface as a smuggling path for a bearer waiver.
    retired_route = client.post(
        f"/api/v1/workspaces/{workspace_id}/context-pages/{created['id']}/publish",
        json={
            "version_id": draft["version_id"],
            "reason": "smuggled override",
            "publish_despite_findings": True,
        },
    )
    assert retired_route.status_code == 404, retired_route.text

    token = issue_personal_api_token(client, scopes=["mcp:write"])
    auth_header = {"Authorization": f"Bearer {token}"}
    pages_url = _workspace_pages_url(client)
    client.cookies.clear()

    response = client.post(
        f"{pages_url}/{created['id']}/versions/{draft['version_id']}/publish",
        headers=auth_header,
        json={
            "reason": "agent override",
            "publish_despite_findings": True,
            "expected_content_hash": draft["content_hash"],
            **draft["_publish_binding"],
            "expected_references_hash": draft["references_hash"],
        },
    )
    # 422 here must come from the browser-provenance gate (PageValidationError
    # -> status_code_for_error), not from a schema-level rejection -- an
    # omitted expected_content_hash would 422 for an unrelated reason and this
    # assertion would stop pinning the waiver gate this test exists for.
    assert response.status_code == 422, response.text
    assert response.json()["error_code"] == "validation_failed"
    assert "interactive browser session" in response.json()["message"]

    with page_service.session_factory() as session:
        page = session.get(ContextPage, UUID(created["id"]))
        assert page is not None
        assert page.current_published_version_id == UUID(created["version_id"])


def test_publish_refuses_a_draft_another_session_rewrote(page_service):
    """Naming the version does not name the bytes.

    A draft row is mutable, so a second tab of the same author can overwrite it
    between this caller staging its buffer and asking to publish it. Publishing
    states the content it meant, and is refused when the row moved.
    """

    client = make_client(page_service)
    created = _create(client, "publish-states-its-content")
    mine = _autosave(
        client,
        created,
        "# Page\n\nWhat I meant to publish.\n",
        session_version_id=None,
    )
    other_tab = _autosave(
        client,
        mine,
        "# Page\n\nAnother tab is still typing.\n",
        session_version_id=mine["version_id"],
    )
    assert other_tab["version_id"] == mine["version_id"]

    # PUB-05: the hash is required at the route boundary; omitting it must
    # 422 instead of publishing whatever bytes the mutable row holds now.
    omitted = client.post(
        f"{_workspace_pages_url(client)}/{created['id']}/versions/{mine['version_id']}/publish",
        json={"reason": "editor Save", "publish_despite_findings": False},
    )
    assert omitted.status_code == 422, omitted.text
    assert "expected_editing_revision" in omitted.text

    response = client.post(
        f"{_workspace_pages_url(client)}/{created['id']}/versions/{mine['version_id']}/publish",
        json={
            "reason": "editor Save",
            "publish_despite_findings": False,
            "expected_content_hash": mine["content_hash"],
            **mine["_publish_binding"],
            "expected_references_hash": mine["references_hash"],
        },
    )
    assert response.status_code == 409, response.text
    assert other_tab["editing_revision"] > mine["editing_revision"]
    assert response.json()["server_content_hash"] == other_tab["content_hash"]

    with page_service.session_factory() as session:
        page = session.get(ContextPage, UUID(created["id"]))
        assert page is not None
        assert page.current_published_version_id == UUID(created["version_id"])


def test_autosave_does_not_adopt_a_ledger_row_that_carries_a_review(page_service):
    """An agent can send our own autosave reason; a review row cannot be faked.

    `reason` is caller-supplied on the MCP write lanes, so ownership cannot rest
    on it. A browser session never files a ReviewItem, so a change set carrying
    one belongs to a write that wanted a decision and is not ours to rewrite.
    """

    client = make_client(page_service)
    created = _create(client, "review-bearing-ledger-is-not-ours")
    page_id = UUID(created["id"])
    token = issue_personal_api_token(client, scopes=["mcp:write"])

    agent_draft = _autosave(
        client,
        created,
        "# Page\n\nAgent staged this.\n",
        headers={"Authorization": f"Bearer {token}"},
        session_version_id=None,
    )

    with page_service.session_factory() as session:
        before = {
            operation.id: (operation.operation, operation.diff_summary)
            for operation in session.scalars(
                select(ChangeOperation).where(
                    ChangeOperation.target_type == "context_page",
                    ChangeOperation.target_id == page_id,
                )
            )
        }

    _autosave(
        client,
        agent_draft,
        "# Page\n\nThe person edits on top of it.\n",
        title="Renamed by the person",
        session_version_id=agent_draft["version_id"],
    )

    with page_service.session_factory() as session:
        after = {
            operation.id: (operation.operation, operation.diff_summary)
            for operation in session.scalars(
                select(ChangeOperation).where(
                    ChangeOperation.target_type == "context_page",
                    ChangeOperation.target_id == page_id,
                )
            )
        }

    for operation_id, recorded in before.items():
        assert after[operation_id] == recorded, (
            "autosave rewrote a ledger row belonging to a reviewed write"
        )

"""Publication is explicit: autosave stages, and Save publishes one draft."""

from itertools import count
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
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
from backend.services.read_errors import ReadValidationError, RejectedByPolicyError
from backend.services.review_service import ReviewService
from backend.tests.conftest import authenticate_test_client_in_workspace
from backend.tests.integration._cross_transport_helpers import (
    issue_personal_api_token,
    make_client,
)

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
    """P9.1: ``POST context/pages`` always lands in "editing" (never
    publishes) -- ``as_draft=True`` (the old upsert-by-slug flag) now maps
    onto that directly (create and stop). The old default, ``as_draft``
    omitted/False, published immediately, so that case additionally PATCHes
    the freshly created page with the real body, which publishes for an
    ordinary browser actor -- the same two end states `write_page` used to
    produce in one call. ``slug`` is accepted for call-site compatibility
    but unused: page-first creation never derives a slug from it, and no
    test in this file asserts on the resulting slug string."""

    as_draft = extra.pop("as_draft", False)
    pages_url = _workspace_pages_url(client)
    created_response = client.post(
        pages_url,
        json={
            "title": "Page",
            "body_md": body if as_draft else "",
        },
    )
    assert created_response.status_code == 201, created_response.text
    created = created_response.json()["data"]
    if as_draft:
        return created
    published_response = client.patch(
        f"{pages_url}/{created['id']}",
        json={
            "title": "Page",
            "body_md": body,
            "base_version_id": created["version_id"],
            "base_content_hash": created["content_hash"],
            **extra,
        },
    )
    assert published_response.status_code == 200, published_response.text
    return published_response.json()["data"]


_autosave_idempotency_keys = count()


def _autosave(client, page, body: str, *, headers=None, **extra):
    response = client.post(
        f"{_workspace_pages_url(client, headers=headers)}/{page['id']}/autosave",
        headers=headers,
        json={
            "idempotency_key": f"autosave-{next(_autosave_idempotency_keys)}",
            "title": extra.pop("title", "Page"),
            "body_md": body,
            "base_version_id": page["version_id"],
            "base_content_hash": page["content_hash"],
            "expected_editing_revision": extra.pop(
                "expected_editing_revision", page["editing_revision"]
            ),
            "session_version_id": extra.pop(
                "session_version_id", page.get("session_version_id")
            ),
            **extra,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


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
    # P9.1: page-first creation always lands in "editing", never "draft"
    # directly -- `latest_draft_version_id` is reserved for a SUBMITTED
    # draft (see `create_page_first`'s own docstring), so it stays unset
    # here. `PageService.publish_draft` requires `version_status == "draft"`
    # AND `page.latest_draft_version_id == version.id`
    # (`require_latest_draft=True`), which a still-"editing" row satisfies
    # only after the explicit submit-for-review step -- unlike the old
    # `write_page(as_draft=True)`, that step unconditionally signals
    # `needs_review=True` (it IS an explicit review request, by design), so
    # reaching a publishable draft now needs a second actor to approve it
    # first, same as `test_editing_full_flow_begin_save_submit_publish`.
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    other_client = TestClient(client.app)
    authenticate_test_client_in_workspace(
        other_client,
        page_service.session_factory,
        email="browser-draft-create-reviewer@example.com",
        role="editor",
    )
    created = _create(client, "browser-draft-create", as_draft=True)

    with page_service.session_factory() as session:
        page = session.get(ContextPage, UUID(created["id"]))
        version = session.get(ContextPageVersion, UUID(created["version_id"]))
        assert page is not None and version is not None
        assert version.version_status == "editing"
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
    review_item_id = submitted.json()["data"]["review_item_id"]
    if review_item_id:
        decision = other_client.post(
            f"/api/v1/workspaces/{workspace_id}/reviews/{review_item_id}/decision",
            json={"decision_type": "approve"},
        )
        assert decision.status_code == 200, decision.text

    saved = client.post(
        f"{pages_url}/{created['id']}/versions/{created['version_id']}/publish",
        json={
            "reason": "first editor Save",
            "publish_despite_findings": False,
            "expected_content_hash": created["content_hash"],
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
        assert page.current_version_id == UUID(draft["version_id"])
        assert page.latest_draft_version_id == UUID(draft["version_id"])

    published = client.post(
        f"{_workspace_pages_url(client)}/{created['id']}/versions/{draft['version_id']}/publish",
        json={
            "reason": "editor Save",
            "publish_despite_findings": False,
            "expected_content_hash": draft["content_hash"],
        },
    )
    assert published.status_code == 200, published.text
    with page_service.session_factory() as session:
        page = session.get(ContextPage, UUID(created["id"]))
        assert page is not None
        assert page.current_published_version_id == UUID(draft["version_id"])
        assert page.current_version_id == UUID(draft["version_id"])
        assert page.latest_draft_version_id is None


def test_human_autosave_cannot_publish_a_malformed_body(page_service):
    client = make_client(page_service)
    created = _create(client, "human-malformed-autosave")
    draft = _autosave(client, created, BROKEN, session_version_id=None)

    with page_service.session_factory() as session:
        page = session.get(ContextPage, UUID(created["id"]))
        version = session.get(ContextPageVersion, UUID(draft["version_id"]))
        assert page is not None and version is not None
        assert version.version_status == "draft"
        assert page.current_published_version_id == UUID(created["version_id"])
        assert page.current_published_version_id != version.id


def test_ai_autosave_cannot_publish_a_malformed_body(page_service):
    client = make_client(page_service)
    created = _create(client, "ai-malformed-autosave")
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

    with page_service.session_factory() as session:
        page = session.get(ContextPage, UUID(created["id"]))
        version = session.get(ContextPageVersion, UUID(draft["version_id"]))
        review = session.get(ReviewItem, UUID(draft["review_item_id"]))
        assert page is not None and version is not None and review is not None
        assert page.workspace_id == workspace_id
        assert version.version_status == "draft"
        assert page.current_published_version_id == UUID(created["version_id"])
        assert review.status == "pending"


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

    with pytest.raises(RejectedByPolicyError, match="format error"):
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

    with page_service.session_factory() as session:
        page = session.get(ContextPage, UUID(created["id"]))
        assert page is not None
        assert page.current_published_version_id == UUID(created["version_id"])


def test_explicit_publish_refuses_findings_without_override(page_service):
    client = make_client(page_service)
    created = _create(client, "publish-refuses-findings")
    draft = _autosave(client, created, BROKEN, session_version_id=None)

    response = client.post(
        f"{_workspace_pages_url(client)}/{created['id']}/versions/{draft['version_id']}/publish",
        json={
            "reason": "editor Save",
            "publish_despite_findings": False,
            "expected_content_hash": draft["content_hash"],
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


def test_workspace_publish_route_cannot_waive_the_floor(page_service):
    """The retired intermediate publish route cannot resurface as a smuggling path.

    This test originally exercised `POST /{workspace_id}/context-pages/{id}
    /publish` (`backend/api/read.py`, deleted by P9.1), which called
    `ContextWriteService.publish_draft` -- a schema deliberately without a
    `publish_despite_findings` field, unlike the browser publish surface
    (`PageService.publish_draft`). P9.1 unified every REST publish caller
    onto the single `PageService.publish_draft` route
    (`context/pages/{page_id}/versions/{version_id}/publish`), which DOES
    carry that field for every caller now (see `test_publish_overrides_
    findings` for the browser-session positive path) -- there is no longer
    a second, waiver-blind schema to smuggle a bearer waiver through.
    `ContextWriteService.publish_draft` itself is consequently unreachable
    from any REST route or MCP tool as of this cutover (confirmed by
    `grep -rn "publish_draft" backend/api backend/mcp` -- zero call sites)
    -- flagged as a real question for the route's owner (dead code to
    delete, or a gap to re-wire), not something this test can resolve.
    The still-live security property this test cared about -- a
    non-interactive bearer caller cannot waive the floor -- is fully
    covered on the canonical route by `test_floor_waiver_requires_an_
    interactive_session` above; this test now only pins that the deleted
    route stays deleted rather than duplicating that assertion.
    """

    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    created = _create(client, "workspace-publish-cannot-waive")

    retired_route = client.post(
        f"/api/v1/workspaces/{workspace_id}/context-pages/{created['id']}/publish",
        json={
            "version_id": created["version_id"],
            "reason": "smuggled override",
            "publish_despite_findings": True,
        },
    )
    assert retired_route.status_code == 404, retired_route.text

    with page_service.session_factory() as session:
        page = session.get(ContextPage, UUID(created["id"]))
        assert page is not None
        assert page.current_published_version_id == UUID(created["version_id"])


def test_explicit_publish_override_reports_editor_coordinate_findings(page_service):
    client = make_client(page_service)
    created = _create(client, "publish-overrides-findings")
    draft = _autosave(client, created, BROKEN, session_version_id=None)

    response = client.post(
        f"{_workspace_pages_url(client)}/{created['id']}/versions/{draft['version_id']}/publish",
        json={
            "reason": "owner override",
            "publish_despite_findings": True,
            "expected_content_hash": draft["content_hash"],
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["envelope"]["result_status"] == "partial"
    [warning] = payload["envelope"]["warnings"]
    assert warning["code"] == "published_with_findings"
    assert "line 3" in warning["message"]
    with page_service.session_factory() as session:
        page = session.get(ContextPage, UUID(created["id"]))
        assert page is not None
        assert page.current_published_version_id == UUID(draft["version_id"])


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
        },
    )
    assert published.status_code == 200, published.text

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


def test_repeated_browser_autosaves_reuse_one_row_and_one_ledger(page_service):
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
        # P9.1: `_create`'s publish-on-create is now two rows structurally
        # (the page-first "editing" placeholder `create_page_first` always
        # inserts, discarded once the immediate PATCH publishes over it --
        # see `apply_context_publication`'s ancestor-demotion walk -- plus
        # the published row itself), not the old single-shot `write_page`
        # row. Repeated autosaves still reuse exactly one further row for
        # the whole session, so the total is 3, not the old 2.
        assert len(versions) == 3
        assert page.current_published_version_id == published_id
        assert page.latest_draft_version_id == UUID(draft_id)
        assert published.body_md == original_body
        assert published.content_hash == original_hash
        assert published_links == original_links
        assert tuple(
            after - before
            for after, before in zip(after_ledger, before_ledger, strict=True)
        ) == (1, 1, 1, 0)


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
    assert revised["version_id"] != first["version_id"]
    assert tuple(
        after - before for after, before in zip(after_revision, after_noop, strict=True)
    ) == (1, 1, 1, 1)


def test_floor_waiver_requires_an_interactive_session(page_service):
    """Only a person who saw the warning may publish over the floor.

    Every channel holds the same draft-write scope and reaches the same publish
    route, so the gate cannot be the route. A bearer client asking to waive the
    floor is refused rather than quietly published without the waiver, because
    a caller told it published cleanly would believe the page is well formed.
    """

    client = make_client(page_service)
    created = _create(client, "waiver-needs-a-person")
    draft = _autosave(client, created, BROKEN, session_version_id=None)
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

    response = client.post(
        f"{_workspace_pages_url(client)}/{created['id']}/versions/{mine['version_id']}/publish",
        json={
            "reason": "editor Save",
            "publish_despite_findings": False,
            "expected_content_hash": mine["content_hash"],
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["server_content_hash"] == other_tab["content_hash"]
    assert response.json()["server_content_hash"] != mine["content_hash"]

    with page_service.session_factory() as session:
        page = session.get(ContextPage, UUID(created["id"]))
        assert page is not None
        assert page.current_published_version_id == UUID(created["version_id"])


def test_publish_page_version_route_requires_expected_content_hash(page_service):
    # PUB-05: expected_content_hash used to default to None and be checked
    # only when present, so a caller could omit it and publish whatever bytes
    # happened to be in the mutable draft row at that instant. The field is
    # now required at the route boundary -- an omitted hash must 422, not
    # silently publish. In-process callers (PageService.publish_draft's own
    # Python signature) are unaffected; only the externally bound REST body
    # changed.
    client = make_client(page_service)
    created = _create(client, "publish-requires-hash")

    # _create publishes immediately (as_draft not passed), so the page
    # already has a published head; the point is that the 422 never even
    # reaches PageService, so that head is untouched by this attempt.
    omitted = client.post(
        f"{_workspace_pages_url(client)}/{created['id']}/versions/{created['version_id']}/publish",
        json={"reason": "editor Save", "publish_despite_findings": False},
    )
    assert omitted.status_code == 422, omitted.text
    assert "expected_content_hash" in omitted.text

    with page_service.session_factory() as session:
        page = session.get(ContextPage, UUID(created["id"]))
        assert page is not None
        assert page.current_published_version_id == UUID(created["version_id"])


def test_publish_page_version_route_reports_stale_context_draft_when_superseded(
    page_service,
):
    # PUB-07 reachability: publishing a page/versions/.../publish target that
    # is no longer the latest linear eligible draft goes through
    # ContextWriteService.publish_draft's assert_context_publish_candidate
    # gate and 409s as stale_context_draft, not as a base-content conflict --
    # this is what the frontend catch (PageEditor.tsx save()) must classify
    # separately from a hash mismatch. An agent draft opens a review, and a
    # browser edit on top of a reviewed draft forks a successor rather than
    # mutating it in place (page_service.py's targeting_reviews fork), which
    # is what supersedes D here.
    client = make_client(page_service)
    created = _create(client, "stale-context-draft-on-publish")
    token = issue_personal_api_token(client, scopes=["mcp:write"])

    agent_draft = _autosave(
        client,
        created,
        "# Page\n\nAgent staged this.\n",
        headers={"Authorization": f"Bearer {token}"},
        session_version_id=None,
    )

    superseding = _autosave(
        client,
        agent_draft,
        "# Page\n\nThe person edits on top of it.\n",
        title="Renamed by the person",
        session_version_id=agent_draft["version_id"],
    )
    assert superseding["version_id"] != agent_draft["version_id"]

    stale = client.post(
        f"{_workspace_pages_url(client)}/{created['id']}/versions/{agent_draft['version_id']}/publish",
        json={
            "reason": "publish the superseded agent draft",
            "publish_despite_findings": False,
            "expected_content_hash": agent_draft["content_hash"],
        },
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["error_code"] == "stale_context_draft"


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


def test_pat_review_cannot_approve_browser_replacement_bytes(page_service):
    """A review stays bound to the immutable bytes the PAT submitted."""

    client = make_client(page_service)
    created = _create(client, "reviewed-bytes-stay-immutable")
    page_id = UUID(created["id"])
    workspace_id = UUID(
        client.get("/api/v1/workspaces").json()["data"]["current_workspace_id"]
    )
    browser_session = client.cookies.get("dtxwiki_session")
    assert browser_session is not None
    token = issue_personal_api_token(client, scopes=["mcp:write"])
    client.cookies.clear()

    reviewed = _autosave(
        client,
        created,
        "# Page\n\nBytes reviewed from the PAT.\n",
        headers={"Authorization": f"Bearer {token}"},
        session_version_id=None,
    )
    reviewed_version_id = UUID(reviewed["version_id"])
    review_id = UUID(reviewed["review_item_id"])

    client.cookies.set("dtxwiki_session", browser_session)
    browser_draft = _autosave(
        client,
        reviewed,
        "# Page\n\nBrowser replacement bytes.\n",
        session_version_id=reviewed["version_id"],
    )
    assert UUID(browser_draft["version_id"]) != reviewed_version_id
    assert browser_draft["superseded_review_count"] == 1

    # The successor is the browser session's one mutable working copy.
    continued = _autosave(
        client,
        browser_draft,
        "# Page\n\nBrowser replacement bytes, continued.\n",
        session_version_id=browser_draft["version_id"],
    )
    assert continued["version_id"] == browser_draft["version_id"]

    with pytest.raises(ReadValidationError, match="already resolved"):
        with page_service.session_factory.begin() as session:
            ReviewService().apply_decision(
                session=session,
                workspace_id=workspace_id,
                review_item_id=review_id,
                request=ReviewDecisionRequest(decision_type="approve"),
                actor_kind="human",
                actor_id=ACTOR_ID,
                actor_role="owner",
                actor_provenance="browser_session",
            )

    with page_service.session_factory() as session:
        page = session.get(ContextPage, page_id)
        reviewed_version = session.get(ContextPageVersion, reviewed_version_id)
        successor = session.get(ContextPageVersion, UUID(continued["version_id"]))
        review = session.get(ReviewItem, review_id)
        assert page is not None and reviewed_version is not None
        assert successor is not None and review is not None
        assert reviewed_version.body_md == "# Page\n\nBytes reviewed from the PAT.\n"
        assert successor.body_md == (
            "# Page\n\nBrowser replacement bytes, continued.\n"
        )
        assert review.status == "superseded"
        assert page.latest_draft_version_id == successor.id
        assert page.current_published_version_id == UUID(created["version_id"])


def test_changes_requested_review_cannot_approve_browser_replacement(page_service):
    """A changes-requested review remains bound to its original bytes."""

    client = make_client(page_service)
    created = _create(client, "changes-requested-bytes-stay-immutable")
    page_id = UUID(created["id"])
    workspace_id = UUID(
        client.get("/api/v1/workspaces").json()["data"]["current_workspace_id"]
    )
    browser_session = client.cookies.get("dtxwiki_session")
    assert browser_session is not None
    token = issue_personal_api_token(client, scopes=["mcp:write"])
    client.cookies.clear()
    reviewed = _autosave(
        client,
        created,
        "# Page\n\nPAT bytes awaiting changes.\n",
        headers={"Authorization": f"Bearer {token}"},
        session_version_id=None,
    )
    reviewed_version_id = UUID(reviewed["version_id"])
    review_id = UUID(reviewed["review_item_id"])

    with page_service.session_factory.begin() as session:
        service = ReviewService()
        service.apply_decision(
            session=session,
            workspace_id=workspace_id,
            review_item_id=review_id,
            request=ReviewDecisionRequest(
                decision_type="request_changes",
                decision_reason="Revise these exact bytes",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
            actor_provenance="browser_session",
        )
        service.claim_review(
            session=session,
            workspace_id=workspace_id,
            review_item_id=review_id,
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
        )

    client.cookies.set("dtxwiki_session", browser_session)
    successor = _autosave(
        client,
        reviewed,
        "# Page\n\nBrowser replacement after requested changes.\n",
        session_version_id=reviewed["version_id"],
    )
    assert UUID(successor["version_id"]) != reviewed_version_id
    assert successor["superseded_review_count"] == 1

    with pytest.raises(ReadValidationError, match="already resolved"):
        with page_service.session_factory.begin() as session:
            ReviewService().apply_decision(
                session=session,
                workspace_id=workspace_id,
                review_item_id=review_id,
                request=ReviewDecisionRequest(decision_type="approve"),
                actor_kind="human",
                actor_id=ACTOR_ID,
                actor_role="owner",
                actor_provenance="browser_session",
            )

    with page_service.session_factory() as session:
        page = session.get(ContextPage, page_id)
        original = session.get(ContextPageVersion, reviewed_version_id)
        review = session.get(ReviewItem, review_id)
        assert page is not None and original is not None and review is not None
        assert original.body_md == "# Page\n\nPAT bytes awaiting changes.\n"
        assert review.status == "superseded"
        assert review.claimed_by is None
        assert review.claimed_at is None
        assert page.current_published_version_id == UUID(created["version_id"])


@pytest.mark.parametrize("review_status", ["pending", "changes_requested"])
def test_same_pat_forks_bytes_targeted_by_unresolved_review(
    page_service,
    review_status,
):
    client = make_client(page_service)
    created = _create(client, f"same-pat-forks-{review_status.replace('_', '-')}")
    page_id = UUID(created["id"])
    workspace_id = UUID(
        client.get("/api/v1/workspaces").json()["data"]["current_workspace_id"]
    )
    token = issue_personal_api_token(client, scopes=["mcp:write"])
    client.cookies.clear()
    auth = {"Authorization": f"Bearer {token}"}

    reviewed = _autosave(
        client,
        created,
        "# Page\n\nExact bytes submitted for review.\n",
        headers=auth,
        session_version_id=None,
    )
    reviewed_version_id = UUID(reviewed["version_id"])
    old_review_id = UUID(reviewed["review_item_id"])
    with page_service.session_factory.begin() as session:
        unrelated_change_set = ChangeSet(
            workspace_id=workspace_id,
            reason="unrelated historical version review",
            trigger_type="test",
            actor_type="human",
            status="draft_created",
            risk_level="low",
        )
        session.add(unrelated_change_set)
        session.flush()
        session.add(
            ChangeOperation(
                workspace_id=workspace_id,
                change_set_id=unrelated_change_set.id,
                target_type="context_page",
                target_id=page_id,
                operation="test",
                diff_ref=f"context_page_version:{created['version_id']}",
            )
        )
        unrelated_review = ReviewItem(
            workspace_id=workspace_id,
            change_set_id=unrelated_change_set.id,
            review_type="context_update",
            status="pending",
            risk_level="low",
            decision_summary="independent historical review",
        )
        session.add(unrelated_review)
        session.flush()
        unrelated_review_id = unrelated_review.id
    if review_status == "changes_requested":
        with page_service.session_factory.begin() as session:
            ReviewService().apply_decision(
                session=session,
                workspace_id=workspace_id,
                review_item_id=old_review_id,
                request=ReviewDecisionRequest(
                    decision_type="request_changes",
                    decision_reason="revise these bytes",
                ),
                actor_kind="human",
                actor_id=ACTOR_ID,
                actor_role="owner",
                actor_provenance="browser_session",
            )

    successor = _autosave(
        client,
        reviewed,
        "# Page\n\nSuccessor bytes from the same PAT.\n",
        headers=auth,
        session_version_id=reviewed["version_id"],
    )

    assert UUID(successor["version_id"]) != reviewed_version_id
    assert UUID(successor["review_item_id"]) != old_review_id
    assert successor["superseded_review_count"] == 1
    with page_service.session_factory() as session:
        original = session.get(ContextPageVersion, reviewed_version_id)
        old_review = session.get(ReviewItem, old_review_id)
        new_review = session.get(ReviewItem, UUID(successor["review_item_id"]))
        unrelated_review = session.get(ReviewItem, unrelated_review_id)
        old_change_set = session.get(ChangeSet, old_review.change_set_id)
        lineage_audit = session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "review.revision_submitted",
                AuditEvent.target_id == old_review_id,
            )
        )
        assert original is not None
        assert original.body_md == "# Page\n\nExact bytes submitted for review.\n"
        assert old_review is not None and old_review.status == "superseded"
        assert old_change_set is not None and old_change_set.status == "superseded"
        assert new_review is not None and new_review.status == "pending"
        assert unrelated_review is not None and unrelated_review.status == "pending"
        assert lineage_audit is not None
        assert lineage_audit.payload["new_review_item_id"] == str(new_review.id)
        assert session.get(ContextPage, page_id).latest_draft_version_id == UUID(
            successor["version_id"]
        )


def test_same_pat_forks_rejected_review_without_rewriting_resolution(page_service):
    client = make_client(page_service)
    created = _create(client, "same-pat-forks-rejected")
    workspace_id = UUID(
        client.get("/api/v1/workspaces").json()["data"]["current_workspace_id"]
    )
    token = issue_personal_api_token(client, scopes=["mcp:write"])
    client.cookies.clear()
    auth = {"Authorization": f"Bearer {token}"}
    reviewed = _autosave(
        client,
        created,
        "# Page\n\nRejected exact bytes.\n",
        headers=auth,
        session_version_id=None,
    )
    reviewed_version_id = UUID(reviewed["version_id"])
    rejected_review_id = UUID(reviewed["review_item_id"])
    with page_service.session_factory.begin() as session:
        ReviewService().apply_decision(
            session=session,
            workspace_id=workspace_id,
            review_item_id=rejected_review_id,
            request=ReviewDecisionRequest(
                decision_type="reject",
                decision_reason="keep this resolution on the exact bytes",
            ),
            actor_kind="human",
            actor_id=ACTOR_ID,
            actor_role="owner",
            actor_provenance="browser_session",
        )

    successor = _autosave(
        client,
        reviewed,
        "# Page\n\nNew PAT bytes after rejection.\n",
        headers=auth,
        session_version_id=reviewed["version_id"],
    )

    assert UUID(successor["version_id"]) != reviewed_version_id
    assert successor["superseded_review_count"] == 0
    with page_service.session_factory() as session:
        original = session.get(ContextPageVersion, reviewed_version_id)
        rejected_review = session.get(ReviewItem, rejected_review_id)
        new_review = session.get(ReviewItem, UUID(successor["review_item_id"]))
        rejected_change_set = session.get(ChangeSet, rejected_review.change_set_id)
        assert original is not None
        assert original.body_md == "# Page\n\nRejected exact bytes.\n"
        assert rejected_review is not None and rejected_review.status == "rejected"
        assert rejected_change_set is not None
        assert rejected_change_set.status == "rejected"
        assert new_review is not None and new_review.status == "pending"

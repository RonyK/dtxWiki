from unittest.mock import Mock
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.context import ContextPage
from backend.models.tree import WorkspaceTreeNode
from backend.models.user import User
from backend.policy.rbac import grant_workspace_membership
from backend.schemas.context_write import WriteDraftRequest
from backend.services.context_write_service import ContextWriteService
from backend.services.domain_read import DomainReadService
from backend.services.tree_read import TreeReadService
from backend.tests.conftest import authenticate_test_client


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
    authenticate_test_client(client)
    return client


def _workspace_id(client: TestClient) -> str:
    return client.get("/api/v1/workspaces").json()["data"]["current_workspace_id"]


def test_context_api_creates_updates_and_publishes_draft(page_service, monkeypatch):
    # P9.1: the old `/{workspace_id}/context-pages*` route family (title-
    # upsert `ContextWriteService.write_draft`/`update_draft`/`publish_draft`)
    # is deleted entirely -- REST callers now use the single canonical
    # `context/pages` P8 state-machine route family
    # (`backend/api/context_pages.py`), which never upserts by title and
    # preserves browser-session direct publication while AI and bearer
    # writers retain the explicit review lifecycle. This retargets the same
    # browser narrative (create, update, conflict-on-stale-base, publish)
    # onto that route family.
    client = make_client(page_service)
    workspace_id = _workspace_id(client)
    pages_url = f"/api/v1/workspaces/{workspace_id}/context/pages"

    created = client.post(
        pages_url,
        json={"title": "API Context Draft", "page_type": "overview", "body_md": ""},
    )
    assert created.status_code == 201
    draft = created.json()["data"]

    updated = client.post(
        f"{pages_url}/{draft['id']}/autosave",
        json={
            "idempotency_key": "context-api-append",
            "title": "API Context Draft",
            "page_type": "overview",
            "body_md": (
                "# API Context Draft\n\nOriginal body.\n\n## Added\n\nUpdated body."
            ),
            "base_version_id": draft["version_id"],
            "base_content_hash": draft["content_hash"],
            "expected_editing_revision": draft["editing_revision"],
        },
    )
    assert updated.status_code == 200
    updated_data = updated.json()["data"]
    updated_id = updated_data["version_id"]
    updated_hash = updated_data["content_hash"]

    stale = client.post(
        f"{pages_url}/{draft['id']}/autosave",
        json={
            "idempotency_key": "context-api-stale",
            "title": "API Context Draft",
            "body_md": "# Stale",
            "base_version_id": draft["version_id"],
            "base_content_hash": draft["content_hash"],
            "expected_editing_revision": draft["editing_revision"],
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error_code"] == "stale_base_version"

    # App assembly already bound its writer. Transport services must retain
    # that explicit dependency even without the legacy session.info bridge.
    monkeypatch.delitem(
        page_service.session_factory.kw["info"], "context_version_writer"
    )
    published = client.post(
        f"{pages_url}/{draft['id']}/versions/{updated_id}/publish",
        json={
            "reason": "publish browser-authored context draft",
            "expected_content_hash": updated_hash,
            "expected_references_hash": updated_data["references_hash"],
        },
    )
    assert published.status_code == 200
    data = published.json()["data"]
    assert data["is_published_head"] is True
    assert data["current_published_version_id"] == updated_id
    assert data["latest_draft_version_id"] is None
    assert "## Added" in data["body_md"]


def test_context_inspector_defers_work_and_checks_the_displayed_version(
    page_service, monkeypatch
):
    from backend.services import document_references as references_module
    from backend.services import domain_read as read_module

    owner = make_client(page_service)
    workspace_id = _workspace_id(owner)
    pages_url = f"/api/v1/workspaces/{workspace_id}/context/pages"
    created = owner.post(
        pages_url,
        json={"title": "Lazy inspector", "body_md": "# Title\n\nClean published body."},
    ).json()["data"]
    published_response = owner.post(
        f"{pages_url}/{created['id']}/versions/{created['version_id']}/publish",
        json={
            "reason": "publish inspector baseline",
            "expected_content_hash": created["content_hash"],
            "expected_references_hash": created["references_hash"],
        },
    )
    assert published_response.status_code == 200, published_response.text
    published = published_response.json()["data"]
    saved = owner.post(
        f"{pages_url}/{created['id']}/autosave",
        json={
            "idempotency_key": "lazy-inspector-save",
            "body_md": "# Title\n\n### Draft heading skips a level\n\nDraft body.",
            "base_version_id": published["version_id"],
            "base_content_hash": published["content_hash"],
            "expected_editing_revision": published["editing_revision"],
        },
    ).json()["data"]
    submitted_response = owner.post(
        f"{pages_url}/{created['id']}/submit-for-review",
        json={
            "idempotency_key": "lazy-inspector-submit",
            "version_id": saved["version_id"],
            "expected_editing_revision": saved["editing_revision"],
        },
    )
    assert submitted_response.status_code == 200, submitted_response.text
    submitted = submitted_response.json()["data"]

    real_lint = read_module.page_format_warnings_for_body
    linted_versions = []
    counts = []
    allow_lint = False

    def track_lint(*args, **kwargs):
        assert allow_lint, "default detail must not run inspector lint"
        linted_versions.append(kwargs["version"].id)
        return real_lint(*args, **kwargs)

    def count_backlinks(self, *args):
        counts.append("backlinks")
        return 3

    def count_children(self, **kwargs):
        counts.append("children")
        return 2

    reference_read = Mock(wraps=references_module.read_document_references)
    reference_hash = Mock(wraps=references_module.document_references_hash)
    monkeypatch.setattr(references_module, "read_document_references", reference_read)
    monkeypatch.setattr(references_module, "document_references_hash", reference_hash)
    monkeypatch.setattr(read_module, "page_format_warnings_for_body", track_lint)
    monkeypatch.setattr(
        DomainReadService, "_context_page_backlink_count", count_backlinks
    )
    monkeypatch.setattr(TreeReadService, "children_count", count_children)
    detail_url = f"{pages_url}/{created['id']}"
    detail_response = owner.get(
        detail_url, params={"version_id": published["version_id"]}
    )
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()["data"]
    assert detail["warnings"] == detail["pending_draft_warnings"] == []
    assert detail["pending_review"]["review_item_id"] == submitted["review_item_id"]
    assert detail["pending_draft_version_id"] == submitted["version_id"]
    assert detail["backlink_count"] is detail["children_count"] is None
    assert counts == linted_versions == []
    reference_read.assert_called_once()
    reference_hash.assert_called_once()

    allow_lint = True
    clean = owner.get(
        f"{detail_url}/inspector",
        params={"section": "summary", "version_id": published["version_id"]},
    )
    assert clean.status_code == 200, clean.text
    assert clean.json()["data"]["warnings"] == []
    draft = owner.get(
        f"{detail_url}/inspector",
        params={"section": "summary", "version_id": submitted["version_id"]},
    )
    assert draft.status_code == 200, draft.text
    assert any(
        "heading" in warning.lower() for warning in draft.json()["data"]["warnings"]
    )
    assert linted_versions == [
        UUID(published["version_id"]),
        UUID(submitted["version_id"]),
    ]
    assert draft.json()["data"]["backlink_count"] is None
    assert draft.json()["data"]["children_count"] is None
    assert counts == []

    allow_lint = False
    backlinks = owner.get(
        f"{detail_url}/inspector",
        params={"section": "backlinks", "version_id": published["version_id"]},
    )
    assert backlinks.status_code == 200, backlinks.text
    assert backlinks.json()["data"] == {
        "page_id": created["id"],
        "version_id": published["version_id"],
        "section": "backlinks",
        "warnings": [],
        "backlink_count": 3,
        "children_count": 2,
    }
    assert counts == ["backlinks", "children"]

    viewer = TestClient(owner.app)
    authenticate_test_client(viewer, email="lazy-inspector-viewer@example.com")
    with page_service.session_factory.begin() as session:
        user = session.scalar(
            select(User).where(User.email == "lazy-inspector-viewer@example.com")
        )
        grant_workspace_membership(
            session, user_id=user.id, workspace_id=UUID(workspace_id), role="viewer"
        )
        other_workspace_id = user.default_workspace_id
    hidden = viewer.get(
        f"{detail_url}/inspector",
        params={"section": "summary", "version_id": submitted["version_id"]},
    )
    assert hidden.status_code == 404, hidden.text
    wrong_workspace = viewer.get(
        f"/api/v1/workspaces/{other_workspace_id}/context/pages/{created['id']}/inspector",
        params={"section": "backlinks", "version_id": published["version_id"]},
    )
    assert wrong_workspace.status_code == 404, wrong_workspace.text
    assert counts == ["backlinks", "children"]


def test_workspace_publish_route_requires_expected_content_hash(page_service):
    # PUB-05: the workspace context-page publish route could not even
    # express expected_content_hash before this fix (the field did not exist
    # on PublishContextPageRequest at all), so this route was a total blind
    # spot for the lost-update it protects against. It is required at the
    # route boundary now -- an omitted hash must 422, not silently publish
    # whatever bytes are currently in the draft row.
    client = make_client(page_service)
    workspace_id = _workspace_id(client)
    pages_url = f"/api/v1/workspaces/{workspace_id}/context/pages"
    created = client.post(
        pages_url,
        json={
            "title": "Publish Requires Hash",
            "body_md": "# Publish Requires Hash\n\nOriginal body.",
        },
    ).json()["data"]
    submitted = client.post(
        f"{pages_url}/{created['id']}/submit-for-review",
        json={
            "idempotency_key": "publish-requires-hash-submit",
            "version_id": created["version_id"],
            "expected_editing_revision": created["editing_revision"],
        },
    ).json()["data"]
    if submitted.get("review_item_id"):
        client.post(
            f"/api/v1/workspaces/{workspace_id}/reviews/{submitted['review_item_id']}"
            "/decision",
            json={"decision_type": "approve"},
        )

    omitted = client.post(
        f"{pages_url}/{created['id']}/versions/{created['version_id']}/publish",
        json={"reason": "publish without a hash"},
    )
    assert omitted.status_code == 422, omitted.text
    assert "expected_content_hash" in omitted.text


def test_workspace_publish_route_rejects_a_stale_hash_and_reports_the_current_one(
    page_service,
):
    # Companion to the omission test: a hash that names bytes the row no
    # longer holds must 409 with the server's current hash in the body, using
    # the same server_version_id/server_content_hash keys the page routes
    # already emit for PageConflictError -- this route used to have no way to
    # report that at all.
    client = make_client(page_service)
    workspace_id = _workspace_id(client)
    pages_url = f"/api/v1/workspaces/{workspace_id}/context/pages"
    created = client.post(
        pages_url,
        json={
            "title": "Stale Hash Reports Current",
            "body_md": "# Stale Hash Reports Current\n\nOriginal body.",
        },
    ).json()["data"]
    original_hash = created["content_hash"]

    revised = client.post(
        f"{pages_url}/{created['id']}/autosave",
        json={
            "idempotency_key": "stale-hash-revise",
            "title": "Stale Hash Reports Current",
            "body_md": "# Stale Hash Reports Current\n\nRevised body.",
            "base_version_id": created["version_id"],
            "base_content_hash": original_hash,
            "expected_editing_revision": created["editing_revision"],
        },
    ).json()["data"]
    revised_hash = revised["content_hash"]
    assert revised_hash != original_hash

    submitted = client.post(
        f"{pages_url}/{created['id']}/submit-for-review",
        json={
            "idempotency_key": "stale-hash-submit",
            "version_id": revised["version_id"],
            "expected_editing_revision": revised["editing_revision"],
        },
    ).json()["data"]
    if submitted.get("review_item_id"):
        client.post(
            f"/api/v1/workspaces/{workspace_id}/reviews/{submitted['review_item_id']}"
            "/decision",
            json={"decision_type": "approve"},
        )

    stale = client.post(
        f"{pages_url}/{created['id']}/versions/{revised['version_id']}/publish",
        json={
            "reason": "publish with a stale hash",
            "expected_content_hash": original_hash,
        },
    )
    assert stale.status_code == 409, stale.text
    body = stale.json()
    assert body["error_code"] == "stale_base_version"
    assert body["server_version_id"] == revised["version_id"]
    assert body["server_content_hash"] == revised_hash


def test_context_api_create_never_upserts_by_title(page_service):
    # P9.1: the old `/context-pages` route's title-upsert behavior (a second
    # POST with the same title redrafted the SAME page, 200 not 201) is
    # deliberately gone -- `ContextPageCreateRequest`'s own docstring says a
    # title is never a lookup key for page-first creation, so two creates
    # with the same title now make two DISTINCT pages, both 201.
    client = make_client(page_service)
    workspace_id = _workspace_id(client)
    pages_url = f"/api/v1/workspaces/{workspace_id}/context/pages"

    created = client.post(
        pages_url,
        json={"title": "Reused Context Title", "body_md": "# First draft."},
    )
    assert created.status_code == 201

    again = client.post(
        pages_url,
        json={"title": "Reused Context Title", "body_md": "# Second draft."},
    )
    assert again.status_code == 201
    assert again.json()["data"]["id"] != created.json()["data"]["id"]


def test_context_api_refuses_a_viewer_before_it_resolves_the_name(page_service):
    """RBAC, not the ambiguity response, is what protects this route.

    The disambiguation payload names every page a bare title matched, so it
    could in principle describe drafts the caller cannot read. On REST it
    cannot: the write route requires editor, and any actor who clears that can
    already read every draft in the workspace. A viewer is refused before the
    name is ever resolved. The vector that does exist is MCP, where role and
    scope are separate -- covered by the sibling test in test_context_name_ownership.

    P9.1: the canonical `context/pages` create route (`create_page_first`)
    never resolves a title to existing candidates at all -- there is no
    disambiguation payload left to leak in the first place. The pages
    seeded below via `ContextWriteService.write_draft` directly (still a
    live, unaffected code path -- MCP's `write_draft` tool) stand in for
    "other content in the workspace a viewer must not learn about from an
    unrelated 403", the narrower property this test still meaningfully
    checks on the new route.
    """
    owner = make_client(page_service)
    workspace_id = UUID(_workspace_id(owner))
    viewer = TestClient(owner.app)
    authenticate_test_client(viewer, email="ambiguity-viewer@example.com")

    with page_service.session_factory.begin() as session:
        viewer_user = session.scalar(
            select(User).where(User.email == "ambiguity-viewer@example.com")
        )
        assert viewer_user is not None
        grant_workspace_membership(
            session,
            user_id=viewer_user.id,
            workspace_id=workspace_id,
            role="viewer",
        )
        placement_parent_node_id = session.scalar(
            select(WorkspaceTreeNode.id).where(
                WorkspaceTreeNode.workspace_id == workspace_id,
                WorkspaceTreeNode.slug == "context",
                WorkspaceTreeNode.node_kind == "folder",
            )
        )
        assert placement_parent_node_id is not None
        request = WriteDraftRequest(
            title="Hidden REST Plan",
            page_type="overview",
            body_markdown="# Hidden REST Plan\n\nEditor-only draft.",
            reason="seed hidden ambiguity",
            placement_parent_node_id=placement_parent_node_id,
        )
        service = ContextWriteService()
        first = service.write_draft(
            session,
            workspace_id,
            request,
            "human",
            "00000000-0000-0000-0000-000000000010",
            actor_role="editor",
        )
        second = service.write_draft(
            session,
            workspace_id,
            request,
            "human",
            "00000000-0000-0000-0000-000000000010",
            actor_role="editor",
            force_new_page=True,
        )
        hidden_pages = [
            session.get(ContextPage, page_id)
            for page_id in (first.page_id, second.page_id)
        ]
        hidden_ids = {page.id for page in hidden_pages}
        hidden_slugs = {page.slug for page in hidden_pages}
        before_count = session.scalar(select(func.count()).select_from(ContextPage))

    response = viewer.post(
        f"/api/v1/workspaces/{workspace_id}/context/pages",
        json={
            "title": "Hidden REST Plan",
            "page_type": "overview",
            "body_md": "# Hidden REST Plan\n\nViewer probe.",
        },
    )

    assert response.status_code == 403
    payload = response.json()
    assert payload["error_code"] == "workspace_role_insufficient"
    assert "candidates" not in payload
    serialized = response.text
    assert "page_id" not in serialized
    assert "slug" not in serialized
    assert "title" not in serialized
    assert "placement_parent_node_id" not in serialized
    for page_id in hidden_ids:
        assert str(page_id) not in serialized
    for slug in hidden_slugs:
        assert slug not in serialized
    assert "Hidden REST Plan" not in serialized
    assert str(placement_parent_node_id) not in serialized
    with page_service.session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(ContextPage))
            == before_count
        )


def test_context_api_feedback_and_review_decision(page_service):
    client = make_client(page_service)
    workspace_id = _workspace_id(client)
    pages_url = f"/api/v1/workspaces/{workspace_id}/context/pages"

    created = client.post(
        pages_url,
        json={
            "title": "API Review Target",
            "page_type": "guide",
            "body_md": "# API Review Target\n\nReview body.",
        },
    ).json()["data"]
    # P9.1: page-first creation never opens a review on its own -- submit
    # for review explicitly so a pending ReviewItem exists for the
    # `reviews?status=pending` lookup below (the old `write_draft` route
    # always opened one as part of creating).
    submitted = client.post(
        f"{pages_url}/{created['id']}/submit-for-review",
        json={
            "idempotency_key": "feedback-review-submit",
            "version_id": created["version_id"],
            "expected_editing_revision": created["editing_revision"],
        },
    )
    assert submitted.status_code == 200, submitted.text
    draft = created

    feedback = client.post(
        f"/api/v1/workspaces/{workspace_id}/feedback",
        json={
            "target_type": "context_page",
            "target_id": draft["id"],
            "body": "Needs a citation.",
            "severity": "major",
        },
    )
    assert feedback.status_code == 200
    assert feedback.json()["data"]["status"] == "open"

    listed_feedback = client.get(
        f"/api/v1/workspaces/{workspace_id}/feedback?target_type=context_page",
    )
    assert listed_feedback.status_code == 200
    assert listed_feedback.json()["data"][0]["body"] == "Needs a citation."

    reviews = client.get(
        f"/api/v1/workspaces/{workspace_id}/reviews?status=pending",
    )
    assert reviews.status_code == 200
    review_id = reviews.json()["data"]["items"][0]["id"]

    decided = client.post(
        f"/api/v1/workspaces/{workspace_id}/reviews/{review_id}/decision",
        json={
            "decision_type": "request_changes",
            "decision_reason": "Need stronger source evidence.",
            "audience_kind": "workspace_editors",
        },
    )
    assert decided.status_code == 200
    assert decided.json()["data"]["status"] == "changes_requested"
    assert decided.json()["data"]["decisions"][0]["decision_type"] == (
        "request_changes"
    )
    assert decided.json()["data"]["decisions"][0]["audience_kind"] == (
        "workspace_editors"
    )
    assert decided.json()["data"]["current_audience_kind"] == "workspace_editors"


def test_write_draft_savepoint_rolls_back_partial_writes_on_error(page_service):
    """B-P1-4 Sprint_6 Fix_4: when an exception is raised mid-write inside
    write_draft, the SAVEPOINT (session.begin_nested) wrapping the method
    must roll back every partial INSERT it produced. After the failed
    call, the outer session must see neither the context_page row nor any
    ContextPageVersion / ChangeSet / ChangeOperation orphans.
    """
    from sqlalchemy import select

    from backend.models.change import ChangeOperation, ChangeSet
    from backend.models.context import ContextPage, ContextPageVersion
    from backend.models.workspace import Workspace
    from backend.schemas.context_write import WriteDraftRequest
    from backend.services.audit_service import AuditService
    from backend.services.context_write_service import ContextWriteService

    class _ExplodingAuditService(AuditService):
        def emit(self, **kwargs):
            raise RuntimeError("simulated audit failure mid-write")

    service = ContextWriteService(audit_service=_ExplodingAuditService())

    with page_service.session_factory.begin() as session:
        workspace = session.query(Workspace).filter_by(slug="default").one()
        workspace_id = workspace.id

    request = WriteDraftRequest(
        title="Savepoint Rollback Target",
        page_type="overview",
        body_markdown="# Savepoint Rollback Target\n\norphan body",
        reason="trigger mid-write rollback",
        citations=[],
        risk_notes=None,
        placement_parent_node_id=None,
    )

    with page_service.session_factory.begin() as session:
        try:
            service.write_draft(
                session=session,
                workspace_id=workspace_id,
                request=request,
                actor_kind="human",
                actor_id="00000000-0000-0000-0000-000000000001",
                actor_role="owner",
            )
        except RuntimeError as exc:
            assert "simulated audit failure" in str(exc)
        else:  # pragma: no cover - defensive
            raise AssertionError(
                "write_draft must propagate the simulated audit failure"
            )

    # After commit, no orphan rows for the target slug should exist.
    with page_service.session_factory() as session:
        page_row = session.scalar(
            select(ContextPage).where(
                ContextPage.slug == "savepoint-rollback-target",
                ContextPage.workspace_id == workspace_id,
            )
        )
        assert page_row is None, "context_page row leaked despite savepoint rollback"
        orphan_version = session.scalar(
            select(ContextPageVersion).where(
                ContextPageVersion.body_md.contains("orphan body")
            )
        )
        assert orphan_version is None, (
            "context_page_version row leaked despite savepoint rollback"
        )
        orphan_change_set = session.scalar(
            select(ChangeSet).where(
                ChangeSet.reason == "trigger mid-write rollback",
            )
        )
        assert orphan_change_set is None, (
            "change_set row leaked despite savepoint rollback"
        )
        orphan_change_op = session.scalar(
            select(ChangeOperation).where(
                ChangeOperation.diff_summary == "trigger mid-write rollback",
            )
        )
        assert orphan_change_op is None, (
            "change_operation row leaked despite savepoint rollback"
        )

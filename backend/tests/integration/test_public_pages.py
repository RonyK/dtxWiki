"""T16 public publishing — unauthenticated read + publish gate (security)."""

from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.tree import WorkspaceTreeNode
from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.services.page_service import PagePublicExposureForbiddenError
from backend.services.public_page_exposure import (
    clear_public_page_exposure,
    grant_public_page_exposure,
)
from backend.services.tree_service import TreeService
from backend.tests.conftest import authenticate_test_client

PUBLIC_FIELDS = {
    "id",
    "slug",
    "title",
    "body_md",
    "updated_at",
    "exposure_scope",
}
# Internal fields that must NEVER appear in the public payload.
LEAKED_FIELDS = {
    "current_version_id",
    "version_id",
    "parent_version_id",
    "content_hash",
    "change_set_id",
    "review_item_id",
    "alternative_draft_id",
    "tags",
    "aliases",
    "page_type",
    "created_at",
}


def _authed_client(page_service) -> TestClient:
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
    with page_service.session_factory.begin() as session:
        user = session.scalar(select(User).where(User.email == "test@example.com"))
        workspace = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        )
        assert user is not None and workspace is not None
        workspace.visibility = "public"
        session.add(
            WorkspaceMembership(
                user_id=user.id,
                workspace_id=workspace.id,
                role="owner",
            )
        )
    return client


def _anon_client(page_service, *, static_root=None) -> TestClient:
    # No authenticate_test_client → no session cookie → anonymous caller.
    return TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
            ),
            page_service=page_service,
            static_root=static_root,
        )
    )


def _make_page(page_service, slug: str, *, published: bool, public: bool):
    """Create a page; optionally give it a published head + is_public flag."""
    created = page_service.write_page(
        workspace_slug="default",
        slug=slug,
        title=slug.replace("-", " ").title(),
        body_md=f"# {slug}\n\nPublished body for {slug}.",
        actor_kind="human",
        actor_id="dev-local-user",
        actor_provenance="browser_session",
    )
    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, created.id)
        # write_page (legacy) auto-publishes via set_current_version; force the
        # draft-only state by clearing the published head when published=False.
        page.current_published_version_id = (
            created.version_id if published else None
        )
        workspace = session.get(Workspace, page.workspace_id)
        if public and published:
            workspace.visibility = "public"
            grant_public_page_exposure(
                page,
                scope="indexed_web",
                version_id=created.version_id,
            )
        else:
            clear_public_page_exposure(page)
    return created


# --------------------------------------------------------------------------
# Public read (UNAUTHENTICATED).
# --------------------------------------------------------------------------


def test_public_read_returns_published_head_without_auth(page_service):
    created = _make_page(page_service, "public-intro", published=True, public=True)
    anon = _anon_client(page_service)

    resp = anon.get("/api/v1/public/w/default/pages/public-intro")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert set(data.keys()) == PUBLIC_FIELDS
    assert data["id"] == str(created.id)
    assert data["slug"] == "public-intro"
    assert data["exposure_scope"] == "indexed_web"
    assert "Published body for public-intro." in data["body_md"]
    # No internal metadata leaks.
    for field in LEAKED_FIELDS:
        assert field not in data
    # Indexed public Workspace pages remain discoverable.
    assert "X-Robots-Tag" not in resp.headers
    assert "Cache-Control" not in resp.headers


def test_public_read_404_when_page_archived(page_service):
    # Keep the explicit archived read gate as defense against legacy or otherwise
    # inconsistent rows where is_public and the published head remain set.
    created = _make_page(page_service, "public-archived", published=True, public=True)
    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, created.id)
        page.lifecycle_status = "archived"
    anon = _anon_client(page_service)

    resp = anon.get("/api/v1/public/w/default/pages/public-archived")

    assert resp.status_code == 404


def test_tree_archive_revokes_public_exposure_and_unarchive_keeps_it_private(
    page_service,
):
    created = _make_page(
        page_service, "public-tree-archived", published=True, public=True
    )
    tree = TreeService()
    with page_service.session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        node = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.target_kind == "context_page",
                WorkspaceTreeNode.target_id == created.id,
                WorkspaceTreeNode.primary_placement.is_(True),
            )
        )
        tree.archive_node(
            session=session,
            workspace_id=workspace_id,
            node_id=node.id,
            actor_kind="human",
            actor_id="dev-local-user",
        )

    with page_service.session_factory() as session:
        page = session.get(ContextPage, created.id)
        assert page.lifecycle_status == "archived"
        assert page.is_public is False
        assert page.public_exposure_scope is None
        assert page.public_exposed_version_id is None
        assert page.current_published_version_id == created.version_id

    anon = _anon_client(page_service)
    assert (
        anon.get("/api/v1/public/w/default/pages/public-tree-archived").status_code
        == 404
    )

    page_service.write_page(
        workspace_slug="default",
        slug="public-tree-archived",
        title="Public Tree Archived",
        body_md="# Public Tree Archived\n\nRevived body.",
        actor_kind="human",
        actor_id="dev-local-user",
        base_version_id=created.version_id,
        base_content_hash=created.content_hash,
        actor_provenance="browser_session",
    )

    with page_service.session_factory() as session:
        page = session.get(ContextPage, created.id)
        assert page.lifecycle_status == "published"
        assert page.is_public is False
    assert (
        anon.get("/api/v1/public/w/default/pages/public-tree-archived").status_code
        == 404
    )


def test_public_read_private_draft_only_and_missing_all_404_identically(page_service):
    # public=False (private), public-but-no-published-head (draft-only), and a
    # never-existed slug must all return the IDENTICAL 404 body — no leak, no 403.
    _make_page(page_service, "private-page", published=True, public=False)
    _make_page(page_service, "draft-only-page", published=False, public=True)
    anon = _anon_client(page_service)

    private = anon.get("/api/v1/public/w/default/pages/private-page")
    draft_only = anon.get("/api/v1/public/w/default/pages/draft-only-page")
    missing = anon.get("/api/v1/public/w/default/pages/does-not-exist")
    missing_ws = anon.get("/api/v1/public/w/no-such-ws/pages/whatever")

    responses = (private, draft_only, missing, missing_ws)
    for resp in responses:
        assert resp.status_code == 404
        assert resp.json()["error_code"] == "not_found"
    # The security-relevant fields are identical across all four (no case
    # distinction): same error_code + message. (The envelope request_id is a
    # per-request random UUID, so byte-identity is not the right invariant.)
    distinguishing = {
        (resp.json()["error_code"], resp.json()["message"]) for resp in responses
    }
    assert distinguishing == {("not_found", "page not found")}
    # None of the bodies echoes a page/workspace identifier that would leak
    # which of {private, draft-only, missing, missing-workspace} it was.
    for resp in responses:
        body = resp.text
        for leak in ("private-page", "draft-only-page", "default", "Private Page"):
            assert leak not in body


def test_public_read_ignores_client_version_param(page_service):
    # A client cannot coax a different (e.g. draft) version out of the public
    # endpoint — extra query params are ignored; it always serves the head.
    _make_page(page_service, "version-locked", published=True, public=True)
    anon = _anon_client(page_service)

    resp = anon.get(
        "/api/v1/public/w/default/pages/version-locked",
        params={"version_id": str(UUID(int=0)), "version": "draft"},
    )
    assert resp.status_code == 200
    assert "Published body for version-locked." in resp.json()["data"]["body_md"]


def test_public_read_works_with_no_authorization_header(page_service):
    # Explicitly assert structural RBAC isolation: a public page resolves with
    # NO Authorization header and NO session cookie.
    _make_page(page_service, "no-auth-needed", published=True, public=True)
    anon = _anon_client(page_service)
    resp = anon.get("/api/v1/public/w/default/pages/no-auth-needed")
    assert resp.status_code == 200


# --------------------------------------------------------------------------
# Publish / unpublish (authed, editor+).
# --------------------------------------------------------------------------


def test_publish_requires_editor_role(page_service):
    from backend.models.user import User
    from backend.models.workspace import Workspace
    from backend.policy.rbac import grant_workspace_membership

    owner = _authed_client(page_service)
    created = _make_page(page_service, "gate-page", published=True, public=False)

    viewer = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client(viewer, email="viewer@example.com")
    with page_service.session_factory.begin() as session:
        user = session.scalar(
            select(User).where(User.email == "viewer@example.com")
        )
        workspace = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        )
        grant_workspace_membership(
            session, user_id=user.id, workspace_id=workspace.id, role="viewer"
        )

    denied = viewer.post(
        f"/api/v1/workspaces/{created.workspace_id}/context/pages"
        f"/{created.id}/publish",
        json={"is_public": True},
    )
    assert denied.status_code == 403

    editor = TestClient(owner.app)
    authenticate_test_client(editor, email="editor@example.com")
    with page_service.session_factory.begin() as session:
        editor_user = session.scalar(
            select(User).where(User.email == "editor@example.com")
        )
        workspace = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        )
        grant_workspace_membership(
            session,
            user_id=editor_user.id,
            workspace_id=workspace.id,
            role="editor",
        )

    allowed = editor.post(
        f"/api/v1/workspaces/{created.workspace_id}/context/pages"
        f"/{created.id}/publish",
        json={"is_public": True},
    )
    assert allowed.status_code == 200
    # Now anonymously readable.
    anon = _anon_client(page_service)
    assert anon.get("/api/v1/public/w/default/pages/gate-page").status_code == 200


def test_publish_without_published_head_returns_version_validation(page_service):
    client = _authed_client(page_service)
    created = _make_page(page_service, "no-head-page", published=False, public=False)

    resp = client.post(
        f"/api/v1/workspaces/{created.workspace_id}/context/pages"
        f"/{created.id}/publish",
        json={"is_public": True},
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "page_has_no_published_version"


def test_public_exposure_cutover_conflict_is_409_and_rolls_back(page_service):
    client = _authed_client(page_service)
    created = _make_page(
        page_service,
        "cutover-public-exposure-api",
        published=True,
        public=False,
    )
    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, created.id)
        assert page is not None
        version = session.get(ContextPageVersion, created.version_id)
        assert version is not None
        digest = "a" * 64
        version.body_md = (
            f"# Cutover public exposure\n\n"
            f"![legacy](/api/v1/uploads/{page.workspace_id}/{digest}.png)"
        )

    response = client.post(
        f"/api/v1/workspaces/{created.workspace_id}/context/pages/{created.id}/publish",
        json={"is_public": True},
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "legacy_media_cutover_conflict"
    with page_service.session_factory() as session:
        page = session.get(ContextPage, created.id)
        assert page is not None
        assert page.is_public is False
        assert page.public_exposure_scope is None
        assert page.public_exposed_version_id is None


def test_private_workspace_share_requires_acknowledgement_bound_to_version(
    page_service,
):
    client = _authed_client(page_service)
    created = _make_page(
        page_service, "private-unlisted-page", published=True, public=False
    )
    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, created.id)
        assert page is not None
        workspace = session.get(Workspace, page.workspace_id)
        assert workspace is not None
        workspace.visibility = "private"
        published_version_id = page.current_published_version_id
    assert published_version_id is not None

    missing_ack = client.post(
        f"/api/v1/workspaces/{created.workspace_id}/context/pages/{created.id}/publish",
        json={
            "is_public": True,
            "expected_published_version_id": str(published_version_id),
        },
    )
    assert missing_ack.status_code == 422
    assert (
        missing_ack.json()["error_code"]
        == "public_exposure_acknowledgement_required"
    )

    stale = client.post(
        f"/api/v1/workspaces/{created.workspace_id}/context/pages/{created.id}/publish",
        json={
            "is_public": True,
            "acknowledge_public_exposure": True,
            "expected_published_version_id": "00000000-0000-0000-0000-000000000001",
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error_code"] == "public_share_version_changed"

    missing_version = client.post(
        f"/api/v1/workspaces/{created.workspace_id}/context/pages/{created.id}/publish",
        json={
            "is_public": True,
            "acknowledge_public_exposure": True,
        },
    )
    assert missing_version.status_code == 409
    assert missing_version.json()["error_code"] == "public_share_version_changed"

    shared = client.post(
        f"/api/v1/workspaces/{created.workspace_id}/context/pages/{created.id}/publish",
        json={
            "is_public": True,
            "acknowledge_public_exposure": True,
            "expected_published_version_id": str(published_version_id),
        },
    )
    assert shared.status_code == 200
    assert shared.json()["data"]["is_public"] is True

    with page_service.session_factory() as session:
        page = session.get(ContextPage, created.id)
        assert page.public_exposure_scope == "unlisted_web"
        assert page.public_exposed_version_id == published_version_id

    anonymous = _anon_client(page_service)
    public_read = anonymous.get(
        "/api/v1/public/w/default/pages/private-unlisted-page"
    )
    assert public_read.status_code == 200
    assert public_read.json()["data"]["exposure_scope"] == "unlisted_web"
    assert public_read.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert public_read.headers["Cache-Control"] == "no-store"


def test_nonpublic_share_requires_owner_browser_provenance(page_service):
    created = _make_page(
        page_service, "private-owner-browser", published=True, public=False
    )
    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, created.id)
        workspace = session.get(Workspace, page.workspace_id)
        workspace.visibility = "private"
        version_id = page.current_published_version_id
    assert version_id is not None

    for role, provenance in (
        ("editor", "browser_session"),
        ("owner", "oauth_access_token"),
    ):
        with pytest.raises(PagePublicExposureForbiddenError):
            page_service.set_page_public(
                created.id,
                True,
                actor_kind="human",
                actor_id="dev-local-user",
                actor_role=role,
                actor_provenance=provenance,
                acknowledge_public_exposure=True,
                expected_published_version_id=version_id,
            )


def test_unpublish_revokes_public_access(page_service):
    client = _authed_client(page_service)
    created = _make_page(page_service, "revoke-page", published=True, public=True)
    anon = _anon_client(page_service)
    assert anon.get("/api/v1/public/w/default/pages/revoke-page").status_code == 200

    unpublish = client.post(
        f"/api/v1/workspaces/{created.workspace_id}/context/pages"
        f"/{created.id}/publish",
        json={"is_public": False},
    )
    assert unpublish.status_code == 200
    with page_service.session_factory() as session:
        page = session.get(ContextPage, created.id)
        assert page.is_public is False
        assert page.public_exposure_scope is None
        assert page.public_exposed_version_id is None
    # no-store means the next request reflects the revocation immediately.
    assert anon.get("/api/v1/public/w/default/pages/revoke-page").status_code == 404


def test_indexed_public_grant_advances_with_new_published_snapshot(page_service):
    created = _make_page(page_service, "indexed-advance", published=True, public=True)
    updated = page_service.update_page(
        page_id=created.id,
        title="Indexed Advance",
        body_md="# Indexed Advance\n\nNew indexed body.",
        actor_kind="human",
        actor_id="dev-local-user",
        base_version_id=created.version_id,
        base_content_hash=created.content_hash,
        actor_provenance="browser_session",
    )

    with page_service.session_factory() as session:
        page = session.get(ContextPage, created.id)
        assert page.is_public is True
        assert page.public_exposure_scope == "indexed_web"
        assert page.public_exposed_version_id == updated.version_id
        assert page.current_published_version_id == updated.version_id

    response = _anon_client(page_service).get(
        "/api/v1/public/w/default/pages/indexed-advance"
    )
    assert response.status_code == 200
    assert "New indexed body." in response.json()["data"]["body_md"]


def test_unlisted_grant_is_revoked_when_a_new_version_is_published(page_service):
    client = _authed_client(page_service)
    created = _make_page(page_service, "unlisted-pinned", published=True, public=False)
    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, created.id)
        workspace = session.get(Workspace, page.workspace_id)
        workspace.visibility = "team"
        published_version_id = page.current_published_version_id
    shared = client.post(
        f"/api/v1/workspaces/{created.workspace_id}/context/pages/{created.id}/publish",
        json={
            "is_public": True,
            "acknowledge_public_exposure": True,
            "expected_published_version_id": str(published_version_id),
        },
    )
    assert shared.status_code == 200, shared.text
    anonymous = _anon_client(page_service)
    path = "/api/v1/public/w/default/pages/unlisted-pinned"
    assert anonymous.get(path).status_code == 200

    updated = page_service.update_page(
        page_id=created.id,
        title="Unlisted Pinned",
        body_md="# Unlisted Pinned\n\nNewly published body.",
        actor_kind="human",
        actor_id="dev-local-user",
        base_version_id=created.version_id,
        base_content_hash=created.content_hash,
        actor_provenance="browser_session",
    )

    with page_service.session_factory() as session:
        page = session.get(ContextPage, created.id)
        assert page.current_published_version_id == updated.version_id
        assert page.is_public is False
        assert page.public_exposure_scope is None
        assert page.public_exposed_version_id is None
    assert anonymous.get(path).status_code == 404


def test_public_read_fails_closed_for_visibility_or_version_mismatch(page_service):
    created = _make_page(
        page_service, "grant-mismatch", published=True, public=True
    )
    other = _make_page(
        page_service, "grant-mismatch-other", published=True, public=False
    )
    anonymous = _anon_client(page_service)
    path = "/api/v1/public/w/default/pages/grant-mismatch"
    assert anonymous.get(path).status_code == 200

    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, created.id)
        workspace = session.get(Workspace, page.workspace_id)
        workspace.visibility = "team"
    assert anonymous.get(path).status_code == 404

    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, created.id)
        workspace = session.get(Workspace, page.workspace_id)
        workspace.visibility = "public"
        page.public_exposed_version_id = other.version_id
    assert anonymous.get(path).status_code == 404


def test_public_read_strips_frontmatter_and_uses_published_timestamp(page_service):
    # Security review follow-ups: the public body must not leak the managed YAML
    # frontmatter (tags/aliases), and updated_at must reflect the PUBLISHED
    # version, not the page row (which private draft edits re-stamp).
    from datetime import UTC, datetime

    from backend.models.context import ContextPageVersion

    created = _make_page(page_service, "fm-page", published=True, public=True)
    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, created.id)
        version = session.get(ContextPageVersion, page.current_published_version_id)
        version.body_md = (
            "---\n"
            "title: Fm Page\n"
            "slug: fm-page\n"
            "type: overview\n"
            'tags: ["internal", "confidential"]\n'
            'aliases: ["secret-alias"]\n'
            "---\n\n"
            "# Fm Page\n\nVisible public body.\n"
        )
        # A published-version timestamp distinct from the (later) page row.
        version.created_at = datetime(2026, 1, 2, tzinfo=UTC)

    anon = _anon_client(page_service)
    resp = anon.get("/api/v1/public/w/default/pages/fm-page")

    assert resp.status_code == 200
    data = resp.json()["data"]
    body = data["body_md"]
    # The frontmatter block and its tags/aliases are stripped; body remains.
    assert "Visible public body." in body
    assert "---" not in body
    for leak in ("internal", "confidential", "secret-alias", "tags:", "aliases:"):
        assert leak not in body
    # updated_at is the published version's timestamp, not a page-row value.
    assert data["updated_at"].startswith("2026-01-02")


def test_public_read_title_from_published_body_not_draft_page_row(page_service):
    # P1: page.title tracks the (possibly unpublished) draft version, so the
    # public title must be derived from the PUBLISHED body's heading — a draft
    # rename must never leak through the public endpoint.
    from backend.models.context import ContextPageVersion

    created = _make_page(page_service, "title-page", published=True, public=True)
    with page_service.session_factory.begin() as session:
        page = session.get(ContextPage, created.id)
        version = session.get(ContextPageVersion, page.current_published_version_id)
        version.body_md = "# Published Title\n\nPublished body."
        # A later draft rename mutates the page row but not the published head.
        page.title = "Secret Draft Title"

    anon = _anon_client(page_service)
    resp = anon.get("/api/v1/public/w/default/pages/title-page")

    assert resp.status_code == 200
    assert resp.json()["data"]["title"] == "Published Title"
    assert "Secret Draft Title" not in resp.text


def test_authenticated_page_payload_exposes_is_public(page_service):
    # P2: is_public is on the authenticated payload so the publish UI can
    # reconcile its toggle without probing the anonymous URL.
    client = _authed_client(page_service)
    created = _make_page(page_service, "flag-page", published=True, public=False)

    published = client.post(
        f"/api/v1/workspaces/{created.workspace_id}/context/pages"
        f"/{created.id}/publish",
        json={"is_public": True},
    )
    assert published.status_code == 200
    assert published.json()["data"]["is_public"] is True

    unpublished = client.post(
        f"/api/v1/workspaces/{created.workspace_id}/context/pages"
        f"/{created.id}/publish",
        json={"is_public": False},
    )
    assert unpublished.json()["data"]["is_public"] is False


def test_public_read_rejects_overlong_slug(page_service):
    # The unauthenticated route bounds path length (max_length=64) like the
    # authenticated sibling, so a huge slug is rejected before any DB lookup.
    anon = _anon_client(page_service)
    resp = anon.get("/api/v1/public/w/default/pages/" + ("a" * 100))
    assert resp.status_code == 422


def test_public_spa_share_route_error_fails_closed(page_service, tmp_path):
    # One SPA shell serves indexed and unlisted pages. The static fallback cannot
    # truthfully choose a crawler policy before the client resolves page scope,
    # so successful index.html responses are not blanket-noindexed. This test
    # app has no SPA bundle, however, and its terminal 404 safely carries the
    # crawler/cache policy just like other public-surface errors.
    anon = _anon_client(page_service, static_root=tmp_path)
    resp = anon.get("/w/default/pages/whatever-slug")
    assert resp.status_code == 404
    assert resp.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert resp.headers["Cache-Control"] == "no-store"


def test_public_validation_error_carries_noindex_nostore(page_service):
    # A pattern-invalid slug fails Path validation BEFORE the handler runs, but
    # the noindex/no-store guarantee must still hold on that 422 (middleware).
    anon = _anon_client(page_service)
    resp = anon.get("/api/v1/public/w/default/pages/Bad_Slug")
    assert resp.status_code == 422
    assert resp.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert resp.headers["Cache-Control"] == "no-store"

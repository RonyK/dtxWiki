import os
import sys
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app.main import create_app
from backend.app.settings import Settings
from backend.converters.external_protocol import ExternalProcessConverter
from backend.models.agent import AgentPage
from backend.models.audit import AuditEvent
from backend.models.bundle import AgentInputBundle, AgentInputBundleItem
from backend.models.context import ContextPage
from backend.models.coverage import KnowledgeCoverageGap
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import MCPClient, Workspace
from backend.schemas.read import SEARCH_QUERY_MAX_LENGTH
from backend.services.extraction_service import ExtractionService
from backend.services.library_service import LibraryService
from backend.services.public_page_exposure import grant_public_page_exposure
from backend.tests.conftest import authenticate_test_client_in_workspace

_CONVERTER_HEALTH_READY: set = set()
# Mirrors the inherited-environment allowlist in ``_run_bounded``.
_CONVERTER_ENV_NAMES = ("LANG", "LC_ALL", "SYSTEMROOT", "WINDIR")


def _is_bundled_host(command) -> bool:
    return (
        len(command) == 5
        and command[0] == sys.executable
        and command[1:4] == ("-m", "backend.worker.converter_host", "--converter")
    )


@pytest.fixture
def cached_converter_health(monkeypatch):
    """Run each bundled converter's health handshake once per module and worker.

    Every per-test container builds a fresh ``ConverterRegistry`` whose first
    readiness check spawns the converter subprocess (~0.26 s). Tests that only
    consume the readiness result opt in; handshake, mismatch and failure
    behavior stays covered, uncached, by ``test_converter_adapters.py``. Only
    the first-party bundled host (``<python> -m backend.worker.converter_host
    --converter <id>``) is cached, and only a ready result is remembered, keyed
    by command, descriptor, timeout and the environment values the subprocess
    inherits. Every other provider runs the real healthcheck unchanged.
    """

    real = ExternalProcessConverter.healthcheck

    def _healthcheck(self, *, timeout_seconds=5.0):
        if not _is_bundled_host(self._command):
            return real(self, timeout_seconds=timeout_seconds)
        key = (
            self._command,
            self._descriptor,
            timeout_seconds,
            tuple(os.environ.get(name) for name in _CONVERTER_ENV_NAMES),
        )
        if key in _CONVERTER_HEALTH_READY:
            return True, None
        result = real(self, timeout_seconds=timeout_seconds)
        if result == (True, None):
            _CONVERTER_HEALTH_READY.add(key)
        return result

    monkeypatch.setattr(ExternalProcessConverter, "healthcheck", _healthcheck)


def make_client(page_service, email: str = "test@example.com") -> TestClient:
    client = TestClient(
        create_app(
            settings=Settings(
                database_url="sqlite+pysqlite:///:memory:",
                session_secret="test-session-secret",
            ),
            page_service=page_service,
        )
    )
    authenticate_test_client_in_workspace(
        client,
        page_service.session_factory,
        email=email,
        workspace_slug="default",
    )
    return client


def _create_published_page(
    client: TestClient,
    workspace_id: str,
    *,
    title: str,
    body_md: str,
    slug: str | None = None,
    page_service=None,
) -> dict:
    """P9.1: ``POST context/pages`` always lands in "editing" -- publish it
    immediately with a follow-up PATCH, mirroring the retired
    ``write_page(as_draft=False)`` single-shot create-and-publish. Needed
    here specifically because backlinks/wikilinks are only indexed for
    non-"editing" content (see `refresh_page_links`'s callers).
    ``create_page_first`` never derives a slug from title (always
    ``p-{page_id}``); pass ``slug`` + ``page_service`` to write a
    human-readable one directly onto the row afterward."""

    pages_url = f"/api/v1/workspaces/{workspace_id}/context/pages"
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
    page = published.json()["data"]
    if slug is not None:
        assert page_service is not None
        with page_service.session_factory.begin() as session:
            session.get(ContextPage, UUID(page["id"])).slug = slug
        page["slug"] = slug
    return page


def _issue_pat(client: TestClient, scopes: list[str]) -> str:
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    issued = client.post(
        "/api/v1/auth/tokens",
        json={
            "workspace_grants": [{"workspace_id": workspace_id}],
            "name": f"read-scope-{','.join(scopes)}",
            "scopes": scopes,
        },
    )
    assert issued.status_code in {200, 201}, issued.text
    token = issued.json()["data"]["plain_token"]
    client.cookies.clear()
    return token


def test_read_api_exposes_workspace_tree_and_agent_reads(page_service):
    client = make_client(page_service)

    workspaces = client.get("/api/v1/workspaces")
    assert workspaces.status_code == 200
    workspace_id = workspaces.json()["data"]["current_workspace_id"]

    workspace = client.get(f"/api/v1/workspaces/{workspace_id}")
    assert workspace.status_code == 200
    assert workspace.json()["data"]["slug"] == "default"

    tree = client.get(
        f"/api/v1/workspaces/{workspace_id}/tree?section=agent",
    )
    assert tree.status_code == 200
    tree_data = tree.json()["data"]
    assert any(node["slug"] == "agent" for node in tree_data["nodes"])
    assert tree_data["policies"]["agent"]["allow_user_folder_create"] is True

    categories = client.get(
        f"/api/v1/workspaces/{workspace_id}/agent/categories",
    )
    assert categories.status_code == 200
    assert categories.json()["data"][0]["category"] == "agent_guide"

    agent_pages = client.get(
        f"/api/v1/workspaces/{workspace_id}/agent/pages?category=agent_guide",
    )
    assert agent_pages.status_code == 200
    agent_page_id = agent_pages.json()["data"][0]["id"]

    agent_page = client.get(
        f"/api/v1/workspaces/{workspace_id}/agent/pages/{agent_page_id}",
    )
    assert agent_page.status_code == 200
    assert agent_page.json()["data"]["body_markdown"].startswith(
        "# Default System Prompt"
    )


def test_agent_reader_bundle_inventory_exposes_state_and_expiry(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    with page_service.session_factory.begin() as session:
        workspace = session.get(Workspace, UUID(workspace_id))
        agent_page = session.scalar(
            select(AgentPage).where(AgentPage.workspace_id == workspace.id)
        )
        mcp_client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == workspace.id)
        )
        now = datetime.now(UTC)
        bundle = AgentInputBundle(
            workspace_id=workspace.id,
            client_id=mcp_client.id,
            task_intent="reader inventory",
            token_budget=2048,
            status="used",
            ttl_seconds=3600,
            valid_until=now + timedelta(hours=1),
            invalidation_keys={},
            used_at=now,
        )
        session.add(bundle)
        session.flush()
        session.add(
            AgentInputBundleItem(
                workspace_id=bundle.workspace_id,
                bundle_id=bundle.id,
                source_kind="agent_page",
                agent_page_version_id=agent_page.current_active_version_id,
                category_or_type=agent_page.category,
                priority=1,
                included_reason="always_policy",
            )
        )
        agent_page_id = agent_page.id
        bundle_id = bundle.id
        valid_until = bundle.valid_until

    old_route = client.get(
        f"/api/v1/workspaces/{workspace_id}/agent-pages/{agent_page_id}/bundles"
    )
    assert old_route.status_code == 404, old_route.text

    response = client.get(
        f"/api/v1/workspaces/{workspace_id}/agent/pages/{agent_page_id}/bundles"
    )

    assert response.status_code == 200, response.text
    assert response.json()["data"] == [
        {
            "bundle_id": str(bundle_id),
            "status": "used",
            "created_at": response.json()["data"][0]["created_at"],
            "valid_until": valid_until.isoformat().replace("+00:00", "Z"),
            "used_at": response.json()["data"][0]["used_at"],
            "expired_at": None,
            "included": True,
        }
    ]


def test_read_only_owner_pat_cannot_read_sensitive_agent_page_body_or_diff(
    page_service,
):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    listed = client.get(
        f"/api/v1/workspaces/{workspace_id}/agent/pages?category=agent_guide"
    )
    assert listed.status_code == 200
    page_id = listed.json()["data"][0]["id"]
    with page_service.session_factory() as session:
        page = session.get(AgentPage, UUID(page_id))
        assert page is not None
        version_id = page.current_active_version_id
        assert version_id is not None

    token = _issue_pat(client, ["mcp:read"])
    headers = {"Authorization": f"Bearer {token}"}
    detail = client.get(
        f"/api/v1/workspaces/{workspace_id}/agent/pages/{page_id}",
        headers=headers,
    )
    diff = client.get(
        f"/api/v1/workspaces/{workspace_id}/agent/pages/{page_id}/diff"
        f"?from_version_id={version_id}&to_version_id={version_id}",
        headers=headers,
    )

    for response in (detail, diff):
        assert response.status_code == 403
        assert response.json()["error_code"] == "workspace_role_insufficient"


def test_read_api_diffs_and_restores_agent_versions(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    created = client.post(
        f"/api/v1/workspaces/{workspace_id}/agent/pages",
        json={
            "category": "persona",
            "title": "Research Persona",
            "slug": "research-persona",
            "body_markdown": "# Research Persona\n\nOriginal",
            "reason": "create persona",
        },
    )
    assert created.status_code == 201
    created_data = created.json()["data"]
    page_id = created_data["agent_page_id"]
    v1 = created_data["draft_version_id"]

    updated = client.post(
        f"/api/v1/workspaces/{workspace_id}/agent/pages/{page_id}/draft",
        json={
            "base_version_id": v1,
            "category": "persona",
            "title": "Research Persona",
            "slug": "research-persona",
            "body_markdown": "# Research Persona\n\nCurrent",
            "reason": "update persona",
        },
    )
    assert updated.status_code == 200
    v2 = updated.json()["data"]["draft_version_id"]

    diff = client.get(
        f"/api/v1/workspaces/{workspace_id}/agent/pages/{page_id}/diff"
        f"?from_version_id={v1}&to_version_id={v2}",
    )
    assert diff.status_code == 200
    diff_data = diff.json()["data"]
    assert diff_data["from_version_no"] == 1
    assert diff_data["to_version_no"] == 2
    assert "-Original" in diff_data["lines"]
    assert "+Current" in diff_data["lines"]

    restored = client.post(
        f"/api/v1/workspaces/{workspace_id}/agent/pages/{page_id}/restore",
        json={"version_id": v1},
    )
    assert restored.status_code == 200
    v3 = restored.json()["data"]["draft_version_id"]
    assert v3 not in {v1, v2}

    draft = client.get(
        f"/api/v1/workspaces/{workspace_id}/agent/pages/{page_id}?version=draft",
    )
    assert draft.status_code == 200
    draft_data = draft.json()["data"]
    assert draft_data["body_markdown"] == "# Research Persona\n\nOriginal"
    assert draft_data["version_info"]["version_no"] == 3
    assert draft_data["version_info"]["version_status"] == "draft"


def test_restore_agent_version_rejects_null_body_markdown(page_service, monkeypatch):
    """B-P0-3 Sprint_6 Fix_4: guard restore against version rows whose
    body_markdown is NULL. The schema marks the column non-nullable, but a
    defensive guard keeps the service from forwarding a None to
    write_draft if a row is ever corrupted (e.g. via a manual fix or a
    future schema relaxation).
    """
    import pytest

    from backend.models.agent import AgentPage, AgentPageVersion
    from backend.models.workspace import Workspace
    from backend.services.agent_page_service import AgentPageService
    from backend.services.read_errors import ReadValidationError

    service = AgentPageService()
    with page_service.session_factory.begin() as session:
        workspace = session.query(Workspace).filter_by(slug="default").one()
        page = AgentPage(
            workspace_id=workspace.id,
            category="persona",
            title="Corrupt Persona",
            slug="corrupt-persona",
            lifecycle_status="active",
            injection_policy="optional",
            priority=100,
            sensitivity="standard",
            created_by_actor_type="human",
            created_by_actor_id=None,
        )
        session.add(page)
        session.flush()
        version = AgentPageVersion(
            workspace_id=workspace.id,
            agent_page_id=page.id,
            version_no=1,
            body_markdown="seed body",
            frontmatter={},
            actor_type="human",
            actor_id=None,
            version_status="active",
        )
        session.add(version)
        session.flush()
        page.current_active_version_id = version.id
        session.flush()
        workspace_id = workspace.id
        page_id = page.id
        version_id = version.id

    # Patch _require_version so it returns a version proxy whose
    # body_markdown attribute is None — simulating a corrupted row. The
    # NOT NULL constraint at the DB layer prevents a true null INSERT in
    # tests, so we drive the guard via the service entry point.
    def _fake_require_version(session, page, version_id):
        from types import SimpleNamespace

        return SimpleNamespace(
            id=version_id,
            agent_page_id=page.id,
            version_no=1,
            body_markdown=None,
            frontmatter={},
        )

    monkeypatch.setattr(
        AgentPageService, "_require_version", staticmethod(_fake_require_version)
    )

    with page_service.session_factory.begin() as session:
        with pytest.raises(ReadValidationError) as exc_info:
            service.restore_version(
                session=session,
                workspace_id=workspace_id,
                agent_page_id=page_id,
                version_id=version_id,
                actor_kind="human",
                actor_id="00000000-0000-0000-0000-000000000001",
                actor_provenance="browser_session",
            )
        assert "no content" in str(exc_info.value)


def test_diff_agent_versions_404_when_from_version_missing(page_service):
    """B-P1-1 Sprint_6 Fix_4: diff endpoint must surface 404 when the
    from_version_id refers to a non-existent AgentPageVersion. The service
    raises ReadNotFoundError via _require_version, which the API maps to
    HTTP 404 with error_code ``not_found``.
    """
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    created = client.post(
        f"/api/v1/workspaces/{workspace_id}/agent/pages",
        json={
            "category": "persona",
            "title": "Diff 404 Persona",
            "slug": "diff-404-persona",
            "body_markdown": "# Diff 404\n\nseed",
            "reason": "create persona for 404 diff test",
        },
    )
    assert created.status_code == 201
    created_data = created.json()["data"]
    page_id = created_data["agent_page_id"]
    real_version_id = created_data["draft_version_id"]
    bogus_version_id = "00000000-0000-0000-0000-0000000ffff1"

    diff = client.get(
        f"/api/v1/workspaces/{workspace_id}/agent/pages/{page_id}/diff"
        f"?from_version_id={bogus_version_id}&to_version_id={real_version_id}",
    )
    assert diff.status_code == 404
    assert diff.json()["error_code"] == "not_found"


def test_diff_agent_versions_422_when_bodies_exceed_size_limit(page_service):
    """B-P1-1 Sprint_6 Fix_4: diff endpoint must return 422 when the combined
    body_markdown length of the two versions exceeds
    AGENT_PAGE_DIFF_MAX_INPUT_CHARS (200_000 chars). Each body is ~150_000
    chars so combined ~300_000 — well past the cap.
    """
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    big_body_a = "x" * 150_000
    created = client.post(
        f"/api/v1/workspaces/{workspace_id}/agent/pages",
        json={
            "category": "persona",
            "title": "Big Diff Persona",
            "slug": "big-diff-persona",
            "body_markdown": big_body_a,
            "reason": "create big body",
        },
    )
    assert created.status_code == 201
    created_data = created.json()["data"]
    page_id = created_data["agent_page_id"]
    v1 = created_data["draft_version_id"]

    big_body_b = "y" * 150_000
    updated = client.post(
        f"/api/v1/workspaces/{workspace_id}/agent/pages/{page_id}/draft",
        json={
            "base_version_id": v1,
            "category": "persona",
            "title": "Big Diff Persona",
            "slug": "big-diff-persona",
            "body_markdown": big_body_b,
            "reason": "second large body",
        },
    )
    assert updated.status_code == 200
    v2 = updated.json()["data"]["draft_version_id"]

    diff = client.get(
        f"/api/v1/workspaces/{workspace_id}/agent/pages/{page_id}/diff"
        f"?from_version_id={v1}&to_version_id={v2}",
    )
    assert diff.status_code == 422
    assert diff.json()["error_code"] == "validation_failed"
    assert "too large" in diff.json()["message"]


def test_restore_agent_version_with_existing_draft_supersedes_it(page_service):
    """B-P1-1 Sprint_6 Fix_4: when restore_version is invoked while the
    AgentPage already has a latest_draft_version_id, the new draft created
    by restore_version must REPLACE the existing draft pointer — not leave
    the previous draft as the page's latest_draft_version_id.
    """
    from uuid import UUID as _UUID

    from backend.models.agent import AgentPage

    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    created = client.post(
        f"/api/v1/workspaces/{workspace_id}/agent/pages",
        json={
            "category": "persona",
            "title": "Restore Supersede",
            "slug": "restore-supersede",
            "body_markdown": "# Restore Supersede\n\nv1 body",
            "reason": "create v1",
        },
    )
    assert created.status_code == 201
    created_data = created.json()["data"]
    page_id = created_data["agent_page_id"]
    v1 = created_data["draft_version_id"]

    second = client.post(
        f"/api/v1/workspaces/{workspace_id}/agent/pages/{page_id}/draft",
        json={
            "base_version_id": v1,
            "category": "persona",
            "title": "Restore Supersede",
            "slug": "restore-supersede",
            "body_markdown": "# Restore Supersede\n\nv2 body",
            "reason": "create v2 draft",
        },
    )
    assert second.status_code == 200
    v2_draft = second.json()["data"]["draft_version_id"]

    # Confirm page now points at v2_draft as the latest draft.
    with page_service.session_factory() as session:
        page_row = session.get(AgentPage, _UUID(page_id))
        assert page_row is not None
        assert str(page_row.latest_draft_version_id) == v2_draft

    restored = client.post(
        f"/api/v1/workspaces/{workspace_id}/agent/pages/{page_id}/restore",
        json={"version_id": v1},
    )
    assert restored.status_code == 200
    v3_draft = restored.json()["data"]["draft_version_id"]
    assert v3_draft not in {v1, v2_draft}

    # The new draft must REPLACE the old one — page.latest_draft_version_id
    # is v3_draft, not v2_draft.
    with page_service.session_factory() as session:
        page_row = session.get(AgentPage, _UUID(page_id))
        assert page_row is not None
        assert str(page_row.latest_draft_version_id) == v3_draft
        assert str(page_row.latest_draft_version_id) != v2_draft


def test_diff_versions_eager_loads_body_markdown(page_service):
    """B-P1-5 Sprint_6 Fix_4: diff_versions resolves two AgentPageVersion
    rows via _require_version (which uses session.get) then reads
    ``.body_markdown`` on each. Because ``body_markdown`` is a regular
    ``Mapped[str]`` column (NOT ``deferred()``), session.get already loads
    it eagerly — no extra SELECT should be issued when the attribute is
    accessed afterwards.

    This test pins that contract by counting SELECTs issued during the
    service call. The expected breakdown is:
      - 1 SELECT for the page (workspace guard)
      - 1 SELECT for each version (2 total)
    so the upper bound is 3. A future regression that marks
    body_markdown as deferred or switches to lazy-loaded relations would
    raise the count to 5+.
    """
    from sqlalchemy import event, select

    from backend.models.workspace import Workspace
    from backend.schemas.agent import AgentPageDraftRequest
    from backend.services.agent_page_service import AgentPageService

    service = AgentPageService()

    def _write_draft(session, workspace_id, **request):
        return service.write_draft(
            session=session,
            workspace_id=workspace_id,
            request=AgentPageDraftRequest(
                category="persona",
                title="Query Count Persona",
                slug="query-count-persona",
                **request,
            ),
            actor_kind="human",
            actor_id="tester",
            actor_provenance="browser_session",
        )

    with page_service.session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        first = _write_draft(
            session,
            workspace_id,
            body_markdown="# Query Count\n\nfirst body",
            reason="create v1 for query-count test",
        )
        page_id = first.agent_page_id
        v1 = first.draft_version_id
        second = _write_draft(
            session,
            workspace_id,
            base_version_id=v1,
            body_markdown="# Query Count\n\nsecond body",
            reason="create v2 for query-count test",
        )
        v2 = second.draft_version_id
    workspace_id = str(workspace_id)

    select_count = 0

    def _count_selects(conn, cursor, statement, parameters, context, executemany):
        nonlocal select_count
        if statement.lstrip().upper().startswith("SELECT"):
            select_count += 1

    engine = page_service.session_factory.kw["bind"]
    event.listen(engine, "before_cursor_execute", _count_selects)
    try:
        with page_service.session_factory() as session:
            select_count = 0
            diff_result = service.diff_versions(
                session=session,
                workspace_id=UUID(workspace_id),
                agent_page_id=page_id,
                from_version_id=v1,
                to_version_id=v2,
            )
    finally:
        event.remove(engine, "before_cursor_execute", _count_selects)

    assert diff_result.from_version_no == 1
    assert diff_result.to_version_no == 2
    # 1 page lookup + 2 version lookups = 3. Allow ≤ 3 as the upper bound.
    assert select_count <= 3, (
        f"diff_versions issued {select_count} SELECTs; expected ≤ 3. "
        "A regression here may indicate body_markdown became deferred."
    )


def test_read_api_exposes_context_changes_and_search(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    capabilities = client.get(f"/api/v1/workspaces/{workspace_id}/search/capabilities")
    assert capabilities.status_code == 200
    assert capabilities.json()["data"] == {"supported_retrieval_modes": ["keyword"]}
    created = _create_published_page(
        client,
        workspace_id,
        title="API Context",
        body_md="# API Context\n\nNeedle body",
        slug="api-context",
        page_service=page_service,
    )

    # Context is the main reading surface for these pages, so it must expose
    # the page-level web-publication flag instead of forcing the UI to make a
    # second generic PageReader request.
    with page_service.session_factory() as session:
        page = session.get(ContextPage, UUID(created["id"]))
        assert page is not None
        assert page.current_published_version_id is not None
        grant_public_page_exposure(
            page,
            scope="unlisted_web",
            version_id=page.current_published_version_id,
        )
        session.commit()

    context_pages = client.get(
        f"/api/v1/workspaces/{workspace_id}/context/pages",
    )
    assert context_pages.status_code == 200
    assert context_pages.json()["data"][0]["slug"] == "api-context"
    assert context_pages.json()["data"][0]["is_public"] is True

    context_page = client.get(
        f"/api/v1/workspaces/{workspace_id}/context/pages/{created['id']}",
    )
    assert context_page.status_code == 200
    assert context_page.json()["data"]["is_public"] is True
    assert context_page.json()["data"]["body_md"] == "# API Context\n\nNeedle body"

    linked = _create_published_page(
        client,
        workspace_id,
        title="API Context Linker",
        body_md="# API Context Linker\n\nSee [[api-context]].",
        slug="api-context-linker",
        page_service=page_service,
    )
    neighbors = client.get(
        f"/api/v1/workspaces/{workspace_id}/context/pages/{created['id']}/neighbors",
    )
    assert neighbors.status_code == 200
    assert neighbors.json()["data"] == {
        "backlinks": [
            {
                "kind": "context_page",
                "id": linked["id"],
                "title": "API Context Linker",
                "path": "/context/api-context-linker.md",
                "edge_kind": "wikilink",
                "direction": "in",
            }
        ],
        "outbound": [],
        "citations": [],
        "co_access": [],
        "mermaid": None,
    }

    changes = client.get(
        f"/api/v1/workspaces/{workspace_id}/changesets"
        f"?target_type=context_page&target_id={created['id']}&include_diff=true",
    )
    assert changes.status_code == 200
    assert changes.json()["data"]["changesets"][0]["status"] == "applied"

    search = client.post(
        f"/api/v1/workspaces/{workspace_id}/search",
        json={"query": "needle", "scope": "context_page"},
    )
    assert search.status_code == 200
    assert search.json()["data"]["ranked_results"][0]["type"] == "context_page"

    advisory = client.post(
        f"/api/v1/workspaces/{workspace_id}/search",
        json={
            "query": "needle",
            "scope": "context_page",
            "filters": {"tag": ["not-yet-supported"]},
        },
    )
    assert advisory.status_code == 200
    assert advisory.json()["data"]["warnings"] == ["unsupported_filter_tag"]
    assert advisory.json()["envelope"]["warnings"] == [
        {
            "code": "unsupported_filter_tag",
            "message": "The tag filter is not applied by any retriever yet.",
        }
    ]

    unsupported = client.post(
        f"/api/v1/workspaces/{workspace_id}/search",
        json={"query": "needle", "retrieval_mode": "semantic"},
    )
    assert unsupported.status_code == 422
    assert unsupported.json()["error_code"] == "unsupported_retrieval_mode"

    too_long = client.post(
        f"/api/v1/workspaces/{workspace_id}/search",
        json={"query": "x" * (SEARCH_QUERY_MAX_LENGTH + 1)},
    )
    assert too_long.status_code == 422
    assert too_long.json()["error_code"] == "validation_failed"

    no_hit = client.post(
        f"/api/v1/workspaces/{workspace_id}/search",
        json={"query": "zzzz rest coverage gap", "scope": "context_page"},
    )
    assert no_hit.status_code == 200
    assert no_hit.json()["data"]["ranked_results"] == []
    assert "coverage_gap_recorded" in no_hit.json()["data"]["warnings"]
    with page_service.session_factory() as session:
        gap = (
            session.query(KnowledgeCoverageGap)
            .filter_by(
                workspace_id=UUID(workspace_id),
                surface="search_rest",
            )
            .one()
        )
    assert gap.query_norm == "zzzz rest coverage gap"


def test_read_api_exposes_sources_versions_and_artifacts(
    page_service, media_installer_factory
):
    client = make_client(page_service)
    with page_service.session_factory.begin() as session:
        workspace = session.query(Workspace).filter_by(slug="default").one()
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace.id,
            canonical_uri="https://example.com/api-source",
            title="API Source",
            raw_text="# API Source\n\nNeedle artifact",
        )
        artifact = ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )
        workspace_id = str(workspace.id)
        library_item_id = str(registration.library_item.id)
        version_id = str(registration.version.id)
        artifact_id = str(artifact.id)

    sources = client.get(f"/api/v1/workspaces/{workspace_id}/library/items")
    assert sources.status_code == 200
    assert sources.json()["data"][0]["uri"] == "https://example.com/api-source"

    source = client.get(
        f"/api/v1/workspaces/{workspace_id}/library/items/{library_item_id}",
    )
    assert source.status_code == 200
    assert source.json()["data"]["current_version"]["id"] == version_id

    version = client.get(
        f"/api/v1/workspaces/{workspace_id}/library/versions/{version_id}",
    )
    assert version.status_code == 200
    assert version.json()["data"]["id"] == version_id

    artifact_response = client.get(
        f"/api/v1/workspaces/{workspace_id}/library/artifacts/{artifact_id}",
    )
    assert artifact_response.status_code == 200
    assert (
        artifact_response.json()["data"]["index_text"]
        == "# API Source\n\nNeedle artifact"
    )


def test_read_api_creates_tree_folder(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]

    created = client.post(
        f"/api/v1/workspaces/{workspace_id}/tree/nodes",
        json={
            "section": "context",
            "title": "Research",
            "slug": "research",
        },
    )

    assert created.status_code == 201
    folder = created.json()["data"]
    assert folder["title"] == "Research"
    assert folder["node_kind"] == "folder"
    assert folder["target_id"] is None
    assert folder["can_have_children"] is True

    duplicate = client.post(
        f"/api/v1/workspaces/{workspace_id}/tree/nodes",
        json={
            "section": "context",
            "title": "Research again",
            "slug": "research",
        },
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error_code"] == "duplicate_slug_under_parent"

    tree = client.get(f"/api/v1/workspaces/{workspace_id}/tree?section=context")
    assert tree.status_code == 200
    assert any(node["id"] == folder["id"] for node in tree.json()["data"]["nodes"])


def test_read_api_allows_library_tree_folder_creation(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]

    created = client.post(
        f"/api/v1/workspaces/{workspace_id}/tree/nodes",
        json={
            "section": "library",
            "title": "Library Drafts",
        },
    )

    assert created.status_code == 201, created.text
    folder = created.json()["data"]
    assert folder["section"] == "library"
    assert folder["node_kind"] == "folder"


def test_read_api_rejects_cross_workspace_tree_parent(page_service):
    from sqlalchemy import select

    from backend.models.tree import WorkspaceTreeNode
    from backend.scripts.seed_dev import seed_default_workspace

    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    with page_service.session_factory.begin() as session:
        foreign_workspace = seed_default_workspace(
            session,
            slug="tree-parent-foreign",
            title="Tree Parent Foreign",
        )
        foreign_context_root = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == foreign_workspace.id,
                WorkspaceTreeNode.slug == "context",
            )
        )
        assert foreign_context_root is not None
        foreign_parent_id = str(foreign_context_root.id)

    created = client.post(
        f"/api/v1/workspaces/{workspace_id}/tree/nodes",
        json={
            "parent_id": foreign_parent_id,
            "section": "context",
            "title": "Foreign Parent Child",
        },
    )

    assert created.status_code == 404
    assert created.json()["error_code"] == "not_found"


def test_read_api_deletes_tree_node_as_archived(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]

    parent = client.post(
        f"/api/v1/workspaces/{workspace_id}/tree/nodes",
        json={
            "section": "context",
            "title": "Archive Parent",
            "slug": "archive-parent",
        },
    ).json()["data"]
    child = client.post(
        f"/api/v1/workspaces/{workspace_id}/tree/nodes",
        json={
            "parent_id": parent["id"],
            "section": "context",
            "title": "Archive Child",
            "slug": "archive-child",
        },
    ).json()["data"]

    deleted = client.delete(
        f"/api/v1/workspaces/{workspace_id}/tree/nodes/{parent['id']}",
    )

    assert deleted.status_code == 200
    assert deleted.json()["data"]["visibility_status"] == "archived"
    tree = client.get(f"/api/v1/workspaces/{workspace_id}/tree?section=context")
    assert tree.status_code == 200
    node_ids = {node["id"] for node in tree.json()["data"]["nodes"]}
    assert parent["id"] not in node_ids
    assert child["id"] not in node_ids


def test_read_api_rejects_deleting_system_tree_node(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    tree = client.get(f"/api/v1/workspaces/{workspace_id}/tree?section=context")
    context_root = next(
        node for node in tree.json()["data"]["nodes"] if node["slug"] == "context"
    )

    deleted = client.delete(
        f"/api/v1/workspaces/{workspace_id}/tree/nodes/{context_root['id']}",
    )

    assert deleted.status_code == 403
    assert deleted.json()["error_code"] == "system_tree_node_protected"


def test_read_api_rejects_tree_mutations_for_workspace_viewer(page_service):
    from sqlalchemy import select

    from backend.models.user import User
    from backend.policy.rbac import grant_workspace_membership

    owner = make_client(page_service)
    viewer = make_client(page_service, email="tree-viewer@example.com")
    workspace_id = owner.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    workspace_uuid = UUID(workspace_id)
    with page_service.session_factory.begin() as session:
        viewer_user = session.scalar(
            select(User).where(User.email == "tree-viewer@example.com")
        )
        assert viewer_user is not None
        grant_workspace_membership(
            session=session,
            user_id=viewer_user.id,
            workspace_id=workspace_uuid,
            role="viewer",
        )

    parent = owner.post(
        f"/api/v1/workspaces/{workspace_id}/tree/nodes",
        json={
            "section": "context",
            "title": "Viewer Guard Parent",
            "slug": "viewer-guard-parent",
        },
    ).json()["data"]
    target = owner.post(
        f"/api/v1/workspaces/{workspace_id}/tree/nodes",
        json={
            "section": "context",
            "title": "Viewer Guard Target",
            "slug": "viewer-guard-target",
        },
    ).json()["data"]

    renamed = viewer.patch(
        f"/api/v1/workspaces/{workspace_id}/tree/nodes/{parent['id']}",
        json={"display_name": "Viewer Rename"},
    )
    moved = viewer.post(
        f"/api/v1/workspaces/{workspace_id}/tree/move",
        json={
            "node_id": parent["id"],
            "new_parent_id": target["id"],
            "reason": "viewer should not move tree nodes",
        },
    )
    deleted = viewer.delete(
        f"/api/v1/workspaces/{workspace_id}/tree/nodes/{parent['id']}",
    )

    assert renamed.status_code == 403
    assert renamed.json()["error_code"] == "workspace_role_insufficient"
    assert moved.status_code == 403
    assert moved.json()["error_code"] == "workspace_role_insufficient"
    assert deleted.status_code == 403
    assert deleted.json()["error_code"] == "workspace_role_insufficient"


def test_context_neighbors_hide_draft_only_seed_from_viewer(page_service):
    from sqlalchemy import select

    from backend.models.user import User
    from backend.policy.rbac import grant_workspace_membership

    owner = make_client(page_service)
    viewer = make_client(page_service, email="neighbors-viewer@example.com")
    workspace_id = owner.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    with page_service.session_factory.begin() as session:
        viewer_user = session.scalar(
            select(User).where(User.email == "neighbors-viewer@example.com")
        )
        assert viewer_user is not None
        grant_workspace_membership(
            session=session,
            user_id=viewer_user.id,
            workspace_id=UUID(workspace_id),
            role="viewer",
        )

    draft = owner.post(
        f"/api/v1/workspaces/{workspace_id}/context/pages",
        json={
            "title": "Neighbors Draft Only",
            "page_type": "concept",
            "body_md": "# Neighbors Draft Only\n\nPrivate draft.",
        },
    )
    assert draft.status_code == 201
    page_id = draft.json()["data"]["id"]

    owner_result = owner.get(
        f"/api/v1/workspaces/{workspace_id}/context/pages/{page_id}/neighbors"
    )
    viewer_result = viewer.get(
        f"/api/v1/workspaces/{workspace_id}/context/pages/{page_id}/neighbors"
    )

    assert owner_result.status_code == 200
    assert owner_result.json()["data"] == {
        "backlinks": [],
        "outbound": [],
        "citations": [],
        "co_access": [],
        "mermaid": None,
    }
    assert viewer_result.status_code == 404
    assert viewer_result.json()["error_code"] == "not_found"


def test_context_page_draft_filter_requires_draft_read_capability(page_service):
    from backend.models.user import User
    from backend.policy.rbac import grant_workspace_membership

    owner = make_client(page_service)
    viewer = make_client(page_service, email="draft-filter-viewer@example.com")
    workspace_id = owner.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    with page_service.session_factory.begin() as session:
        viewer_user = session.scalar(
            select(User).where(User.email == "draft-filter-viewer@example.com")
        )
        assert viewer_user is not None
        grant_workspace_membership(
            session=session,
            user_id=viewer_user.id,
            workspace_id=UUID(workspace_id),
            role="viewer",
        )

    draft = owner.post(
        f"/api/v1/workspaces/{workspace_id}/context/pages",
        json={
            "title": "Draft Filter Page",
            "page_type": "concept",
            "body_md": "# Draft Filter Page\n\nPrivate draft.",
        },
    )
    assert draft.status_code == 201

    owner_result = owner.get(
        f"/api/v1/workspaces/{workspace_id}/context/pages?has_draft=true"
    )
    assert owner_result.status_code == 200
    assert [item["title"] for item in owner_result.json()["data"]] == [
        "Draft Filter Page"
    ]
    assert owner_result.json()["total"] == 1

    viewer_result = viewer.get(
        f"/api/v1/workspaces/{workspace_id}/context/pages?has_draft=true"
    )
    assert viewer_result.status_code == 403
    assert viewer_result.json()["error_code"] == "scope_denied"

    invalid = owner.get(
        f"/api/v1/workspaces/{workspace_id}/context/pages?has_draft=maybe"
    )
    assert invalid.status_code == 422


def test_context_activity_hides_draft_only_page_from_viewer(page_service):
    from backend.models.user import User
    from backend.policy.rbac import grant_workspace_membership

    owner = make_client(page_service)
    viewer = make_client(page_service, email="activity-viewer@example.com")
    workspace_id = owner.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    with page_service.session_factory.begin() as session:
        viewer_user = session.scalar(
            select(User).where(User.email == "activity-viewer@example.com")
        )
        assert viewer_user is not None
        grant_workspace_membership(
            session=session,
            user_id=viewer_user.id,
            workspace_id=UUID(workspace_id),
            role="viewer",
        )

    draft = owner.post(
        f"/api/v1/workspaces/{workspace_id}/context/pages",
        json={
            "title": "Activity Draft Only",
            "page_type": "concept",
            "body_md": "# Activity Draft Only\n\nPrivate draft.",
        },
    )
    assert draft.status_code == 201
    page_id = draft.json()["data"]["id"]

    owner_result = owner.get(
        f"/api/v1/workspaces/{workspace_id}/context/pages/{page_id}/activity"
    )
    viewer_result = viewer.get(
        f"/api/v1/workspaces/{workspace_id}/context/pages/{page_id}/activity"
    )

    assert owner_result.status_code == 200
    owner_items = owner_result.json()["data"]["items"]
    assert owner_items
    actors = [item["actor"] for item in owner_items if item["actor"] is not None]
    assert actors
    assert all(actor["actor_id"] is None for actor in actors)
    assert all(actor["display_name"].strip() for actor in actors)
    assert any(actor["is_current_user"] is True for actor in actors)
    assert viewer_result.status_code == 404
    assert viewer_result.json()["error_code"] == "not_found"


def test_read_api_reorders_tree_siblings(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]

    def create(title: str, slug: str) -> str:
        res = client.post(
            f"/api/v1/workspaces/{workspace_id}/tree/nodes",
            json={"section": "context", "title": title, "slug": slug},
        )
        assert res.status_code == 201, res.text
        return res.json()["data"]["id"]

    alpha = create("Alpha", "alpha")
    bravo = create("Bravo", "bravo")
    charlie = create("Charlie", "charlie")

    def relative_order() -> list[str]:
        nodes = client.get(
            f"/api/v1/workspaces/{workspace_id}/tree?section=context"
        ).json()["data"]["nodes"]
        ordered = sorted(
            (node for node in nodes if node["id"] in {alpha, bravo, charlie}),
            key=lambda node: (node["sort_key"], node["title"], node["id"]),
        )
        return [node["id"] for node in ordered]

    assert relative_order() == [alpha, bravo, charlie]

    moved_up = client.post(
        f"/api/v1/workspaces/{workspace_id}/tree/reorder",
        json={"node_id": charlie, "direction": "up"},
    )
    assert moved_up.status_code == 200, moved_up.text
    assert relative_order() == [alpha, charlie, bravo]

    moved_down = client.post(
        f"/api/v1/workspaces/{workspace_id}/tree/reorder",
        json={"node_id": charlie, "direction": "down"},
    )
    assert moved_down.status_code == 200, moved_down.text
    assert relative_order() == [alpha, bravo, charlie]

    # Alpha is the first user folder, directly below the system "Inbox"; moving
    # it up must be refused so system folders stay pinned at the top.
    blocked = client.post(
        f"/api/v1/workspaces/{workspace_id}/tree/reorder",
        json={"node_id": alpha, "direction": "up"},
    )
    assert blocked.status_code == 403
    assert blocked.json()["error_code"] == "system_tree_node_protected"


def test_read_api_reorders_tree_siblings_by_anchor(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]

    def create(title: str, slug: str, **extra) -> dict:
        response = client.post(
            f"/api/v1/workspaces/{workspace_id}/tree/nodes",
            json={
                "section": "context",
                "title": title,
                "slug": slug,
                **extra,
            },
        )
        assert response.status_code == 201, response.text
        return response.json()["data"]

    alpha = create("Anchor Alpha", "anchor-alpha")
    bravo = create("Anchor Bravo", "anchor-bravo")
    charlie = create("Anchor Charlie", "anchor-charlie")

    def relative_order() -> list[str]:
        nodes = client.get(
            f"/api/v1/workspaces/{workspace_id}/tree?section=context"
        ).json()["data"]["nodes"]
        ordered = sorted(
            (
                node
                for node in nodes
                if node["id"] in {alpha["id"], bravo["id"], charlie["id"]}
            ),
            key=lambda node: (node["sort_key"], node["title"], node["id"]),
        )
        return [node["id"] for node in ordered]

    before = client.post(
        f"/api/v1/workspaces/{workspace_id}/tree/reorder",
        json={
            "node_id": charlie["id"],
            "anchor_node_id": alpha["id"],
            "placement": "before",
        },
    )
    assert before.status_code == 200, before.text
    assert relative_order() == [charlie["id"], alpha["id"], bravo["id"]]

    after = client.post(
        f"/api/v1/workspaces/{workspace_id}/tree/reorder",
        json={
            "node_id": charlie["id"],
            "anchor_node_id": bravo["id"],
            "placement": "after",
        },
    )
    assert after.status_code == 200, after.text
    assert relative_order() == [alpha["id"], bravo["id"], charlie["id"]]


def test_read_api_anchor_under_another_parent_moves_to_that_position(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]

    def create(title: str, slug: str, **extra) -> str:
        response = client.post(
            f"/api/v1/workspaces/{workspace_id}/tree/nodes",
            json={"section": "context", "title": title, "slug": slug, **extra},
        )
        assert response.status_code == 201, response.text
        return response.json()["data"]["id"]

    folder = create("Cross Folder", "cross-folder")
    first = create("Cross First", "cross-first", parent_id=folder)
    second = create("Cross Second", "cross-second", parent_id=folder)
    outside = create("Cross Outside", "cross-outside")

    def place(node_id: str, anchor_id: str, placement: str):
        return client.post(
            f"/api/v1/workspaces/{workspace_id}/tree/reorder",
            json={
                "node_id": node_id,
                "anchor_node_id": anchor_id,
                "placement": placement,
            },
        )

    def children(parent_id: str) -> list[str]:
        nodes = client.get(
            f"/api/v1/workspaces/{workspace_id}/tree?section=context"
        ).json()["data"]["nodes"]
        ordered = sorted(
            (node for node in nodes if node["parent_id"] == parent_id),
            key=lambda node: (node["sort_key"], node["title"], node["id"]),
        )
        return [node["id"] for node in ordered]

    root_id = client.get(
        f"/api/v1/workspaces/{workspace_id}/tree/nodes/{folder}"
    ).json()["data"]["parent_id"]

    into = place(outside, second, "before")
    assert into.status_code == 200, into.text
    assert children(folder) == [first, outside, second]
    with page_service.session_factory() as session:
        audits = session.scalars(
            select(AuditEvent).where(
                AuditEvent.target_id == UUID(outside),
                AuditEvent.action.in_(("tree.node.move", "tree.node.reorder")),
            )
        ).all()
    # Recorded as the same move the Move dialog writes, not a sibling reorder.
    assert [(audit.action, audit.payload["new_parent_id"]) for audit in audits] == [
        ("tree.node.move", folder)
    ]
    assert audits[0].payload["previous_parent_path"][-1]["id"] == root_id
    assert audits[0].payload["new_parent_path"][-1] == {
        "id": folder,
        "title": "Cross Folder",
    }

    out = place(first, folder, "after")
    assert out.status_code == 200, out.text
    top_level = children(root_id)
    assert top_level[top_level.index(folder) + 1] == first

    cycle = place(folder, outside, "after")
    assert cycle.status_code in {403, 404}, cycle.text
    assert children(folder) == [outside, second]


def test_read_api_anchor_reorder_cannot_cross_system_node(page_service):
    # LEAK-04: the direction form refuses to move a node past a system folder
    # (test_read_api_rejects_reordering_system_tree_node and the "Alpha is
    # pinned below Inbox" case above), but the anchor form only validated the
    # ANCHOR itself, not whether the moved node crossed a system sibling that
    # was neither the anchor nor the moved node. Build siblings in the order
    # [Alpha, Inbox(system), Bravo, Charlie] -- Inbox is created lazily
    # (after Alpha already exists), so its sort_key is swapped directly to
    # land it between Alpha and Bravo, matching the shape the bug needs.
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]

    def create(title: str, slug: str) -> str:
        res = client.post(
            f"/api/v1/workspaces/{workspace_id}/tree/nodes",
            json={"section": "context", "title": title, "slug": slug},
        )
        assert res.status_code == 201, res.text
        return res.json()["data"]["id"]

    alpha = create("Cross Guard Alpha", "cross-guard-alpha")
    bravo = create("Cross Guard Bravo", "cross-guard-bravo")
    charlie = create("Cross Guard Charlie", "cross-guard-charlie")

    with page_service.session_factory.begin() as session:
        inbox = session.scalar(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == UUID(workspace_id),
                WorkspaceTreeNode.section == "context",
                WorkspaceTreeNode.is_system_node.is_(True),
                WorkspaceTreeNode.parent_id.is_not(None),
            )
        )
        alpha_node = session.get(WorkspaceTreeNode, UUID(alpha))
        assert inbox is not None
        # Natural creation order is [Inbox, Alpha, Bravo, Charlie] (Inbox
        # already existed). Swap Inbox and Alpha's sort_keys so the sibling
        # order becomes [Alpha, Inbox, Bravo, Charlie].
        inbox.sort_key, alpha_node.sort_key = alpha_node.sort_key, inbox.sort_key
        inbox_id = str(inbox.id)
        inbox_sort_key_before = inbox.sort_key

    def relative_order() -> list[str]:
        nodes = client.get(
            f"/api/v1/workspaces/{workspace_id}/tree?section=context"
        ).json()["data"]["nodes"]
        ordered = sorted(
            (node for node in nodes if node["id"] in {alpha, inbox_id, bravo, charlie}),
            key=lambda node: (node["sort_key"], node["title"], node["id"]),
        )
        return [node["id"] for node in ordered]

    assert relative_order() == [alpha, inbox_id, bravo, charlie]

    # (2) Anchor form: moving Charlie before Alpha crosses Inbox -- must be
    # refused with the SAME error_code the direction form uses.
    crossing = client.post(
        f"/api/v1/workspaces/{workspace_id}/tree/reorder",
        json={"node_id": charlie, "anchor_node_id": alpha, "placement": "before"},
    )
    assert crossing.status_code == 403, crossing.text
    assert crossing.json()["error_code"] == "system_tree_node_protected"

    # (3) The rejected call must not have moved anything, and Inbox's
    # sort_key must be byte-identical (transaction rolled back).
    assert relative_order() == [alpha, inbox_id, bravo, charlie]
    with page_service.session_factory() as session:
        assert (
            session.get(WorkspaceTreeNode, UUID(inbox_id)).sort_key
            == inbox_sort_key_before
        )

    # (1) Direction form: the equivalent move (Bravo, which is directly below
    # Inbox, moving up) is refused the same way -- fixes this branch's fixed
    # baseline behavior.
    direction_blocked = client.post(
        f"/api/v1/workspaces/{workspace_id}/tree/reorder",
        json={"node_id": bravo, "direction": "up"},
    )
    assert direction_blocked.status_code == 403
    assert direction_blocked.json()["error_code"] == "system_tree_node_protected"

    # (4) Control: an anchor reorder among non-system siblings that does NOT
    # cross Inbox still succeeds -- proves the fix does not just refuse the
    # anchor form outright.
    allowed = client.post(
        f"/api/v1/workspaces/{workspace_id}/tree/reorder",
        json={"node_id": charlie, "anchor_node_id": bravo, "placement": "before"},
    )
    assert allowed.status_code == 200, allowed.text
    assert relative_order() == [alpha, inbox_id, charlie, bravo]


def test_read_api_reorder_rejects_ambiguous_or_ineligible_anchor(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]

    def create(
        title: str,
        slug: str,
        *,
        section: str = "context",
        parent_id: str | None = None,
        target_workspace_id: str = workspace_id,
    ) -> dict:
        response = client.post(
            f"/api/v1/workspaces/{target_workspace_id}/tree/nodes",
            json={
                "section": section,
                "title": title,
                "slug": slug,
                **({"parent_id": parent_id} if parent_id else {}),
            },
        )
        assert response.status_code == 201, response.text
        return response.json()["data"]

    moving = create("Eligibility Moving", "eligibility-moving")
    sibling = create("Eligibility Sibling", "eligibility-sibling")
    other_section = create(
        "Eligibility Agent",
        "eligibility-agent",
        section="agent",
    )
    context_nodes = client.get(
        f"/api/v1/workspaces/{workspace_id}/tree?section=context"
    ).json()["data"]["nodes"]
    system_sibling = next(
        node
        for node in context_nodes
        if node["is_system_node"] and node["parent_id"] == moving["parent_id"]
    )

    ambiguous = client.post(
        f"/api/v1/workspaces/{workspace_id}/tree/reorder",
        json={
            "node_id": moving["id"],
            "direction": "up",
            "anchor_node_id": sibling["id"],
            "placement": "before",
        },
    )
    assert ambiguous.status_code == 422

    for anchor_id in (other_section["id"], system_sibling["id"]):
        refused = client.post(
            f"/api/v1/workspaces/{workspace_id}/tree/reorder",
            json={
                "node_id": moving["id"],
                "anchor_node_id": anchor_id,
                "placement": "before",
            },
        )
        assert refused.status_code in {403, 404}, refused.text

    created_workspace = client.post(
        "/api/v1/workspaces",
        json={"title": "Anchor Other Workspace", "slug": "anchor-other-workspace"},
    )
    assert created_workspace.status_code == 201, created_workspace.text
    other_workspace_id = created_workspace.json()["data"]["id"]
    other_workspace_anchor = create(
        "Other Workspace Anchor",
        "other-workspace-anchor",
        target_workspace_id=other_workspace_id,
    )
    wrong_workspace = client.post(
        f"/api/v1/workspaces/{workspace_id}/tree/reorder",
        json={
            "node_id": moving["id"],
            "anchor_node_id": other_workspace_anchor["id"],
            "placement": "before",
        },
    )
    assert wrong_workspace.status_code == 404

    with page_service.session_factory.begin() as session:
        hidden = session.get(WorkspaceTreeNode, UUID(sibling["id"]))
        assert hidden is not None
        hidden.visibility_status = "archived"
    invisible = client.post(
        f"/api/v1/workspaces/{workspace_id}/tree/reorder",
        json={
            "node_id": moving["id"],
            "anchor_node_id": sibling["id"],
            "placement": "after",
        },
    )
    assert invisible.status_code in {403, 404}


def test_read_api_anchor_reorder_noop_and_single_audit(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]

    created: list[dict] = []
    for word in ("One", "Two", "Three", "Four"):
        response = client.post(
            f"/api/v1/workspaces/{workspace_id}/tree/nodes",
            json={
                "section": "context",
                "title": f"Audit {word}",
                "slug": f"audit-{word.lower()}",
            },
        )
        assert response.status_code == 201, response.text
        created.append(response.json()["data"])

    node_ids = [node["id"] for node in created]
    # The API hands back JSON strings; the column is a UUID, so the IN clause
    # needs real UUIDs or SQLAlchemy tries to read .hex off a str.
    node_uuids = [UUID(node_id) for node_id in node_ids]

    def sort_keys() -> dict[str, str]:
        with page_service.session_factory() as session:
            rows = session.scalars(
                select(WorkspaceTreeNode).where(WorkspaceTreeNode.id.in_(node_uuids))
            ).all()
            return {str(row.id): row.sort_key for row in rows}

    before_noop_keys = sort_keys()
    noop = client.post(
        f"/api/v1/workspaces/{workspace_id}/tree/reorder",
        json={
            "node_id": created[1]["id"],
            "anchor_node_id": created[0]["id"],
            "placement": "after",
        },
    )
    assert noop.status_code == 200, noop.text
    assert sort_keys() == before_noop_keys

    with page_service.session_factory() as session:
        audits_before = session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == "tree.node.reorder")
        )

    moved = client.post(
        f"/api/v1/workspaces/{workspace_id}/tree/reorder",
        json={
            "node_id": created[3]["id"],
            "anchor_node_id": created[0]["id"],
            "placement": "before",
        },
    )
    assert moved.status_code == 200, moved.text

    with page_service.session_factory() as session:
        siblings = session.scalars(
            select(WorkspaceTreeNode)
            .where(
                WorkspaceTreeNode.workspace_id == UUID(workspace_id),
                WorkspaceTreeNode.parent_id == UUID(created[0]["parent_id"]),
                WorkspaceTreeNode.visibility_status == "visible",
            )
            .order_by(WorkspaceTreeNode.sort_key)
        ).all()
        audits_after = session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == "tree.node.reorder")
        )
    assert [node.sort_key for node in siblings] == [
        f"{position:04d}" for position in range(1, len(siblings) + 1)
    ]
    assert audits_after == audits_before + 1


def test_read_api_rejects_reordering_system_tree_node(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    nodes = client.get(f"/api/v1/workspaces/{workspace_id}/tree?section=agent").json()[
        "data"
    ]["nodes"]
    system_node = next(
        node
        for node in nodes
        if node["is_system_node"] and node["parent_id"] is not None
    )

    res = client.post(
        f"/api/v1/workspaces/{workspace_id}/tree/reorder",
        json={"node_id": system_node["id"], "direction": "up"},
    )

    assert res.status_code == 403
    assert res.json()["error_code"] == "system_tree_node_protected"


@pytest.mark.usefixtures("cached_converter_health")
def test_read_api_deletes_unreferenced_library_item(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    upload = client.post(
        f"/api/v1/workspaces/{workspace_id}/library/items",
        files={"file": ("note.md", b"# Note\n\nBody", "text/markdown")},
    )
    assert upload.status_code == 201, upload.text
    item_id = upload.json()["data"]["id"]

    refs = client.get(
        f"/api/v1/workspaces/{workspace_id}/library/items/{item_id}/references"
    )
    assert refs.status_code == 200
    assert refs.json()["data"] == []

    deleted = client.delete(
        f"/api/v1/workspaces/{workspace_id}/library/items/{item_id}"
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"]["deleted"] is True

    listing = client.get(f"/api/v1/workspaces/{workspace_id}/library/items")
    assert all(item["id"] != item_id for item in listing.json()["data"])
    tree = client.get(f"/api/v1/workspaces/{workspace_id}/tree?section=library")
    assert all(
        node.get("target_id") != item_id for node in tree.json()["data"]["nodes"]
    )


def test_read_api_library_summary_includes_current_processing_status(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    with page_service.session_factory.begin() as session:
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=UUID(workspace_id),
            canonical_uri="https://example.com/converting-overview",
            title="Converting Overview",
            raw_text="conversion source",
        )
        registration.version.processing_status = "converting"
        item_id = str(registration.library_item.id)

    listing = client.get(f"/api/v1/workspaces/{workspace_id}/library/items")

    assert listing.status_code == 200, listing.text
    item = next(row for row in listing.json()["data"] if row["id"] == item_id)
    assert item["processing_status"] == "converting"


@pytest.mark.usefixtures("cached_converter_health")
def test_read_api_deletes_referenced_library_item_with_tombstone(page_service):
    from uuid import uuid4

    from sqlalchemy import select

    from backend.models.base import utc_now
    from backend.models.bundle import AgentInputBundle, AgentInputBundleItem
    from backend.models.context import (
        ContextCitation,
        ContextPage,
        ContextPageVersion,
    )
    from backend.models.library import (
        LibraryArtifact,
        LibraryArtifactConversionDetail,
        LibraryItemTombstone,
        LibraryItemVersion,
    )
    from backend.models.media import (
        BlobIncarnationRow,
        StoredBlobRow,
        WorkspaceMediaWriteIntent,
    )
    from backend.models.review import ReviewItem
    from backend.models.workspace import MCPClient

    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    upload = client.post(
        f"/api/v1/workspaces/{workspace_id}/library/items",
        files={"file": ("cited.md", b"# Cited\n\nBody", "text/markdown")},
    )
    item_id = upload.json()["data"]["id"]

    # A real Context page version that cites the uploaded source. Real citations
    # store the *page-version* id in target_id (never the page id), so this is
    # what list_item_references / the tombstone notice must resolve back to a
    # page.
    with page_service.session_factory.begin() as session:
        version = session.scalar(
            select(LibraryItemVersion).where(
                LibraryItemVersion.library_item_id == UUID(item_id)
            )
        )
        artifact = session.scalar(
            select(LibraryArtifact).where(
                LibraryArtifact.library_item_version_id == version.id
            )
        )
        if artifact is None:
            # session_factory (backend/tests/conftest.py) uses a bare
            # create_engine() with no PRAGMA foreign_keys=ON, so attempt_id
            # need not reference a real LibraryAttempt row -- but deleting
            # this item for real (below) walks every artifact's blob fields
            # and unregisters them via SqlAlchemyBlobReferenceLedger, which
            # requires a real blob_incarnation row to exist (an application
            # check, not an FK), so blob_id/epoch must be real here.
            now = utc_now()
            if session.get(WorkspaceMediaWriteIntent, version.workspace_id) is None:
                session.add(
                    WorkspaceMediaWriteIntent(
                        workspace_id=version.workspace_id,
                        state="open",
                        delete_generation=0,
                        updated_at=now,
                    )
                )
                session.flush()
            blob_id = uuid4()
            session.add(
                StoredBlobRow(
                    workspace_id=version.workspace_id,
                    id=blob_id,
                    sha256="a" * 64,
                    next_epoch=2,
                )
            )
            session.flush()
            session.add(
                BlobIncarnationRow(
                    workspace_id=version.workspace_id,
                    blob_id=blob_id,
                    blob_epoch=1,
                    byte_size=len("Body"),
                    storage_key=f"w/{version.workspace_id.hex}/test/{blob_id.hex}",
                    state="available",
                    publish_generation=1,
                    security_generation=0,
                    gc_generation=0,
                    available_at=now,
                )
            )
            session.flush()
            artifact = LibraryArtifact(
                workspace_id=version.workspace_id,
                library_item_version_id=version.id,
                attempt_id=uuid4(),
                stage="conversion",
                blob_workspace_id=version.workspace_id,
                blob_id=blob_id,
                blob_epoch=1,
                role="markdown",
                kind="text",
                media_type="text/plain",
                content_sha256="a" * 64,
                byte_size=len("Body"),
            )
            artifact.conversion_detail = LibraryArtifactConversionDetail(
                is_complete=True,
                index_text="Body",
            )
            session.add(artifact)
            session.flush()
        mcp_client = session.scalar(
            select(MCPClient).where(MCPClient.workspace_id == UUID(workspace_id))
        )
        page = ContextPage(
            workspace_id=UUID(workspace_id),
            slug="cited-page",
            title="Cited Page",
            page_type="overview",
        )
        session.add(page)
        session.flush()
        page_version = ContextPageVersion(
            workspace_id=page.workspace_id,
            page_id=page.id,
            version_no=1,
            body_md="See @cite{cited-source}.",
            content_hash="hash-cited",
            actor_type="user",
            created_by_actor_kind="user",
            created_by_actor_id="tester",
        )
        session.add(page_version)
        session.flush()
        page.current_version_id = page_version.id
        bundle = AgentInputBundle(
            workspace_id=UUID(workspace_id),
            client_id=mcp_client.id,
            task_intent="uses source slated for deletion",
            token_budget=2048,
            status="generated",
            ttl_seconds=3600,
            valid_until=utc_now(),
            invalidation_keys={},
        )
        session.add(bundle)
        session.flush()
        session.add(
            AgentInputBundleItem(
                workspace_id=bundle.workspace_id,
                bundle_id=bundle.id,
                source_kind="library_artifact_excerpt",
                library_artifact_id=artifact.id,
                priority=1,
                included_reason="test bundle reference",
            )
        )
        session.add(
            ContextCitation(
                workspace_id=page_version.workspace_id,
                target_kind="context_page",
                target_id=page_version.id,
                target_location={},
                location={},
                citation_type="raw_source",
                verification_status="weak",
                created_via="cite_tool",
                library_item_version_id=version.id,
            )
        )
        page_id = str(page.id)

    # Regression: the reference resolves to the citing page (the old filter kept
    # only target_kind == "context_page" and treated target_id as a page id).
    refs = client.get(
        f"/api/v1/workspaces/{workspace_id}/library/items/{item_id}/references"
    )
    assert refs.status_code == 200
    assert [ref["id"] for ref in refs.json()["data"]] == [page_id]

    deleted = client.delete(
        f"/api/v1/workspaces/{workspace_id}/library/items/{item_id}"
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"]["deleted"] is True

    # The source is gone from the listing...
    listing = client.get(f"/api/v1/workspaces/{workspace_id}/library/items")
    assert all(item["id"] != item_id for item in listing.json()["data"])

    # ...a tombstone preserves uri/title for render-time cite resolution...
    tombstones = client.get(f"/api/v1/workspaces/{workspace_id}/library/tombstones")
    assert tombstones.status_code == 200
    assert any(
        row["library_item_id"] == item_id and row["title"] == "cited.md"
        for row in tombstones.json()["data"]
    )

    # ...the RESTRICT citation row was removed and a pending review notice exists.
    with page_service.session_factory() as session:
        assert session.scalars(select(ContextCitation)).all() == []
        assert session.scalars(select(AgentInputBundleItem)).all() == []
        tombstone_rows = session.scalars(
            select(LibraryItemTombstone).where(
                LibraryItemTombstone.library_item_id == UUID(item_id)
            )
        ).all()
        assert len(tombstone_rows) == 1
        # Codex #14: per-citation provenance is preserved on the tombstone even
        # though the structured ContextCitation rows were hard-deleted.
        preserved = tombstone_rows[0].citations
        assert preserved and len(preserved) >= 1
        first = preserved[0]
        assert "citation_type" in first and "snippet" in first
        assert "target_id" in first and "location" in first
        notices = session.scalars(
            select(ReviewItem).where(ReviewItem.review_type == "source_deleted_notice")
        ).all()
        assert len(notices) == 1
        assert notices[0].status == "approved"


@pytest.mark.usefixtures("cached_converter_health")
def test_read_api_uploads_library_item(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]

    uploaded = client.post(
        f"/api/v1/workspaces/{workspace_id}/library/items",
        files={"file": ("notes.md", b"# Notes\n\nEvidence", "text/markdown")},
    )

    assert uploaded.status_code == 201
    source = uploaded.json()["data"]
    assert source["title"] == "notes.md"
    assert source["connector_type"] == "manual_upload"
    assert source["format_hint"] == "markdown"
    assert source["current_version"] is not None

    tree = client.get(f"/api/v1/workspaces/{workspace_id}/tree?section=library")
    assert tree.status_code == 200
    assert any(
        node["target_kind"] == "library_item" and node["target_id"] == source["id"]
        for node in tree.json()["data"]["nodes"]
    )

    # P4.2: no filename-derived upsert -- a second POST to library/items
    # with the SAME filename is a brand-new, unrelated item, never a merge.
    reuploaded = client.post(
        f"/api/v1/workspaces/{workspace_id}/library/items",
        files={"file": ("notes.md", b"# Notes\n\nRevised", "text/markdown")},
    )

    assert reuploaded.status_code == 201, reuploaded.text
    assert reuploaded.json()["data"]["id"] != source["id"]

    # Adding a version to the ORIGINAL item is instead an explicit command
    # naming that exact item -- never a filename lookup.
    new_version = client.post(
        f"/api/v1/workspaces/{workspace_id}/library/items/{source['id']}/versions",
        files={
            "file": ("notes.md", b"# Notes\n\nExplicit new version", "text/markdown")
        },
    )

    assert new_version.status_code == 201, new_version.text
    assert new_version.json()["data"]["id"] == source["id"]
    assert (
        new_version.json()["data"]["current_version"]["id"]
        != source["current_version"]["id"]
    )


@pytest.mark.usefixtures("cached_converter_health")
def test_read_api_upload_places_item_under_parent(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]

    folder = client.post(
        f"/api/v1/workspaces/{workspace_id}/tree/nodes",
        json={"section": "library", "title": "Sources", "node_kind": "folder"},
    )
    assert folder.status_code in (200, 201), folder.text
    folder_id = folder.json()["data"]["id"]

    uploaded = client.post(
        f"/api/v1/workspaces/{workspace_id}/library/items",
        files={"file": ("placed.md", b"# Placed\n\nBody", "text/markdown")},
        data={"parent_node_id": folder_id},
    )
    assert uploaded.status_code == 201, uploaded.text
    source_id = uploaded.json()["data"]["id"]

    nodes = client.get(
        f"/api/v1/workspaces/{workspace_id}/tree?section=library"
    ).json()["data"]["nodes"]
    item_node = next(
        node
        for node in nodes
        if node["target_kind"] == "library_item" and node["target_id"] == source_id
    )
    assert item_node["parent_id"] == folder_id


@pytest.mark.usefixtures("cached_converter_health")
def test_read_api_uses_library_policy_upload_limit(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]

    policies = client.get(
        f"/api/v1/workspaces/{workspace_id}/settings/policies"
    ).json()["data"]
    updated = client.patch(
        f"/api/v1/workspaces/{workspace_id}/settings/policies/batch",
        json={
            "group": "library",
            "expected_revision": policies["library"]["revision"],
            "changes": {"max_upload_bytes": 1000},
        },
    )

    assert updated.status_code == 200
    assert updated.json()["data"]["library"]["policy"]["max_upload_bytes"] == 1000

    rejected = client.post(
        f"/api/v1/workspaces/{workspace_id}/library/items",
        files={"file": ("notes.md", b"#" * 1001, "text/markdown")},
    )

    assert rejected.status_code == 413
    body = rejected.json()
    assert body["error_code"] == "library_upload_too_large"
    assert body["limit_bytes"] == 1000
    assert body["settings_link"] == (
        f"/workspaces/{workspace_id}/settings?section=library"
    )
    assert "1 KB" in body["message"]


@pytest.mark.usefixtures("cached_converter_health")
def test_read_api_rejects_library_upload_for_workspace_viewer(page_service):
    from sqlalchemy import select

    from backend.models.user import User
    from backend.policy.rbac import grant_workspace_membership
    from backend.scripts.seed_dev import seed_default_workspace

    viewer = make_client(page_service, email="viewer@example.com")
    with page_service.session_factory.begin() as session:
        viewer_user = session.scalar(
            select(User).where(User.email == "viewer@example.com")
        )
        assert viewer_user is not None
        target_workspace = seed_default_workspace(
            session,
            slug="viewer-upload-target",
            title="Viewer Upload Target",
        )
        grant_workspace_membership(
            session=session,
            user_id=viewer_user.id,
            workspace_id=target_workspace.id,
            role="viewer",
        )
        workspace_id = target_workspace.id

    denied = viewer.post(
        f"/api/v1/workspaces/{workspace_id}/library/items",
        files={"file": ("notes.md", b"# Notes", "text/markdown")},
    )

    assert denied.status_code == 403
    assert denied.json()["error_code"] in {
        "scope_denied",
        "workspace_role_insufficient",
    }


def test_source_endpoints_reject_cross_workspace_identifier_pair(
    page_service, media_installer_factory
):
    """Fix-6 Phase 4 — REST Source identifier consistency matrix.

    When the URL workspace_id and the library_item_id (or version_id, artifact_id)
    point to different workspaces, the response must be 404 `not_found` —
    the same response a non-existent source would get. Returning a richer
    response (e.g. 403 with the resource owner workspace) would leak the
    existence of cross-workspace resources to an actor that only holds
    membership in the URL workspace.

    This is the Source-side counterpart to Fix-6 Task 1.1 (MCP read_page
    inconsistent_identifier). Because REST sources are loaded by primary
    key directly (no slug ambiguity), the service-layer guard already
    enforces consistency by checking `source.workspace_id !=
    workspace_id`; this test pins that behaviour as a regression gate.
    """
    from sqlalchemy import select

    from backend.models.user import User
    from backend.policy.rbac import grant_workspace_membership
    from backend.scripts.seed_dev import seed_default_workspace

    client = make_client(page_service)
    with page_service.session_factory.begin() as session:
        default_workspace = session.query(Workspace).filter_by(slug="default").one()
        foreign_workspace = seed_default_workspace(
            session,
            slug="fix6-source-foreign",
            title="Fix-6 Source Foreign",
        )
        # Authenticated test client user must be a member of BOTH workspaces
        # so the auth dependency does not short-circuit on membership before
        # the identifier consistency check fires.
        user = session.scalars(select(User)).first()
        grant_workspace_membership(
            session,
            user_id=user.id,
            workspace_id=foreign_workspace.id,
            role="owner",
        )
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=foreign_workspace.id,
            canonical_uri="https://example.com/foreign-source",
            title="Foreign Source",
            raw_text="# Foreign Source\n\nSecret artifact",
        )
        artifact = ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )
        default_workspace_id = str(default_workspace.id)
        foreign_library_item_id = str(registration.library_item.id)
        foreign_version_id = str(registration.version.id)
        foreign_artifact_id = str(artifact.id)

    cross_source = client.get(
        f"/api/v1/workspaces/{default_workspace_id}/library/items/{foreign_library_item_id}",
    )
    cross_version = client.get(
        f"/api/v1/workspaces/{default_workspace_id}/library/versions/{foreign_version_id}",
    )
    cross_artifact = client.get(
        f"/api/v1/workspaces/{default_workspace_id}/library/artifacts/{foreign_artifact_id}",
    )

    assert cross_source.status_code == 404
    assert cross_source.json()["error_code"] == "not_found"
    assert cross_version.status_code == 404
    assert cross_version.json()["error_code"] == "not_found"
    assert cross_artifact.status_code == 404
    assert cross_artifact.json()["error_code"] == "not_found"


def _register_restricted_library_item(
    page_service,
    workspace_id: UUID,
    media_installer_factory,
) -> tuple[str, str, str]:
    with page_service.session_factory.begin() as session:
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace_id,
            canonical_uri="https://example.com/restricted-source",
            title="Restricted Source",
            trust_level="restricted",
            raw_text="# Restricted Source\n\nSecret restricted body",
        )
        artifact = ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )
        return (
            str(registration.library_item.id),
            str(registration.version.id),
            str(artifact.id),
        )


def test_read_api_allows_restricted_library_reads_for_workspace_owner(
    page_service, media_installer_factory
):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    item_id, version_id, artifact_id = _register_restricted_library_item(
        page_service, UUID(workspace_id), media_installer_factory
    )

    listing = client.get(f"/api/v1/workspaces/{workspace_id}/library/items")
    assert listing.status_code == 200
    assert any(item["id"] == item_id for item in listing.json()["data"])

    detail = client.get(f"/api/v1/workspaces/{workspace_id}/library/items/{item_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["trust_level"] == "restricted"

    version = client.get(
        f"/api/v1/workspaces/{workspace_id}/library/versions/{version_id}"
    )
    assert version.status_code == 200, version.text

    artifact = client.get(
        f"/api/v1/workspaces/{workspace_id}/library/artifacts/{artifact_id}"
    )
    assert artifact.status_code == 200, artifact.text


def test_read_api_blocks_restricted_library_reads_for_workspace_viewer(
    page_service, media_installer_factory
):
    from sqlalchemy import select

    from backend.models.user import User
    from backend.policy.rbac import grant_workspace_membership

    owner = make_client(page_service)
    workspace_id = owner.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    item_id, version_id, artifact_id = _register_restricted_library_item(
        page_service, UUID(workspace_id), media_installer_factory
    )

    viewer = make_client(page_service, email="restricted-viewer@example.com")
    with page_service.session_factory.begin() as session:
        viewer_user = session.scalar(
            select(User).where(User.email == "restricted-viewer@example.com")
        )
        assert viewer_user is not None
        grant_workspace_membership(
            session=session,
            user_id=viewer_user.id,
            workspace_id=UUID(workspace_id),
            role="viewer",
        )

    listing = viewer.get(f"/api/v1/workspaces/{workspace_id}/library/items")
    assert listing.status_code == 200
    listing_body = listing.json()
    assert all(item["id"] != item_id for item in listing_body["data"])
    # The aggregate `total` must be permission-filtered so the count cannot
    # leak the existence of restricted items the viewer may not see.
    assert listing_body["total"] == 0
    assert listing_body["has_more"] is False

    restricted_listing = viewer.get(
        f"/api/v1/workspaces/{workspace_id}/library/items?trust=restricted"
    )
    assert restricted_listing.status_code == 200
    assert restricted_listing.json()["data"] == []
    assert restricted_listing.json()["total"] == 0

    detail = viewer.get(f"/api/v1/workspaces/{workspace_id}/library/items/{item_id}")
    assert detail.status_code == 403, detail.text
    assert detail.json()["error_code"] == "restricted_library_denied"

    version = viewer.get(
        f"/api/v1/workspaces/{workspace_id}/library/versions/{version_id}"
    )
    assert version.status_code == 403, version.text
    assert version.json()["error_code"] == "restricted_library_denied"

    artifact = viewer.get(
        f"/api/v1/workspaces/{workspace_id}/library/artifacts/{artifact_id}"
    )
    assert artifact.status_code == 403, artifact.text
    assert artifact.json()["error_code"] == "restricted_library_denied"


def test_read_api_allows_restricted_library_reads_for_owner_pat(
    page_service,
    media_installer_factory,
    mcp_default_actor,
):
    from backend.policy.rest_scopes import require_token_scope

    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    item_id, version_id, artifact_id = _register_restricted_library_item(
        page_service, UUID(workspace_id), media_installer_factory
    )
    owner_pat = mcp_default_actor(
        actor_id="restricted-library-owner-pat",
        scopes=("mcp:read",),
    )
    client.app.dependency_overrides[require_token_scope] = lambda: owner_pat
    try:
        listing = client.get(f"/api/v1/workspaces/{workspace_id}/library/items")
        assert listing.status_code == 200
        assert any(item["id"] == item_id for item in listing.json()["data"])

        detail = client.get(
            f"/api/v1/workspaces/{workspace_id}/library/items/{item_id}"
        )
        assert detail.status_code == 200, detail.text

        version = client.get(
            f"/api/v1/workspaces/{workspace_id}/library/versions/{version_id}"
        )
        assert version.status_code == 200, version.text

        artifact = client.get(
            f"/api/v1/workspaces/{workspace_id}/library/artifacts/{artifact_id}"
        )
        assert artifact.status_code == 200, artifact.text
    finally:
        client.app.dependency_overrides.clear()


def _seed_api_context_pages(page_service, workspace_id: UUID, count: int) -> None:
    from datetime import UTC, datetime, timedelta

    from backend.models.context import ContextPage, ContextPageVersion

    base = datetime(2026, 1, 1, tzinfo=UTC)
    with page_service.session_factory.begin() as session:
        for index in range(count):
            page = ContextPage(
                workspace_id=workspace_id,
                slug=f"api-keyset-context-{index:03d}",
                title=f"API Keyset Context {index:03d}",
                page_type="overview",
                lifecycle_status="published",
                updated_at=base + timedelta(minutes=index),
            )
            session.add(page)
            session.flush()
            version = ContextPageVersion(
                workspace_id=workspace_id,
                page_id=page.id,
                version_no=1,
                created_at=base + timedelta(minutes=index),
                body_md=f"# {page.title}\n\nPublished body.",
                content_hash=f"api-keyset-context-{index:03d}",
                actor_type="system",
                actor_id=None,
                created_by_actor_kind="system",
                created_by_actor_id="test-system",
                version_status="published_snapshot",
            )
            session.add(version)
            session.flush()
            page.current_version_id = version.id
            page.current_published_version_id = version.id


def test_read_api_context_pages_paginate_with_cursor(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    _seed_api_context_pages(page_service, UUID(workspace_id), 23)

    first = client.get(f"/api/v1/workspaces/{workspace_id}/context/pages?limit=10")
    assert first.status_code == 200
    first_body = first.json()
    assert len(first_body["data"]) == 10
    assert first_body["total"] == 23
    assert first_body["has_more"] is True
    assert first_body["next_cursor"]

    second = client.get(
        f"/api/v1/workspaces/{workspace_id}/context/pages"
        f"?limit=10&cursor={first_body['next_cursor']}"
    )
    assert second.status_code == 200
    second_body = second.json()
    assert len(second_body["data"]) == 10
    assert second_body["has_more"] is True

    first_ids = {item["id"] for item in first_body["data"]}
    second_ids = {item["id"] for item in second_body["data"]}
    assert first_ids.isdisjoint(second_ids)

    third = client.get(
        f"/api/v1/workspaces/{workspace_id}/context/pages"
        f"?limit=10&cursor={second_body['next_cursor']}"
    )
    third_body = third.json()
    assert len(third_body["data"]) == 3
    assert third_body["has_more"] is False
    assert third_body["next_cursor"] is None


def test_read_api_context_pages_tolerate_garbled_cursor(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    _seed_api_context_pages(page_service, UUID(workspace_id), 12)

    first = client.get(
        f"/api/v1/workspaces/{workspace_id}/context/pages?limit=10"
    ).json()
    garbled = client.get(
        f"/api/v1/workspaces/{workspace_id}/context/pages"
        "?limit=10&cursor=not-a-real-cursor"
    )
    assert garbled.status_code == 200
    assert [item["id"] for item in garbled.json()["data"]] == [
        item["id"] for item in first["data"]
    ]


def test_read_api_agent_pages_recency_order_and_envelope(page_service):
    from datetime import UTC, datetime, timedelta

    from backend.models.agent import AgentPage

    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    base = datetime(2026, 2, 1, tzinfo=UTC)
    with page_service.session_factory.begin() as session:
        for index in range(12):
            session.add(
                AgentPage(
                    workspace_id=UUID(workspace_id),
                    category="skill",
                    title=f"API Keyset Agent {index:03d}",
                    slug=f"api-keyset-agent-{index:03d}",
                    lifecycle_status="active",
                    priority=index,
                    updated_at=base + timedelta(minutes=index),
                )
            )

    first = client.get(f"/api/v1/workspaces/{workspace_id}/agent/pages?limit=10")
    assert first.status_code == 200
    body = first.json()
    timestamps = [item["updated_at"] for item in body["data"]]
    assert timestamps == sorted(timestamps, reverse=True)
    assert body["has_more"] is True
    assert body["next_cursor"]


@pytest.mark.usefixtures("cached_converter_health")
def test_read_api_rejects_malformed_pdf_and_docx_library_uploads(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]

    for filename, content_type, content in (
        ("report.pdf", "application/pdf", b"%PDF-1.7 truncated"),
        (
            "notes.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            b"PK\x03\x04corrupt-zip-container",
        ),
    ):
        rejected = client.post(
            f"/api/v1/workspaces/{workspace_id}/library/items",
            files={"file": (filename, content, content_type)},
        )
        assert rejected.status_code == 422, rejected.text
        body = rejected.json()
        assert body["error_code"] == "library_container_invalid"

    accepted = client.post(
        f"/api/v1/workspaces/{workspace_id}/library/items",
        files={"file": ("plain.txt", b"plain text body", "text/plain")},
    )
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["data"]["format_hint"] == "text"


@pytest.mark.usefixtures("cached_converter_health")
def test_read_api_rejects_extensionless_and_dotfile_library_uploads(page_service):
    client = make_client(page_service)
    workspace_id = client.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]

    for filename, content_type in (
        ("binaryblob", "application/octet-stream"),
        (".env", "text/plain"),
    ):
        rejected = client.post(
            f"/api/v1/workspaces/{workspace_id}/library/items",
            files={"file": (filename, b"SECRET=value", content_type)},
        )
        assert rejected.status_code == 422, rejected.text
        body = rejected.json()
        assert body["error_code"] == "unsupported_library_upload"

    listing = client.get(f"/api/v1/workspaces/{workspace_id}/library/items")
    assert all(
        item["title"] not in {"binaryblob", ".env"} for item in listing.json()["data"]
    )


def test_activity_feed_splits_mcp_client_actor_into_ai_name_and_credential_owner(
    page_service,
):
    # An mcp_client Activity row used to collapse to the generic constant
    # "MCP client" for every registered AI tool, with no way to tell which
    # human's access it ran under. list_workspace_activity must now resolve
    # both: the registered client name and, when known, its credential owner.
    from backend.models.user import User

    owner = make_client(page_service)
    workspace_id = owner.get("/api/v1/workspaces").json()["data"][
        "current_workspace_id"
    ]
    with page_service.session_factory.begin() as session:
        owner_user = session.scalar(
            select(User).where(User.email == "test@example.com")
        )
        named_client = MCPClient(
            workspace_id=UUID(workspace_id),
            name="Claude Desktop",
            client_type="generic",
            issued_to_user_id=owner_user.id,
        )
        orphaned_client = MCPClient(
            workspace_id=UUID(workspace_id),
            name="Legacy Key",
            client_type="generic",
            issued_to_user_id=None,
        )
        session.add_all([named_client, orphaned_client])
        session.flush()
        session.add_all(
            [
                AuditEvent(
                    workspace_id=UUID(workspace_id),
                    actor_kind="mcp_client",
                    actor_id=str(named_client.id),
                    actor_type="mcp_client",
                    action="context_page.example_write",
                    target_kind="workspace",
                    target_id=UUID(workspace_id),
                ),
                AuditEvent(
                    workspace_id=UUID(workspace_id),
                    actor_kind="mcp_client",
                    actor_id=str(orphaned_client.id),
                    actor_type="mcp_client",
                    action="context_page.example_write",
                    target_kind="workspace",
                    target_id=UUID(workspace_id),
                ),
            ]
        )

    with page_service.session_factory() as session:
        owner_display_name = session.scalar(
            select(User.display_name).where(User.email == "test@example.com")
        )

    activity = owner.get(f"/api/v1/workspaces/{workspace_id}/activity")
    assert activity.status_code == 200
    items = activity.json()["data"]["items"]
    actors_by_name = {
        item["actor"]["display_name"]: item["actor"]
        for item in items
        if item["actor"] is not None and item["actor"]["actor_kind"] == "mcp_client"
    }

    assert (
        actors_by_name["Claude Desktop"]["credential_owner_display_name"]
        == owner_display_name
    )
    assert actors_by_name["Legacy Key"]["credential_owner_display_name"] is None

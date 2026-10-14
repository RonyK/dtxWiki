import time

import pytest
from sqlalchemy import select

from backend.mcp.router import dispatch
from backend.models.agent import AgentPage, AgentPageVersion
from backend.models.context import EdgeProjection
from backend.models.coverage import KnowledgeCoverageGap
from backend.models.user import User
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.policy.rbac import grant_workspace_membership
from backend.schemas.read import SEARCH_QUERY_MAX_LENGTH
from backend.scripts.seed_dev import DEFAULT_AGENT_PAGE_SLUG
from backend.services.extraction_service import ExtractionService
from backend.services.library_service import LibraryService
from backend.services.retrieval.outline_location import raw_body_sha256
from backend.utils.content_hash import content_hash as calculate_content_hash


@pytest.fixture
def actor(mcp_default_actor):
    # Fix-6 Task 1.2: mcp_client must operate inside own workspace.
    return mcp_default_actor(
        scopes=(
            "guide",
            "search",
            "read",
            "audit-read",
            "agent-page-read",
            "draft-write",
        ),
    )


def _workspace_id(page_service):
    with page_service.session_factory() as session:
        return session.scalar(select(Workspace).where(Workspace.slug == "default")).id


def test_mcp_lists_actor_workspaces(page_service):
    with page_service.session_factory.begin() as session:
        user = User(
            email="mcp-user@example.com",
            password_hash="unused",
            display_name="MCP User",
            status="active",
        )
        session.add(user)
        session.flush()
        workspace = session.scalar(
            select(Workspace).where(Workspace.slug == "default")
        )
        assert workspace is not None
        grant_workspace_membership(
            session,
            user_id=user.id,
            workspace_id=workspace.id,
            role="owner",
        )
        user_id = user.id

    actor = ActorContext(
        actor_kind="human",
        actor_id=str(user_id),
        scopes=("workspace-read",),
        user_id=user_id,
    )
    response = dispatch(
        "list_my_workspaces",
        {},
        actor=actor,
        page_service=page_service,
    )

    assert response.envelope.result_status == "succeeded"
    assert [workspace.slug for workspace in response.data.workspaces] == ["default"]
    assert response.data.workspaces[0].role == "owner"


def test_mcp_guide_and_agent_read_tools(page_service, actor, mcp_default_actor):
    workspace_id = _workspace_id(page_service)

    guide = dispatch(
        "guide",
        {"workspace_id": str(workspace_id), "detail_level": "brief"},
        actor=actor,
        page_service=page_service,
    )
    categories = dispatch(
        "list_agent_categories",
        {"workspace_id": str(workspace_id)},
        actor=actor,
        page_service=page_service,
    )
    sensitive_actor = mcp_default_actor(scopes=("mcp:write",))
    agent_page = dispatch(
        "read_agent_page",
        {"workspace_id": str(workspace_id), "slug": DEFAULT_AGENT_PAGE_SLUG},
        actor=sensitive_actor,
        page_service=page_service,
    )

    assert guide.envelope.result_status == "succeeded"
    assert guide.data.workspace_summary["slug"] == "default"
    assert guide.data.available_tools == [
        "guide",
        "search",
        "fetch",
        "grep_wiki",
        "read",
        "list_agent_categories",
    ]
    assert categories.data[0].category == "agent_guide"
    assert agent_page.data.body_markdown.startswith("# Default System Prompt")
    assert agent_page.data.tree_path[-1].title == "Default System Prompt"


def test_mcp_search_read_and_list_changes_tools(page_service, actor):
    workspace_id = _workspace_id(page_service)
    # Seed synchronously-published content via the PageService method
    # directly (the same call the deprecated write_page MCP tool used to
    # make) rather than write_draft, which stages a review-gated draft that
    # is not immediately visible to search/read/list_changes.
    written = page_service.write_page(
        workspace_slug="default",
        slug="mcp-read-context",
        title="MCP Read Context",
        body_md="# MCP Read Context\n\nNeedle body",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )

    search = dispatch(
        "search",
        {
            "workspace_id": str(workspace_id),
            "query": "needle",
            "scope": "context_page",
        },
        actor=actor,
        page_service=page_service,
    )
    read = dispatch(
        "read",
        {
            "workspace_id": str(workspace_id),
            "target_type": "context_page",
            "target_id": str(written.id),
        },
        actor=actor,
        page_service=page_service,
    )
    changes = dispatch(
        "list_changes",
        {
            "workspace_id": str(workspace_id),
            "target_type": "context_page",
            "target_id": str(written.id),
            "include_diff": True,
        },
        actor=actor,
        page_service=page_service,
    )

    assert search.envelope.result_status == "succeeded"
    assert search.data.ranked_results[0].type == "context_page"
    assert read.data["body_markdown"] == "# MCP Read Context\n\nNeedle body"
    assert changes.data.changesets[0].status == "applied"
    assert changes.data.diffs[0]["target_type"] == "context_page"


def test_mcp_read_concept_resolves_alias_redirect_and_ambiguity(page_service, actor):
    workspace_id = _workspace_id(page_service)
    alpha = page_service.write_page(
        workspace_slug="default",
        slug="canonical-alpha",
        title="Canonical Alpha",
        body_md="# Canonical Alpha\n\nAlpha body",
        aliases=["Shared Alias", "Alpha Shortcut"],
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    beta = page_service.write_page(
        workspace_slug="default",
        slug="canonical-beta",
        title="Canonical Beta",
        body_md="# Canonical Beta\n\nBeta body",
        aliases=["Shared Alias"],
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    with page_service.session_factory.begin() as session:
        session.add(
            EdgeProjection(
                workspace_id=workspace_id,
                source_kind="context_page",
                source_id=alpha.id,
                target_kind="context_page",
                target_id=beta.id,
                edge_kind="co_access",
                provenance="usage",
                weight=3.0,
                source_ref={},
            )
        )

    redirected = dispatch(
        "read_concept",
        {
            "workspace_id": str(workspace_id),
            "path": "/context/Alpha Shortcut.md",
        },
        actor=actor,
        page_service=page_service,
    )
    ambiguous = dispatch(
        "read_concept",
        {
            "workspace_id": str(workspace_id),
            "path": "/context/Shared Alias.md",
        },
        actor=actor,
        page_service=page_service,
    )
    mermaid = dispatch(
        "read_concept",
        {
            "workspace_id": str(workspace_id),
            "path": "/context/canonical-alpha.md",
            "format": "mermaid",
        },
        actor=actor,
        page_service=page_service,
    )

    assert redirected.envelope.result_status == "succeeded"
    assert redirected.status == "redirected"
    assert redirected.canonical_path == "/context/canonical-alpha.md"
    assert redirected.page_id is not None
    assert redirected.version_id is not None
    assert "# Canonical Alpha" in redirected.content
    assert redirected.mermaid is None
    assert redirected.neighbors is not None
    # MCP read tools surface co_access like any other edge kind. The relation
    # spent this branch behind a k-anonymity gate that a workspace of named
    # teammates can never satisfy, so the gate only kept it empty; what has to
    # hold is that the actor can read the neighbour, which hydration enforces.
    assert [item.id for item in redirected.neighbors.co_access] == [beta.id]
    assert redirected.neighbors.co_access[0].edge_kind == "co_access"
    assert mermaid.mermaid is not None
    assert mermaid.mermaid.startswith("graph LR")
    assert ambiguous.envelope.result_status == "succeeded"
    assert ambiguous.status == "ambiguous"
    assert {candidate.concept_path for candidate in ambiguous.candidates} == {
        "/context/canonical-alpha.md",
        "/context/canonical-beta.md",
    }
    assert set(type(ambiguous.candidates[0]).model_fields) == {
        "id",
        "title",
        "concept_path",
        "match_reason",
    }


def test_read_context_outline_duplicate_sections_and_selected_hash(
    page_service, actor
):
    workspace_id = _workspace_id(page_service)
    body = "# Root\n\nIntro\n\n## Repeat\n\nfirst\n\n## Repeat\n\nsecond\n"
    page = page_service.write_page(
        workspace_slug="default",
        slug="outline-duplicates",
        title="Outline Duplicates",
        body_md=body,
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )

    outline = dispatch(
        "read_context_outline",
        {
            "workspace_id": str(workspace_id),
            "target": {"page_id": str(page.id)},
        },
        actor=actor,
        page_service=page_service,
    )

    repeats = [entry for entry in outline.outline if entry.title == "Repeat"]
    assert len(repeats) == 2
    assert repeats[0].section_id != repeats[1].section_id
    assert repeats[0].heading_path == ["Root", "Repeat"]
    assert repeats[1].heading_path == ["Root", "Repeat"]
    assert outline.content_trust == "untrusted_user_content"

    selected = dispatch(
        "read_context_outline",
        {
            "workspace_id": str(workspace_id),
            "target": {"page_id": str(page.id)},
            "select": {
                "by": "section_id",
                "section_id": repeats[1].section_id,
            },
        },
        actor=actor,
        page_service=page_service,
    )

    raw_slice = "## Repeat\n\nsecond\n"
    assert selected.section.content == raw_slice
    assert selected.section.section_hash == calculate_content_hash(raw_slice)
    assert selected.section.anchor_status == "exact"
    assert selected.section.truncated is False
    assert selected.section.next_cursor is None


def test_read_context_outline_cursor_round_trip_and_version_binding(
    page_service, actor
):
    workspace_id = _workspace_id(page_service)
    body = "# Root\n\n" + "abcdefghij" * 4
    page = page_service.write_page(
        workspace_slug="default",
        slug="outline-cursor",
        title="Outline Cursor",
        body_md=body,
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    request = {
        "workspace_id": str(workspace_id),
        "target": {"page_id": str(page.id)},
        "select": {"by": "section_id", "section_id": "root"},
        "max_chars": 12,
    }

    first = dispatch(
        "read_context_outline",
        request,
        actor=actor,
        page_service=page_service,
    )
    assert first.section.truncated is True
    assert first.section.next_cursor

    parts = [first.section.content]
    cursor = first.section.next_cursor
    while cursor is not None:
        page_response = dispatch(
            "read_context_outline",
            {**request, "cursor": cursor},
            actor=actor,
            page_service=page_service,
        )
        parts.append(page_response.section.content)
        cursor = page_response.section.next_cursor
    assert "".join(parts) == body

    page_service.write_page(
        workspace_slug="default",
        slug="outline-cursor",
        title="Outline Cursor",
        body_md=body + "\nnew visible version",
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
        base_version_id=page.version_id,
        base_content_hash=page.content_hash,
    )
    rejected = dispatch(
        "read_context_outline",
        {**request, "cursor": first.section.next_cursor},
        actor=actor,
        page_service=page_service,
    )
    assert rejected.envelope.result_status == "failed"
    assert rejected.envelope.error_code == "validation_failed"


def test_read_context_outline_hides_frontmatter_and_separates_raw_hash(
    page_service, actor
):
    workspace_id = _workspace_id(page_service)
    body = "---\nsecret: true\n---\n# Visible\n\nbody\n"
    page = page_service.write_page(
        workspace_slug="default",
        slug="outline-frontmatter",
        title="Outline Frontmatter",
        body_md=body,
        actor_kind="human",
        actor_id="00000000-0000-0000-0000-000000000010",
        actor_provenance="browser_session",
    )
    response = dispatch(
        "read_context_outline",
        {
            "workspace_id": str(workspace_id),
            "target": {"page_id": str(page.id)},
        },
        actor=actor,
        page_service=page_service,
    )
    assert [entry.title for entry in response.outline] == ["Visible"]

    frontmatter_select = dispatch(
        "read_context_outline",
        {
            "workspace_id": str(workspace_id),
            "target": {"page_id": str(page.id)},
            "select": {
                "by": "anchor",
                "anchor_text": "secret: true",
                "context_before": "---\n",
                "context_after": "\n---",
                "char_offset_hint": body.index("secret: true"),
            },
        },
        actor=actor,
        page_service=page_service,
    )
    assert frontmatter_select.envelope.result_status == "failed"
    assert frontmatter_select.envelope.error_code == "not_found"

    a = "# Title\n\nHello world\n\n## Sec\n\nbody\n"
    b = "# Title  \r\n\r\nHello world   \r\n\r\n## Sec\r\n\r\nbody\r\n\r\n\r\n"
    assert calculate_content_hash(a) == calculate_content_hash(b)
    assert raw_body_sha256(a) != raw_body_sha256(b)

    raw_pages = []
    for suffix, raw_body in (("a", a), ("b", b)):
        created = page_service.write_page(
            workspace_slug="default",
            slug=f"outline-raw-{suffix}",
            title=f"Outline Raw {suffix.upper()}",
            body_md=raw_body,
            actor_kind="human",
            actor_id="00000000-0000-0000-0000-000000000010",
            actor_provenance="browser_session",
        )
        raw_pages.append(
            dispatch(
                "read_context_outline",
                {
                    "workspace_id": str(workspace_id),
                    "target": {"page_id": str(created.id)},
                },
                actor=actor,
                page_service=page_service,
            )
        )
    assert raw_pages[0].document.content_hash == raw_pages[1].document.content_hash
    assert (
        raw_pages[0].document.body_raw_sha256
        != raw_pages[1].document.body_raw_sha256
    )


def test_mcp_grep_wiki_returns_visible_line_context(page_service, actor):
    workspace_id = _workspace_id(page_service)
    page_service.write_page(
        workspace_slug="default",
        slug="grep-target",
        title="Grep Target",
        body_md="# Grep Target\n\nBefore\nliteral/needle\nAfter",
        actor_kind=actor.actor_kind,
        actor_id=actor.actor_id,
        actor_provenance=actor.provenance,
    )

    response = dispatch(
        "grep_wiki",
        {
            "workspace_id": str(workspace_id),
            "pattern": "literal/needle",
            "context_lines": 1,
        },
        actor=actor,
        page_service=page_service,
    )

    assert response.envelope.result_status == "succeeded"
    assert response.matches[0]["concept_path"] == "/context/grep-target.md"
    assert response.matches[0]["line"] == "literal/needle"
    assert "Before" in response.matches[0]["context"]
    assert "After" in response.matches[0]["context"]


def test_mcp_grep_wiki_uses_fence_aware_full_heading_paths(page_service, actor):
    workspace_id = _workspace_id(page_service)
    page_service.write_page(
        workspace_slug="default",
        slug="grep-outline-target",
        title="Grep Outline Target",
        body_md=(
            "# Parent\n"
            "```text\n"
            "# Fake code comment\n"
            "needle in fence\n"
            "```\n"
            "## Child\n"
            "needle nested\n"
        ),
        actor_kind=actor.actor_kind,
        actor_id=actor.actor_id,
        actor_provenance=actor.provenance,
    )

    response = dispatch(
        "grep_wiki",
        {
            "workspace_id": str(workspace_id),
            "pattern": "needle",
            "context_lines": 0,
        },
        actor=actor,
        page_service=page_service,
    )

    assert response.envelope.result_status == "succeeded"
    assert [match["line"] for match in response.matches] == [
        "needle in fence",
        "needle nested",
    ]
    assert [match["heading_path"] for match in response.matches] == [
        "Parent",
        "Parent > Child",
    ]


def test_mcp_search_maps_payload_warnings_into_envelope(page_service, actor):
    """B-11: payload advisory codes surface as structured envelope warnings
    through the single mapper, so MCP clients and trace capture share the
    same channel."""

    workspace_id = _workspace_id(page_service)
    for slug in ("warn-map-a", "warn-map-b"):
        page_service.write_page(
            workspace_slug="default",
            slug=slug,
            title="Warn Map Twin",
            body_md=f"# Warn Map Twin\n\n{slug}.",
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            actor_provenance=actor.provenance,
        )

    response = dispatch(
        "search",
        {
            "workspace_id": str(workspace_id),
            "query": "Warn Map Twin",
            "scope": "context_page",
        },
        actor=actor,
        page_service=page_service,
    )

    assert response.envelope.result_status == "succeeded"
    assert "disambiguation_required" in response.data.warnings
    envelope_codes = [warning.code for warning in response.envelope.warnings]
    assert "disambiguation_required" in envelope_codes
    assert len(response.data.disambiguation.candidates) == 2

    no_hit = dispatch(
        "search",
        {
            "workspace_id": str(workspace_id),
            "query": "zzzz mcp coverage gap",
            "scope": "context_page",
        },
        actor=actor,
        page_service=page_service,
    )
    assert no_hit.data.ranked_results == []
    assert "coverage_gap_recorded" in no_hit.data.warnings
    assert "coverage_gap_recorded" in [
        warning.code for warning in no_hit.envelope.warnings
    ]
    with page_service.session_factory() as session:
        gap = (
            session.query(KnowledgeCoverageGap)
            .filter_by(
                workspace_id=workspace_id,
                surface="search_mcp",
            )
            .one()
        )
    assert gap.query_norm == "zzzz mcp coverage gap"


def test_mcp_grep_wiki_is_literal_only(page_service, actor):
    """REDOS-1: user patterns never reach the backtracking regex engine —
    a catastrophic pattern is matched literally within a bounded time, and
    requesting regex mode is a validation error, not a silent fallback."""

    workspace_id = _workspace_id(page_service)
    page_service.write_page(
        workspace_slug="default",
        slug="redos-target",
        title="ReDoS Target",
        body_md="# ReDoS Target\n\n" + "a" * 4096 + "b",
        actor_kind=actor.actor_kind,
        actor_id=actor.actor_id,
        actor_provenance=actor.provenance,
    )

    started = time.perf_counter()
    response = dispatch(
        "grep_wiki",
        {
            "workspace_id": str(workspace_id),
            "pattern": "(a+)+$",
        },
        actor=actor,
        page_service=page_service,
    )
    elapsed = time.perf_counter() - started

    assert response.envelope.result_status == "succeeded"
    assert response.matches == []  # matched literally, not as a regex
    assert elapsed < 2.0, f"literal grep took {elapsed:.1f}s"

    rejected = dispatch(
        "grep_wiki",
        {
            "workspace_id": str(workspace_id),
            "pattern": "needle",
            "literal": False,
        },
        actor=actor,
        page_service=page_service,
    )
    assert rejected.envelope.result_status == "failed"
    assert rejected.envelope.error_code == "validation_failed"


def test_mcp_read_library_artifact_and_rejects_unsupported_search(
    page_service, actor, media_installer_factory
):
    workspace_id = _workspace_id(page_service)
    with page_service.session_factory.begin() as session:
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace_id,
            canonical_uri="https://example.com/mcp-source",
            title="MCP Source",
            raw_text="# MCP Source\n\nArtifact needle",
        )
        artifact = ExtractionService(
            media_installer_factory=media_installer_factory
        ).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )

    read_artifact = dispatch(
        "read",
        {
            "workspace_id": str(workspace_id),
            "target_type": "library_artifact",
            "target_id": str(artifact.id),
        },
        actor=actor,
        page_service=page_service,
    )
    unsupported = dispatch(
        "search",
        {
            "workspace_id": str(workspace_id),
            "query": "needle",
            "retrieval_mode": "semantic",
        },
        actor=actor,
        page_service=page_service,
    )

    assert read_artifact.envelope.result_status == "succeeded"
    assert read_artifact.data["index_text"] == "# MCP Source\n\nArtifact needle"
    assert unsupported.envelope.result_status == "rejected_by_policy"
    assert unsupported.envelope.error_code == "unsupported_retrieval_mode"
    assert unsupported.data.ranked_results == []
    assert unsupported.data.supported_retrieval_modes == ["keyword"]


def test_mcp_search_rejects_overlong_query(page_service, actor):
    workspace_id = _workspace_id(page_service)

    response = dispatch(
        "search",
        {
            "workspace_id": str(workspace_id),
            "query": "x" * (SEARCH_QUERY_MAX_LENGTH + 1),
        },
        actor=actor,
        page_service=page_service,
    )

    assert response.envelope.result_status == "failed"
    assert response.envelope.error_code == "validation_failed"


def test_mcp_read_agent_page_denies_sensitive_to_non_owner(page_service):
    """Item 3 (D3-a): read_agent_page must not hand a non-owner the body of a
    sensitive-category AgentPage. The seeded Default System Prompt is
    system_prompt (sensitive), so a viewer is denied while the owner's own
    mcp_client (covered by test_mcp_guide_and_agent_read_tools) still reads it.
    """
    from backend.models.user import User
    from backend.policy.rbac import grant_workspace_membership

    workspace_id = _workspace_id(page_service)
    with page_service.session_factory.begin() as session:
        viewer = User(
            email="mcp-viewer@example.com",
            password_hash="unused",
            display_name="MCP Viewer",
            status="active",
        )
        session.add(viewer)
        session.flush()
        grant_workspace_membership(
            session,
            user_id=viewer.id,
            workspace_id=workspace_id,
            role="viewer",
        )
        viewer_id = viewer.id

    viewer_actor = ActorContext(
        actor_kind="human",
        actor_id=str(viewer_id),
        scopes=("guide", "agent-page-read"),
        user_id=viewer_id,
    )
    response = dispatch(
        "read_agent_page",
        {"workspace_id": str(workspace_id), "slug": DEFAULT_AGENT_PAGE_SLUG},
        actor=viewer_actor,
        page_service=page_service,
    )

    assert response.envelope.result_status == "rejected_by_policy"
    assert (
        response.envelope.error_code
        == "workspace_role_insufficient:sensitive_agent_page"
    )
    assert response.data is None


def test_mcp_read_agent_page_denies_sensitive_to_read_only_client(
    page_service, mcp_default_actor
):
    """A user-less mcp_client is gated by its provisioned scopes, not the
    blanket owner mapping: a read-only client (mcp:read expands to
    agent-page-read, so dispatch admits it) must not receive a
    sensitive-category body, while the write-capable default client
    (test_mcp_guide_and_agent_read_tools) still reads it — matching the
    prompts/resources surface, which denies the same actor.
    """
    workspace_id = _workspace_id(page_service)
    read_only_actor = mcp_default_actor(scopes=("mcp:read",))

    response = dispatch(
        "read_agent_page",
        {"workspace_id": str(workspace_id), "slug": DEFAULT_AGENT_PAGE_SLUG},
        actor=read_only_actor,
        page_service=page_service,
    )

    assert response.envelope.result_status == "rejected_by_policy"
    assert (
        response.envelope.error_code
        == "workspace_role_insufficient:sensitive_agent_page"
    )
    assert response.data is None


def test_mcp_fetch_allows_non_sensitive_agent_page_body_with_mcp_read_scope(
    page_service, mcp_default_actor
):
    """The public mcp:read scope includes non-sensitive AgentPage bodies."""

    workspace_id = _workspace_id(page_service)
    with page_service.session_factory.begin() as session:
        page = AgentPage(
            workspace_id=workspace_id,
            category="skill",
            title="MCP-02 Non-sensitive Agent Page",
            slug="mcp-02-non-sensitive-agent-page",
            lifecycle_status="active",
        )
        session.add(page)
        session.flush()
        version = AgentPageVersion(
            workspace_id=workspace_id,
            agent_page_id=page.id,
            version_no=1,
            body_markdown="# Non-sensitive body",
            frontmatter={},
            actor_type="system",
            version_status="active",
        )
        session.add(version)
        session.flush()
        page.current_active_version_id = version.id
        page_id = page.id

    read_actor = mcp_default_actor(
        actor_id="fetch-scope-reader",
        scopes=("mcp:read",),
    )
    response = dispatch(
        "fetch",
        {
            "workspace_id": str(workspace_id),
            "id": str(page_id),
            "type": "agent_page",
        },
        actor=read_actor,
        page_service=page_service,
    )
    assert response.envelope.result_status == "succeeded"
    assert response.text == "# Non-sensitive body"


def test_mcp_read_agent_page_requires_id_or_slug(page_service, actor):
    workspace_id = _workspace_id(page_service)

    response = dispatch(
        "read_agent_page",
        {"workspace_id": str(workspace_id)},
        actor=actor,
        page_service=page_service,
    )

    assert response.envelope.result_status == "failed"
    assert response.envelope.error_code == "validation_failed"


def test_read_concept_resolves_library_paths(
    page_service, actor, media_installer_factory
):
    """A-8: every /library/<id>.md path the index advertises must resolve via
    read_concept (previously a hard not_found dead-end)."""

    workspace_id = _workspace_id(page_service)
    with page_service.session_factory.begin() as session:
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace_id,
            canonical_uri="https://example.com/a8-source",
            title="A8 Source",
            raw_text="# A8 Source\n\nExtracted source text needle.",
        )
        ExtractionService(media_installer_factory=media_installer_factory).extract_library_item_version(
            session=session,
            library_item_version_id=registration.version.id,
        )
        item_id = registration.library_item.id

    output = dispatch(
        "read_concept",
        {
            "workspace_id": str(workspace_id),
            "path": f"/library/{item_id}.md",
        },
        actor=actor,
        page_service=page_service,
    )
    assert output.envelope.result_status == "succeeded"
    assert output.status == "found"
    assert output.target_kind == "library_item"
    assert output.target_id == item_id
    assert "Extracted source text needle." in output.content
    assert output.frontmatter["type"] == "source"

    # index advertises the same path — parity check
    index = dispatch(
        "get_index",
        {"workspace_id": str(workspace_id), "sections": ["library"]},
        actor=actor,
        page_service=page_service,
    )
    assert f"/library/{item_id}.md" in index.index


def test_read_concept_library_gates_return_not_found(page_service, actor):
    from backend.models.library import LibraryItem

    workspace_id = _workspace_id(page_service)
    with page_service.session_factory.begin() as session:
        registration = LibraryService().register_library_item(
            session=session,
            workspace_id=workspace_id,
            canonical_uri="https://example.com/a8-restricted",
            title="A8 Restricted",
            raw_text="# Secret\n\nRestricted text.",
        )
        item = session.get(LibraryItem, registration.library_item.id)
        item.trust_level = "restricted"
        item_id = item.id

    # a viewer (no restricted grant, not owner) must read not_found —
    # absence may not disclose existence (design 3.3). The dispatch path
    # rescopes mcp_client to its registered role, so pin the predicate at
    # the resolver seam with an explicit viewer actor.
    from uuid import uuid4 as _uuid4

    from backend.policy.auth import ActorContext as _Actor
    from backend.services.okf import resolve_library_concept

    viewer = _Actor(
        actor_kind="human",
        actor_id=str(_uuid4()),
        token_kind="session",
        scopes=(),
        workspace_role="viewer",
    )
    with page_service.session_factory() as session:
        denied_item, denied_status = resolve_library_concept(
            session, workspace_id, identifier=str(item_id), actor=viewer
        )
    assert denied_item is None
    assert denied_status == "not_found"

    # owner-scoped dispatch may read it (grant contrast)
    output = dispatch(
        "read_concept",
        {
            "workspace_id": str(workspace_id),
            "path": f"/library/{item_id}.md",
        },
        actor=actor,
        page_service=page_service,
    )
    assert output.envelope.result_status == "succeeded"


def test_read_rejects_removed_include_and_range_fields(page_service, actor):
    """D-2: the formerly-ignored fields now fail loudly instead of silently
    succeeding (reverting the removal makes these pass silently again)."""

    workspace_id = _workspace_id(page_service)
    payload = page_service.write_page(
        workspace_slug="default",
        slug="d2-page",
        title="D2 Page",
        body_md="# D2\n\nBody.",
        actor_kind="human",
        actor_id="tester",
        actor_provenance="browser_session",
    )
    for extra in ({"include": ["backlinks"]}, {"range": {"lines": [1, 5]}}):
        output = dispatch(
            "read",
            {
                "workspace_id": str(workspace_id),
                "target_type": "context_page",
                "target_id": str(payload.id),
                **extra,
            },
            actor=actor,
            page_service=page_service,
        )
        assert output.envelope.result_status == "failed"
        assert output.envelope.error_code == "validation_failed"

    clean = dispatch(
        "read",
        {
            "workspace_id": str(workspace_id),
            "target_type": "context_page",
            "target_id": str(payload.id),
        },
        actor=actor,
        page_service=page_service,
    )
    assert clean.envelope.result_status == "succeeded"

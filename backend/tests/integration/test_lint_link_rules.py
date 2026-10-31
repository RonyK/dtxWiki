from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models.agent import AgentPage
from backend.models.context import ContextPage, ContextPageVersion, PageLink
from backend.models.library import LibraryItem
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.services.body_preparer import BodyInput, BodyPreparer, WriteContext
from backend.services.lint import LintContext, LintRegistry
from backend.services.lint.scopes import findings_for_target
from backend.services.page_service import PageService, refresh_page_links

pytestmark = pytest.mark.integration


def _workspace_id(session):
    return session.scalar(select(Workspace.id).where(Workspace.slug == "default"))


def _page(session, workspace_id, slug, body):
    page = ContextPage(
        workspace_id=workspace_id, slug=slug, title=slug, page_type="concept"
    )
    session.add(page)
    session.flush()
    version = ContextPageVersion(
        workspace_id=workspace_id,
        page_id=page.id,
        version_no=1,
        body_md=body,
        content_hash=uuid4().hex,
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="test",
        version_status="published_snapshot",
    )
    session.add(version)
    session.flush()
    page.current_published_version_id = version.id
    refresh_page_links(session, page=page, version=version)
    session.flush()
    return page, version


def _check(session, rule_id, page):
    return findings_for_target(
        LintRegistry.builtin().rule(rule_id),
        page,
        LintContext(session, page.workspace_id, page=page),
    )


def test_unresolved_and_healthy_links(session_factory):
    from backend.services.context_links import (
        LinkContext,
        editor_link_actor,
        rewrite_links_with_report,
    )
    from backend.services.index_refs import format_index_ref

    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        target, _ = _page(session, workspace_id, "healthy-target", "Target.\n")
        broken, _ = _page(
            session, workspace_id, "broken-source", "[[missing-target]]\n"
        )
        # Reuse the existing visible-target fixture for copied system refs.
        body = f"[Target]({format_index_ref('context', target.id)})\n"
        healthy, _ = _page(session, workspace_id, "healthy-source", body)

        unresolved = _check(session, "link.unresolved", broken)
        assert [finding.message_args["written_slug"] for finding in unresolved] == [
            "missing-target"
        ]
        assert _check(session, "link.internal-noncanonical", broken) == []
        assert _check(session, "link.unresolved", healthy) == []
        canonical = f"/p/{workspace_id}/{target.id}"
        [finding] = _check(session, "link.internal-noncanonical", healthy)
        assert finding.message_args["replacement"] == canonical
        assert rewrite_links_with_report(
            body,
            LinkContext(session, workspace_id, editor_link_actor("human", "test")),
        )[0] == f"[Target]({canonical})\n"

        # Budget exception: these security paths differ from Context visibility.
        # Restricted Library, sensitive Agent and a folder under a hidden Agent
        # ancestor must each match the ordinary reader. Reuse this DB traversal
        # to prove real caller capabilities survive the preparation seam; a pure
        # locator test cannot detect an ingress replacing an owner with editor.
        restricted = LibraryItem(
            workspace_id=workspace_id,
            connector_type="manual_upload",
            uri="https://example.invalid/restricted-ref",
            title="Restricted",
            trust_level="restricted",
            lifecycle_status="ready",
        )
        session.add(restricted)
        guide = session.scalar(select(AgentPage).where(
            AgentPage.workspace_id == workspace_id,
            AgentPage.category == "agent_guide",
        ))
        parent = session.scalar(select(WorkspaceTreeNode).where(
            WorkspaceTreeNode.workspace_id == workspace_id,
            WorkspaceTreeNode.target_kind == "agent_page",
            WorkspaceTreeNode.target_id == guide.id,
        ))
        folder = WorkspaceTreeNode(
            workspace_id=workspace_id, section="agent", node_kind="folder",
            parent_id=parent.id, sort_key="0001", title="Private descendant",
            slug="private-descendant", visibility_status="visible",
        )
        session.add(folder)
        session.flush()
        library_ref = format_index_ref("library", restricted.id)
        agent_ref = format_index_ref("agent", guide.id)
        folder_ref = format_index_ref("folder", folder.id)
        missing_ref = format_index_ref("library", uuid4())
        authored_route = f"/workspaces/{workspace_id}/library/items/{restricted.id}"
        protected_body = (
            f"[Restricted]({library_ref}) [Guide]({agent_ref}) "
            f"[Folder]({folder_ref}) [Missing]({missing_ref}) "
            f"[Authored]({authored_route})\n"
        )
        owner = ActorContext(
            actor_kind="human", actor_id="test", workspace_id=workspace_id,
            workspace_role="owner", role="owner", token_kind="api_token",
            token_workspace_ids=(workspace_id,), scopes=("mcp:write",),
        )
        preparer = BodyPreparer()

        def prepare_for(actor):
            return preparer.prepare(
                BodyInput(protected_body),
                WriteContext(
                    workspace=session.get(Workspace, workspace_id),
                    session=session, actor_kind=actor.actor_kind,
                    actor_id=actor.actor_id, actor_provenance="internal", actor=actor,
                ),
            )

        editor = ActorContext(
            actor_kind="human", actor_id="test", workspace_id=workspace_id,
            workspace_role="editor", role="editor", token_kind="session",
        )
        denied = prepare_for(editor)
        assert denied.body == protected_body
        unavailable = [f for f in denied.findings if f.rule_id == "link.unresolved"]
        assert {f.message_args["locator"] for f in unavailable} == {
            library_ref, agent_ref, folder_ref, missing_ref,
        }
        assert {f.suggested_action for f in unavailable} == {
            "This document link is unavailable.",
        }
        allowed_body = (
            protected_body.replace(library_ref, authored_route)
            .replace(agent_ref, f"/workspaces/{workspace_id}/agent/pages/{guide.id}")
            .replace(
                folder_ref, f"/workspaces/{workspace_id}/agent/folders/{folder.id}"
            )
        )
        assert prepare_for(owner).body == allowed_body
        limited = ActorContext(
            actor_kind="human", actor_id="test", workspace_id=workspace_id,
            workspace_role="owner", role="owner", token_kind="api_token",
            token_workspace_ids=(workspace_id,), scopes=("mcp:agent",),
        )
        # An owner role alone must not invent the admin scope of an MCP caller.
        assert prepare_for(limited).body == protected_body.replace(
            library_ref, authored_route
        )
        system = ActorContext(
            actor_kind="system", actor_id="lint-autofix", workspace_id=workspace_id,
            workspace_role="editor", role="editor",
        )
        assert prepare_for(system).body == allowed_body

    scoped_pages = PageService(session_factory, actor_context=owner)
    saved = scoped_pages.write_page(
        workspace_slug="default", slug="scoped-ref-authority", title="Authority",
        body_md=protected_body, actor_kind="human", actor_id="test",
        actor_provenance="personal_access_token",
    )
    assert saved.body_md == allowed_body



def test_renamed_pinned_link_is_broken_slug_not_unresolved(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        target, _ = _page(session, workspace_id, "old-target", "Target.\n")
        source, _ = _page(session, workspace_id, "source", "[[old-target]]\n")
        target.slug = "new-target"
        session.flush()

        assert _check(session, "link.unresolved", source) == []
        findings = _check(session, "link.internal-noncanonical", source)
        assert len(findings) == 1
        assert findings[0].message_args == {
            "written_slug": "old-target",
            "locator_kind": "wikilink",
            "locator": "[[old-target]]",
            "replacement": f"/p/{workspace_id}/{target.id}",
            # LEAK-01: link findings now carry provenance like format findings,
            # so list_findings can hide a draft-only finding from a viewer.
            # This fixture only sets current_published_version_id.
            "body_scopes": ["published"],
        }


def test_renamed_context_url_target_does_not_suggest_wikilink_rewrite(
    session_factory,
):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        target, _ = _page(session, workspace_id, "old-url-target", "Target.\n")
        source, source_version = _page(
            session,
            workspace_id,
            "url-source",
            (
                "[Target]"
                f"(/p/{workspace_id}/{target.id})\n"
            ),
        )
        url_link = session.scalar(
            select(PageLink).where(
                PageLink.source_version_id == source_version.id,
                PageLink.link_syntax == "context_url",
            )
        )
        assert url_link is not None
        assert url_link.target_slug is None

        target.slug = "new-url-target"
        session.flush()

        assert _check(session, "link.unresolved", source) == []
        assert _check(session, "link.internal-noncanonical", source) == []


def test_defensive_unresolved_context_url_row_does_not_create_link_finding(
    session_factory,
):
    """Missing URL locators are retained; unrelated stale rows add no finding."""
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        missing_page_id = uuid4()
        source, source_version = _page(
            session,
            workspace_id,
            "defensive-url-source",
            (
                "[Missing]"
                f"(/workspaces/{workspace_id}/context/pages/{missing_page_id})\n"
            ),
        )
        assert (
            session.scalar(
                select(PageLink).where(
                    PageLink.source_version_id == source_version.id,
                    PageLink.link_syntax == "context_url",
                )
            )
            is not None
        )

        session.add(
            PageLink(
                workspace_id=workspace_id,
                source_page_id=source.id,
                source_version_id=source_version.id,
                link_syntax="context_url",
                target_slug="missing-url-target",
                target_page_id=None,
                occurrence_count=1,
                first_snippet="[Missing](/context?target=missing)",
            )
        )
        session.flush()

        findings = _check(session, "link.unresolved", source)
        assert len(findings) == 1
        assert findings[0].message_args["locator"] == (
            f"/workspaces/{workspace_id}/context/pages/{missing_page_id}"
        )
        assert _check(session, "link.internal-noncanonical", source) == []


def _stage_draft(session, page, published_version, draft_body):
    """Stage a draft version on top of ``page``'s existing published head and
    sync its links, leaving BOTH the published and draft bodies live."""
    draft_version = ContextPageVersion(
        workspace_id=page.workspace_id,
        page_id=page.id,
        version_no=2,
        body_md=draft_body,
        content_hash=uuid4().hex,
        actor_type="human",
        created_by_actor_kind="human",
        created_by_actor_id="test",
        version_status="draft",
        parent_version_id=published_version.id,
    )
    session.add(draft_version)
    session.flush()
    page.latest_draft_version_id = draft_version.id
    page.current_version_id = draft_version.id
    refresh_page_links(session, page=page, version=draft_version)
    session.flush()
    return draft_version


def test_link_finding_body_scopes_track_which_body_the_defect_is_in(session_factory):
    # LEAK-01: list_findings hides a finding whose defect exists ONLY in a
    # draft an actor cannot read, keyed off message_args["body_scopes"]. A
    # link finding must carry that provenance exactly like a format finding
    # (scopes.collapse_by_fingerprint) or the exclusion cannot
    # distinguish a draft-only broken wikilink from a published one.
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)

        # draft-only: the published head has no links; the staged draft adds
        # an unresolved [[secret-slug]].
        draft_only, published = _page(
            session, workspace_id, "draft-only-source", "No links here.\n"
        )
        _stage_draft(session, draft_only, published, "[[secret-slug]]\n")
        draft_only_findings = _check(session, "link.unresolved", draft_only)
        assert len(draft_only_findings) == 1
        assert draft_only_findings[0].message_args["body_scopes"] == ["draft"]
        assert draft_only_findings[0].message_args["written_slug"] == "secret-slug"

        # both bodies: the same unresolved slug in the published head AND the
        # draft -> one collapsed finding tagged with both scopes.
        both, published2 = _page(
            session, workspace_id, "both-source", "[[secret-slug]]\n"
        )
        _stage_draft(session, both, published2, "[[secret-slug]]\n")
        both_findings = _check(session, "link.unresolved", both)
        assert len(both_findings) == 1
        assert both_findings[0].message_args["body_scopes"] == ["published", "draft"]

        # A page-global pointer can name another author's private editing row.
        # Both link rules must keep scanning only the shared published head.
        target, _ = _page(session, workspace_id, "private-old", "Target.\n")
        private_source, shared = _page(
            session, workspace_id, "private-source", "Clean.\n"
        )
        private = _stage_draft(
            session, private_source, shared, "[[private-missing]] [[private-old]]\n"
        )
        private.version_status = "editing"
        private.created_by_actor_id = "other-author"
        target.slug = "private-new"
        session.flush()
        assert _check(session, "link.unresolved", private_source) == []
        assert _check(session, "link.internal-noncanonical", private_source) == []


def test_rebuild_coalesces_two_repaired_slugs_without_losing_pin(session_factory):
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        target, _ = _page(session, workspace_id, "current-target", "Target.\n")
        source, version = _page(
            session, workspace_id, "source-two", "[[old-one]] and [[old-two]]\n"
        )
        links = session.scalars(
            select(PageLink).where(PageLink.source_version_id == version.id)
        ).all()
        for link in links:
            link.target_page_id = target.id
        session.flush()

        version.body_md = "[[current-target]] and [[current-target]]\n"
        refresh_page_links(session, page=source, version=version)
        session.flush()

        rebuilt = session.scalars(
            select(PageLink).where(PageLink.source_version_id == version.id)
        ).all()
        assert len(rebuilt) == 1
        assert rebuilt[0].target_slug == "current-target"
        assert rebuilt[0].occurrence_count == 2
        assert rebuilt[0].target_page_id == target.id

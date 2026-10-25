from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models.context import ContextPage, ContextPageVersion, PageLink
from backend.models.lint import LintFinding
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.lint import LintRunCreateRequest
from backend.services.lint import LintContext, LintRegistry
from backend.services.lint.scopes import findings_for_target
from backend.services.lint_service import LintService
from backend.services.page_service import refresh_page_links

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
    with session_factory.begin() as session:
        workspace_id = _workspace_id(session)
        _page(session, workspace_id, "healthy-target", "Target.\n")
        broken, _ = _page(
            session, workspace_id, "broken-source", "[[missing-target]]\n"
        )
        healthy, _ = _page(
            session, workspace_id, "healthy-source", "[[healthy-target]]\n"
        )

        unresolved = _check(session, "link.unresolved", broken)
        assert [finding.message_args["written_slug"] for finding in unresolved] == [
            "missing-target"
        ]
        assert _check(session, "link.internal-noncanonical", broken) == []
        assert _check(session, "link.unresolved", healthy) == []
        assert _check(session, "link.internal-noncanonical", healthy) == []

        # Queued RUN has an internal editor's shared-draft visibility, while
        # a caller-supplied interactive actor retains its narrower authority.
        # The same existing fixture distinguishes unpublished from hidden
        # targets without adding another collected database scenario.
        shared_target, shared_version = _page(
            session, workspace_id, "shared-draft-target", "Draft.\n"
        )
        private_target, private_version = _page(
            session, workspace_id, "private-edit-target", "Private.\n"
        )
        for target, version, status in (
            (shared_target, shared_version, "draft"),
            (private_target, private_version, "editing"),
        ):
            target.current_published_version_id = None
            target.current_version_id = version.id
            target.latest_draft_version_id = version.id
            version.version_status = status
        source, _ = _page(
            session, workspace_id, "unpublished-source",
            "[[shared-draft-target]] [[private-edit-target]]\n",
        )
        session.get(Workspace, workspace_id).lint_policy = {"link_fix_mode": "suggest"}
        lint = LintService()
        queued = lint.execute_queued_run(
            session, workspace_id=workspace_id, run_id=None,
            trigger_type="manual", actor_kind="human", actor_id="test",
        )
        findings = list(session.scalars(select(LintFinding).where(
            LintFinding.target_id == source.id, LintFinding.status == "open",
            LintFinding.lint_run_id == queued.id,
            LintFinding.rule_id.startswith("link."),
        )))
        assert {(finding.rule_id, finding.message_args["written_slug"])
                for finding in findings} == {
            ("link.target-unpublished", "shared-draft-target"),
            ("link.unresolved", "private-edit-target"),
        }
        viewer = ActorContext(
            actor_kind="human", actor_id="viewer", workspace_id=workspace_id,
            workspace_role="viewer", role="viewer",
        )
        lint.run_lint(
            session, workspace_id, LintRunCreateRequest(trigger_type="manual"),
            "human", "viewer", actor=viewer,
        )
        assert {(finding.rule_id, finding.message_args["written_slug"])
                for finding in session.scalars(select(LintFinding).where(
                    LintFinding.target_id == source.id, LintFinding.status == "open",
                    LintFinding.rule_id.startswith("link."),
                ))} == {
            ("link.unresolved", "shared-draft-target"),
            ("link.unresolved", "private-edit-target"),
        }


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

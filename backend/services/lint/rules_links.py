from __future__ import annotations

from backend.models.context import ContextPage, PageLink
from backend.repositories.workspace_scoped import join_in_workspace, select_in_workspace
from backend.services.lint.base import (
    LintContext,
    LintFindingDraft,
    LintModule,
    LintStage,
)

_TARGETS = frozenset({"context_page"})


class _UnresolvedLinkRule:
    rule_id = "link.unresolved"
    layer = "knowledge"
    default_enabled = True
    applies_to = _TARGETS
    stages = frozenset({LintStage.RUN})
    version_scoped = True

    def check_target(self, target, ctx: LintContext):
        session, workspace_id = ctx.session, ctx.workspace_id
        version_id = ctx.version.id
        rows = session.scalars(
            select_in_workspace(
                PageLink,
                workspace_id=workspace_id,
                conditions=(
                    PageLink.source_page_id == target.id,
                    PageLink.source_version_id == version_id,
                    PageLink.target_page_id.is_(None),
                    PageLink.locator_key.startswith("wikilink:"),
                    PageLink.target_slug.is_not(None),
                ),
            )
        ).all()
        return [_unresolved_finding(target.id, link) for link in rows]


def _unresolved_finding(page_id, link):
    message = "Unresolved wikilink: " + link.target_slug
    args = {"written_slug": link.target_slug}
    return _finding("link.unresolved", page_id, link.target_slug, message, args)


def _broken_slug_finding(page_id, link, current_slug):
    message = f"Rewrite [[{link.target_slug}]] as [[{current_slug}]]."
    args = {"written_slug": link.target_slug, "current_slug": current_slug}
    return _finding("link.broken-slug", page_id, link.target_slug, message, args)


def _finding(rule_id, page_id, written_slug, message, message_args):
    return LintFindingDraft(
        rule_id=rule_id,
        severity="warning",
        target_type="context_page",
        target_id=page_id,
        suggested_action=message,
        message_key=rule_id,
        message_args=message_args,
        fingerprint=f"{rule_id}|{written_slug}",
    )


class _BrokenSlugLinkRule:
    rule_id = "link.broken-slug"
    layer = "knowledge"
    default_enabled = True
    applies_to = _TARGETS
    stages = frozenset({LintStage.RUN})
    version_scoped = True

    def check_target(self, target, ctx: LintContext):
        session, workspace_id = ctx.session, ctx.workspace_id
        version_id = ctx.version.id
        rows = session.execute(
            join_in_workspace(
                select_in_workspace(
                    PageLink,
                    workspace_id=workspace_id,
                    conditions=(
                        PageLink.source_page_id == target.id,
                        PageLink.source_version_id == version_id,
                        PageLink.locator_key.startswith("wikilink:"),
                        PageLink.target_slug.is_not(None),
                        PageLink.target_slug != ContextPage.slug,
                    ),
                ),
                PageLink,
                ContextPage,
                workspace_id=workspace_id,
                on=ContextPage.id == PageLink.target_page_id,
            )
            .with_only_columns(PageLink, ContextPage.slug)
        ).all()
        return [_broken_slug_finding(target.id, link, slug) for link, slug in rows]


MODULE = LintModule(
    "links",
    100,
    (
        _UnresolvedLinkRule(),
        _BrokenSlugLinkRule(),
    ),
)

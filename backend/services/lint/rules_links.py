from __future__ import annotations

from dataclasses import replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.context import ContextPage, ContextPageVersion, PageLink
from backend.services.lint.base import LintFindingDraft, register_rule

_TARGETS = frozenset({"context_page"})


def _version_scopes(session: Session, page: ContextPage) -> list[tuple[str, object]]:
    """(scope, version_id) for the published head and the staged draft.

    Mirrors ``rules_format._page_bodies``: a link finding must carry the same
    provenance as a format finding, because ``list_findings`` hides draft-only
    findings from an actor who may not read drafts. Published first, so a link
    broken in both bodies is reported as a published defect.

    P8.1: ``latest_draft_version_id`` can durably name another author's still-
    private "editing" row (see ``rules_format._shared_draft_version``'s
    docstring for why this scan has no actor context to hide it from later) --
    exclude it here rather than surface its `PageLink.target_slug` text as a
    "draft" finding any editor-role actor can then read.
    """
    pairs: list[tuple[str, object]] = []
    seen: set = set()
    published_id = page.current_published_version_id
    if published_id is not None:
        seen.add(published_id)
        pairs.append(("published", published_id))
    draft_id = page.latest_draft_version_id
    if draft_id is not None and draft_id not in seen:
        draft = session.get(ContextPageVersion, draft_id)
        if draft is not None and draft.version_status != "editing":
            pairs.append(("draft", draft_id))
    return pairs


def _collapse(
    collapsed: dict[str, LintFindingDraft],
    finding: LintFindingDraft,
    scope: str,
) -> None:
    """One row per fingerprint, tagged with every body it was seen in."""
    existing = collapsed.get(finding.fingerprint)
    if existing is None:
        args = dict(finding.message_args or {})
        args["body_scopes"] = [scope]
        collapsed[finding.fingerprint] = replace(finding, message_args=args)
        return
    scopes = existing.message_args["body_scopes"]
    if scope not in scopes:
        scopes.append(scope)


class _UnresolvedLinkRule:
    rule_id = "link.unresolved"
    layer = "knowledge"
    default_enabled = True
    applies_to = _TARGETS

    def check(self, *, session, workspace_id, target, body=None):
        collapsed: dict[str, LintFindingDraft] = {}
        for scope, version_id in _version_scopes(session, target):
            rows = session.scalars(
                select(PageLink).where(
                    PageLink.workspace_id == workspace_id,
                    PageLink.source_page_id == target.id,
                    PageLink.source_version_id == version_id,
                    PageLink.target_page_id.is_(None),
                )
            ).all()
            for link in rows:
                _collapse(collapsed, _unresolved_finding(target.id, link), scope)
        return list(collapsed.values())


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

    def check(self, *, session, workspace_id, target, body=None):
        collapsed: dict[str, LintFindingDraft] = {}
        for scope, version_id in _version_scopes(session, target):
            rows = session.execute(
                select(PageLink, ContextPage.slug)
                .join(ContextPage, ContextPage.id == PageLink.target_page_id)
                .where(
                    PageLink.workspace_id == workspace_id,
                    PageLink.source_page_id == target.id,
                    PageLink.source_version_id == version_id,
                    PageLink.target_slug != ContextPage.slug,
                )
            ).all()
            for link, slug in rows:
                _collapse(collapsed, _broken_slug_finding(target.id, link, slug), scope)
        return list(collapsed.values())


register_rule(_UnresolvedLinkRule())
register_rule(_BrokenSlugLinkRule())

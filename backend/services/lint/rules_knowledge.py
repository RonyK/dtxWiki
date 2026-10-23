"""Deterministic knowledge rules backed by authoritative write-time projections."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import exists, func, select

from backend.models.context import ContextPage, PageLink, PageNameIndex
from backend.services.lint.base import (
    LintContext,
    LintFindingDraft,
    LintModule,
    LintStage,
)

_TARGETS = frozenset({"context_page"})


def _finding(
    rule_id: str,
    severity: str,
    target_id: UUID,
    message: str,
    *,
    anchor: str = "",
    message_args: dict[str, Any] | None = None,
) -> LintFindingDraft:
    return LintFindingDraft(
        rule_id=rule_id,
        severity=severity,
        target_type="context_page",
        target_id=target_id,
        suggested_action=message,
        message_key=rule_id,
        message_args=message_args or {},
        fingerprint=f"{rule_id}|{anchor}",
    )


class _OrphanPageRule:
    rule_id = "kb.orphan_page"
    layer = "knowledge"
    default_enabled = True
    applies_to = _TARGETS
    stages = frozenset({LintStage.RUN})

    def check_target(self, target, ctx: LintContext) -> list[LintFindingDraft]:
        session, workspace_id = ctx.session, ctx.workspace_id
        if (
            target.lifecycle_status == "archived"
            or target.current_published_version_id is None
        ):
            return []
        inbound = session.scalar(
            select(func.count(PageLink.id))
            .join(ContextPage, ContextPage.id == PageLink.source_page_id)
            .where(
                PageLink.workspace_id == workspace_id,
                PageLink.target_page_id == target.id,
                PageLink.source_page_id != target.id,
                PageLink.source_version_id == ContextPage.current_published_version_id,
                ContextPage.lifecycle_status != "archived",
            )
        )
        if inbound:
            return []
        return [
            _finding(
                self.rule_id,
                "info",
                target.id,
                f"Page '{target.slug}' has no inbound wikilinks (orphan).",
                message_args={"slug": target.slug},
            )
        ]


class _MissingBacklinkRule:
    rule_id = "kb.missing_backlink"
    layer = "knowledge"
    default_enabled = True
    applies_to = _TARGETS
    stages = frozenset({LintStage.RUN})

    def check_target(self, target, ctx: LintContext) -> list[LintFindingDraft]:
        session, workspace_id = ctx.session, ctx.workspace_id
        if (
            target.lifecycle_status == "archived"
            or target.current_published_version_id is None
        ):
            return []
        outbound_ids = select(PageLink.target_page_id).where(
            PageLink.workspace_id == workspace_id,
            PageLink.source_page_id == target.id,
            PageLink.source_version_id == target.current_published_version_id,
            PageLink.target_page_id.is_not(None),
            PageLink.target_page_id != target.id,
        )
        reciprocal = exists(
            select(PageLink.id).where(
                PageLink.workspace_id == workspace_id,
                PageLink.source_page_id == ContextPage.id,
                PageLink.source_version_id == ContextPage.current_published_version_id,
                PageLink.target_page_id == target.id,
            )
        )
        missing = session.execute(
            select(ContextPage.id, ContextPage.slug)
            .where(
                ContextPage.workspace_id == workspace_id,
                ContextPage.id.in_(outbound_ids),
                ContextPage.lifecycle_status != "archived",
                ContextPage.current_published_version_id.is_not(None),
                ~reciprocal,
            )
            .order_by(ContextPage.slug, ContextPage.id)
        ).all()
        findings: list[LintFindingDraft] = []
        for _other_id, other_slug in missing:
            findings.append(
                _finding(
                    self.rule_id,
                    "warning",
                    target.id,
                    f"Page '{target.slug}' links to '{other_slug}', but "
                    f"'{other_slug}' does not link back.",
                    anchor=other_slug,
                    message_args={
                        "source_slug": target.slug,
                        "target_slug": other_slug,
                    },
                )
            )
        return findings


class _DuplicateConceptRule:
    rule_id = "kb.duplicate_concept"
    layer = "knowledge"
    default_enabled = True
    applies_to = _TARGETS
    stages = frozenset({LintStage.RUN})

    def check_target(self, target, ctx: LintContext) -> list[LintFindingDraft]:
        session, workspace_id = ctx.session, ctx.workspace_id
        title = (target.title or "").strip().casefold()
        if (
            not title
            or target.lifecycle_status == "archived"
            or target.current_published_version_id is None
        ):
            return []
        duplicates = session.scalar(
            select(func.count(ContextPage.id)).where(
                ContextPage.workspace_id == workspace_id,
                ContextPage.id != target.id,
                ContextPage.lifecycle_status != "archived",
                ContextPage.current_published_version_id.is_not(None),
                func.lower(ContextPage.title) == title,
            )
        )
        if not duplicates:
            return []
        return [
            _finding(
                self.rule_id,
                "warning",
                target.id,
                f"{duplicates} other page(s) share the title "
                f"'{target.title}' — possible duplicate concept.",
            )
        ]


class _TermCollisionRule:
    rule_id = "kb.term_collision"
    layer = "knowledge"
    default_enabled = True
    applies_to = _TARGETS
    stages = frozenset({LintStage.RUN})

    def check_target(self, target, ctx: LintContext) -> list[LintFindingDraft]:
        session, workspace_id = ctx.session, ctx.workspace_id
        cache_key = "_lint_term_collision_map"
        collisions_by_page = session.info.get(cache_key)
        if collisions_by_page is None:
            collision_names = (
                select(PageNameIndex.name_norm)
                .where(PageNameIndex.workspace_id == workspace_id)
                .group_by(PageNameIndex.name_norm)
                .having(func.count(func.distinct(PageNameIndex.page_id)) > 1)
            )
            collisions_by_page = {}
            for page_id, name_norm in session.execute(
                select(PageNameIndex.page_id, PageNameIndex.name_norm)
                .where(
                    PageNameIndex.workspace_id == workspace_id,
                    PageNameIndex.name_norm.in_(collision_names),
                )
                .distinct()
            ).all():
                collisions_by_page.setdefault(page_id, []).append(name_norm)
            session.info[cache_key] = collisions_by_page
        collisions = collisions_by_page.get(target.id, [])
        return [
            _finding(
                self.rule_id,
                "warning",
                target.id,
                f"Name '{name}' resolves to more than one page.",
                anchor=name,
                message_args={"name": name},
            )
            for name in collisions
        ]


MODULE = LintModule(
    "knowledge",
    200,
    (
        _OrphanPageRule(),
        _MissingBacklinkRule(),
        _DuplicateConceptRule(),
        _TermCollisionRule(),
    ),
)

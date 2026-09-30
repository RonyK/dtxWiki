"""Deterministic grep-like access over actor-visible wiki Markdown."""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.context import ContextPage, ContextPageVersion
from backend.policy.auth import ActorContext
from backend.services.context_visibility import visible_context_version_id
from backend.services.okf import context_concept_path
from backend.utils.markdown_outline import parse_outline


@dataclass(frozen=True)
class GrepWikiMatch:
    page_id: UUID
    version_id: UUID
    title: str
    concept_path: str
    line_number: int
    line: str
    context: str
    heading_path: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "page_id": str(self.page_id),
            "version_id": str(self.version_id),
            "title": self.title,
            "concept_path": self.concept_path,
            "line_number": self.line_number,
            "line": self.line,
            "context": self.context,
            "heading_path": self.heading_path,
        }


def grep_wiki(
    session: Session,
    *,
    workspace_id: UUID,
    pattern: str,
    actor: ActorContext | None,
    context_lines: int = 2,
    max_matches: int = 50,
) -> list[GrepWikiMatch]:
    # Literal-only: the pattern is always escaped, so matching cost stays
    # linear in the scanned text (no user regex reaches the backtracking
    # engine — Sprint-2 review REDOS-1).
    regex = re.compile(re.escape(pattern), re.IGNORECASE)
    matches: list[GrepWikiMatch] = []
    pages = session.scalars(
        select(ContextPage)
        .where(ContextPage.workspace_id == workspace_id)
        # An archived page must leave every search/read lane (B-8); the archive
        # cascade leaves current_published_version_id set, so filter here like
        # search_service._direct_lane rather than rely on the version resolver
        # (Codex R14).
        .where(ContextPage.lifecycle_status != "archived")
        .order_by(ContextPage.title.asc())
    ).all()
    for page in pages:
        version_id, _is_draft = visible_context_version_id(page, actor)
        if version_id is None:
            continue
        version = session.get(ContextPageVersion, version_id)
        if version is None:
            continue
        lines = version.body_md.splitlines()
        outline = parse_outline(version.body_md)
        headings_by_line = {
            section.line_start: section.heading_path
            for section in outline.sections
            if section.kind == "heading"
        }
        heading_path: tuple[str, ...] = ()
        for index, line in enumerate(lines):
            heading_path = headings_by_line.get(index + 1, heading_path)
            if regex.search(line) is None:
                continue
            start = max(0, index - context_lines)
            end = min(len(lines), index + context_lines + 1)
            matches.append(
                GrepWikiMatch(
                    page_id=page.id,
                    version_id=version.id,
                    title=page.title,
                    concept_path=context_concept_path(page.slug),
                    line_number=index + 1,
                    line=line,
                    context="\n".join(lines[start:end]),
                    heading_path=(
                        " > ".join(heading_path) if heading_path else None
                    ),
                )
            )
            if len(matches) >= max_matches:
                return matches
    return matches

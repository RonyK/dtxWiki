"""Shared actor-visible lookup over the rebuildable page-name projection."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from backend.models.context import ContextPage, ContextPageVersion, PageNameIndex
from backend.policy.auth import ActorContext
from backend.services.context_visibility import (
    editable_context_version_id,
    visible_context_version_id,
)
from backend.services.read_errors import PageNameOccupiedError, ReadServiceError

NAME_KIND_PRIORITY = {"slug": 0, "title": 1, "alias": 2}


@dataclass(frozen=True, slots=True)
class VisiblePageNameMatch:
    index: PageNameIndex
    page: ContextPage
    version: ContextPageVersion


@dataclass(frozen=True, slots=True)
class PageNameAmbiguityCandidate:
    page_id: UUID
    slug: str
    title: str
    placement_parent_node_id: UUID | None

    def as_payload(self) -> dict[str, UUID | str | None]:
        return {
            "page_id": self.page_id,
            "slug": self.slug,
            "title": self.title,
            "placement_parent_node_id": self.placement_parent_node_id,
        }


class PageNameAmbiguityError(ReadServiceError):
    """One normalized Context name resolves to more than one page."""

    error_code = "disambiguation_required"

    def __init__(self, name: str, matches: list[VisiblePageNameMatch]) -> None:
        self.name = name
        selected = best_match_per_page(matches)
        self.candidates = tuple(
            PageNameAmbiguityCandidate(
                page_id=match.page.id,
                slug=match.page.slug,
                title=match.page.title,
                placement_parent_node_id=match.page.placement_parent_node_id,
            )
            for match in selected
        )
        self.page_ids = tuple(candidate.page_id for candidate in self.candidates)
        pages = ", ".join(
            f"{match.page.title!r} ({match.page.id})" for match in selected
        )
        super().__init__(
            f"ambiguous page name {name!r}; select a page_id and update that "
            f"page directly; matches: {pages}"
        )

    def candidate_payloads(self) -> list[dict[str, UUID | str | None]]:
        return [candidate.as_payload() for candidate in self.candidates]


def visible_page_name_matches(
    session: Session,
    workspace_id: UUID,
    *,
    actor: ActorContext | None,
    name_norm: str | None = None,
    page_ids: set[UUID] | None = None,
    name_kinds: set[str] | None = None,
    context_types: list[str] | None = None,
    statuses: list[str] | None = None,
    include_unpublished: bool = False,
) -> list[VisiblePageNameMatch]:
    """Return projection rows that point at the actor's visible page head.

    ``include_unpublished`` resolves against the page's editable head instead,
    ignoring ``actor``. It exists for write-time deduplication, which has to
    find a page the writer cannot read -- not finding it is what created the
    duplicate ``-2`` pages. It hands back a page to write onto, never content,
    so it is not a read grant.
    """

    if name_norm is None and not page_ids:
        return []
    kind_order = case(
        (PageNameIndex.name_kind == "slug", 0),
        (PageNameIndex.name_kind == "title", 1),
        (PageNameIndex.name_kind == "alias", 2),
        else_=9,
    )
    statement = (
        select(PageNameIndex, ContextPage, ContextPageVersion)
        .join(ContextPage, ContextPage.id == PageNameIndex.page_id)
        .join(
            ContextPageVersion,
            ContextPageVersion.id == PageNameIndex.source_version_id,
        )
        .where(
            PageNameIndex.workspace_id == workspace_id,
            ContextPage.workspace_id == workspace_id,
            ContextPage.lifecycle_status != "archived",
        )
        .order_by(kind_order, ContextPage.title, ContextPage.id)
    )
    if name_norm is not None:
        statement = statement.where(PageNameIndex.name_norm == name_norm)
    if page_ids:
        statement = statement.where(PageNameIndex.page_id.in_(page_ids))
    if name_kinds:
        statement = statement.where(PageNameIndex.name_kind.in_(name_kinds))
    if context_types:
        statement = statement.where(ContextPage.page_type.in_(context_types))
    if statuses:
        statement = statement.where(ContextPage.lifecycle_status.in_(statuses))

    matches: list[VisiblePageNameMatch] = []
    for index, page, version in session.execute(statement):
        if include_unpublished:
            version_id = editable_context_version_id(page)
        else:
            version_id, _is_draft = visible_context_version_id(page, actor)
        if version_id == index.source_version_id == version.id:
            matches.append(
                VisiblePageNameMatch(index=index, page=page, version=version)
            )
    return matches


def best_match_per_page(
    matches: list[VisiblePageNameMatch],
) -> list[VisiblePageNameMatch]:
    selected: dict[UUID, VisiblePageNameMatch] = {}
    for match in matches:
        current = selected.get(match.page.id)
        if current is None or _match_order(match) < _match_order(current):
            selected[match.page.id] = match
    return sorted(selected.values(), key=_match_order)


def unique_visible_page_name_match(
    session: Session,
    workspace_id: UUID,
    *,
    name: str,
    actor: ActorContext | None,
    include_unpublished: bool = False,
) -> VisiblePageNameMatch | None:
    """Resolve title, slug, or alias while refusing cross-page ambiguity.

    ``include_unpublished`` keeps occupancy detection independent of actor
    visibility. When that wider projection is ambiguous, however, detailed
    candidates are safe only if every match is also present in the actor's
    visible projection.
    """

    from backend.services.entity_ref import normalize_entity_name

    name_norm = normalize_entity_name(name)
    if not name_norm:
        return None
    per_page = best_match_per_page(
        visible_page_name_matches(
            session,
            workspace_id,
            actor=actor,
            name_norm=name_norm,
            include_unpublished=include_unpublished,
        )
    )
    if len(per_page) > 1:
        if include_unpublished:
            detected_page_ids = {match.page.id for match in per_page}
            visible_page_ids = {
                match.page.id
                for match in best_match_per_page(
                    visible_page_name_matches(
                        session,
                        workspace_id,
                        actor=actor,
                        name_norm=name_norm,
                        page_ids=detected_page_ids,
                    )
                )
            }
            if visible_page_ids != detected_page_ids:
                raise PageNameOccupiedError()
        raise PageNameAmbiguityError(name, per_page)
    return per_page[0] if per_page else None


def _match_order(match: VisiblePageNameMatch) -> tuple[int, str, str]:
    return (
        NAME_KIND_PRIORITY.get(match.index.name_kind, 9),
        match.page.title,
        str(match.page.id),
    )

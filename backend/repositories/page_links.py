"""Current authored-link grammar for stored PageLink read projections."""

from sqlalchemy import and_, case, func, or_

from backend.models.context import ContextPage, PageLink
from backend.repositories.workspace_scoped import join_in_workspace, select_in_workspace


def active_page_link_condition(model=PageLink):
    return ~model.locator_key.startswith(
        "concept_path:", autoescape=True
    ) & ~model.locator_key.startswith("slug:", autoescape=True)


def _supported_wikilink_weight(edge):
    """Read an old projection through its current published PageLink rows.

    Only projections backed by retired locators need an override. Keeping NULL
    for all other rows preserves unrelated and historical projection behavior.
    """
    active = active_page_link_condition()
    return (
        join_in_workspace(
            select_in_workspace(
                PageLink,
                workspace_id=edge.workspace_id,
                conditions=(
                    PageLink.source_page_id == edge.source_id,
                    PageLink.target_page_id == edge.target_id,
                    PageLink.source_version_id
                    == ContextPage.current_published_version_id,
                ),
            ),
            PageLink,
            ContextPage,
            workspace_id=edge.workspace_id,
            on=ContextPage.id == PageLink.source_page_id,
        )
        .with_only_columns(func.sum(case((active, PageLink.occurrence_count), else_=0)))
        .having(func.sum(case((~active, PageLink.occurrence_count), else_=0)) > 0)
        .correlate_except(PageLink, ContextPage)
        .scalar_subquery()
    )


def _is_page_link_edge(edge):
    return and_(
        edge.edge_kind == "wikilink",
        edge.source_kind == "context_page",
        edge.target_kind == "context_page",
    )


def active_page_link_edge_condition(edge):
    """Exclude stored edges supported only by retired authored links."""
    return or_(
        ~_is_page_link_edge(edge),
        func.coalesce(_supported_wikilink_weight(edge), 1) > 0,
    )


def active_page_link_edge_weight(edge):
    """Remove retired occurrences from mixed stored wikilink weights."""
    return case(
        (
            _is_page_link_edge(edge),
            func.coalesce(_supported_wikilink_weight(edge), edge.weight),
        ),
        else_=edge.weight,
    )

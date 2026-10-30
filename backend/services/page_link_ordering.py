"""Shared deterministic ordering for authored PageLink snippets."""

from __future__ import annotations

from typing import Any

from sqlalchemy import case
from sqlalchemy.sql.elements import ColumnElement

from backend.models.context import PageLink


def page_link_snippet_order() -> tuple[ColumnElement[Any], ...]:
    """Prefer useful legacy snippets before stable row-level tie breakers."""

    return (
        case((PageLink.first_snippet.is_not(None), 0), else_=1),
        case(
            (PageLink.link_syntax == "wikilink", 0),
            (PageLink.link_syntax == "context_url", 1),
            else_=2,
        ),
        PageLink.target_slug.asc(),
        PageLink.id.asc(),
    )

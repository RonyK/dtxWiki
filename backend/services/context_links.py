"""Workspace-scoped authored link identity, resolution, and safe rewrites.

Every consumer uses these decisions. In particular a foreign locator never
causes a database read, and an existing pin always precedes a name lookup.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.context import (
    ContextPage,
    ContextPageVersion,
    PageLink,
    PageNameIndex,
)
from backend.services.context_visibility import visible_context_version_id

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_WIKI = re.compile(r"\[\[([^\]\n|]+)(?:\|([^\]\n]+))?\]\]")


def format_stable_url(
    workspace_id: UUID | str, page_id: UUID | str, fragment: str | None = None
) -> str:
    href = f"/p/{workspace_id}/{page_id}"
    return href + (f"#{fragment.lstrip('#')}" if fragment else "")


def escape_link_label(title: str) -> str:
    return re.sub(r"([\\`*_\[\]<>!&~])", r"\\\1", title)


def link_markdown(
    workspace_id: UUID | str,
    page_id: UUID | str,
    title: str,
    fragment: str | None = None,
) -> str:
    href = format_stable_url(workspace_id, page_id, fragment)
    return f"[{escape_link_label(title)}]({href})"


@dataclass(frozen=True)
class Locator:
    kind: str
    workspace_id: UUID | None = None
    page_id: UUID | None = None
    node_id: UUID | None = None
    slug: str | None = None
    fragment: str | None = None
    href: str = ""
    absolute: bool = False

    @property
    def key(self) -> str:
        if self.page_id is not None:
            return f"page_id:{self.page_id}"
        if self.node_id is not None:
            return f"nav_target:{self.node_id}"
        return f"{self.kind}:{self.slug or self.href}"


def _uuid(value: str) -> UUID | None:
    return UUID(value) if re.fullmatch(_UUID, value) else None


def _slug(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip()


def normalize_wikilink_target(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", (value or "").strip()).lower()
    return re.sub(r"[\W_]+", "-", normalized).strip("-")[:80]


def configured_self_origins(settings=None) -> frozenset[str]:
    if settings is None:
        from backend.app.settings import get_settings

        settings = get_settings()
    values = set()
    if settings.public_base_url:
        values.add(settings.public_base_url)
    for host in (settings.allowed_hosts or "").split(","):
        host = host.strip()
        if host and host != "*":
            values.add(host)
    return frozenset(values)


def _self_origin(parts, self_origins) -> bool:
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
        for value in self_origins:
            expected = urlsplit(value if "://" in value else "https://" + value)
            if parts.hostname != expected.hostname:
                continue
            if "://" not in value:
                if expected.port is None or port == expected.port:
                    return True
            elif parts.scheme.lower() == expected.scheme.lower() and port == (
                expected.port or (443 if expected.scheme == "https" else 80)
            ):
                return True
    except ValueError:
        return False
    return False


def classify(href: str, self_origins=()) -> Locator:
    wiki = _WIKI.fullmatch(href) if isinstance(href, str) else None
    if wiki:
        target, _, fragment = wiki[1].partition("#")
        return Locator(
            "wikilink" if target.strip() else "local",
            slug=normalize_wikilink_target(target),
            fragment=fragment or None,
            href=href,
        )
    if (
        not isinstance(href, str)
        or not href
        or href != href.strip()
        or re.search(r"[\x00-\x20\\]", href)
        or href.startswith("//")
    ):
        return Locator("unknown", href=href if isinstance(href, str) else "")
    if (page_id := _uuid(href)) is not None:
        return Locator("page_id", page_id=page_id, href=href)
    try:
        parts = urlsplit(href)
        if parts.username is not None or parts.password is not None:
            return Locator("unknown", href=href)
        absolute = parts.scheme.lower() in {"http", "https"}
        if absolute and not _self_origin(parts, self_origins):
            return Locator("external", href=href)
        if parts.scheme and not absolute and parts.scheme != "dtxwiki":
            return Locator(
                "media"
                if parts.scheme in {"asset", "context-media", "dtx-media"}
                else "external",
                href=href,
            )
        path = parts.path
        if re.search(r"%(?:2f|5c|2e|25|00)", path, re.I) or re.search(
            r"%(?![0-9a-fA-F]{2})", path
        ):
            return Locator("unknown", href=href)
        decoded = unquote(path, errors="strict")
        if any(part in {".", ".."} for part in decoded.split("/")):
            return Locator("unknown", href=href)
        base = dict(href=href, fragment=parts.fragment or None, absolute=absolute)
        if href.startswith("#"):
            return Locator("local", **base)
        if re.fullmatch(r"/w/[^/]+/pages/[^/]+", decoded):
            return Locator("public_reader", **base)
        if decoded.startswith(("/api/", "/uploads/")):
            return Locator("media", **base)
        if parts.scheme == "dtxwiki":
            match = re.fullmatch(rf"/({_UUID})/context/pages/([^/]+)", decoded)
            if parts.netloc == "workspaces" and match:
                return Locator(
                    "resource_uri",
                    workspace_id=UUID(match[1]),
                    slug=_slug(match[2]),
                    **base,
                )
            return Locator("unknown", **base)
        if (
            parts.query
            and decoded != "/context"
            and decoded.startswith(("/p/", "/context/", "/workspaces/", "/pages/"))
        ):
            return Locator("unknown", **base)
        if match := re.fullmatch(rf"/p/({_UUID})/({_UUID})", decoded):
            return Locator(
                "stable_full",
                workspace_id=UUID(match[1]),
                page_id=UUID(match[2]),
                **base,
            )
        if match := re.fullmatch(rf"/p/({_UUID})", decoded):
            return Locator("stable_short", page_id=UUID(match[1]), **base)
        if match := re.fullmatch(
            rf"/workspaces/({_UUID})/context/pages/({_UUID})(?:/edit)?", decoded
        ):
            return Locator(
                "canonical_route",
                workspace_id=UUID(match[1]),
                page_id=UUID(match[2]),
                **base,
            )
        if decoded == "/context":
            query = parse_qs(parts.query)
            values = query.get("target", [])
            if len(values) == 1 and (node_id := _uuid(values[0])) is not None:
                return Locator("nav_target", node_id=node_id, **base)
        if match := re.fullmatch(r"/context/(?:[^/]+/)*([^/]+)\.md", decoded):
            return Locator("concept_path", slug=_slug(match[1]), **base)
        if match := re.fullmatch(r"/pages/([^/]+)", decoded):
            return Locator("slug", slug=_slug(match[1]), **base)
        return Locator(
            "unknown"
            if absolute
            or decoded.startswith(("/p/", "/context", "/workspaces/", "/pages/"))
            else "external",
            **base,
        )
    except (ValueError, UnicodeError):
        return Locator("unknown", href=href)


def parse_page_input(value: str | UUID) -> Locator:
    locator = classify(str(value))
    # Legacy MCP paths historically accepted a title/alias containing spaces.
    # This is input compatibility, never a second body-URL classification rule.
    if locator.kind == "unknown" and str(value).startswith("/context/"):
        locator = classify(str(value).replace(" ", "%20"))
    if locator.kind not in {"page_id", "stable_full", "stable_short", "concept_path"}:
        raise ValueError("Expected a Context page UUID or permanent page URL")
    return locator


class PageLocatorNotFound(ValueError):
    pass


@dataclass(frozen=True)
class LinkContext:
    session: Session
    workspace_id: UUID
    actor: object | None = None
    source_page_id: UUID | None = None
    source_version_id: UUID | None = None
    source_is_published: bool = False
    self_origins: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Resolution:
    state: str
    page_id: UUID | None = None
    href: str | None = None
    target_slug: str | None = None
    published: bool = False


def resolve(
    locator: Locator,
    ctx: LinkContext,
    *,
    preserved_pins: Mapping[str, UUID] | None = None,
) -> Resolution:
    if locator.workspace_id is not None and locator.workspace_id != ctx.workspace_id:
        return Resolution("foreign")
    if locator.kind in {"external", "public_reader", "local", "media"}:
        return Resolution("ignored")
    session = ctx.session
    pinned = (preserved_pins or {}).get(locator.key)
    if pinned is None and locator.node_id is not None:
        pinned = (preserved_pins or {}).get(f"page_id:{locator.node_id}")
    if pinned is None and ctx.source_page_id is not None:
        statement = select(PageLink.target_page_id).where(
            PageLink.workspace_id == ctx.workspace_id,
            PageLink.source_page_id == ctx.source_page_id,
            PageLink.target_page_id.is_not(None),
        )
        # During a rolling upgrade the legacy model has no locator_key yet.
        statement = statement.where(PageLink.locator_key == locator.key)
        if ctx.source_version_id is not None:
            statement = statement.where(
                PageLink.source_version_id == ctx.source_version_id
            )
        pinned = session.scalar(statement.order_by(PageLink.created_at.desc()).limit(1))
        if pinned is None and ctx.source_version_id is not None:
            # A newly opened copy has no projection yet. Live source pins
            # still own its locators before the save-time fixer runs.
            keys = [locator.key]
            if locator.node_id is not None:
                keys.append(f"page_id:{locator.node_id}")
            pinned = session.scalar(
                select(PageLink.target_page_id)
                .where(
                    PageLink.workspace_id == ctx.workspace_id,
                    PageLink.source_page_id == ctx.source_page_id,
                    PageLink.locator_key.in_(keys),
                    PageLink.target_page_id.is_not(None),
                )
                .order_by(PageLink.created_at.desc())
                .limit(1)
            )
    page_id = pinned or locator.page_id
    if page_id is None and locator.node_id is not None:
        page_id = session.scalar(
            select(ContextPage.id).where(
                ContextPage.workspace_id == ctx.workspace_id,
                ContextPage.id == locator.node_id,
            )
        )
        if page_id is None:
            from backend.models.tree import WorkspaceTreeNode

            page_id = session.scalar(
                select(WorkspaceTreeNode.target_id).where(
                    WorkspaceTreeNode.workspace_id == ctx.workspace_id,
                    WorkspaceTreeNode.id == locator.node_id,
                    WorkspaceTreeNode.target_kind == "context_page",
                )
            )
    statement = select(ContextPage).where(ContextPage.workspace_id == ctx.workspace_id)
    if page_id is not None:
        pages = list(session.scalars(statement.where(ContextPage.id == page_id)))
    elif locator.slug is not None:
        pages = list(session.scalars(statement.where(ContextPage.slug == locator.slug)))
    else:
        return Resolution("unavailable")
    if len(pages) != 1:
        if len(pages) > 1:
            return Resolution("ambiguous")
        if locator.slug:
            from backend.services.entity_ref import normalize_entity_name

            candidates = (
                session.scalars(
                    statement.join(
                        PageNameIndex, PageNameIndex.page_id == ContextPage.id
                    ).where(
                        PageNameIndex.workspace_id == ctx.workspace_id,
                        PageNameIndex.name_norm == normalize_entity_name(locator.slug),
                    )
                )
                .unique()
                .all()
            )
            visible = [page for page in candidates if _visible_page(page, ctx)]
            if len(visible) == 1:
                return Resolution("needs_review", page_id=visible[0].id)
            if len(visible) > 1:
                return Resolution("ambiguous")
        return Resolution("unavailable")
    page = pages[0]
    if not _visible_page(page, ctx):
        return Resolution("unavailable")
    return Resolution(
        "ok",
        page.id,
        format_stable_url(ctx.workspace_id, page.id, locator.fragment),
        page.slug,
        _published_page(page, ctx),
    )


def _published_page(page, ctx) -> bool:
    if page.current_published_version_id is None:
        return False
    version = ctx.session.scalar(
        select(ContextPageVersion).where(
            ContextPageVersion.workspace_id == ctx.workspace_id,
            ContextPageVersion.page_id == page.id,
            ContextPageVersion.id == page.current_published_version_id,
        )
    )
    return version is not None and version.version_status in {
        "published_snapshot",
        "superseded_snapshot",
    }


def _visible_page(page, ctx) -> bool:
    if page.lifecycle_status == "archived":
        return False
    version_id, _ = visible_context_version_id(page, ctx.actor)
    if version_id is None:
        return False
    version = ctx.session.scalar(
        select(ContextPageVersion).where(
            ContextPageVersion.workspace_id == ctx.workspace_id,
            ContextPageVersion.page_id == page.id,
            ContextPageVersion.id == version_id,
        )
    )
    from backend.services.actor_visible_read import context_version_is_visible

    return version is not None and context_version_is_visible(version, ctx.actor)


def resolve_page_input(value: str | UUID, ctx: LinkContext) -> UUID:
    result = resolve(parse_page_input(value), ctx)
    if result.state not in {"ok", "needs_review"} or result.page_id is None:
        raise PageLocatorNotFound("Context page not found")
    return result.page_id

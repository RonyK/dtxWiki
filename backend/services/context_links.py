"""Workspace-scoped authored link identity, resolution, and extraction.

Foreign locators never cause a target database read, and an existing pin
always precedes a name lookup. This module only classifies and reads links.
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import UUID

from sqlalchemy.orm import Session

from backend.context_media.domain.markdown_media import SourceSpan
from backend.models.context import (
    ContextPage,
    ContextPageVersion,
    PageLink,
    PageNameIndex,
)
from backend.repositories.workspace_scoped import join_in_workspace, select_in_workspace
from backend.services.context_visibility import visible_context_version_id

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
MCP_PAGE_INPUT_PATTERN = rf"^(?:{_UUID}|/p/{_UUID}(?:/{_UUID})?(?:#[^\s]*)?)$"
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


def normalize_heading(value: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]+", "-", value.strip().lower())).strip(
        "-"
    )[:80]


def configured_self_origins(settings=None) -> frozenset[str]:
    if settings is None:
        from backend.app.settings import get_settings

        settings = get_settings()
    values = set()
    public_base_url = getattr(settings, "public_base_url", None)
    allowed_hosts = getattr(settings, "allowed_hosts", None)
    if public_base_url:
        values.add(public_base_url)
    for host in (allowed_hosts or "").split(","):
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
            fragment=normalize_heading(fragment) or None,
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
            # Context resource URIs have no query contract. Preserve the
            # authored URI for diagnosis instead of resolving by slug and
            # silently dropping an unsupported query during canonicalization.
            if "?" in href.split("#", 1)[0]:
                return Locator("unknown", **base)
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


_LINK_LOOKUP_CHUNK_SIZE = 400


def _chunks(values: list, size: int = _LINK_LOOKUP_CHUNK_SIZE):
    for start in range(0, len(values), size):
        yield values[start : start + size]


class LinkResolutionBatch:
    """Request-local, workspace-scoped reads shared by authored-link resolvers.

    This is only a read snapshot for one response. Callers that need write-time
    freshness continue to use ``resolve`` without a batch.
    """

    def __init__(self, locators: list[Locator], ctx: LinkContext) -> None:
        self.workspace_id = ctx.workspace_id
        self.source_version_id = ctx.source_version_id
        self.pins: dict[str, UUID] = {}
        self.latest_pins: dict[str, tuple[datetime, UUID]] = {}
        self.node_page_ids: dict[UUID, UUID] = {}
        self.pages_by_id: dict[UUID, ContextPage] = {}
        self.pages_by_slug: dict[str, list[ContextPage]] = {}
        self.alias_pages: dict[str, list[ContextPage]] = {}
        self.visible_version_ids: dict[UUID, UUID | None] = {}
        self.versions: dict[UUID, ContextPageVersion] = {}

        eligible = [
            locator
            for locator in locators
            if locator.kind
            not in {"external", "public_reader", "local", "media", "unknown"}
            and locator.workspace_id in {None, ctx.workspace_id}
        ]
        if not eligible:
            return

        keys = {locator.key for locator in eligible}
        node_ids = {locator.node_id for locator in eligible if locator.node_id}
        if node_ids:
            keys.update(f"page_id:{node_id}" for node_id in node_ids)
        if ctx.source_page_id is not None:
            key_list = list(keys)
            for key_chunk in _chunks(key_list):
                conditions = [
                    PageLink.source_page_id == ctx.source_page_id,
                    PageLink.target_page_id.is_not(None),
                    PageLink.locator_key.in_(key_chunk),
                ]
                if ctx.source_version_id is not None:
                    conditions.append(
                        PageLink.source_version_id == ctx.source_version_id
                    )
                rows = ctx.session.execute(
                    select_in_workspace(
                        PageLink, workspace_id=ctx.workspace_id, conditions=conditions
                    )
                    .with_only_columns(
                        PageLink.locator_key,
                        PageLink.target_page_id,
                        PageLink.created_at,
                    )
                    .order_by(PageLink.created_at.desc())
                )
                for key, target_id, _created_at in rows:
                    self.pins.setdefault(key, target_id)
            if ctx.source_version_id is not None:
                # The scalar fallback chooses the newest pin across the
                # locator's legacy aliases, not a fixed key priority. Keep
                # that ordering for nav_target/page_id dual-key migrations.
                fallback_keys: set[str] = set()
                for locator in eligible:
                    if locator.key in self.pins:
                        continue
                    fallback_keys.add(locator.key)
                    if locator.node_id is not None:
                        fallback_keys.add(f"page_id:{locator.node_id}")
                for key_chunk in _chunks(list(fallback_keys)):
                    rows = ctx.session.execute(
                        select_in_workspace(
                            PageLink,
                            workspace_id=ctx.workspace_id,
                            conditions=(
                                PageLink.source_page_id == ctx.source_page_id,
                                PageLink.locator_key.in_(key_chunk),
                                PageLink.target_page_id.is_not(None),
                            ),
                        )
                        .with_only_columns(
                            PageLink.locator_key,
                            PageLink.target_page_id,
                            PageLink.created_at,
                        )
                        .order_by(PageLink.created_at.desc())
                    )
                    for key, target_id, created_at in rows:
                        self.latest_pins.setdefault(key, (created_at, target_id))

        if node_ids:
            from backend.models.tree import WorkspaceTreeNode

            for node_chunk in _chunks(list(node_ids)):
                nodes = ctx.session.scalars(
                    select_in_workspace(
                        WorkspaceTreeNode,
                        workspace_id=ctx.workspace_id,
                        conditions=(
                            WorkspaceTreeNode.id.in_(node_chunk),
                            WorkspaceTreeNode.target_kind == "context_page",
                            WorkspaceTreeNode.target_id.is_not(None),
                        ),
                    )
                )
                self.node_page_ids.update(
                    (node.id, node.target_id) for node in nodes
                )

        page_ids = {
            locator.page_id for locator in eligible if locator.page_id is not None
        }
        page_ids.update(node_ids)
        page_ids.update(self.pins.values())
        page_ids.update(
            target_id for _created_at, target_id in self.latest_pins.values()
        )
        page_ids.update(self.node_page_ids.values())
        direct_slugs = {
            locator.slug for locator in eligible if locator.slug is not None
        }
        from backend.services.entity_ref import normalize_entity_name

        for page_id_chunk in _chunks(list(page_ids)):
            pages = ctx.session.scalars(
                select_in_workspace(
                    ContextPage,
                    workspace_id=ctx.workspace_id,
                    conditions=(ContextPage.id.in_(page_id_chunk),),
                )
            )
            for page in pages:
                self._add_page(page)
        for slug_chunk in _chunks(list(direct_slugs)):
            pages = ctx.session.scalars(
                select_in_workspace(
                    ContextPage,
                    workspace_id=ctx.workspace_id,
                    conditions=(ContextPage.slug.in_(slug_chunk),),
                )
            )
            for page in pages:
                self._add_page(page)

        missing_aliases = {
            slug
            for slug in direct_slugs
            if not self.pages_by_slug.get(slug)
        }
        alias_names: dict[str, set[str]] = {}
        for slug in missing_aliases:
            alias_names.setdefault(normalize_entity_name(slug), set()).add(slug)
        alias_name_list = list(alias_names)
        if alias_name_list:
            for name_chunk in _chunks(alias_name_list):
                rows = ctx.session.execute(
                    join_in_workspace(
                        select_in_workspace(
                            ContextPage, workspace_id=ctx.workspace_id
                        ),
                        ContextPage,
                        PageNameIndex,
                        workspace_id=ctx.workspace_id,
                        on=(PageNameIndex.page_id == ContextPage.id)
                        & (PageNameIndex.name_norm.in_(name_chunk)),
                    ).add_columns(PageNameIndex.name_norm)
                ).unique()
                for page, name in rows:
                    self._add_page(page)
                    for original_slug in alias_names.get(name, ()):
                        values = self.alias_pages.setdefault(original_slug, [])
                        if all(existing.id != page.id for existing in values):
                            values.append(page)

        all_page_ids = list(self.pages_by_id)
        if not all_page_ids:
            return
        from backend.repositories.context_visibility import (
            visible_context_version_expression,
        )

        for id_chunk in _chunks(all_page_ids):
            rows = ctx.session.execute(
                select_in_workspace(
                    ContextPage,
                    workspace_id=ctx.workspace_id,
                    conditions=(ContextPage.id.in_(id_chunk),),
                ).with_only_columns(
                    ContextPage.id,
                    visible_context_version_expression(ctx.actor),
                )
            )
            for page_id, version_id in rows:
                self.visible_version_ids[page_id] = version_id
        version_ids = {
            version_id
            for version_id in self.visible_version_ids.values()
            if version_id is not None
        }
        version_ids.update(
            page.current_published_version_id
            for page in self.pages_by_id.values()
            if page.current_published_version_id is not None
        )
        for id_chunk in _chunks(list(version_ids)):
            versions = ctx.session.scalars(
                select_in_workspace(
                    ContextPageVersion,
                    workspace_id=ctx.workspace_id,
                    conditions=(ContextPageVersion.id.in_(id_chunk),),
                )
            )
            self.versions.update((version.id, version) for version in versions)

    def _add_page(self, page: ContextPage) -> None:
        self.pages_by_id[page.id] = page
        values = self.pages_by_slug.setdefault(page.slug, [])
        if all(existing.id != page.id for existing in values):
            values.append(page)

    def page(self, page_id: UUID) -> ContextPage | None:
        return self.pages_by_id.get(page_id)

    def pin_for(self, locator: Locator) -> tuple[bool, UUID | None]:
        if locator.key in self.pins:
            return True, self.pins[locator.key]
        if self.source_version_id is None:
            return False, None
        candidate_keys = [locator.key]
        if locator.node_id is not None:
            candidate_keys.append(f"page_id:{locator.node_id}")
        candidates = [
            self.latest_pins[candidate]
            for candidate in candidate_keys
            if candidate in self.latest_pins
        ]
        if candidates:
            return True, max(candidates, key=lambda pin: pin[0])[1]
        return False, None

    def visible(self, page: ContextPage, actor: object | None) -> bool:
        if page.lifecycle_status == "archived":
            return False
        version_id = self.visible_version_ids.get(page.id)
        if version_id is None:
            return False
        from backend.services.actor_visible_read import context_version_is_visible

        version = self.versions.get(version_id)
        return (
            version is not None
            and version.workspace_id == self.workspace_id
            and version.page_id == page.id
            and context_version_is_visible(version, actor)
        )

    def published(self, page: ContextPage) -> bool:
        if page.current_published_version_id is None:
            return False
        version = self.versions.get(page.current_published_version_id)
        return (
            version is not None
            and version.workspace_id == self.workspace_id
            and version.page_id == page.id
            and version.version_status
            in {"published_snapshot", "superseded_snapshot"}
        )


def resolve(
    locator: Locator,
    ctx: LinkContext,
    *,
    preserved_pins: Mapping[str, UUID] | None = None,
    batch: LinkResolutionBatch | None = None,
) -> Resolution:
    if locator.workspace_id is not None and locator.workspace_id != ctx.workspace_id:
        return Resolution("foreign")
    if locator.kind in {"external", "public_reader", "local", "media"}:
        return Resolution("ignored")
    session = ctx.session
    pinned = (preserved_pins or {}).get(locator.key)
    if pinned is None and locator.node_id is not None:
        pinned = (preserved_pins or {}).get(f"page_id:{locator.node_id}")
    batch_pin_known = False
    if pinned is None and batch is not None:
        batch_pin_known, pinned = batch.pin_for(locator)
    if (
        pinned is None
        and not batch_pin_known
        and batch is None
        and ctx.source_page_id is not None
    ):
        conditions = [
            PageLink.source_page_id == ctx.source_page_id,
            PageLink.target_page_id.is_not(None),
            PageLink.locator_key == locator.key,
        ]
        # During a rolling upgrade the legacy model has no locator_key yet.
        if ctx.source_version_id is not None:
            conditions.append(PageLink.source_version_id == ctx.source_version_id)
        statement = select_in_workspace(
            PageLink, workspace_id=ctx.workspace_id, conditions=conditions
        ).with_only_columns(PageLink.target_page_id)
        pinned = session.scalar(statement.order_by(PageLink.created_at.desc()).limit(1))
        if pinned is None and ctx.source_version_id is not None:
            # A newly opened copy has no projection yet. Live source pins
            # still own its locators before the save-time fixer runs.
            keys = [locator.key]
            if locator.node_id is not None:
                keys.append(f"page_id:{locator.node_id}")
            pinned = session.scalar(
                select_in_workspace(
                    PageLink,
                    workspace_id=ctx.workspace_id,
                    conditions=(
                        PageLink.source_page_id == ctx.source_page_id,
                        PageLink.locator_key.in_(keys),
                        PageLink.target_page_id.is_not(None),
                    ),
                )
                .with_only_columns(PageLink.target_page_id)
                .order_by(PageLink.created_at.desc())
                .limit(1)
            )
    page_id = pinned or locator.page_id
    if page_id is None and locator.node_id is not None:
        page_id = (
            locator.node_id
            if batch is not None and batch.page(locator.node_id) is not None
            else (
                batch.node_page_ids.get(locator.node_id)
                if batch is not None
                else session.scalar(
                    select_in_workspace(
                        ContextPage,
                        workspace_id=ctx.workspace_id,
                        conditions=(ContextPage.id == locator.node_id,),
                    ).with_only_columns(ContextPage.id)
                )
            )
        )
        if page_id is None and batch is None:
            from backend.models.tree import WorkspaceTreeNode

            page_id = session.scalar(
                select_in_workspace(
                    WorkspaceTreeNode,
                    workspace_id=ctx.workspace_id,
                    conditions=(
                        WorkspaceTreeNode.id == locator.node_id,
                        WorkspaceTreeNode.target_kind == "context_page",
                    ),
                ).with_only_columns(WorkspaceTreeNode.target_id)
            )
    if page_id is not None:
        condition = ContextPage.id == page_id
    elif locator.slug is not None:
        condition = ContextPage.slug == locator.slug
    else:
        return Resolution("unavailable")
    if batch is not None:
        page = batch.page(page_id) if page_id is not None else None
        pages = [page] if page is not None else []
        if page_id is None and locator.slug is not None:
            pages = batch.pages_by_slug.get(locator.slug, [])
            if not pages:
                visible_aliases = [
                    candidate
                    for candidate in batch.alias_pages.get(locator.slug, [])
                    if batch.visible(candidate, ctx.actor)
                ]
                if len(visible_aliases) == 1:
                    return Resolution("needs_review", page_id=visible_aliases[0].id)
                if len(visible_aliases) > 1:
                    return Resolution("ambiguous")
    else:
        pages = list(
            session.scalars(
                select_in_workspace(
                    ContextPage, workspace_id=ctx.workspace_id, conditions=(condition,)
                )
            )
        )
    if len(pages) != 1:
        if len(pages) > 1:
            return Resolution("ambiguous")
        if locator.slug and batch is not None:
            visible = [
                candidate
                for candidate in batch.alias_pages.get(locator.slug, [])
                if batch.visible(candidate, ctx.actor)
            ]
            if len(visible) == 1:
                return Resolution("needs_review", page_id=visible[0].id)
            if len(visible) > 1:
                return Resolution("ambiguous")
        elif locator.slug:
            from backend.services.entity_ref import normalize_entity_name

            candidates = (
                session.scalars(
                    join_in_workspace(
                        select_in_workspace(ContextPage, workspace_id=ctx.workspace_id),
                        ContextPage,
                        PageNameIndex,
                        workspace_id=ctx.workspace_id,
                        on=(PageNameIndex.page_id == ContextPage.id)
                        & (
                            PageNameIndex.name_norm
                            == normalize_entity_name(locator.slug)
                        ),
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
    if not (
        batch.visible(page, ctx.actor)
        if batch is not None
        else _visible_page(page, ctx)
    ):
        return Resolution("unavailable")
    return Resolution(
        "ok",
        page.id,
        format_stable_url(ctx.workspace_id, page.id, locator.fragment),
        page.slug,
        batch.published(page) if batch is not None else _published_page(page, ctx),
    )


def _published_page(page, ctx) -> bool:
    if page.current_published_version_id is None:
        return False
    version = ctx.session.scalar(
        select_in_workspace(
            ContextPageVersion,
            workspace_id=ctx.workspace_id,
            conditions=(
                ContextPageVersion.page_id == page.id,
                ContextPageVersion.id == page.current_published_version_id,
            ),
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
        select_in_workspace(
            ContextPageVersion,
            workspace_id=ctx.workspace_id,
            conditions=(
                ContextPageVersion.page_id == page.id,
                ContextPageVersion.id == version_id,
            ),
        )
    )
    from backend.services.actor_visible_read import context_version_is_visible

    return version is not None and context_version_is_visible(version, ctx.actor)


def resolve_page_input(value: str | UUID, ctx: LinkContext) -> UUID:
    result = resolve(parse_page_input(value), ctx)
    if result.state not in {"ok", "needs_review"} or result.page_id is None:
        raise PageLocatorNotFound("Context page not found")
    return result.page_id


@dataclass(frozen=True)
class LinkOccurrence:
    href: str
    kind: str
    span: SourceSpan
    slug: str | None = None
    label: str | None = None
    snippet: str = ""

    @property
    def destination_span(self) -> SourceSpan:
        return self.span


def _span(body: str, start: int, end: int) -> SourceSpan:
    return SourceSpan(
        start, end, len(body[:start].encode("utf-8")), len(body[:end].encode("utf-8"))
    )


def _markdown_parser():
    from markdown_it import MarkdownIt

    return MarkdownIt("commonmark", {"html": True}).enable("table")


def _rendered_destinations(body: str) -> list[str]:
    return [
        child.attrGet("href")
        for token in _markdown_parser().parse(body)
        for child in token.children or ()
        if child.type == "link_open"
    ]


def _plain_text_nodes(body: str) -> list[str]:
    result = []
    for token in _markdown_parser().parse(body):
        link_depth = 0
        for child in token.children or ():
            if child.type == "link_open":
                link_depth += 1
            elif child.type == "link_close":
                link_depth -= 1
            elif child.type == "text" and not link_depth:
                result.append(child.content)
    return result


def extract_links(body: str) -> tuple[LinkOccurrence, ...]:
    """Verify exact source destinations against the original rendered structure.

    Shared reference definitions are deliberately not rewritable: a definition
    may serve an image or another link. The probe never creates eligibility;
    original text-node membership is checked before probing a wikilink.
    """
    from collections import Counter
    from hashlib import sha256

    from backend.context_media.domain.markdown_media import (
        _GFM_AUTOLINK_LITERAL,
        _code_mask,
        _gfm_literal_source_spans_visible,
        _gfm_literal_url_candidates,
        _is_escaped,
        _line_offsets,
        _math_mask,
        _parse_bracket,
        _parse_definition_line,
        _parse_image_use,
        _parse_inline_image_destination,
        _parse_link_reference,
        _rendered_page_body,
    )

    rendered, offset = _rendered_page_body(body)
    masked = _code_mask(rendered)
    math_mask = _math_mask(rendered)
    for index, value in enumerate(math_mask):
        if value:
            masked[index] = 1
    line_offsets = _line_offsets(rendered)
    tokens = _markdown_parser().parse(rendered)
    for token in tokens:
        if token.type == "html_block" and token.map:
            start, end = line_offsets[token.map[0]], line_offsets[token.map[1]]
            masked[start:end] = b"\x01" * (end - start)
    # Raw HTML, including its contents, does not participate in authored links.
    # Track paired tags so nesting and mixed-case closing tags cannot leave
    # part of an HTML region eligible. Quoted attribute values may contain >.
    html_stack: list[tuple[str, int]] = []
    void_tags = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
    for match in re.finditer(
        r"<!--.*?(?:-->|$)|</?(?P<tag>[A-Za-z][\w:-]*)(?=\s|/?>)"
        r"(?:\"[^\"]*\"|'[^']*'|[^'\"<>])*?>",
        rendered,
        re.S,
    ):
        start, end = match.span()
        if masked[start]:
            continue
        masked[start:end] = b"\x01" * (end - start)
        if match["tag"] is None:
            continue
        tag = match["tag"].lower()
        if match[0].startswith("</"):
            for index in range(len(html_stack) - 1, -1, -1):
                if html_stack[index][0] == tag:
                    start = html_stack[index][1]
                    masked[start:end] = b"\x01" * (end - start)
                    del html_stack[index:]
                    break
        elif tag not in void_tags and not match[0].endswith("/>"):
            html_stack.append((tag, start))
    for _, start in html_stack:
        masked[start:] = b"\x01" * (len(rendered) - start)

    definitions = {}
    definition_counts = Counter()
    definition_ranges = []
    cursor = 0
    for line in rendered.splitlines(keepends=True):
        end = cursor + len(line.rstrip("\r\n"))
        definition = _parse_definition_line(rendered, cursor, end, masked)
        if definition is not None and not any(
            masked[definition.syntax_start : definition.syntax_end]
        ):
            definitions[definition.label] = definition
            definition_counts[definition.label] += 1
            definition_ranges.append((cursor, cursor + len(line)))
        cursor += len(line)

    candidates = []
    uses = Counter()
    reference_uses = []
    original_text = _plain_text_nodes(rendered)
    eligible_wikis = {
        match[0] for node in original_text for match in _WIKI.finditer(node)
    }
    wiki_candidates = []
    salt = sha256(body.encode()).hexdigest()[:16]
    while f"dtxlinkprobe{salt}" in rendered:
        salt += "x"
    cursor = 0
    while cursor < len(rendered):
        definition_end = next(
            (end for start, end in definition_ranges if start <= cursor < end), None
        )
        if definition_end is not None:
            cursor = definition_end
            continue
        if masked[cursor] or _is_escaped(rendered, cursor):
            cursor += 1
            continue
        if rendered.startswith("![", cursor):
            image = _parse_image_use(rendered, cursor, masked)
            if image is not None:
                if image.reference_label:
                    uses[image.reference_label] += 1
                cursor = image.syntax_end
                continue
        wiki = _WIKI.match(rendered, cursor)
        if wiki:
            raw = wiki[0]
            # Replacing [[tar*get*]] with a probe FIRST would incorrectly turn
            # emphasis/text into a valid wikilink. Keep the original parse.
            decoded = html.unescape(
                re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])", r"\1", raw)
            )
            if decoded in eligible_wikis:
                wiki_candidates.append(
                    (cursor, wiki.end(), raw, decoded, wiki[1], wiki[2])
                )
            cursor = wiki.end()
            continue
        if rendered[cursor] == "[":
            bracket = _parse_bracket(rendered, cursor, masked)
            if bracket:
                bracket_end, label = bracket
                if bracket_end < len(rendered) and rendered[bracket_end] == "(":
                    parsed = _parse_inline_image_destination(rendered, bracket_end)
                    if parsed:
                        start, end, syntax_end = parsed
                        if not any(masked[start:end]):
                            candidates.append(
                                (
                                    start,
                                    end,
                                    rendered[start:end],
                                    "markdown",
                                    None,
                                    label,
                                )
                            )
                        cursor = syntax_end
                        continue
                reference = _parse_link_reference(rendered, cursor, masked)
                if reference and reference[0] in definitions:
                    label_key, syntax_end = reference
                    uses[label_key] += 1
                    reference_uses.append((label_key, label))
                    cursor = syntax_end
                    continue
        if rendered[cursor] == "<":
            angle = re.match(
                r"<((?:https?://|dtxwiki://)[^<>\s]+)>", rendered[cursor:], re.I
            )
            if angle:
                candidates.append(
                    (
                        cursor,
                        cursor + len(angle[0]),
                        angle[1],
                        "autolink",
                        None,
                        angle[1],
                    )
                )
                cursor += len(angle[0])
                continue
        cursor += 1
    # Insert markers around, never instead of, original wiki syntax. A single
    # parse qualifies every occurrence without an O(links) document reparse.
    marked_parts = []
    cursor = 0
    marked_wikis = []
    for index, (start, end, raw, decoded, slug, label) in enumerate(wiki_candidates):
        before, after = f"dtxlinkprobe{salt}a{index}x", f"dtxlinkprobe{salt}b{index}x"
        marked_parts.extend((rendered[cursor:start], before, raw, after))
        cursor = end
        marked_wikis.append((before + decoded + after, start, end, raw, slug, label))
    marked_parts.append(rendered[cursor:])
    qualified_nodes = (
        _plain_text_nodes("".join(marked_parts)) if wiki_candidates else []
    )
    for marker, start, end, raw, slug, label in marked_wikis:
        if any(marker in node for node in qualified_nodes):
            candidates.append((start, end, raw, "wikilink", slug, label))

    for label_key, label in reference_uses:
        definition = definitions[label_key]
        if uses[label_key] == 1 and definition_counts[label_key] == 1:
            candidates.append(
                (
                    definition.destination_start,
                    definition.destination_end,
                    definition.destination,
                    "markdown",
                    None,
                    label,
                )
            )

    literals = []
    for match in _GFM_AUTOLINK_LITERAL.finditer(rendered):
        if any(masked[match.start() : match.end()]):
            continue
        # The last candidate has GFM punctuation/entity trimming applied.
        href = _gfm_literal_url_candidates(match[0])[-1]
        end = match.start() + len(href)
        if any(match.start() < stop and end > start for start, stop, *_ in candidates):
            continue
        literals.append((match.start(), end, href))
    visible_literals = _gfm_literal_source_spans_visible(
        rendered, tuple((start, end) for start, end, _ in literals)
    )
    candidates.extend(
        (start, end, href, "literal_autolink", None, href)
        for index, (start, end, href) in enumerate(literals)
        if index in visible_literals
    )

    # All Markdown destinations are probed together, once, like Context media.
    # Every unique marker must be a rendered link exactly once, not an image.
    probes = {}
    pieces = []
    cursor = 0
    ordered = sorted(candidates)
    for index, (start, end, _href, kind, _slug, _label) in enumerate(ordered):
        if start < cursor:
            continue
        probe = f"https://dtxlinkprobe.invalid/{salt}/{index}"
        replacement = f"<{probe}>" if kind == "autolink" else probe
        if kind in {"wikilink", "literal_autolink"}:
            replacement = rendered[start:end]
        pieces.extend((rendered[cursor:start], replacement))
        cursor = end
        probes[index] = probe
    pieces.append(rendered[cursor:])
    rendered_probes = Counter(_rendered_destinations("".join(pieces)))
    result = []
    for index, (start, end, href, kind, slug, label) in enumerate(ordered):
        if index not in probes or (
            kind not in {"wikilink", "literal_autolink"}
            and rendered_probes[probes[index]] != 1
        ):
            continue
        raw_href = (
            href
            if kind in {"autolink", "literal_autolink"}
            else html.unescape(
                re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])", r"\1", href)
            )
        )
        # remark-gfm gives www literals an implicit HTTP scheme. Readers see
        # that destination while the authored label and span stay unchanged.
        if kind == "literal_autolink" and raw_href.lower().startswith("www."):
            raw_href = "http://" + raw_href
        result.append(
            LinkOccurrence(
                raw_href,
                kind,
                _span(body, offset + start, offset + end),
                html.unescape(
                    re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])", r"\1", slug)
                )
                if slug is not None
                else None,
                html.unescape(
                    re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])", r"\1", label)
                )
                if kind == "wikilink" and label is not None
                else label,
                body[max(0, offset + start - 60) : offset + end + 60],
            )
        )
    return tuple(result)

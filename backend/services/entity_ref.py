"""Shared entity identity helpers for search, MCP resources, and traces."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import quote, unquote
from uuid import UUID

from backend.schemas.read import RankedResult
from backend.utils.uuid import parse_uuid_or_none

CONTEXT_RESOURCE_URI_RE = re.compile(
    r"^dtxwiki://workspaces/(?P<workspace_id>[0-9a-fA-F-]{36})/"
    r"context/pages/(?P<slug>[^/?#]+)$"
)


class EntityKind(StrEnum):
    """Canonical entity-kind vocabulary (G0-3).

    Storage strings are unchanged; rosters of kinds (search lanes, trace
    capture, edge projection, MCP chat artifacts) derive from this enum
    instead of repeating string literals.
    """

    CONTEXT_PAGE = "context_page"
    LIBRARY_ITEM = "library_item"
    LIBRARY_ARTIFACT = "library_artifact"
    AGENT_PAGE = "agent_page"
    REVIEW_ITEM = "review_item"
    CONTENT_CHUNK = "content_chunk"
    CONTEXT_CLAIM = "context_claim"
    FEEDBACK = "feedback"
    CHANGE_SET = "change_set"
    ASK_TURN = "ask_turn"
    LIBRARY_ITEM_VERSION = "library_item_version"
    LIBRARY_SYNC_RUN = "library_sync_run"
    WORKSPACE_TREE_NODE = "workspace_tree_node"
    CONTEXT_PAGE_VERSION = "context_page_version"
    # Registered so McpChatArtifactKind.CONTEXT_MEDIA_ASSET (an upload_image
    # provenance edge, Phase 5b) satisfies the G0-3 one-vocabulary contract
    # (test_mcp_chat_resource_kinds_align_with_entity_kind_vocabulary). Every
    # consumer of this enum dispatches off a roster of registered kinds
    # rather than matching exhaustively, so this is additive: no hydrator,
    # search lane, or edge extractor is implied or required by this entry.
    CONTEXT_MEDIA_ASSET = "context_media_asset"


@dataclass(frozen=True)
class EntityRef:
    kind: EntityKind
    id: UUID | None = None
    workspace_id: UUID | None = None
    version_id: UUID | None = None
    concept_path: str | None = None
    resource_uri: str | None = None

    def source_ref(self) -> dict[str, str]:
        data: dict[str, str] = {"kind": self.kind.value}
        if self.id is not None:
            data["id"] = str(self.id)
        if self.version_id is not None:
            data["version_id"] = str(self.version_id)
        if self.concept_path:
            data["concept_path"] = self.concept_path
        if self.resource_uri:
            data["resource_uri"] = self.resource_uri
        return data


def normalize_entity_name(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def coerce_entity_kind(raw: str | EntityKind) -> EntityKind:
    """Validate a kind at an identity boundary; unknown values fail loudly."""

    if isinstance(raw, EntityKind):
        return raw
    return EntityKind(raw)


def context_resource_uri(workspace_id: UUID, slug: str) -> str:
    return f"dtxwiki://workspaces/{workspace_id}/context/pages/{quote(slug, safe='')}"


def context_concept_path(slug: str) -> str:
    return f"/context/{slug}.md"


def library_concept_path(item_id: UUID) -> str:
    return f"/library/{item_id}.md"


def parse_context_resource_uri(uri: str) -> tuple[UUID, str] | None:
    match = CONTEXT_RESOURCE_URI_RE.match(uri)
    if match is None:
        return None
    try:
        return UUID(match.group("workspace_id")), unquote(match.group("slug"))
    except ValueError:
        return None


def context_page_entity_ref(
    *,
    workspace_id: UUID,
    page_id: UUID,
    slug: str,
    version_id: UUID | None = None,
) -> EntityRef:
    return EntityRef(
        kind=EntityKind.CONTEXT_PAGE,
        id=page_id,
        workspace_id=workspace_id,
        version_id=version_id,
        concept_path=context_concept_path(slug),
        resource_uri=context_resource_uri(workspace_id, slug),
    )


def library_item_entity_ref(
    *,
    workspace_id: UUID,
    item_id: UUID,
    version_id: UUID | None = None,
) -> EntityRef:
    return EntityRef(
        kind=EntityKind.LIBRARY_ITEM,
        id=item_id,
        workspace_id=workspace_id,
        version_id=version_id,
        concept_path=library_concept_path(item_id),
    )


def ranked_result_entity_ref(result: RankedResult | dict[str, Any]) -> EntityRef:
    source_ref = _source_ref(result)
    source_kind = str(source_ref.get("kind") or "")
    document_id = ranked_result_document_id(result)
    if isinstance(result, RankedResult):
        result_type = result.type
        result_version_id = result.version_id
        concept_path = result.concept_path
    else:
        result_type = result.get("type")
        result_version_id = parse_uuid_or_none(
            str(result.get("version_id") or "")
        )
        concept_path = result.get("concept_path")
    version_id = (
        parse_uuid_or_none(str(source_ref.get("version_id") or ""))
        or result_version_id
    )
    if source_kind == EntityKind.CONTENT_CHUNK:
        kind = coerce_entity_kind(
            str(source_ref.get("document_kind") or result_type or "")
        )
    elif source_kind in {
        EntityKind.CONTEXT_PAGE,
        EntityKind.LIBRARY_ITEM,
        EntityKind.AGENT_PAGE,
        EntityKind.REVIEW_ITEM,
    }:
        kind = coerce_entity_kind(source_kind)
    else:
        kind = coerce_entity_kind(str(result_type or source_kind or ""))
    return EntityRef(
        kind=kind,
        id=document_id,
        version_id=version_id,
        concept_path=str(concept_path) if concept_path else None,
    )


def ranked_result_document_id(
    result: RankedResult | dict[str, Any],
) -> UUID | None:
    """Return the stable document id behind a ranked result."""

    source_ref = _source_ref(result)
    if source_ref.get("kind") == EntityKind.CONTENT_CHUNK:
        return parse_uuid_or_none(str(source_ref.get("document_id") or ""))
    value = result.id if isinstance(result, RankedResult) else result.get("id")
    if isinstance(value, UUID):
        return value
    return parse_uuid_or_none(str(value or ""))


def _source_ref(result: RankedResult | dict[str, Any]) -> dict[str, Any]:
    if isinstance(result, RankedResult):
        value = result.source_ref
    else:
        value = result.get("source_ref")
    return dict(value) if isinstance(value, dict) else {}

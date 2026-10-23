"""Canonical payload types owned by the public overlay contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

DEFAULT_REST_ROLE = "owner"
ActorProvenance = Literal[
    "browser_session",
    "oauth_client",
    "personal_access_token",
    "mcp_client",
    "internal",
]

# The channels an AI reaches dtxWiki through. A review gate exists to put a
# human in front of what an AI wrote, so it is these -- not actor_kind, which
# reads "human" for every bearer token -- that a gate must key on.
#
# "internal" is deliberately absent: it is our own code (background jobs,
# maintenance rounds, service-to-service calls). Writing the guards as "not a
# browser session" instead of "one of these" swept those in and made the
# maintenance paths require human review.
AI_PROVENANCES: frozenset[str] = frozenset(
    {"oauth_client", "personal_access_token", "mcp_client"}
)


@dataclass(frozen=True)
class ActorContext:
    actor_kind: str
    actor_id: str
    scopes: tuple[str, ...] = ()
    workspace_role: str = DEFAULT_REST_ROLE
    user_id: UUID | None = None
    workspace_id: UUID | None = None
    # Effective role for the current target workspace after REST/MCP scoping.
    role: str | None = None
    token_kind: str | None = None
    token_id: UUID | None = None
    client_id: str | None = None
    # D-01v2: the set of workspaces a PAT or OAuth credential is allowed to
    # reach (``api_token_workspaces``/``oauth_consents`` rows with
    # ``removed_at IS NULL``, read live on every request). ``()`` means "no
    # token-level restriction" ONLY for a session actor, which carries no
    # token claim at all; for a PAT/OAuth actor an empty tuple means the
    # credential grants zero workspaces and every request must fail closed.
    # ``workspace_id``/``role`` are not resolved from this set until a
    # request names a target workspace -- see
    # ``trust_boundaries.actor_with_target_workspace_scope``, the sole choke
    # point that backfills both from live ``WorkspaceMembership``. See also
    # ``backend.policy.trust_boundaries.check_actor_can_access``, which fails
    # an empty-claim PAT/OAuth actor closed rather than falling through to
    # membership.
    token_workspace_ids: tuple[UUID, ...] = ()
    # Authentication-channel provenance; actor_kind retains legacy identity.
    provenance: ActorProvenance = "internal"


SearchResultType = Literal[
    "context_page",
    "context_claim",
    "library_item",
    "library_artifact",
    "feedback",
    "change_set",
    "agent_page",
    "source",
]
SearchMatchReason = Literal[
    "exact_id",
    "exact_slug",
    "exact_title",
    "exact_alias",
    "trigram",
    "keyword",
    "semantic",
    "hybrid",
    "associative",
]


class SearchFilters(BaseModel):
    context_type: list[str] | None = None
    connector_type: list[str] | None = None
    format_hint: list[str] | None = None
    tag: list[str] | None = None
    status: list[str] | None = None
    trust_level: list[str] | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None

    model_config = ConfigDict(extra="forbid")


class RankedResult(BaseModel):
    id: UUID
    type: SearchResultType
    title: str
    snippet: str
    score: float
    citation_available: bool
    stale: bool = Field(default=False, deprecated=True)
    archived: bool = False
    summary: str | None = None
    location: list[str] | None = None
    concept_path: str | None = None
    concept_type: str | None = None
    version_id: UUID | None = None
    heading_path: list[str] | None = None
    section_id: str | None = None
    source_ref: dict[str, str] | None = None
    match_reason: SearchMatchReason | None = None
    matched_name: str | None = None
    entity_ref: dict[str, str] | None = None

    # Hydrators already read the actor-visible body. Retain it privately until
    # the dispatcher paginates, so summary cleanup needs no additional reads.
    _summary_markdown: str | None = PrivateAttr(default=None)
    _location_fallback: tuple[str, UUID] | None = PrivateAttr(default=None)

    model_config = ConfigDict(extra="forbid")


__all__ = [
    "ActorContext",
    "ActorProvenance",
    "DEFAULT_REST_ROLE",
    "RankedResult",
    "SearchFilters",
    "SearchMatchReason",
    "SearchResultType",
]

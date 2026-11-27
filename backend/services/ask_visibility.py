from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.ask import (
    AskTurn,
    AskTurnCitation,
)
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.library import LibraryArtifact, LibraryItem, LibraryItemVersion
from backend.policy.auth import ActorContext
from backend.repositories.workspace_scoped import get_in_workspace
from backend.services.actor_visible_read import (
    actor_can_read_library_trust_level,
    context_page_visible,
    context_version_is_visible,
)
from backend.services.ask_types import AskEvidence
from backend.services.library_canonical import (
    canonical_library_artifact_belongs_to_item,
)
from backend.services.okf import EXCLUDED_LIBRARY_LIFECYCLE_STATUSES

# Library sources checked per query, well under any database's bind limit.
_VISIBILITY_BATCH = 500


def ask_turn_visible(
    session: Session,
    turn: AskTurn,
    citations: tuple[list[UUID], list[UUID]],
    actor: ActorContext | None,
    workspace_id: UUID,
    cache: dict[tuple[str, UUID, UUID | None], bool] | None = None,
) -> bool:
    """Whether ``actor`` may see the answer of ``turn``.

    A turn with ``input_evidence`` is checked against everything its model was
    given, earlier turns' evidence included: an answer that drew on a
    restricted source through the conversation is hidden with it. A malformed
    record is hidden. Older turns are checked against their citations.
    """

    if turn.input_evidence is not None:
        evidence = AskEvidence.from_record(turn.input_evidence)
        return evidence is not None and ask_evidence_visible(
            session,
            context_refs=evidence.context,
            artifact_ids=evidence.library,
            actor=actor,
            workspace_id=workspace_id,
            cache=cache,
        )
    cited_page_ids, cited_artifact_ids = citations
    return ask_evidence_visible(
        session,
        context_refs=[(page_id, None) for page_id in cited_page_ids],
        artifact_ids=cited_artifact_ids,
        actor=actor,
        workspace_id=workspace_id,
        cache=cache,
    )


def ask_evidence_visible(
    session: Session,
    *,
    context_refs: Iterable[tuple[UUID, UUID | None]],
    artifact_ids: Iterable[UUID],
    actor: ActorContext | None,
    workspace_id: UUID,
    cache: dict[tuple[str, UUID, UUID | None], bool] | None = None,
) -> bool:
    """Whether ``actor`` can see every source an Ask answer was built from.

    Used to re-validate an Ask turn's stored answer/citations/trace (and any
    ingress proposal cloned from it) against the CURRENT actor. A DANGLING
    citation (the cited page/artifact no longer exists) fails CLOSED: the
    stored answer text was composed from that evidence and we can no longer
    confirm the current actor was ever allowed to see it, so the turn must
    redact rather than replay. Every cited document must also belong to the
    turn's own ``workspace_id`` — a malformed/migrated citation resolving to a
    visible entity in another workspace must not authorize replay (Codex R12).

    ``cache`` lets one read of a session (one actor) check each source once:
    later turns repeat their history's sources.
    """

    cache = {} if cache is None else cache
    # Read once: the sources are walked twice below.
    library_ids = set(artifact_ids)
    for page_id, version_id in context_refs:
        key = ("context", page_id, version_id)
        if key not in cache:
            cache[key] = _context_ref_visible(
                session, page_id, version_id, actor, workspace_id
            )
        if not cache[key]:
            return False
    unchecked = [
        artifact_id
        for artifact_id in library_ids
        if ("library", artifact_id, None) not in cache
    ]
    for start in range(0, len(unchecked), _VISIBILITY_BATCH):
        batch = unchecked[start : start + _VISIBILITY_BATCH]
        visible = _library_artifacts_visible(session, batch, actor, workspace_id)
        for artifact_id in batch:
            cache[("library", artifact_id, None)] = artifact_id in visible
    return all(cache[("library", artifact_id, None)] for artifact_id in library_ids)


def _context_ref_visible(
    session: Session,
    page_id: UUID,
    version_id: UUID | None,
    actor: ActorContext | None,
    workspace_id: UUID,
) -> bool:
    page = get_in_workspace(
        session,
        ContextPage,
        workspace_id=workspace_id,
        resource_id=page_id,
    )
    if not context_page_visible(page, actor, workspace_id=workspace_id):
        return False
    if version_id is None:
        return True
    # The answer used this version's text. A version later closed (an undone
    # approval) is not readable just because the page still is.
    version = get_in_workspace(
        session,
        ContextPageVersion,
        workspace_id=workspace_id,
        resource_id=version_id,
    )
    return (
        version is not None
        and version.page_id == page_id
        and context_version_is_visible(version, actor)
    )


def _library_artifacts_visible(
    session: Session,
    artifact_ids: list[UUID],
    actor: ActorContext | None,
    workspace_id: UUID,
) -> set[UUID]:
    """The given artifacts ``actor`` may read; a missing one is not in it."""

    if not artifact_ids:
        return set()
    rows = session.execute(
        select(
            LibraryArtifact.id,
            LibraryItem,
        )
        .join(
            LibraryItemVersion,
            LibraryItemVersion.library_item_id == LibraryItem.id,
        )
        .join(
            LibraryArtifact,
            LibraryArtifact.library_item_version_id == LibraryItemVersion.id,
        )
        .where(
            LibraryArtifact.id.in_(artifact_ids),
            LibraryItem.workspace_id == workspace_id,
        )
    ).all()
    # The stored answer paraphrases this evidence. If its source was
    # archived/hidden or the artifact left the canonical ready attempt, the
    # content must not replay — fail closed (Codex round-11).
    return {
        artifact_id
        for artifact_id, item in rows
        if item.lifecycle_status not in EXCLUDED_LIBRARY_LIFECYCLE_STATUSES
        and canonical_library_artifact_belongs_to_item(
            session,
            item=item,
            artifact_id=artifact_id,
        )
        and actor_can_read_library_trust_level(item.trust_level, actor)
    }


def _turn_citations_by_turn(
    session: Session,
    turn_ids: list[UUID],
) -> dict[UUID, tuple[list[UUID], list[UUID]]]:
    """Rank-ordered (context_page_ids, library_artifact_ids) per turn."""

    if not turn_ids:
        return {}
    rows = session.execute(
        select(
            AskTurnCitation.ask_turn_id,
            AskTurnCitation.target_kind,
            AskTurnCitation.target_id,
        )
        .where(AskTurnCitation.ask_turn_id.in_(turn_ids))
        .order_by(AskTurnCitation.ask_turn_id, AskTurnCitation.rank)
    ).all()
    citations: dict[UUID, tuple[list[UUID], list[UUID]]] = {}
    for turn_id, kind, target_id in rows:
        pages, artifacts = citations.setdefault(turn_id, ([], []))
        if kind == "context_page":
            pages.append(target_id)
        elif kind == "library_artifact":
            artifacts.append(target_id)
    return citations

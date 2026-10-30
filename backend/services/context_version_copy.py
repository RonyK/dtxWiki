"""Copy inherited claims and citations when creating a Context version."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend.models.context import ContextCitation, ContextClaim, ContextPageVersion
from backend.repositories.workspace_scoped import select_in_workspace


@dataclass(frozen=True)
class CopiedContextReferences:
    claim_count: int
    citation_count: int
    anchors_preserved: bool


def copy_context_version_references(
    session: Session,
    *,
    source: ContextPageVersion,
    target: ContextPageVersion,
) -> CopiedContextReferences:
    """Preserve evidence identity; uncertain local anchors require review.

    The destination is a fresh version in the same page and Workspace. Source
    citation references and their verification metadata remain unchanged; claim
    targets are remapped to the copied claims. No transaction boundary is owned.
    """
    if source.workspace_id != target.workspace_id or source.page_id != target.page_id:
        raise ValueError("Context references can only be copied within their page")
    claims = list(
        session.scalars(
            select_in_workspace(
                ContextClaim,
                workspace_id=source.workspace_id,
                conditions=(ContextClaim.page_version_id == source.id,),
            )
        )
    )
    if (
        session.scalar(
            select_in_workspace(
                ContextClaim,
                workspace_id=target.workspace_id,
                conditions=(ContextClaim.page_version_id == target.id,),
            )
            .with_only_columns(ContextClaim.id)
            .limit(1)
        )
        is not None
    ):
        raise ValueError("Context reference destination already has claims")
    unchanged = source.body_md == target.body_md
    anchors_preserved = True
    claim_ids = {}
    for claim in claims:
        values = {
            column.name: deepcopy(getattr(claim, column.name))
            for column in ContextClaim.__table__.columns
            if column.name not in {"id", "page_version_id"}
        }
        copied = ContextClaim(page_version_id=target.id, **values)
        session.add(copied)
        session.flush()
        claim_ids[claim.id] = copied.id
        location = claim.location_in_page or {}
        if not unchanged and location and location != {"kind": "whole_page"}:
            anchors_preserved = False
    citations = list(
        session.scalars(
            select_in_workspace(
                ContextCitation,
                workspace_id=source.workspace_id,
                conditions=(
                    or_(
                        (ContextCitation.target_kind == "context_claim")
                        & ContextCitation.target_id.in_(claim_ids),
                        (ContextCitation.target_kind != "context_claim")
                        & (ContextCitation.target_id == source.id),
                    ),
                ),
            )
        )
    )
    for citation in citations:
        values = {
            column.name: deepcopy(getattr(citation, column.name))
            for column in ContextCitation.__table__.columns
            if column.name not in {"id", "target_id"}
        }
        session.add(
            ContextCitation(
                target_id=(
                    claim_ids[citation.target_id]
                    if citation.target_kind == "context_claim"
                    else target.id
                ),
                **values,
            )
        )
        if not unchanged and citation.target_location:
            anchors_preserved = False
    session.flush()
    return CopiedContextReferences(len(claims), len(citations), anchors_preserved)

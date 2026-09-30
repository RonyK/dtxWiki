"""Rejects a lifecycle change that would surface a legacy upload reference.

Every workspace's legacy-media cutover is complete by construction now (P5.3:
the marker/reconciliation machinery this used to gate on is gone -- a
newly-provisioned workspace never had legacy uploads to begin with, and no
workspace predating the marker system exists in this codebase's actual
environment, so the check this module makes is now unconditional).
"""

from __future__ import annotations

from uuid import UUID

from backend.context_media.domain.markdown_media import (
    LegacyMarkdownMediaError,
    scan_legacy_upload_references,
)
from backend.services.read_errors import RejectedByPolicyError


class LegacyMediaCutoverConflictError(RejectedByPolicyError):
    """A page body still references a legacy upload; publish/edit is refused."""

    error_code = "legacy_media_cutover_conflict"

    def __init__(self) -> None:
        super().__init__(
            "legacy media cannot become visible after Workspace cutover",
            error_code=self.error_code,
        )


def require_no_visible_legacy_media(
    *,
    workspace_id: UUID,
    body_markdown: str,
) -> None:
    """Reject a body that still contains a browser-reachable legacy reference."""

    try:
        occurrences = scan_legacy_upload_references(
            body_markdown,
            workspace_id=workspace_id,
        )
    except LegacyMarkdownMediaError as exc:
        # An ambiguous/foreign/raw-HTML legacy candidate must fail closed too;
        # it cannot safely be proved non-rendering.
        raise LegacyMediaCutoverConflictError() from exc
    if occurrences:
        raise LegacyMediaCutoverConflictError()


__all__ = [
    "LegacyMediaCutoverConflictError",
    "require_no_visible_legacy_media",
]

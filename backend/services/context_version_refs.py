"""How a change operation names a version in its ``diff_ref``.

A version write records the bare ``"<kind>_version:<uuid>"`` ref. An inline
comment on a Context page records an anchor JSON that carries the version id.
"""

import json
from typing import Any
from uuid import UUID

from backend.utils.uuid import parse_uuid_or_none

CONTEXT_VERSION_REF_PREFIX = "context_page_version:"
INLINE_COMMENT_ANCHOR_KIND = "page_inline_comment_anchor"


def context_version_ref(version_id: UUID) -> str:
    return f"{CONTEXT_VERSION_REF_PREFIX}{version_id}"


def inline_comment_anchor_ref(version_id: UUID, anchor: Any) -> str:
    return json.dumps(
        {
            "kind": INLINE_COMMENT_ANCHOR_KIND,
            "version_id": str(version_id),
            "anchor": anchor,
        },
        separators=(",", ":"),
    )


def version_id_from_ref(diff_ref: str | None, prefix: str) -> UUID | None:
    """The version a bare ``<prefix><uuid>`` ref names."""
    if not diff_ref or not diff_ref.startswith(prefix):
        return None
    return parse_uuid_or_none(diff_ref[len(prefix) :])


def written_context_version_id(diff_ref: str | None) -> UUID | None:
    """The Context version a write recorded."""
    return version_id_from_ref(diff_ref, CONTEXT_VERSION_REF_PREFIX)


def _inline_comment_record(diff_ref: str | None) -> dict | None:
    if not diff_ref or not diff_ref.startswith("{"):
        return None
    try:
        record = json.loads(diff_ref)
    except (ValueError, RecursionError):
        return None
    if isinstance(record, dict) and record.get("kind") == INLINE_COMMENT_ANCHOR_KIND:
        return record
    return None


def inline_comment_anchor(diff_ref: str | None) -> dict | None:
    """The anchor an inline comment recorded, if ``diff_ref`` is one."""
    record = _inline_comment_record(diff_ref)
    anchor = record.get("anchor") if record else None
    return anchor if isinstance(anchor, dict) else None


def referenced_context_version_id(diff_ref: str | None) -> UUID | None:
    """The Context version a write recorded or an inline comment anchors to."""
    written = written_context_version_id(diff_ref)
    if written is not None:
        return written
    record = _inline_comment_record(diff_ref)
    return parse_uuid_or_none(record.get("version_id")) if record else None

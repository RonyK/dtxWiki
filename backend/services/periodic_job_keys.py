"""Shared periodic job keys and the periods they protect from cleanup."""

import re
from datetime import UTC, datetime, timedelta
from uuid import UUID

MINUTE_JOB_TYPES = frozenset(
    {
        "bundle_expiration_sweep",
        "media_install_reconcile",
        "credential_replica_reconcile",
    }
)
POLICY_KEYED_JOB_TYPE = "context_media_orphan_sweep"
PERIODIC_JOB_TYPES = MINUTE_JOB_TYPES | {
    "lint_tick",
    "trail_tick",
    "librarian_tick",
    POLICY_KEYED_JOB_TYPE,
    "media_blob_gc",
}


def minute_bucket(now: datetime) -> str:
    return now.replace(second=0, microsecond=0).isoformat()


def periodic_job_key(
    job_type: str,
    workspace_id: UUID,
    now: datetime,
    interval_hours: int | None = None,
    policy_revision: str | None = None,
) -> str:
    if interval_hours is None:
        return f"{workspace_id}:{minute_bucket(now)}"
    bucket = int(now.timestamp()) // (interval_hours * 3600)
    key = f"{job_type}:{interval_hours}h:{bucket}"
    if job_type == POLICY_KEYED_JOB_TYPE and policy_revision is not None:
        key += f":{policy_revision}"
    return key


def periodic_key_end(job_type: str, key: str | None) -> datetime | None:
    """Return the encoded period's end; preserve unfamiliar keys."""
    if job_type not in PERIODIC_JOB_TYPES or key is None:
        return None
    try:
        if job_type in MINUTE_JOB_TYPES:
            workspace, separator, timestamp = key.partition(":")
            if not separator:
                return None
            UUID(workspace)
            start = datetime.fromisoformat(timestamp)
            if start.tzinfo is None or start.second or start.microsecond:
                return None
            return start + timedelta(minutes=1)
        suffix = r"(?::[0-9a-f]{64})?" if job_type == POLICY_KEYED_JOB_TYPE else ""
        match = re.fullmatch(
            re.escape(job_type) + r":([1-9][0-9]*)h:([0-9]+)" + suffix,
            key,
        )
        if match is None:
            return None
        hours, bucket = map(int, match.group(1, 2))
        return datetime.fromtimestamp((bucket + 1) * hours * 3600, UTC)
    except (ValueError, OverflowError, OSError):
        return None

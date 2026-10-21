"""Resolve the CONTROL-plane retention policy without touching workspace data."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from backend.models.workspace import Workspace

MAX_ORPHAN_RETENTION_HOURS = 876_000  # 100 years; also bounds date arithmetic.


def validate_retention_hours(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_ORPHAN_RETENTION_HOURS:
        raise ValueError(
            "orphan_retention_hours must be an integer from 0 to "
            f"{MAX_ORPHAN_RETENTION_HOURS}"
        )
    return value


@dataclass(frozen=True)
class ContextMediaPolicy:
    override_hours: int | None
    default_hours: int

    @property
    def effective_hours(self) -> int:
        return (
            self.default_hours if self.override_hours is None else self.override_hours
        )

    @property
    def policy(self) -> dict[str, object]:
        return {
            "orphan_retention_hours": self.override_hours,
            "effective_orphan_retention_hours": self.effective_hours,
            "default_orphan_retention_hours": self.default_hours,
            "source": "server" if self.override_hours is None else "workspace",
            "max_orphan_retention_hours": MAX_ORPHAN_RETENTION_HOURS,
        }

    @property
    def revision(self) -> str:
        payload = json.dumps(
            {"group": "attachments", "policy": self.policy},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def metadata(self) -> dict[str, object]:
        return {**self.policy, "revision": self.revision}


def resolve_context_media_policy(
    workspace: Workspace, default_hours: int
) -> ContextMediaPolicy:
    default = validate_retention_hours(default_hours)
    policy = workspace.context_media_policy
    if not isinstance(policy, dict):
        raise ValueError("Invalid workspace attachment policy")
    override = policy.get("orphan_retention_hours")
    return ContextMediaPolicy(
        None if override is None else validate_retention_hours(override), default
    )


def read_context_media_policy(
    session: Session, workspace_id: UUID, default_hours: int
) -> ContextMediaPolicy:
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        raise ValueError("Workspace attachment policy was not found")
    return resolve_context_media_policy(workspace, default_hours)

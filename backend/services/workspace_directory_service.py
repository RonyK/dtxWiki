from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import and_, collate, or_, select

from backend.models.normalized_keys import normalize_search_key
from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.models.workspace_join import WorkspaceJoinRequest

DIRECTORY_DEFAULT_LIMIT = 20
DIRECTORY_MAX_LIMIT = 50
DIRECTORY_MAX_QUERY_LENGTH = 128
DIRECTORY_DESCRIPTION_MAX_LENGTH = 280


class WorkspaceDirectoryError(RuntimeError):
    error_code = "validation_failed"


class WorkspaceDirectoryRequesterInactiveError(WorkspaceDirectoryError):
    error_code = "workspace_join_request_requester_inactive"


class WorkspaceDirectoryCursorError(WorkspaceDirectoryError):
    error_code = "validation_failed"


@dataclass(frozen=True)
class WorkspaceDirectoryItem:
    workspace_id: UUID
    title: str
    description: str | None
    visibility: str
    join_policy: str
    current_request_status: str | None


@dataclass(frozen=True)
class WorkspaceDirectoryPage:
    items: list[WorkspaceDirectoryItem]
    next_cursor: str | None


@dataclass(frozen=True)
class WorkspaceDirectoryService:
    session_factory: object

    def list_directory(
        self,
        *,
        actor_user_id: UUID,
        query: str | None = None,
        cursor: str | None = None,
        limit: int = DIRECTORY_DEFAULT_LIMIT,
        pending_only: bool = False,
    ) -> WorkspaceDirectoryPage:
        if not 1 <= limit <= DIRECTORY_MAX_LIMIT:
            raise WorkspaceDirectoryError(
                f"limit must be between 1 and {DIRECTORY_MAX_LIMIT}"
            )
        normalized_query = normalize_directory_text(query or "")
        if len(normalized_query) > DIRECTORY_MAX_QUERY_LENGTH:
            raise WorkspaceDirectoryError("directory query is too long")
        after = (
            decode_directory_cursor(cursor, normalized_query, pending_only)
            if cursor is not None
            else None
        )

        with self.session_factory() as session:
            actor = session.get(User, actor_user_id)
            if actor is None or actor.status != "active":
                raise WorkspaceDirectoryRequesterInactiveError(
                    "an active user session is required"
                )
            dialect_name = session.get_bind().dialect.name
            if dialect_name == "sqlite":
                title_key = collate(Workspace.directory_title_key, "BINARY")
            elif dialect_name == "postgresql":
                title_key = collate(Workspace.directory_title_key, "C")
            else:
                raise WorkspaceDirectoryError(
                    f"unsupported directory collation dialect: {dialect_name}"
                )
            member_exists = (
                select(WorkspaceMembership.user_id)
                .where(
                    WorkspaceMembership.user_id == actor_user_id,
                    WorkspaceMembership.workspace_id == Workspace.id,
                )
                .exists()
            )
            join_condition = and_(
                WorkspaceJoinRequest.workspace_id == Workspace.id,
                WorkspaceJoinRequest.requester_user_id == actor_user_id,
            )
            predicates = [
                Workspace.visibility.in_(("team", "public")),
                ~member_exists,
            ]
            if normalized_query:
                predicates.append(
                    title_key.contains(normalized_query, autoescape=True)
                )
            if pending_only:
                predicates.append(WorkspaceJoinRequest.status == "pending")
            if after is not None:
                title_after, workspace_after = after
                predicates.append(
                    or_(
                        title_key > title_after,
                        and_(
                            title_key == title_after,
                            Workspace.id > UUID(hex=workspace_after),
                        ),
                    )
                )
            page_rows = list(
                session.execute(
                    select(
                        Workspace,
                        WorkspaceJoinRequest.status,
                        title_key.label("title_key"),
                    )
                    .outerjoin(WorkspaceJoinRequest, join_condition)
                    .where(*predicates)
                    .order_by(title_key, Workspace.id)
                    .limit(limit + 1)
                ).all()
            )
            selected = page_rows[:limit]
            items = [
                WorkspaceDirectoryItem(
                    workspace_id=workspace.id,
                    title=workspace.title,
                    description=short_directory_description(workspace.description),
                    visibility=workspace.visibility,
                    join_policy=workspace.join_policy,
                    current_request_status=request_status,
                )
                for workspace, request_status, _ in selected
            ]
            next_cursor = None
            if len(page_rows) > limit and selected:
                next_cursor = encode_directory_cursor(
                    normalized_query,
                    (selected[-1][2], selected[-1][0].id.hex),
                    pending_only=pending_only,
                )
            return WorkspaceDirectoryPage(items=items, next_cursor=next_cursor)


def normalize_directory_text(value: str) -> str:
    return normalize_search_key(value)


def short_directory_description(value: str | None) -> str | None:
    if value is None:
        return None
    collapsed = " ".join(value.split())
    return collapsed[:DIRECTORY_DESCRIPTION_MAX_LENGTH] or None


def encode_directory_cursor(
    query: str,
    sort_key: tuple[str, str],
    *,
    pending_only: bool = False,
) -> str:
    payload = {
        "v": 1,
        "q": hashlib.sha256(
            f"{query}\0{int(pending_only)}".encode()
        ).hexdigest(),
        "t": sort_key[0],
        "id": sort_key[1],
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_directory_cursor(
    cursor: str,
    query: str,
    pending_only: bool = False,
) -> tuple[str, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding))
        if set(payload) != {"v", "q", "t", "id"} or payload["v"] != 1:
            raise ValueError("unsupported directory cursor")
        expected_query = hashlib.sha256(
            f"{query}\0{int(pending_only)}".encode()
        ).hexdigest()
        if payload["q"] != expected_query:
            raise ValueError("directory cursor does not match query")
        title_key = payload["t"]
        workspace_hex = payload["id"]
        if not isinstance(title_key, str) or not isinstance(workspace_hex, str):
            raise ValueError("invalid directory cursor values")
        if UUID(hex=workspace_hex).hex != workspace_hex:
            raise ValueError("invalid directory cursor workspace")
        return title_key, workspace_hex
    except (
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        binascii.Error,
    ) as exc:
        raise WorkspaceDirectoryCursorError(
            "invalid workspace directory cursor"
        ) from exc

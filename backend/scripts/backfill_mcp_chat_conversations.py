"""Conservatively group legacy MCP chat fragments (dry-run by default)."""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.db import make_session_factory_from_settings
from backend.app.settings import get_settings
from backend.models.mcp_chat import McpChatArtifactLink, McpChatSession
from backend.models.workspace import Workspace, default_actor_id
from backend.services.audit_service import AuditService

MAX_FRAGMENT_GAP_SECONDS = 60
HEURISTIC = (
    "same workspace, client, non-null owner, normalized automatic title, "
    "exact non-empty artifact set, archive state, and at most 60 seconds "
    "between adjacent sessions"
)


@dataclass(frozen=True)
class ConversationBackfillGroup:
    workspace_id: UUID
    workspace_slug: str
    conversation_group_id: UUID
    session_ids: tuple[UUID, ...]
    mcp_session_ids: tuple[str, ...]
    gaps_seconds: tuple[int, ...]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _normalized_title(chat: McpChatSession) -> str:
    return " ".join((chat.title or "").split()).casefold()


def _artifact_sets(
    session: Session, session_ids: list[UUID]
) -> dict[UUID, frozenset[tuple[str, UUID]]]:
    result: dict[UUID, set[tuple[str, UUID]]] = defaultdict(set)
    if not session_ids:
        return {}
    rows = session.execute(
        select(
            McpChatArtifactLink.chat_session_id,
            McpChatArtifactLink.resource_kind,
            McpChatArtifactLink.resource_id,
        ).where(McpChatArtifactLink.chat_session_id.in_(session_ids))
    )
    for chat_session_id, resource_kind, resource_id in rows:
        result[chat_session_id].add((resource_kind, resource_id))
    return {key: frozenset(value) for key, value in result.items()}


def _fragment_gap_seconds(
    previous: McpChatSession, current: McpChatSession
) -> int | None:
    delta = _as_utc(current.started_at) - _as_utc(previous.last_activity_at)
    seconds = int(delta.total_seconds())
    if seconds < 0 or seconds > MAX_FRAGMENT_GAP_SECONDS:
        return None
    return seconds


def _can_join(
    previous: McpChatSession,
    current: McpChatSession,
    artifacts: dict[UUID, frozenset[tuple[str, UUID]]],
) -> int | None:
    if previous.title_user_edited or current.title_user_edited:
        return None
    title = _normalized_title(previous)
    if not title or title != _normalized_title(current):
        return None
    previous_artifacts = artifacts.get(previous.id, frozenset())
    if not previous_artifacts or previous_artifacts != artifacts.get(
        current.id, frozenset()
    ):
        return None
    if (previous.archived_at is None) != (current.archived_at is None):
        return None
    return _fragment_gap_seconds(previous, current)


def _build_group(
    cluster: list[McpChatSession],
    gaps: list[int],
    workspace_slugs: dict[UUID, str],
) -> ConversationBackfillGroup | None:
    if len(cluster) < 2:
        return None
    first = cluster[0]
    group_id = uuid5(
        NAMESPACE_URL,
        f"dtxwiki:mcp-chat-backfill:{first.workspace_id}:{first.id}",
    )
    return ConversationBackfillGroup(
        workspace_id=first.workspace_id,
        workspace_slug=workspace_slugs.get(first.workspace_id, "<unknown>"),
        conversation_group_id=group_id,
        session_ids=tuple(chat.id for chat in cluster),
        mcp_session_ids=tuple(chat.mcp_session_id for chat in cluster),
        gaps_seconds=tuple(gaps),
    )


def find_conversation_groups(
    session: Session, *, workspace_id: UUID | None = None
) -> list[ConversationBackfillGroup]:
    statement = select(McpChatSession).where(
        McpChatSession.conversation_group_id.is_(None),
        McpChatSession.owner_user_id.is_not(None),
    )
    if workspace_id is not None:
        statement = statement.where(McpChatSession.workspace_id == workspace_id)
    chats = list(session.scalars(statement).all())
    artifacts = _artifact_sets(session, [chat.id for chat in chats])
    workspace_slugs = dict(
        session.execute(select(Workspace.id, Workspace.slug)).all()
    )
    partitions: dict[tuple[UUID, UUID, UUID], list[McpChatSession]] = defaultdict(list)
    for chat in chats:
        assert chat.owner_user_id is not None
        partitions[(chat.workspace_id, chat.client_id, chat.owner_user_id)].append(chat)

    groups: list[ConversationBackfillGroup] = []
    for partition in partitions.values():
        partition.sort(key=lambda chat: (_as_utc(chat.started_at), str(chat.id)))
        cluster: list[McpChatSession] = []
        gaps: list[int] = []

        for chat in partition:
            if not cluster:
                cluster = [chat]
                gaps = []
                continue
            gap = _can_join(cluster[-1], chat, artifacts)
            if gap is None:
                if group := _build_group(cluster, gaps, workspace_slugs):
                    groups.append(group)
                cluster = [chat]
                gaps = []
                continue
            cluster.append(chat)
            gaps.append(gap)
        if group := _build_group(cluster, gaps, workspace_slugs):
            groups.append(group)
    return sorted(
        groups,
        key=lambda group: (group.workspace_slug, str(group.session_ids[0])),
    )


def backfill_conversation_groups(
    session: Session, *, workspace_id: UUID | None = None, apply: bool = False
) -> list[ConversationBackfillGroup]:
    groups = find_conversation_groups(session, workspace_id=workspace_id)
    if not apply:
        return groups
    audit = AuditService()
    applied: list[ConversationBackfillGroup] = []
    for group in groups:
        rows = list(
            session.scalars(
                select(McpChatSession)
                .where(McpChatSession.id.in_(group.session_ids))
                .with_for_update()
            ).all()
        )
        if len(rows) != len(group.session_ids) or any(
            row.conversation_group_id is not None for row in rows
        ):
            continue
        # Discovery and application are separate reads. Re-run the complete
        # adjacency heuristic after locking the candidate rows so a title,
        # artifact, archive, timestamp, or intervening-session change cannot
        # turn a stale report into an incorrect merge.
        fresh = next(
            (
                candidate
                for candidate in find_conversation_groups(
                    session, workspace_id=group.workspace_id
                )
                if candidate.session_ids == group.session_ids
                and candidate.conversation_group_id == group.conversation_group_id
            ),
            None,
        )
        if fresh is None:
            continue
        for row in rows:
            row.conversation_group_id = group.conversation_group_id
            session.flush()
            audit.emit(
                session=session,
                workspace_id=group.workspace_id,
                actor_kind="system",
                actor_id=str(default_actor_id()),
                action="mcp_chat.conversation_group_backfilled",
                target_kind="mcp_chat_session",
                target_id=row.id,
                payload={
                    "conversation_group_id": str(group.conversation_group_id),
                    "session_ids": [str(value) for value in group.session_ids],
                    "heuristic": HEURISTIC,
                },
            )
        applied.append(group)
    return applied


def _resolve_workspace(session: Session, value: str | None) -> UUID | None:
    if value is None:
        return None
    workspace = session.scalar(select(Workspace).where(Workspace.slug == value))
    if workspace is None:
        try:
            workspace = session.get(Workspace, UUID(value))
        except ValueError:
            workspace = None
    if workspace is None:
        raise ValueError(f"workspace not found: {value}")
    return workspace.id


def _print_report(
    groups: list[ConversationBackfillGroup], *, apply: bool
) -> None:
    session_count = sum(len(group.session_ids) for group in groups)
    print(
        f"{'APPLY' if apply else 'DRY-RUN'}: {len(groups)} conservative "
        f"conversation group(s), {session_count} session change(s)"
    )
    print(f"heuristic: {HEURISTIC}")
    for group in groups:
        print(
            f"workspace {group.workspace_slug} ({group.workspace_id}) "
            f"group={group.conversation_group_id}"
        )
        for index, (session_id, mcp_session_id) in enumerate(
            zip(group.session_ids, group.mcp_session_ids, strict=True)
        ):
            gap = "first" if index == 0 else f"gap={group.gaps_seconds[index - 1]}s"
            print(
                f"  session {session_id} mcp_session_id={mcp_session_id} {gap}"
            )
    if not apply:
        print("No changes made. Re-run with --apply to assign and audit these groups.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill conservative conversation groups for legacy MCP chats."
    )
    parser.add_argument("--workspace", help="workspace slug or UUID (default: all)")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="assign groups and emit one audit per session",
    )
    args = parser.parse_args(argv)
    factory = make_session_factory_from_settings(get_settings())
    with factory() as session:
        try:
            workspace_id = _resolve_workspace(session, args.workspace)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        groups = backfill_conversation_groups(
            session, workspace_id=workspace_id, apply=args.apply
        )
        _print_report(groups, apply=args.apply)
        session.commit() if args.apply else session.rollback()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

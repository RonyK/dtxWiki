from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from backend.domain.input_policy import fit_generated_title
from backend.models.ask import (
    AskSession,
    AskTurn,
    AskTurnCitation,
)
from backend.models.context import ContextPage
from backend.models.tree import TreePolicy, WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.repositories.workspace_scoped import get_in_workspace
from backend.schemas.ask import (
    AskSessionCreate,
    AskSessionDetail,
    AskSessionListData,
)
from backend.schemas.input_policy import validate_request_inputs
from backend.services.ask_reader import AskTurnReader
from backend.services.ask_types import ASK_TITLE_MAX_WORDS
from backend.services.audit_service import AuditService
from backend.services.list_pagination import (
    _decode_keyset_cursor,
    _encode_keyset_cursor,
)
from backend.services.read_errors import ReadNotFoundError
from backend.services.tree_service import (
    create_tree_node,
    resolve_tree_parent,
)
from backend.utils.uuid import parse_uuid_or_none


def derive_title_snippet(message: str, max_words: int = ASK_TITLE_MAX_WORDS) -> str:
    cleaned = re.sub(r"\s+", " ", message).strip(" \t\r\n#*_`>-")
    if not cleaned:
        return ""
    words = cleaned.split(" ")
    snippet = " ".join(words[:max_words]).strip(" .,:;!?")
    if len(words) > max_words:
        snippet = f"{snippet}..."
    return fit_generated_title(snippet, max_length=96) if snippet else ""


class AskSessionService:
    def __init__(self, audit_service: AuditService, reader: AskTurnReader) -> None:
        self.audit_service = audit_service
        self._reader = reader

    def list_sessions(
        self,
        session: Session,
        workspace_id: UUID,
        limit: int = 50,
        cursor: str | None = None,
    ) -> AskSessionListData:
        base = select(AskSession).where(AskSession.workspace_id == workspace_id)
        total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
        # Keyset requires the ORDER BY columns to match the cursor WHERE columns.
        # The cursor compares (updated_at, id), so the ordering must be exactly
        # (updated_at desc, id desc) — id (a UUID) is already a unique tiebreak,
        # so a started_at term here would only break the cursor (skip/duplicate
        # rows when updated_at ties and started_at order != id order).
        stmt = base.order_by(
            AskSession.updated_at.desc(),
            AskSession.id.desc(),
        )
        decoded = _decode_keyset_cursor(cursor)
        if decoded is not None:
            cur_ts, cur_id = decoded
            stmt = stmt.where(
                tuple_(AskSession.updated_at, AskSession.id) < (cur_ts, cur_id)
            )
        sessions = session.scalars(stmt.limit(limit + 1)).all()
        page, has_more = sessions[:limit], len(sessions) > limit
        next_cursor = (
            _encode_keyset_cursor(page[-1].updated_at, page[-1].id)
            if has_more and page
            else None
        )
        return AskSessionListData(
            items=[
                self._reader.session_ref(session, ask_session) for ask_session in page
            ],
            date_buckets=date_buckets(page),
            next_cursor=next_cursor,
            total=total,
            has_more=has_more,
        )

    def create_session(
        self,
        session: Session,
        workspace_id: UUID,
        request: AskSessionCreate,
        actor_kind: str,
        actor_id: str,
        actor: ActorContext | None = None,
    ) -> AskSessionDetail:
        validate_request_inputs(request)
        now = datetime.now(UTC)
        day_node = self._ensure_ask_day_node(
            session=session,
            workspace_id=workspace_id,
            at=now,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        default_context_parent = resolve_tree_parent(
            session=session,
            workspace_id=workspace_id,
            section="context",
            parent_id=request.default_context_parent_tree_node_id,
            default_parent_slug="inbox",
        )
        ask_session = AskSession(
            workspace_id=workspace_id,
            title=(
                request.title.strip()
                if request.title and request.title.strip()
                else _default_ask_title(now)
            ),
            title_user_edited=bool(request.title and request.title.strip()),
            actor_kind=actor_kind,
            actor_id=parse_uuid_or_none(actor_id),
            default_context_parent_tree_node_id=(
                default_context_parent.id if default_context_parent else None
            ),
            default_topic_tags=request.default_topic_tags,
            placement_tree_node_id=day_node.id,
        )
        session.add(ask_session)
        session.flush()
        session_node = create_tree_node(
            session,
            workspace_id=workspace_id,
            section="ask",
            node_kind="ask_session_ref",
            parent_id=day_node.id,
            title=ask_session.title,
            target_kind="ask_session",
            target_id=ask_session.id,
            primary_placement=True,
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        ask_session.placement_tree_node_id = session_node.id
        self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="ask.session.create",
            target_kind="ask_session",
            target_id=ask_session.id,
            event_summary=f"Ask session created: {ask_session.title}",
        )
        return self._reader.session_detail(session, ask_session, actor=actor)

    def _ensure_ask_day_node(
        self,
        session: Session,
        workspace_id: UUID,
        at: datetime,
        actor_kind: str,
        actor_id: str,
    ) -> WorkspaceTreeNode:
        root = ensure_ask_root(
            session,
            workspace_id,
            actor_id=actor_id,
        )
        year = ensure_folder(
            session=session,
            workspace_id=workspace_id,
            parent=root,
            slug=f"ask-{at:%Y}",
            title=f"{at:%Y}",
            sort_key=f"{at:%Y}",
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        month = ensure_folder(
            session=session,
            workspace_id=workspace_id,
            parent=year,
            slug=f"ask-{at:%Y}-{at:%m}",
            title=f"{at:%m}",
            sort_key=f"{at:%m}",
            actor_kind=actor_kind,
            actor_id=actor_id,
        )
        return ensure_folder(
            session=session,
            workspace_id=workspace_id,
            parent=month,
            slug=f"ask-{at:%Y}-{at:%m}-{at:%d}",
            title=f"{at:%d}",
            sort_key=f"{at:%d}",
            actor_kind=actor_kind,
            actor_id=actor_id,
        )

    def refresh_title(
        self,
        session: Session,
        ask_session: AskSession,
    ) -> None:
        if ask_session.title_user_edited:
            return
        first_message = session.scalar(
            select(AskTurn.user_message)
            .where(AskTurn.ask_session_id == ask_session.id)
            .order_by(AskTurn.turn_no.asc())
            .limit(1)
        )
        if not first_message:
            return
        intent = derive_title_snippet(first_message)
        if not intent:
            return
        anchor = _dominant_context_anchor(session, ask_session)
        if anchor:
            anchor = fit_generated_title(anchor, max_length=256 - 2 - len(intent))
        next_title = fit_generated_title(f"{anchor}: {intent}" if anchor else intent)
        if next_title == ask_session.title:
            return
        ask_session.title = next_title
        _sync_ask_tree_node_title(session, ask_session)


def ensure_ask_root(
    session: Session,
    workspace_id: UUID,
    *,
    actor_id: str,
) -> WorkspaceTreeNode:
    workspace = session.scalar(
        select(Workspace).where(Workspace.id == workspace_id).with_for_update()
    )
    if workspace is None:
        raise ReadNotFoundError("workspace not found")

    root = session.scalar(
        select(WorkspaceTreeNode).where(
            WorkspaceTreeNode.workspace_id == workspace_id,
            WorkspaceTreeNode.section == "ask",
            WorkspaceTreeNode.slug == "ask",
            WorkspaceTreeNode.parent_id.is_(None),
        )
    )
    if root is None:
        root = WorkspaceTreeNode(
            workspace_id=workspace_id,
            section="ask",
            node_kind="folder",
            parent_id=None,
            sort_key="0004",
            title="Ask",
            slug="ask",
            is_system_node=True,
            can_have_children=True,
            primary_placement=True,
            visibility_status="visible",
            created_by_actor_type="system",
            created_by_actor_id=parse_uuid_or_none(actor_id),
        )
        session.add(root)
        session.flush([root])

    policy = session.get(TreePolicy, (workspace_id, "ask"))
    if policy is None:
        policy = TreePolicy(
            workspace_id=workspace_id,
            section="ask",
            allow_user_folder_create=False,
            allow_page_as_parent=False,
            fixed_root_node_ids=[str(root.id)],
        )
        session.add(policy)
    else:
        policy.fixed_root_node_ids = [str(root.id)]
    return root


def ensure_folder(
    session: Session,
    workspace_id: UUID,
    parent: WorkspaceTreeNode,
    slug: str,
    title: str,
    sort_key: str,
    actor_kind: str,
    actor_id: str,
) -> WorkspaceTreeNode:
    node = session.scalar(
        select(WorkspaceTreeNode).where(
            WorkspaceTreeNode.workspace_id == workspace_id,
            WorkspaceTreeNode.parent_id == parent.id,
            WorkspaceTreeNode.slug == slug,
            WorkspaceTreeNode.visibility_status == "visible",
        )
    )
    if node is not None:
        return node
    return create_tree_node(
        session,
        workspace_id=workspace_id,
        section="ask",
        node_kind="folder",
        parent_id=parent.id,
        title=title,
        slug=slug,
        sort_key=sort_key,
        actor_kind=actor_kind,
        actor_id=actor_id,
    )


def _default_ask_title(at: datetime) -> str:
    # Include seconds (L3): two sessions created in the same minute would
    # otherwise share an identical "New Ask YYYY-MM-DD HH:MM" title until the
    # first turn refreshes it to a context-derived label.
    return f"New Ask {at:%Y-%m-%d %H:%M:%S}"


def _dominant_context_anchor(
    session: Session,
    ask_session: AskSession,
    min_turns: int = 2,
) -> str | None:
    rows = session.execute(
        select(AskTurnCitation.target_id, AskTurn.turn_no)
        .join(AskTurn, AskTurnCitation.ask_turn_id == AskTurn.id)
        .where(AskTurn.ask_session_id == ask_session.id)
        .where(AskTurnCitation.target_kind == "context_page")
        .order_by(AskTurn.turn_no.asc())
    ).all()
    cited_per_turn: Counter[UUID] = Counter()
    first_seen: dict[UUID, int] = {}
    seen_pairs: set[tuple[UUID, int]] = set()
    for page_id, turn_no in rows:
        if (page_id, turn_no) in seen_pairs:
            continue
        seen_pairs.add((page_id, turn_no))
        cited_per_turn[page_id] += 1
        first_seen.setdefault(page_id, turn_no)
    eligible = [
        (page_id, count)
        for page_id, count in cited_per_turn.items()
        if count >= min_turns
    ]
    if not eligible:
        return _dominant_topic_tag(ask_session.default_topic_tags)
    dominant_id, _ = sorted(
        eligible,
        key=lambda item: (-item[1], first_seen.get(item[0], 0), str(item[0])),
    )[0]
    page = get_in_workspace(
        session,
        ContextPage,
        workspace_id=ask_session.workspace_id,
        resource_id=dominant_id,
    )
    if page is None:
        return _dominant_topic_tag(ask_session.default_topic_tags)
    # H-01: the session title is returned to every workspace member by
    # list_sessions, so it must not embed a draft-only page's title (which a
    # viewer cannot read). Only a published page's title is safe as the anchor.
    if page.current_published_version_id is None:
        return _dominant_topic_tag(ask_session.default_topic_tags)
    return page.title


def _dominant_topic_tag(topic_tags: list[str]) -> str | None:
    for tag in topic_tags:
        normalized = tag.strip()
        if normalized:
            return normalized
    return None


def _sync_ask_tree_node_title(session: Session, ask_session: AskSession) -> None:
    node = get_in_workspace(
        session,
        WorkspaceTreeNode,
        workspace_id=ask_session.workspace_id,
        resource_id=ask_session.placement_tree_node_id,
    )
    if node is None:
        return
    node.title = ask_session.title


def date_buckets(sessions: list[AskSession]) -> list[dict[str, object]]:
    counts: dict[str, int] = {}
    for ask_session in sessions:
        key = ask_session.updated_at.date().isoformat()
        counts[key] = counts.get(key, 0) + 1
    return [
        {"date": date_value, "session_count": count}
        for date_value, count in sorted(counts.items(), reverse=True)
    ]

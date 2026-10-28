import base64
import binascii
import json
from datetime import UTC, date, datetime, time, timedelta
from typing import get_args
from uuid import UUID

from sqlalchemy import and_, desc, func, not_, or_, select
from sqlalchemy.orm import Session

from backend.models.agent import AgentPage
from backend.models.audit import AuditEvent
from backend.models.change import ChangeOperation
from backend.models.context import ContextPage
from backend.models.job import Job
from backend.models.library import LibraryItem
from backend.models.mcp_trace import MCPToolCall
from backend.models.review import ReviewItem
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import MCPClient
from backend.policy.auth import ActorContext
from backend.repositories.workspace_scoped import get_in_workspace
from backend.schemas.activity import (
    ActivityActor,
    ActivityEventKey,
    ActivityItemRead,
    ActivityListData,
    ActivityTarget,
)
from backend.services.activity_projection import ContextActivityProjection
from backend.services.actor_visible_read import (
    agent_page_visible,
    context_page_visible,
    library_item_visible,
)
from backend.services.job_queue import HIDDEN_JOB_TYPES
from backend.services.job_read_service import public_job_failure_code
from backend.services.read_errors import ReadValidationError
from backend.services.review_service import ReviewService
from backend.services.tree_read import TreeReadService
from backend.utils.actor_display import (
    DeferredIdentityLabels,
    resolve_actor_display_names,
    resolve_mcp_client_actor_details,
    workspace_plane_actor_display_names,
)


class PageActivityScope:
    """Everything that counts as one Context page's own history.

    A page is written about under two different targets: publishing and
    reviewing name the ContextPage, while renaming and moving name the tree
    node that points at it. Scoping to the page id alone loses half the story,
    which is why the reader's Activity tab had nothing to show.
    """

    def __init__(self, page_id: UUID, tree_node_ids: tuple[UUID, ...]) -> None:
        self.page_id = page_id
        self.tree_node_ids = tree_node_ids

    @classmethod
    def for_page(
        cls, session: Session, workspace_id: UUID, page_id: UUID
    ) -> "PageActivityScope":
        node_ids = tuple(
            session.scalars(
                select(WorkspaceTreeNode.id).where(
                    WorkspaceTreeNode.workspace_id == workspace_id,
                    WorkspaceTreeNode.target_kind == "context_page",
                    WorkspaceTreeNode.target_id == page_id,
                )
            ).all()
        )
        return cls(page_id, node_ids)

    def audit_clause(self):
        page_clause = and_(
            AuditEvent.target_kind == "context_page",
            AuditEvent.target_id == self.page_id,
        )
        return or_(
            page_clause,
            and_(
                AuditEvent.target_kind == "workspace_tree_node",
                AuditEvent.target_id.in_(self.tree_node_ids),
            ),
            and_(
                AuditEvent.target_kind == "review_item",
                select(ReviewItem.id)
                .where(
                    ReviewItem.id == AuditEvent.target_id,
                    ReviewItem.workspace_id == AuditEvent.workspace_id,
                    select(ChangeOperation.id)
                    .where(
                        ChangeOperation.workspace_id == ReviewItem.workspace_id,
                        ChangeOperation.change_set_id == ReviewItem.change_set_id,
                        ChangeOperation.target_type == "context_page",
                        ChangeOperation.target_id == self.page_id,
                    )
                    .exists(),
                )
                .exists(),
            ),
        )


class ActivityService:
    def __init__(self, review_service: ReviewService) -> None:
        self.review_service = review_service

    def list_activity(
        self,
        session: Session,
        workspace_id: UUID,
        period: str = "today",
        lane: str | None = None,
        sub_tab: str | None = None,
        kind: str = "all",
        limit: int = 50,
        cursor: str | None = None,
        audience_for: UUID | None = None,
        actor_role: str | None = None,
        actor: ActorContext | None = None,
        page_id: UUID | None = None,
        deferred_identity: DeferredIdentityLabels | None = None,
    ) -> ActivityListData:
        """One workspace's activity feed, read from ``session``.

        Every row this reads is workspace-plane except one thing: the display
        name behind a human actor id, which lives in control-plane ``users``
        (``backend/tenancy_manifest.py``). ``deferred_identity`` is how a
        caller says "I will resolve those on the identity session myself":
        pass a :class:`DeferredIdentityLabels`, and the human labels come back
        as ``"Human editor"`` placeholders for the caller to fill in with
        ``deferred_identity.apply(identity_session)`` AFTER this session is
        closed. ``backend/api/read.py``'s two Activity routes do exactly that.

        Omitting it keeps the older single-session shape, which resolves
        ``users`` on ``session``. That is correct only while the control and
        workspace factories return the same sessionmaker -- the topology this
        release ships (see tests/static/test_workspace_plane_boundary.py). On a
        physically separate workspace shard the same call reads a table that is
        not there: ``OperationalError: no such table: users`` if the shard is
        manifest-pure, or a silent feed of "Human editor" if someone mirrored
        an empty ``users`` into it.
        """

        # A page-scoped feed is the page's own history, so it draws only from
        # the audit ledger: pending reviews, jobs and MCP traces are workspace
        # concerns and are not what "this page's activity" means.
        page_scope = (
            PageActivityScope.for_page(session, workspace_id, page_id)
            if page_id is not None
            else None
        )
        since, until = period_bounds(period)
        if page_scope is not None:
            return self._page_activity(
                session,
                workspace_id,
                page_scope,
                period,
                since,
                until,
                limit,
                cursor,
                actor,
                deferred_identity,
            )
        # The dedicated lane pages paginate by recency (cursor / "Load more"); the
        # overview and Dashboard keep the review-reserved budget and never page.
        # For a lane page, over-fetch enough from EACH source to fill the
        # requested window plus one row to detect a further page (the global
        # newest offset+limit+1 rows are guaranteed present after the merge, so
        # the slice and has-more flag are exact).
        paginated = lane is not None
        offset = _decode_activity_cursor(cursor) if paginated else 0
        fetch_limit = (offset + limit + 1) if paginated else limit
        pending_items = self._pending_reviews(
            session=session,
            workspace_id=workspace_id,
            since=since,
            until=until,
            limit=fetch_limit,
            audience_for=audience_for,
            actor_role=actor_role,
        )
        job_items = self._jobs(
            session=session,
            workspace_id=workspace_id,
            since=since,
            until=until,
            limit=fetch_limit,
        )
        mcp_items = self._mcp_traces(
            session=session,
            workspace_id=workspace_id,
            since=since,
            until=until,
            limit=fetch_limit,
            deferred_identity=deferred_identity,
        )
        history_items = self._audit_history(
            session=session,
            workspace_id=workspace_id,
            since=since,
            until=until,
            kind=kind,
            limit=fetch_limit,
            actor=actor,
            page_scope=page_scope,
            deferred_identity=deferred_identity,
        )
        if page_scope is not None:
            pending_items = []
            job_items = []
            mcp_items = []
        items = pending_items + job_items + mcp_items + history_items
        if sub_tab == "proposals":
            items = [item for item in items if item.lane == "pending"]
        elif sub_tab == "live":
            items = job_items
        elif sub_tab == "traces":
            items = mcp_items
        elif sub_tab == "history":
            items = history_items
        if lane is not None:
            items = [item for item in items if item.lane == lane]
        # The kind split (workspace vs system) MUST run BEFORE the capped budget
        # below: the workspace-scoped feed reserves the limit for workspace rows,
        # so a flood of newer sign-in/token audit events can't crowd a workspace
        # failed job out of the window (the client used to filter system rows
        # AFTER the cap, which dropped older workspace rows). "all" keeps both.
        if kind == "workspace":
            items = [item for item in items if not _is_system_activity_item(item)]
        elif kind == "system":
            items = [item for item in items if _is_system_activity_item(item)]
        elif kind == "review_decision":
            items = [item for item in items if _is_review_decision_activity_item(item)]
        unread_count = len(pending_items) + len(
            [item for item in job_items if item.lane == "pending"]
        )
        if paginated:
            # Dedicated lane page: pure recency, offset/limit window + a cursor
            # for "Load more". No reservation — a full lane list shows every item
            # across pages, so nothing needs pinning into a capped window.
            items = sorted(items, key=lambda item: item.created_at, reverse=True)
            next_offset = offset + limit
            has_more = len(items) > next_offset and next_offset <= _MAX_ACTIVITY_OFFSET
            items = items[offset : offset + limit]
            next_cursor = _encode_activity_cursor(next_offset) if has_more else None
            return ActivityListData(
                items=items, unread_count=unread_count, next_cursor=next_cursor
            )
        # Overview / Dashboard: budget capped at `limit`, ONLY review decisions
        # get reserved priority — the human decision queue must never be hidden by
        # the limit. The pending lane also holds failed jobs (review_item_id None,
        # via lane_for_job), but failed jobs have NO such priority: they compete
        # with the non-pending history/mcp rows by recency for the remaining
        # slots. So a flood of OLDER failed jobs can't drop newer queued/running
        # jobs (sub_tab=live) or newer history (the Dashboard recent feed) — only
        # the review decisions are reserved, everything else is recency.
        review_pending = sorted(
            [
                item
                for item in items
                if item.lane == "pending" and item.review_item_id is not None
            ],
            key=lambda item: item.created_at,
            reverse=True,
        )
        others = sorted(
            [
                item
                for item in items
                if not (item.lane == "pending" and item.review_item_id is not None)
            ],
            key=lambda item: item.created_at,
            reverse=True,
        )
        reserved = review_pending[:limit]
        others = others[: max(0, limit - len(reserved))]
        # Present the survivors in recency order — job-only views (sub_tab=live)
        # and flat recent feeds (Dashboard) stay newest-first. Pinning the review
        # decision to the TOP of the pending-lane PREVIEW is a presentation
        # concern handled client-side (ActivityScreen), not by reordering this
        # shared feed.
        items = sorted(
            reserved + others,
            key=lambda item: item.created_at,
            reverse=True,
        )
        return ActivityListData(
            items=items,
            unread_count=unread_count,
            next_cursor=None,
        )

    def _page_activity(
        self,
        session,
        workspace_id,
        scope,
        period,
        since,
        until,
        limit,
        cursor,
        actor,
        deferred_identity,
    ) -> ActivityListData:
        """One audit stream with a scope-bound, stable recency cursor."""
        upper = datetime.now(UTC)
        before = None
        skip = 0
        if cursor:
            if len(cursor) > 2048:
                raise ReadValidationError("invalid page activity cursor")
            if cursor.startswith("page-activity-v2:"):
                try:
                    value = json.loads(
                        base64.urlsafe_b64decode(cursor.split(":", 1)[1])
                    )
                    if value["scope"] != [
                        str(workspace_id),
                        str(scope.page_id),
                        period,
                    ]:
                        raise ValueError("cursor scope")
                    upper = datetime.fromisoformat(value["upper"])
                    boundary = datetime.fromisoformat(value["time"])
                    if (
                        upper.tzinfo is None
                        or boundary.tzinfo is None
                        or boundary > upper
                    ):
                        raise ValueError("cursor time")
                    before = (boundary, UUID(value["id"]))
                except (ValueError, KeyError, TypeError, binascii.Error) as exc:
                    raise ReadValidationError("invalid page activity cursor") from exc
            else:
                # Existing clients may finish an old offset page once, then
                # receive the new cursor without changing workspace lanes.
                skip = _decode_activity_cursor(cursor)
        visible = []
        while len(visible) <= limit:
            batch = self._audit_history(
                session=session,
                workspace_id=workspace_id,
                since=since,
                until=until,
                kind="all",
                limit=limit + 1,
                actor=actor,
                page_scope=scope,
                deferred_identity=deferred_identity,
                before=before,
                upper=upper,
            )
            if not batch:
                break
            for item in batch:
                if item.target and item.target.target_id:
                    if skip:
                        skip -= 1
                    else:
                        visible.append(item)
            before = (batch[-1].created_at, batch[-1].id)
            if len(batch) < limit + 1:
                break
        more = len(visible) > limit
        items = visible[:limit]
        next_cursor = None
        if more:
            last = items[-1]
            encoded = json.dumps(
                {
                    "scope": [str(workspace_id), str(scope.page_id), period],
                    "upper": upper.isoformat(),
                    "time": last.created_at.isoformat(),
                    "id": str(last.id),
                }
            ).encode()
            next_cursor = (
                "page-activity-v2:" + base64.urlsafe_b64encode(encoded).decode()
            )
        return ActivityListData(items=items, unread_count=0, next_cursor=next_cursor)

    def _pending_reviews(
        self,
        session: Session,
        workspace_id: UUID,
        since: datetime | None,
        until: datetime | None,
        limit: int,
        audience_for: UUID | None,
        actor_role: str | None,
    ) -> list[ActivityItemRead]:
        statement = select(ReviewItem).where(
            ReviewItem.workspace_id == workspace_id,
            ReviewItem.status == "pending",
        )
        statement, _ = self.review_service.apply_list_visibility(
            session=session,
            statement=statement,
            workspace_id=workspace_id,
            audience_for=audience_for,
            scope="all",
            actor_role=actor_role,
        )
        if since is not None:
            statement = statement.where(ReviewItem.created_at >= since)
        if until is not None:
            statement = statement.where(ReviewItem.created_at < until)
        rows = session.scalars(
            statement.order_by(ReviewItem.created_at.desc()).limit(limit)
        ).all()
        return [
            ActivityItemRead(
                id=review.id,
                lane="pending",
                event_key=None,
                event_args={},
                title=f"Review required: {review.review_type}",
                body=review.decision_summary,
                target_title=review.decision_summary,
                actor=None,
                target=ActivityTarget(
                    target_kind="change_set",
                    target_id=review.change_set_id,
                ),
                created_at=review.created_at,
                review_item_id=review.id,
                affordances=["open"],
            )
            for review in rows
        ]

    @staticmethod
    def _audit_history(
        session: Session,
        workspace_id: UUID,
        since: datetime | None,
        until: datetime | None,
        kind: str,
        limit: int,
        actor: ActorContext | None = None,
        page_scope: PageActivityScope | None = None,
        deferred_identity: DeferredIdentityLabels | None = None,
        before: tuple[datetime, UUID] | None = None,
        upper: datetime | None = None,
    ) -> list[ActivityItemRead]:
        statement = select(AuditEvent).where(
            AuditEvent.workspace_id == workspace_id,
            not_(_audit_only_observation_event_clause()),
        )
        if since is not None:
            statement = statement.where(AuditEvent.created_at >= since)
        if until is not None:
            statement = statement.where(AuditEvent.created_at < until)
        if page_scope is not None:
            statement = statement.where(page_scope.audit_clause())
        if upper is not None:
            statement = statement.where(AuditEvent.created_at <= upper)
        if before is not None:
            statement = statement.where(
                or_(
                    AuditEvent.created_at < before[0],
                    and_(AuditEvent.created_at == before[0], AuditEvent.id < before[1]),
                )
            )
        # Apply the workspace/system split IN the audit query, before the limit:
        # audit is the only source that produces system rows, so without this a
        # flood of newer auth/oauth rows fills this page and crowds out workspace
        # audit rows of the requested kind (the post-merge filter then can't
        # recover rows that were never fetched).
        if kind == "system":
            statement = statement.where(_system_audit_event_clause())
        elif kind == "workspace":
            statement = statement.where(not_(_system_audit_event_clause()))
        elif kind == "review_decision":
            statement = statement.where(_review_decision_audit_event_clause())
        rows = session.scalars(
            statement.order_by(
                AuditEvent.created_at.desc(), AuditEvent.id.desc()
            ).limit(limit)
        ).all()
        projection = ContextActivityProjection(session, workspace_id, actor, rows)
        # `users` is control-plane and `session` is the workspace plane, so the
        # human labels are either resolved here (single-database shape) or left
        # as placeholders for the caller's identity session -- never read off
        # `session` when the caller asked to defer them.
        actor_keys = [(event.actor_kind, event.actor_id) for event in rows]
        actor_names = (
            workspace_plane_actor_display_names(actor_keys)
            if deferred_identity is not None
            else resolve_actor_display_names(session, actor_keys)
        )
        # An audit "mcp_client" actor is only ever generically labeled by
        # actor_names above ("MCP client"); resolve the real registered name
        # and credential owner so the feed can tell two AI tools apart.
        # MCPClient is workspace-plane, so its name always comes off `session`;
        # only the owner's name is an identity-plane read.
        mcp_client_actors = resolve_mcp_client_actor_details(
            session,
            workspace_id,
            (event.actor_id for event in rows if event.actor_kind == "mcp_client"),
        )
        owner_names = (
            {}
            if deferred_identity is not None
            else resolve_actor_display_names(
                session,
                (
                    ("human", str(client.owner_user_id))
                    for client in mcp_client_actors.values()
                    if client.owner_user_id is not None
                ),
            )
        )
        items: list[ActivityItemRead] = []
        for event in rows:
            # M-01: when the audit target is not visible to the actor, redact
            # EVERY name/identity the row would otherwise expose — the summary
            # (which can embed the target title, e.g. a tree rename), the target
            # title, and the target id — not just the resolved title.
            review = projection.reviews.get(event.target_id)
            context_review = review and any(
                op.target_type == "context_page"
                for op in projection.operations[review.change_set_id]
            )
            context_target = (
                event.target_kind == "context_page"
                or (event.target_kind == "review_item" and context_review)
                or (
                    event.target_kind == "workspace_tree_node"
                    and projection.tree
                    and event.target_id in projection.tree.nodes
                )
            )
            visible = (
                projection.visible(event)
                if context_target
                else _audit_target_visible(session, event, actor)
            )
            event_key, event_args = activity_event_metadata(event, visible=visible)
            page_id = projection.page_for_event(
                event, page_scope.page_id if page_scope else None
            )
            details, review_id = (
                projection.details(event, page_id) if visible else (None, None)
            )
            if details is not None:
                event_args = (
                    {
                        "previous_title": details.rename.before or "",
                        "title": details.rename.after or "",
                    }
                    if details.rename
                    else {}
                )
            client_actor = (
                mcp_client_actors.get(event.actor_id)
                if event.actor_kind == "mcp_client"
                else None
            )
            activity_actor = ActivityActor(
                actor_kind=event.actor_kind,
                # A page Activity card has no user-facing use for a raw
                # UUID.  Keep ids in the workspace/debug feed, but make
                # the Context-scoped contract presentation-safe.
                actor_id=(None if page_scope is not None else event.actor_id),
                display_name=(
                    client_actor.display_name
                    if client_actor is not None
                    else actor_names[(event.actor_kind, event.actor_id)]
                ),
                credential_owner_display_name=(
                    owner_names.get(("human", str(client_actor.owner_user_id)))
                    if client_actor is not None
                    and client_actor.owner_user_id is not None
                    else None
                ),
                is_current_user=(
                    actor is not None
                    and actor.actor_kind == event.actor_kind
                    and actor.actor_id == event.actor_id
                ),
            )
            if deferred_identity is not None:
                # The two identity-plane labels this row could not produce: the
                # human author's own name, and the name of the human whose
                # credential an MCP client ran under.
                if event.actor_kind == "human" and event.actor_id is not None:
                    deferred_identity.request(
                        event.actor_id, activity_actor, "display_name"
                    )
                if client_actor is not None and client_actor.owner_user_id is not None:
                    deferred_identity.request(
                        client_actor.owner_user_id,
                        activity_actor,
                        "credential_owner_display_name",
                    )
            items.append(
                ActivityItemRead(
                    id=event.id,
                    lane=lane_for_audit_event(event),
                    event_key=event_key,
                    event_args=event_args,
                    title=(
                        (event.event_summary or event.action)
                        if visible and not projection.private_edit_hidden(event)
                        else event.action
                    ),
                    body=event.action,
                    target_title=(
                        (
                            projection.pages[page_id].title
                            if details
                            else target_title_for_audit_event(session, event)
                        )
                        if visible
                        else None
                    ),
                    actor=activity_actor,
                    details=details,
                    target=ActivityTarget(
                        target_kind=event.target_kind,
                        target_id=event.target_id if visible else None,
                    ),
                    created_at=event.created_at,
                    review_item_id=review_id,
                    affordances=["open"],
                )
            )
        return items

    @staticmethod
    def _mcp_traces(
        session: Session,
        workspace_id: UUID,
        since: datetime | None,
        until: datetime | None,
        limit: int,
        deferred_identity: DeferredIdentityLabels | None = None,
    ) -> list[ActivityItemRead]:
        occurred_at = func.coalesce(MCPToolCall.completed_at, MCPToolCall.started_at)
        statement = (
            select(MCPToolCall, MCPClient)
            .join(
                MCPClient,
                MCPToolCall.client_id == MCPClient.id,
            )
            .where(MCPToolCall.workspace_id == workspace_id)
        )
        if since is not None:
            statement = statement.where(occurred_at >= since)
        if until is not None:
            statement = statement.where(occurred_at < until)
        rows = session.execute(
            statement.order_by(desc(occurred_at), desc(MCPToolCall.id)).limit(limit)
        ).all()
        # `client` is already joined per row here (unlike the audit path,
        # which only has an actor_id string), but the credential owner still
        # needs its own lookup -- and that one reads control-plane `users`, so
        # it is skipped entirely when the caller defers identity labels.
        owner_names = (
            {}
            if deferred_identity is not None
            else resolve_actor_display_names(
                session,
                (
                    ("human", str(client.issued_to_user_id))
                    for _, client in rows
                    if client.issued_to_user_id is not None
                ),
            )
        )
        items: list[ActivityItemRead] = []
        for tool_call, client in rows:
            activity_actor = ActivityActor(
                actor_kind="mcp_client",
                actor_id=str(client.id),
                display_name=client.name or "MCP client",
                credential_owner_display_name=(
                    owner_names.get(("human", str(client.issued_to_user_id)))
                    if client.issued_to_user_id is not None
                    else None
                ),
            )
            if deferred_identity is not None and client.issued_to_user_id is not None:
                deferred_identity.request(
                    client.issued_to_user_id,
                    activity_actor,
                    "credential_owner_display_name",
                )
            items.append(
                ActivityItemRead(
                    id=tool_call.id,
                    lane=lane_for_mcp_tool_call(tool_call),
                    event_key=None,
                    event_args={},
                    title=mcp_tool_call_title(tool_call),
                    body=mcp_tool_call_body(tool_call, client),
                    target_title=client.name,
                    actor=activity_actor,
                    target=ActivityTarget(
                        target_kind="mcp_tool_call",
                        target_id=tool_call.id,
                    ),
                    created_at=tool_call.completed_at or tool_call.started_at,
                    review_item_id=None,
                    affordances=["open"],
                )
            )
        return items

    @staticmethod
    def _jobs(
        session: Session,
        workspace_id: UUID,
        since: datetime | None,
        until: datetime | None,
        limit: int,
    ) -> list[ActivityItemRead]:
        # Internal index-maintenance jobs are not user-facing operations: a
        # context edit/publish or a library extraction enqueues an `embed` job on
        # every change, which would flood the operations feed with silent
        # successes. Exclude them here — index failures surface via the
        # stale-index query warning and `dtxwiki embedding doctor`, not activity.
        statement = select(Job).where(
            Job.workspace_id == workspace_id,
            Job.job_type.not_in(HIDDEN_JOB_TYPES),
        )
        if since is not None:
            statement = statement.where(Job.updated_at >= since)
        if until is not None:
            statement = statement.where(Job.updated_at < until)
        rows = session.scalars(
            statement.order_by(Job.updated_at.desc(), Job.created_at.desc()).limit(
                limit
            )
        ).all()
        return [
            ActivityItemRead(
                id=job.id,
                lane=lane_for_job(job),
                event_key=None,
                event_args={},
                title=f"{job.job_type.replace('_', ' ').title()} {job.status}",
                body=job_summary(job),
                target_title=job.job_type.replace("_", " ").title(),
                actor=None,
                target=ActivityTarget(target_kind="job", target_id=job.id),
                created_at=job.updated_at,
                review_item_id=None,
                affordances=["open"],
            )
            for job in rows
        ]


# "System" = platform/operations (not user-authored content): authentication,
# OAuth, automated quality checks (lint.*), workspace settings/admin (settings.*),
# and background jobs (target_kind=job). The audit-action prefixes; jobs are
# matched separately in _is_system_activity_item since they are not AuditEvents.
_SYSTEM_AUDIT_PREFIXES = ("auth.", "oauth.", "lint.", "settings.")

# These Library transport reads belong in the immutable security audit ledger,
# but they are observations rather than workspace changes. Browser PDF/media
# viewers can issue many byte-range requests for one open, so projecting them
# into History both misrepresents the operation and can crowd real changes out
# of a limited page.
_AUDIT_ONLY_OBSERVATION_ACTIONS = (
    "library.artifact.read",
    "library.original.downloaded",
    "library.preview.source.read",
    "library.semantic_overlay.preview.read",
)


def _audit_only_observation_event_clause():
    return AuditEvent.action.in_(_AUDIT_ONLY_OBSERVATION_ACTIONS)


def _system_audit_event_clause():
    # The audit-source half of _is_system_activity_item, expressed on the
    # AuditEvent columns so the workspace/system split runs in SQL before the
    # per-source limit. The projection is title=event_summary or action,
    # body=action; coalesce keeps a NULL event_summary from turning the OR into
    # SQL NULL (which would wrongly drop workspace rows under not_()).
    summary = func.coalesce(AuditEvent.event_summary, "")
    clauses = []
    for prefix in _SYSTEM_AUDIT_PREFIXES:
        clauses.append(AuditEvent.action.like(f"{prefix}%"))
        clauses.append(summary.like(f"{prefix}%"))
    return or_(*clauses)


def _is_system_activity_item(item: ActivityItemRead) -> bool:
    # Mirrors the frontend isSystemActivityItem (activityLabels.ts). Background
    # jobs (target_kind=job) and auth/oauth/lint/settings audit rows are "system"
    # (platform/operations); everything else is workspace content. The audit
    # projection sets body=action, so an event whose human summary is "Signed in"
    # still classifies via its "auth." body.
    if item.target is not None and item.target.target_kind == "job":
        return True
    title = item.title or ""
    body = item.body or ""
    return any(
        title.startswith(prefix) or body.startswith(prefix)
        for prefix in _SYSTEM_AUDIT_PREFIXES
    )


def _review_decision_audit_event_clause():
    # RD-3: the audit-source half of _is_review_decision_activity_item, expressed
    # on AuditEvent so the kind split runs in SQL before the per-source limit.
    return AuditEvent.action == "review.decision"


def _is_review_decision_activity_item(item: ActivityItemRead) -> bool:
    # RD-3: review-decision audit rows. The audit projection sets body=action,
    # so an exact compare on the action string classifies the row.
    return (item.body or "") == "review.decision"


def lane_for_audit_event(event: AuditEvent) -> str:
    if event.action in {"feedback.submit", "agent_page.draft"}:
        return "advisory"
    return "history"


_ACTIVITY_EVENT_ARGUMENTS: dict[str, tuple[ActivityEventKey, tuple[str, ...]]] = {
    "context_page.draft.publish": (
        "context_page.draft.publish",
        ("version_id",),
    ),
    "tree.node.move": (
        "tree.node.move",
        ("previous_parent_id", "new_parent_id", "reason"),
    ),
    "tree.node.rename": (
        "tree.node.rename",
        ("previous_title", "title"),
    ),
    "workspace.join_request.requested": (
        "workspace.join_request.requested",
        ("attempt_no",),
    ),
    "workspace.join_request.approved": (
        "workspace.join_request.approved",
        ("attempt_no", "granted_role"),
    ),
    "workspace.join_request.rejected": (
        "workspace.join_request.rejected",
        ("attempt_no",),
    ),
}

for _event_key in get_args(ActivityEventKey):
    _ACTIVITY_EVENT_ARGUMENTS.setdefault(_event_key, (_event_key, ()))


def activity_event_metadata(
    event: AuditEvent, *, visible: bool
) -> tuple[ActivityEventKey | None, dict[str, str]]:
    """Project known audit actions into stable presentation metadata.

    The audit summary remains untouched and is still returned as ``title``.
    Unknown actions deliberately have no key so clients fall back to that
    stored text. Redacted targets also stay unkeyed to preserve the existing
    fail-closed presentation rather than translating a sensitive operation.
    """
    definition = _ACTIVITY_EVENT_ARGUMENTS.get(event.action)
    if definition is None or not visible:
        return None, {}
    event_key, argument_names = definition
    payload = event.payload or {}
    arguments = {
        name: str(payload[name])
        for name in argument_names
        if payload.get(name) is not None
    }
    return event_key, arguments


def lane_for_mcp_tool_call(tool_call: MCPToolCall) -> str:
    if tool_call.result_status in {"failed", "rejected_by_policy", "conflict"}:
        return "advisory"
    return "history"


def mcp_tool_call_title(tool_call: MCPToolCall) -> str:
    return f"MCP {tool_call.tool_name.replace('_', ' ')} {tool_call.result_status}"


def mcp_tool_call_body(tool_call: MCPToolCall, client: MCPClient) -> str:
    parts = [client.name, f"status={tool_call.result_status}"]
    if tool_call.latency_ms is not None:
        parts.append(f"{tool_call.latency_ms} ms")
    if tool_call.error_code:
        parts.append(f"error={tool_call.error_code}")
    if tool_call.result_summary:
        parts.append(tool_call.result_summary)
    return "; ".join(parts)


def _audit_target_visible(
    session: Session, event: AuditEvent, actor: ActorContext | None
) -> bool:
    """Whether ``actor`` may see the audit event's target.

    Governs whether the row's summary/title/target id are surfaced. Handles the
    sensitivity-bearing target kinds (context/agent/library/tree node); kinds
    with no per-actor target sensitivity (change_set, workspace, ...) are
    visible. A dangling target (already gone) leaks nothing.
    """
    target_id = event.target_id
    if target_id is None:
        return True
    kind = event.target_kind
    # A DANGLING sensitivity-bearing target (already deleted) fails CLOSED: the
    # stored event_summary / payload title still holds the target's snapshot
    # name, and we can no longer confirm its trust level, so we must not surface
    # a possibly-restricted name we cannot re-authorize.
    if kind == "context_page":
        if event.workspace_id is None:
            return False
        page = get_in_workspace(
            session,
            ContextPage,
            workspace_id=event.workspace_id,
            resource_id=target_id,
        )
        return context_page_visible(page, actor, workspace_id=event.workspace_id)
    if kind == "agent_page":
        if event.workspace_id is None:
            return False
        page = get_in_workspace(
            session,
            AgentPage,
            workspace_id=event.workspace_id,
            resource_id=target_id,
        )
        return agent_page_visible(page, actor, workspace_id=event.workspace_id)
    if kind in {"source", "library_item"}:
        if event.workspace_id is None:
            return False
        source = get_in_workspace(
            session,
            LibraryItem,
            workspace_id=event.workspace_id,
            resource_id=target_id,
        )
        return library_item_visible(source, actor, workspace_id=event.workspace_id)
    if kind == "workspace_tree_node":
        if event.workspace_id is None:
            return False
        node = get_in_workspace(
            session,
            WorkspaceTreeNode,
            workspace_id=event.workspace_id,
            resource_id=target_id,
        )
        if node is None or actor is None or node.workspace_id != event.workspace_id:
            return False
        return TreeReadService.node_visible_to_actor(session, node, actor)
    if kind == "workspace_join_request":
        # Join requests are an owner-only surface (WorkspaceOwnerAccessDep);
        # the requester's name (now in event_summary/target_title, see
        # workspace_join_service._emit_audit) must not widen to every
        # member's activity feed just because it started riding the payload.
        return actor is not None and actor.role == "owner"
    return True


def target_title_for_audit_event(session: Session, event: AuditEvent) -> str | None:
    """Resolve the target's display title (payload snapshot first, then the live
    model). Visibility is enforced by the caller via ``_audit_target_visible``;
    this only resolves the name once the target is known to be visible."""
    payload_title = _audit_payload_title(event)
    if payload_title is not None:
        return payload_title
    if event.workspace_id is None:
        return None
    if event.target_kind == "context_page":
        page = get_in_workspace(
            session,
            ContextPage,
            workspace_id=event.workspace_id,
            resource_id=event.target_id,
        )
        return page.title if page is not None else None
    if event.target_kind == "agent_page":
        page = get_in_workspace(
            session,
            AgentPage,
            workspace_id=event.workspace_id,
            resource_id=event.target_id,
        )
        return page.title if page is not None else None
    if event.target_kind in {"source", "library_item"}:
        source = get_in_workspace(
            session,
            LibraryItem,
            workspace_id=event.workspace_id,
            resource_id=event.target_id,
        )
        if source is None:
            return None
        return source.title or source.uri
    return None


def _audit_payload_title(event: AuditEvent) -> str | None:
    payload = event.payload or {}
    for key in ("target_title", "title", "proposed_title"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def lane_for_job(job: Job) -> str:
    if job.status == "failed":
        return "pending"
    if job.status in {"queued", "running"}:
        return "advisory"
    return "history"


def job_summary(job: Job) -> str:
    # /activity is a viewer-gated surface, so the failure reason here must be
    # the same stable code GET /workspaces/{id}/jobs serves -- never
    # job.last_error, whose arbitrary worker ``str(exc)`` text (host paths,
    # SQLAlchemy SQL with bound parameters, provider messages) never crosses
    # any API boundary, including /api/v1/admin/jobs -- it is a protected
    # server-log/DB-diagnostic value only. Same helper as job_read_projection
    # so the two readers of this column cannot diverge again. A code rather
    # than a sentence, because the feed is bilingual and the client owns the
    # copy.
    parts = [f"trigger={job.trigger_type}", f"progress={job.progress}%"]
    if job.stage:
        parts.append(f"stage={job.stage}")
    failure_code = public_job_failure_code(job)
    if failure_code is not None:
        parts.append(f"error_code={failure_code}")
    return "; ".join(parts)


def period_bounds(period: str) -> tuple[datetime | None, datetime | None]:
    now = datetime.now(UTC)
    today = datetime.combine(now.date(), time.min, tzinfo=UTC)
    if period == "today":
        return today, None
    if period == "yesterday":
        start = today - timedelta(days=1)
        return start, today
    if period == "week":
        start = today - timedelta(days=today.weekday())
        return start, None
    if period == "month":
        start = datetime.combine(date(now.year, now.month, 1), time.min, tzinfo=UTC)
        return start, None
    return None, None


_ACTIVITY_CURSOR_PREFIX = "activity-offset:"
_MAX_ACTIVITY_OFFSET = 10_000


def _encode_activity_cursor(offset: int) -> str:
    token = f"{_ACTIVITY_CURSOR_PREFIX}{offset}".encode()
    return base64.urlsafe_b64encode(token).decode()


def _decode_activity_cursor(cursor: str | None) -> int:
    # Tolerant: a missing/garbled cursor restarts at offset 0 rather than 500ing
    # (mirrors the review/ask cursor handling).
    if not cursor:
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return 0
    if not raw.startswith(_ACTIVITY_CURSOR_PREFIX):
        return 0
    try:
        offset = int(raw[len(_ACTIVITY_CURSOR_PREFIX) :])
    except ValueError:
        return 0
    return min(max(0, offset), _MAX_ACTIVITY_OFFSET)

"""Reader-visible typed activity, independent of the legacy audit feed.

Grouping consumes the merged ledger before UI filters. Only a proven pure read
can join a run; an excluded but visible event still ends that run. Hidden MCP
records are absent even as boundaries. Cursors advance whole display entries.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import islice
from uuid import UUID

from sqlalchemy import and_, func, literal, or_, select, union_all
from sqlalchemy.orm import Session

from backend.models.audit import AuditEvent
from backend.models.job import Job
from backend.models.mcp_trace import MCPToolCall
from backend.models.review import ReviewItem
from backend.policy.auth import ActorContext
from backend.repositories.workspace_scoped import select_in_workspace
from backend.schemas.activity import (
    ActivityFeedData,
    ActivityFeedEntry,
    ActivityFeedEvent,
    ActivityFeedLaneCount,
    ActivityItemRead,
    ActivityNavigation,
    ActivityReadGroupData,
)
from backend.services.activity_service import (
    ActivityService,
    _audit_only_observation_event_clause,
    _is_review_decision_activity_item,
    _is_system_activity_item,
    period_bounds,
)
from backend.services.job_queue import HIDDEN_JOB_TYPES
from backend.services.job_read_service import public_job_failure_code
from backend.services.workspace_urls import (
    agent_page_url,
    context_page_url,
    library_item_url,
    review_item_url,
)
from backend.utils.actor_display import DeferredIdentityLabels

_BATCH_SIZE = 50
# Filters run after the visible ledger is merged (they must not move group
# boundaries), so a sparse filter could otherwise walk the whole ledger. Each
# request scans at most this many source rows and resumes from its scan
# position through the cursor.
_SCAN_BUDGET = 10 * _BATCH_SIZE
_MAX_GROUP_SIZE = 50
_GROUP_SPAN = timedelta(minutes=5)
_TOKEN_TTL = timedelta(hours=2)
# Tokens are signed here; tolerate another app node's clock running ahead.
_CLOCK_SKEW = timedelta(minutes=2)
_LANES = ("pending", "advisory", "history")
_SUBTYPES = {
    "all": {"all"},
    "workspace": {"all", "library", "context", "agent", "ask", "review", "chat"},
    "mcp": {"all", "read", "write", "search"},
    "system": {"all", "auth", "oauth", "settings", "lint", "jobs"},
    "review_decision": {"all"},
}
_PREFIX_SUBTYPE = {
    "library": "library",
    "context_page": "context",
    "page": "context",
    "context_ingress": "context",
    "tree": "context",
    "feedback": "context",
    "agent_page": "agent",
    "agent": "agent",
    "bundle": "agent",
    "ask": "ask",
    "review": "review",
    "mcp_chat_session": "chat",
}
# Opening, patching and discarding an editing session all describe a private
# editing row. Like the matching MCP calls, they stay with their author.
_PRIVATE_EDITING_ACTIONS = (
    "page.begin_context_edit",
    "page.patch_context_edit",
    "page.discard_context_edit",
)
# Feed operations for MCP tools whose presentation verb ("edit") would
# misdescribe the transition; these match the audit vocabulary below.
_MCP_OPERATIONS = {
    "begin_context_edit": "log",
    "discard_context_edit": "delete",
    "submit_context_edit_for_review": "review",
    "submit_revision_for_review": "review",
}


# Audit actions describe completed domain transitions, not arbitrary verbs in
# display copy. Keep this vocabulary explicit: an autosave is an edit, while
# opening/discarding an editing session is never a completed content edit.
_AUDIT_OPERATIONS = {
    "page.create": "create",
    "page.update": "edit",
    "page.autosave.draft": "edit",
    "page.patch_context_edit": "edit",
    "page.begin_context_edit": "log",
    "page.discard_context_edit": "delete",
    "page.submit_context_edit_for_review": "review",
    "page.publish": "publish",
    "page.unpublish": "edit",
    "page.restore_version": "restore",
    "context_page.draft.create": "create",
    "context_page.draft.update": "edit",
    "context_page.draft.publish": "publish",
    "context_page.updated": "edit",
    "context_page.draft.reconciled": "edit",
    "agent_page.draft.create": "create",
    "agent_page.draft.update": "edit",
    "agent_page.draft.restore": "restore",
    "agent_page.activated": "publish",
    "library.item.register": "create",
    "library.item.version.create": "create",
    "library.item.delete": "delete",
    "library.semantic_overlay.saved": "edit",
    "library.upload.accepted": "create",
    "tree.node.create": "create",
    "tree.node.rename": "edit",
    "tree.node.move": "edit",
    "tree.node.reorder": "edit",
    "tree.node.delete": "delete",
    "workspace.access.updated": "edit",
    "workspace.join_request.requested": "review",
    "workspace.join_request.approved": "review",
    "workspace.join_request.rejected": "review",
    "review.decision": "review",
    "context_ingress.proposal.create": "create",
    "context_ingress.proposal.accept": "review",
    "context_ingress.proposal.reject": "review",
}
_AUDIT_RESULTS = {
    "library.extraction.failed": "failed",
    "library.conversion.failed": "failed",
    "auth.password.change.failed": "failed",
    "auth.reauthentication.failed": "failed",
    "auth.email.delivery.failed": "failed",
    "credential_replica.sync_failed": "failed",
    "mcp.client.runtime.denied": "rejected_by_policy",
    "oauth.permission.denied": "rejected_by_policy",
    "library.upload.accepted": "accepted",
}


class InvalidActivityTokenError(ValueError):
    """Public callers must reset the list after a rejected/expired token."""


@dataclass
class _VisibleEvent:
    event: ActivityFeedEvent
    source: str
    grouping_key: tuple[str, ...] | None = None
    # The merged ledger's (occurred_at, source, id). Hydration re-reads
    # mutable rows (a job's updated_at), so cursors use this, not the event.
    position: tuple[datetime, str, str] | None = None

    @property
    def key(self) -> tuple[datetime, str, str]:
        return self.position or (self.event.created_at, self.source, str(self.event.id))


@dataclass
class _Entry:
    members: list[_VisibleEvent]

    @property
    def last_key(self) -> tuple[datetime, str, str]:
        return self.members[-1].key


@dataclass
class _Scan:
    """Where a budgeted scan of the merged ledger stopped."""

    position: tuple[datetime, str, str] | None
    truncated: bool = False
    # Position before the last yielded visible row, and where to resume.
    before: tuple[datetime, str, str] | None = None
    resume: tuple[datetime, str, str] | None = None


class ActivityFeedService:
    def __init__(self, activity: ActivityService, *, cursor_secret: str):
        if not cursor_secret:
            raise ValueError("Activity cursor signing requires a persistent secret.")
        self.activity = activity
        # Separate domain from browser sessions/access-key cursors using the
        # deployment's persistent secret (never a process-random signing key).
        self._secret = hmac.new(
            cursor_secret.encode(), b"dtxwiki.activity-feed.v1", hashlib.sha256
        ).digest()

    def list_feed(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        actor: ActorContext,
        actor_role: str,
        period: str = "today",
        kind: str = "all",
        subtype: str = "all",
        lane: str | None = None,
        group_reads: bool = True,
        limit: int = 50,
        cursor: str | None = None,
        deferred_identity: DeferredIdentityLabels | None = None,
    ) -> ActivityFeedData:
        if kind not in _SUBTYPES or subtype not in _SUBTYPES[kind]:
            raise InvalidActivityTokenError(
                "Activity filters are invalid; reload the list."
            )
        if period not in {"today", "yesterday", "week", "month", "all"}:
            raise InvalidActivityTokenError(
                "Activity period is invalid; reload the list."
            )
        if lane is not None and lane not in _LANES:
            raise InvalidActivityTokenError(
                "Activity lane is invalid; reload the list."
            )
        if not 1 <= limit <= 50:
            raise InvalidActivityTokenError("Activity limit is invalid.")
        fingerprint = self._fingerprint(
            workspace_id, actor, period, kind, subtype, lane, group_reads
        )
        now = datetime.now(UTC)
        after = None
        snapshot = now
        since, until = period_bounds(period)
        if cursor:
            payload = self._decode(cursor, "cursor", fingerprint, now)
            snapshot = _parse_time(payload["snapshot"])
            since = _parse_time(payload["since"]) if payload.get("since") else None
            until = _parse_time(payload["until"]) if payload.get("until") else None
            after = _parse_key(payload.get("after"))
            if lane is None:
                raise InvalidActivityTokenError("Overview activity must be refreshed.")
        scan = _Scan(position=after)
        raw = self._visible_stream(
            session,
            workspace_id,
            actor=actor,
            actor_role=actor_role,
            snapshot=snapshot,
            since=since,
            until=until,
            after=after,
            scan=scan,
            deferred_identity=deferred_identity,
        )
        grouped = self._entries(raw, kind, subtype, lane, group_reads, scan, after)
        # Lane lists page in pure recency. Overview reserves review decisions
        # before filling its budget by recency, without treating failed jobs as
        # human reviews. The overview intentionally has no load-more cursor.
        reserved: list[_Entry] = []
        if (
            lane is None
            and kind in {"all", "workspace"}
            and subtype in {"all", "review"}
        ):
            reviews = self.activity._pending_reviews(
                session=session,
                workspace_id=workspace_id,
                since=since,
                until=until,
                snapshot=snapshot,
                limit=limit + 1,
                audience_for=actor.user_id,
                actor_role=actor_role,
            )
            reserved = [
                _Entry(
                    [
                        _VisibleEvent(
                            self._event(item, "review", workspace_id), "review"
                        )
                    ]
                )
                for item in reviews
            ]
            reserved.sort(key=lambda entry: entry.last_key, reverse=True)
        candidate_budget = limit + len(reserved) + 1
        candidates = list(islice(grouped, candidate_budget))
        # A budget-truncated scan has not proven the rest of the range empty.
        exhausted = len(candidates) < candidate_budget and not scan.truncated
        if lane is None:
            selected = reserved[:limit]
            seen = {
                (entry.members[0].source, entry.members[0].event.id)
                for entry in selected
            }
            selected += [
                entry
                for entry in candidates
                if (entry.members[0].source, entry.members[0].event.id) not in seen
            ][: limit - len(selected)]
            selected.sort(key=lambda entry: entry.members[0].key, reverse=True)
        else:
            selected = candidates[:limit]
        selected_ids = {
            (entry.members[0].source, entry.members[0].event.id) for entry in selected
        }
        remaining = [
            entry
            for entry in candidates + reserved
            if (entry.members[0].source, entry.members[0].event.id) not in selected_ids
        ]
        has_more = bool(remaining) or not exhausted
        entries = [
            self._serialize_entry(entry, fingerprint, snapshot, since, until)
            for entry in selected
        ]
        resume = None
        if lane is not None:
            if remaining and selected:
                resume = selected[-1].last_key
            elif scan.truncated:
                # Possibly an empty page: the cursor carries the scan forward.
                resume = scan.resume
        next_cursor = None
        if resume is not None:
            next_cursor = self._encode(
                {
                    "type": "cursor",
                    "fp": fingerprint,
                    "snapshot": snapshot.isoformat(),
                    "since": since.isoformat() if since else None,
                    "until": until.isoformat() if until else None,
                    "after": _key_payload(resume),
                    "expires": (snapshot + _TOKEN_TTL).isoformat(),
                }
            )
        lanes = {
            item_lane: ActivityFeedLaneCount(
                returned_entry_count=sum(e.event.lane == item_lane for e in entries),
                represented_event_count=sum(
                    e.event_count for e in entries if e.event.lane == item_lane
                ),
                has_more=(
                    True
                    if any(e.members[0].event.lane == item_lane for e in remaining)
                    else False
                    if exhausted or lane is not None
                    else None
                ),
            )
            for item_lane in _LANES
        }
        return ActivityFeedData(
            entries=entries,
            next_cursor=next_cursor,
            unread_count=self._attention_count(
                session,
                workspace_id,
                actor,
                actor_role,
                since,
                until,
                snapshot,
                kind,
                subtype,
                lane,
            ),
            returned_entry_count=len(entries),
            represented_event_count=sum(entry.event_count for entry in entries),
            has_more=has_more,
            lanes=lanes,
        )

    def read_group(
        self,
        session: Session,
        workspace_id: UUID,
        *,
        actor: ActorContext,
        actor_role: str,
        token: str,
        deferred_identity: DeferredIdentityLabels | None = None,
    ) -> ActivityReadGroupData:
        payload = self._decode(token, "group", None, datetime.now(UTC))
        if payload.get("workspace") != str(workspace_id) or payload.get(
            "reader"
        ) != _reader(actor):
            raise InvalidActivityTokenError(
                "Activity group scope changed; reload the list."
            )
        ids = payload.get("members")
        if not isinstance(ids, list) or not 2 <= len(ids) <= _MAX_GROUP_SIZE:
            raise InvalidActivityTokenError(
                "Activity group is invalid; reload the list."
            )
        try:
            member_ids = [UUID(value) for value in ids]
            snapshot = _parse_time(payload["snapshot"])
        except (TypeError, ValueError, KeyError) as exc:
            raise InvalidActivityTokenError(
                "Activity group is invalid; reload the list."
            ) from exc
        rows = self._hydrate(
            session,
            workspace_id,
            {"mcp": member_ids},
            actor,
            actor_role,
            deferred_identity,
        )
        visible = [
            rows[("mcp", identifier)]
            for identifier in member_ids
            if ("mcp", identifier) in rows
        ]
        for row in visible:
            if row.event.created_at > snapshot or _key_hash(
                row.grouping_key
            ) != payload.get("key"):
                raise InvalidActivityTokenError(
                    "Activity group changed; reload the list."
                )
        return ActivityReadGroupData(
            group_id=payload["group_id"],
            events=[row.event for row in visible],
            event_count=len(visible),
        )

    def _visible_stream(
        self,
        session,
        workspace_id,
        *,
        actor,
        actor_role,
        snapshot,
        since,
        until,
        after,
        scan,
        deferred_identity,
    ) -> Iterator[_VisibleEvent]:
        sources = []
        for source, model, occurred in [
            ("audit", AuditEvent, AuditEvent.created_at),
            ("job", Job, Job.updated_at),
            (
                "mcp",
                MCPToolCall,
                func.coalesce(MCPToolCall.completed_at, MCPToolCall.started_at),
            ),
            ("review", ReviewItem, ReviewItem.created_at),
        ]:
            conditions = []
            if source == "audit":
                conditions.append(~_audit_only_observation_event_clause())
                # Absent rather than redacted: a private record is not even a
                # group boundary for another reader.
                conditions.append(_private_editing_visible(actor))
            elif source == "job":
                conditions.append(Job.job_type.not_in(HIDDEN_JOB_TYPES))
            elif source == "review":
                conditions.append(ReviewItem.status == "pending")
            columns = (
                literal(source).label("source"),
                model.id.label("id"),
                occurred.label("occurred_at"),
            )
            if source == "audit":
                # AuditEvent is explicitly mixed-plane: its global NULL rows
                # must never enter this workspace projection. Other sources
                # use the required-scope repository rather than new direct ORM.
                statement = select(*columns).where(
                    model.workspace_id == workspace_id, *conditions
                )
            else:
                statement = select_in_workspace(
                    model, workspace_id=workspace_id, conditions=conditions
                ).with_only_columns(*columns)
            sources.append(statement)
        merged = union_all(*sources).subquery()
        position = after
        scanned = 0
        while True:
            statement = select(merged).where(merged.c.occurred_at <= snapshot)
            if since:
                statement = statement.where(merged.c.occurred_at >= since)
            if until:
                statement = statement.where(merged.c.occurred_at < until)
            if position:
                occurred, source, identifier = position
                statement = statement.where(
                    or_(
                        merged.c.occurred_at < occurred,
                        and_(
                            merged.c.occurred_at == occurred, merged.c.source < source
                        ),
                        and_(
                            merged.c.occurred_at == occurred,
                            merged.c.source == source,
                            merged.c.id < UUID(identifier),
                        ),
                    )
                )
            rows = session.execute(
                statement.order_by(
                    merged.c.occurred_at.desc(),
                    merged.c.source.desc(),
                    merged.c.id.desc(),
                ).limit(_BATCH_SIZE)
            ).all()
            if not rows:
                return
            ids: dict[str, list[UUID]] = {}
            for source, identifier, _occurred in rows:
                ids.setdefault(source, []).append(identifier)
            projected = self._hydrate(
                session, workspace_id, ids, actor, actor_role, deferred_identity
            )
            for source, identifier, occurred in rows:
                previous = position
                position = (_aware(occurred), source, str(identifier))
                scan.position = position
                item = projected.get((source, identifier))
                if item is not None:
                    item.position = position
                    scan.before = previous
                    yield item
            if len(rows) < _BATCH_SIZE:
                return
            scanned += len(rows)
            if scanned >= _SCAN_BUDGET:
                scan.truncated = True
                return

    def _hydrate(
        self, session, workspace_id, ids, actor, actor_role, deferred_identity
    ):
        result = {}
        common = dict(
            session=session,
            workspace_id=workspace_id,
            since=None,
            until=None,
            limit=_BATCH_SIZE,
        )
        for source, identifiers in ids.items():
            # The source readers hand back the rows they loaded, so the
            # projection below needs no second SELECT of the same batch.
            loaded = {}
            if source == "audit":
                items = self.activity._audit_history(
                    **common,
                    kind="all",
                    actor=actor,
                    item_ids=identifiers,
                    deferred_identity=deferred_identity,
                )
            elif source == "job":
                items = self.activity._jobs(
                    **common, item_ids=identifiers, rows_out=loaded
                )
            elif source == "review":
                items = self.activity._pending_reviews(
                    **common,
                    item_ids=identifiers,
                    audience_for=actor.user_id,
                    actor_role=actor_role,
                )
            else:
                items = self.activity._mcp_traces(
                    **common,
                    item_ids=identifiers,
                    deferred_identity=deferred_identity,
                    rows_out=loaded,
                )
            mcp_rows = loaded if source == "mcp" else {}
            mcp_projections = {}
            if source == "mcp":
                from backend.services.mcp_read_projection import (
                    project_mcp_activity_batch,
                )

                mcp_projections = project_mcp_activity_batch(
                    session, list(mcp_rows.values()), actor
                )
            job_rows = loaded if source == "job" else {}
            for item in items:
                event = self._event(item, source, workspace_id)
                if source == "job":
                    job = job_rows[item.id]
                    event = event.model_copy(
                        update={
                            "result_status": job.status,
                            "public_error_code": public_job_failure_code(job),
                        }
                    )
                grouping_key = None
                if source == "mcp":
                    call = mcp_rows[item.id]
                    projection = mcp_projections[item.id]
                    if not projection.visible:
                        continue
                    tool_name = (
                        call.tool_name
                        if re.fullmatch(r"[a-z][a-z0-9_]{0,99}", call.tool_name)
                        else None
                    )
                    operation = _MCP_OPERATIONS.get(tool_name, projection.operation)
                    event = event.model_copy(
                        update={
                            "title": "MCP tool execution",
                            "body": None,
                            "target_title": projection.title,
                            "tool_name": tool_name,
                            "operation": operation,
                            "result_status": projection.result,
                            "public_error_code": projection.public_error_code,
                            "duration_ms": projection.duration_ms,
                            "navigation": ActivityNavigation(
                                target_kind=projection.resource_kind,
                                target_id=projection.resource_id,
                                title=projection.title,
                                href=projection.navigation_path,
                            )
                            if projection.resource_id and projection.navigation_path
                            else None,
                        }
                    )
                    grouping_key = projection.grouping_key
                result[(source, item.id)] = _VisibleEvent(event, source, grouping_key)
        return result

    @staticmethod
    def _event(item, source, workspace_id):
        navigation = None
        target = item.target
        if item.review_item_id:
            navigation = ActivityNavigation(
                target_kind="review",
                target_id=item.review_item_id,
                title=item.target_title,
                href=review_item_url(workspace_id, item.review_item_id),
            )
        elif target and target.target_id:
            path = {
                "context_page": "context",
                "agent_page": "agent",
                "source": "library",
                "library_item": "library",
            }.get(target.target_kind)
            if path:
                navigation = ActivityNavigation(
                    target_kind=target.target_kind,
                    target_id=target.target_id,
                    title=item.target_title,
                    href=(
                        context_page_url(workspace_id, target.target_id)
                        if target.target_kind == "context_page"
                        else agent_page_url(workspace_id, target.target_id)
                        if target.target_kind == "agent_page"
                        else library_item_url(workspace_id, target.target_id)
                    ),
                )
        operation = "log"
        status = None
        action = item.body or ""
        if source == "review":
            operation, status = "review", "needs_review"
        elif source == "audit":
            operation = _AUDIT_OPERATIONS.get(action, "log")
            status = _AUDIT_RESULTS.get(action)
        return ActivityFeedEvent(
            **{name: getattr(item, name) for name in ActivityItemRead.model_fields},
            operation=operation,
            result_status=status,
            navigation=navigation,
        )

    @staticmethod
    def _matches(row, kind, subtype, lane):
        item = row.event
        if lane and item.lane != lane:
            return False
        system = _is_system_activity_item(item)
        if kind == "system" and not system:
            return False
        if kind == "workspace" and system:
            return False
        if kind == "mcp" and row.source != "mcp":
            return False
        if kind == "review_decision" and not _is_review_decision_activity_item(item):
            return False
        if subtype == "all":
            return True
        if kind == "mcp":
            actual = ""
            if item.operation == "search":
                actual = "search"
            elif item.operation in {"read", "guide", "cite"}:
                actual = "read"
            elif item.operation in {
                "create",
                "edit",
                "review",
                "publish",
                "restore",
                "delete",
            }:
                actual = "write"
        elif kind == "system":
            actual = "jobs" if row.source == "job" else (item.body or "").split(".")[0]
        elif row.source == "review":
            actual = "review"
        elif row.source == "mcp":
            return False
        else:
            actual = _PREFIX_SUBTYPE.get((item.body or "").split(".")[0], "")
        return subtype == actual

    def _entries(self, stream, kind, subtype, lane, group_reads, scan=None, after=None):
        scan = scan or _Scan(position=after)
        run: list[_VisibleEvent] = []
        run_resume = None
        for row in stream:
            matches = self._matches(row, kind, subtype, lane)
            joins = bool(
                group_reads
                and matches
                and row.grouping_key
                and run
                and run[0].grouping_key == row.grouping_key
                and len(run) < _MAX_GROUP_SIZE
                and run[0].event.created_at - row.event.created_at <= _GROUP_SPAN
                and run[0].event.created_at.date() == row.event.created_at.date()
            )
            if run and not joins:
                yield _Entry(run)
                run = []
            if not matches:
                continue
            if group_reads and row.grouping_key:
                if not run:
                    run_resume = scan.before
                run.append(row)
            else:
                yield _Entry([row])
        scan.resume = scan.position
        if run:
            # A truncated scan may have stopped inside this run. Resume at its
            # first member rather than split it across pages, unless that
            # would make no progress at all.
            if scan.truncated and run_resume != after:
                scan.resume = run_resume
            else:
                yield _Entry(run)

    def _serialize_entry(self, entry, fingerprint, snapshot, since, until):
        members = entry.members
        representative = members[0].event
        if len(members) == 1:
            return ActivityFeedEntry(
                entry_type="event",
                id=f"{members[0].source}:{representative.id}",
                event=representative,
                event_count=1,
                first_occurred_at=representative.created_at,
                last_occurred_at=representative.created_at,
            )
        group_id = (
            "read-group:"
            + hmac.new(
                self._secret,
                json.dumps(
                    [
                        fingerprint,
                        snapshot.isoformat(),
                        [str(row.event.id) for row in members],
                    ],
                    separators=(",", ":"),
                ).encode(),
                hashlib.sha256,
            ).hexdigest()[:32]
        )
        scope = json.loads(fingerprint)
        token = self._encode(
            {
                "type": "group",
                "fp": fingerprint,
                "workspace": scope["workspace"],
                "reader": scope["reader"],
                "snapshot": snapshot.isoformat(),
                "expires": (snapshot + _TOKEN_TTL).isoformat(),
                "group_id": group_id,
                "members": [str(row.event.id) for row in members],
                "key": _key_hash(members[0].grouping_key),
            }
        )
        return ActivityFeedEntry(
            entry_type="read_group",
            id=group_id,
            event=representative,
            event_count=len(members),
            first_occurred_at=members[-1].event.created_at,
            last_occurred_at=representative.created_at,
            group_token=token,
        )

    def _attention_count(
        self,
        session,
        workspace_id,
        actor,
        actor_role,
        since,
        until,
        snapshot,
        kind,
        subtype,
        lane,
    ):
        if lane not in {None, "pending"}:
            return 0
        count = 0
        if kind in {"all", "workspace"} and subtype in {"all", "review"}:
            conditions = [
                ReviewItem.status == "pending",
                ReviewItem.created_at <= snapshot,
            ]
            if since:
                conditions.append(ReviewItem.created_at >= since)
            if until:
                conditions.append(ReviewItem.created_at < until)
            statement = select_in_workspace(
                ReviewItem, workspace_id=workspace_id, conditions=conditions
            )
            statement, _ = self.activity.review_service.apply_list_visibility(
                session=session,
                statement=statement,
                workspace_id=workspace_id,
                audience_for=actor.user_id,
                scope="all",
                actor_role=actor_role,
            )
            count += int(
                session.scalar(select(func.count()).select_from(statement.subquery()))
                or 0
            )
        if kind in {"all", "system"} and subtype in {"all", "jobs"}:
            conditions = [
                Job.status == "failed",
                Job.job_type.not_in(HIDDEN_JOB_TYPES),
                Job.updated_at <= snapshot,
            ]
            if since:
                conditions.append(Job.updated_at >= since)
            if until:
                conditions.append(Job.updated_at < until)
            statement = select_in_workspace(
                Job, workspace_id=workspace_id, conditions=conditions
            )
            count += int(
                session.scalar(select(func.count()).select_from(statement.subquery()))
                or 0
            )
        return count

    @staticmethod
    def _fingerprint(workspace_id, actor, period, kind, subtype, lane, group_reads):
        return json.dumps(
            {
                "workspace": str(workspace_id),
                "reader": _reader(actor),
                "period": period,
                "kind": kind,
                "subtype": subtype,
                "lane": lane,
                "group_reads": group_reads,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def _encode(self, payload):
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).rstrip(b"=")
        signature = hmac.new(self._secret, encoded, hashlib.sha256).digest()
        return (
            encoded + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")
        ).decode()

    def _decode(self, token, expected_type, fingerprint, now):
        try:
            if len(token) > 16000:
                raise ValueError("oversized token")
            encoded, signature = token.split(".", 1)
            expected = hmac.new(
                self._secret, encoded.encode("ascii"), hashlib.sha256
            ).digest()
            if not hmac.compare_digest(_base64(signature), expected):
                raise ValueError("signature")
            payload = json.loads(_base64(encoded))
            if payload.get("type") != expected_type:
                raise ValueError("type")
            if fingerprint is not None and payload.get("fp") != fingerprint:
                raise ValueError("scope")
            if _parse_time(payload["expires"]) <= now:
                raise ValueError("expired")
            if _parse_time(payload["snapshot"]) > now + _CLOCK_SKEW:
                raise ValueError("future snapshot")
            return payload
        except (
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            binascii.Error,
            UnicodeError,
        ) as exc:
            raise InvalidActivityTokenError(
                "Activity token is invalid or expired; reload the list."
            ) from exc


def _reader(actor):
    return f"{actor.actor_kind}:{actor.actor_id}:{actor.user_id or ''}"


def _private_editing_visible(actor):
    private = AuditEvent.action.in_(_PRIVATE_EDITING_ACTIONS)
    if not actor.actor_kind or not actor.actor_id:
        return ~private
    return or_(
        ~private,
        and_(
            AuditEvent.actor_kind == actor.actor_kind,
            AuditEvent.actor_id == str(actor.actor_id),
        ),
    )


def _aware(value):
    # SQLite returns the merged ledger's timestamps without an offset.
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _key_hash(key):
    return (
        hashlib.sha256(json.dumps(key, separators=(",", ":")).encode()).hexdigest()
        if key
        else None
    )


def _key_payload(key):
    return [key[0].isoformat(), key[1], key[2]]


def _parse_key(value):
    if (
        not isinstance(value, list)
        or len(value) != 3
        or value[1] not in {"audit", "mcp", "job", "review"}
    ):
        raise InvalidActivityTokenError("Activity cursor is invalid; reload the list.")
    try:
        return _parse_time(value[0]), value[1], str(UUID(value[2]))
    except (TypeError, ValueError) as exc:
        raise InvalidActivityTokenError(
            "Activity cursor is invalid; reload the list."
        ) from exc


def _parse_time(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("missing UTC offset")
    return result.astimezone(UTC)


def _base64(value):
    return base64.b64decode(
        value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
    )

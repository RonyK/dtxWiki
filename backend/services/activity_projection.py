"""Read-time Context activity details; the audit ledger remains unchanged."""

from collections import defaultdict
from typing import get_args
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.orm import Session, load_only, selectinload

from backend.models.audit import AuditEvent
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.review import ReviewDecision, ReviewItem
from backend.policy.auth import ActorContext
from backend.policy.rbac import role_allows
from backend.repositories.workspace_scoped import (
    select_in_workspace,
    select_in_workspace_by_ids,
)
from backend.schemas.activity import (
    ActivityDetails,
    ActivityEventKey,
    ActivityLocation,
    ActivityMove,
    ActivityRename,
    ActivityVersion,
)
from backend.services.actor_visible_read import (
    context_page_visible,
    context_version_is_visible,
)
from backend.services.tree_projection import TreeProjectionService
from backend.services.tree_service import DEFAULT_TREE_MOVE_REASON


def audit_uuid(value) -> UUID | None:
    try:
        return UUID(str(value)) if value is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def audit_text(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def operation_version(operation: ChangeOperation) -> UUID | None:
    prefix = "context_page_version:"
    return (
        audit_uuid(operation.diff_ref[len(prefix) :])
        if operation.diff_ref and operation.diff_ref.startswith(prefix)
        else None
    )


class ContextActivityProjection:
    """Batch related objects once, then authorize every exposed reference.

    Payload ids are hints, never ownership evidence. Review ownership comes
    from its actual ChangeSet/ChangeOperation relationships. No body is read
    to reconstruct a historical save, and mutable revision counters are never
    substituted for a missing event-time revision.
    """

    def __init__(
        self,
        session: Session,
        workspace_id: UUID,
        actor: ActorContext | None,
        rows: list[AuditEvent],
    ) -> None:
        self.actor = actor
        self.workspace_id = workspace_id
        payloads = [row.payload or {} for row in rows]
        review_ids = {
            row.target_id for row in rows if row.target_kind == "review_item"
        } | {audit_uuid(p.get("review_item_id")) for p in payloads}
        change_ids = {audit_uuid(p.get("change_set_id")) for p in payloads}
        self.reviews = (
            {
                r.id: r
                for r in session.scalars(
                    select_in_workspace(
                        ReviewItem,
                        workspace_id=workspace_id,
                        criteria=(
                            or_(
                                ReviewItem.id.in_(review_ids - {None}),
                                ReviewItem.change_set_id.in_(change_ids - {None}),
                            ),
                        ),
                    )
                )
            }
            if review_ids - {None} or change_ids - {None}
            else {}
        )
        change_ids.update(r.change_set_id for r in self.reviews.values())
        change_ids.discard(None)
        from backend.services.review_service import (
            ReviewService,
            review_decision_audience_clause,
        )

        principal = actor.user_id if actor else None
        role = (actor.workspace_role or actor.role) if actor else None
        self.readable_reviews = (
            set(self.reviews) if role_allows(role or "viewer", "editor") else set()
        )
        if not self.readable_reviews and principal is not None and self.reviews:
            policy = ReviewService()
            for scope in ("to_me", "from_me"):
                self.readable_reviews.update(
                    session.scalars(
                        policy._apply_scope_predicate(
                            session=session,
                            statement=select_in_workspace_by_ids(
                                ReviewItem,
                                workspace_id=workspace_id,
                                resource_ids=self.reviews,
                            ).with_only_columns(ReviewItem.id),
                            workspace_id=workspace_id,
                            principal=principal,
                            scope=scope,
                            principal_role=role,
                        )
                    )
                )
        self.changes = (
            {
                c.id: c
                for c in session.scalars(
                    select_in_workspace_by_ids(
                        ChangeSet,
                        workspace_id=workspace_id,
                        resource_ids=change_ids,
                    )
                )
            }
            if change_ids
            else {}
        )
        self.operations = defaultdict(list)
        if change_ids:
            for op in session.scalars(
                select_in_workspace(
                    ChangeOperation,
                    workspace_id=workspace_id,
                    criteria=(ChangeOperation.change_set_id.in_(change_ids),),
                )
            ):
                self.operations[op.change_set_id].append(op)
        version_fields = (
            "version_id",
            "base_version_id",
            "previous_published_version_id",
            "restored_from_version_id",
            "draft_version_id",
            "parent_version_id",
            "session_version_id",
            "published_version_id",
            "exposed_version_id",
        )
        version_ids = {
            audit_uuid(p.get(key)) for p in payloads for key in version_fields
        } | {operation_version(op) for ops in self.operations.values() for op in ops}
        version_ids.discard(None)
        self.versions = (
            {
                v.id: v
                for v in session.scalars(
                    select_in_workspace_by_ids(
                        ContextPageVersion,
                        workspace_id=workspace_id,
                        resource_ids=version_ids,
                    ).options(
                        load_only(
                            ContextPageVersion.id,
                            ContextPageVersion.page_id,
                            ContextPageVersion.version_no,
                            ContextPageVersion.version_status,
                            ContextPageVersion.created_by_actor_kind,
                            ContextPageVersion.created_by_actor_id,
                        )
                    )
                )
            }
            if version_ids
            else {}
        )
        need_tree = any(row.target_kind == "workspace_tree_node" for row in rows)
        need_tree |= any(
            op.target_type == "workspace_tree_node"
            for ops in self.operations.values()
            for op in ops
        )
        self.tree = (
            TreeProjectionService().load(
                session,
                workspace_id,
                actor,
                sections=("context",),
            )
            if need_tree
            else None
        )
        page_ids = (
            {row.target_id for row in rows if row.target_kind == "context_page"}
            | {v.page_id for v in self.versions.values()}
            | {
                op.target_id
                for ops in self.operations.values()
                for op in ops
                if op.target_type in {"context_page", "context_page_metadata"}
            }
        )
        if self.tree:
            page_ids.update(
                node.target_id
                for row in rows
                if (node := self.tree.nodes.get(row.target_id)) is not None
                and node.target_kind == "context_page"
            )
        self.pages = (
            {
                p.id: p
                for p in session.scalars(
                    select_in_workspace_by_ids(
                        ContextPage,
                        workspace_id=workspace_id,
                        resource_ids=page_ids - {None},
                    ).options(
                        selectinload(ContextPage.current_version),
                        selectinload(ContextPage.latest_draft_version),
                    )
                )
            }
            if page_ids - {None}
            else {}
        )
        decision_ids = {audit_uuid(p.get("decision_id")) for p in payloads} - {None}
        from backend.app.container import SINGLE_DATABASE_TOPOLOGY

        # Owner recovery reads control-plane Users. Only explicitly co-located
        # providers may add that lookup to this workspace-scoped activity query.
        include_owner_fallback = (
            getattr(session.info.get("database_provider"), "storage_topology", None)
            == SINGLE_DATABASE_TOPOLOGY
        )
        self.decisions = (
            {
                d.id: d
                for d in session.scalars(
                    select_in_workspace(
                        ReviewDecision,
                        workspace_id=workspace_id,
                        criteria=(
                            ReviewDecision.id.in_(decision_ids),
                            review_decision_audience_clause(
                                role,
                                principal,
                                include_owner_fallback=include_owner_fallback,
                            ),
                        ),
                    )
                )
            }
            if decision_ids
            else {}
        )
        self._change_visibility: dict[UUID, bool] = {}

    def page_visible(self, page_id: UUID) -> bool:
        return context_page_visible(
            self.pages.get(page_id),
            self.actor,
            workspace_id=self.workspace_id,
        )

    def change_visible(self, change_id: UUID) -> bool:
        if change_id not in self._change_visibility:
            ops = self.operations.get(change_id, [])
            self._change_visibility[change_id] = bool(ops) and all(
                self._operation_visible(op) for op in ops
            )
        return self._change_visibility[change_id]

    def _operation_visible(self, op: ChangeOperation) -> bool:
        if op.target_type in {"context_page", "context_page_metadata"}:
            if not self.page_visible(op.target_id):
                return False
            vid = operation_version(op)
            if (
                op.diff_ref
                and op.diff_ref.startswith("context_page_version:")
                and vid is None
            ):
                return False
            return not vid or self.version(vid, op.target_id) is not None
        return bool(
            op.target_type == "workspace_tree_node"
            and self.tree
            and self.tree.is_visible(op.target_id)
        )

    def page_for_event(
        self, event: AuditEvent, scoped_page: UUID | None
    ) -> UUID | None:
        if event.target_kind == "context_page":
            return event.target_id
        if event.target_kind == "workspace_tree_node" and self.tree:
            node = self.tree.nodes.get(event.target_id)
            return (
                node.target_id if node and node.target_kind == "context_page" else None
            )
        if event.target_kind == "review_item":
            review = self.reviews.get(event.target_id)
            if review:
                pages = {
                    op.target_id
                    for op in self.operations[review.change_set_id]
                    if op.target_type == "context_page"
                }
                if scoped_page in pages:
                    return scoped_page
                if len(pages) == 1:
                    return next(iter(pages))
        return None

    def visible(self, event: AuditEvent) -> bool:
        if event.target_kind == "context_page":
            return self.page_visible(event.target_id)
        if event.target_kind == "review_item":
            review = self.reviews.get(event.target_id)
            if (
                not review
                or review.id not in self.readable_reviews
                or not self.change_visible(review.change_set_id)
            ):
                return False
            if event.action == "review.decision":
                decision = self.decisions.get(
                    audit_uuid((event.payload or {}).get("decision_id"))
                )
                return bool(decision and decision.review_item_id == review.id)
            return True
        if event.target_kind == "workspace_tree_node":
            return bool(self.tree and self.tree.is_visible(event.target_id))
        return False

    def version(self, value, page_id: UUID) -> ActivityVersion | None:
        version = self.versions.get(audit_uuid(value))
        if (
            version is None
            or version.page_id != page_id
            or not context_version_is_visible(version, self.actor)
        ):
            return None
        return ActivityVersion(
            id=version.id,
            number=version.version_no,
            immutable=version.version_status
            in {
                "published_snapshot",
                "superseded_snapshot",
            },
        )

    def location(self, parent_id, snapshot) -> ActivityLocation:
        if parent_id is None:
            return ActivityLocation(provenance="root")
        node_id = audit_uuid(parent_id)
        path = []
        seen = set()
        while node_id is not None:
            if not self.tree or node_id in seen or not self.tree.is_visible(node_id):
                return ActivityLocation(provenance="unavailable")
            seen.add(node_id)
            node = self.tree.nodes.get(node_id)
            if node is None:
                return ActivityLocation(provenance="unavailable")
            path.append((node.id, node.title))
            node_id = node.parent_id
        path.reverse()
        # A stored snapshot may name ancestors no longer in today's path.
        # Reauthorize ALL of them, and require the actual referenced parent.
        if isinstance(snapshot, list) and snapshot:
            ids = [
                audit_uuid(entry.get("id"))
                for entry in snapshot
                if isinstance(entry, dict)
            ]
            titles = [
                audit_text(entry.get("title"))
                for entry in snapshot
                if isinstance(entry, dict)
            ]
            if (
                len(ids) == len(snapshot)
                and all(ids)
                and all(titles)
                and ids[-1] == audit_uuid(parent_id)
                and self.tree
                and all(self.tree.is_visible(i) for i in ids)
            ):
                return ActivityLocation(path=titles, provenance="recorded")
            return ActivityLocation(provenance="unavailable")
        return ActivityLocation(path=[title for _, title in path], provenance="current")

    def details(
        self, event: AuditEvent, page_id: UUID | None
    ) -> tuple[ActivityDetails | None, UUID | None]:
        if page_id is None or not self.page_visible(page_id) or not self.visible(event):
            return None, None
        payload = event.payload or {}
        own = bool(
            self.actor
            and (event.actor_kind, event.actor_id)
            == (self.actor.actor_kind, self.actor.actor_id)
        )
        # Another reader's private editing records never reach this projection:
        # the audit query drops them (activity_service._private_editing_visible).
        review = self.reviews.get(
            event.target_id
            if event.target_kind == "review_item"
            else audit_uuid(payload.get("review_item_id"))
        )
        change_id = (
            review.change_set_id if review else audit_uuid(payload.get("change_set_id"))
        )
        ops = self.operations.get(change_id, [])
        bound = (
            bool(ops)
            and any(
                op.target_type == "context_page" and op.target_id == page_id
                for op in ops
            )
            and self.change_visible(change_id)
        )
        if not bound:
            review = None
        elif review is None:
            candidates = [
                r for r in self.reviews.values() if r.change_set_id == change_id
            ]
            review = candidates[0] if len(candidates) == 1 else None
        hidden_review = bool(review and review.id not in self.readable_reviews)
        if hidden_review:
            review = None
        version_id = payload.get("version_id") or payload.get("draft_version_id")
        if event.action in {"page.publish", "page.unpublish"}:
            version_id = payload.get("exposed_version_id") or payload.get(
                "published_version_id"
            )
        if version_id is None and bound:
            ids = {operation_version(op) for op in ops if op.target_id == page_id} - {
                None
            }
            if len(ids) == 1:
                version_id = next(iter(ids))
        version = self.version(version_id, page_id)
        reason = audit_text(payload.get("reason"))
        if bound and reason is None:
            reason = audit_text(self.changes[change_id].reason)
        if reason and (
            reason in get_args(ActivityEventKey)
            or reason
            in {
                "approved through Context review",
                "author submitted for review",
                DEFAULT_TREE_MOVE_REASON,
            }
        ):
            reason = None
        decision_type = None
        if review and event.action == "review.decision":
            decision = self.decisions.get(audit_uuid(payload.get("decision_id")))
            if decision and decision.review_item_id == review.id:
                decision_type = (
                    decision.decision_type
                    if decision.decision_type
                    in {"approve", "reject", "request_changes"}
                    else None
                )
                reason = decision.decision_reason
        # A version-bearing event with an unreadable/missing binding cannot
        # disclose its free-text reason or revision, even on a public page.
        if version_id is not None and version is None:
            reason = None
        if hidden_review:
            reason = None
        revision = payload.get("editing_revision")
        base = self.version(
            payload.get("base_version_id") or payload.get("parent_version_id"),
            page_id,
        )
        if base and version and base.id == version.id:
            base = None
        return ActivityDetails(
            page_id=page_id,
            version=version,
            base_version=base,
            previous_published_version=self.version(
                payload.get("previous_published_version_id"), page_id
            ),
            restored_from_version=self.version(
                payload.get("restored_from_version_id"), page_id
            ),
            editing_revision=revision
            if own
            and isinstance(revision, int)
            and not isinstance(revision, bool)
            and version
            else None,
            reason=reason,
            decision_type=decision_type,
            rename=ActivityRename(
                before=audit_text(payload.get("previous_title")),
                after=audit_text(payload.get("title")),
            )
            if event.action == "tree.node.rename"
            else None,
            move=ActivityMove(
                before=(
                    self.location(
                        payload.get("previous_parent_id"),
                        payload.get("previous_parent_path"),
                    )
                    if "previous_parent_id" in payload
                    else ActivityLocation(provenance="unavailable")
                ),
                after=(
                    self.location(
                        payload.get("new_parent_id"), payload.get("new_parent_path")
                    )
                    if "new_parent_id" in payload
                    else ActivityLocation(provenance="unavailable")
                ),
            )
            if event.action == "tree.node.move"
            else None,
        ), review.id if review else None

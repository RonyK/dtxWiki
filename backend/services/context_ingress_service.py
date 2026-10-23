import re
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from backend.context_media.domain import (
    MarkdownMediaPolicyError,
    validate_markdown_media_policy,
)
from backend.context_media.ports import ContextMediaRepository
from backend.domain.input_policy import fit_generated_title, validate_text
from backend.domain.prepared_body import PreparedBody
from backend.models.ask import (
    AskSession,
    AskTurn,
    ContextIngressProposal,
    ProposalSourceItem,
)
from backend.models.change import ChangeOperation, ChangeSet
from backend.models.context import ContextPage, ContextPageVersion, PageProvenance
from backend.models.feedback import FeedbackItem
from backend.models.review import ReviewItem
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.repositories.context_page_repo import insert_version
from backend.repositories.workspace_scoped import get_in_workspace
from backend.schemas.context_ingress import (
    AskTurnPromoteRequest,
    ContextIngressAcceptRequest,
    ContextIngressAcceptResult,
    ContextIngressProposalDetail,
    ContextIngressProposalListData,
    ContextIngressRejectRequest,
    ProposalSourceRef,
)
from backend.schemas.input_policy import validate_request_inputs
from backend.services.audit_service import AuditService
from backend.services.body_preparer import BodyInput, BodyPreparer, WriteContext
from backend.services.db_write_intent import acquire_write_intent
from backend.services.page_service import refresh_page_links
from backend.services.read_errors import (
    ReadNotFoundError,
    ReadValidationError,
    RejectedByPolicyError,
)
from backend.services.review_risk_policy import (
    ReviewRiskResolver,
    review_risk_snapshot,
)
from backend.services.tree_read import TreeReadService
from backend.services.tree_service import ensure_primary_tree_placement
from backend.spi.types import AI_PROVENANCES
from backend.utils.uuid import parse_uuid_or_none


class ContextIngressService:
    def __init__(
        self,
        audit_service: AuditService | None = None,
        tree_read_service: TreeReadService | None = None,
        context_media_repository_factory: Callable[[Session], ContextMediaRepository]
        | None = None,
        body_preparer: BodyPreparer | None = None,
    ) -> None:
        self.audit_service = audit_service or AuditService()
        self.tree_read_service = tree_read_service or TreeReadService()
        self.risk_resolver = ReviewRiskResolver()
        self.body_preparer = body_preparer or BodyPreparer()
        self.context_media_repository_factory = context_media_repository_factory

    def promote_ask_turn(
        self,
        session: Session,
        workspace_id: UUID,
        ask_turn_id: UUID,
        request: AskTurnPromoteRequest,
        actor_kind: str,
        actor_id: str,
        actor: ActorContext | None = None,
    ) -> ContextIngressProposalDetail:
        validate_request_inputs(request)
        turn, ask_session = require_ask_turn(session, workspace_id, ask_turn_id)
        # H-01: promotion materializes the turn's answer into a proposal. An
        # actor who cannot see the turn's grounding evidence must not promote it,
        # exactly as the session read redacts it — otherwise a direct-ID promote
        # launders restricted evidence into a normal Context page.
        if not _proposal_evidence_visible(session, turn.id, actor, workspace_id):
            raise ReadNotFoundError("ask turn not found")
        parent = self._recommended_parent(
            session=session,
            workspace_id=workspace_id,
            requested_parent_id=request.parent_node_id,
            turn=turn,
        )
        body = request.body_markdown or turn.assistant_message or ""
        if not body.strip():
            raise RejectedByPolicyError("ask turn has no answer to promote")
        title = request.title or inferred_title(ask_session, turn)
        proposal = ContextIngressProposal(
            workspace_id=workspace_id,
            source_kind="ask_turn",
            source_ask_turn_id=turn.id,
            proposed_context_page_type=request.page_type,
            proposed_parent_tree_node_id=parent.id,
            proposed_title=title,
            proposed_save_mode=request.save_mode,
            placement_reason=(f"Ask turn {turn.turn_no} in {ask_session.title}"),
            placement_confidence=turn.placement_confidence or "medium",
            alternatives=[
                *request.alternatives,
                {"body_markdown": body, "source": "ask_turn"},
            ],
            status="proposed",
            created_by_actor_type=actor_kind,
            created_by_actor_id=parse_uuid_or_none(actor_id),
        )
        session.add(proposal)
        session.flush()
        turn.promotion_state = "proposed"
        self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="context_ingress.proposal.create",
            target_kind="context_ingress_proposal",
            target_id=proposal.id,
            payload={
                "ask_turn_id": str(turn.id),
                "save_mode": request.save_mode,
                "parent_node_id": str(parent.id),
            },
        )
        return self._detail(session, workspace_id, proposal)

    def list_proposals(
        self,
        session: Session,
        workspace_id: UUID,
        status: str | None = None,
        source_kind: str | None = None,
        limit: int = 50,
        actor: ActorContext | None = None,
    ) -> ContextIngressProposalListData:
        statement = select(ContextIngressProposal).where(
            ContextIngressProposal.workspace_id == workspace_id
        )
        if status is not None:
            statement = statement.where(ContextIngressProposal.status == status)
        if source_kind is not None:
            statement = statement.where(
                ContextIngressProposal.source_kind == source_kind
            )
        rows = session.scalars(
            statement.order_by(
                ContextIngressProposal.created_at.desc(),
                ContextIngressProposal.id.desc(),
            ).limit(limit)
        ).all()
        return ContextIngressProposalListData(
            proposals=[
                self._detail(session, workspace_id, proposal, actor)
                for proposal in rows
            ]
        )

    def get_proposal(
        self,
        session: Session,
        workspace_id: UUID,
        proposal_id: UUID,
        actor: ActorContext | None = None,
    ) -> ContextIngressProposalDetail:
        return self._detail(
            session,
            workspace_id,
            require_proposal(session, workspace_id, proposal_id),
            actor,
        )

    def accept_proposal(
        self,
        session: Session,
        workspace_id: UUID,
        proposal_id: UUID,
        request: ContextIngressAcceptRequest,
        actor_kind: str,
        actor_id: str,
        actor: ActorContext | None = None,
    ) -> ContextIngressAcceptResult:
        # SQLite: FOR UPDATE is a no-op and the first DML here is the change_set
        # flush far below, so without write intent two concurrent accepts both
        # pass the "proposed" guard and materialize the proposal twice.
        # PostgreSQL keeps using the narrower row lock below.
        validate_request_inputs(request)
        acquire_write_intent(session)
        if (
            request.final_save_mode != "feedback_only"
            and self.context_media_repository_factory is not None
        ):
            # The accepted proposal may update an existing Context page. Enter
            # the shared media intent before that Page row is locked so the
            # later Usage replacement cannot invert upload/reconciliation.
            self.context_media_repository_factory(session).lock_workspace_for_usage(
                workspace_id
            )
        proposal = require_proposal_for_update(session, workspace_id, proposal_id)
        if proposal.status != "proposed":
            raise RejectedByPolicyError("proposal is not proposed")
        # H-01: accepting materializes an ask_turn proposal (its body/placement
        # derive from the turn's answer). Fail closed unless the actor can see
        # the source turn's evidence. The source_ask_turn_id FK is nulled when
        # the turn is deleted, so a null source on an ask_turn proposal is an
        # UNVERIFIABLE dangling answer and must also be refused — otherwise a
        # direct-ID accept launders a restricted-grounded answer into a page.
        if proposal.source_kind == "ask_turn" and not _proposal_evidence_visible(
            session, proposal.source_ask_turn_id, actor, proposal.workspace_id
        ):
            raise ReadNotFoundError("proposal not found")
        parent = require_context_parent(
            session=session,
            workspace_id=workspace_id,
            parent_id=request.final_parent_node_id,
        )
        body = self._body_for_accept(session, workspace_id, proposal, request)
        if request.final_save_mode == "feedback_only":
            validate_text(body, field="body", min_length=1, max_length=10000)
        try:
            validate_markdown_media_policy(body)
        except MarkdownMediaPolicyError as exc:
            raise ReadValidationError(
                str(exc),
                error_code=exc.error_code,
            ) from exc
        review_reason = review_required_reason(proposal, request)
        review_required = request.review_required
        if actor is not None and actor.provenance in AI_PROVENANCES:
            review_required = True
            review_reason = (
                review_reason or "agent-authored ingress requires human review"
            )
        elif review_required is None:
            review_required = review_reason is not None
        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            raise ReadNotFoundError("workspace not found")
        risk_resolution = self.risk_resolver.resolve(
            workspace=workspace,
            event=(
                "context_ingress_review_required"
                if review_required
                else "context_ingress_auto"
            ),
            signals={
                "decision_page_type": "page_type_decision" in (review_reason or ""),
                "low_placement_confidence": "low_confidence" in (review_reason or ""),
                "review_required": review_required,
                "caller_needs_review": request.review_required is True,
            },
            actor_provenance=(actor.provenance if actor is not None else "internal"),
        )
        change_set = create_ingress_change_set(
            session=session,
            workspace_id=workspace_id,
            reason=f"accept context ingress proposal {proposal.id}",
            actor_kind=actor_kind,
            actor_id=actor_id,
            risk_level=risk_resolution.risk_level,
            review_required=risk_resolution.review_required,
        )
        page, version, tree_node, feedback = self._apply_accept(
            session=session,
            workspace_id=workspace_id,
            proposal=proposal,
            request=request,
            parent=parent,
            body=body,
            change_set=change_set,
            actor_kind=actor_kind,
            actor_id=actor_id,
            actor_provenance=actor.provenance if actor is not None else "internal",
        )
        review = None
        if risk_resolution.review_required:
            review = ReviewItem(
                workspace_id=workspace_id,
                change_set_id=change_set.id,
                review_type="context_ingress_accept",
                status="pending",
                risk_level=change_set.risk_level,
                **review_risk_snapshot(risk_resolution),
                decision_summary=review_reason or risk_resolution.summary,
            )
            session.add(review)
        proposal.status = "accepted"
        proposal.accepted_change_set_id = change_set.id
        proposal.accepted_at = datetime.now(UTC)
        turn = self._source_turn(session, workspace_id, proposal)
        if turn is not None:
            turn.promotion_state = "accepted"
            if page is not None:
                turn.promoted_context_page_id = page.id
        audit = self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="context_ingress.proposal.accept",
            target_kind="context_ingress_proposal",
            target_id=proposal.id,
            payload={
                "change_set_id": str(change_set.id),
                "context_page_id": str(page.id) if page else None,
                "context_page_version_id": str(version.id) if version else None,
                "tree_node_id": str(tree_node.id) if tree_node else None,
                "feedback_item_id": str(feedback.id) if feedback else None,
                "review_required": risk_resolution.review_required,
            },
        )
        session.flush()
        return ContextIngressAcceptResult(
            proposal_id=proposal.id,
            change_set_id=change_set.id,
            context_page_id=page.id if page else None,
            context_page_version_id=version.id if version else None,
            tree_node_id=tree_node.id if tree_node else None,
            feedback_item_id=feedback.id if feedback else None,
            review_item_id=review.id if review else None,
            review_required_reason=review_reason,
            audit_event_id=audit.id,
        )

    def reject_proposal(
        self,
        session: Session,
        workspace_id: UUID,
        proposal_id: UUID,
        request: ContextIngressRejectRequest,
        actor_kind: str,
        actor_id: str,
        actor: ActorContext | None = None,
    ) -> ContextIngressProposalDetail:
        # SQLite: see accept_proposal. Without write intent a reject can race an
        # accept, recording the rejection while the page is materialized anyway.
        validate_request_inputs(request)
        acquire_write_intent(session)
        proposal = require_proposal_for_update(session, workspace_id, proposal_id)
        if proposal.status != "proposed":
            raise RejectedByPolicyError("proposal is not proposed")
        proposal.status = "rejected"
        turn = self._source_turn(session, workspace_id, proposal)
        if turn is not None:
            turn.promotion_state = "rejected"
        self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="context_ingress.proposal.reject",
            target_kind="context_ingress_proposal",
            target_id=proposal.id,
            payload={"reason": request.reason},
        )
        session.flush()
        return self._detail(session, workspace_id, proposal, actor)

    def _apply_accept(
        self,
        session: Session,
        workspace_id: UUID,
        proposal: ContextIngressProposal,
        request: ContextIngressAcceptRequest,
        parent: WorkspaceTreeNode,
        body: str,
        change_set: ChangeSet,
        actor_kind: str,
        actor_id: str,
        actor_provenance: str,
    ) -> tuple[
        ContextPage | None,
        ContextPageVersion | None,
        WorkspaceTreeNode | None,
        FeedbackItem | None,
    ]:
        if request.final_save_mode == "feedback_only":
            feedback = FeedbackItem(
                workspace_id=workspace_id,
                target_type=proposal.source_kind,
                target_id=proposal.source_ask_turn_id or proposal.id,
                body=body,
                severity="minor",
                status="open",
                created_by=parse_uuid_or_none(actor_id),
            )
            session.add(feedback)
            session.flush()
            session.add(
                ChangeOperation(
                    workspace_id=change_set.workspace_id,
                    change_set_id=change_set.id,
                    target_type="feedback",
                    target_id=feedback.id,
                    operation="context_ingress_feedback",
                    diff_summary=f"feedback_only: {request.final_title}",
                    diff_ref=f"context_ingress_proposal:{proposal.id}",
                )
            )
            return None, None, None, feedback

        if request.final_save_mode in {"append_section", "update_draft"}:
            if request.target_page_id is None:
                raise RejectedByPolicyError("target_page_id is required")
            page = require_context_page_for_update(
                session,
                workspace_id,
                request.target_page_id,
            )
            latest = latest_context_version(session, workspace_id, page)
            base_body = latest.body_md if latest else ""
            body = (
                f"{base_body.rstrip()}\n\n{body.strip()}"
                if request.final_save_mode == "append_section"
                else body
            )
            tree_node = None
        else:
            page = insert_ingress_page_with_unique_slug(
                session=session,
                workspace_id=workspace_id,
                title=request.final_title,
                page_type=(
                    request.final_page_type or proposal.proposed_context_page_type
                ),
                parent_id=parent.id,
                placement_reason=proposal.placement_reason,
                placement_confidence=proposal.placement_confidence,
                actor_kind=actor_kind,
                actor_id=actor_id,
            )
            if proposal.source_ask_turn_id is not None:
                session.add(
                    PageProvenance(
                        workspace_id=workspace_id,
                        page_id=page.id,
                        source_kind="ask_turn",
                        source_id=proposal.source_ask_turn_id,
                    )
                )
            # B-8: route the primary placement through the canonical
            # upsert so ingress gains the revive semantics (an archived
            # placement for this page is revived, never duplicated).
            tree_node = ensure_primary_tree_placement(
                session=session,
                workspace_id=workspace_id,
                target_kind="context_page",
                target_id=page.id,
                title=page.title,
                section="context",
                parent_id=parent.id,
                default_parent_slug="inbox",
                actor_kind=actor_kind,
                actor_id=actor_id,
                can_have_children=True,
                move_existing=True,
            )
        prepared = self.body_preparer.prepare(
            BodyInput(body),
            WriteContext(
                workspace=session.get(Workspace, workspace_id),
                actor_kind=actor_kind,
                actor_id=actor_id,
                actor_provenance=actor_provenance,
                page=page,
                session=session,
            ),
        )
        version = insert_context_version(
            session=session,
            page=page,
            body=prepared,
            change_set_id=change_set.id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            context_media_repository=(
                self.context_media_repository_factory(session)
                if self.context_media_repository_factory is not None
                else None
            ),
        )
        page.current_version_id = page.current_version_id or version.id
        page.latest_draft_version_id = version.id
        page.page_type = request.final_page_type or page.page_type
        refresh_page_links(session, page=page, version=version)
        session.add(
            ChangeOperation(
                workspace_id=change_set.workspace_id,
                change_set_id=change_set.id,
                target_type="context_page",
                target_id=page.id,
                operation="context_ingress_accept",
                diff_summary=f"{request.final_save_mode}: {request.final_title}",
                diff_ref=f"context_page_version:{version.id}",
            )
        )
        if tree_node is not None:
            session.add(
                ChangeOperation(
                    workspace_id=change_set.workspace_id,
                    change_set_id=change_set.id,
                    target_type="workspace_tree_node",
                    target_id=tree_node.id,
                    operation="tree_place",
                    diff_summary=f"placed under {parent.title}",
                )
            )
        return page, version, tree_node, None

    def _recommended_parent(
        self,
        session: Session,
        workspace_id: UUID,
        requested_parent_id: UUID | None,
        turn: AskTurn,
    ) -> WorkspaceTreeNode:
        if requested_parent_id is not None:
            return require_context_parent(session, workspace_id, requested_parent_id)
        if turn.recommended_placement_tree_node_id is not None:
            return require_context_parent(
                session,
                workspace_id,
                turn.recommended_placement_tree_node_id,
            )
        return default_ask_capture_parent(session, workspace_id)

    def _body_for_accept(
        self,
        session: Session,
        workspace_id: UUID,
        proposal: ContextIngressProposal,
        request: ContextIngressAcceptRequest,
    ) -> str:
        if request.body_markdown:
            return request.body_markdown
        turn = self._source_turn(session, workspace_id, proposal)
        if turn is not None and turn.assistant_message:
            return turn.assistant_message
        for entry in proposal.alternatives:
            body = entry.get("body_markdown") if isinstance(entry, dict) else None
            if isinstance(body, str) and body.strip():
                return body
        raise RejectedByPolicyError("proposal has no body to accept")

    @staticmethod
    def _source_turn(
        session: Session,
        workspace_id: UUID,
        proposal: ContextIngressProposal,
    ) -> AskTurn | None:
        if proposal.source_kind != "ask_turn" or proposal.source_ask_turn_id is None:
            return None
        return get_in_workspace(
            session,
            AskTurn,
            workspace_id=workspace_id,
            resource_id=proposal.source_ask_turn_id,
        )

    def _detail(
        self,
        session: Session,
        workspace_id: UUID,
        proposal: ContextIngressProposal,
        actor: ActorContext | None = None,
    ) -> ContextIngressProposalDetail:
        parent = get_in_workspace(
            session,
            WorkspaceTreeNode,
            workspace_id=workspace_id,
            resource_id=proposal.proposed_parent_tree_node_id,
        )
        breadcrumb = (
            self.tree_read_service.path_for_node(session=session, node=parent)
            if parent is not None
            else []
        )
        source_item_ids = session.scalars(
            select(ProposalSourceItem.library_item_id)
            .where(ProposalSourceItem.proposal_id == proposal.id)
            .order_by(ProposalSourceItem.rank)
        ).all()
        # H-01: an ask_turn proposal clones the turn's answer into `alternatives`.
        # Re-validate the source turn's evidence against the current actor; if it
        # is no longer visible — OR the source turn is gone (source_ask_turn_id
        # nulled on delete), leaving the clone unverifiable — drop the cloned
        # answer body so a lower-privileged reader cannot recover restricted
        # evidence via the proposal.
        alternatives = proposal.alternatives
        if proposal.source_kind == "ask_turn" and not _proposal_evidence_visible(
            session, proposal.source_ask_turn_id, actor, proposal.workspace_id
        ):
            alternatives = [
                alt
                for alt in (proposal.alternatives or [])
                if not (isinstance(alt, dict) and alt.get("source") == "ask_turn")
            ]
        return ContextIngressProposalDetail(
            id=proposal.id,
            workspace_id=proposal.workspace_id,
            source_kind=proposal.source_kind,
            source_ref=ProposalSourceRef(
                kind=proposal.source_kind,
                id=proposal.source_ask_turn_id,
            ),
            source_item_ids=list(source_item_ids),
            proposed_context_page_type=proposal.proposed_context_page_type,
            proposed_parent_tree_node_id=proposal.proposed_parent_tree_node_id,
            proposed_parent_breadcrumb=breadcrumb,
            proposed_title=proposal.proposed_title,
            proposed_save_mode=proposal.proposed_save_mode,
            placement_reason=proposal.placement_reason,
            placement_confidence=proposal.placement_confidence,
            alternatives=alternatives,
            status=proposal.status,
            accepted_change_set_id=proposal.accepted_change_set_id,
            created_by_actor_type=proposal.created_by_actor_type,
            created_by_actor_id=proposal.created_by_actor_id,
            created_at=proposal.created_at,
            updated_at=proposal.updated_at,
            accepted_at=proposal.accepted_at,
        )


def _proposal_evidence_visible(
    session: Session,
    source_ask_turn_id: UUID | None,
    actor: ActorContext | None,
    expected_workspace_id: UUID,
) -> bool:
    # Imported lazily to avoid a module-load cycle with ask_service.
    from backend.services.ask_service import (
        _turn_citations_by_turn,
        ask_turn_evidence_visible,
    )

    # Fail CLOSED when the source turn is gone (source_ask_turn_id nulled on
    # delete, or the id points at a deleted turn): the cloned answer can no
    # longer be re-authorized, and a deleted turn has no citations to check —
    # which would otherwise read as "nothing restricted".
    if source_ask_turn_id is None:
        return False
    turn = get_in_workspace(
        session,
        AskTurn,
        workspace_id=expected_workspace_id,
        resource_id=source_ask_turn_id,
    )
    if turn is None:
        return False
    ask_session = get_in_workspace(
        session,
        AskSession,
        workspace_id=expected_workspace_id,
        resource_id=turn.ask_session_id,
    )
    # The proposal and its source turn have independent workspace ancestry: a
    # malformed/migrated proposal in workspace A could reference a turn in
    # workspace B and launder B's (possibly-empty-cited) answer to an A-only
    # actor. Require the turn's workspace to match the proposal's (Codex final).
    if ask_session is None or ask_session.workspace_id != expected_workspace_id:
        return False
    cited_page_ids, cited_artifact_ids = _turn_citations_by_turn(
        session, [source_ask_turn_id]
    ).get(source_ask_turn_id, ([], []))
    return ask_turn_evidence_visible(
        session, cited_page_ids, cited_artifact_ids, actor, ask_session.workspace_id
    )


def require_ask_turn(
    session: Session,
    workspace_id: UUID,
    ask_turn_id: UUID,
) -> tuple[AskTurn, AskSession]:
    turn = get_in_workspace(
        session,
        AskTurn,
        workspace_id=workspace_id,
        resource_id=ask_turn_id,
    )
    ask_session = (
        get_in_workspace(
            session,
            AskSession,
            workspace_id=workspace_id,
            resource_id=turn.ask_session_id,
        )
        if turn
        else None
    )
    if turn is None or ask_session is None or ask_session.workspace_id != workspace_id:
        raise ReadNotFoundError("ask turn not found")
    return turn, ask_session


def require_proposal(
    session: Session,
    workspace_id: UUID,
    proposal_id: UUID,
) -> ContextIngressProposal:
    proposal = get_in_workspace(
        session,
        ContextIngressProposal,
        workspace_id=workspace_id,
        resource_id=proposal_id,
    )
    if proposal is None:
        raise ReadNotFoundError("context ingress proposal not found")
    return proposal


def context_ingress_proposal_for_update_statement(
    workspace_id: UUID,
    proposal_id: UUID,
):
    return (
        select(ContextIngressProposal)
        .where(
            ContextIngressProposal.workspace_id == workspace_id,
            ContextIngressProposal.id == proposal_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def require_proposal_for_update(
    session: Session,
    workspace_id: UUID,
    proposal_id: UUID,
) -> ContextIngressProposal:
    proposal = session.scalar(
        context_ingress_proposal_for_update_statement(workspace_id, proposal_id)
    )
    if proposal is None:
        raise ReadNotFoundError("context ingress proposal not found")
    return proposal


def require_context_parent(
    session: Session,
    workspace_id: UUID,
    parent_id: UUID,
) -> WorkspaceTreeNode:
    parent = get_in_workspace(
        session,
        WorkspaceTreeNode,
        workspace_id=workspace_id,
        resource_id=parent_id,
    )
    if parent is None:
        raise ReadNotFoundError("context tree parent not found")
    if parent.section != "context" or not parent.can_have_children:
        raise RejectedByPolicyError("invalid context tree parent")
    return parent


def require_context_page(
    session: Session,
    workspace_id: UUID,
    page_id: UUID,
) -> ContextPage:
    page = get_in_workspace(
        session,
        ContextPage,
        workspace_id=workspace_id,
        resource_id=page_id,
    )
    if page is None:
        raise ReadNotFoundError("context page not found")
    return page


def context_page_for_ingress_update_statement(
    workspace_id: UUID,
    page_id: UUID,
):
    return (
        select(ContextPage)
        .where(
            ContextPage.workspace_id == workspace_id,
            ContextPage.id == page_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def require_context_page_for_update(
    session: Session,
    workspace_id: UUID,
    page_id: UUID,
) -> ContextPage:
    page = session.scalar(
        context_page_for_ingress_update_statement(workspace_id, page_id)
    )
    if page is None:
        raise ReadNotFoundError("context page not found")
    return page


def default_ask_capture_parent(
    session: Session,
    workspace_id: UUID,
) -> WorkspaceTreeNode:
    parent = session.scalar(
        select(WorkspaceTreeNode).where(
            WorkspaceTreeNode.workspace_id == workspace_id,
            WorkspaceTreeNode.section == "context",
            WorkspaceTreeNode.slug == "ask_captures",
            WorkspaceTreeNode.visibility_status == "visible",
        )
    )
    if parent is not None:
        return parent
    parent = session.scalar(
        select(WorkspaceTreeNode).where(
            WorkspaceTreeNode.workspace_id == workspace_id,
            WorkspaceTreeNode.section == "context",
            WorkspaceTreeNode.slug == "context",
            WorkspaceTreeNode.visibility_status == "visible",
        )
    )
    if parent is None:
        raise ReadNotFoundError("context root not found")
    return parent


def create_ingress_change_set(
    session: Session,
    workspace_id: UUID,
    reason: str,
    actor_kind: str,
    actor_id: str,
    risk_level: str,
    review_required: bool = False,
) -> ChangeSet:
    if reason is not None:
        validate_text(reason, field="reason", max_length=2000)
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        raise ReadNotFoundError("workspace not found")
    change_set = ChangeSet(
        workspace_id=workspace_id,
        reason=reason,
        trigger_type="context_ingress_accept",
        actor_type=actor_kind,
        actor_id=parse_uuid_or_none(actor_id),
        status="needs_review" if review_required else "draft_created",
        risk_level=risk_level,
    )
    session.add(change_set)
    session.flush()
    return change_set


def insert_context_version(
    session: Session,
    page: ContextPage,
    body: PreparedBody,
    change_set_id: UUID,
    actor_kind: str,
    actor_id: str,
    context_media_repository: ContextMediaRepository | None = None,
) -> ContextPageVersion:
    return insert_version(
        session,
        page_id=page.id,
        parent_version_id=page.latest_draft_version_id or page.current_version_id,
        body=body,
        actor_kind=actor_kind,
        actor_id=actor_id,
        version_status="draft",
        change_set_id=change_set_id,
        context_media_repository=context_media_repository,
    )


def latest_context_version(
    session: Session,
    workspace_id: UUID,
    page: ContextPage,
) -> ContextPageVersion | None:
    version_id = page.latest_draft_version_id or page.current_version_id
    if version_id is None:
        return None
    return get_in_workspace(
        session,
        ContextPageVersion,
        workspace_id=workspace_id,
        resource_id=version_id,
    )


def unique_context_slug(session: Session, workspace_id: UUID, title: str) -> str:
    base = slugify(title) or "context-page"
    slug = base
    index = 2
    while session.scalar(
        select(ContextPage.id).where(
            ContextPage.workspace_id == workspace_id,
            ContextPage.slug == slug,
        )
    ):
        slug = f"{base}-{index}"
        index += 1
    return slug


def insert_ingress_page_with_unique_slug(
    session: Session,
    workspace_id: UUID,
    title: str,
    page_type: str,
    parent_id: UUID,
    placement_reason: str | None,
    placement_confidence: str,
    actor_kind: str,
    actor_id: str,
) -> ContextPage:
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        insert_statement = sqlite_insert(ContextPage)
    elif dialect == "postgresql":
        insert_statement = postgresql_insert(ContextPage)
    else:
        raise RuntimeError(f"unsupported context-page dialect: {dialect}")
    for _attempt in range(100):
        inserted_id = session.scalar(
            insert_statement.values(
                workspace_id=workspace_id,
                slug=unique_context_slug(session, workspace_id, title),
                title=title,
                page_type=page_type,
                lifecycle_status="draft",
                placement_parent_node_id=parent_id,
                placement_reason=placement_reason,
                placement_confidence=placement_confidence,
                created_by_actor_type=actor_kind,
                created_by_actor_id=parse_uuid_or_none(actor_id),
            )
            .on_conflict_do_nothing(index_elements=["workspace_id", "slug"])
            .returning(ContextPage.id)
        )
        if inserted_id is None:
            continue
        page = get_in_workspace(
            session,
            ContextPage,
            workspace_id=workspace_id,
            resource_id=inserted_id,
        )
        if page is None:
            raise RuntimeError("inserted context page is not visible")
        return page
    raise RuntimeError("unable to allocate a unique context page slug")


def slugify(value: str) -> str:
    return re.sub(r"(^-+|-+$)", "", re.sub(r"[^a-z0-9]+", "-", value.lower()))


def inferred_title(ask_session: AskSession, turn: AskTurn) -> str:
    return fit_generated_title(
        turn.user_message or ask_session.title,
        max_length=60,
        fallback=f"{ask_session.title} turn {turn.turn_no}",
    )


def review_required_reason(
    proposal: ContextIngressProposal,
    request: ContextIngressAcceptRequest,
) -> str | None:
    reasons = []
    if proposal.placement_confidence == "low":
        reasons.append("low_confidence")
    if proposal.proposed_context_page_type == "decision" or (
        request.final_page_type == "decision"
    ):
        reasons.append("page_type_decision")
    return ",".join(reasons) if reasons else None

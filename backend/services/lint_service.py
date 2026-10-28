from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from backend.models.context import ContextPage
from backend.models.lint import LintFinding, LintRun
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.policy.trust_boundaries import actor_can_read_drafts
from backend.repositories.context_visibility import (
    visible_context_version_expression,
    visible_context_version_id,
)
from backend.schemas.input_policy import validate_request_inputs
from backend.schemas.lint import (
    LintFindingBatchDecisionFailure,
    LintFindingBatchDecisionRequest,
    LintFindingDecisionRequest,
    LintFindingListData,
    LintFindingRead,
    LintRuleInfo,
    LintRuleListData,
    LintRunCreateRequest,
    LintRunRead,
)
from backend.services.audit_service import AuditService
from backend.services.delegated_tasks import produce_folder_summary_tasks
from backend.services.job_queue import JobQueueService
from backend.services.lint import (
    LintContext,
    LintModule,
    LintPolicy,
    LintRegistry,
    LintStage,
)
from backend.services.lint.scopes import findings_for_target
from backend.services.list_pagination import (
    _decode_keyset_cursor,
    _encode_keyset_cursor,
)
from backend.services.read_errors import (
    ReadNotFoundError,
    ReadServiceError,
    ReadValidationError,
)

STATUS_BY_DECISION = {
    "acknowledge": "acknowledged",
    "dismiss": "dismissed",
    "fix": "fixed",
}

# I11: rules that were permanently REMOVED from the registry. Their open findings
# can never reproduce, yet they fall through both the list_findings visibility
# filter and the enabled-rule reconciliation in run_lint — so on an upgraded DB
# they would linger on the Lint screen forever. run_lint tombstone-resolves them.
# (A fresh DB never has these rows; this only matters for DBs that ran the old
# rule. Kept as a runtime path because the repo enforces a single baseline
# migration — see tests/packaging/test_migration_chain.py.)
# `maintenance.near_duplicate_verdict` joined the set when Q-5 moved to enriching
# the originating `kb.semantic_near_duplicate` finding instead of minting its own.
_REMOVED_RULE_IDS = frozenset({"md.frontmatter", "maintenance.near_duplicate_verdict"})


class LintService:
    def __init__(
        self,
        audit_service: AuditService | None = None,
        job_queue: JobQueueService | None = None,
        *,
        lint_registry: LintRegistry | None = None,
    ) -> None:
        self.lint_registry = (
            lint_registry if lint_registry is not None else LintRegistry.builtin()
        )
        self.audit_service = audit_service or AuditService()
        self.job_queue = job_queue or JobQueueService()

    def list_findings(
        self,
        session: Session,
        workspace_id: UUID,
        severity: str | None = None,
        rule: str | None = None,
        status: str | None = None,
        layer: str | None = None,
        finding_id: UUID | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        page: int | None = None,
        page_size: int | None = None,
        actor: ActorContext | None = None,
    ) -> LintFindingListData:
        # M11/DEC-4: hide findings of rules the operator explicitly DISABLED
        # (registered + toggled off); rows are preserved, not deleted. Findings of
        # enabled rules — and of unregistered/orphan rule_ids (e.g. a removed or
        # legacy rule) — still show, so disabling never conflates with "fixed" and
        # no historical finding silently vanishes. This is a workspace policy gate,
        # not a list filter: a finding_id deep link bypasses the caller's
        # severity/layer/status filters but stays subject to it.
        workspace = session.get(Workspace, workspace_id)
        policy = LintPolicy(workspace)
        enabled_ids = {rule.rule_id for rule in self.lint_registry.enabled(policy)}
        disabled_ids = [
            rule.rule_id
            for rule in self.lint_registry.rules()
            if getattr(rule, "runtime_checkable", True)
            if rule.rule_id not in enabled_ids
        ]
        base = select(LintFinding).where(LintFinding.workspace_id == workspace_id)
        if disabled_ids:
            base = base.where(LintFinding.rule_id.not_in(disabled_ids))
        # M-02: a finding on a target the actor cannot read — a draft-only
        # Context page, a sensitive/draft tree node, or a deleted target — must
        # not surface its title, UUID, slug, or suggested action. Exclude those
        # findings BEFORE the count/pagination so totals and cursors stay
        # consistent, and fail CLOSED on a dangling (deleted) target.
        if actor is not None:
            # Context-page findings: keep only pages the actor can see. A draft
            # and a deleted page both fall out (a deleted page is not in the set).
            visible_page_ids = (
                select(ContextPage.id)
                .where(ContextPage.workspace_id == workspace_id)
                .where(visible_context_version_expression(actor).is_not(None))
            )
            base = base.where(
                (LintFinding.target_type != "context_page")
                | LintFinding.target_id.in_(visible_page_ids)
            )
        if finding_id is not None:
            base = base.where(LintFinding.id == finding_id)
        else:
            if severity is not None:
                base = base.where(LintFinding.severity == severity)
            if rule is not None:
                base = base.where(LintFinding.rule_id == rule)
            if status is not None:
                if status == "resolved":
                    base = base.where(LintFinding.status.in_(("dismissed", "fixed")))
                elif status == "open":
                    base = base.where(LintFinding.status.in_(("open", "acknowledged")))
                else:
                    base = base.where(LintFinding.status == status)
            if layer is not None:
                # Layer is a property of the producing rule, not the finding, so
                # map it to the rule_ids registered under that layer.
                layer_rule_ids = [
                    r.rule_id for r in self.lint_registry.rules() if r.layer == layer
                ]
                base = base.where(LintFinding.rule_id.in_(layer_rule_ids))
        if actor is not None:
            # Tree-node findings: node visibility is projection-based, so check
            # each target node against the SAME projection the tree GET uses
            # (covers sensitive-agent and draft-context nodes; a missing node
            # fails closed). Scope this to the ALREADY-filtered set — so a
            # finding_id deep-link projects one node, not the whole workspace —
            # and load ONE projection for the batch rather than per node.
            filtered = base.subquery()
            tree_node_ids = session.scalars(
                select(filtered.c.target_id)
                .where(filtered.c.target_type == "workspace_tree_node")
                .where(filtered.c.target_id.is_not(None))
                .distinct()
            ).all()
            if tree_node_ids:
                from backend.services.tree_read import TreeReadService

                nodes = session.scalars(
                    select(WorkspaceTreeNode).where(
                        WorkspaceTreeNode.workspace_id == workspace_id,
                        WorkspaceTreeNode.id.in_(tree_node_ids),
                    )
                ).all()
                visible_ids = TreeReadService.visible_node_ids(session, nodes, actor)
                hidden_node_ids = [
                    node_id for node_id in tree_node_ids if node_id not in visible_ids
                ]
                if hidden_node_ids:
                    base = base.where(
                        ~(
                            (LintFinding.target_type == "workspace_tree_node")
                            & LintFinding.target_id.in_(hidden_node_ids)
                        )
                    )
        if actor is not None and not actor_can_read_drafts(actor):
            base = self._exclude_draft_only_findings(session, base)
        numeric_pagination = page is not None or page_size is not None
        if numeric_pagination and (limit is not None or cursor is not None):
            raise ValueError("page/page_size cannot be combined with limit/cursor")
        effective_page = page or 1
        effective_page_size = page_size or 20
        effective_limit = limit or 50

        total = session.scalar(select(func.count()).select_from(base.subquery())) or 0
        stmt = base.order_by(
            LintFinding.created_at.desc(),
            LintFinding.id.desc(),
        )
        if numeric_pagination:
            page_count = (
                (total + effective_page_size - 1) // effective_page_size if total else 0
            )
            finding_rows = session.scalars(
                stmt.offset((effective_page - 1) * effective_page_size).limit(
                    effective_page_size
                )
            ).all()
            has_previous = effective_page > 1 and page_count > 0
            has_next = effective_page < page_count
            has_more = has_next
            next_cursor = None
        else:
            page_count = None
            has_previous = None
            has_next = None
            decoded = _decode_keyset_cursor(cursor)
            if decoded is not None:
                cur_ts, cur_id = decoded
                stmt = stmt.where(
                    tuple_(LintFinding.created_at, LintFinding.id) < (cur_ts, cur_id)
                )
            fetched = session.scalars(stmt.limit(effective_limit + 1)).all()
            finding_rows = fetched[:effective_limit]
            has_more = len(fetched) > effective_limit
            next_cursor = (
                _encode_keyset_cursor(
                    finding_rows[-1].created_at,
                    finding_rows[-1].id,
                )
                if has_more and finding_rows
                else None
            )
        target_titles = self._target_titles(session, finding_rows)
        return LintFindingListData(
            findings=[
                self._finding_read(
                    finding,
                    target_title=target_titles.get(
                        (finding.target_type, finding.target_id)
                    ),
                )
                for finding in finding_rows
            ],
            next_cursor=next_cursor,
            total=total,
            has_more=has_more,
            page=effective_page if numeric_pagination else None,
            page_size=effective_page_size if numeric_pagination else None,
            page_count=page_count,
            has_previous=has_previous,
            has_next=has_next,
        )

    def run_lint(
        self,
        session: Session,
        workspace_id: UUID,
        request: LintRunCreateRequest,
        actor_kind: str,
        actor_id: str,
        *,
        policy: dict[str, bool] | None = None,
        rules: list | None = None,
        run: LintRun | None = None,
    ) -> LintRunRead:
        """Execute the enabled lint rules over the scope's targets and persist a
        run + its findings. ``policy`` is the workspace ``lint`` toggle map
        ({rule_id: bool}); ``rules`` overrides the registry for testing."""
        validate_request_inputs(request)
        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            raise ReadNotFoundError("workspace not found")
        # Rule caches are run-scoped even when callers reuse one DB session.
        session.info.pop("_lint_nav_reachable", None)
        session.info.pop("_lint_term_collision_map", None)
        now = datetime.now(UTC)
        lint_run = run or LintRun(
            workspace_id=workspace_id,
            lint_scope={"kind": request.lint_scope},
            trigger_type=request.trigger_type,
            status="queued",
            finding_count_by_severity={},
        )
        if run is None:
            session.add(lint_run)
            session.flush()
        elif lint_run.workspace_id != workspace_id:
            raise ValueError("lint run does not belong to this workspace")
        elif lint_run.status == "completed":
            return self._run_read(lint_run)
        elif lint_run.status != "queued":
            raise ValueError("lint run is not queued for this workspace")
        lint_run.status = "running"
        lint_run.started_at = now
        lint_run.completed_at = None
        lint_scope = dict(lint_run.lint_scope or {"kind": request.lint_scope})
        registry = (
            self.lint_registry
            if rules is None
            else LintRegistry((LintModule("run", 0, tuple(rules)),))
        )
        targets = self._resolve_targets(session, workspace_id, lint_scope)
        linted_target_ids = {target_id for _, _, target_id in targets}

        # M3 (current-state model): index existing findings of the ENABLED rules
        # by (target_id, fingerprint), most-recent wins. Disabled-rule findings
        # are deliberately left untouched (DEC-4); only rules that run here are
        # reconciled, so a clean re-run resolves what no longer reproduces.
        enabled_ids: set[str] = set()
        for target_type in {tt for tt, _, _ in targets}:
            enabled_ids |= {
                r.rule_id
                for r in registry.rules_for(
                    LintStage.RUN, policy, target_type=target_type
                )
            }
        existing = (
            list(
                session.scalars(
                    select(LintFinding)
                    .where(
                        LintFinding.workspace_id == workspace_id,
                        LintFinding.rule_id.in_(enabled_ids),
                        LintFinding.status.in_(("open", "acknowledged", "dismissed")),
                    )
                    .order_by(LintFinding.created_at)
                ).all()
            )
            if enabled_ids
            else []
        )
        by_key: dict[tuple[UUID | None, str], LintFinding] = {
            (finding.target_id, finding.fingerprint): finding for finding in existing
        }

        counts: dict[str, int] = {}
        produced_keys: set[tuple[UUID | None, str]] = set()
        for target_type, target, target_id in targets:
            ctx = LintContext(session, workspace_id, page=target)
            for rule in registry.rules_for(
                LintStage.RUN, policy, target_type=target_type
            ):
                for draft in findings_for_target(
                    rule, target, ctx
                ):
                    tid = draft.target_id if draft.target_id is not None else target_id
                    key = (tid, draft.fingerprint)
                    produced_keys.add(key)
                    counts[draft.severity] = counts.get(draft.severity, 0) + 1
                    prior = by_key.get(key)
                    if prior is not None and prior.status in {
                        "open",
                        "acknowledged",
                        "dismissed",
                    }:
                        # Already open, or already triaged by a human — keep it;
                        # never duplicate an open finding or resurrect a decision.
                        prior.severity = draft.severity
                        prior.suggested_action = draft.suggested_action
                        prior.message_key = draft.message_key
                        prior.message_args = draft.message_args
                        continue
                    session.add(
                        LintFinding(
                            lint_run_id=lint_run.id,
                            workspace_id=workspace_id,
                            target_type=draft.target_type,
                            target_id=tid,
                            rule_id=draft.rule_id,
                            severity=draft.severity,
                            status="open",
                            suggested_action=draft.suggested_action,
                            message_key=draft.message_key,
                            message_args=draft.message_args,
                            fingerprint=draft.fingerprint,
                        )
                    )
        # Reconcile: enabled-rule OPEN findings on a linted target that did not
        # reproduce this run are now fixed.
        for finding in existing:
            if (
                finding.status == "open"
                and finding.target_id in linted_target_ids
                and (finding.target_id, finding.fingerprint) not in produced_keys
            ):
                finding.status = "fixed"
                finding.resolved_at = now
        # I11: tombstone-resolve open findings of permanently-removed rules so
        # they clear from upgraded DBs (a removed rule can never reproduce).
        if _REMOVED_RULE_IDS:
            for finding in session.scalars(
                select(LintFinding).where(
                    LintFinding.workspace_id == workspace_id,
                    LintFinding.status == "open",
                    LintFinding.rule_id.in_(_REMOVED_RULE_IDS),
                )
            ).all():
                finding.status = "fixed"
                finding.resolved_at = now
        produce_folder_summary_tasks(session, workspace_id)
        lint_run.finding_count_by_severity = counts
        lint_run.status = "completed"
        lint_run.completed_at = now
        session.flush()
        self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="lint.run",
            target_kind="lint_run",
            target_id=lint_run.id,
            payload={
                "lint_scope": lint_scope,
                "trigger_type": request.trigger_type,
                "finding_count_by_severity": counts,
            },
        )
        return self._run_read(lint_run)

    def create_run(
        self,
        session: Session,
        workspace_id: UUID,
        request: LintRunCreateRequest,
        actor_kind: str,
        actor_id: str,
    ) -> LintRunRead:
        # Resolve the per-workspace lint toggle policy ({rule_id: bool}); rules
        # with no entry fall back to default_enabled. (workspace None -> run_lint
        # raises not-found.)
        validate_request_inputs(request)
        workspace = session.get(Workspace, workspace_id)
        policy = (
            dict(workspace.lint_policy)
            if workspace is not None and workspace.lint_policy
            else None
        )
        return self.run_lint(
            session, workspace_id, request, actor_kind, actor_id, policy=policy
        )

    def enqueue_run(
        self,
        session: Session,
        workspace_id: UUID,
        request: LintRunCreateRequest,
        actor_kind: str,
        actor_id: str,
    ) -> LintRunRead:
        validate_request_inputs(request)
        if session.get(Workspace, workspace_id) is None:
            raise ReadNotFoundError("workspace not found")
        lint_run = LintRun(
            workspace_id=workspace_id,
            lint_scope={"kind": request.lint_scope},
            trigger_type=request.trigger_type,
            status="queued",
            finding_count_by_severity={},
        )
        session.add(lint_run)
        session.flush()
        self.job_queue.enqueue(
            session=session,
            workspace_id=workspace_id,
            job_type="lint_tick",
            trigger_type=request.trigger_type,
            idempotency_key=f"lint_run:{lint_run.id}",
            target_ref={
                "lint_run_id": str(lint_run.id),
                "actor_kind": actor_kind,
                "actor_id": actor_id,
            },
        )
        return self._run_read(lint_run)

    def execute_queued_run(
        self,
        session: Session,
        *,
        workspace_id: UUID,
        run_id: UUID | None,
        trigger_type: str,
        actor_kind: str = "system",
        actor_id: str = "lint-worker",
    ) -> LintRunRead:
        lint_run = session.get(LintRun, run_id) if run_id is not None else None
        if run_id is not None and lint_run is None:
            raise ReadNotFoundError("lint run not found")
        if lint_run is None:
            lint_run = LintRun(
                workspace_id=workspace_id,
                lint_scope={"kind": "workspace"},
                trigger_type=trigger_type,
                status="queued",
                finding_count_by_severity={},
            )
            session.add(lint_run)
            session.flush()
        workspace = session.get(Workspace, workspace_id)
        policy = (
            dict(workspace.lint_policy) if workspace and workspace.lint_policy else None
        )
        request = LintRunCreateRequest(
            lint_scope="workspace",
            trigger_type=lint_run.trigger_type,
        )
        return self.run_lint(
            session,
            workspace_id,
            request,
            actor_kind,
            actor_id,
            policy=policy,
            run=lint_run,
        )

    def list_rules(self, session: Session, workspace_id: UUID) -> LintRuleListData:
        """The lint rule catalog with each rule's effective enabled state for the
        workspace (policy override, else default_enabled). Drives the toggle UI."""
        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            raise ReadNotFoundError("workspace not found")
        infos = [
            LintRuleInfo(
                rule_id=rule.rule_id,
                layer=rule.layer,
                default_enabled=rule.default_enabled,
                enabled=(
                    LintStage.PUBLISH_FLOOR in rule.stages
                    or LintPolicy(workspace).rule_enabled(
                        rule.rule_id, default_enabled=rule.default_enabled
                    )
                ),
                runtime_checkable=getattr(rule, "runtime_checkable", True),
                policy_locked=LintStage.PUBLISH_FLOOR in rule.stages,
            )
            for rule in self.lint_registry.rules()
        ]
        infos.sort(key=lambda info: (info.layer, info.rule_id))
        return LintRuleListData(rules=infos)

    def get_run(
        self,
        session: Session,
        workspace_id: UUID,
        run_id: UUID,
        actor: ActorContext | None,
    ) -> LintRunRead:
        lint_run = session.get(LintRun, run_id)
        if lint_run is None or lint_run.workspace_id != workspace_id:
            raise ReadNotFoundError("lint run not found")
        run_read = self._run_read(lint_run)
        if not actor_can_read_drafts(actor):
            # LEAK-01 (third surface): finding_count_by_severity is a raw
            # tally persisted at run-completion time over EVERY finding this
            # run reproduced — including ones produced solely by a draft
            # body (list_findings excludes exactly those; this endpoint used
            # to skip that exclusion entirely, so an actor who cannot read
            # drafts could still learn "there are N findings of severity X"
            # for draft-only defects via the count alone). The run only
            # persisted the aggregate, not which finding ids it counted, so
            # a historical run's exact snapshot can't be reconstructed after
            # the fact; recompute a live, draft-safe tally with the same
            # predicate list_findings uses instead.
            #
            # `actor` has no default and is deliberately required (not
            # optional-with-a-default) so a caller cannot silently forget to
            # pass it the way the REST route originally did (it received an
            # actor and never forwarded it). And `actor_can_read_drafts(None)`
            # is False, so a caller that DOES pass `actor=None` still lands
            # here, not in the fully-open branch — the exact fail-OPEN-on-
            # missing-actor shape LEAK-03 fixed elsewhere (a target-metadata
            # projector that treated a missing actor as fully authorized)
            # must not be reintroduced here one commit later.
            run_read = run_read.model_copy(
                update={
                    "finding_count_by_severity": self._draft_safe_finding_counts(
                        session, lint_run.workspace_id
                    )
                }
            )
        return run_read

    def _resolve_targets(
        self, session: Session, workspace_id: UUID, lint_scope: dict
    ) -> list[tuple[str, object, UUID]]:
        """Resolve (target_type, target, target_id) tuples for the scope.

        ``workspace`` scope = every Context page in the workspace. A single-page
        scope can be added later by parsing ``lint_scope``.
        """
        pages = session.scalars(
            select(ContextPage).where(ContextPage.workspace_id == workspace_id)
        ).all()
        nodes = session.scalars(
            select(WorkspaceTreeNode).where(
                WorkspaceTreeNode.workspace_id == workspace_id
            )
        ).all()
        return [
            *(("context_page", page, page.id) for page in pages),
            *(("workspace_tree_node", node, node.id) for node in nodes),
        ]

    @staticmethod
    def _exclude_draft_only_findings(session: Session, base):
        """Drop findings whose defect exists ONLY in a draft body (LEAK-01).
        ``base`` must already be a ``select(LintFinding)`` scoped to the
        caller's workspace/status/etc. filters. Pure SQL exclusion with NO
        actor decision baked in — each caller decides WHETHER a given actor
        needs it (``list_findings``: only when the actor can't read drafts;
        ``get_run``'s draft-safe recount: unconditionally, since it only
        runs once the caller has already established the actor can't read
        drafts). Kept as one shared implementation so the two callers cannot
        drift on what counts as "draft-only" — exclude BEFORE any
        count/pagination so totals stay consistent, and fail CLOSED on a
        dangling (deleted) target via the page-visibility filter callers
        apply upstream. A finding with no ``body_scopes`` at all (e.g. a
        knowledge-layer rule that isn't body-derived) is not draft-only and
        stays visible — the check is an exact match, not "missing".
        """
        scoped = base.subquery()
        draft_only_ids = [
            row_id
            for row_id, message_args in session.execute(
                select(scoped.c.id, scoped.c.message_args).where(
                    scoped.c.target_type == "context_page"
                )
            ).all()
            if (message_args or {}).get("body_scopes") == ["draft"]
        ]
        if draft_only_ids:
            base = base.where(LintFinding.id.not_in(draft_only_ids))
        return base

    @classmethod
    def _draft_safe_finding_counts(
        cls, session: Session, workspace_id: UUID
    ) -> dict[str, int]:
        """Severity tally equivalent to ``run_lint``'s ``counts``, restricted
        to findings a draft-blind actor could see via ``list_findings``.
        Only call this once the caller has already established the actor
        can't read drafts (``get_run``) — unlike ``list_findings``, this
        applies the exclusion unconditionally rather than re-deciding it.
        Mirrors ``run_lint``'s own "existing" query (open + acknowledged +
        dismissed = everything a run counts as still-produced) and reuses
        the exact draft-only exclusion ``list_findings`` applies, via
        ``_exclude_draft_only_findings``, so the two surfaces cannot drift.
        """
        base = select(LintFinding).where(
            LintFinding.workspace_id == workspace_id,
            LintFinding.status.in_(("open", "acknowledged", "dismissed")),
        )
        base = cls._exclude_draft_only_findings(session, base)
        counts: dict[str, int] = {}
        for severity in session.scalars(
            base.with_only_columns(LintFinding.severity)
        ).all():
            counts[severity] = counts.get(severity, 0) + 1
        return counts

    @staticmethod
    def _finding_target_visible(
        session: Session, finding: LintFinding, actor: ActorContext | None
    ) -> bool:
        """Whether the actor may see a finding's target (context page / tree
        node). Used to hide a finding whose target the actor cannot read."""
        if actor is None or finding.target_id is None:
            return True
        # Fail CLOSED on a deleted target: its stored suggested_action /
        # message_args still hold the old title/slug snapshot.
        if finding.target_type == "context_page":
            page = session.get(ContextPage, finding.target_id)
            return (
                page is not None
                and page.workspace_id == finding.workspace_id
                and visible_context_version_id(page, actor)[0] is not None
            )
        if finding.target_type == "workspace_tree_node":
            from backend.services.tree_read import TreeReadService

            node = session.get(WorkspaceTreeNode, finding.target_id)
            return (
                node is not None
                and node.workspace_id == finding.workspace_id
                and TreeReadService.node_visible_to_actor(session, node, actor)
            )
        return True

    @staticmethod
    def _draft_only_hidden(finding: LintFinding, actor: ActorContext | None) -> bool:
        """Whether ``finding`` was produced ONLY by a draft body the actor
        cannot read (LEAK-01). Mirrors the ``list_findings`` exclusion so a
        direct finding_id decide cannot bypass it — otherwise a viewer who
        cannot see the list row could still fetch/decide it by guessing the
        id. A finding with no body_scopes, or one that also reproduces in the
        published head, is unaffected.

        Fails CLOSED on a missing actor: ``actor_can_read_drafts(None)`` is
        False, so ``actor=None`` falls through to the same hidden-if-draft-
        only check as any other draft-blind actor, not an automatic
        "visible". A caller that forgets to pass ``actor`` (or passes it as
        ``None``) must not bypass this the way ``get_run``'s sibling gate
        once could (LEAK-01/LEAK-03 shape) — see ``decide_finding``'s
        required ``actor`` parameter below.
        """
        if actor_can_read_drafts(actor):
            return False
        if finding.target_type != "context_page":
            return False
        return (finding.message_args or {}).get("body_scopes") == ["draft"]

    def decide_finding(
        self,
        session: Session,
        workspace_id: UUID,
        finding_id: UUID,
        request: LintFindingDecisionRequest,
        actor_kind: str,
        actor_id: str,
        actor: ActorContext | None,
    ) -> LintFindingRead:
        validate_request_inputs(request)
        finding = session.get(LintFinding, finding_id)
        if finding is None or finding.workspace_id != workspace_id:
            raise ReadNotFoundError("lint finding not found")
        # M-02 (Codex round-2): a direct-ID decide must 404 a finding whose
        # target the actor cannot read, exactly like the filtered list hides it.
        if not self._finding_target_visible(session, finding, actor):
            raise ReadNotFoundError("lint finding not found")
        # LEAK-01: same for a finding whose target page IS visible but whose
        # defect only exists in a draft body the actor cannot read.
        if self._draft_only_hidden(finding, actor):
            raise ReadNotFoundError("lint finding not found")
        target_status = STATUS_BY_DECISION[request.decision]

        reason = request.reason
        if request.decision == "dismiss" and finding.severity == "error":
            if not reason or not reason.strip():
                raise ReadValidationError(
                    "a reason is required to dismiss an error-level finding"
                )
            reason = reason.strip()

        if finding.status == target_status:
            target_title = self._target_titles(session, [finding]).get(
                (finding.target_type, finding.target_id)
            )
            return self._finding_read(finding, target_title=target_title)

        if finding.status in {"dismissed", "fixed"}:
            raise ReadValidationError("resolved lint finding cannot be changed")

        finding.status = target_status
        # Keep resolved_at in lockstep with the status (LG-F6): set it for
        # terminal states, clear it otherwise so an acknowledged finding never
        # keeps a stale resolved_at that downstream "resolved" checks misread.
        if finding.status in {"dismissed", "fixed"}:
            finding.resolved_at = datetime.now(UTC)
            if (
                finding.rule_id == "kb.dependent_maybe_stale"
                and finding.target_id is not None
            ):
                from backend.services.change_propagation import (
                    clear_stale_candidate_if_resolved,
                )

                clear_stale_candidate_if_resolved(
                    session,
                    workspace_id=workspace_id,
                    page_id=finding.target_id,
                    excluding_finding_id=finding.id,
                )
                from backend.services.change_propagation import (
                    expire_stale_contradiction_tasks,
                )

                expire_stale_contradiction_tasks(
                    session,
                    workspace_id=workspace_id,
                    finding_ids={finding.id},
                )
        else:
            finding.resolved_at = None
        self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="lint.finding.decision",
            target_kind="lint_finding",
            target_id=finding.id,
            payload={"decision": request.decision, "reason": reason},
        )
        session.flush()
        target_title = self._target_titles(session, [finding]).get(
            (finding.target_type, finding.target_id)
        )
        return self._finding_read(finding, target_title=target_title)

    def batch_decide_findings(
        self,
        session: Session,
        workspace_id: UUID,
        request: LintFindingBatchDecisionRequest,
        actor_kind: str,
        actor_id: str,
        actor: ActorContext | None,
    ) -> dict:
        validate_request_inputs(request)
        updated = []
        failed = []

        for finding_id in request.finding_ids:
            try:
                with session.begin_nested():
                    single_req = LintFindingDecisionRequest(
                        decision=request.decision, reason=request.reason
                    )
                    finding_read = self.decide_finding(
                        session=session,
                        workspace_id=workspace_id,
                        finding_id=finding_id,
                        request=single_req,
                        actor_kind=actor_kind,
                        actor_id=actor_id,
                        actor=actor,
                    )
                    updated.append(finding_read)
            except ReadServiceError as e:
                failed.append(
                    LintFindingBatchDecisionFailure(
                        id=finding_id, error_code=e.error_code, message=str(e)
                    )
                )

        return {"updated": updated, "failed": failed}

    @staticmethod
    def _finding_read(
        finding: LintFinding, *, target_title: str | None = None
    ) -> LintFindingRead:
        return LintFindingRead(
            id=finding.id,
            lint_run_id=finding.lint_run_id,
            workspace_id=finding.workspace_id,
            target_type=finding.target_type,
            target_id=finding.target_id,
            rule_id=finding.rule_id,
            severity=finding.severity,
            status=finding.status,
            suggested_action=finding.suggested_action,
            message_key=finding.message_key or finding.rule_id,
            message_args=finding.message_args or {},
            target_title=target_title,
            created_at=finding.created_at,
            resolved_at=finding.resolved_at,
        )

    @staticmethod
    def _target_titles(
        session: Session, findings: list[LintFinding]
    ) -> dict[tuple[str, UUID | None], str]:
        page_ids = {
            finding.target_id
            for finding in findings
            if finding.target_type == "context_page" and finding.target_id is not None
        }
        node_ids = {
            finding.target_id
            for finding in findings
            if finding.target_type == "workspace_tree_node"
            and finding.target_id is not None
        }
        titles: dict[tuple[str, UUID | None], str] = {}
        if page_ids:
            for entity_id, title in session.execute(
                select(ContextPage.id, ContextPage.title).where(
                    ContextPage.id.in_(page_ids)
                )
            ):
                titles[("context_page", entity_id)] = title
        if node_ids:
            for entity_id, title in session.execute(
                select(WorkspaceTreeNode.id, WorkspaceTreeNode.title).where(
                    WorkspaceTreeNode.id.in_(node_ids)
                )
            ):
                titles[("workspace_tree_node", entity_id)] = title
        return titles

    @staticmethod
    def _run_read(lint_run: LintRun) -> LintRunRead:
        return LintRunRead(
            id=lint_run.id,
            workspace_id=lint_run.workspace_id,
            lint_scope=lint_run.lint_scope,
            trigger_type=lint_run.trigger_type,
            status=lint_run.status,
            finding_count_by_severity=lint_run.finding_count_by_severity,
            created_at=lint_run.created_at,
            started_at=lint_run.started_at,
            completed_at=lint_run.completed_at,
        )

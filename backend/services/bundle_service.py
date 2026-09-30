import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.agent import AgentPage, AgentPageVersion
from backend.models.base import utc_now
from backend.models.bundle import AgentInputBundle, AgentInputBundleItem
from backend.models.context import ContextPage, ContextPageVersion
from backend.models.library import LibraryArtifact, LibraryItem, LibraryItemVersion
from backend.models.workspace import MCPClient, Workspace
from backend.policy.auth import ActorContext
from backend.repositories.workspace_scoped import get_in_workspace
from backend.schemas.agent import (
    AGENT_PAGE_CATEGORIES,
    BuildBundleRequest,
    BuildBundleResult,
    BundleItemEntry,
)
from backend.services.actor_visible_read import (
    actor_can_read_agent_page,
    actor_can_read_library_trust_level,
    agent_version_is_visible,
    context_version_is_visible,
    library_item_visible,
)
from backend.services.audit_service import AuditService
from backend.services.library_canonical import (
    canonical_library_artifact_belongs_to_item,
)
from backend.services.okf import EXCLUDED_LIBRARY_LIFECYCLE_STATUSES
from backend.services.read_errors import ReadNotFoundError, RejectedByPolicyError

PACKING_ORDER = {
    "agent_guide": 1,
    "persona": 2,
    "skill": 3,
    "memory": 4,
}
DEFAULT_CONTEXT_PRIORITY = 100
DEFAULT_ARTIFACT_PRIORITY = 200


@dataclass(frozen=True)
class BundleCandidate:
    source_kind: str
    pinned_id: UUID
    category_or_type: str
    priority: int
    token_count: int
    included_range: dict | None
    included_reason: str | None
    omission_reason: str | None


@dataclass(frozen=True)
class AgentPageBundleSummary:
    bundle_id: UUID
    status: str
    created_at: datetime
    valid_until: datetime
    used_at: datetime | None
    expired_at: datetime | None
    included: bool


class AgentInputBundleService:
    def __init__(self, audit_service: AuditService | None = None) -> None:
        self.audit_service = audit_service or AuditService()

    def build(
        self,
        session: Session,
        workspace_id: UUID,
        request: BuildBundleRequest,
        actor: ActorContext,
        actor_kind: str = "system",
        actor_id: str = "system",
    ) -> BuildBundleResult:
        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            raise ReadNotFoundError("workspace not found")
        client = get_in_workspace(
            session,
            MCPClient,
            workspace_id=workspace_id,
            resource_id=request.client_id,
        )
        if client is None:
            raise ReadNotFoundError("MCP client not found")

        ttl_seconds = self._resolve_ttl_seconds(workspace, request)
        valid_until = utc_now() + timedelta(seconds=ttl_seconds)
        candidates = self._agent_page_candidates(
            session=session,
            workspace_id=workspace_id,
            requested_categories=set(request.requested_categories),
            actor=actor,
            task_intent=request.task_intent,
        )
        candidates.extend(
            self._context_ref_candidates(
                session=session,
                workspace_id=workspace_id,
                context_refs=request.context_refs,
                actor=actor,
            )
        )
        candidates.extend(
            self._library_artifact_ref_candidates(
                session=session,
                workspace_id=workspace_id,
                library_artifact_refs=request.library_artifact_refs,
                actor=actor,
            )
        )

        packed = self._apply_budget(candidates, request.token_budget)
        permission_snapshot = {
            "actor_kind": actor.actor_kind,
            "workspace_role": actor.workspace_role,
            "scopes": sorted(actor.scopes),
        }
        invalidation_keys = self._invalidation_keys(
            workspace=workspace,
            permission_context=permission_snapshot,
            candidates=packed,
        )
        bundle = AgentInputBundle(
            workspace_id=workspace_id,
            client_id=client.id,
            task_intent=request.task_intent,
            token_budget=request.token_budget,
            status="generated",
            ttl_seconds=ttl_seconds,
            ttl_override_seconds=request.ttl_override_seconds,
            valid_until=valid_until,
            invalidation_keys=invalidation_keys,
        )
        session.add(bundle)
        session.flush()

        entries = []
        for candidate in packed:
            item = self._bundle_item(bundle.workspace_id, bundle.id, candidate)
            session.add(item)
            entries.append(self._entry(candidate))
        session.flush()
        self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            action="bundle.build",
            target_kind="agent_input_bundle",
            target_id=bundle.id,
            payload={
                "client_id": str(client.id),
                "task_intent": request.task_intent,
                "item_count": len(entries),
                "omitted_count": sum(
                    1 for entry in entries if entry.omission_reason is not None
                ),
            },
        )
        return BuildBundleResult(
            bundle_id=bundle.id,
            status="generated",
            valid_until=bundle.valid_until,
            invalidation_keys=bundle.invalidation_keys,
            items=entries,
        )

    def get_bundle(
        self,
        session: Session,
        workspace_id: UUID,
        bundle_id: UUID,
        actor: ActorContext | None = None,
    ) -> BuildBundleResult:
        bundle = get_in_workspace(
            session,
            AgentInputBundle,
            workspace_id=workspace_id,
            resource_id=bundle_id,
        )
        if bundle is None:
            raise ReadNotFoundError("bundle not found")
        items = session.scalars(
            select(AgentInputBundleItem)
            .where(AgentInputBundleItem.bundle_id == bundle.id)
            .order_by(AgentInputBundleItem.priority, AgentInputBundleItem.id)
        ).all()
        # M-03: the build path filters every source by the building actor, but a
        # stored bundle GET re-serves those pinned ids. Re-validate each item
        # against the CURRENT actor and drop any source it cannot see, so a
        # lower-privileged reader cannot recover hidden AgentPage/Context version
        # or restricted-Library artifact ids.
        return BuildBundleResult(
            bundle_id=bundle.id,
            status=bundle.status,
            valid_until=bundle.valid_until,
            invalidation_keys=bundle.invalidation_keys,
            items=[
                self._entry_from_item(item)
                for item in items
                if self._bundle_item_visible(session, item, actor, workspace_id)
            ],
        )

    def mark_used(
        self,
        session: Session,
        workspace_id: UUID,
        bundle_id: UUID,
        actor_kind: str = "system",
        actor_id: str = "system",
    ) -> AgentInputBundle:
        bundle = get_in_workspace(
            session,
            AgentInputBundle,
            workspace_id=workspace_id,
            resource_id=bundle_id,
        )
        if bundle is None:
            raise ReadNotFoundError("bundle not found")
        if bundle.status == "generated":
            bundle.status = "used"
            bundle.used_at = utc_now()
            self.audit_service.emit(
                session=session,
                workspace_id=workspace_id,
                actor_kind=actor_kind,
                actor_id=actor_id,
                action="bundle.use",
                target_kind="agent_input_bundle",
                target_id=bundle.id,
                payload={"used_at": bundle.used_at.isoformat()},
            )
            session.flush()
        return bundle

    def list_for_agent_page(
        self,
        session: Session,
        workspace_id: UUID,
        agent_page_id: UUID,
        actor: ActorContext | None = None,
    ) -> list[AgentPageBundleSummary]:
        page = get_in_workspace(
            session,
            AgentPage,
            workspace_id=workspace_id,
            resource_id=agent_page_id,
        )
        if (
            page is None
            or page.lifecycle_status == "archived"
            or not actor_can_read_agent_page(page, actor)
        ):
            raise ReadNotFoundError("Agent page not found")
        rows = session.execute(
            select(AgentInputBundle, AgentInputBundleItem.omission_reason)
            .join(
                AgentInputBundleItem,
                AgentInputBundleItem.bundle_id == AgentInputBundle.id,
            )
            .join(
                AgentPageVersion,
                AgentPageVersion.id == AgentInputBundleItem.agent_page_version_id,
            )
            .where(
                AgentInputBundle.workspace_id == workspace_id,
                AgentPageVersion.agent_page_id == agent_page_id,
            )
            .order_by(AgentInputBundle.created_at.desc(), AgentInputBundle.id)
        ).all()
        summaries: dict[UUID, AgentPageBundleSummary] = {}
        for bundle, omission_reason in rows:
            existing = summaries.get(bundle.id)
            included = omission_reason is None
            if existing is not None:
                if included and not existing.included:
                    summaries[bundle.id] = replace(existing, included=True)
                continue
            summaries[bundle.id] = AgentPageBundleSummary(
                bundle_id=bundle.id,
                status=bundle.status,
                created_at=bundle.created_at,
                valid_until=bundle.valid_until,
                used_at=bundle.used_at,
                expired_at=bundle.expired_at,
                included=included,
            )
        return list(summaries.values())

    @staticmethod
    def _bundle_item_visible(
        session: Session,
        item: AgentInputBundleItem,
        actor: ActorContext | None,
        workspace_id: UUID,
    ) -> bool:
        # Canonicality is a data-freshness boundary, not an actor permission.
        # Apply it to Library refs even for internal/system reads; the actor-less
        # compatibility path remains unchanged for every other bundle item kind.
        if actor is None and item.library_artifact_id is None:
            return True
        if item.agent_page_version_id is not None:
            version = get_in_workspace(
                session,
                AgentPageVersion,
                workspace_id=workspace_id,
                resource_id=item.agent_page_version_id,
            )
            page = (
                get_in_workspace(
                    session,
                    AgentPage,
                    workspace_id=workspace_id,
                    resource_id=version.agent_page_id,
                )
                if version
                else None
            )
            # Version-aware sensitivity/draft gate (agent_version_is_visible) plus
            # parent workspace + non-archived lifecycle — a stored ref must not
            # re-serve a cross-workspace or archived agent page (Codex R10/R12).
            return (
                page is not None
                and version is not None
                and page.lifecycle_status != "archived"
                and agent_version_is_visible(page, version, actor)
            )
        if item.context_page_version_id is not None:
            version = get_in_workspace(
                session,
                ContextPageVersion,
                workspace_id=workspace_id,
                resource_id=item.context_page_version_id,
            )
            page = (
                get_in_workspace(
                    session,
                    ContextPage,
                    workspace_id=workspace_id,
                    resource_id=version.page_id,
                )
                if version
                else None
            )
            # context_version_is_visible checks draft capability but not archived
            # lifecycle or workspace — add both (Codex R12).
            return (
                version is not None
                and page is not None
                and page.lifecycle_status != "archived"
                and context_version_is_visible(version, actor)
            )
        if item.library_artifact_id is not None:
            artifact = get_in_workspace(
                session,
                LibraryArtifact,
                workspace_id=workspace_id,
                resource_id=item.library_artifact_id,
            )
            version = (
                get_in_workspace(
                    session,
                    LibraryItemVersion,
                    workspace_id=workspace_id,
                    resource_id=artifact.library_item_version_id,
                )
                if artifact
                else None
            )
            source = (
                get_in_workspace(
                    session,
                    LibraryItem,
                    workspace_id=workspace_id,
                    resource_id=version.library_item_id,
                )
                if version
                else None
            )
            # Match the round-9 import gate: a bundle item must not re-serve an
            # artifact whose source is cross-workspace, archived/hidden, or whose
            # version has been superseded (Codex R11/R12).
            if (
                source is None
                or artifact is None
                or not canonical_library_artifact_belongs_to_item(
                    session,
                    item=source,
                    artifact_id=artifact.id,
                )
            ):
                return False
            if actor is None:
                return True
            return library_item_visible(
                source, actor, workspace_id=workspace_id, version=version
            )
        return True

    def _resolve_ttl_seconds(
        self,
        workspace: Workspace,
        request: BuildBundleRequest,
    ) -> int:
        policy = workspace.agent_injection_policy
        default_ttl = int(policy.get("bundle_ttl_default_seconds", 3600))
        max_ttl = int(policy.get("bundle_ttl_max_seconds", 86400))
        if request.ttl_override_seconds is None:
            return default_ttl
        if request.ttl_override_seconds > max_ttl:
            raise RejectedByPolicyError(
                "ttl_override_seconds exceeds workspace bundle_ttl_max_seconds"
            )
        return request.ttl_override_seconds

    def _agent_page_candidates(
        self,
        session: Session,
        workspace_id: UUID,
        requested_categories: set[str],
        actor: ActorContext,
        task_intent: str,
    ) -> list[BundleCandidate]:
        pages = session.scalars(
            select(AgentPage)
            .where(AgentPage.workspace_id == workspace_id)
            .order_by(AgentPage.priority, AgentPage.slug)
        ).all()
        candidates = []
        for page in sorted(pages, key=self._agent_page_sort_key):
            candidate = self._candidate_for_agent_page(
                session=session,
                page=page,
                requested_categories=requested_categories,
                actor=actor,
                task_intent=task_intent,
            )
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    def _candidate_for_agent_page(
        self,
        session: Session,
        page: AgentPage,
        requested_categories: set[str],
        actor: ActorContext,
        task_intent: str,
    ) -> BundleCandidate | None:
        if not actor_can_read_agent_page(page, actor):
            return None
        if page.lifecycle_status != "active":
            draft_id = page.latest_draft_version_id
            if draft_id is None:
                return None
            return BundleCandidate(
                source_kind="agent_page",
                pinned_id=draft_id,
                category_or_type=page.category,
                priority=PACKING_ORDER.get(page.category, 100),
                token_count=0,
                included_range=None,
                included_reason=None,
                omission_reason="draft_not_injectable",
            )
        if page.current_active_version_id is None:
            return None
        version = get_in_workspace(
            session,
            AgentPageVersion,
            workspace_id=page.workspace_id,
            resource_id=page.current_active_version_id,
        )
        if version is None:
            return None
        reason, omission = self._selection_reason(
            page=page,
            requested_categories=requested_categories,
            actor=actor,
            task_intent=task_intent,
        )
        return BundleCandidate(
            source_kind="agent_page",
            pinned_id=version.id,
            category_or_type=page.category,
            priority=PACKING_ORDER.get(page.category, 100),
            token_count=self._estimate_tokens(version.body_markdown),
            included_range=None,
            included_reason=reason,
            omission_reason=omission,
        )

    def _selection_reason(
        self,
        page: AgentPage,
        requested_categories: set[str],
        actor: ActorContext,
        task_intent: str,
    ) -> tuple[str | None, str | None]:
        if page.injection_policy == "disabled":
            return None, "policy_disabled"
        if not self._has_permission(page, actor):
            return None, "permission_denied"
        if page.injection_policy == "always":
            return "always_policy", None
        if page.category in requested_categories:
            return "requested_category", None
        if page.injection_policy == "default_on":
            return "default_on_policy", None
        if page.injection_policy == "permission_scoped":
            return "permission_scoped_match", None
        if self._task_matches(page, task_intent):
            return "task_matched", None
        return None, "task_not_matched"

    def _context_ref_candidates(
        self,
        session: Session,
        workspace_id: UUID,
        context_refs: list[UUID],
        actor: ActorContext,
    ) -> list[BundleCandidate]:
        candidates = []
        for version_id in context_refs:
            version = get_in_workspace(
                session,
                ContextPageVersion,
                workspace_id=workspace_id,
                resource_id=version_id,
            )
            page = (
                get_in_workspace(
                    session,
                    ContextPage,
                    workspace_id=workspace_id,
                    resource_id=version.page_id,
                )
                if version
                else None
            )
            if page is None:
                raise ReadNotFoundError("context page version not found")
            if not context_version_is_visible(version, actor):
                raise ReadNotFoundError("context page version not found")
            candidates.append(
                BundleCandidate(
                    source_kind="context_page_excerpt",
                    pinned_id=version.id,
                    category_or_type=page.page_type,
                    priority=DEFAULT_CONTEXT_PRIORITY,
                    token_count=self._estimate_tokens(version.body_md),
                    included_range=None,
                    included_reason="requested_context_ref",
                    omission_reason=None,
                )
            )
        return candidates

    def _library_artifact_ref_candidates(
        self,
        session: Session,
        workspace_id: UUID,
        library_artifact_refs: list[UUID],
        actor: ActorContext,
    ) -> list[BundleCandidate]:
        candidates = []
        for artifact_id in library_artifact_refs:
            artifact = get_in_workspace(
                session,
                LibraryArtifact,
                workspace_id=workspace_id,
                resource_id=artifact_id,
            )
            version = (
                get_in_workspace(
                    session,
                    LibraryItemVersion,
                    workspace_id=workspace_id,
                    resource_id=artifact.library_item_version_id,
                )
                if artifact
                else None
            )
            source = (
                get_in_workspace(
                    session,
                    LibraryItem,
                    workspace_id=workspace_id,
                    resource_id=version.library_item_id,
                )
                if version
                else None
            )
            if source is None:
                raise ReadNotFoundError("source artifact not found")
            if (
                source.lifecycle_status in EXCLUDED_LIBRARY_LIFECYCLE_STATUSES
                or not actor_can_read_library_trust_level(source.trust_level, actor)
                or not canonical_library_artifact_belongs_to_item(
                    session,
                    item=source,
                    artifact_id=artifact.id,
                )
            ):
                raise ReadNotFoundError("source artifact not found")
            candidates.append(
                BundleCandidate(
                    source_kind="library_artifact_excerpt",
                    pinned_id=artifact.id,
                    category_or_type=artifact.kind,
                    priority=DEFAULT_ARTIFACT_PRIORITY,
                    token_count=self._estimate_tokens(
                        artifact.conversion_detail.index_text or ""
                    ),
                    included_range=artifact.conversion_detail.location_model,
                    included_reason="requested_library_artifact_ref",
                    omission_reason=None,
                )
            )
        return candidates

    def _apply_budget(
        self,
        candidates: list[BundleCandidate],
        token_budget: int,
    ) -> list[BundleCandidate]:
        total = 0
        packed = []
        for candidate in candidates:
            if candidate.omission_reason is not None:
                packed.append(candidate)
                continue
            if total + candidate.token_count > token_budget:
                packed.append(
                    BundleCandidate(
                        **{
                            **candidate.__dict__,
                            "included_reason": None,
                            "omission_reason": "token_budget",
                        }
                    )
                )
                continue
            total += candidate.token_count
            packed.append(candidate)
        return packed

    def _invalidation_keys(
        self,
        workspace: Workspace,
        permission_context: dict,
        candidates: list[BundleCandidate],
    ) -> dict[str, object]:
        active_agent_refs = [
            str(candidate.pinned_id)
            for candidate in candidates
            if candidate.source_kind == "agent_page"
            and candidate.omission_reason is None
        ]
        context_refs = [
            str(candidate.pinned_id)
            for candidate in candidates
            if candidate.source_kind == "context_page_excerpt"
            and candidate.omission_reason is None
        ]
        library_artifact_refs = [
            str(candidate.pinned_id)
            for candidate in candidates
            if candidate.source_kind == "library_artifact_excerpt"
            and candidate.omission_reason is None
        ]
        return {
            "active_agent_pages_hash": self._stable_hash(active_agent_refs),
            "workspace_policy_hash": self._stable_hash(
                workspace.agent_injection_policy
            ),
            "permission_snapshot_hash": self._stable_hash(permission_context),
            "context_version_refs": context_refs,
            "library_artifact_refs": library_artifact_refs,
        }

    @staticmethod
    def _bundle_item(
        workspace_id: UUID,
        bundle_id: UUID,
        candidate: BundleCandidate,
    ) -> AgentInputBundleItem:
        kwargs = {
            "workspace_id": workspace_id,
            "bundle_id": bundle_id,
            "source_kind": candidate.source_kind,
            "category_or_type": candidate.category_or_type,
            "priority": candidate.priority,
            "included_range": candidate.included_range,
            "included_reason": candidate.included_reason,
            "omission_reason": candidate.omission_reason,
        }
        if candidate.source_kind == "agent_page":
            kwargs["agent_page_version_id"] = candidate.pinned_id
        elif candidate.source_kind == "context_page_excerpt":
            kwargs["context_page_version_id"] = candidate.pinned_id
        else:
            kwargs["library_artifact_id"] = candidate.pinned_id
        return AgentInputBundleItem(**kwargs)

    @staticmethod
    def _entry(candidate: BundleCandidate) -> BundleItemEntry:
        return BundleItemEntry(
            source_kind=candidate.source_kind,
            pinned_id=candidate.pinned_id,
            category_or_type=candidate.category_or_type,
            priority=candidate.priority,
            included_range=candidate.included_range,
            included_reason=candidate.included_reason,
            omission_reason=candidate.omission_reason,
        )

    @staticmethod
    def _entry_from_item(item: AgentInputBundleItem) -> BundleItemEntry:
        pinned_id = (
            item.agent_page_version_id
            or item.context_page_version_id
            or item.library_artifact_id
        )
        return BundleItemEntry(
            source_kind=item.source_kind,
            pinned_id=pinned_id,
            category_or_type=item.category_or_type or "",
            priority=item.priority,
            included_range=item.included_range,
            included_reason=item.included_reason,
            omission_reason=item.omission_reason,
        )

    @staticmethod
    def _agent_page_sort_key(page: AgentPage) -> tuple[int, int, str]:
        return (
            PACKING_ORDER.get(page.category, len(AGENT_PAGE_CATEGORIES) + 1),
            page.priority,
            page.slug,
        )

    @staticmethod
    def _has_permission(page: AgentPage, actor: ActorContext) -> bool:
        return actor_can_read_agent_page(page, actor)

    @staticmethod
    def _task_matches(page: AgentPage, task_intent: str) -> bool:
        text = f"{page.category} {page.title} {page.slug}".lower()
        intent = task_intent.lower()
        return any(term and term in intent for term in text.split())

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        return max(1, len(text.split()))

    @staticmethod
    def _stable_hash(payload: object) -> str:
        encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

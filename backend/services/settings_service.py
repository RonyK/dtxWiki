import base64
import binascii
import copy
import hashlib
import hmac
import json
import logging
import re
from datetime import datetime, timedelta
from math import ceil
from uuid import UUID

from sqlalchemy import and_, case, func, not_, or_, select, tuple_
from sqlalchemy.orm import Session

from backend.domain.input_policy import normalize_short_text, validate_text
from backend.models.base import utc_now
from backend.models.workspace import (
    NEW_LIBRARY_MAX_UPLOAD_BYTES,
    MCPClient,
    Workspace,
)
from backend.policy.auth import ActorContext
from backend.schemas.settings import (
    PolicyCard,
    SettingsMcpClientDeleteData,
    SettingsMcpClientListData,
    SettingsPolicies,
)
from backend.services.audit_service import AuditService
from backend.services.context_media_policy import (
    resolve_context_media_policy,
    validate_retention_hours,
)
from backend.services.db_read_snapshot import acquire_consistent_read_snapshot
from backend.services.db_write_intent import (
    acquire_workspace_write_intent,
    acquire_write_intent,
)
from backend.services.library_formats import (
    LIBRARY_FORMAT_POLICY_ALIASES,
    LIBRARY_SUPPORTED_POLICY_FORMAT_IDS,
)
from backend.services.library_policy import (
    LIBRARY_POLICY_SCHEMA_VERSION,
    MAX_LIBRARY_FORMAT_UPLOAD_BYTES,
    MAX_LIBRARY_WORKSPACE_UPLOAD_BYTES,
    MIN_LIBRARY_UPLOAD_BYTES,
    LibraryFamilyPolicyError,
    LibraryFamilyPolicyService,
    get_default_library_family_policy_service,
)
from backend.services.lint import LintRegistry
from backend.services.mcp_credential_health import binding_key_for_client
from backend.services.oauth_service import (
    WorkspaceOAuthHealth,
    attach_name_matched_oauth_client_ids,
    connection_cursor_predicate,
    decode_connection_cursor,
    encode_connection_cursor,
    load_workspace_oauth_health,
    oauth_client_id_for_mcp_client,
    refine_workspace_oauth_health,
)
from backend.services.read_errors import ReadNotFoundError
from backend.services.review_risk_policy import (
    AUTO_APPROVE_DELAY_MAX_DAYS,
    AUTO_APPROVE_DELAY_MIN_DAYS,
    RECONCILIATION_ATTENTION_MAX_DAYS,
    RECONCILIATION_REMINDER_MAX_HOURS,
    REVIEW_RISK_PRESETS,
    REVIEW_RISK_SIGNALS,
    RISK_LEVELS,
    apply_review_preset,
    normalized_review_policy,
    validate_review_policy,
)

logger = logging.getLogger(__name__)

# Two key sets get pushed into the workspace-plane listing SQL, and both are
# new unbounded-ish quantities in a hot admin query -- everything else here is
# O(1) in binds. One is the Active facet's: one bind parameter set per healthy
# (user, oauth client) pair in the workspace. The other is the client_name
# search's, bounded by the distinct OAuth client ids this workspace's own rows
# name. BOTH are counted by _warn_if_pushdown_key_set_is_large; a guard that
# watches only one of them is how the search set went unbounded unnoticed.
# Warn well below SQLite's compiled SQLITE_LIMIT_VARIABLE_NUMBER (250,000
# here) so an oddly-shaped workspace shows up in logs before it shows up as a
# failure.
MCP_CLIENT_PUSHDOWN_KEY_WARNING_THRESHOLD = 2000
# A cursor freezes its first page's evaluation time so a walk stays coherent,
# but only briefly: an older cursor re-evaluates credential expiry at the
# current time instead of showing long-expired credentials as usable.
MCP_CLIENT_CURSOR_EVALUATION_MAX_AGE = timedelta(minutes=10)
LANGUAGE_CODES = {"en", "ko"}
LITERAL_OPTIONS = {
    "visibility": {"private", "team", "public"},
    "strictness": {"light", "standard", "strict"},
    "decision": {"owner_review", "auto_accept", "manual_review"},
    "trust_level": {"open", "trusted", "restricted"},
    "category_inclusion_policy.agent_guide": {"always", "default_on", "never"},
    "category_inclusion_policy.persona": {
        "always",
        "default_on",
        "task_matched",
        "never",
    },
    "category_inclusion_policy.skill": {
        "always",
        "default_on",
        "task_matched",
        "never",
    },
    "category_inclusion_policy.memory": {
        "always",
        "default_on",
        "task_matched",
        "never",
    },
}
BOOLEAN_POLICY_KEYS = {
    "allow_store_without_converter",
    "manual_approval_required",
}
NUMBER_POLICY_KEYS = {"bundle_ttl_default_seconds", "bundle_ttl_max_seconds"}
REVIEW_NUMBER_POLICY_KEYS = {
    "automation.auto_approve_after_hours",
    "automation.auto_approve_by_risk.low.delay_days",
    "automation.auto_approve_by_risk.medium.delay_days",
    "automation.auto_approve_by_risk.high.delay_days",
    "reconciliation.reminder_after_hours",
    "reconciliation.attention_after_days",
}
REVIEW_BOOLEAN_POLICY_KEYS = {
    "automation.auto_approve_enabled",
    "automation.auto_approve_by_risk.low.allowed",
    "automation.auto_approve_by_risk.medium.allowed",
    "automation.auto_approve_by_risk.high.allowed",
    "review_required.always_for_sensitive_agent",
    "review_required.always_for_decision_page",
    "review_required.always_for_ai_authored",
}
# Alembic upgrades every persisted Workspace to the current schema. A malformed
# or programmatically constructed sparse value therefore falls back to the same
# default as a newly created Workspace rather than entering a legacy mode.
DEFAULT_LIBRARY_UPLOAD_BYTES = NEW_LIBRARY_MAX_UPLOAD_BYTES
LIBRARY_FORMATS = LIBRARY_SUPPORTED_POLICY_FORMAT_IDS
MAX_BUNDLE_TTL_SECONDS = 8760 * 3600
RETIRED_LIBRARY_SOURCE_RULE_KEYS = {
    "allowed_sources",
    "denied_sources",
    "source_patterns",
}
CITATION_TYPES = {"raw_source", "derived_artifact", "context_page"}
LIBRARY_RULE_TYPES = {"web", "manual_upload"}
MCP_CLIENT_STATUSES = {"active", "disabled"}
MCP_CLIENT_LIST_STATUSES = MCP_CLIENT_STATUSES | {"all"}
MCP_CLIENT_CURSOR_VERSION = 2
MCP_CLIENT_CURSOR_SORT_VERSION = "created_at_desc_id_desc_v1"
SYSTEM_MCP_CLIENT_TYPES = {
    "internal",
    "system",
    "internal_agent",
    "system_managed",
}
POLICY_GROUP_ATTRS = {
    "library": "library_policy",
    "citation": "citation_policy",
    "agent_injection": "agent_injection_policy",
    "review": "review_policy",
}
WORKSPACE_EDITABLE_FIELDS = {
    "title",
    "slug",
    "goal",
    "default_language",
    "ask_retrieval_mode",
}
# UI-04: "visibility" is deliberately absent. This generic field editor used
# to write workspace.visibility directly, bypassing every invariant
# WorkspaceAccessService.update_access enforces for the same field --
# stale-write rejection, private+join_policy="request" rejection,
# auto-downgrading join_policy on private, closing pending join requests,
# and clearing is_public on every page when a workspace stops being public.
# A round trip through this path (public -> private -> public) would leave
# stale requests open and instantly re-expose every page that used to be
# public, with no confirmation and no audit trail. `key not in
# WORKSPACE_EDITABLE_FIELDS` below is what actually rejects it; visibility
# changes go through Access & sharing (workspace_access_service.py) instead.
ASK_RETRIEVAL_MODES = ("auto", "keyword", "semantic", "hybrid")


class SettingsPolicyUpdateRejected(RuntimeError):
    error_code = "settings_policy_update_rejected"


class LibrarySourceRulesRetired(SettingsPolicyUpdateRejected):
    error_code = "library_source_rules_retired"


class LibraryPolicyBatchUpdateRequired(SettingsPolicyUpdateRejected):
    error_code = "library_policy_batch_update_required"


class LibraryFormatExtensionTransferRequiresRelease(SettingsPolicyUpdateRejected):
    error_code = "library_format_extension_transfer_requires_release"


class SettingsPolicyRevisionConflict(SettingsPolicyUpdateRejected):
    error_code = "settings_revision_conflict"

    def __init__(
        self,
        *,
        current_revision: str,
        current_policy: dict[str, object],
    ) -> None:
        super().__init__(
            "Settings changed in another session. Reload and review again."
        )
        self.current_revision = current_revision
        self.current_policy = current_policy


class LibraryPolicyValidationRejected(SettingsPolicyUpdateRejected):
    def __init__(self, error: LibraryFamilyPolicyError) -> None:
        super().__init__(error.safe_message)
        self.error_code = error.code


class SettingsMcpClientCursorInvalid(RuntimeError):
    error_code = "mcp_client_cursor_invalid"


class SettingsMcpClientDeleteRejected(RuntimeError):
    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def settings_policy_revision(group: str, policy: dict[str, object]) -> str:
    """Return the optimistic-concurrency token for one rendered policy card."""

    payload = json.dumps(
        {"group": group, "policy": policy},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _policy_card(*, name: str, title: str, policy: dict) -> PolicyCard:
    return PolicyCard(
        name=name,
        title=title,
        policy=policy,
        revision=settings_policy_revision(name, policy),
    )


class SettingsService:
    def __init__(
        self,
        *,
        library_family_policy: LibraryFamilyPolicyService | None = None,
        orphan_retention_hours: int = 168,
        lint_registry: LintRegistry | None = None,
    ) -> None:
        self.lint_registry = (
            lint_registry if lint_registry is not None else LintRegistry.builtin()
        )
        self.orphan_retention_hours = orphan_retention_hours
        self.library_family_policy = (
            library_family_policy or get_default_library_family_policy_service()
        )

    def list_mcp_clients(
        self,
        *,
        control_session: Session,
        workspace_session: Session,
        workspace_id: UUID,
        actor: ActorContext,
        status: str = "active",
        query: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
        cursor_secret: str,
    ) -> SettingsMcpClientListData:
        """Filter and count against one control-plane credential snapshot.

        Only exact credential identities with current workspace permission are
        active. Inactive is the exhaustive complement, including unknown legacy
        identities. Control-plane facts cross into the workspace query as
        literal key sets; no cross-database join or page-local filter is used.
        """

        acquire_consistent_read_snapshot(control_session)
        if control_session.get(Workspace, workspace_id) is None:
            raise ReadNotFoundError("workspace not found")
        if status not in MCP_CLIENT_LIST_STATUSES:
            raise SettingsMcpClientCursorInvalid("invalid MCP client status filter")
        # Cursor pages retain the first page's evaluation time for a short
        # window (MCP_CLIENT_CURSOR_EVALUATION_MAX_AGE). Explicit revocations
        # and membership changes are still re-read on each request.
        now = utc_now()
        normalized_query = " ".join((query or "").split()).casefold()
        actor_binding = _mcp_client_cursor_actor_binding(actor)
        typed_cursor = (
            _decode_mcp_client_cursor(
                cursor,
                secret=cursor_secret,
                workspace_id=workspace_id,
                actor_binding=actor_binding,
                status=status,
                query=normalized_query,
            )
            if cursor
            else None
        )
        if (
            typed_cursor is not None
            and now - typed_cursor[2] <= MCP_CLIENT_CURSOR_EVALUATION_MAX_AGE
        ):
            now = typed_cursor[2]
        control_session.rollback()

        # Credentials are judged by the exact bindings this workspace's own rows
        # name, so the control-plane read follows the connection count instead of
        # the members x tokens x rotations population. That costs one workspace
        # read before the control snapshot and a fresh workspace snapshot after
        # it. Every transaction is released first: the planes can share a single
        # connection (the single-database profile and every :memory: test pool),
        # where a second BEGIN is a hard error, and holding both at once is the
        # nesting backend/api/deps.py warns about.
        acquire_consistent_read_snapshot(workspace_session)
        oauth_client_id = MCPClient.token_metadata["oauth_client_id"].as_string()
        manageable_filters = (
            MCPClient.workspace_id == workspace_id,
            MCPClient.deleted_at.is_(None),
            MCPClient.status.in_(MCP_CLIENT_STATUSES),
            func.lower(MCPClient.client_type).notin_(SYSTEM_MCP_CLIENT_TYPES),
            func.lower(MCPClient.name) != "internal-agent",
        )
        # One read names every credential and OAuth app the rows point at; the
        # client_name search below reuses it instead of asking again.
        bound_rows = workspace_session.execute(
            select(
                MCPClient.issued_to_user_id,
                _mcp_client_binding_expression(),
                oauth_client_id,
            )
            .where(*manageable_filters)
            .distinct()
        ).all()
        workspace_session.rollback()
        bound_keys = {(row[0], row[1]) for row in bound_rows}

        acquire_consistent_read_snapshot(control_session)
        health = load_workspace_oauth_health(
            control_session,
            workspace_id=workspace_id,
            now=now,
            bindings={binding for _user, binding in bound_keys if binding is not None},
        )
        # Two later steps still read CONTROL -- the client_name search and the
        # page refinement below -- and both deliberately run OUTSIDE this
        # released snapshot, as plain key lookups rather than a re-entered
        # transaction. Neither can take a second explicit BEGIN on a shared
        # connection, and neither needs one: they resolve ids the workspace
        # plane just named, and an OAuthClient row renamed in between can only
        # change whether it matches the search term or what name it renders
        # under -- never which facet it lands in, because every facet input was
        # read inside the released snapshot.
        control_session.rollback()

        acquire_consistent_read_snapshot(workspace_session)
        search_filters = ()
        if normalized_query:
            # Resolve the client_name arm against the ids THIS workspace's own
            # rows name, and nothing else. The outer join this replaces could
            # only ever match a client a workspace row already pointed at, so
            # the answer is identical -- and both this IN and the control-plane
            # LIKE that produced it are now bounded by the workspace instead of
            # by the installation's whole oauth_client table. See
            # attach_name_matched_oauth_client_ids for what the unbounded shape
            # cost.
            bound_app_ids = {
                credential.oauth_client_id
                for key in bound_keys
                if (credential := health.credential_health.get(key)) is not None
                and credential.oauth_client_id is not None
            }
            health = attach_name_matched_oauth_client_ids(
                control_session,
                health,
                name_search=normalized_query,
                oauth_client_ids={row[2] for row in bound_rows if row[2] is not None}
                | bound_app_ids,
            )
            matched_bindings = [
                key
                for key, credential in health.credential_health.items()
                if credential.oauth_client_id
                in (health.name_matched_oauth_client_ids or ())
            ]
            search_filters = (
                or_(
                    or_(
                        tuple_(
                            MCPClient.issued_to_user_id,
                            _mcp_client_binding_expression(),
                        ).in_(matched_bindings),
                        and_(
                            MCPClient.issued_to_user_id.is_(None),
                            MCPClient.runtime_binding_key.in_(
                                [key[1] for key in matched_bindings]
                            ),
                        ),
                    ),
                    func.lower(MCPClient.name).contains(
                        normalized_query, autoescape=True
                    ),
                    func.lower(MCPClient.client_type).contains(
                        normalized_query, autoescape=True
                    ),
                    oauth_client_id.in_(
                        sorted(health.name_matched_oauth_client_ids or ())
                    ),
                    func.lower(oauth_client_id).contains(
                        normalized_query, autoescape=True
                    ),
                ),
            )

        total_count = int(
            workspace_session.scalar(
                select(func.count()).select_from(MCPClient).where(*manageable_filters)
            )
            or 0
        )
        _warn_if_pushdown_key_set_is_large(health)
        # `_mcp_client_active_connection_predicate` is called once per use and
        # never bound to a variable: the pushed-down key sets compile to
        # expanding bind parameters, and SQLAlchemy consumes one expression
        # object's values exactly once per statement.
        facet_row = workspace_session.execute(
            select(
                func.coalesce(
                    func.sum(
                        case(
                            (_mcp_client_active_connection_predicate(health), 1),
                            else_=0,
                        )
                    ),
                    0,
                ),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                not_(_mcp_client_active_connection_predicate(health)),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ),
            )
            .select_from(MCPClient)
            .where(*manageable_filters, *search_filters)
        ).one()
        active_count = int(facet_row[0])
        disabled_count = int(facet_row[1])
        filtered_count = {
            "active": active_count,
            "disabled": disabled_count,
            "all": active_count + disabled_count,
        }[status]

        data_filters = [*manageable_filters, *search_filters]
        if status == "active":
            data_filters.append(_mcp_client_active_connection_predicate(health))
        elif status == "disabled":
            data_filters.append(not_(_mcp_client_active_connection_predicate(health)))
        if typed_cursor is not None:
            cursor_created_at, cursor_id, _evaluated_at = typed_cursor
            data_filters.append(
                or_(
                    MCPClient.created_at < cursor_created_at,
                    and_(
                        MCPClient.created_at == cursor_created_at,
                        MCPClient.id < cursor_id,
                    ),
                )
            )
        rows = workspace_session.scalars(
            select(MCPClient)
            .where(*data_filters)
            .order_by(MCPClient.created_at.desc(), MCPClient.id.desc())
            .limit(limit + 1)
        ).all()
        page = list(rows[:limit])
        next_cursor = None
        if len(rows) > limit and page:
            last = page[-1]
            next_cursor = _encode_mcp_client_cursor(
                secret=cursor_secret,
                workspace_id=workspace_id,
                actor_binding=actor_binding,
                status=status,
                query=normalized_query,
                created_at=last.created_at,
                client_id=last.id,
                evaluated_at=now,
            )
        # A page row may name an OAuth client the workspace-wide scan above
        # never saw (no consent here). Its facet answer is already settled;
        # this adds the client rows the rendering still needs, at most `limit`
        # primary keys in one CONTROL query.
        health = refine_workspace_oauth_health(
            control_session,
            health,
            oauth_client_ids=_page_oauth_client_ids(page),
        )
        return SettingsMcpClientListData(
            data=_settings_mcp_client_payload(page, health=health),
            next_cursor=next_cursor,
            total_count=total_count,
            filtered_count=filtered_count,
            status_counts={"active": active_count, "disabled": disabled_count},
        )

    def get_policies(
        self,
        *,
        control_session: Session,
        workspace_session: Session,
        workspace_id: UUID,
        mcp_client_limit: int = 50,
        mcp_client_cursor: str | None = None,
    ) -> SettingsPolicies:
        """Render every policy card plus the first page of MCP connections.

        Every policy value is a ``Workspace`` column, so the cards are a pure
        CONTROL-plane read; only the ``MCPClient`` listing at the bottom is
        workspace-plane, and the OAuth health its rows are rendered with comes
        back off CONTROL again. Reads only -- no plane is written here.
        """

        workspace = control_session.get(Workspace, workspace_id)
        if workspace is None:
            raise ReadNotFoundError("workspace not found")
        decoded_cursor = decode_connection_cursor(
            mcp_client_cursor,
            identifier_count=1,
        )
        typed_cursor = None
        if decoded_cursor is not None:
            last_used_at, identifiers = decoded_cursor
            try:
                typed_cursor = (last_used_at, (UUID(identifiers[0]),))
            except ValueError:
                typed_cursor = None
        mcp_query = (
            select(MCPClient)
            .where(
                MCPClient.workspace_id == workspace_id,
                MCPClient.deleted_at.is_(None),
            )
            .order_by(
                MCPClient.last_used_at.is_(None),
                MCPClient.last_used_at.desc(),
                MCPClient.id,
            )
        )
        if typed_cursor is not None:
            mcp_query = mcp_query.where(
                connection_cursor_predicate(
                    MCPClient.last_used_at,
                    (MCPClient.id,),
                    typed_cursor,
                )
            )
        mcp_rows = workspace_session.scalars(
            mcp_query.limit(mcp_client_limit + 1)
        ).all()
        mcp_clients = mcp_rows[:mcp_client_limit]
        mcp_next_cursor = None
        if len(mcp_rows) > mcp_client_limit and mcp_clients:
            last_client = mcp_clients[-1]
            mcp_next_cursor = encode_connection_cursor(
                last_client.last_used_at,
                last_client.id,
            )
        # Narrowed to this page's own credentials, so the control-plane read stays
        # O(page) rather than O(workspace members): there is no facet here to
        # need the whole answer set.
        mcp_payload = _settings_mcp_client_payload(
            mcp_clients,
            health=load_workspace_oauth_health(
                control_session,
                workspace_id=workspace_id,
                now=utc_now(),
                oauth_client_ids=_page_oauth_client_ids(mcp_clients),
                bindings=_page_bindings(mcp_clients),
            ),
        )
        return SettingsPolicies(
            attachments=_policy_card(
                name="attachments",
                title="Unused file retention",
                policy=self._policy_group_projection(workspace, "attachments"),
            ),
            workspace=_policy_card(
                name="workspace",
                title="Workspace basics",
                policy={
                    "slug": workspace.slug,
                    "title": workspace.title,
                    "goal": workspace.goal,
                    "default_language": workspace.default_language,
                    "visibility": workspace.visibility,
                    "ask_retrieval_mode": workspace.ask_retrieval_mode,
                },
            ),
            library=_policy_card(
                name="library",
                title="Library upload policy",
                policy=normalized_library_policy(
                    workspace.library_policy,
                    family_policy=self.library_family_policy,
                ),
            ),
            citation=_policy_card(
                name="citation",
                title="Citation policy",
                policy=workspace.citation_policy,
            ),
            context_write=_policy_card(
                name="context_write",
                title="Context write policy",
                policy={},
            ),
            agent_injection=_policy_card(
                name="agent_injection",
                title="Agent injection policy",
                policy=workspace.agent_injection_policy,
            ),
            review=_policy_card(
                name="review",
                title="Review risk policy",
                policy=normalized_review_policy(workspace.review_policy),
            ),
            lint=_policy_card(
                name="lint",
                title="Lint rules",
                # Rule toggles ({rule_id: bool}) plus the auto-fix mode flag (F2),
                # so the Settings lint section can render both from one card.
                policy={
                    **dict(workspace.lint_policy or {}),
                    "auto_fix": workspace.auto_fix_enabled,
                },
            ),
            mcp_clients=mcp_payload,
            mcp_clients_next_cursor=mcp_next_cursor,
        )

    def update_policy(
        self,
        *,
        control_session: Session,
        workspace_session: Session,
        workspace_id: UUID,
        actor: ActorContext,
        group: str,
        key: str,
        value,
    ) -> SettingsPolicies:
        """Apply an owner-authorized policy update.

        The REST boundary establishes the owner role on the control plane
        before this transaction starts. ``actor`` remains here for audit
        attribution only.

        Writes CONTROL only -- see ``_update_policy_locked``. ``workspace_session``
        is read-only and exists solely so the refreshed cards can carry the MCP
        connection list.
        """
        return self._update_policy_locked(
            control_session=control_session,
            workspace_session=workspace_session,
            workspace_id=workspace_id,
            actor=actor,
            group=group,
            changes={key: value},
            expected_revision=None,
        )

    def update_policy_batch(
        self,
        *,
        control_session: Session,
        workspace_session: Session,
        workspace_id: UUID,
        actor: ActorContext,
        group: str,
        changes: dict[str, object],
        expected_revision: str,
    ) -> SettingsPolicies:
        """Atomically apply one card save guarded by its rendered revision.

        The guard stays atomic because every value it compares and writes lives
        on the CONTROL plane; ``workspace_session`` is read-only here.
        """

        if not changes:
            raise SettingsPolicyUpdateRejected("changes cannot be empty")
        return self._update_policy_locked(
            control_session=control_session,
            workspace_session=workspace_session,
            workspace_id=workspace_id,
            actor=actor,
            group=group,
            changes=changes,
            expected_revision=expected_revision,
        )

    def _update_policy_locked(
        self,
        *,
        control_session: Session,
        workspace_session: Session,
        workspace_id: UUID,
        actor: ActorContext,
        group: str,
        changes: dict[str, object],
        expected_revision: str | None,
    ) -> SettingsPolicies:
        """One CONTROL-plane write transaction, start to finish.

        Every target of this method is control-plane: the row it locks, every
        ``_update_workspace_field`` column, every JSON policy column, and the
        ``audit_event`` row (which backend/tenancy_manifest.py marks MIXED, so
        it may land on either plane and belongs on the plane being written).
        ``workspace_session`` is read-only and used only by the trailing
        ``get_policies``, so no route reaching here writes two planes and there
        is no distributed transaction to reason about.
        """

        if group == "attachments" and expected_revision is None:
            raise SettingsPolicyUpdateRejected(
                "attachments requires a batch save with expected_revision"
            )
        acquire_write_intent(control_session)
        workspace = control_session.scalar(
            select(Workspace).where(Workspace.id == workspace_id).with_for_update()
        )
        if workspace is None:
            raise ReadNotFoundError("workspace not found")

        current_policy = self._policy_group_projection(workspace, group)
        current_revision = settings_policy_revision(group, current_policy)
        if expected_revision is not None and not hmac.compare_digest(
            expected_revision,
            current_revision,
        ):
            raise SettingsPolicyRevisionConflict(
                current_revision=current_revision,
                current_policy=current_policy,
            )

        if group == "attachments":
            if set(changes) != {"orphan_retention_hours"}:
                raise SettingsPolicyUpdateRejected("Unknown attachment policy field")
            value = changes["orphan_retention_hours"]
            try:
                if value is not None:
                    value = validate_retention_hours(value)
            except ValueError as exc:
                raise SettingsPolicyUpdateRejected(str(exc)) from exc
            policy = dict(workspace.context_media_policy or {})
            if value is None:
                policy.pop("orphan_retention_hours", None)
            else:
                policy["orphan_retention_hours"] = value
            workspace.context_media_policy = policy
        elif group == "workspace":
            for key, value in changes.items():
                self._update_workspace_field(control_session, workspace, key, value)
        elif group == "lint":
            for key, value in changes.items():
                self._update_lint_policy(workspace, key, value)
        elif group in POLICY_GROUP_ATTRS:
            # Deep copy: _set_path mutates nested dicts in place (for example
            # category_inclusion_policy.memory). The column is plain JSON, not a
            # mutable type, so with a shallow copy that nested dict is the very
            # object the ORM holds as the committed value; the assignment below
            # then compares equal to it, no UPDATE is emitted, and the response
            # shows a value that was never stored.
            policy = copy.deepcopy(getattr(workspace, POLICY_GROUP_ATTRS[group]) or {})
            previous_library_policy: dict[str, object] | None = None
            if group == "library":
                policy = normalized_library_policy(
                    policy,
                    family_policy=self.library_family_policy,
                )
                previous_library_policy = dict(policy)
            elif group == "review":
                policy = normalized_review_policy(policy)

            ordered_changes = list(changes.items())
            if group == "review":
                ordered_changes.sort(key=lambda item: item[0] != "preset")
            elif group == "library":
                library_order = {
                    "policy_schema_version": 0,
                    "builtin_format_overrides": 1,
                    "custom_formats": 2,
                    "format_families": 3,
                    "format_overrides": 3,
                }
                ordered_changes.sort(key=lambda item: library_order.get(item[0], 3))
            for key, value in ordered_changes:
                if group == "library" and key in RETIRED_LIBRARY_SOURCE_RULE_KEYS:
                    raise LibrarySourceRulesRetired(
                        "Library source rules are retired until URL ingestion "
                        "is enforced."
                    )
                self._update_json_policy(
                    policy,
                    key,
                    value,
                    previous_library_policy=previous_library_policy,
                )
                if group == "review":
                    _apply_review_legacy_auto_approve_patch(policy, key, value)
                if group == "review" and key == "preset":
                    policy = apply_review_preset(policy, str(policy["preset"]))

            if group == "review":
                policy = normalized_review_policy(policy)
            elif group == "library":
                policy = normalized_library_policy(
                    policy,
                    family_policy=self.library_family_policy,
                )
                if previous_library_policy is None:
                    raise RuntimeError("Library policy baseline is unavailable")
                _validate_stable_library_extension_ownership(
                    previous_library_policy,
                    policy,
                    library_family_policy=self.library_family_policy,
                )
            _validate_policy_cross_fields(
                group,
                policy,
                library_family_policy=self.library_family_policy,
            )
            setattr(workspace, POLICY_GROUP_ATTRS[group], policy)
        else:
            raise SettingsPolicyUpdateRejected("unknown settings group")

        keys = list(changes)
        action = (
            f"settings.workspace.{keys[0]}.updated"
            if group == "workspace" and len(keys) == 1
            else f"settings.policy.{group}.updated"
        )
        AuditService().emit(
            session=control_session,
            workspace_id=workspace_id,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            action=action,
            target_kind="workspace",
            target_id=workspace_id,
            payload={
                "group": group,
                "key": keys[0],
                "keys": keys,
                **(
                    {
                        "before": current_policy,
                        "after": self._policy_group_projection(workspace, group),
                    }
                    if group == "attachments"
                    else {}
                ),
            },
        )
        control_session.flush()
        return self.get_policies(
            control_session=control_session,
            workspace_session=workspace_session,
            workspace_id=workspace_id,
        )

    def _policy_group_projection(
        self,
        workspace: Workspace,
        group: str,
    ) -> dict[str, object]:
        if group == "attachments":
            return resolve_context_media_policy(
                workspace, self.orphan_retention_hours
            ).policy
        if group == "workspace":
            return {
                "slug": workspace.slug,
                "title": workspace.title,
                "goal": workspace.goal,
                "default_language": workspace.default_language,
                "visibility": workspace.visibility,
                "ask_retrieval_mode": workspace.ask_retrieval_mode,
            }
        if group == "lint":
            return {
                **dict(workspace.lint_policy or {}),
                "auto_fix": workspace.auto_fix_enabled,
            }
        if group == "context_write":
            return {}
        if group not in POLICY_GROUP_ATTRS:
            raise SettingsPolicyUpdateRejected("unknown settings group")
        policy = dict(getattr(workspace, POLICY_GROUP_ATTRS[group]) or {})
        if group == "library":
            return normalized_library_policy(
                policy,
                family_policy=self.library_family_policy,
            )
        if group == "review":
            return normalized_review_policy(policy)
        return policy

    def update_mcp_client_status(
        self,
        *,
        control_session: Session,
        workspace_session: Session,
        workspace_id: UUID,
        actor: ActorContext,
        client_id: UUID,
        status: str,
    ) -> SettingsPolicies:
        """Update a client after the REST boundary's control-plane owner gate.

        Writes the WORKSPACE plane only: ``MCPClient.status`` plus the MIXED
        ``audit_event`` row, which belongs on the plane being written.
        ``control_session`` is read-only here (the existence gate and, through
        ``get_policies``, the refreshed policy cards), so this route writes one
        plane and there is no distributed transaction.
        """

        # The RESERVED lock is taken before ANY read this decision depends on,
        # and it names a workspace-plane table because that is the database
        # being written -- a shard has no `workspace` table to lock.
        acquire_workspace_write_intent(workspace_session)
        if control_session.get(Workspace, workspace_id) is None:
            raise ReadNotFoundError("workspace not found")
        client = workspace_session.scalar(
            select(MCPClient)
            .where(
                MCPClient.id == client_id,
                MCPClient.workspace_id == workspace_id,
            )
            .with_for_update()
        )
        if client is None:
            raise ReadNotFoundError("MCP client not found")
        if client.deleted_at is not None:
            raise SettingsPolicyUpdateRejected("deleted MCP clients cannot be edited")
        if _is_system_mcp_client(client):
            raise SettingsPolicyUpdateRejected("system MCP clients cannot be edited")
        if status not in MCP_CLIENT_STATUSES:
            options = ", ".join(sorted(MCP_CLIENT_STATUSES))
            raise SettingsPolicyUpdateRejected(
                f"MCP client status must be one of: {options}"
            )
        # MCP-01: Disable is a reversible toggle, not a revoke. The
        # underlying OAuth grant / PAT is left intact; the bearer-
        # verification path (backend/policy/rbac.require_workspace_role)
        # checks MCPClient.status directly for the (actor, workspace) pair
        # so a disabled connection stops authenticating immediately without
        # destroying credentials a re-enable would need back. Destructive
        # revocation belongs to the separate Revoke action, not this toggle.
        client.status = status
        AuditService().emit(
            session=workspace_session,
            workspace_id=workspace_id,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            action="settings.mcp_client.status.changed",
            target_kind="mcp_client",
            target_id=client_id,
            payload={"status": status},
        )
        workspace_session.flush()
        return self.get_policies(
            control_session=control_session,
            workspace_session=workspace_session,
            workspace_id=workspace_id,
        )

    def delete_mcp_client(
        self,
        *,
        control_session: Session,
        workspace_session: Session,
        workspace_id: UUID,
        actor: ActorContext,
        client_id: UUID,
    ) -> SettingsMcpClientDeleteData:
        """Permanently hide a disabled client while preserving its history.

        Writes the WORKSPACE plane only (the tombstone plus the MIXED audit
        row); ``control_session`` backs the existence gate and nothing else.
        """

        # See update_mcp_client_status: workspace-plane lock, workspace-plane
        # write, control-plane gate.
        acquire_workspace_write_intent(workspace_session)
        if control_session.get(Workspace, workspace_id) is None:
            raise ReadNotFoundError("workspace not found")
        client = workspace_session.scalar(
            select(MCPClient)
            .where(
                MCPClient.id == client_id,
                MCPClient.workspace_id == workspace_id,
            )
            .with_for_update()
        )
        if client is None:
            raise ReadNotFoundError("MCP client not found")
        if client.deleted_at is not None:
            raise SettingsMcpClientDeleteRejected(
                "MCP client is already deleted",
                error_code="mcp_client_already_deleted",
            )
        if _is_system_mcp_client(client):
            raise SettingsMcpClientDeleteRejected(
                "system MCP clients cannot be deleted",
                error_code="mcp_client_system_managed",
            )
        if client.status != "disabled":
            raise SettingsMcpClientDeleteRejected(
                "active MCP clients must be disabled before deletion",
                error_code="mcp_client_must_be_disabled",
            )

        deleted_at = utc_now()
        client.deleted_at = deleted_at
        AuditService().emit(
            session=workspace_session,
            workspace_id=workspace_id,
            actor_kind=actor.actor_kind,
            actor_id=actor.actor_id,
            action="settings.mcp_client.deleted",
            target_kind="mcp_client",
            target_id=client_id,
            payload={
                "client_type": client.client_type,
                "previous_status": client.status,
            },
        )
        workspace_session.flush()
        return SettingsMcpClientDeleteData(
            deleted_client_id=client.id,
            deleted_at=deleted_at,
        )

    @staticmethod
    def _update_workspace_field(
        control_session: Session,
        workspace: Workspace,
        key: str,
        value,
    ) -> None:
        """Every editable field here is a ``Workspace`` column, so the uniqueness
        probe below is a control-plane read on the same session that writes."""

        if key not in WORKSPACE_EDITABLE_FIELDS:
            raise SettingsPolicyUpdateRejected(f"{key} cannot be edited")
        if key == "slug":
            normalized = _normalize_workspace_slug(value)
            if normalized != workspace.slug:
                existing = control_session.scalar(
                    select(Workspace.id).where(
                        Workspace.slug == normalized,
                        Workspace.id != workspace.id,
                    )
                )
                if existing is not None:
                    raise SettingsPolicyUpdateRejected("workspace slug already exists")
            workspace.slug = normalized
            return
        if key == "default_language":
            normalized = _coerce_string(value)
            if normalized not in LANGUAGE_CODES:
                raise SettingsPolicyUpdateRejected("unsupported language")
            workspace.default_language = normalized
            return
        if key == "ask_retrieval_mode":
            normalized = _coerce_string(value).strip()
            if normalized not in ASK_RETRIEVAL_MODES:
                raise SettingsPolicyUpdateRejected(
                    "ask_retrieval_mode must be one of "
                    + ", ".join(ASK_RETRIEVAL_MODES)
                )
            workspace.ask_retrieval_mode = normalized
            return
        text = (
            normalize_short_text(value, field=key, max_length=128)
            if key == "title"
            else validate_text(_coerce_string(value).strip(), field=key, max_length=512)
        )
        if not text:
            raise SettingsPolicyUpdateRejected(f"{key} cannot be empty")
        setattr(workspace, key, text)

    def _update_lint_policy(self, workspace: Workspace, key: str, value) -> None:
        # Lint toggles are dynamic ({rule_id: bool}), so they bypass the
        # fixed-key _update_json_policy. The reserved "auto_fix" key sets the
        # workspace auto-fix mode (F2); everything else is a rule_id toggle
        # validated against the registry. Both require a boolean.
        if not isinstance(value, bool):
            raise SettingsPolicyUpdateRejected("lint toggle must be a boolean")
        if key == "auto_fix":
            workspace.auto_fix_enabled = value
            return
        rule = self.lint_registry.rule(key)
        if rule is None:
            raise SettingsPolicyUpdateRejected(f"unknown lint rule_id: {key}")
        if not getattr(rule, "runtime_checkable", True):
            raise SettingsPolicyUpdateRejected(
                f"lint rule is supplied externally and cannot be toggled: {key}"
            )
        policy = dict(workspace.lint_policy or {})
        policy[key] = value
        workspace.lint_policy = policy

    def _update_json_policy(
        self,
        policy: dict,
        key: str,
        value,
        *,
        previous_library_policy: dict[str, object] | None = None,
    ) -> None:
        if key == "policy_schema_version":
            if value != LIBRARY_POLICY_SCHEMA_VERSION:
                raise SettingsPolicyUpdateRejected(
                    "policy_schema_version must name the current Library policy schema"
                )
            policy["policy_schema_version"] = LIBRARY_POLICY_SCHEMA_VERSION
            return
        current = _get_path(policy, key)
        dynamic_format_override = key.startswith("format_overrides.")
        if (
            current is _MISSING
            and key != "max_upload_bytes"
            and not dynamic_format_override
        ):
            raise SettingsPolicyUpdateRejected(f"{key} cannot be edited")
        if key == "format_overrides":
            # A batch may remove a custom format and its exact preference
            # together. Once ``custom_formats`` has been applied first, the
            # old id is no longer in the catalog; keep only already-persisted
            # custom override ids editable so the same final-state CAS can
            # delete them. Cross-field validation still rejects any orphan
            # retained or newly supplied at the end of the batch.
            existing_custom_override_ids = {
                str(format_id)
                for format_id in policy.get(key, {})
                if str(format_id).startswith("custom-")
            }
            allowed_format_ids = frozenset(
                {
                    *LIBRARY_FORMATS,
                    *existing_custom_override_ids,
                    *(
                        str(item["format_id"])
                        for item in self.library_family_policy.coerce_custom_formats(
                            policy.get("custom_formats", []),
                            builtin_format_overrides=policy.get(
                                "builtin_format_overrides", {}
                            ),
                        )
                    ),
                }
            )
            policy_value = _coerce_library_limit_mapping_update(
                value,
                existing=policy.get(key),
                field_name=key,
                allowed_keys=allowed_format_ids,
                aliases=LIBRARY_FORMAT_POLICY_ALIASES,
                max_bytes=MAX_LIBRARY_FORMAT_UPLOAD_BYTES,
            )
        elif key == "builtin_format_overrides":
            try:
                policy_value = (
                    self.library_family_policy.coerce_builtin_format_overrides(value)
                )
            except LibraryFamilyPolicyError as exc:
                raise LibraryPolicyValidationRejected(exc) from exc
        elif key == "format_families":
            try:
                policy_value = self.library_family_policy.coerce_families_for_update(
                    value,
                    existing_families=policy.get("format_families", []),
                    custom_formats=policy.get("custom_formats", []),
                    builtin_format_overrides=policy.get("builtin_format_overrides", {}),
                )
            except LibraryFamilyPolicyError as exc:
                raise LibraryPolicyValidationRejected(exc) from exc
        elif key == "custom_formats":
            try:
                policy_value = (
                    self.library_family_policy.coerce_custom_formats_for_update(
                        value,
                        existing_custom_formats=policy.get("custom_formats", []),
                        builtin_format_overrides=policy.get(
                            "builtin_format_overrides", {}
                        ),
                        existing_builtin_format_overrides=(
                            previous_library_policy or {}
                        ).get("builtin_format_overrides", {}),
                    )
                )
            except LibraryFamilyPolicyError as exc:
                raise LibraryPolicyValidationRejected(exc) from exc
        elif dynamic_format_override:
            custom_ids = {
                str(item["format_id"])
                for item in self.library_family_policy.coerce_custom_formats(
                    policy.get("custom_formats", []),
                    builtin_format_overrides=policy.get("builtin_format_overrides", {}),
                )
            }
            raw_format_id = key.removeprefix("format_overrides.")
            normalized_format_id = _normalized_library_format_id(raw_format_id)
            if normalized_format_id not in LIBRARY_FORMATS | custom_ids:
                raise SettingsPolicyUpdateRejected(
                    "format_overrides key must name a built-in or Workspace "
                    "custom format"
                )
            policy_value = _coerce_library_upload_limit(
                value,
                field_name=key,
                max_bytes=MAX_LIBRARY_FORMAT_UPLOAD_BYTES,
            )
        else:
            policy_value = _coerce_policy_value(key, value)
        _set_path(policy, key, policy_value)


class _Missing:
    pass


_MISSING = _Missing()


def _validate_policy_cross_fields(
    group: str,
    policy: dict,
    *,
    library_family_policy: LibraryFamilyPolicyService | None = None,
) -> None:
    # Per-key coercion (_coerce_policy_value) cannot see sibling fields, so the
    # default<=max TTL invariant is enforced here against the merged policy (C8).
    # update_policy changes one key per call, so checking the merged dict is
    # correct and order-independent.
    if group == "review":
        try:
            validate_review_policy(policy)
        except ValueError as exc:
            raise SettingsPolicyUpdateRejected(str(exc)) from exc
        return
    if group == "library":
        family_policy = (
            library_family_policy or get_default_library_family_policy_service()
        )
        try:
            builtin_format_overrides = family_policy.coerce_builtin_format_overrides(
                policy.get("builtin_format_overrides", {})
            )
            custom_formats = family_policy.coerce_custom_formats(
                policy.get("custom_formats", []),
                builtin_format_overrides=builtin_format_overrides,
            )
            custom_ids = {str(item["format_id"]) for item in custom_formats}
            orphaned_custom_overrides = sorted(
                format_id
                for format_id in policy.get("format_overrides", {})
                if str(format_id).startswith("custom-") and format_id not in custom_ids
            )
            if orphaned_custom_overrides:
                raise LibraryFamilyPolicyError(
                    "library_custom_format_id_conflict",
                    "Remove exact limits for deleted custom formats in the same "
                    "policy update.",
                )
            family_policy.coerce_families(
                policy.get("format_families", []),
                custom_formats=custom_formats,
                builtin_format_overrides=builtin_format_overrides,
            )
        except LibraryFamilyPolicyError as exc:
            raise LibraryPolicyValidationRejected(exc) from exc
        return
    if group != "agent_injection":
        return
    default = policy.get("bundle_ttl_default_seconds")
    maximum = policy.get("bundle_ttl_max_seconds")
    if (
        isinstance(default, int)
        and not isinstance(default, bool)
        and isinstance(maximum, int)
        and not isinstance(maximum, bool)
        and default > maximum
    ):
        raise SettingsPolicyUpdateRejected(
            "bundle_ttl_default_seconds must be less than or equal to "
            "bundle_ttl_max_seconds"
        )


def _validate_stable_library_extension_ownership(
    before: dict[str, object],
    after: dict[str, object],
    *,
    library_family_policy: LibraryFamilyPolicyService,
) -> None:
    """Require one committed release before an alias changes owners."""

    before_owners = library_family_policy.extension_owners(
        builtin_format_overrides=before.get("builtin_format_overrides", {}),
        custom_formats=before.get("custom_formats", []),
    )
    after_owners = library_family_policy.extension_owners(
        builtin_format_overrides=after.get("builtin_format_overrides", {}),
        custom_formats=after.get("custom_formats", []),
    )
    package_owners = library_family_policy.extension_owners()
    transferred = next(
        (
            extension
            for extension, owners in after_owners.items()
            if before_owners.get(extension)
            and owners - before_owners[extension]
            and not (
                len(package_owners.get(extension, ())) > 1
                and owners == package_owners[extension]
            )
        ),
        None,
    )
    if transferred is not None:
        raise LibraryFormatExtensionTransferRequiresRelease(
            f"Release extension '{transferred}' in one saved revision before "
            "assigning it to another format."
        )


def _apply_review_legacy_auto_approve_patch(policy: dict, key: str, value) -> None:
    if key not in {
        "automation.auto_approve_enabled",
        "automation.auto_approve_below",
        "automation.auto_approve_after_hours",
    }:
        return
    automation = policy.get("automation")
    if not isinstance(automation, dict):
        return
    by_risk = automation.get("auto_approve_by_risk")
    if not isinstance(by_risk, dict):
        return
    if key == "automation.auto_approve_after_hours":
        if isinstance(value, bool) or not isinstance(value, int):
            return
        delay_days = min(max(ceil(value / 24), 1), 30)
        for level in ("low", "medium", "high"):
            row = by_risk.get(level)
            if isinstance(row, dict):
                row["delay_days"] = delay_days
        return
    enabled = bool(automation.get("auto_approve_enabled"))
    threshold = automation.get("auto_approve_below")
    if threshold not in {"low", "medium"}:
        threshold = "medium"
    for level in ("low", "medium"):
        row = by_risk.get(level)
        if isinstance(row, dict):
            row["allowed"] = enabled and (level == "low" or threshold == "medium")
    high = by_risk.get("high")
    if isinstance(high, dict):
        high["allowed"] = False


def _coerce_policy_value(key: str, value):
    if key == "preset":
        return _coerce_review_preset(value)
    if key.startswith("defaults."):
        return _coerce_review_risk_level(key, value)
    if key in {"review_required.from_risk", "automation.auto_approve_below"}:
        return _coerce_review_risk_level(key, value)
    if key.startswith("risk_rules."):
        return _coerce_review_signal_list(key, value)
    if key in REVIEW_BOOLEAN_POLICY_KEYS:
        if not isinstance(value, bool):
            raise SettingsPolicyUpdateRejected(f"{key} must be a boolean")
        return value
    if key in REVIEW_NUMBER_POLICY_KEYS:
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingsPolicyUpdateRejected(f"{key} must be an integer")
        if key == "reconciliation.reminder_after_hours":
            if value < 1 or value > RECONCILIATION_REMINDER_MAX_HOURS:
                raise SettingsPolicyUpdateRejected(
                    f"{key} must be between 1 and "
                    f"{RECONCILIATION_REMINDER_MAX_HOURS} hours"
                )
            return value
        if key == "reconciliation.attention_after_days":
            if value < 1 or value > RECONCILIATION_ATTENTION_MAX_DAYS:
                raise SettingsPolicyUpdateRejected(
                    f"{key} must be between 1 and "
                    f"{RECONCILIATION_ATTENTION_MAX_DAYS} days"
                )
            return value
        if key.endswith(".delay_days"):
            # Bounds come from the policy contract, which allows 0 ("approve on
            # the next sweep, no aging wait") and is what the form offers. This
            # check used to demand >= 1 on its own, so a 0 the UI invited was
            # rejected by the write and then displayed back as 1.
            if (
                value < AUTO_APPROVE_DELAY_MIN_DAYS
                or value > AUTO_APPROVE_DELAY_MAX_DAYS
            ):
                raise SettingsPolicyUpdateRejected(
                    f"{key} must be between {AUTO_APPROVE_DELAY_MIN_DAYS} and "
                    f"{AUTO_APPROVE_DELAY_MAX_DAYS} days"
                )
            return value
        if value < 1 or value > 720:
            raise SettingsPolicyUpdateRejected(f"{key} must be between 1 and 720 hours")
        return value
    if key == "max_upload_bytes":
        return _coerce_library_upload_limit(
            value,
            max_bytes=MAX_LIBRARY_WORKSPACE_UPLOAD_BYTES,
        )
    if key == "format_overrides":
        return _coerce_library_limit_mapping(
            value,
            field_name=key,
            allowed_keys=LIBRARY_FORMATS,
            aliases=LIBRARY_FORMAT_POLICY_ALIASES,
            max_bytes=MAX_LIBRARY_FORMAT_UPLOAD_BYTES,
        )
    if key.startswith("format_overrides."):
        format_id = key.removeprefix("format_overrides.")
        _require_library_mapping_key(
            format_id,
            field_name="format_overrides",
            allowed_keys=LIBRARY_FORMATS,
            aliases=LIBRARY_FORMAT_POLICY_ALIASES,
        )
        return _coerce_library_upload_limit(
            value,
            field_name=key,
            max_bytes=MAX_LIBRARY_FORMAT_UPLOAD_BYTES,
        )
    if key in RETIRED_LIBRARY_SOURCE_RULE_KEYS:
        raise LibrarySourceRulesRetired(
            "Library source rules are retired until URL ingestion is enforced."
        )
    if key == "accepted_citation_types":
        return _coerce_citation_types(value)
    if key in LITERAL_OPTIONS:
        return _coerce_literal(key, value)
    if key in BOOLEAN_POLICY_KEYS:
        if not isinstance(value, bool):
            raise SettingsPolicyUpdateRejected(f"{key} must be a boolean")
        return value
    if key in NUMBER_POLICY_KEYS:
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingsPolicyUpdateRejected(f"{key} must be an integer")
        if value < 0:
            raise SettingsPolicyUpdateRejected(f"{key} must be non-negative")
        if value > MAX_BUNDLE_TTL_SECONDS:
            raise SettingsPolicyUpdateRejected(
                f"{key} must be {MAX_BUNDLE_TTL_SECONDS} seconds or smaller "
                f"({MAX_BUNDLE_TTL_SECONDS // 3600} hours)."
            )
        return value
    raise SettingsPolicyUpdateRejected(f"{key} cannot be edited")


def _normalize_workspace_slug(value) -> str:
    validate_text(_coerce_string(value), field="slug", max_length=80, min_length=1)
    slug = re.sub(r"[^a-z0-9-]+", "-", _coerce_string(value).lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        raise SettingsPolicyUpdateRejected("workspace slug is required")
    validate_text(slug, field="slug", max_length=80, min_length=1)
    return slug


def normalized_library_policy(
    policy: dict | None,
    *,
    family_policy: LibraryFamilyPolicyService | None = None,
) -> dict:
    """Return a safe, forward-compatible Library policy projection.

    Persisted rows are upgraded to schema v5 by Alembic. Runtime normalization
    consequently has one shape and no legacy UI/runtime mode. Known nested
    fields are normalized while compatible future top-level values survive.
    """

    source = dict(policy) if isinstance(policy, dict) else {}
    removed_development_keys = {
        "category_limits",
        "enabled_formats",
        "family_modes",
        "format_exceptions",
        "profile_id",
        "profile_revision",
        *RETIRED_LIBRARY_SOURCE_RULE_KEYS,
    }
    normalized = {
        key: value
        for key, value in source.items()
        if key not in removed_development_keys
    }
    normalized["policy_schema_version"] = LIBRARY_POLICY_SCHEMA_VERSION
    normalized["max_upload_bytes"] = _normalized_library_upload_limit(
        source.get("max_upload_bytes", _MISSING),
        fallback=DEFAULT_LIBRARY_UPLOAD_BYTES,
        max_bytes=MAX_LIBRARY_WORKSPACE_UPLOAD_BYTES,
    )
    try:
        policy_service = family_policy or get_default_library_family_policy_service()
        normalized["builtin_format_overrides"] = (
            policy_service.coerce_builtin_format_overrides(
                source.get("builtin_format_overrides", {})
            )
        )
        normalized["custom_formats"] = policy_service.coerce_custom_formats(
            source.get("custom_formats", []),
            builtin_format_overrides=normalized["builtin_format_overrides"],
        )
        normalized["format_families"] = policy_service.coerce_families(
            source.get("format_families", []),
            custom_formats=normalized["custom_formats"],
            builtin_format_overrides=normalized["builtin_format_overrides"],
        )
    except LibraryFamilyPolicyError as exc:
        raise LibraryPolicyValidationRejected(exc) from exc
    custom_ids = {str(item["format_id"]) for item in normalized["custom_formats"]}
    allowed_override_ids = LIBRARY_FORMATS | custom_ids
    normalized["format_overrides"] = {
        format_id: limit
        for format_id, limit in _normalized_library_limit_mapping(
            source.get("format_overrides"),
            aliases=LIBRARY_FORMAT_POLICY_ALIASES,
            max_bytes=MAX_LIBRARY_FORMAT_UPLOAD_BYTES,
        ).items()
        if format_id in allowed_override_ids or format_id.startswith("custom-")
    }
    unavailable_behavior = source.get("unavailable_behavior")
    normalized["unavailable_behavior"] = (
        dict(unavailable_behavior)
        if isinstance(unavailable_behavior, dict)
        else {
            "preview": "store_and_download",
            "semantic_conversion": "store_and_download",
        }
    )
    for key in ("manual_approval_required", "allow_store_without_converter"):
        value = source.get(key, True)
        normalized[key] = value if isinstance(value, bool) else True
    return normalized


def library_upload_limit_bytes(
    policy: dict | None,
    *,
    format_id: str | None = None,
    instance_hard_cap_bytes: int | None = None,
    family_policy: LibraryFamilyPolicyService | None = None,
) -> int:
    """Resolve one Workspace preference beneath the deployment hard cap.

    Exact-format preference replaces a family preference, which replaces the
    Workspace default. The instance limit remains an absolute ceiling.
    """

    policy_service = family_policy or get_default_library_family_policy_service()
    normalized = normalized_library_policy(
        policy,
        family_policy=policy_service,
    )
    preference = normalized["max_upload_bytes"]
    if format_id is not None:
        policy_format_id = _normalized_library_format_id(format_id)
        format_limit = normalized["format_overrides"].get(policy_format_id)
        if format_limit is not None:
            preference = format_limit
        else:
            family = policy_service.family_for_format(
                normalized["format_families"],
                format_id,
                custom_formats=normalized["custom_formats"],
                builtin_format_overrides=normalized["builtin_format_overrides"],
            )
            if family is not None and family.max_upload_bytes is not None:
                preference = family.max_upload_bytes

    limits = [preference]
    if instance_hard_cap_bytes is not None:
        if (
            isinstance(instance_hard_cap_bytes, bool)
            or not isinstance(instance_hard_cap_bytes, int)
            or instance_hard_cap_bytes <= 0
        ):
            raise ValueError("instance_hard_cap_bytes must be a positive integer")
        limits.append(instance_hard_cap_bytes)

    return min(limits)


def library_upload_limit_sources(
    policy: dict | None,
    *,
    format_id: str | None = None,
    instance_hard_cap_bytes: int | None = None,
    family_policy: LibraryFamilyPolicyService | None = None,
) -> dict[str, int]:
    """Return the active Workspace preference and absolute instance ceiling."""

    policy_service = family_policy or get_default_library_family_policy_service()
    normalized = normalized_library_policy(
        policy,
        family_policy=policy_service,
    )
    preference_source = "workspace"
    preference = normalized["max_upload_bytes"]
    if format_id is not None:
        policy_format_id = _normalized_library_format_id(format_id)
        format_limit = normalized["format_overrides"].get(policy_format_id)
        if format_limit is not None:
            preference_source = "format"
            preference = format_limit
        else:
            family = policy_service.family_for_format(
                normalized["format_families"],
                format_id,
                custom_formats=normalized["custom_formats"],
                builtin_format_overrides=normalized["builtin_format_overrides"],
            )
            if family is not None and family.max_upload_bytes is not None:
                preference_source = "family"
                preference = family.max_upload_bytes
    sources = {preference_source: preference}
    if instance_hard_cap_bytes is not None:
        if (
            isinstance(instance_hard_cap_bytes, bool)
            or not isinstance(instance_hard_cap_bytes, int)
            or instance_hard_cap_bytes <= 0
        ):
            raise ValueError("instance_hard_cap_bytes must be a positive integer")
        sources["instance"] = instance_hard_cap_bytes
    return sources


def library_format_is_enabled(
    policy: dict | None,
    format_id: str,
    *,
    family_policy: LibraryFamilyPolicyService | None = None,
) -> bool:
    """Return whether a detected format belongs to the current policy catalog."""

    normalized = normalized_library_policy(
        policy,
        family_policy=family_policy,
    )
    custom_ids = {str(item["format_id"]) for item in normalized["custom_formats"]}
    policy_format_id = _normalized_library_format_id(format_id)
    return policy_format_id in LIBRARY_FORMATS or policy_format_id in custom_ids


def format_library_upload_limit(limit_bytes: int) -> str:
    if limit_bytes >= 1_000_000:
        if limit_bytes % 1_000_000 == 0:
            return f"{limit_bytes // 1_000_000} MB"
        return f"{limit_bytes / 1_000_000:.1f} MB"
    if limit_bytes % 1_000 == 0:
        return f"{limit_bytes // 1_000} KB"
    return f"{limit_bytes} bytes"


def _coerce_library_upload_limit(
    value,
    *,
    field_name: str = "max_upload_bytes",
    max_bytes: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsPolicyUpdateRejected(f"{field_name} must be an integer")
    if value < MIN_LIBRARY_UPLOAD_BYTES:
        raise SettingsPolicyUpdateRejected(
            f"{field_name} must be at least {MIN_LIBRARY_UPLOAD_BYTES}"
        )
    if value > max_bytes:
        raise SettingsPolicyUpdateRejected(f"{field_name} must be at most {max_bytes}")
    return value


def _coerce_library_limit_mapping(
    value,
    *,
    field_name: str,
    allowed_keys: frozenset[str],
    aliases: dict[str, str] | None = None,
    max_bytes: int,
) -> dict[str, int]:
    if not isinstance(value, dict):
        raise SettingsPolicyUpdateRejected(f"{field_name} must be an object")
    normalized: dict[str, int] = {}
    for raw_key, raw_limit in value.items():
        key = _require_library_mapping_key(
            raw_key,
            field_name=field_name,
            allowed_keys=allowed_keys,
            aliases=aliases,
        )
        if key in normalized:
            raise SettingsPolicyUpdateRejected(
                f"{field_name} contains duplicate key after normalization: {key}"
            )
        normalized[key] = _coerce_library_upload_limit(
            raw_limit,
            field_name=f"{field_name}.{key}",
            max_bytes=max_bytes,
        )
    return normalized


def _coerce_library_limit_mapping_update(
    value,
    *,
    existing,
    field_name: str,
    allowed_keys: frozenset[str],
    aliases: dict[str, str] | None = None,
    max_bytes: int,
) -> dict[str, int]:
    """Validate a known mapping edit while preserving future server keys.

    Older clients round-trip the complete mapping rendered by the server. They
    may preserve a future key unchanged, but may neither create nor modify one.
    Omitting a future key also cannot delete it accidentally.
    """

    if not isinstance(value, dict):
        raise SettingsPolicyUpdateRejected(f"{field_name} must be an object")
    existing_compatible = _normalized_library_limit_mapping(
        existing,
        aliases=aliases,
        max_bytes=max_bytes,
    )
    normalized: dict[str, int] = {}
    for raw_key, raw_limit in value.items():
        try:
            key = _normalized_library_mapping_key(raw_key)
        except ValueError as exc:
            raise SettingsPolicyUpdateRejected(
                f"{field_name} keys must be lowercase format identifiers"
            ) from exc
        if aliases is not None:
            key = aliases.get(key, key)
        if key in normalized:
            raise SettingsPolicyUpdateRejected(
                f"{field_name} contains duplicate key after normalization: {key}"
            )
        if key not in allowed_keys:
            if key not in existing_compatible or raw_limit != existing_compatible[key]:
                raise SettingsPolicyUpdateRejected(
                    f"{field_name} contains an unknown key that this client "
                    "cannot edit: "
                    f"{key}"
                )
            normalized[key] = existing_compatible[key]
            continue
        normalized[key] = _coerce_library_upload_limit(
            raw_limit,
            field_name=f"{field_name}.{key}",
            max_bytes=max_bytes,
        )
    for key, limit in existing_compatible.items():
        if key not in allowed_keys:
            normalized.setdefault(key, limit)
    return normalized


def _normalized_library_upload_limit(
    value,
    *,
    fallback: int,
    max_bytes: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return fallback
    if value < MIN_LIBRARY_UPLOAD_BYTES or value > max_bytes:
        return fallback
    return value


def _normalized_library_limit_mapping(
    value,
    *,
    defaults: dict[str, int] | None = None,
    aliases: dict[str, str] | None = None,
    max_bytes: int,
) -> dict[str, int]:
    """Normalize persisted mappings without destroying future valid entries."""

    normalized = dict(defaults or {})
    if not isinstance(value, dict):
        return normalized
    for raw_key, raw_limit in value.items():
        try:
            key = _normalized_library_mapping_key(raw_key)
        except ValueError:
            continue
        if aliases is not None:
            key = aliases.get(key, key)
        fallback = normalized.get(key, _MISSING)
        valid_limit = _normalized_library_upload_limit(
            raw_limit,
            fallback=fallback if isinstance(fallback, int) else -1,
            max_bytes=max_bytes,
        )
        if valid_limit >= MIN_LIBRARY_UPLOAD_BYTES:
            normalized[key] = valid_limit
    return normalized


def _normalized_library_format_id(value) -> str:
    normalized = _normalized_library_mapping_key(value)
    return LIBRARY_FORMAT_POLICY_ALIASES.get(normalized, normalized)


def _normalized_library_mapping_key(value) -> str:
    if not isinstance(value, str):
        raise ValueError("Library policy mapping keys must be strings")
    normalized = value.strip().casefold()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", normalized):
        raise ValueError("invalid Library policy mapping key")
    return normalized


def _require_library_mapping_key(
    value,
    *,
    field_name: str,
    allowed_keys: frozenset[str],
    aliases: dict[str, str] | None = None,
) -> str:
    try:
        normalized = _normalized_library_mapping_key(value)
    except ValueError as exc:
        raise SettingsPolicyUpdateRejected(
            f"{field_name} keys must be lowercase format identifiers"
        ) from exc
    if aliases is not None:
        normalized = aliases.get(normalized, normalized)
    if normalized not in allowed_keys:
        raise SettingsPolicyUpdateRejected(
            f"{field_name} key must be one of: {', '.join(sorted(allowed_keys))}"
        )
    return normalized


def _coerce_library_rules(value) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise SettingsPolicyUpdateRejected("library rules must be a list")
    rules: list[dict[str, str]] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise SettingsPolicyUpdateRejected(
                f"library rule {index} must be an object"
            )
        raw_type = item.get("type")
        if not isinstance(raw_type, str):
            raise SettingsPolicyUpdateRejected(f"library rule {index} type is required")
        rule_type = raw_type.strip()
        if rule_type not in LIBRARY_RULE_TYPES:
            options = ", ".join(sorted(LIBRARY_RULE_TYPES))
            raise SettingsPolicyUpdateRejected(
                f"library rule {index} type must be one of: {options}"
            )
        if rule_type == "manual_upload":
            rules.append({"type": "manual_upload"})
            continue
        raw_pattern = item.get("pattern")
        if not isinstance(raw_pattern, str):
            raise SettingsPolicyUpdateRejected(
                f"library rule {index} pattern must be a string"
            )
        pattern = raw_pattern.strip()
        if not pattern:
            raise SettingsPolicyUpdateRejected(
                f"library rule {index} pattern is required"
            )
        rules.append({"type": "web", "pattern": pattern})
    return rules


def _coerce_citation_types(value) -> list[str]:
    if not isinstance(value, list):
        raise SettingsPolicyUpdateRejected("accepted citation types must be a list")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise SettingsPolicyUpdateRejected(
                "accepted citation type must be a string"
            )
        citation_type = item.strip()
        if citation_type not in CITATION_TYPES:
            options = ", ".join(sorted(CITATION_TYPES))
            raise SettingsPolicyUpdateRejected(
                f"accepted citation type must be one of: {options}"
            )
        if citation_type not in normalized:
            normalized.append(citation_type)
    return normalized


def _is_system_mcp_client(client: MCPClient) -> bool:
    client_type = client.client_type.casefold()
    name = client.name.casefold()
    return client_type in SYSTEM_MCP_CLIENT_TYPES or name == "internal-agent"


def _page_bindings(clients: list[MCPClient]) -> set[str]:
    return {key for client in clients if (key := binding_key_for_client(client))}


def _page_oauth_client_ids(clients: list[MCPClient]) -> list[str]:
    return [
        oauth_client_id
        for oauth_client_id in (
            oauth_client_id_for_mcp_client(client) for client in clients
        )
        if oauth_client_id is not None
    ]


def _mcp_client_binding_expression():
    metadata = MCPClient.token_metadata
    fallback = case(
        (
            metadata["token_kind"].as_string() == "oauth_jwt",
            "oauth_grant:" + metadata["token_id"].as_string(),
        ),
        (
            metadata["token_kind"].as_string().in_(["api_token", "pat"]),
            "api_token:" + metadata["token_id"].as_string(),
        ),
        else_=None,
    )
    return func.coalesce(MCPClient.runtime_binding_key, fallback)


def _mcp_client_active_connection_predicate(health: WorkspaceOAuthHealth):
    """Use the same exact control-plane credential snapshot as row rendering.

    Literal key sets cross the database boundary; token tables never join the
    workspace-plane query. Unknown identities belong to the inactive complement.
    """

    metadata = MCPClient.token_metadata
    binding = _mcp_client_binding_expression()
    app_id = func.nullif(metadata["oauth_client_id"].as_string(), "")
    groups: dict[str | None, list[tuple[UUID, str]]] = {}
    for (user_id, key), credential in health.credential_health.items():
        if credential.state == "active":
            groups.setdefault(credential.oauth_client_id, []).append((user_id, key))
    arms = []
    for oauth_id, keys in groups.items():
        arms.append(
            and_(
                or_(
                    tuple_(MCPClient.issued_to_user_id, binding).in_(sorted(keys)),
                    and_(
                        MCPClient.issued_to_user_id.is_(None),
                        MCPClient.runtime_binding_key.in_([key[1] for key in keys]),
                    ),
                ),
                or_(app_id.is_(None), app_id == oauth_id)
                if oauth_id is not None
                else app_id.is_(None),
            )
        )
    return and_(MCPClient.status == "active", func.coalesce(or_(*arms, False), False))


def _warn_if_pushdown_key_set_is_large(health: WorkspaceOAuthHealth) -> None:
    """One log line per listing, not one per predicate instance.

    It counts EVERY key set this listing pushes into workspace-plane SQL, not
    just the facet's. The name-search set is the one that most needs watching:
    it is the only one a request parameter can steer, and an earlier cut of
    this rewrite left it unbounded by the workspace entirely while this guard
    measured only ``active_connection_keys()`` -- so the request that blew the
    bind count up was precisely the one that logged nothing.
    """

    key_count = len(health.credential_health) + len(
        health.name_matched_oauth_client_ids or ()
    )
    if key_count <= MCP_CLIENT_PUSHDOWN_KEY_WARNING_THRESHOLD:
        return
    # Bind-parameter count now scales with a workspace's consent count and its
    # connection count, neither of which anything else in this query does.
    # SQLite's compiled limit here is 250,000 and PostgreSQL's is far higher,
    # so this is a "look at the shape of this workspace" signal rather than an
    # imminent failure.
    logger.warning(
        "settings.mcp_clients.pushdown_keys_large",
        extra={
            "workspace_id": str(health.workspace_id),
            "pushdown_key_count": key_count,
            "threshold": MCP_CLIENT_PUSHDOWN_KEY_WARNING_THRESHOLD,
        },
    )


def _settings_mcp_client_payload(
    clients: list[MCPClient],
    *,
    health: WorkspaceOAuthHealth,
) -> list[dict[str, object]]:
    """Render a page of clients from an already-loaded control-plane snapshot.

    PURE by construction -- it holds no session, so it cannot read the wrong
    plane. `list_mcp_clients` builds `health` at the same instant it filtered
    and counted with, so a row can never render a state that contradicts the
    facet it arrived in.
    """

    payload: list[dict[str, object]] = []
    for client in clients:
        connection_state, next_expiry, oauth_client_id, oauth_client_name = (
            health.connection_state_for(client)
        )
        credential = health.credential_for(client)
        payload.append(
            {
                "credential_kind": "unknown"
                if credential is None
                else "oauth"
                if credential.oauth_client_id
                else "pat",
                "id": client.id,
                "name": client.name,
                "client_name": oauth_client_name or client.name,
                "client_type": client.client_type,
                "scopes": client.scopes,
                "status": client.status,
                "connection_state": connection_state,
                "oauth_client_id": oauth_client_id,
                "last_used_at": client.last_used_at,
                "next_expiry": next_expiry,
            }
        )
    return payload


def _mcp_client_cursor_actor_binding(actor: ActorContext) -> str:
    stable_actor_id = actor.user_id or actor.actor_id
    return f"{actor.actor_kind}:{stable_actor_id}"


def _mcp_client_cursor_filter_fingerprint(
    *,
    workspace_id: UUID,
    actor_binding: str,
    status: str,
    query: str,
) -> str:
    material = "\0".join(
        (
            str(workspace_id),
            actor_binding,
            status,
            query,
            MCP_CLIENT_CURSOR_SORT_VERSION,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _encode_mcp_client_cursor(
    *,
    secret: str,
    workspace_id: UUID,
    actor_binding: str,
    status: str,
    query: str,
    created_at: datetime,
    client_id: UUID,
    evaluated_at: datetime,
) -> str:
    if not secret:
        raise ValueError("MCP client cursor secret must not be empty")
    payload = {
        "v": MCP_CLIENT_CURSOR_VERSION,
        "f": _mcp_client_cursor_filter_fingerprint(
            workspace_id=workspace_id,
            actor_binding=actor_binding,
            status=status,
            query=query,
        ),
        "created": created_at.isoformat(),
        "evaluated": evaluated_at.isoformat(),
        "id": client_id.hex,
    }
    encoded = (
        base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        .decode("ascii")
        .rstrip("=")
    )
    signature = hmac.new(
        secret.encode("utf-8"),
        encoded.encode("ascii"),
        hashlib.sha256,
    ).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"{encoded}.{encoded_signature}"


def _decode_mcp_client_cursor(
    cursor: str,
    *,
    secret: str,
    workspace_id: UUID,
    actor_binding: str,
    status: str,
    query: str,
) -> tuple[datetime, UUID, datetime]:
    try:
        if not secret:
            raise ValueError("missing cursor secret")
        encoded, encoded_signature = cursor.split(".")
        expected_signature = (
            base64.urlsafe_b64encode(
                hmac.new(
                    secret.encode("utf-8"),
                    encoded.encode("ascii"),
                    hashlib.sha256,
                ).digest()
            )
            .decode("ascii")
            .rstrip("=")
        )
        if not hmac.compare_digest(encoded_signature, expected_signature):
            raise ValueError("invalid cursor signature")
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
        )
        if set(payload) != {"v", "f", "created", "id", "evaluated"}:
            raise ValueError("invalid cursor payload")
        if payload["v"] != MCP_CLIENT_CURSOR_VERSION:
            raise ValueError("unsupported cursor version")
        expected_filter = _mcp_client_cursor_filter_fingerprint(
            workspace_id=workspace_id,
            actor_binding=actor_binding,
            status=status,
            query=query,
        )
        if not hmac.compare_digest(str(payload["f"]), expected_filter):
            raise ValueError("cursor does not match query")
        created_at = datetime.fromisoformat(payload["created"])
        client_id = UUID(hex=payload["id"])
        if client_id.hex != payload["id"]:
            raise ValueError("invalid cursor client")
        evaluated_at = datetime.fromisoformat(payload["evaluated"])
        if evaluated_at.tzinfo is None:
            raise ValueError("missing evaluation timezone")
        return created_at, client_id, evaluated_at
    except (
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
        binascii.Error,
    ) as exc:
        raise SettingsMcpClientCursorInvalid("invalid MCP client cursor") from exc


def _coerce_literal(key: str, value) -> str:
    normalized = _coerce_string(value)
    options = LITERAL_OPTIONS[key]
    if normalized not in options:
        raise SettingsPolicyUpdateRejected(
            f"{key} must be one of: {', '.join(sorted(options))}"
        )
    return normalized


def _coerce_review_preset(value) -> str:
    normalized = _coerce_string(value)
    if normalized not in REVIEW_RISK_PRESETS:
        raise SettingsPolicyUpdateRejected(
            f"preset must be one of: {', '.join(REVIEW_RISK_PRESETS)}"
        )
    return normalized


def _coerce_review_risk_level(key: str, value) -> str:
    normalized = _coerce_string(value)
    allowed = (
        ("low", "medium") if key == "automation.auto_approve_below" else RISK_LEVELS
    )
    if normalized not in allowed:
        raise SettingsPolicyUpdateRejected(
            f"{key} must be one of: {', '.join(allowed)}"
        )
    return normalized


def _coerce_review_signal_list(key: str, value) -> list[str]:
    if not isinstance(value, list):
        raise SettingsPolicyUpdateRejected(f"{key} must be a list")
    normalized = []
    for item in value:
        if not isinstance(item, str) or item not in REVIEW_RISK_SIGNALS:
            raise SettingsPolicyUpdateRejected(
                f"{key} entries must be known review risk signals"
            )
        if item not in normalized:
            normalized.append(item)
    return normalized


def _coerce_string(value) -> str:
    if not isinstance(value, str):
        raise SettingsPolicyUpdateRejected("value must be a string")
    return value.strip()


def _get_path(policy: dict, path: str):
    current = policy
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _set_path(policy: dict, path: str, value) -> None:
    current = policy
    parts = path.split(".")
    for part in parts[:-1]:
        current = current[part]
    current[parts[-1]] = value

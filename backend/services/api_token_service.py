import base64
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from backend.catalog.mcp_apps import (
    CatalogRepository,
    get_mcp_app_catalog_repository,
)
from backend.domain.input_policy import (
    SHORT_TEXT_SPACES,
    normalize_short_text,
    validate_list_count,
)
from backend.models.api_token import ApiToken, ApiTokenWorkspace
from backend.models.base import utc_now
from backend.models.user import User
from backend.models.workspace import Workspace, WorkspaceMembership
from backend.policy.rbac import get_workspace_access, role_allows
from backend.services.audit_service import AuditService
from backend.services.credential_hashing import CredentialHasher
from backend.services.credential_replica_service import (
    CredentialReplicaFanOutRequest,
    register_fan_out,
)
from backend.services.db_read_snapshot import acquire_consistent_read_snapshot
from backend.services.db_write_intent import acquire_write_intent
from backend.services.session_service import AuthenticatedUser, authenticated_user

ALLOWED_API_TOKEN_SCOPES = ("mcp:read", "mcp:write", "mcp:agent")
LEGACY_GENERIC_MCP_CLIENT_TYPE = "generic"
LEGACY_GENERIC_MCP_CLIENT_LABEL = "Generic MCP client"
TOKEN_PREVIEW_LENGTH = 9
ACCESS_KEY_CURSOR_VERSION = 1
ACCESS_KEY_SORT_VERSION = "created_at_desc_id_desc_v1"
AccessKeyStatus = Literal["active", "expired", "revoked"]
AccessKeyListStatus = Literal["active", "inactive", "expired", "revoked", "all"]
# D-01v3/P10.3 decision B (.claude/plan/260909-p10-3-multi-workspace-credential.md
# section 2.7): a credential's reach is now an allowed-workspace SET rather
# than one workspace, so an unlimited-lifetime PAT's blast radius on leak is
# larger than it used to be. REST defaults a create request that omits an
# explicit expiry choice to this many days; an explicit `null` choice is
# still honored as unlimited. PATs get no sliding/renewal-on-use -- see
# ApiTokenCreateRequest.resolved_expires_at in backend/schemas/auth.py.
ALLOWED_PAT_EXPIRES_IN_DAYS = (30, 90, 180, 365)
DEFAULT_PAT_EXPIRES_IN_DAYS = 90


class ApiTokenServiceError(RuntimeError):
    error_code = "api_token_failed"


class ApiTokenNotFoundError(ApiTokenServiceError):
    error_code = "api_token_not_found"


class InvalidApiTokenRequestError(ApiTokenServiceError):
    error_code = "invalid_api_token_request"


class InvalidApiTokenError(ApiTokenServiceError):
    error_code = "invalid_api_token"


class ApiTokenWorkspaceAccessError(ApiTokenServiceError):
    error_code = "api_token_workspace_access_denied"


class InvalidApiTokenCursorError(InvalidApiTokenRequestError):
    error_code = "invalid_access_key_cursor"


@dataclass(frozen=True)
class WorkspaceGrantRequest:
    """One requested allowed-workspace entry for PAT create/update (T4).

    ``scopes`` is optional: omitted means "use the token's own ceiling
    ``scopes``"; when given explicitly it must be a subset of the ceiling
    (D-01v3-f, checked by ``require_scopes_subset_of_ceiling``).
    """

    workspace_id: UUID
    scopes: list[str] | None = None


@dataclass(frozen=True)
class ApiTokenWorkspaceGrant:
    """One row of a PAT's live allowed-workspace set, enriched for REST
    display -- title/slug from ``Workspace``, ``membership_role`` from the
    owning user's live ``WorkspaceMembership`` (``None`` if membership was
    lost after the grant was made)."""

    workspace_id: UUID
    title: str
    slug: str
    membership_role: str | None
    granted_scopes: list[str]
    sync_state: str


@dataclass(frozen=True)
class ApiTokenRecord:
    id: UUID
    user_id: UUID
    workspaces: list[ApiTokenWorkspaceGrant]
    name: str
    token_preview: str | None
    scopes: list[str]
    catalog_app_id: str | None
    catalog_app_label_snapshot: str | None
    catalog_revision: str | None
    mcp_client_type: str | None
    mcp_client_label: str | None
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True)
class ApiTokenIssueResult:
    api_token: ApiTokenRecord
    plain_token: str


@dataclass(frozen=True)
class ApiTokenActor:
    """The identity + candidate workspace set a PAT resolves to at verification
    time.

    D-01v2: role is deliberately absent here -- authentication no longer
    confirms a role for any one workspace up front. ``workspace_ids`` is the
    live allowed set (``api_token_workspaces`` rows with ``removed_at IS
    NULL``); the caller resolves a specific target workspace's role later via
    ``backend.policy.trust_boundaries.actor_with_target_workspace_scope``.
    """

    user: AuthenticatedUser
    token_id: UUID
    workspace_ids: tuple[UUID, ...]
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class AccessKeyListItem:
    api_token: ApiTokenRecord
    status: AccessKeyStatus


@dataclass(frozen=True)
class AccessKeyStatusCounts:
    active: int
    expired: int
    revoked: int


@dataclass(frozen=True)
class AccessKeyListPage:
    data: list[AccessKeyListItem]
    next_cursor: str | None
    evaluated_at: datetime
    total_count: int
    filtered_count: int
    status_counts: AccessKeyStatusCounts


@dataclass(frozen=True)
class _AccessKeyCursor:
    evaluated_at: datetime
    created_at: datetime
    token_id: UUID


@dataclass(frozen=True)
class ApiTokenService:
    session_factory: object
    credential_hasher: CredentialHasher
    audit_service: AuditService = field(default_factory=AuditService)
    cursor_secret: str | None = None
    catalog_repository: CatalogRepository | None = None

    def list_tokens(self, user_id: UUID) -> list[ApiTokenRecord]:
        with self.session_factory() as session:
            tokens = session.scalars(
                select(ApiToken)
                .where(ApiToken.user_id == user_id)
                .options(selectinload(ApiToken.workspaces))
                # id is a deterministic tie-break: two tokens created within the
                # same created_at tick would otherwise come back in an arbitrary
                # order, giving the UI an unstable list and flaking order-sensitive
                # tests under full-suite timing (merged I14).
                .order_by(ApiToken.created_at.desc(), ApiToken.id.desc())
            ).all()
            return api_token_records(session, list(tokens))

    def create_token(
        self,
        *,
        user_id: UUID,
        workspace_grants: list[WorkspaceGrantRequest],
        name: str,
        scopes: list[str],
        expires_at: datetime | None = None,
        mcp_client_type: str | None = None,
        catalog_app_id: str | None = None,
        custom_app_label: str | None = None,
    ) -> ApiTokenIssueResult:
        requested_name = normalize_token_name(name)
        normalized_scopes = normalize_scopes(scopes)
        require_unique_workspace_grants(workspace_grants)
        association = resolve_catalog_app_association(
            self.catalog_repository or get_mcp_app_catalog_repository(),
            catalog_app_id=catalog_app_id,
            custom_app_label=custom_app_label,
            legacy_mcp_client_type=mcp_client_type,
        )
        with self.session_factory.begin() as session:
            acquire_write_intent(session)
            user = session.get(User, user_id)
            if user is None or user.status != "active":
                raise InvalidApiTokenRequestError("active user is required")
            resolved_grants: list[tuple[UUID, list[str]]] = []
            for grant in workspace_grants:
                access = require_api_token_workspace_access(
                    session,
                    user_id=user_id,
                    workspace_id=grant.workspace_id,
                )
                grant_scopes = (
                    normalize_scopes(grant.scopes)
                    if grant.scopes is not None
                    else normalized_scopes
                )
                require_scopes_subset_of_ceiling(grant_scopes, normalized_scopes)
                validate_scopes_for_workspace_role(grant_scopes, access.role)
                resolved_grants.append((grant.workspace_id, grant_scopes))
            token_name = unique_active_token_name(
                session,
                user_id=user_id,
                requested_name=requested_name,
            )
            plain_token = self.credential_hasher.generate_api_token()
            model = ApiToken(
                user_id=user_id,
                name=token_name,
                token_lookup_hash=self.credential_hasher.token_lookup_hash(plain_token),
                hashed_token=self.credential_hasher.hash_api_token(plain_token),
                token_preview=plain_token[:TOKEN_PREVIEW_LENGTH],
                scopes=normalized_scopes,
                catalog_app_id=association.catalog_app_id,
                catalog_app_label_snapshot=association.label_snapshot,
                catalog_revision=association.catalog_revision,
                mcp_client_type=association.legacy_client_type,
                mcp_client_label=association.label_snapshot,
                expires_at=expires_at,
                revoked_at=None,
                last_used_at=None,
            )
            session.add(model)
            session.flush()
            # D-01v2/T4: one control-plane index row per requested workspace
            # grant. Each fans out to its own workspace-plane replica later
            # (T4.5) -- this only writes the control side, revision=1,
            # sync_state="pending".
            grant_rows = [
                ApiTokenWorkspace(
                    token_id=model.id,
                    workspace_id=workspace_id,
                    desired_scopes=grant_scopes,
                    revision=1,
                    sync_state="pending",
                )
                for workspace_id, grant_scopes in resolved_grants
            ]
            session.add_all(grant_rows)
            session.flush()
            model.workspaces = grant_rows
            # T4.5 (section 2.6): one fan-out request per newly-created
            # control-index row, queued for a best-effort apply right after
            # this transaction commits.
            for grant_row in grant_rows:
                register_fan_out(
                    session,
                    CredentialReplicaFanOutRequest(
                        credential_kind="pat",
                        credential_id=model.id,
                        workspace_id=grant_row.workspace_id,
                        user_id=user_id,
                        desired_scopes=tuple(grant_row.desired_scopes),
                        revision=grant_row.revision,
                        removed_at=grant_row.removed_at,
                    ),
                )
            self.audit_service.emit(
                session=session,
                # Account-scoped, not workspace-scoped: a created credential's
                # allowed set can name several workspaces at once, so there is
                # no single workspace this event belongs to.
                workspace_id=None,
                actor_kind="human",
                actor_id=str(user_id),
                action="auth.api_token.created",
                target_kind="api_token",
                target_id=model.id,
                payload={
                    "workspace_ids": [str(w) for w, _ in resolved_grants],
                },
                event_summary="API token created",
                retention_class="sensitive",
            )
            return ApiTokenIssueResult(
                api_token=api_token_record(session, model),
                plain_token=plain_token,
            )

    def update_token(
        self,
        *,
        user_id: UUID,
        token_id: UUID,
        name: str | None = None,
        workspace_grants: list[WorkspaceGrantRequest] | None = None,
    ) -> ApiTokenRecord:
        if name is None and workspace_grants is None:
            raise InvalidApiTokenRequestError(
                "at least one of name or workspace_grants is required"
            )
        requested_name = normalize_token_name(name) if name is not None else None
        if workspace_grants is not None:
            if not workspace_grants:
                raise InvalidApiTokenRequestError(
                    "at least one workspace grant is required"
                )
            require_unique_workspace_grants(workspace_grants)
        with self.session_factory.begin() as session:
            acquire_write_intent(session)
            model = session.scalar(api_token_by_id_for_update_statement(token_id))
            if model is None or model.user_id != user_id:
                raise ApiTokenNotFoundError("API token not found")
            if requested_name is not None:
                if model.revoked_at is None:
                    model.name = unique_active_token_name(
                        session,
                        user_id=user_id,
                        requested_name=requested_name,
                        exclude_token_id=token_id,
                    )
                else:
                    model.name = requested_name
            if workspace_grants is not None:
                self._replace_workspace_grants(
                    session,
                    user_id=user_id,
                    model=model,
                    workspace_grants=workspace_grants,
                )
            session.flush()
            return api_token_record(session, model)

    def _replace_workspace_grants(
        self,
        session: Session,
        *,
        user_id: UUID,
        model: ApiToken,
        workspace_grants: list[WorkspaceGrantRequest],
    ) -> None:
        """Full-set REPLACE of a token's allowed-workspace set (T4, PATCH).

        Every named grant is re-validated (membership + scope subset) the
        same way ``create_token`` validates a new one -- a role can have
        changed since the grant was first made. New workspace ids get a new
        row (``revision=1``, ``sync_state=pending``); existing ones bump
        ``revision``/reset to ``pending`` only if ``desired_scopes`` actually
        changed; live rows dropped from the new set are soft-removed
        (``removed_at``), never hard-deleted, per D-01v3-c/§2.6.
        """

        live_grants = {
            grant.workspace_id: grant
            for grant in session.scalars(
                select(ApiTokenWorkspace).where(
                    ApiTokenWorkspace.token_id == model.id,
                    ApiTokenWorkspace.removed_at.is_(None),
                )
            )
        }
        new_workspace_ids = {grant.workspace_id for grant in workspace_grants}
        ceiling_scopes = list(model.scopes)
        added_workspace_ids: list[UUID] = []
        removed_workspace_ids: list[UUID] = []
        # T4.5: every row whose revision this method bumps (added, changed,
        # or soft-removed) needs its own fan-out request -- rows left
        # untouched (already live, same scopes, still requested) do not.
        fan_out_rows: list[ApiTokenWorkspace] = []
        for grant in workspace_grants:
            access = require_api_token_workspace_access(
                session,
                user_id=user_id,
                workspace_id=grant.workspace_id,
            )
            grant_scopes = (
                normalize_scopes(grant.scopes)
                if grant.scopes is not None
                else ceiling_scopes
            )
            require_scopes_subset_of_ceiling(grant_scopes, ceiling_scopes)
            validate_scopes_for_workspace_role(grant_scopes, access.role)
            existing = live_grants.get(grant.workspace_id)
            if existing is None:
                new_row = ApiTokenWorkspace(
                    token_id=model.id,
                    workspace_id=grant.workspace_id,
                    desired_scopes=grant_scopes,
                    revision=1,
                    sync_state="pending",
                )
                session.add(new_row)
                added_workspace_ids.append(grant.workspace_id)
                fan_out_rows.append(new_row)
            elif list(existing.desired_scopes) != grant_scopes:
                existing.desired_scopes = grant_scopes
                existing.revision += 1
                existing.sync_state = "pending"
                fan_out_rows.append(existing)
        for workspace_id, existing in live_grants.items():
            if workspace_id in new_workspace_ids:
                continue
            existing.removed_at = utc_now()
            existing.revision += 1
            existing.sync_state = "pending"
            removed_workspace_ids.append(workspace_id)
            fan_out_rows.append(existing)
        for fan_out_row in fan_out_rows:
            register_fan_out(
                session,
                CredentialReplicaFanOutRequest(
                    credential_kind="pat",
                    credential_id=model.id,
                    workspace_id=fan_out_row.workspace_id,
                    user_id=user_id,
                    desired_scopes=tuple(fan_out_row.desired_scopes),
                    revision=fan_out_row.revision,
                    removed_at=fan_out_row.removed_at,
                ),
            )
        if added_workspace_ids or removed_workspace_ids:
            self.audit_service.emit(
                session=session,
                workspace_id=None,
                actor_kind="human",
                actor_id=str(user_id),
                action="auth.api_token.workspaces_changed",
                target_kind="api_token",
                target_id=model.id,
                payload={
                    "added": [str(w) for w in added_workspace_ids],
                    "removed": [str(w) for w in removed_workspace_ids],
                },
                event_summary="API token workspace grants changed",
                retention_class="sensitive",
            )

    def revoke_token(self, *, user_id: UUID, token_id: UUID) -> ApiTokenRecord:
        with self.session_factory.begin() as session:
            acquire_write_intent(session)
            model = session.scalar(api_token_by_id_for_update_statement(token_id))
            if model is None or model.user_id != user_id:
                raise ApiTokenNotFoundError("API token not found")
            if model.revoked_at is None:
                model.revoked_at = utc_now()
                session.flush()
            return api_token_record(session, model)

    def delete_revoked_token(self, *, user_id: UUID, token_id: UUID) -> UUID:
        """``DELETE /auth/tokens/{id}/record`` -- the account-scoped inactive
        record deletion (T4 removed the workspace-scoped
        ``delete_inactive_token``/settings-access-keys surface entirely; this
        is now the only REST path for deleting a token row). Accepts either
        an expired or a revoked token, matching the deleted route's
        "expired-or-revoked" contract exactly (an earlier draft of this
        method only accepted revoked, which would have quietly removed the
        only way to delete a merely-expired-but-not-yet-revoked PAT record).
        Still applies the Workspace ownership and audit invariants the
        deleted Settings surface used. A legacy unbound row is retained:
        without a Workspace there is no safe audit scope for a destructive
        operation.
        """
        with self.session_factory.begin() as session:
            acquire_write_intent(session)
            model = session.scalar(api_token_by_id_for_update_statement(token_id))
            if model is None or model.user_id != user_id:
                raise ApiTokenNotFoundError("API token not found")
            evaluated_at = utc_now()
            token_status = access_key_status(model, evaluated_at=evaluated_at)
            if token_status == "active":
                raise InvalidApiTokenRequestError(
                    "API token must be revoked or expired before deletion"
                )
            workspace_id = _primary_workspace_id(model)
            if workspace_id is None:
                raise InvalidApiTokenRequestError(
                    "Unbound legacy API token records cannot be deleted"
                )
            require_api_token_workspace_access(
                session,
                user_id=user_id,
                workspace_id=workspace_id,
            )
            self._emit_record_deleted_audit(
                session=session,
                user_id=user_id,
                workspace_id=workspace_id,
                token_id=model.id,
                inactive_reason=token_status,
            )
            session.delete(model)
            return token_id

    def _emit_record_deleted_audit(
        self,
        *,
        session: Session,
        user_id: UUID,
        workspace_id: UUID,
        token_id: UUID,
        inactive_reason: AccessKeyStatus,
    ) -> None:
        """Write the shared, credential-free access-key deletion event."""

        self.audit_service.emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind="human",
            actor_id=str(user_id),
            action="auth.api_token.record_deleted",
            target_kind="api_token",
            target_id=token_id,
            payload={
                "workspace_id": str(workspace_id),
                "inactive_reason": inactive_reason,
            },
            event_summary="Access key record deleted",
            retention_class="sensitive",
        )

    def list_user_tokens(
        self,
        *,
        user_id: UUID,
        status: AccessKeyListStatus = "active",
        q: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> AccessKeyListPage:
        """Return one query-bound, deterministic ``GET /auth/tokens`` page.

        D-01v2/T4: account-scoped, not Workspace-scoped -- a PAT's allowed
        set can span many workspaces now, so "the Settings list for
        workspace X" is no longer a coherent question. This replaces the
        deleted workspace-scoped ``list_workspace_tokens``/
        ``GET /workspaces/{id}/settings/access-keys``; see
        .claude/plan/260909-p10-3-multi-workspace-credential.md section 2.4.
        """

        with self.session_factory() as session:
            # The counts, status facets, and page are one projection.  Pin the
            # database snapshot at entry, before validation or state reads.
            acquire_consistent_read_snapshot(session)
            normalized_status = normalize_access_key_list_status(status)
            normalized_q = normalize_access_key_search(q)
            if limit < 1 or limit > 100:
                raise InvalidApiTokenRequestError("limit must be between 1 and 100")
            if cursor is None:
                evaluated_at = utc_now()
                decoded_cursor = None
            else:
                decoded_cursor = self._decode_access_key_cursor(
                    cursor,
                    user_id=user_id,
                    status=normalized_status,
                    q=normalized_q,
                    limit=limit,
                )
                evaluated_at = decoded_cursor.evaluated_at

            visibility = (ApiToken.user_id == user_id,)
            search_predicate = access_key_search_predicate(normalized_q)
            total_count = count_api_tokens(session, *visibility)
            status_counts = AccessKeyStatusCounts(
                active=count_api_tokens(
                    session,
                    *visibility,
                    search_predicate,
                    access_key_status_predicate("active", evaluated_at),
                ),
                expired=count_api_tokens(
                    session,
                    *visibility,
                    search_predicate,
                    access_key_status_predicate("expired", evaluated_at),
                ),
                revoked=count_api_tokens(
                    session,
                    *visibility,
                    search_predicate,
                    access_key_status_predicate("revoked", evaluated_at),
                ),
            )
            selected_status = access_key_status_predicate(
                normalized_status,
                evaluated_at,
            )
            filtered_count = count_api_tokens(
                session,
                *visibility,
                search_predicate,
                selected_status,
            )

            predicates = [*visibility, search_predicate, selected_status]
            if decoded_cursor is not None:
                predicates.append(
                    or_(
                        ApiToken.created_at < decoded_cursor.created_at,
                        and_(
                            ApiToken.created_at == decoded_cursor.created_at,
                            ApiToken.id < decoded_cursor.token_id,
                        ),
                    )
                )
            models = list(
                session.scalars(
                    select(ApiToken)
                    .where(*predicates)
                    .options(selectinload(ApiToken.workspaces))
                    .order_by(ApiToken.created_at.desc(), ApiToken.id.desc())
                    .limit(limit + 1)
                ).all()
            )
            has_next = len(models) > limit
            page_models = models[:limit]
            records = api_token_records(session, page_models)
            items = [
                AccessKeyListItem(
                    api_token=record,
                    status=access_key_status(model, evaluated_at=evaluated_at),
                )
                for model, record in zip(page_models, records, strict=True)
            ]
            next_cursor = None
            if has_next and page_models:
                last = page_models[-1]
                next_cursor = self._encode_access_key_cursor(
                    user_id=user_id,
                    status=normalized_status,
                    q=normalized_q,
                    limit=limit,
                    evaluated_at=evaluated_at,
                    created_at=last.created_at,
                    token_id=last.id,
                )
            return AccessKeyListPage(
                data=items,
                next_cursor=next_cursor,
                evaluated_at=evaluated_at,
                total_count=total_count,
                filtered_count=filtered_count,
                status_counts=status_counts,
            )

    def authenticate_token(self, token: str) -> ApiTokenActor:
        try:
            lookup_hash = self.credential_hasher.token_lookup_hash(token)
        except ValueError as exc:
            raise InvalidApiTokenError("API token is invalid") from exc
        with self.session_factory.begin() as session:
            acquire_write_intent(session)
            model = session.scalar(
                api_token_by_lookup_hash_for_update_statement(lookup_hash)
            )
            if model is None:
                raise InvalidApiTokenError("API token is invalid")
            if model.revoked_at is not None:
                raise InvalidApiTokenError("API token is revoked")
            if (
                model.expires_at is not None
                and to_aware_utc(model.expires_at) <= utc_now()
            ):
                raise InvalidApiTokenError("API token is expired")
            if not self.credential_hasher.verify_api_token(token, model.hashed_token):
                raise InvalidApiTokenError("API token is invalid")
            user = session.get(User, model.user_id)
            if user is None or user.status != "active":
                raise InvalidApiTokenError("API token user is inactive")
            workspace_ids = tuple(
                session.scalars(
                    select(ApiTokenWorkspace.workspace_id)
                    .where(
                        ApiTokenWorkspace.token_id == model.id,
                        ApiTokenWorkspace.removed_at.is_(None),
                    )
                    .order_by(ApiTokenWorkspace.workspace_id)
                ).all()
            )
            if not workspace_ids:
                raise InvalidApiTokenError("API token has no allowed Workspace")
            model.last_used_at = utc_now()
            session.flush()
            return ApiTokenActor(
                user=authenticated_user(user),
                token_id=model.id,
                workspace_ids=workspace_ids,
                scopes=tuple(model.scopes),
            )

    def _encode_access_key_cursor(
        self,
        *,
        user_id: UUID,
        status: AccessKeyListStatus,
        q: str,
        limit: int,
        evaluated_at: datetime,
        created_at: datetime,
        token_id: UUID,
    ) -> str:
        payload = {
            "v": ACCESS_KEY_CURSOR_VERSION,
            "sort": ACCESS_KEY_SORT_VERSION,
            # D-01v2/T4: this cursor now walks a user-scoped (account-wide)
            # listing, not a workspace-scoped one -- the tamper-evident scope
            # key is user_id, not workspace_id.
            "user_id": str(user_id),
            "status": status,
            "q": q,
            "limit": limit,
            "evaluated_at": cursor_datetime(evaluated_at),
            "created_at": cursor_datetime(created_at),
            "token_id": str(token_id),
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(
                payload,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).rstrip(b"=")
        signature = hmac.new(
            self._access_key_cursor_secret(),
            encoded,
            hashlib.sha256,
        ).digest()
        encoded_signature = base64.urlsafe_b64encode(signature).rstrip(b"=")
        return f"{encoded.decode('ascii')}.{encoded_signature.decode('ascii')}"

    def _decode_access_key_cursor(
        self,
        cursor: str,
        *,
        user_id: UUID,
        status: AccessKeyListStatus,
        q: str,
        limit: int,
    ) -> _AccessKeyCursor:
        try:
            encoded_text, signature_text = cursor.split(".", 1)
            encoded = encoded_text.encode("ascii")
            signature = decode_base64url(signature_text)
            expected_signature = hmac.new(
                self._access_key_cursor_secret(),
                encoded,
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(signature, expected_signature):
                raise ValueError("cursor signature mismatch")
            payload = json.loads(decode_base64url(encoded_text).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("cursor payload must be an object")
            expected = {
                "v": ACCESS_KEY_CURSOR_VERSION,
                "sort": ACCESS_KEY_SORT_VERSION,
                "user_id": str(user_id),
                "status": status,
                "q": q,
                "limit": limit,
            }
            if any(payload.get(key) != value for key, value in expected.items()):
                raise ValueError("cursor query mismatch")
            evaluated_at = parse_cursor_datetime(payload["evaluated_at"])
            created_at = parse_cursor_datetime(payload["created_at"])
            token_id = UUID(payload["token_id"])
        except (
            KeyError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise InvalidApiTokenCursorError("access key cursor is invalid") from exc
        return _AccessKeyCursor(
            evaluated_at=evaluated_at,
            created_at=created_at,
            token_id=token_id,
        )

    def _access_key_cursor_secret(self) -> bytes:
        if not self.cursor_secret:
            raise InvalidApiTokenCursorError("access key cursor signing is unavailable")
        return self.cursor_secret.encode("utf-8")


def api_token_by_id_for_update_statement(token_id: UUID):
    return select(ApiToken).where(ApiToken.id == token_id).with_for_update()


def api_token_by_lookup_hash_for_update_statement(lookup_hash: str):
    return (
        select(ApiToken)
        .where(ApiToken.token_lookup_hash == lookup_hash)
        .with_for_update()
    )


def _primary_workspace_id(token: ApiToken) -> UUID | None:
    """The lowest live allowed workspace_id, used only by the legacy
    ``delete_revoked_token`` (``/auth/tokens/{id}/record``) audit trail,
    which is Workspace-scoped by convention even though a T4 credential's
    allowed set can now legitimately hold several workspaces. Only counts a
    *live* (``removed_at IS NULL``) grant -- once a token's every grant is
    removed it is the "unbound legacy token" case callers already fail
    closed on.
    """

    live = [grant for grant in token.workspaces if grant.removed_at is None]
    if not live:
        return None
    return min(live, key=lambda grant: grant.workspace_id).workspace_id


def api_token_records(session: Session, tokens: list[ApiToken]) -> list[ApiTokenRecord]:
    """Batch-build display records for a page of tokens (T4).

    Each live grant is enriched with the granting workspace's title/slug and
    the owning user's live ``WorkspaceMembership`` role (``None`` if
    membership was since lost) -- fetched here in two bounded queries across
    the whole page rather than per token/grant.
    """

    all_workspace_ids: set[UUID] = set()
    all_user_ids: set[UUID] = set()
    for token in tokens:
        all_user_ids.add(token.user_id)
        for grant in token.workspaces:
            if grant.removed_at is None:
                all_workspace_ids.add(grant.workspace_id)

    workspaces: dict[UUID, Workspace] = {}
    roles: dict[tuple[UUID, UUID], str] = {}
    if all_workspace_ids:
        workspaces = {
            workspace.id: workspace
            for workspace in session.scalars(
                select(Workspace).where(Workspace.id.in_(all_workspace_ids))
            )
        }
        roles = {
            (membership.user_id, membership.workspace_id): membership.role
            for membership in session.scalars(
                select(WorkspaceMembership).where(
                    WorkspaceMembership.user_id.in_(all_user_ids),
                    WorkspaceMembership.workspace_id.in_(all_workspace_ids),
                )
            )
        }

    records: list[ApiTokenRecord] = []
    for token in tokens:
        grants: list[ApiTokenWorkspaceGrant] = []
        live_grants = sorted(
            (grant for grant in token.workspaces if grant.removed_at is None),
            key=lambda grant: grant.workspace_id,
        )
        for grant in live_grants:
            workspace = workspaces.get(grant.workspace_id)
            if workspace is None:
                # Deleted underneath a not-yet-cascaded read -- FK CASCADE
                # removes this row too, so this should be transient/rare.
                # Skip rather than crash the whole page.
                continue
            grants.append(
                ApiTokenWorkspaceGrant(
                    workspace_id=grant.workspace_id,
                    title=workspace.title,
                    slug=workspace.slug,
                    membership_role=roles.get((token.user_id, grant.workspace_id)),
                    granted_scopes=list(grant.desired_scopes),
                    sync_state=grant.sync_state,
                )
            )
        records.append(
            ApiTokenRecord(
                id=token.id,
                user_id=token.user_id,
                workspaces=grants,
                name=token.name,
                token_preview=token.token_preview,
                scopes=list(token.scopes),
                catalog_app_id=token.catalog_app_id,
                catalog_app_label_snapshot=token.catalog_app_label_snapshot,
                catalog_revision=token.catalog_revision,
                mcp_client_type=token.mcp_client_type,
                mcp_client_label=token.mcp_client_label,
                created_at=token.created_at,
                last_used_at=token.last_used_at,
                expires_at=token.expires_at,
                revoked_at=token.revoked_at,
            )
        )
    return records


def api_token_record(session: Session, token: ApiToken) -> ApiTokenRecord:
    return api_token_records(session, [token])[0]


def normalize_token_name(name: str) -> str:
    return normalize_short_text(name, field="name", max_length=128)


def normalize_scopes(scopes: list[str]) -> list[str]:
    normalized = sorted({scope.strip() for scope in scopes if scope.strip()})
    unknown = [scope for scope in normalized if scope not in ALLOWED_API_TOKEN_SCOPES]
    if unknown:
        raise InvalidApiTokenRequestError(f"unsupported API token scope: {unknown[0]}")
    if not normalized:
        raise InvalidApiTokenRequestError("at least one API token scope is required")
    return normalized


@dataclass(frozen=True)
class CatalogAppAssociation:
    """Immutable, display-only catalog metadata captured at issuance."""

    catalog_app_id: str | None
    label_snapshot: str | None
    catalog_revision: str
    legacy_client_type: str | None


def resolve_catalog_app_association(
    repository: CatalogRepository,
    *,
    catalog_app_id: str | None,
    custom_app_label: str | None,
    legacy_mcp_client_type: str | None,
) -> CatalogAppAssociation:
    """Resolve new and legacy request fields through the canonical catalog.

    ``catalog_app_id`` is intentionally only a display association. This
    function validates that association and snapshots its label, but token
    authentication never consults the catalog. The one hard-coded value below
    is a compatibility adapter for the historical ``generic`` request value;
    it is not an application allowlist and maps to a null catalog association.
    """

    snapshot = repository.snapshot()
    requested_app_id = _normalized_optional_text(catalog_app_id)
    legacy_type = _normalized_optional_text(legacy_mcp_client_type)
    requested_custom_label = custom_app_label
    if isinstance(custom_app_label, str) and not custom_app_label.strip(
        SHORT_TEXT_SPACES
    ):
        requested_custom_label = None
    if requested_custom_label is not None:
        requested_custom_label = normalize_short_text(
            requested_custom_label, field="custom_app_label", max_length=160
        )

    if legacy_type == LEGACY_GENERIC_MCP_CLIENT_TYPE:
        if requested_app_id is not None:
            raise InvalidApiTokenRequestError(
                "generic MCP client cannot claim a catalog app"
            )
        return CatalogAppAssociation(
            catalog_app_id=None,
            label_snapshot=requested_custom_label or LEGACY_GENERIC_MCP_CLIENT_LABEL,
            catalog_revision=snapshot.catalog_revision,
            legacy_client_type=LEGACY_GENERIC_MCP_CLIENT_TYPE,
        )

    if requested_app_id is not None and legacy_type not in {
        None,
        requested_app_id,
    }:
        raise InvalidApiTokenRequestError(
            "catalog_app_id and mcp_client_type must identify the same app"
        )
    effective_app_id = requested_app_id or legacy_type
    if effective_app_id is None:
        return CatalogAppAssociation(
            catalog_app_id=None,
            label_snapshot=requested_custom_label,
            catalog_revision=snapshot.catalog_revision,
            legacy_client_type=(
                LEGACY_GENERIC_MCP_CLIENT_TYPE
                if requested_custom_label is not None
                else None
            ),
        )

    app = repository.find_app(effective_app_id)
    if app is None or app.lifecycle != "active":
        field_name = "MCP client type" if requested_app_id is None else "catalog app"
        raise InvalidApiTokenRequestError(
            f"unsupported {field_name}: {effective_app_id}"
        )
    if requested_custom_label is not None:
        raise InvalidApiTokenRequestError(
            "custom app label is only allowed without catalog_app_id"
        )
    return CatalogAppAssociation(
        catalog_app_id=app.id,
        label_snapshot=app.labels.en,
        catalog_revision=snapshot.catalog_revision,
        legacy_client_type=app.id,
    )


def _normalized_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def unique_active_token_name(
    session: Session,
    *,
    user_id: UUID,
    requested_name: str,
    exclude_token_id: UUID | None = None,
) -> str:
    """Disambiguate ``requested_name`` against this user's other active PATs.

    D-01v2/T4: checked within ``(user)`` alone now, not ``(user,
    workspace)`` -- a credential's allowed set can span several workspaces,
    so there is no longer a single workspace to scope uniqueness to. An
    already-existing duplicate name from before this change is left as-is
    (not retroactively suffixed); only a new/renamed collision gets one.
    """

    statement = select(ApiToken.name).where(
        ApiToken.user_id == user_id,
        ApiToken.revoked_at.is_(None),
    )
    if exclude_token_id is not None:
        statement = statement.where(ApiToken.id != exclude_token_id)
    existing = {name.casefold() for name in session.scalars(statement)}
    if requested_name.casefold() not in existing:
        return requested_name

    for suffix in range(2, 1000):
        suffix_text = f" {suffix}"
        base = requested_name[: 128 - len(suffix_text)].rstrip()
        candidate = f"{base}{suffix_text}"
        if candidate.casefold() not in existing:
            return candidate
    raise InvalidApiTokenRequestError("API token name is too ambiguous")


def require_api_token_workspace_access(
    session: Session,
    *,
    user_id: UUID,
    workspace_id: UUID,
):
    access = get_workspace_access(
        session,
        user_id=user_id,
        workspace_id=workspace_id,
    )
    if access is None:
        raise ApiTokenWorkspaceAccessError("Workspace membership is required")
    return access


def validate_scopes_for_workspace_role(scopes: list[str], role: str) -> None:
    if {"mcp:write", "mcp:agent"} & set(scopes) and not role_allows(role, "editor"):
        raise InvalidApiTokenRequestError(
            "editor Workspace role is required for write or agent scopes"
        )


def require_scopes_subset_of_ceiling(scopes: list[str], ceiling: list[str]) -> None:
    """D-01v3-f: a workspace grant's explicit scopes must not exceed the
    token's own ceiling ``scopes``."""

    if not set(scopes) <= set(ceiling):
        raise InvalidApiTokenRequestError(
            "workspace grant scopes must be a subset of the token's scopes"
        )


def require_unique_workspace_grants(grants: list[WorkspaceGrantRequest]) -> None:
    validate_list_count(grants, field="workspace_grants", max_items=256)
    if not grants:
        raise InvalidApiTokenRequestError("at least one workspace grant is required")
    workspace_ids = [grant.workspace_id for grant in grants]
    if len(set(workspace_ids)) != len(workspace_ids):
        raise InvalidApiTokenRequestError("duplicate workspace_id in workspace grants")


def normalize_access_key_list_status(value: str) -> AccessKeyListStatus:
    if value not in {"active", "inactive", "expired", "revoked", "all"}:
        raise InvalidApiTokenRequestError("unsupported access key status")
    return value  # type: ignore[return-value]


def normalize_access_key_search(value: str | None) -> str:
    if value is None:
        return ""
    normalized = " ".join(value.strip().split()).casefold()
    if len(normalized) > 128:
        raise InvalidApiTokenRequestError("access key search is too long")
    return normalized


def access_key_search_predicate(q: str):
    if not q:
        return ApiToken.id.is_not(None)
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return func.lower(ApiToken.name).like(f"%{escaped}%", escape="\\")


def access_key_status_predicate(status: AccessKeyListStatus, evaluated_at: datetime):
    active = and_(
        ApiToken.revoked_at.is_(None),
        or_(ApiToken.expires_at.is_(None), ApiToken.expires_at > evaluated_at),
    )
    expired = and_(
        ApiToken.revoked_at.is_(None),
        ApiToken.expires_at.is_not(None),
        ApiToken.expires_at <= evaluated_at,
    )
    revoked = ApiToken.revoked_at.is_not(None)
    if status == "active":
        return active
    if status == "expired":
        return expired
    if status == "revoked":
        return revoked
    if status == "inactive":
        return or_(expired, revoked)
    return or_(active, expired, revoked)


def access_key_status(token: ApiToken, *, evaluated_at: datetime) -> AccessKeyStatus:
    if token.revoked_at is not None:
        return "revoked"
    if token.expires_at is not None and to_aware_utc(token.expires_at) <= evaluated_at:
        return "expired"
    return "active"


def count_api_tokens(session: Session, *predicates) -> int:
    # D-01v2/T4: user-scoped listing predicates only ever touch ApiToken
    # columns now (no per-workspace filter), so this no longer needs the
    # ApiTokenWorkspace join -- and therefore no DISTINCT to undo the
    # one-row-per-grant fan-out that join used to introduce.
    return int(
        session.scalar(select(func.count()).select_from(ApiToken).where(*predicates))
        or 0
    )


def cursor_datetime(value: datetime) -> str:
    return to_aware_utc(value).isoformat().replace("+00:00", "Z")


def parse_cursor_datetime(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("cursor datetime must be a string")
    return to_aware_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def decode_base64url(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("invalid base64url value")
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(
        value + padding,
        altchars=b"-_",
        validate=True,
    )


def to_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)

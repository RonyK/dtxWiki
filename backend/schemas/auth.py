from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from backend.domain.identity_policy import (
    normalize_display_name,
    normalize_email,
    validate_password,
)
from backend.domain.input_policy import normalize_short_text
from backend.schemas.common import RESTResponseBase
from backend.services.api_token_service import (
    ALLOWED_API_TOKEN_SCOPES,
    ALLOWED_PAT_EXPIRES_IN_DAYS,
    DEFAULT_PAT_EXPIRES_IN_DAYS,
)
from backend.services.session_service import MIN_PASSWORD_LENGTH

EmailInput = Annotated[
    str,
    BeforeValidator(normalize_email),
    Field(
        description=(
            "One email address, normalized before validating at most 254 UTF-8 "
            "bytes; local part at most 64 UTF-8 bytes."
        ),
        json_schema_extra={"x-normalized-maxBytes": 254, "x-localPart-maxBytes": 64},
    ),
]
NewPasswordInput = Annotated[
    str,
    BeforeValidator(lambda value: validate_password(value, new=True)),
    Field(min_length=MIN_PASSWORD_LENGTH, max_length=128),
]
CurrentPasswordInput = Annotated[
    str,
    BeforeValidator(validate_password),
    Field(min_length=1, max_length=128),
]


class SignupRequest(BaseModel):
    email: EmailInput
    password: NewPasswordInput
    display_name: str | None = Field(
        default=None,
        description=(
            "Optional display name; at most 32 Unicode code points after "
            "surrounding spaces are removed. Empty values use an automatic name."
        ),
        json_schema_extra={"x-normalized-maxLength": 32},
    )
    # Fail closed like PasswordResetConfirmRequest below: the SPA always sends
    # an explicit value (AuthScreen.onSubmit), so a caller that omits this
    # field is a script/CI client that never opted into a 30-day renewable
    # browser credential. Defaulting to True would silently hand every such
    # caller session_remembered_ttl_seconds instead of session_ttl_seconds.
    remember_me: bool = False

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _normalize_display_name(self):
        self.display_name = normalize_display_name(self.display_name, self.email)
        return self


class LoginRequest(BaseModel):
    email: EmailInput
    password: CurrentPasswordInput
    # See SignupRequest.remember_me: omitting the field must not upgrade a
    # caller to the remembered (30-day, renewable) session policy.
    remember_me: bool = False

    model_config = ConfigDict(extra="forbid")


class PasswordChangeRequest(BaseModel):
    current_password: CurrentPasswordInput
    new_password: NewPasswordInput

    model_config = ConfigDict(extra="forbid")


class ReauthenticationRequest(BaseModel):
    current_password: CurrentPasswordInput

    model_config = ConfigDict(extra="forbid")


class PasswordResetConfirmRequest(BaseModel):
    token: str = Field(min_length=16, max_length=1024)
    new_password: NewPasswordInput
    # Fail closed: a caller that omits this field (bypassing the SPA, which
    # always sends an explicit value) gets credential revocation, not
    # silence. The issue-time flag encoded in the token (dtxr_0_/dtxr_1_)
    # still ORs in — see PasswordRecoveryService.consume_password_reset —
    # so this only changes the outcome for callers that never set it (AUTH-01).
    revoke_api_tokens: bool = True

    model_config = ConfigDict(extra="forbid")


class PasswordResetRequest(BaseModel):
    email: EmailInput

    model_config = ConfigDict(extra="forbid")


class EmailVerificationConfirmRequest(BaseModel):
    token: str = Field(min_length=16, max_length=1024)

    model_config = ConfigDict(extra="forbid")


class WorkspaceGrantInput(BaseModel):
    """One allowed-workspace entry in a PAT create/update request (D-01v2 T4).

    ``scopes`` omitted means "use the token's own ceiling ``scopes``"; given
    explicitly it must be a subset of the ceiling (D-01v3-f) -- enforced at
    the request level here when the ceiling is on the same request
    (``ApiTokenCreateRequest``); ``ApiTokenUpdateRequest`` has no top-level
    ceiling field, so that case is validated against the token's existing
    ``scopes`` in the service instead.
    """

    workspace_id: UUID
    scopes: list[str] | None = Field(default=None, min_length=1, max_length=16)

    model_config = ConfigDict(extra="forbid")

    @field_validator("scopes")
    @classmethod
    def _scopes_are_supported(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        unknown = [scope for scope in value if scope not in ALLOWED_API_TOKEN_SCOPES]
        if unknown:
            raise ValueError(f"unsupported scope: {unknown[0]}")
        return value


def _require_unique_workspace_ids(grants: list[WorkspaceGrantInput]) -> None:
    workspace_ids = [grant.workspace_id for grant in grants]
    if len(set(workspace_ids)) != len(workspace_ids):
        raise ValueError("duplicate workspace_id in workspace_grants")


TokenNameInput = Annotated[
    str,
    BeforeValidator(
        lambda value: normalize_short_text(value, field="name", max_length=128)
    ),
    Field(json_schema_extra={"x-normalized-maxLength": 128}),
]
CustomAppLabelInput = Annotated[
    str,
    BeforeValidator(
        lambda value: normalize_short_text(
            value, field="custom_app_label", max_length=160
        )
    ),
    Field(json_schema_extra={"x-normalized-maxLength": 160}),
]


class ApiTokenCreateRequest(BaseModel):
    # D-01v2: an allowed-workspace SET, not one workspace_id. At least one
    # entry, no duplicate workspace_ids (checked below); every named
    # workspace must be one the caller has live membership in (checked by
    # the service, which needs a DB read this schema cannot do).
    workspace_grants: list[WorkspaceGrantInput] = Field(min_length=1, max_length=256)
    name: TokenNameInput
    scopes: list[str] = Field(min_length=1, max_length=16)
    # P10.3 decision B (.claude/plan/260909-p10-3-multi-workspace-credential.md
    # section 2.7): PAT default expiration. One of {30, 90, 180, 365} days,
    # or `null` for an explicit, honored "unlimited" choice. Omitting the
    # field entirely (not sending an explicit `null`) means the caller made
    # no expiry choice at all -- the server then defaults to
    # DEFAULT_PAT_EXPIRES_IN_DAYS (90), not unlimited; see
    # resolved_expires_at below, which is what callers must use instead of
    # this field directly. A credential's allowed-workspace set can now span
    # several workspaces (D-01v2), so an unlimited-lifetime leaked PAT's
    # blast radius is larger than the old single-workspace binding -- hence
    # defaulting a no-opinion request to a bounded lifetime rather than
    # unlimited. PATs get no sliding/renewal-on-use: a leaked, still-abused
    # key must not be able to keep resetting its own clock by being used.
    expires_in_days: int | None = None
    # Preferred display association. Membership in the catalog does not grant
    # authorization; the service snapshots the canonical label at issuance.
    catalog_app_id: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    # Used only for a catalog_app_id=null custom/generic client.
    custom_app_label: CustomAppLabelInput | None = None
    # Compatibility input retained for existing clients. Dynamic catalog
    # validation belongs to the service/repository boundary, not this schema.
    mcp_client_type: str | None = None

    model_config = ConfigDict(extra="forbid")

    @field_validator("scopes")
    @classmethod
    def _scopes_are_supported(cls, value: list[str]) -> list[str]:
        unknown = [scope for scope in value if scope not in ALLOWED_API_TOKEN_SCOPES]
        if unknown:
            raise ValueError(f"unsupported scope: {unknown[0]}")
        return value

    @field_validator("expires_in_days")
    @classmethod
    def _expires_in_days_is_supported(cls, value: int | None) -> int | None:
        if value is not None and value not in ALLOWED_PAT_EXPIRES_IN_DAYS:
            raise ValueError(f"unsupported expires_in_days: {value}")
        return value

    @model_validator(mode="after")
    def _workspace_grants_are_well_formed(self):
        _require_unique_workspace_ids(self.workspace_grants)
        ceiling = set(self.scopes)
        for grant in self.workspace_grants:
            if grant.scopes is not None and not set(grant.scopes) <= ceiling:
                raise ValueError("workspace grant scopes must be a subset of scopes")
        return self

    def resolved_expires_at(self, *, now: datetime) -> datetime | None:
        """The actual ``ApiToken.expires_at`` to persist.

        Distinguishes "field absent" (no opinion -> DEFAULT_PAT_EXPIRES_IN_DAYS)
        from "field explicitly null" (unlimited, honored) via
        ``model_fields_set`` -- both otherwise look like ``expires_in_days is
        None``.
        """

        if "expires_in_days" not in self.model_fields_set:
            days = DEFAULT_PAT_EXPIRES_IN_DAYS
        elif self.expires_in_days is None:
            return None
        else:
            days = self.expires_in_days
        return now + timedelta(days=days)


class ApiTokenUpdateRequest(BaseModel):
    name: TokenNameInput | None = None
    # D-01v2: full-set REPLACE semantics when provided (>=1 entries).
    workspace_grants: list[WorkspaceGrantInput] | None = Field(
        default=None, min_length=1, max_length=256
    )

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _require_a_change(self):
        if not self.model_fields_set:
            raise ValueError("at least one of name or workspace_grants is required")
        if self.workspace_grants is not None:
            _require_unique_workspace_ids(self.workspace_grants)
        return self


class AuthUserData(BaseModel):
    user_id: UUID
    email: str
    display_name: str
    status: str
    email_verified_at: datetime | None = None
    # Instance-level (server) admin flag — populated authoritatively by GET
    # /auth/me (the SPA's startup load) to gate system/deployment surfaces.
    # Other auth responses leave it at the default; the SPA reads /me.
    is_server_admin: bool = False
    # Authoritative MCP OAuth availability (https issuer only -- see MCP-10),
    # populated by GET /auth/me so the MCP-access UI steers from the server's
    # truth instead of a client-side URL heuristic. Other auth responses
    # leave the defaults.
    # Null means "this response did not carry the capability", never "yes".
    # They defaulted to True, so a login response -- which does not fill them --
    # deserialised as "OAuth is available" on a server where it is not, and a
    # reader could not tell an unfilled field from a real answer. GET /auth/me
    # always sets both explicitly; every other auth response leaves them null
    # and the client treats that as unknown.
    oauth_available: bool | None = None
    oauth_unavailable_reason: str | None = None
    # MCP-10: advisory only -- DCR off does not make oauth_available False
    # (a client using an https URL client identifier authorizes without it).
    oauth_dcr_enabled: bool | None = None
    # The canonical MCP endpoint under the configured public origin. The screen
    # shows and copies THIS, never an address derived from the browser's own
    # location: the issuer is the configured origin, so a recipe built from
    # wherever an admin happened to browse would register the wrong server.
    mcp_url: str | None = None

    model_config = ConfigDict(extra="forbid")


class AuthSessionData(BaseModel):
    user: AuthUserData
    expires_at: datetime

    model_config = ConfigDict(extra="forbid")


class AuthLogoutData(BaseModel):
    logged_out: bool = True

    model_config = ConfigDict(extra="forbid")


class ReauthenticationData(BaseModel):
    reauthenticated_until: datetime

    model_config = ConfigDict(extra="forbid")


class PasswordResetConfirmData(BaseModel):
    reset: bool = True

    model_config = ConfigDict(extra="forbid")


class PasswordResetStatusData(BaseModel):
    valid: bool = True

    model_config = ConfigDict(extra="forbid")


class PasswordResetRequestData(BaseModel):
    requested: bool = True
    message: str = "If the email is registered, password reset instructions were sent."

    model_config = ConfigDict(extra="forbid")


class EmailVerificationData(BaseModel):
    requested: bool = False
    verified: bool = False

    model_config = ConfigDict(extra="forbid")


class PasswordRecoveryCapabilities(BaseModel):
    mode: Literal["email", "admin_assisted", "disabled"]
    admin_assisted: bool

    model_config = ConfigDict(extra="forbid")


class AuthCapabilitiesData(BaseModel):
    signup_enabled: bool
    password_recovery: PasswordRecoveryCapabilities

    model_config = ConfigDict(extra="forbid")


class WorkspaceGrantData(BaseModel):
    """One row of a PAT's live allowed-workspace set (D-01v2 T4)."""

    id: UUID
    title: str
    slug: str
    membership_role: str | None = None
    granted_scopes: list[str]
    sync_state: str

    model_config = ConfigDict(extra="forbid")


class ApiTokenData(BaseModel):
    id: UUID
    # D-01v2: an allowed-workspace SET, not one workspace_id -- this repo has
    # zero real deployed data, so the legacy single-workspace field is
    # removed outright rather than kept alongside this one.
    workspaces: list[WorkspaceGrantData] = Field(default_factory=list)
    name: str
    token_preview: str | None = None
    scopes: list[str]
    catalog_app_id: str | None = None
    catalog_app_label_snapshot: str | None = None
    catalog_revision: str | None = None
    mcp_client_type: str | None = None
    mcp_client_label: str | None = None
    created_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None

    model_config = ConfigDict(extra="forbid")


class ApiTokenIssueData(BaseModel):
    api_token: ApiTokenData
    plain_token: str

    model_config = ConfigDict(extra="forbid")


class ApiTokenDeleteData(BaseModel):
    id: UUID
    deleted: bool = True

    model_config = ConfigDict(extra="forbid")


class ApiTokenStatusCounts(BaseModel):
    active: int = Field(ge=0)
    expired: int = Field(ge=0)
    revoked: int = Field(ge=0)

    model_config = ConfigDict(extra="forbid")


class OAuthConsentWorkspaceData(BaseModel):
    """One workspace this (user, client) OAuth consent grants access to
    (P10.3 T3, section 2.3)."""

    id: UUID
    title: str
    slug: str
    membership_role: str | None = None

    model_config = ConfigDict(extra="forbid")


class OAuthConsentData(BaseModel):
    """One (user, client) OAuth consent GROUP -- a client can now hold
    several allowed workspaces at once (D-01v2), so the row unit is the
    group, not one workspace (P10.3 T3, section 2.3)."""

    client_id: str
    client_name: str
    catalog_app_id: str | None = None
    catalog_app_label_snapshot: str | None = None
    catalog_revision: str | None = None
    scope: str
    # Fix-6 Task 2.2 (F5-5) invariant, restated for the group shape (P10.3
    # T3): a consent row with zero workspaces cannot exist through the
    # service layer (issue_authorization_code/update_consent_settings both
    # require a non-empty set; revoke_consent deletes the whole group rather
    # than leaving it empty), so the schema pins that here too.
    workspaces: list[OAuthConsentWorkspaceData] = Field(min_length=1)
    granted_at: datetime
    connection_state: Literal["active", "expired", "revoked", "disabled"]
    last_used_at: datetime | None = None
    next_expiry: datetime | None = None
    mcp_client_ids: list[UUID] = Field(default_factory=list)
    refresh_ttl_seconds: int | None
    effective_refresh_ttl_seconds: int
    sliding_enabled: bool
    effective_sliding_enabled: bool
    # MCP-05(b): the session-length UI control has no other way to learn the
    # instance ceiling (effective_refresh_ttl_seconds is min(instance,
    # requested) and is indistinguishable from the ceiling once an explicit
    # consent TTL is smaller) -- without this it renders a 180-day option
    # that 422s (OAuthConsentTTLAboveInstanceError) whenever the instance
    # max is lower.
    instance_max_refresh_ttl_seconds: int

    model_config = ConfigDict(extra="forbid")


class OAuthConsentSettingsUpdateRequest(BaseModel):
    refresh_ttl_seconds: int | None = Field(default=None, gt=0)
    sliding_enabled: bool | None = None
    # P10.3 T3 (section 2.4): full-set REPLACE of the allowed-workspace set,
    # mirroring ApiTokenUpdateRequest.workspace_grants. Omitted leaves the
    # set unchanged.
    workspace_ids: list[UUID] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _require_a_setting(self):
        if not self.model_fields_set:
            raise ValueError("at least one consent setting is required")
        if "sliding_enabled" in self.model_fields_set and self.sliding_enabled is None:
            raise ValueError("sliding_enabled must be true or false")
        if self.workspace_ids is not None and len(set(self.workspace_ids)) != len(
            self.workspace_ids
        ):
            raise ValueError("duplicate workspace_id in workspace_ids")
        return self

    model_config = ConfigDict(extra="forbid")


class OAuthConsentRevokeData(BaseModel):
    revoked: bool

    model_config = ConfigDict(extra="forbid")


class OAuthConnectionCleanupData(BaseModel):
    eligible_consent_count: int
    affected_mcp_client_count: int
    cleaned_consent_count: int

    model_config = ConfigDict(extra="forbid")


class OAuthConsentListResponse(RESTResponseBase):
    data: list[OAuthConsentData]
    next_cursor: str | None = None


class OAuthConsentRevokeResponse(RESTResponseBase):
    data: OAuthConsentRevokeData


class OAuthConsentSettingsUpdateResponse(RESTResponseBase):
    data: OAuthConsentData


class OAuthConnectionCleanupResponse(RESTResponseBase):
    data: OAuthConnectionCleanupData


class AuthSessionResponse(RESTResponseBase):
    data: AuthSessionData


class AuthMeResponse(RESTResponseBase):
    data: AuthUserData


class AuthLogoutResponse(RESTResponseBase):
    data: AuthLogoutData = Field(default_factory=AuthLogoutData)


class ReauthenticationResponse(RESTResponseBase):
    data: ReauthenticationData


class PasswordResetConfirmResponse(RESTResponseBase):
    data: PasswordResetConfirmData = Field(default_factory=PasswordResetConfirmData)


class PasswordResetStatusResponse(RESTResponseBase):
    data: PasswordResetStatusData = Field(default_factory=PasswordResetStatusData)


class PasswordResetRequestResponse(RESTResponseBase):
    data: PasswordResetRequestData = Field(default_factory=PasswordResetRequestData)


class EmailVerificationResponse(RESTResponseBase):
    data: EmailVerificationData


class AuthCapabilitiesResponse(RESTResponseBase):
    data: AuthCapabilitiesData


class ApiTokenListResponse(RESTResponseBase):
    """``GET /api/v1/auth/tokens`` -- the canonical, account-scoped, paginated
    Settings listing (D-01v2 T4; replaces the deleted workspace-scoped
    ``GET /workspaces/{id}/settings/access-keys``)."""

    data: list[ApiTokenData]
    next_cursor: str | None = None
    evaluated_at: datetime
    total_count: int = Field(ge=0)
    filtered_count: int = Field(ge=0)
    status_counts: ApiTokenStatusCounts


class ApiTokenIssueResponse(RESTResponseBase):
    data: ApiTokenIssueData


class ApiTokenRevokeResponse(RESTResponseBase):
    data: ApiTokenData


class ApiTokenDeleteResponse(RESTResponseBase):
    data: ApiTokenDeleteData

from __future__ import annotations

import base64
import binascii
import hashlib
import itertools
import json
import logging
import re
import secrets
import threading
import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, NoReturn
from urllib.parse import urlparse
from uuid import UUID, uuid4

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import and_, event, func, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from backend.app.settings import Settings
from backend.audit.emitter import emit_oauth_event
from backend.catalog.mcp_apps import (
    CatalogRepository,
    VerifiedOAuthAssociation,
    get_mcp_app_catalog_repository,
)
from backend.domain.credential_input import (
    OAUTH_IDENTIFIER_MAX_BYTES,
    OAUTH_JWT_MAX_BYTES,
    OAUTH_OPAQUE_TOKEN_MAX_BYTES,
    WORKSPACE_GRANTS_MAX_COUNT,
    ascii_token_is_valid,
    require_ascii_token,
    utf8_size_is_valid,
)
from backend.domain.oauth_input import (
    validate_client_name,
    validate_identifier,
    validate_redirect_list,
    validate_scope_input,
    validate_supported_metadata,
)
from backend.models.base import utc_now
from backend.models.oauth import (
    OAuthAccessTokenJti,
    OAuthClient,
    OAuthCode,
    OAuthConsent,
    OAuthRefreshToken,
)
from backend.models.user import User
from backend.models.workspace import MCPClient, Workspace, WorkspaceMembership
from backend.policy.rbac import list_user_workspaces
from backend.policy.workspace_resolution import (
    WorkspaceAmbiguityError,
    resolve_workspace_for_actor,
)
from backend.services.audit_service import AuditService
from backend.services.credential_hashing import CredentialHasher
from backend.services.credential_replica_service import (
    CredentialReplicaFanOutRequest,
    register_fan_out,
    resolve_oauth_replica_credential_id,
)
from backend.services.db_write_intent import acquire_write_intent
from backend.services.oauth_cimd import (
    CIMDClientMetadata,
    fetch_client_metadata_document,
)
from backend.services.session_service import AuthenticatedUser, authenticated_user

ALLOWED_OAUTH_SCOPES = (
    "mcp:read",
    "mcp:write",
    "mcp:agent",
    "offline_access",
)
DEFAULT_GRANT_TYPES = ["authorization_code", "refresh_token"]
DEFAULT_RESPONSE_TYPES = ["code"]
CODE_TTL_SECONDS = 60
# P10.3 T3 (section 2.3, ADR A5 amendment): a rotated refresh token reused
# within this many seconds of its own rotation mints a new sibling pair
# instead of triggering reuse-detection family revoke (Auth0 "reuse
# interval" pattern). Reuse after this window still revokes the family.
REFRESH_REUSE_GRACE_WINDOW_SECONDS = 30
JWT_KID = "dtxwiki-local-rs256-1"
PKCE_METHOD_S256 = "S256"
# 128 bits of entropy encoded as base64url needs 22 characters
# (ceil(128 / 6)). The server cannot prove client-side randomness, but it
# should reject obviously undersized state values instead of accepting
# short, guessable CSRF correlators.
MIN_STATE_LENGTH = 22
_PRIVATE_KEY_PEM: str | None = None
_DCR_BUCKET: dict[str, list[float]] = {}
# Sprint_5 Phase_3 Task 3.6 (ADR A12) — Rate-limit correctness mode:
#
# The Sprint_5 production deployment runs as a *single* backend process
# (Phase_4 systemd unit ``dtxwiki.service``, ``Type=simple``, exactly one
# ``ExecStart`` line). The in-process bucket counters below are therefore
# the authoritative rate-limit state for the entire deployment.
#
# **Invariant**: Multi-worker (uvicorn --workers N>1, gunicorn, etc.) is
# **explicitly out of scope** for Sprint_5 and silently defeats these
# counters because each worker has its own copy of ``_TOKEN_*_BUCKET``.
# The Sprint_5 epilogue (PG activation) does NOT change this invariant.
# See the Sprint_6 catalog entry "Redis-backed rate-limit" for the
# upgrade path.
#
# **Thread-safety**: The in-process dict + list mutations below run
# under a single asyncio event loop (no threading; FastAPI dispatches
# requests on the loop's executor only for sync route handlers, of
# which ``/oauth/token`` is one — but it returns before yielding, so
# the critical section between the read-modify-write is effectively
# atomic from the event loop's perspective). A future move to async
# handlers would need an asyncio.Lock; flagged in the Sprint_6 catalog.
#
# Initial baseline (Sprint-4 closure R-F6-1): per-IP 120 req/min,
# per-client_id 60 req/min. Phase_3 leaves the thresholds unchanged
# and adds the meta-test ``test_single_process_invariant_documented``
# (test_rate_limit_in_process.py) so a future refactor that drops the
# single-process commitment trips an explicit failure.
_TOKEN_IP_BUCKET: dict[str, list[float]] = {}
_TOKEN_CLIENT_BUCKET: dict[str, list[float]] = {}
_OAUTH_REFRESH_LOCK = threading.RLock()
TOKEN_RATE_LIMIT_PER_IP = 120  # requests per minute
TOKEN_RATE_LIMIT_PER_CLIENT = 60  # requests per minute
REFRESH_REFUSAL_REASONS = frozenset(
    {
        "expired",
        "revoked",
        "rotated_reuse",
        "client_mismatch",
        "consent_missing",
        "user_inactive",
    }
)
_REFRESH_REFUSALS_REVOKING_FAMILY = frozenset(
    {
        "revoked",
        "rotated_reuse",
        "consent_missing",
        "user_inactive",
    }
)

# Sprint_5 Phase_3 Task 3.3 (ADR A3): server-side bookkeeping for the
# state-bound CSRF cookie. The cookie itself carries the state value; these
# two in-process maps let the POST handler diagnose the *reason* a cookie is
# absent — replay (state was already consumed) vs expired (state was issued
# but the cookie max_age elapsed) vs missing (state never issued from this
# server / unknown). Both maps key on the state string and store
# ``time.monotonic()`` at the recording moment. TTL is generous (1 hour) so
# the diagnosis stays accurate well after the cookie's 600s expiry. The
# Sprint_5 §12.2.6 task replaces these with persistent storage.
OAUTH_CSRF_COOKIE_NAME = "oauth_csrf_state"
OAUTH_CSRF_COOKIE_MAX_AGE = 600  # 10 minutes
OAUTH_CSRF_COOKIE_PATH = "/oauth"
_OAUTH_CSRF_STATE_BUCKET_TTL = 3600  # 1 hour
_OAUTH_CSRF_STATE_BUCKET_CAP = 4096
_OAUTH_CSRF_ISSUED_STATES: dict[str, float] = {}
_OAUTH_CSRF_CONSUMED_STATES: dict[str, float] = {}
_OAUTH_CSRF_STATE_LOCK = threading.Lock()


def _csrf_state_bucket_prune_locked(now: float) -> None:
    """Drop stale entries from both CSRF state buckets (TTL = 1h)."""

    for bucket in (_OAUTH_CSRF_ISSUED_STATES, _OAUTH_CSRF_CONSUMED_STATES):
        stale = [
            key
            for key, stamp in bucket.items()
            if now - stamp > _OAUTH_CSRF_STATE_BUCKET_TTL
        ]
        for key in stale:
            bucket.pop(key, None)


def _csrf_state_bucket_enforce_cap_locked(bucket: dict[str, float]) -> None:
    overflow = len(bucket) - _OAUTH_CSRF_STATE_BUCKET_CAP
    if overflow <= 0:
        return
    oldest = sorted(bucket.items(), key=lambda item: item[1])[:overflow]
    for key, _stamp in oldest:
        bucket.pop(key, None)


def _csrf_state_bucket_prune(now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    with _OAUTH_CSRF_STATE_LOCK:
        _csrf_state_bucket_prune_locked(now)


def record_oauth_csrf_state_issued(state: str) -> None:
    """Called from GET /oauth/authorize when the cookie is set."""

    if not state:
        return
    now = time.monotonic()
    with _OAUTH_CSRF_STATE_LOCK:
        _csrf_state_bucket_prune_locked(now)
        _OAUTH_CSRF_ISSUED_STATES[state] = now
        _csrf_state_bucket_enforce_cap_locked(_OAUTH_CSRF_ISSUED_STATES)


def consume_oauth_csrf_state(state: str) -> None:
    """Called from POST /oauth/authorize on successful CSRF match."""

    if not state:
        return
    now = time.monotonic()
    with _OAUTH_CSRF_STATE_LOCK:
        _csrf_state_bucket_prune_locked(now)
        _OAUTH_CSRF_ISSUED_STATES.pop(state, None)
        _OAUTH_CSRF_CONSUMED_STATES[state] = now
        _csrf_state_bucket_enforce_cap_locked(_OAUTH_CSRF_CONSUMED_STATES)


def classify_oauth_csrf_failure(
    *,
    form_state: str,
    cookie_state: str | None,
) -> str:
    """Diagnose why a consent POST failed CSRF binding (ADR A3).

    Returns one of {``missing``, ``mismatch``, ``replay``, ``expired``}. The
    label is recorded in the ``oauth.consent.csrf`` audit payload so
    operators can distinguish broken UX from active attack.
    """

    if not form_state:
        return "missing"
    if cookie_state is None:
        # The browser never sent the cookie. Three sub-cases:
        # * state was previously consumed (one-time delete on success) → replay
        # * state is in the issued bucket but older than the cookie max_age →
        #   expired (browser dropped the cookie even though server still
        #   remembers it)
        # * state is unknown to this server → missing
        now = time.monotonic()
        with _OAUTH_CSRF_STATE_LOCK:
            _csrf_state_bucket_prune_locked(now)
            consumed = form_state in _OAUTH_CSRF_CONSUMED_STATES
            issued_at = _OAUTH_CSRF_ISSUED_STATES.get(form_state)
        if consumed:
            return "replay"
        if issued_at is not None and now - issued_at > OAUTH_CSRF_COOKIE_MAX_AGE:
            return "expired"
        return "missing"
    if cookie_state != form_state:
        return "mismatch"
    # Caller should not reach this branch when cookie_state == form_state.
    return "mismatch"


logger = logging.getLogger(__name__)


class OAuthServiceError(RuntimeError):
    error_code = "oauth_error"


class InvalidClientRegistrationError(OAuthServiceError):
    error_code = "invalid_client_metadata"


class _RefreshRefusal(Exception):
    """Carry a known refresh-row refusal into a committed audit transaction.

    The refresh read transaction must roll back when issuance is refused. This
    exception preserves only non-secret row metadata so the follow-up
    transaction can durably audit the refusal and, for security lifecycle
    failures, revoke exactly the O4 grant family.
    """

    def __init__(
        self,
        *,
        user_id: UUID,
        client_id: str,
        reused_refresh_id: UUID,
        family_key: UUID,
        reason: str,
        token_age_seconds: int,
        public_message: str,
        detected_via: str | None = None,
    ) -> None:
        if reason not in REFRESH_REFUSAL_REASONS:
            raise ValueError(f"unknown refresh refusal reason: {reason}")
        super().__init__(public_message)
        self.user_id = user_id
        self.client_id = client_id
        self.reused_refresh_id = reused_refresh_id
        self.family_key = family_key
        self.reason = reason
        self.token_age_seconds = token_age_seconds
        self.public_message = public_message
        self.detected_via = detected_via


class InvalidOAuthRequestError(OAuthServiceError):
    error_code = "invalid_request"


class OAuthWorkspacePlaneUnavailableError(OAuthServiceError):
    """A consent path needed the workspace plane and was never given a router.

    ``MCPClient`` is workspace-plane data while everything else this service
    touches is control-plane, so the consent methods need ``session_for``. A
    caller that forgets to inject the provider gets this at the call site
    rather than a check that silently reports every connection as healthy --
    the same reasoning backend/policy/rbac.py gives for making
    ``workspace_session_factory`` a required keyword argument.
    """


class OAuthConsentNotFoundError(OAuthServiceError):
    error_code = "consent_not_found"


class OAuthConsentTTLAboveInstanceError(OAuthServiceError):
    error_code = "refresh_ttl_above_instance_max"


class InvalidOAuthGrantError(OAuthServiceError):
    error_code = "invalid_grant"

    def __init__(
        self,
        message: str,
        *,
        refusal_reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.refusal_reason = (
            refusal_reason if refusal_reason in REFRESH_REFUSAL_REASONS else None
        )


class UnsupportedGrantTypeError(InvalidOAuthGrantError):
    """The token endpoint was asked for a grant type it does not implement.

    Distinct from ``invalid_grant`` on purpose (RFC 6749 §5.2): a client that
    reads ``invalid_grant`` is being told its credential is dead and must
    discard the refresh token and send the user through interactive re-auth. A
    grant type we never supported says nothing about the credential.
    """

    error_code = "unsupported_grant_type"


class OAuthRateLimitError(OAuthServiceError):
    """Token endpoint rate-limit exceeded. HTTP 429 + RFC 6749 §5.2-style body."""

    error_code = "rate_limited"


@dataclass(frozen=True)
class RegisteredClient:
    client_id: str
    client_name: str
    redirect_uris: list[str]
    scope: str
    issued_at: int
    application_type: str


@dataclass(frozen=True)
class OAuthTokenResult:
    access_token: str
    refresh_token: str
    expires_in: int
    scope: str
    refresh_token_expires_in: int = 0


@dataclass(frozen=True)
class OAuthAccessTokenActor:
    """P10.3 T3: no ``workspace_id`` -- the access JWT no longer carries one
    (D-01v2/D-01v3-d). ``backend.policy.auth._authenticate_oauth_token`` reads
    the credential's live allowed-workspace set from ``oauth_consents``
    instead (``removed_at IS NULL``), not from this actor.
    """

    user: AuthenticatedUser
    client_id: str
    scopes: tuple[str, ...]
    grant_id: UUID | None = None


@dataclass(frozen=True)
class AuthorizeWorkspaceOption:
    """One row of the GET /oauth/authorize workspace checklist (section 2.3)."""

    id: UUID
    title: str
    slug: str
    checked: bool


def _registered_client(client: OAuthClient) -> RegisteredClient:
    return RegisteredClient(
        client_id=client.id,
        client_name=client.client_name,
        redirect_uris=list(client.redirect_uris),
        scope=client.scope,
        issued_at=int(client.created_at.timestamp()),
        application_type=client.application_type,
    )


ConnectionState = Literal["active", "expired", "revoked", "disabled"]


def _registration_identity_redirect_uri(uri: str) -> str:
    """Return the redirect value used only by the DCR identity hash.

    RFC 8252 loopback ports are ephemeral and the runtime matcher already
    ignores that one component. Keep the exact validated URI on OAuthClient;
    only replace the port in the registration identity. Host, scheme, path,
    params, query, and every non-loopback port remain identity-bearing.

    Client ID Metadata Documents (CIMD) are the structural fix for stable
    native-client identity; this normalization keeps DCR usable until then.
    """

    parsed = urlparse(uri)
    if parsed.scheme != "http" or not _is_loopback_host(parsed.hostname):
        return uri
    host = f"[{parsed.hostname}]" if parsed.hostname == "::1" else parsed.hostname
    return parsed._replace(netloc=f"{host}:<loopback-port>").geturl()


def _dcr_registration_identity(
    *,
    client_name: str,
    redirect_uris: list[str],
    token_endpoint_auth_method: str,
    grant_types: list[str],
    response_types: list[str],
    application_type: str,
    scope: str,
) -> str:
    """Hash the canonical, security-complete DCR identity.

    URI, grant, response, and scope order is not identity-bearing. Canonical
    JSON makes equivalent registrations converge on one database value, whose
    unique constraint closes the lookup/insert race.
    """
    canonical = json.dumps(
        {
            "application_type": application_type,
            "client_name": client_name,
            "grant_types": sorted(set(grant_types)),
            "redirect_uris": sorted(
                {_registration_identity_redirect_uri(uri) for uri in redirect_uris}
            ),
            "response_types": sorted(set(response_types)),
            "scope": " ".join(sorted(set(scope.split()))),
            "token_endpoint_auth_method": token_endpoint_auth_method,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _oauth_catalog_model_values(
    *,
    metadata_fingerprint: str,
    association: VerifiedOAuthAssociation | None,
) -> dict[str, str | None]:
    return {
        "catalog_identity_fingerprint": metadata_fingerprint,
        "catalog_app_id": (
            association.catalog_app_id if association is not None else None
        ),
        "catalog_app_label_snapshot": (
            association.label_snapshot if association is not None else None
        ),
        "catalog_revision": (
            association.catalog_revision if association is not None else None
        ),
        "catalog_association_provenance": (
            association.provenance_ref if association is not None else None
        ),
    }


def _apply_verified_oauth_association(
    client: OAuthClient,
    *,
    metadata_fingerprint: str,
    association: VerifiedOAuthAssociation | None,
    replace_unconsented: bool,
) -> None:
    """Persist an exact display association without changing OAuth identity.

    DCR metadata is immutable, so an existing verified snapshot survives a
    catalog rename/removal. CIMD metadata may change only before its first grant;
    that pre-consent refresh replaces or clears a stale association.
    """

    client.catalog_identity_fingerprint = metadata_fingerprint
    if not replace_unconsented and client.catalog_app_id is not None:
        return
    values = _oauth_catalog_model_values(
        metadata_fingerprint=metadata_fingerprint,
        association=association,
    )
    client.catalog_app_id = values["catalog_app_id"]
    client.catalog_app_label_snapshot = values["catalog_app_label_snapshot"]
    client.catalog_revision = values["catalog_revision"]
    client.catalog_association_provenance = values["catalog_association_provenance"]


def oauth_client_id_for_mcp_client(client: MCPClient) -> str | None:
    value = (client.token_metadata or {}).get("oauth_client_id")
    return str(value) if value else None


def related_mcp_clients_for_consent(
    session: Session,
    *,
    user_id: UUID,
    client_id: str,
    workspace_id: UUID,
) -> list[MCPClient]:
    candidates = session.scalars(
        select(MCPClient).where(
            MCPClient.workspace_id == workspace_id,
            MCPClient.issued_to_user_id == user_id,
        )
    ).all()
    return [
        client
        for client in candidates
        if oauth_client_id_for_mcp_client(client) == client_id
    ]


def classify_oauth_connection_state(
    *,
    physical_status: str,
    oauth_client_is_live: bool,
    consent_granted: bool,
    live_expiry: datetime | None,
) -> tuple[ConnectionState, datetime | None]:
    """The one definition of an OAuth-backed connection's health.

    Pure on purpose: every caller below differs only in how it gathers the
    four inputs -- one row at a time, batched over a page, or from a
    control-plane snapshot handed across a database boundary. Keeping the
    decision itself in a single place is what lets the Settings facet's SQL
    twin and the per-row state it renders stay in agreement; they used to be
    two independently maintained copies of these four lines.
    """

    if physical_status != "active":
        return "disabled", live_expiry
    if not oauth_client_is_live or not consent_granted:
        return "revoked", live_expiry
    if live_expiry is not None:
        return "active", live_expiry
    # The ordinary expired case has token rows but none live. A consent can
    # also be observed in the brief code-before-token exchange window; it has
    # no working connection either, so the exhaustive UI state is expired.
    return "expired", None


def derive_oauth_connection_state(
    session: Session,
    *,
    client_id: str,
    workspace_id: UUID,
    user_id: UUID | None,
    physical_status: str = "active",
    now: datetime | None = None,
) -> tuple[ConnectionState, datetime | None]:
    """Derive user-visible connection health without mutating MCPClient.status."""

    consent_filters = [
        OAuthConsent.client_id == client_id,
        OAuthConsent.workspace_id == workspace_id,
    ]
    # D-01v2/D-01v3: OAuthRefreshToken lost its workspace_id -- family
    # liveness is a GLOBAL (user, client) fact now, matching
    # load_workspace_oauth_health's same redefinition (section 2.3).
    token_filters = [
        OAuthRefreshToken.client_id == client_id,
    ]
    if user_id is not None:
        consent_filters.append(OAuthConsent.user_id == user_id)
        token_filters.append(OAuthRefreshToken.user_id == user_id)
    current_time = now or utc_now()
    live_expiries = session.scalars(
        select(OAuthRefreshToken.expires_at).where(
            *token_filters,
            OAuthRefreshToken.revoked_at.is_(None),
            OAuthRefreshToken.expires_at > current_time,
        )
    ).all()
    next_expiry = max(live_expiries) if live_expiries else None
    if physical_status != "active":
        # The classifier answers "disabled" without consulting the other two
        # inputs, so this arm skips their queries rather than issuing them and
        # discarding the answers. The placeholders below are never read.
        return classify_oauth_connection_state(
            physical_status=physical_status,
            oauth_client_is_live=False,
            consent_granted=False,
            live_expiry=next_expiry,
        )
    client = session.get(OAuthClient, client_id)
    if client is None or client.revoked_at is not None:
        # Same reason as the arm above: the classifier reaches "revoked" from
        # a dead client alone, so the consent SELECT would be issued and its
        # answer discarded. This is the consent screen's hot path -- the screen
        # exists to render exactly this branch -- so the query stays skipped.
        return classify_oauth_connection_state(
            physical_status=physical_status,
            oauth_client_is_live=False,
            consent_granted=False,
            live_expiry=next_expiry,
        )
    consent_exists = session.scalar(
        select(OAuthConsent.client_id).where(*consent_filters).limit(1)
    )
    return classify_oauth_connection_state(
        physical_status=physical_status,
        oauth_client_is_live=True,
        consent_granted=consent_exists is not None,
        live_expiry=next_expiry,
    )


# Keyed by (issuing user, oauth client, workspace) because an MCPClient with
# no issuing user matches any row for its client/workspace pair.
OAuthConnectionKey = tuple[UUID | None, str, UUID]


def _derive_oauth_connection_states_batch(
    session: Session,
    *,
    keys: set[OAuthConnectionKey],
    physical_status: dict[OAuthConnectionKey, str],
    now: datetime,
    oauth_clients: dict[str, OAuthClient] | None = None,
    known_consents: set[OAuthConnectionKey] | None = None,
) -> dict[OAuthConnectionKey, tuple[ConnectionState, datetime | None]]:
    if not keys:
        return {}
    client_ids = {key[1] for key in keys}
    workspace_ids = {key[2] for key in keys}
    user_ids = {key[0] for key in keys if key[0] is not None}
    aggregate_users = any(key[0] is None for key in keys)
    clients_by_id = oauth_clients
    if clients_by_id is None:
        clients_by_id = {
            client.id: client
            for client in session.scalars(
                select(OAuthClient).where(OAuthClient.id.in_(client_ids))
            ).all()
        }

    consent_keys = set(known_consents or ())
    if known_consents is None:
        consent_query = select(OAuthConsent).where(
            OAuthConsent.client_id.in_(client_ids),
            OAuthConsent.workspace_id.in_(workspace_ids),
        )
        if not aggregate_users:
            consent_query = consent_query.where(OAuthConsent.user_id.in_(user_ids))
        for consent in session.scalars(consent_query).all():
            exact = (consent.user_id, consent.client_id, consent.workspace_id)
            consent_keys.add(exact)
            consent_keys.add((None, consent.client_id, consent.workspace_id))

    # D-01v2/D-01v3: OAuthRefreshToken lost its workspace_id -- family
    # liveness is a GLOBAL (user, client) fact, not a (user, client,
    # workspace) one. Read it keyed by (user, client) only, then broadcast
    # each family's liveness to every ``keys`` row that shares that pair,
    # matching consent_keys' own dual (exact user / any-user) keying.
    token_query = select(
        OAuthRefreshToken.user_id,
        OAuthRefreshToken.client_id,
        OAuthRefreshToken.expires_at,
    ).where(
        OAuthRefreshToken.client_id.in_(client_ids),
        OAuthRefreshToken.revoked_at.is_(None),
        OAuthRefreshToken.expires_at > now,
    )
    if not aggregate_users:
        token_query = token_query.where(OAuthRefreshToken.user_id.in_(user_ids))
    family_live_expiry: dict[tuple[UUID | None, str], datetime] = {}
    for user_id, client_id, expires_at in session.execute(token_query):
        for family_key in ((user_id, client_id), (None, client_id)):
            previous = family_live_expiry.get(family_key)
            if previous is None or expires_at > previous:
                family_live_expiry[family_key] = expires_at
    live_expiry: dict[OAuthConnectionKey, datetime] = {
        key: family_live_expiry[(key[0], key[1])]
        for key in keys
        if (key[0], key[1]) in family_live_expiry
    }

    result: dict[OAuthConnectionKey, tuple[ConnectionState, datetime | None]] = {}
    for key in keys:
        client = clients_by_id.get(key[1])
        result[key] = classify_oauth_connection_state(
            physical_status=physical_status.get(key, "active"),
            oauth_client_is_live=client is not None and client.revoked_at is None,
            consent_granted=key in consent_keys,
            live_expiry=live_expiry.get(key),
        )
    return result


WorkspaceConnectionKey = tuple[UUID | None, str]


@dataclass(frozen=True)
class WorkspaceOAuthHealth:
    """Everything ONE workspace's connection health depends on, read off the
    control plane and small enough to push back down as literal key sets.

    ``MCPClient`` is workspace-plane while ``OAuthClient``/``OAuthConsent``/
    ``OAuthRefreshToken`` are control-plane, so the correlation between them
    cannot be a join once the two planes are separate databases. Both control
    tables involved carry ``workspace_id``, though, so for a single workspace
    the answer set is finite and computable on the control plane alone: this
    snapshot is that answer, and callers turn it into either an ``IN`` filter
    over ``MCPClient`` columns or a per-row classification.

    Keys are ``(issued_to_user_id, oauth_client_id)``. Every consent and token
    contributes BOTH its exact key and an aggregate ``(None, client_id)`` key,
    because an ``MCPClient`` with no issuing user matches any row for the
    client/workspace pair -- the same disjunction the previous correlated
    EXISTS spelled out in SQL.
    """

    workspace_id: UUID
    now: datetime
    # OAuth client ids this snapshot actually looked up, so a refinement can
    # tell "known absent" apart from "never asked".
    known_oauth_client_ids: frozenset[str]
    live_oauth_client_ids: frozenset[str]
    consent_keys: frozenset[WorkspaceConnectionKey]
    live_expiry: Mapping[WorkspaceConnectionKey, datetime]
    oauth_client_names: Mapping[str, str]
    # None means "no search term", which is NOT the same as "nothing matched".
    name_matched_oauth_client_ids: frozenset[str] | None

    def connection_state_for(
        self,
        client: MCPClient,
    ) -> tuple[ConnectionState, datetime | None, str | None, str | None]:
        """Classify one MCPClient row without touching any session."""

        oauth_client_id = oauth_client_id_for_mcp_client(client)
        if oauth_client_id is None:
            state: ConnectionState = (
                "active" if client.status == "active" else "disabled"
            )
            return state, None, None, None
        key = (client.issued_to_user_id, oauth_client_id)
        state, next_expiry = classify_oauth_connection_state(
            physical_status=client.status,
            oauth_client_is_live=oauth_client_id in self.live_oauth_client_ids,
            consent_granted=key in self.consent_keys,
            live_expiry=self.live_expiry.get(key),
        )
        return (
            state,
            next_expiry,
            oauth_client_id,
            self.oauth_client_names.get(oauth_client_id),
        )

    def active_connection_keys(
        self,
    ) -> tuple[frozenset[str], frozenset[tuple[UUID, str]]]:
        """The keys whose OAuth side is healthy, split by the SQL shape each needs.

        Returned as (aggregate client ids, exact ``(user_id, client_id)`` pairs).
        They stay separate because a row-value ``IN`` never matches a NULL
        ``issued_to_user_id``: SQL comparison with NULL is UNKNOWN, not TRUE.
        """

        aggregate: set[str] = set()
        exact: set[tuple[UUID, str]] = set()
        for key in self.consent_keys:
            user_id, oauth_client_id = key
            if oauth_client_id not in self.live_oauth_client_ids:
                continue
            if key not in self.live_expiry:
                continue
            if user_id is None:
                aggregate.add(oauth_client_id)
            else:
                exact.add((user_id, oauth_client_id))
        return frozenset(aggregate), frozenset(exact)


def load_workspace_oauth_health(
    session: Session,
    *,
    workspace_id: UUID,
    now: datetime,
    oauth_client_ids: Collection[str] | None = None,
) -> WorkspaceOAuthHealth:
    """Read one workspace's connection health from a CONTROL session.

    ``oauth_client_ids`` narrows the consent/token scan to a known page of
    clients; leaving it None loads the whole workspace, which is what a facet
    count over the whole table needs. Either way the result is keyed exactly,
    so the two agree row for row.

    Name search is NOT part of this snapshot: it needs a candidate id set read
    off the workspace plane, which does not exist yet at this point in a
    listing. ``attach_name_matched_oauth_client_ids`` adds it afterwards.
    """

    # D-01v2/D-01v3 (P10.3 T1/section 2.3): OAuthRefreshToken lost its
    # workspace_id -- family liveness ("family_alive") is now a GLOBAL
    # (user, client) value by design, not scoped to this workspace. Only
    # the consent scan stays workspace-filtered ("consented" = this
    # workspace has a live oauth_consents row), which is still exactly
    # right: OAuthConsent's PK still carries workspace_id.
    consent_filters = [OAuthConsent.workspace_id == workspace_id]
    token_filters = [
        OAuthRefreshToken.revoked_at.is_(None),
        OAuthRefreshToken.expires_at > now,
    ]
    if oauth_client_ids is not None:
        restriction = list(oauth_client_ids)
        if not restriction:
            # A page with no OAuth-backed client correlates against nothing, so
            # skip both scans instead of issuing two guaranteed-empty IN
            # queries on what is a hot settings read.
            return WorkspaceOAuthHealth(
                workspace_id=workspace_id,
                now=now,
                known_oauth_client_ids=frozenset(),
                live_oauth_client_ids=frozenset(),
                consent_keys=frozenset(),
                live_expiry={},
                oauth_client_names={},
                name_matched_oauth_client_ids=None,
            )
        consent_filters.append(OAuthConsent.client_id.in_(restriction))
        token_filters.append(OAuthRefreshToken.client_id.in_(restriction))

    consent_keys: set[WorkspaceConnectionKey] = set()
    for user_id, client_id in session.execute(
        select(OAuthConsent.user_id, OAuthConsent.client_id).where(*consent_filters)
    ):
        consent_keys.add((user_id, client_id))
        consent_keys.add((None, client_id))

    live_expiry: dict[WorkspaceConnectionKey, datetime] = {}
    for user_id, client_id, expires_at in session.execute(
        select(
            OAuthRefreshToken.user_id,
            OAuthRefreshToken.client_id,
            OAuthRefreshToken.expires_at,
        ).where(*token_filters)
    ):
        for key in ((user_id, client_id), (None, client_id)):
            previous = live_expiry.get(key)
            if previous is None or expires_at > previous:
                live_expiry[key] = expires_at

    candidate_ids = {key[1] for key in consent_keys} | {key[1] for key in live_expiry}
    if oauth_client_ids is not None:
        candidate_ids |= set(oauth_client_ids)
    live_ids, names = _load_oauth_client_liveness(session, candidate_ids)

    return WorkspaceOAuthHealth(
        workspace_id=workspace_id,
        now=now,
        known_oauth_client_ids=frozenset(candidate_ids),
        live_oauth_client_ids=live_ids,
        consent_keys=frozenset(consent_keys),
        live_expiry=live_expiry,
        oauth_client_names=names,
        name_matched_oauth_client_ids=None,
    )


def attach_name_matched_oauth_client_ids(
    session: Session,
    health: WorkspaceOAuthHealth,
    *,
    name_search: str,
    oauth_client_ids: Collection[str],
) -> WorkspaceOAuthHealth:
    """Resolve a client_name search to ids, bounded by the CALLER's candidates.

    This replaces `lower(OAuthClient.client_name) LIKE ...` reached through an
    outer join from ``MCPClient``. That join only ever produced ``OAuthClient``
    rows whose id a workspace-plane row already named, so ``oauth_client_ids``
    must be exactly the distinct set of ids that workspace's rows name: the
    result set is then identical to the join's, and both the ``LIKE``'s ``IN``
    and the pushdown it feeds are bounded by the workspace rather than by the
    global ``oauth_client`` table.

    That bound is the point. Run unrestricted -- as the first cut of this
    rewrite was -- the match set is every tenant's OAuth apps whose name
    contains an attacker-chosen substring, pushed into workspace-plane SQL as
    an ``IN`` list by any workspace viewer. Nothing leaked (the ``IN`` only
    ever filtered this workspace's own rows), but a workspace holding one
    connection compiled a ~5,000-bind statement against a 5,000-client
    installation. Availability, not confidentiality, and fixed here.
    """

    candidates = sorted(set(oauth_client_ids))
    if not candidates:
        return replace(health, name_matched_oauth_client_ids=frozenset())
    matches = frozenset(
        session.scalars(
            select(OAuthClient.id).where(
                OAuthClient.id.in_(candidates),
                func.lower(OAuthClient.client_name).contains(
                    name_search, autoescape=True
                ),
            )
        ).all()
    )
    return replace(health, name_matched_oauth_client_ids=matches)


def refine_workspace_oauth_health(
    session: Session,
    health: WorkspaceOAuthHealth,
    *,
    oauth_client_ids: Collection[str],
) -> WorkspaceOAuthHealth:
    """Add the OAuth client rows for a page that the workspace scan never saw.

    A page row can name an ``oauth_client_id`` with no consent in this
    workspace. Its facet answer is already settled (no consent means revoked),
    but rendering it still needs the client's name and whether the client row
    exists, so this is one extra primary-key ``IN`` over at most ``limit`` ids
    on the CONTROL session -- never a per-row lookup.
    """

    missing = {
        client_id
        for client_id in oauth_client_ids
        if client_id not in health.known_oauth_client_ids
    }
    if not missing:
        return health
    live_ids, names = _load_oauth_client_liveness(session, missing)
    return replace(
        health,
        known_oauth_client_ids=health.known_oauth_client_ids | frozenset(missing),
        live_oauth_client_ids=health.live_oauth_client_ids | live_ids,
        oauth_client_names={**health.oauth_client_names, **names},
    )


def _load_oauth_client_liveness(
    session: Session,
    oauth_client_ids: Collection[str],
) -> tuple[frozenset[str], dict[str, str]]:
    if not oauth_client_ids:
        return frozenset(), {}
    rows = session.execute(
        select(OAuthClient.id, OAuthClient.client_name, OAuthClient.revoked_at).where(
            OAuthClient.id.in_(list(oauth_client_ids))
        )
    ).all()
    live = frozenset(row[0] for row in rows if row[2] is None)
    names = {row[0]: row[1] for row in rows}
    return live, names


def encode_connection_cursor(
    last_used_at: datetime | None,
    *identifiers: str | UUID,
) -> str:
    payload = [
        to_aware_utc(last_used_at).isoformat() if last_used_at is not None else None,
        *(str(identifier) for identifier in identifiers),
    ]
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return encoded.rstrip("=")


_OAUTH_CONSENT_CURSOR_VERSION = "oauth-consent-identity-v2"


def encode_oauth_consent_cursor(client_id: str) -> str:
    """Encode the (user, client)-grouped consents keyset cursor.

    P10.3 T3 (section 2.3, Codex review #9/R2-3): a page row is now one
    (user, client) GROUP -- possibly several ``oauth_consents`` rows, one per
    allowed workspace -- so the keyset is ``client_id`` alone, not
    ``(client_id, workspace_id)``. The version marker changed from v1 to v2
    so a cursor encoded before this change is rejected outright (returns
    None, restarting the listing from the first page) instead of being
    misread as a single-row identity.
    """

    payload = [_OAUTH_CONSENT_CURSOR_VERSION, client_id]
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    return encoded.rstrip("=")


def decode_oauth_consent_cursor(cursor: str | None) -> str | None:
    """Decode a client_id keyset cursor, or ``None`` for an absent/invalid one."""

    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ):
        return None
    if (
        isinstance(payload, list)
        and len(payload) == 2
        and payload[0] == _OAUTH_CONSENT_CURSOR_VERSION
        and isinstance(payload[1], str)
        and payload[1]
    ):
        return payload[1]
    return None


def decode_connection_cursor(
    cursor: str | None,
    *,
    identifier_count: int,
) -> tuple[datetime | None, tuple[str, ...]] | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if not isinstance(payload, list) or len(payload) != identifier_count + 1:
            return None
        raw_timestamp, *raw_identifiers = payload
        if raw_timestamp is None:
            timestamp = None
        elif isinstance(raw_timestamp, str):
            timestamp = datetime.fromisoformat(raw_timestamp)
            if timestamp.tzinfo is None:
                return None
            timestamp = timestamp.astimezone(UTC)
        else:
            return None
        if not all(isinstance(value, str) and value for value in raw_identifiers):
            return None
        return timestamp, tuple(raw_identifiers)
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ):
        return None


def identity_cursor_predicate(
    identity_columns: tuple[Any, ...],
    identifiers: tuple[Any, ...],
):
    """Strict lexicographic ">" over an immutable identity tuple.

    MCP-11: unlike a cursor keyed on a column that mutates during normal
    operation (``OAuthClient.last_used_at``, ``OAuthConsent.granted_at`` --
    the latter is overwritten on re-consent), an identity-only cursor over
    columns that never change once a row exists cannot drop rows across
    pages no matter what else touches the table between page 1 and page 2.
    """

    return or_(
        *(
            and_(
                *(
                    identity_columns[index] == identifiers[index]
                    for index in range(stop)
                ),
                identity_columns[stop] > identifiers[stop],
            )
            for stop in range(len(identity_columns))
        )
    )


def connection_cursor_predicate(
    last_used_column,
    identity_columns: tuple[Any, ...],
    cursor: tuple[datetime | None, tuple[Any, ...]],
):
    last_used_at, identifiers = cursor
    tie_breaker = identity_cursor_predicate(identity_columns, identifiers)
    if last_used_at is None:
        return and_(last_used_column.is_(None), tie_breaker)
    return or_(
        last_used_column.is_(None),
        last_used_column < last_used_at,
        and_(last_used_column == last_used_at, tie_breaker),
    )


@dataclass(frozen=True)
class OAuthService:
    session_factory: object
    settings: Settings
    public_base_url: str
    credential_hasher: CredentialHasher = dataclass_field(
        default_factory=CredentialHasher
    )
    catalog_repository: CatalogRepository | None = None
    # Router to the workspace plane. Only the consent methods need it;
    # verify_access_token and the token endpoints are control-plane only,
    # so those construction sites legitimately leave it None.
    database_provider: Any = None

    @property
    def issuer(self) -> str:
        return self.public_base_url.rstrip("/")

    @property
    def resource(self) -> str:
        return f"{self.issuer}/mcp"

    def _workspace_session_factory(self, workspace_id: UUID):
        if self.database_provider is None:
            raise OAuthWorkspacePlaneUnavailableError(
                "this OAuthService was built without a database_provider, so "
                "it cannot read workspace-plane MCPClient rows"
            )
        return self.database_provider.session_for(workspace_id)

    def _related_mcp_clients_by_key(
        self,
        *,
        user_id: UUID,
        consent_keys: set[tuple[str, UUID]],
    ) -> tuple[dict[tuple[str, UUID], list[MCPClient]], set[UUID]]:
        """Fan MCPClient reads out across the workspace plane, one shard each.

        Returns the ``(client_id, workspace_id) -> clients`` map and the set of
        workspaces whose shard could not be read. Callers decide what an
        unreachable shard means for them, because the safe answer differs:
        a read-only listing may degrade, a delete may not.
        """

        related: dict[tuple[str, UUID], list[MCPClient]] = {}
        unreachable: set[UUID] = set()
        for workspace_id in {workspace_id for _, workspace_id in consent_keys}:
            try:
                with self._workspace_session_factory(workspace_id)() as session:
                    candidates = session.scalars(
                        select(MCPClient).where(
                            MCPClient.workspace_id == workspace_id,
                            MCPClient.issued_to_user_id == user_id,
                        )
                    ).all()
            except SQLAlchemyError:
                logger.warning(
                    "oauth consents: workspace %s shard unreachable",
                    workspace_id,
                    exc_info=True,
                )
                unreachable.add(workspace_id)
                continue
            for candidate in candidates:
                oauth_client_id = oauth_client_id_for_mcp_client(candidate)
                if oauth_client_id is None:
                    continue
                key = (oauth_client_id, candidate.workspace_id)
                if key in consent_keys:
                    related.setdefault(key, []).append(candidate)
        return related, unreachable

    def protected_resource_metadata(self) -> dict[str, Any]:
        return {
            "resource": self.resource,
            "authorization_servers": [self.issuer],
            "scopes_supported": list(ALLOWED_OAUTH_SCOPES),
            "bearer_methods_supported": ["header"],
            "resource_documentation": f"{self.issuer}/settings/integrations",
        }

    def authorization_server_metadata(self) -> dict[str, Any]:
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/oauth/authorize",
            "token_endpoint": f"{self.issuer}/oauth/token",
            "registration_endpoint": f"{self.issuer}/oauth/register",
            # Sprint_5 Phase_3 Task 3.5 (RFC 7009): revoke endpoint
            # advertised on the authorization-server metadata document so
            # native clients (Claude Code, Codex CLI) can discover it.
            "revocation_endpoint": f"{self.issuer}/oauth/revoke",
            "revocation_endpoint_auth_methods_supported": ["none"],
            "jwks_uri": f"{self.issuer}/.well-known/jwks.json",
            "response_types_supported": ["code"],
            "grant_types_supported": DEFAULT_GRANT_TYPES,
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": list(ALLOWED_OAUTH_SCOPES),
            "client_id_metadata_document_supported": True,
        }

    def jwks(self) -> dict[str, Any]:
        public_key = _load_private_key(self.settings).public_key()
        numbers = public_key.public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": JWT_KID,
                    "use": "sig",
                    "alg": "RS256",
                    "n": _uint_to_base64url(numbers.n),
                    "e": _uint_to_base64url(numbers.e),
                }
            ]
        }

    def register_client(
        self,
        *,
        payload: dict[str, Any],
        client_ip: str,
        _rate_limit_consumed: bool = False,
    ) -> RegisteredClient:
        if not self.settings.oauth_allow_dcr:
            raise InvalidClientRegistrationError("dynamic client registration disabled")
        # HTTP callers consume before JSON parsing; direct callers still count
        # invalid metadata, exactly once per registration attempt.
        if not _rate_limit_consumed:
            _consume_dcr_slot(client_ip)
        if not isinstance(payload, dict):
            raise InvalidClientRegistrationError(
                "client metadata must be a JSON object"
            )
        try:
            validate_supported_metadata(payload)
            redirects = validate_redirect_list(payload.get("redirect_uris"))
        except ValueError as exc:
            raise InvalidClientRegistrationError(str(exc)) from exc
        normalized_redirects = [validate_redirect_uri(uri) for uri in redirects]
        # Sprint_5 Phase_3 Task 3.2: scope subset of ALLOWED_OAUTH_SCOPES.
        # ``normalize_scope`` raises ``InvalidOAuthRequestError`` for unknown
        # scopes; DCR converts that to ``InvalidClientRegistrationError`` so
        # the /oauth/register endpoint returns 400 instead of 500 and the
        # error_code reads ``invalid_client_metadata`` per RFC 7591 §3.2.2.
        raw_scope = payload.get("scope")
        # Least-privilege default (RFC 7591): an omitted scope registers read-only,
        # not every scope. A client wanting mcp:write/mcp:agent must request it
        # explicitly (and the user still approves it at /oauth/authorize consent).
        default_scope = "mcp:read"
        try:
            scope = normalize_scope(default_scope if raw_scope is None else raw_scope)
        except InvalidOAuthRequestError as exc:
            raise InvalidClientRegistrationError(f"scope is invalid: {exc}") from exc
        # Sprint_5 Phase_3 Task 3.2: client_name length ∈ [1, 200]. Previously
        # silently truncated to 128 chars, which masked over-long submissions.
        raw_client_name = payload.get("client_name")
        if raw_client_name is None or raw_client_name == "":
            client_name = "Dynamic MCP Client"
        else:
            client_name = raw_client_name
        try:
            client_name = validate_client_name(client_name)
        except ValueError as exc:
            raise InvalidClientRegistrationError(str(exc)) from exc
        # Like the auth method, register only a supported value (RFC 7591 lets
        # the server replace metadata) instead of storing arbitrary text.
        application_type = (
            "web" if payload.get("application_type") == "web" else "native"
        )
        registration_identity = _dcr_registration_identity(
            client_name=client_name,
            redirect_uris=normalized_redirects,
            token_endpoint_auth_method="none",
            grant_types=DEFAULT_GRANT_TYPES,
            response_types=DEFAULT_RESPONSE_TYPES,
            application_type=application_type,
            scope=scope,
        )
        catalog_repository = self.catalog_repository or get_mcp_app_catalog_repository()
        catalog_association = catalog_repository.find_verified_oauth_association(
            registration_identity
        )
        with self.session_factory.begin() as session:
            # DCR is unauthenticated, so reuse requires an exact match of both
            # fallback identity fields and every effective security setting.
            # A matching name alone cannot replace redirects or broaden scope.
            existing = session.scalar(
                select(OAuthClient).where(
                    OAuthClient.registration_identity == registration_identity,
                )
            )
            if existing is not None:
                # Revoking the last consent retires the registration without
                # deleting its immutable row. Re-registration may safely revive
                # that public client id; the user must still grant new consent.
                existing.revoked_at = None
                _apply_verified_oauth_association(
                    existing,
                    metadata_fingerprint=registration_identity,
                    association=catalog_association,
                    replace_unconsented=False,
                )
                return _registered_client(existing)

            # Rows created before the identity column remain reusable. They are
            # deliberately not mutated here: the database uniqueness invariant
            # protects every new registration, while a legacy duplicate may
            # already exist and cannot be made unique safely at request time.
            candidates = session.scalars(
                select(OAuthClient).where(
                    OAuthClient.created_via == "dcr",
                    OAuthClient.client_name == client_name,
                )
            ).all()
            normalized_redirect_set = {
                _registration_identity_redirect_uri(uri) for uri in normalized_redirects
            }
            for existing in candidates:
                if (
                    {
                        _registration_identity_redirect_uri(uri)
                        for uri in existing.redirect_uris
                    }
                    == normalized_redirect_set
                    and existing.token_endpoint_auth_method == "none"
                    and list(existing.grant_types) == DEFAULT_GRANT_TYPES
                    and list(existing.response_types) == DEFAULT_RESPONSE_TYPES
                    and existing.application_type == application_type
                    and existing.scope == scope
                ):
                    existing.revoked_at = None
                    _apply_verified_oauth_association(
                        existing,
                        metadata_fingerprint=registration_identity,
                        association=catalog_association,
                        replace_unconsented=False,
                    )
                    return _registered_client(existing)

            client_id = f"dyn_{secrets.token_urlsafe(16)}"
            client = OAuthClient(
                id=client_id,
                client_name=client_name,
                redirect_uris=normalized_redirects,
                token_endpoint_auth_method="none",
                grant_types=DEFAULT_GRANT_TYPES,
                response_types=DEFAULT_RESPONSE_TYPES,
                application_type=application_type,
                scope=scope,
                created_via="dcr",
                registration_identity=registration_identity,
                **_oauth_catalog_model_values(
                    metadata_fingerprint=registration_identity,
                    association=catalog_association,
                ),
            )
            try:
                with session.begin_nested():
                    session.add(client)
                    session.flush()
            except IntegrityError:
                # Another identical DCR request won the unique-identity insert.
                # Re-read its immutable registration instead of returning a
                # second client id or surfacing a database error.
                existing = session.scalar(
                    select(OAuthClient).where(
                        OAuthClient.registration_identity == registration_identity,
                    )
                )
                if existing is None:
                    raise
                existing.revoked_at = None
                _apply_verified_oauth_association(
                    existing,
                    metadata_fingerprint=registration_identity,
                    association=catalog_association,
                    replace_unconsented=False,
                )
                return _registered_client(existing)
            # Sprint_5 Phase_3 Task 3.2 (ADR A11): global audit event with
            # ``workspace_id=NULL`` (DCR has no workspace yet). The Phase_1
            # ``emit_oauth_event`` helper normalises the str ``client.id`` via
            # UUIDv5 and preserves the original in ``payload.target_external_id``.
            emit_oauth_event(
                session=session,
                action="oauth.client.registered",
                target_id=client.id,
                target_kind="oauth_client",
                workspace_id=None,
                actor_kind="system",
                actor_id="dcr",
                payload={
                    "client_id": client.id,
                    "client_name": client.client_name,
                    "redirect_uris": list(client.redirect_uris),
                    "scope": client.scope,
                    "client_ip": client_ip,
                },
                retention_class="sensitive",
            )
            return _registered_client(client)

    def get_client(self, client_id: str) -> OAuthClient:
        _validate_client_id(client_id)
        with self.session_factory() as session:
            client = session.get(OAuthClient, client_id)
            if client is None or client.revoked_at is not None:
                raise InvalidOAuthRequestError("client is invalid")
            return client

    def _resolve_authorization_client(self, client_id: str) -> OAuthClient:
        """Resolve DCR/pre-registered clients or securely materialize a CIMD.

        A CIMD row is the persisted metadata snapshot used by relational OAuth
        state. Before the first grant it may be refreshed from the bounded
        fetch cache. Once a live code, consent, or refresh-token grant exists,
        the security-bearing snapshot is frozen: a later remote edit cannot
        widen redirects and a fetch outage cannot revoke a working grant.
        """

        _validate_client_id(client_id)
        with self.session_factory() as session:
            client = session.get(OAuthClient, client_id)
            if client is not None and client.created_via != "cimd":
                if client.revoked_at is not None:
                    raise InvalidOAuthRequestError("client is invalid")
                client.redirect_uris = list(client.redirect_uris)
                return _validate_client_snapshot(client)
            if client is not None and _cimd_has_established_grant(session, client_id):
                if client.revoked_at is not None:
                    raise InvalidOAuthRequestError("client is invalid")
                client.redirect_uris = list(client.redirect_uris)
                return _validate_client_snapshot(client)

            if client is None and urlparse(client_id).scheme.lower() != "https":
                raise InvalidOAuthRequestError("client is invalid")

        try:
            metadata = fetch_client_metadata_document(client_id)
            if metadata.client_id != client_id:
                raise InvalidOAuthRequestError("client metadata is unavailable")
            registration = _validated_cimd_registration(metadata)
        except Exception:
            # This is an unauthenticated network probe. No DNS result,
            # response body, transport exception, or validation detail may
            # cross the OAuth boundary or reach normal request logs.
            raise InvalidOAuthRequestError("client metadata is unavailable") from None

        metadata_fingerprint = _dcr_registration_identity(
            client_name=registration.client_name,
            redirect_uris=list(registration.redirect_uris),
            token_endpoint_auth_method="none",
            grant_types=list(registration.grant_types),
            response_types=list(registration.response_types),
            application_type=registration.application_type,
            scope=registration.scope,
        )
        catalog_repository = self.catalog_repository or get_mcp_app_catalog_repository()
        catalog_association = catalog_repository.find_verified_oauth_association(
            metadata_fingerprint
        )

        with self.session_factory.begin() as session:
            # Re-check after the network call. A concurrent request may have
            # established the first grant while this request was fetching.
            client = session.get(OAuthClient, client_id)
            if client is not None and client.created_via != "cimd":
                if client.revoked_at is not None:
                    raise InvalidOAuthRequestError("client is invalid")
                client.redirect_uris = list(client.redirect_uris)
                return _validate_client_snapshot(client)
            if client is not None and _cimd_has_established_grant(session, client_id):
                if client.revoked_at is not None:
                    raise InvalidOAuthRequestError("client is invalid")
                client.redirect_uris = list(client.redirect_uris)
                return _validate_client_snapshot(client)
            if client is None:
                client = OAuthClient(
                    id=client_id,
                    client_name=registration.client_name,
                    redirect_uris=list(registration.redirect_uris),
                    token_endpoint_auth_method="none",
                    grant_types=list(registration.grant_types),
                    response_types=list(registration.response_types),
                    application_type=registration.application_type,
                    scope=registration.scope,
                    created_via="cimd",
                    registration_identity=None,
                    **_oauth_catalog_model_values(
                        metadata_fingerprint=metadata_fingerprint,
                        association=catalog_association,
                    ),
                )
                try:
                    with session.begin_nested():
                        session.add(client)
                        session.flush()
                except IntegrityError:
                    client = session.get(OAuthClient, client_id)
                    if client is None:
                        raise InvalidOAuthRequestError(
                            "client metadata is unavailable"
                        ) from None
                    _apply_verified_oauth_association(
                        client,
                        metadata_fingerprint=metadata_fingerprint,
                        association=catalog_association,
                        replace_unconsented=True,
                    )
            else:
                client.client_name = registration.client_name
                client.redirect_uris = list(registration.redirect_uris)
                client.token_endpoint_auth_method = "none"
                client.grant_types = list(registration.grant_types)
                client.response_types = list(registration.response_types)
                client.application_type = registration.application_type
                client.scope = registration.scope
                client.revoked_at = None
                _apply_verified_oauth_association(
                    client,
                    metadata_fingerprint=metadata_fingerprint,
                    association=catalog_association,
                    replace_unconsented=True,
                )
            client.redirect_uris = list(client.redirect_uris)
            return _validate_client_snapshot(client)

    def validate_authorization_request(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str,
        scope: str,
        state: str,
        response_type: str = "code",
        workspace_id: UUID | None = None,
    ) -> OAuthClient:
        if response_type != "code":
            raise InvalidOAuthRequestError("response_type must be code")
        normalized_scope = normalize_scope(scope)
        validate_pkce_request(code_challenge, code_challenge_method)
        validate_state(state)
        _validate_request_redirect(redirect_uri)
        client = self._resolve_authorization_client(client_id)
        if "authorization_code" not in client.grant_types:
            raise InvalidOAuthRequestError("grant_type is not registered")
        if response_type not in client.response_types:
            raise InvalidOAuthRequestError("response_type is not registered")
        if not redirect_uri_matches(redirect_uri, list(client.redirect_uris)):
            raise InvalidOAuthRequestError("redirect_uri is not registered")
        if not scope_covers(client.scope, normalized_scope):
            raise InvalidOAuthRequestError("requested scope is not registered")
        client.redirect_uris = list(client.redirect_uris)
        return client

    def list_user_consents(
        self,
        *,
        user_id: UUID,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Return the user's OAuth consents, one entry per (user, client).

        P10.3 T3 (section 2.3, Codex review #3/#9/R2-3): a client can now
        hold several ``oauth_consents`` rows for the same user -- one per
        allowed workspace -- so a page row is the whole (user, client)
        GROUP, not one workspace. The keyset cursor is ``client_id`` alone
        (immutable once a row exists); the per-group row count is small in
        practice (bounded by how many workspaces one user has granted one
        client), so this reads every remaining row for the page's clients in
        one query and groups them in Python rather than attempting a
        SQL-side "top N groups" query.
        """

        with self.session_factory() as session:
            cursor_client_id = decode_oauth_consent_cursor(cursor)
            query = (
                select(OAuthConsent)
                .add_columns(OAuthClient)
                .join(OAuthClient, OAuthClient.id == OAuthConsent.client_id)
                .where(
                    OAuthConsent.user_id == user_id,
                    OAuthConsent.removed_at.is_(None),
                )
            )
            if cursor_client_id is not None:
                query = query.where(OAuthConsent.client_id > cursor_client_id)
            query = query.order_by(
                OAuthConsent.client_id,
                OAuthConsent.workspace_id,
            )
            rows = session.execute(query).all()

            groups: dict[str, list[tuple[OAuthConsent, OAuthClient]]] = {}
            for consent, client in rows:
                groups.setdefault(consent.client_id, []).append((consent, client))
            ordered_client_ids = list(groups.keys())
            page_client_ids = ordered_client_ids[:limit]
            next_cursor = (
                encode_oauth_consent_cursor(page_client_ids[-1])
                if len(ordered_client_ids) > limit and page_client_ids
                else None
            )

            clients_by_id = {
                client_id: groups[client_id][0][1] for client_id in page_client_ids
            }
            workspace_ids = {
                consent.workspace_id
                for client_id in page_client_ids
                for consent, _client in groups[client_id]
            }
            workspaces_by_id = (
                {
                    workspace.id: workspace
                    for workspace in session.scalars(
                        select(Workspace).where(Workspace.id.in_(workspace_ids))
                    ).all()
                }
                if workspace_ids
                else {}
            )
            memberships_by_workspace = (
                {
                    membership.workspace_id: membership.role
                    for membership in session.scalars(
                        select(WorkspaceMembership).where(
                            WorkspaceMembership.user_id == user_id,
                            WorkspaceMembership.workspace_id.in_(workspace_ids),
                        )
                    ).all()
                }
                if workspace_ids
                else {}
            )
            # MCPClient is workspace-plane; this control session cannot see it.
            # One shard read per workspace on the page, and a shard that is
            # down degrades that consent's display rather than failing the
            # listing -- this is a read-only screen.
            related_by_key: dict[tuple[str, UUID], list[MCPClient]] = {}
            if workspace_ids:
                related_by_key, _unreachable = self._related_mcp_clients_by_key(
                    user_id=user_id,
                    consent_keys={
                        (consent.client_id, consent.workspace_id)
                        for client_id in page_client_ids
                        for consent, _client in groups[client_id]
                    },
                )

            now = utc_now()
            # Section 2.3's connection-health redefinition made "family_alive"
            # and "oauth_client_is_live" GLOBAL (user, client) facts, so every
            # row in a group agrees on them; only physical_status (this row's
            # own workspace's MCPClient) can vary per row. One representative
            # key per group therefore carries the group's shared
            # active/expired/revoked answer -- physical_status is forced
            # "active" here on purpose so a *different* workspace's disabled
            # MCPClient cannot mask a still-live family; the group-level
            # "disabled" override below is computed separately, from every
            # row's related MCPClients, not from this representative alone.
            group_state_keys: dict[str, OAuthConnectionKey] = {
                client_id: (user_id, client_id, groups[client_id][0][0].workspace_id)
                for client_id in page_client_ids
            }
            states = _derive_oauth_connection_states_batch(
                session,
                keys=set(group_state_keys.values()),
                physical_status=dict.fromkeys(group_state_keys.values(), "active"),
                now=now,
                oauth_clients=clients_by_id,
                known_consents=set(group_state_keys.values()),
            )

            result: list[dict[str, Any]] = []
            for client_id in page_client_ids:
                group = groups[client_id]
                client = clients_by_id[client_id]
                all_related: list[MCPClient] = []
                for consent, _client in group:
                    all_related.extend(
                        related_by_key.get(
                            (consent.client_id, consent.workspace_id), []
                        )
                    )
                group_disabled = bool(all_related) and all(
                    item.status != "active" for item in all_related
                )
                base_state, next_expiry = states[group_state_keys[client_id]]
                connection_state = "disabled" if group_disabled else base_state
                first_consent = group[0][0]
                mcp_client_ids: list[UUID] = []
                for consent, _client in group:
                    mcp_client_ids.extend(
                        item.id
                        for item in related_by_key.get(
                            (consent.client_id, consent.workspace_id), []
                        )
                    )
                workspaces_payload = [
                    {
                        "id": str(consent.workspace_id),
                        "title": workspaces_by_id[consent.workspace_id].title,
                        "slug": workspaces_by_id[consent.workspace_id].slug,
                        "membership_role": memberships_by_workspace.get(
                            consent.workspace_id
                        ),
                    }
                    for consent, _client in group
                    if consent.workspace_id in workspaces_by_id
                ]
                result.append(
                    {
                        "client_id": client_id,
                        "client_name": (
                            first_consent.catalog_app_label_snapshot
                            or client.client_name
                        ),
                        "catalog_app_id": first_consent.catalog_app_id,
                        "catalog_app_label_snapshot": (
                            first_consent.catalog_app_label_snapshot
                        ),
                        "catalog_revision": first_consent.catalog_revision,
                        "scope": first_consent.scope,
                        "workspaces": workspaces_payload,
                        "granted_at": first_consent.granted_at,
                        "connection_state": connection_state,
                        "last_used_at": client.last_used_at,
                        "next_expiry": next_expiry,
                        "mcp_client_ids": mcp_client_ids,
                        "refresh_ttl_seconds": first_consent.refresh_ttl_seconds,
                        "effective_refresh_ttl_seconds": (
                            effective_consent_refresh_ttl_seconds(
                                self.settings,
                                first_consent,
                            )
                        ),
                        "sliding_enabled": first_consent.sliding_enabled,
                        "effective_sliding_enabled": (
                            effective_consent_sliding_enabled(
                                self.settings,
                                first_consent,
                            )
                        ),
                        # MCP-05(b): the session-length UI control has no
                        # other way to learn the instance ceiling
                        # (effective_refresh_ttl_seconds is min(instance,
                        # requested), indistinguishable from the ceiling
                        # once the explicit consent TTL is smaller).
                        "instance_max_refresh_ttl_seconds": (
                            self.settings.oauth_refresh_token_ttl_seconds
                        ),
                    }
                )
            return {"items": result, "next_cursor": next_cursor}

    def update_consent_settings(
        self,
        *,
        user_id: UUID,
        client_id: str,
        refresh_ttl_seconds: int | None,
        update_refresh_ttl: bool,
        sliding_enabled: bool | None,
        update_sliding: bool,
        workspace_ids: list[UUID] | None = None,
    ) -> dict[str, Any]:
        """Persist future-rotation policy and/or the allowed-workspace set.

        P10.3 T3 (section 2.3/2.4): the unit is now the whole (user, client)
        GROUP, not one workspace's row -- TTL/sliding are documented (user,
        client)-common settings (section 2.1) applied to every live row at
        once, and ``workspace_ids`` (when given) is a full-set REPLACE
        mirroring ``ApiTokenService._replace_workspace_grants`` (T4): a
        newly-added workspace gets a fresh row copying the group's common
        scope, a dropped one is soft-removed (``removed_at``) rather than
        hard-deleted so a T4.5 fan-out can still tombstone its workspace-plane
        replica -- distinct from the authorize-screen's own hard-delete
        set-editing path in ``issue_authorization_code``, which has no
        replica-fan-out step to wait for.
        """

        _validate_workspace_ids(workspace_ids)
        if workspace_ids is not None and not workspace_ids:
            raise InvalidOAuthRequestError("workspace_ids must not be empty")

        # MCPClient is workspace-plane and needs its own session(s) -- opened
        # by _related_mcp_clients_by_key below. That call (and every other
        # workspace-plane read this method needs) must run AFTER the control
        # write transaction has committed and closed, never inside it: in the
        # single-database profile both factories are the same SQLite file,
        # and opening a second session inside an open write transaction is
        # the nesting backend/api/deps.py warns about -- it silently
        # perturbed this exact method's writes (a PATCH's removed_at update
        # never reached the database even though the response looked
        # correct, because the response was built from the still-open
        # transaction's in-memory ORM objects, not a post-commit re-read).
        # So: do ALL control-plane writes and commit FIRST, capturing plain
        # values (not ORM objects, which would be detached once the session
        # closes) for the response; only then open any workspace-plane
        # session.
        with self.session_factory.begin() as session:
            acquire_write_intent(session)
            live_group = list(
                session.scalars(
                    select(OAuthConsent)
                    .where(
                        OAuthConsent.user_id == user_id,
                        OAuthConsent.client_id == client_id,
                        OAuthConsent.removed_at.is_(None),
                    )
                    .with_for_update()
                )
            )
            if not live_group:
                raise OAuthConsentNotFoundError("consent not found")
            client = session.get(OAuthClient, client_id)
            if client is None:
                raise OAuthConsentNotFoundError("consent not found")

            old_refresh_ttl = live_group[0].refresh_ttl_seconds
            old_sliding = live_group[0].sliding_enabled
            if update_refresh_ttl:
                if (
                    refresh_ttl_seconds is not None
                    and refresh_ttl_seconds
                    > self.settings.oauth_refresh_token_ttl_seconds
                ):
                    raise OAuthConsentTTLAboveInstanceError(
                        "requested refresh TTL exceeds the instance maximum of "
                        f"{self.settings.oauth_refresh_token_ttl_seconds} seconds"
                    )
                for row in live_group:
                    row.refresh_ttl_seconds = refresh_ttl_seconds
            if update_sliding:
                if sliding_enabled is None:
                    raise InvalidOAuthRequestError(
                        "sliding_enabled must be true or false"
                    )
                for row in live_group:
                    row.sliding_enabled = sliding_enabled
            if update_refresh_ttl or update_sliding:
                self._emit_user_audit(
                    session=session,
                    user_id=user_id,
                    action="oauth.consent.settings.updated",
                    target_kind="oauth_consent",
                    target_id=user_id,
                    workspace_id=None,
                    payload={
                        "client_id": client_id,
                        "old_refresh_ttl_seconds": old_refresh_ttl,
                        "new_refresh_ttl_seconds": live_group[0].refresh_ttl_seconds,
                        "old_sliding_enabled": old_sliding,
                        "new_sliding_enabled": live_group[0].sliding_enabled,
                    },
                )

            if workspace_ids is not None:
                live_group = self._replace_consent_workspace_grants(
                    session,
                    user_id=user_id,
                    client_id=client_id,
                    client=client,
                    live_group=live_group,
                    workspace_ids=workspace_ids,
                )
            session.flush()

            # Snapshot into plain values now, while the rows are still
            # attached -- every workspace-plane read below opens its own
            # session only after this ``with`` block (and its commit) exits.
            client_name = client.client_name
            client_last_used_at = client.last_used_at
            row_snapshots = [
                {
                    "workspace_id": consent.workspace_id,
                    "scope": consent.scope,
                    "catalog_app_id": consent.catalog_app_id,
                    "catalog_app_label_snapshot": consent.catalog_app_label_snapshot,
                    "catalog_revision": consent.catalog_revision,
                    "granted_at": consent.granted_at,
                    "refresh_ttl_seconds": consent.refresh_ttl_seconds,
                    "sliding_enabled": consent.sliding_enabled,
                }
                for consent in live_group
            ]

        now = utc_now()
        workspace_id_set = {row["workspace_id"] for row in row_snapshots}
        related_by_key: dict[tuple[str, UUID], list[MCPClient]] = {}
        if workspace_id_set:
            related_by_key, _unreachable = self._related_mcp_clients_by_key(
                user_id=user_id,
                consent_keys={(client_id, wid) for wid in workspace_id_set},
            )
        with self.session_factory() as session:
            workspaces_by_id = (
                {
                    workspace.id: workspace
                    for workspace in session.scalars(
                        select(Workspace).where(Workspace.id.in_(workspace_id_set))
                    ).all()
                }
                if workspace_id_set
                else {}
            )
            memberships_by_workspace = (
                {
                    membership.workspace_id: membership.role
                    for membership in session.scalars(
                        select(WorkspaceMembership).where(
                            WorkspaceMembership.user_id == user_id,
                            WorkspaceMembership.workspace_id.in_(workspace_id_set),
                        )
                    ).all()
                }
                if workspace_id_set
                else {}
            )
            mcp_client_ids: list[UUID] = []
            all_related: list[MCPClient] = []
            for row in row_snapshots:
                related = related_by_key.get((client_id, row["workspace_id"]), [])
                mcp_client_ids.extend(item.id for item in related)
                all_related.extend(related)
            group_disabled = bool(all_related) and all(
                item.status != "active" for item in all_related
            )
            representative = row_snapshots[0] if row_snapshots else None
            if representative is None:
                connection_state: str = "revoked"
                next_expiry = None
            else:
                base_state, next_expiry = derive_oauth_connection_state(
                    session,
                    client_id=client_id,
                    workspace_id=representative["workspace_id"],
                    user_id=user_id,
                    physical_status="active",
                    now=now,
                )
                connection_state = "disabled" if group_disabled else base_state
            workspaces_payload = [
                {
                    "id": str(row["workspace_id"]),
                    "title": workspaces_by_id[row["workspace_id"]].title,
                    "slug": workspaces_by_id[row["workspace_id"]].slug,
                    "membership_role": memberships_by_workspace.get(
                        row["workspace_id"]
                    ),
                }
                for row in row_snapshots
                if row["workspace_id"] in workspaces_by_id
            ]

            def _effective_refresh_ttl(row: dict[str, Any] | None) -> int:
                if row is None:
                    return self.settings.oauth_refresh_token_ttl_seconds
                requested = row["refresh_ttl_seconds"]
                return min(
                    self.settings.oauth_refresh_token_ttl_seconds,
                    requested
                    if requested is not None
                    else self.settings.oauth_refresh_token_ttl_seconds,
                )

            def _effective_sliding(row: dict[str, Any] | None) -> bool:
                if row is None:
                    return False
                return bool(
                    self.settings.oauth_refresh_token_sliding_enabled
                    and row["sliding_enabled"]
                )

            return {
                "client_id": client_id,
                "client_name": client_name,
                "catalog_app_id": (
                    representative["catalog_app_id"] if representative else None
                ),
                "catalog_app_label_snapshot": (
                    representative["catalog_app_label_snapshot"]
                    if representative
                    else None
                ),
                "catalog_revision": (
                    representative["catalog_revision"] if representative else None
                ),
                "scope": representative["scope"] if representative else "",
                "workspaces": workspaces_payload,
                "granted_at": (representative["granted_at"] if representative else now),
                "connection_state": connection_state,
                "last_used_at": client_last_used_at,
                "next_expiry": next_expiry,
                "mcp_client_ids": mcp_client_ids,
                "refresh_ttl_seconds": (
                    representative["refresh_ttl_seconds"] if representative else None
                ),
                "effective_refresh_ttl_seconds": _effective_refresh_ttl(representative),
                "sliding_enabled": (
                    representative["sliding_enabled"] if representative else False
                ),
                "effective_sliding_enabled": _effective_sliding(representative),
                "instance_max_refresh_ttl_seconds": (
                    self.settings.oauth_refresh_token_ttl_seconds
                ),
            }

    def _replace_consent_workspace_grants(
        self,
        session: Session,
        *,
        user_id: UUID,
        client_id: str,
        client: OAuthClient,
        live_group: list[OAuthConsent],
        workspace_ids: list[UUID],
    ) -> list[OAuthConsent]:
        """Full-set REPLACE of an OAuth consent group's allowed workspaces.

        Mirrors ``ApiTokenService._replace_workspace_grants`` (T4): every
        named workspace is re-validated for live membership, a newly-added
        one gets a fresh row copying the group's common ``scope`` (section
        2.3's scope-identity invariant), and a dropped one is soft-removed
        (``removed_at``) so a future fan-out (T4.5) can still tombstone its
        workspace-plane replica -- never hard-deleted here.
        """

        if len(set(workspace_ids)) != len(workspace_ids):
            raise InvalidOAuthRequestError("duplicate workspace_id in workspace_ids")
        common_scope = live_group[0].scope
        common_refresh_ttl = live_group[0].refresh_ttl_seconds
        common_sliding = live_group[0].sliding_enabled
        live_by_workspace = {row.workspace_id: row for row in live_group}
        requested_ids = set(workspace_ids)
        added: list[UUID] = []
        removed: list[UUID] = []
        now = utc_now()
        # T4.5: one fan-out request per row this method adds or soft-removes
        # -- an unchanged live row (still requested, scope untouched by this
        # REPLACE) needs none.
        fan_out_rows: list[OAuthConsent] = []
        for workspace_id in workspace_ids:
            if workspace_id in live_by_workspace:
                continue
            membership = session.get(WorkspaceMembership, (user_id, workspace_id))
            if membership is None:
                raise InvalidOAuthRequestError("workspace access is required")
            new_row = OAuthConsent(
                user_id=user_id,
                client_id=client_id,
                workspace_id=workspace_id,
                scope=common_scope,
                catalog_app_id=client.catalog_app_id,
                catalog_app_label_snapshot=client.catalog_app_label_snapshot,
                catalog_revision=client.catalog_revision,
                refresh_ttl_seconds=common_refresh_ttl,
                sliding_enabled=common_sliding,
                granted_at=now,
                desired_scopes=normalize_scope(common_scope).split(),
                revision=1,
                sync_state="pending",
            )
            session.add(new_row)
            live_group.append(new_row)
            added.append(workspace_id)
            fan_out_rows.append(new_row)
        for workspace_id, row in live_by_workspace.items():
            if workspace_id in requested_ids:
                continue
            row.removed_at = now
            row.revision += 1
            row.sync_state = "pending"
            live_group.remove(row)
            removed.append(workspace_id)
            fan_out_rows.append(row)
        if fan_out_rows:
            # One lookup for the whole (user, client) family -- every row's
            # replica shares the same workspace-plane credential_id (section
            # 2.6/module docstring in credential_replica_service.py). May be
            # None (family has never exchanged a token yet); the shared
            # apply function treats that as an ordinary, recorded fan-out
            # failure, not a crash here.
            replica_credential_id = resolve_oauth_replica_credential_id(
                session, user_id=user_id, client_id=client_id
            )
            for fan_out_row in fan_out_rows:
                register_fan_out(
                    session,
                    CredentialReplicaFanOutRequest(
                        credential_kind="oauth",
                        credential_id=replica_credential_id,
                        workspace_id=fan_out_row.workspace_id,
                        user_id=user_id,
                        desired_scopes=tuple(fan_out_row.desired_scopes),
                        revision=fan_out_row.revision,
                        removed_at=fan_out_row.removed_at,
                        client_id=client_id,
                    ),
                )
        if added or removed:
            self._emit_user_audit(
                session=session,
                user_id=user_id,
                action="oauth.consent.workspaces_changed",
                target_kind="oauth_consent",
                target_id=user_id,
                workspace_id=None,
                payload={
                    "client_id": client_id,
                    "added": [str(w) for w in added],
                    "removed": [str(w) for w in removed],
                },
            )
        return live_group

    def revoke_consent(self, *, user_id: UUID, client_id: str) -> bool:
        """Revoke a user's consents for a client and invalidate every
        outstanding credential: refresh tokens, live access JWTs (via the
        jti denylist), and unconsumed authorization codes."""

        # Management addresses an existing, user-owned registration verbatim.
        # Legacy CIMD IDs used a code-point limit and may exceed today's bytes.
        with self.session_factory.begin() as session:
            consents = session.scalars(
                select(OAuthConsent).where(
                    OAuthConsent.user_id == user_id,
                    OAuthConsent.client_id == client_id,
                )
            ).all()
            if not consents:
                return False
            for consent in consents:
                session.delete(consent)
            now = utc_now()
            revoked_refresh_count, revoked_access_count = (
                revoke_all_live_tokens_for_user_client(
                    session,
                    user_id=user_id,
                    client_id=client_id,
                    revoked_at=now,
                )
            )
            authorization_codes = session.scalars(
                select(OAuthCode).where(
                    OAuthCode.user_id == user_id,
                    OAuthCode.client_id == client_id,
                    OAuthCode.consumed_at.is_(None),
                    OAuthCode.expires_at > now,
                )
            ).all()
            for code in authorization_codes:
                code.consumed_at = now
            session.flush()
            client = session.get(OAuthClient, client_id)
            remaining_consent = session.scalar(
                select(OAuthConsent.client_id)
                .where(OAuthConsent.client_id == client_id)
                .limit(1)
            )
            if client is not None and remaining_consent is None:
                client.revoked_at = now
            self._emit_user_audit(
                session=session,
                user_id=user_id,
                action="oauth.consent.revoked",
                target_kind="oauth_consent",
                target_id=user_id,
                payload={
                    "client_id": client_id,
                    "revoked_refresh_tokens": revoked_refresh_count,
                    "revoked_access_tokens": revoked_access_count,
                    "revoked_authorization_codes": len(authorization_codes),
                },
            )
        return True

    def cleanup_user_connections(
        self,
        *,
        user_id: UUID,
        dry_run: bool = False,
    ) -> dict[str, int]:
        """Remove expired/revoked consent rows while retaining referenced
        clients and any row a workspace has deliberately disabled.

        P10.3 T3 (section 2.3): row-level eligibility is UNCHANGED from
        pre-T3 -- it still uses the per-row ``derive_oauth_connection_state``
        (physical_status from THIS row's own workspace's MCPClient set), so a
        row whose MCPClient is explicitly disabled is never eligible even
        when the (user, client) family is globally dead (TEN-01C,
        test_cloud_composition.py::
        test_split_provider_consent_cleanup_refuses_to_delete_on_an_unreadable_plane).
        What DID change is the family-revoke call: pre-T3 it ran once per
        eligible ROW; now the family is a (user, client)-wide unit (T1
        dropped OAuthRefreshToken.workspace_id), so it is deduplicated to run
        at most once per CLIENT that had at least one eligible row, instead
        of redundantly once per row.
        """

        # Which consents exist, and which workspaces they span, is a
        # control-plane question; the MCPClient status that decides
        # eligibility is workspace-plane. Read both BEFORE the delete
        # transaction opens -- in the single-database profile a workspace
        # session opened inside an IMMEDIATE write transaction is the nesting
        # backend/api/deps.py warns about.
        with self.session_factory() as session:
            consent_keys = {
                (consent.client_id, consent.workspace_id)
                for consent in session.scalars(
                    select(OAuthConsent).where(
                        OAuthConsent.user_id == user_id,
                        OAuthConsent.removed_at.is_(None),
                    )
                )
            }
        # This path DELETES consents and revokes their grant families, so it
        # fails closed: a shard we cannot read excludes its consents entirely
        # rather than falling through to physical_status="active". That
        # fall-through is exactly what makes a live (or deliberately
        # disabled) connection look "expired" and get collected.
        related_by_key, unreachable = self._related_mcp_clients_by_key(
            user_id=user_id,
            consent_keys=consent_keys,
        )

        context = self.session_factory() if dry_run else self.session_factory.begin()
        with context as session:
            now = utc_now()
            consents = session.scalars(
                select(OAuthConsent).where(
                    OAuthConsent.user_id == user_id,
                    OAuthConsent.removed_at.is_(None),
                )
            ).all()
            eligible: list[OAuthConsent] = []
            related_ids: set[UUID] = set()
            eligible_client_ids: set[str] = set()
            for consent in consents:
                if consent.workspace_id in unreachable:
                    logger.warning(
                        "oauth cleanup: skipping consent for workspace %s -- its "
                        "shard is unreachable, so its connection state cannot be "
                        "established and deletion would be a guess",
                        consent.workspace_id,
                    )
                    continue
                related_clients = related_by_key.get(
                    (consent.client_id, consent.workspace_id), []
                )
                physical_status = (
                    "disabled"
                    if any(item.status != "active" for item in related_clients)
                    else "active"
                )
                state, _ = derive_oauth_connection_state(
                    session,
                    client_id=consent.client_id,
                    workspace_id=consent.workspace_id,
                    user_id=user_id,
                    physical_status=physical_status,
                    now=now,
                )
                if state not in {"expired", "revoked"}:
                    continue
                eligible.append(consent)
                related_ids.update(item.id for item in related_clients)
                eligible_client_ids.add(consent.client_id)

            if dry_run:
                return {
                    "eligible_consent_count": len(eligible),
                    "affected_mcp_client_count": len(related_ids),
                    "cleaned_consent_count": 0,
                }

            for consent in eligible:
                session.delete(consent)
            # Deduplicated: at most one family-revoke call per client, not
            # once per eligible row (the pre-T3 redundancy this function used
            # to carry -- harmless since revoke is idempotent, but wasteful
            # whenever a client had more than one eligible workspace row).
            for client_id in eligible_client_ids:
                revoke_live_token_grant_families(
                    session,
                    user_id=user_id,
                    client_id=client_id,
                    revoked_at=now,
                )
            session.flush()
            for client_id in eligible_client_ids:
                remaining_consent = session.scalar(
                    select(OAuthConsent.client_id)
                    .where(OAuthConsent.client_id == client_id)
                    .limit(1)
                )
                if remaining_consent is None:
                    client = session.get(OAuthClient, client_id)
                    if client is not None:
                        client.revoked_at = now
            result = {
                "eligible_consent_count": len(eligible),
                "affected_mcp_client_count": len(related_ids),
                "cleaned_consent_count": len(eligible),
            }
        return result

    def has_existing_consent(
        self,
        *,
        user_id: UUID,
        client_id: str,
        scope: str,
    ) -> bool:
        """Return ``True`` when a live consent for this (user, client) covers
        ``scope``, for ANY of its allowed workspaces.

        P10.3 T3 (section 2.3): consent is a (user, client)-group fact now, not
        a per-workspace one -- an existing consent for this client (however
        many workspaces it currently names) skips the authorize screen
        entirely and goes straight to the handoff page; set-editing from that
        point on is Settings' job, not this check's.
        """

        normalized_scope = normalize_scope(scope)
        with self.session_factory() as session:
            consent = session.scalar(
                select(OAuthConsent)
                .where(
                    OAuthConsent.user_id == user_id,
                    OAuthConsent.client_id == client_id,
                    OAuthConsent.removed_at.is_(None),
                )
                .limit(1)
            )
            return consent is not None and scope_covers(
                consent.scope,
                normalized_scope,
            )

    def record_authorization_request(
        self,
        *,
        user_id: UUID,
        client_id: str,
        client_name: str,
        redirect_uri: str,
        scope: str,
        existing_consent: bool,
    ) -> None:
        """Audit a genuinely new authorization request (account-scoped).

        P10.3 T3: no single ``workspace_id`` names this request any more --
        the checklist screen the user is about to see may cover several
        workspaces at once (section 2.3) -- so this is an account-scoped
        (``workspace_id=None``) audit row, matching
        ``ApiTokenService.create_token``'s ``auth.api_token.created`` for the
        same reason (D-01v2: several workspaces, no single owning one).
        """

        normalized_scope = normalize_scope(scope)
        with self.session_factory.begin() as session:
            user = session.get(User, user_id)
            if user is None or user.status != "active":
                raise InvalidOAuthRequestError("user is invalid")
            # Only record a genuinely new access request (the consent form is
            # shown and the user will decide). An existing-consent reconnect
            # auto-redirects without a decision and was already audited at grant
            # time; re-emitting a sensitive, redirect_uri-bearing row on every
            # reconnect / refresh / prefetch would inflate the audit log with
            # duplicate PII (C7). The user-validity precondition above still runs
            # for both paths.
            if existing_consent:
                return
            AuditService().emit(
                session=session,
                workspace_id=None,
                actor_kind="human",
                actor_id=str(user_id),
                action="oauth.authorization.requested",
                target_kind="oauth_authorization",
                target_id=user_id,
                payload={
                    "client_id": client_id,
                    "client_name": client_name,
                    "redirect_uri": redirect_uri,
                    "scope": normalized_scope,
                    "existing_consent": existing_consent,
                },
                event_summary=f"{client_name} requested MCP OAuth access",
                retention_class="sensitive",
            )

    def authorize_workspace_options(
        self,
        *,
        user_id: UUID,
        client_id: str,
        preselected_workspace_ids: Collection[UUID] = (),
    ) -> list[AuthorizeWorkspaceOption]:
        """Build the workspace checklist for the initial consent screen.

        Section 2.3: default-checked = this (user, client) pair's existing
        live consent workspace set; if there is none, the ``workspace_id``
        query hint (repeatable, membership-validated) when present, else the
        user's one resolved default workspace. Every returned option is a
        workspace the user currently holds membership in; an authorize
        request from a user with zero memberships returns an empty list (the
        caller renders a submit-disabled empty checklist).

        Uses a COMMITTING transaction, not a read-only session: the no-hint,
        no-existing-consent fallback calls ``resolve_workspace_for_actor``,
        whose single-membership path can repair a stale/inaccessible
        ``User.default_workspace_id`` to the live Personal workspace
        (``repair_default_workspace_to_personal``) -- a real write that must
        survive past this call, exactly as the deleted
        ``resolve_authorization_workspace`` committed it.
        """

        with self.session_factory.begin() as session:
            memberships = list_user_workspaces(session, user_id)
            if not memberships:
                return []
            member_ids = {workspace.id for workspace, _role in memberships}
            existing_ids = set(
                session.scalars(
                    select(OAuthConsent.workspace_id).where(
                        OAuthConsent.user_id == user_id,
                        OAuthConsent.client_id == client_id,
                        OAuthConsent.removed_at.is_(None),
                    )
                ).all()
            )
            checked_ids = existing_ids & member_ids
            if not checked_ids:
                hinted_ids = set(preselected_workspace_ids) & member_ids
                if hinted_ids:
                    checked_ids = hinted_ids
                else:
                    actor = SimpleNamespace(
                        actor_kind="human",
                        actor_id=str(user_id),
                        user_id=user_id,
                        token_kind="session",
                        token_workspace_ids=(),
                    )
                    try:
                        resolved = resolve_workspace_for_actor(
                            session=session,
                            actor=actor,
                            requested_workspace_id=None,
                        )
                        if resolved.workspace_id in member_ids:
                            checked_ids = {resolved.workspace_id}
                    except WorkspaceAmbiguityError:
                        checked_ids = set()
            ordered = sorted(
                memberships,
                key=lambda pair: (pair[0].id not in checked_ids, pair[0].title.lower()),
            )
            return [
                AuthorizeWorkspaceOption(
                    id=workspace.id,
                    title=workspace.title,
                    slug=workspace.slug,
                    checked=workspace.id in checked_ids,
                )
                for workspace, _role in ordered
            ]

    def deny_authorization_request(
        self,
        *,
        user_id: UUID,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str,
        scope: str,
        state: str,
        response_type: str = "code",
    ) -> None:
        self.validate_authorization_request(
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            scope=scope,
            state=state,
            response_type=response_type,
        )
        with self.session_factory.begin() as session:
            user = session.get(User, user_id)
            if user is None or user.status != "active":
                raise InvalidOAuthRequestError("user is invalid")
            self._emit_user_audit(
                session=session,
                user_id=user_id,
                action="oauth.permission.denied",
                target_kind="oauth_authorization",
                target_id=user_id,
                payload={
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "scope": normalize_scope(scope),
                },
            )

    def issue_authorization_code(
        self,
        *,
        user_id: UUID,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str,
        scope: str,
        state: str,
        response_type: str = "code",
        grant_consent: bool = True,
        workspace_ids: list[UUID] | None = None,
    ) -> str:
        """Issue an authorization code.

        P10.3 T3 (D-01v3-g): stays control-plane only -- no workspace session
        is opened here. ``OAuthCode`` itself carries no workspace (T1 already
        dropped the column). ``workspace_ids`` only matters when
        ``grant_consent`` is True (the interactive Allow decision): it is the
        checklist's full selected set, upserted/deleted against the (user,
        client) consent group (section 2.3's own set-editing path -- hard
        DELETE for a deselected row, distinct from the Settings PATCH's
        soft-remove in ``update_consent_settings``). The non-interactive
        reconnect path (``grant_consent=False``, GET's existing-consent
        skip) needs no workspace at all: it only re-checks that some live
        consent for this (user, client) still covers the requested scope.
        """

        _validate_workspace_ids(workspace_ids)
        validate_state(state)
        validate_pkce_request(code_challenge, code_challenge_method)
        _validate_request_redirect(redirect_uri)
        normalized_scope = normalize_scope(scope)
        # Re-resolve before the state-changing POST. A successful GET will hit
        # the bounded cache; after the first grant the persisted snapshot wins.
        self._resolve_authorization_client(client_id)
        with self.session_factory.begin() as session:
            acquire_write_intent(session)
            client = session.get(OAuthClient, client_id)
            if client is None or client.revoked_at is not None:
                raise InvalidOAuthRequestError("client is invalid")
            if response_type != "code":
                raise InvalidOAuthRequestError("response_type must be code")
            if "authorization_code" not in client.grant_types:
                raise InvalidOAuthRequestError("grant_type is not registered")
            if response_type not in client.response_types:
                raise InvalidOAuthRequestError("response_type is not registered")
            validate_pkce_request(code_challenge, code_challenge_method)
            validate_state(state)
            if not scope_covers(client.scope, normalized_scope):
                raise InvalidOAuthRequestError("requested scope is not registered")
            if not redirect_uri_matches(redirect_uri, list(client.redirect_uris)):
                self._emit_user_audit(
                    session=session,
                    user_id=user_id,
                    action="oauth.permission.denied",
                    target_kind="oauth_authorization",
                    target_id=user_id,
                    payload={
                        "client_id": client_id,
                        "redirect_uri": redirect_uri,
                        "scope": normalized_scope,
                        "reason": "redirect_uri_not_registered",
                    },
                )
                raise InvalidOAuthRequestError("redirect_uri is not registered")
            user = session.get(User, user_id)
            if user is None or user.status != "active":
                raise InvalidOAuthRequestError("user is invalid")

            # Lock the whole (user, client) consent group before reading or
            # writing it (Codex review #8/#18): the authorize-screen upsert
            # and the Settings PATCH (update_consent_settings) must not race.
            live_group = list(
                session.scalars(
                    select(OAuthConsent)
                    .where(
                        OAuthConsent.user_id == user_id,
                        OAuthConsent.client_id == client_id,
                        OAuthConsent.removed_at.is_(None),
                    )
                    .with_for_update()
                )
            )
            if grant_consent:
                if not workspace_ids:
                    raise InvalidOAuthRequestError("workspace_ids is required")
                if len(set(workspace_ids)) != len(workspace_ids):
                    raise InvalidOAuthRequestError(
                        "duplicate workspace_id in workspace_ids"
                    )
                memberships = list_user_workspaces(session, user_id)
                member_ids = {workspace.id for workspace, _role in memberships}
                for requested_id in workspace_ids:
                    if requested_id not in member_ids:
                        raise InvalidOAuthRequestError("workspace access is required")
                selected_ids = set(workspace_ids)
                live_by_workspace = {row.workspace_id: row for row in live_group}
                merged_scope = normalized_scope
                if live_group:
                    merged_scope = merge_scopes(live_group[0].scope, normalized_scope)
                added_ids: list[UUID] = []
                removed_ids: list[UUID] = []
                now = utc_now()
                # P10.3 GAP-1 fix (this screen's Allow-decision path was the
                # one register_fan_out call site T4.5 deliberately left
                # unwired -- see the removal-semantics note below for why
                # only ADDED/scope-changed rows need one here): one fan-out
                # request per row this loop adds or scope-changes, mirroring
                # _replace_consent_workspace_grants's own fan_out_rows
                # tracking above. An unchanged, still-selected row (same
                # merged_scope, no revision bump) needs none.
                fan_out_rows: list[OAuthConsent] = []
                for workspace_id in workspace_ids:
                    existing = live_by_workspace.get(workspace_id)
                    if existing is None:
                        new_row = OAuthConsent(
                            user_id=user_id,
                            client_id=client_id,
                            workspace_id=workspace_id,
                            scope=merged_scope,
                            catalog_app_id=client.catalog_app_id,
                            catalog_app_label_snapshot=(
                                client.catalog_app_label_snapshot
                            ),
                            catalog_revision=client.catalog_revision,
                            granted_at=now,
                            desired_scopes=normalize_scope(merged_scope).split(),
                            revision=1,
                            sync_state="pending",
                        )
                        session.add(new_row)
                        added_ids.append(workspace_id)
                        fan_out_rows.append(new_row)
                    else:
                        if existing.scope != merged_scope:
                            existing.desired_scopes = normalize_scope(
                                merged_scope
                            ).split()
                            existing.revision += 1
                            existing.sync_state = "pending"
                            fan_out_rows.append(existing)
                        existing.scope = merged_scope
                        existing.granted_at = now
                # Section 2.3: this screen's own set-editing path hard-deletes
                # a deselected row (distinct from the Settings PATCH's
                # soft-remove in _replace_consent_workspace_grants -- that
                # path leaves a removed_at tombstone for a later fan-out to
                # reconcile; this one has no fan-out step to wait for). A
                # removed row here needs no register_fan_out call: the hard
                # DELETE already denies the next request immediately (the
                # control index row itself is gone, so
                # actor_with_target_workspace_scope's token_workspace_ids
                # check fails closed well before the workspace-plane grant
                # read in require_workspace_role ever runs), and the
                # resulting orphaned replica (if one existed) is left for
                # T4.6's orphan-tombstone sweep -- identical reasoning to
                # backend.services.oauth_service.revoke_workspace_oauth_
                # consent below, which also hard-deletes without a fan-out
                # call.
                for workspace_id, existing in live_by_workspace.items():
                    if workspace_id in selected_ids:
                        continue
                    session.delete(existing)
                    removed_ids.append(workspace_id)
                if fan_out_rows:
                    # One lookup for the whole (user, client) family -- every
                    # row's replica shares the same workspace-plane
                    # credential_id (section 2.6 / credential_replica_
                    # service.py's module docstring). May be None: this can
                    # be the very FIRST authorize round for this (user,
                    # client) pair, before exchange_authorization_code has
                    # ever minted a grant_id -- the shared apply function
                    # treats a None credential_id as an ordinary, recorded
                    # "not ready yet" fan-out failure (see
                    # CredentialReplicaFanOutRequest's docstring), not a
                    # crash, and the T4.6 reconciler picks it up once the
                    # token exchange that follows this authorize round mints
                    # one.
                    replica_credential_id = resolve_oauth_replica_credential_id(
                        session, user_id=user_id, client_id=client_id
                    )
                    for fan_out_row in fan_out_rows:
                        register_fan_out(
                            session,
                            CredentialReplicaFanOutRequest(
                                credential_kind="oauth",
                                credential_id=replica_credential_id,
                                workspace_id=fan_out_row.workspace_id,
                                user_id=user_id,
                                desired_scopes=tuple(fan_out_row.desired_scopes),
                                revision=fan_out_row.revision,
                                removed_at=None,
                                client_id=client_id,
                            ),
                        )
                self._emit_user_audit(
                    session=session,
                    user_id=user_id,
                    action="oauth.consent.granted",
                    target_kind="oauth_consent",
                    target_id=user_id,
                    workspace_id=None,
                    payload={
                        "client_id": client_id,
                        "scope": merged_scope,
                        "workspace_ids": [str(w) for w in workspace_ids],
                        "added": [str(w) for w in added_ids],
                        "removed": [str(w) for w in removed_ids],
                    },
                )
            else:
                if not live_group or not scope_covers(
                    live_group[0].scope, normalized_scope
                ):
                    raise InvalidOAuthRequestError("consent is required")
            code = secrets.token_urlsafe(32)
            _validate_opaque_token(code, "authorization code")
            session.add(
                OAuthCode(
                    code_hash=token_hash(code),
                    client_id=client_id,
                    user_id=user_id,
                    redirect_uri=redirect_uri,
                    code_challenge=code_challenge,
                    scope=normalized_scope,
                    expires_at=utc_now() + timedelta(seconds=CODE_TTL_SECONDS),
                )
            )
            self._emit_user_audit(
                session=session,
                user_id=user_id,
                action="oauth.code.issued",
                target_kind="oauth_code",
                target_id=user_id,
                workspace_id=None,
                payload={
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "scope": normalized_scope,
                },
            )
            session.flush()
        return code

    def exchange_authorization_code(
        self,
        *,
        code: str,
        client_id: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> OAuthTokenResult:
        _validate_client_id(client_id)
        _validate_opaque_token(code, "authorization code")
        validate_code_verifier(code_verifier)
        try:
            validate_redirect_uri(redirect_uri)
        except InvalidClientRegistrationError as exc:
            raise InvalidOAuthRequestError(str(exc)) from exc
        with self.session_factory.begin() as session:
            # SQLite needs explicit write intent before the guarded read;
            # pysqlite otherwise defers BEGIN until the first DML statement.
            # PostgreSQL keeps using the narrower row lock below.
            acquire_write_intent(session)
            stored = session.scalar(
                select(OAuthCode)
                .where(OAuthCode.code_hash == token_hash(code))
                .with_for_update()
            )
            if stored is None:
                raise InvalidOAuthGrantError("authorization code is invalid")
            if stored.consumed_at is not None:
                raise InvalidOAuthGrantError("authorization code was already used")
            if to_aware_utc(stored.expires_at) <= utc_now():
                raise InvalidOAuthGrantError("authorization code is expired")
            if stored.client_id != client_id or stored.redirect_uri != redirect_uri:
                raise InvalidOAuthGrantError("authorization code binding mismatch")
            if challenge_s256(code_verifier) != stored.code_challenge:
                raise InvalidOAuthGrantError("PKCE verification failed")
            user = session.get(User, stored.user_id)
            if user is None or user.status != "active":
                raise InvalidOAuthGrantError("authorization user is invalid")
            _validate_token_issuance(
                session,
                user_id=stored.user_id,
                client_id=client_id,
                requested_scope=stored.scope,
            )
            stored.consumed_at = utc_now()
            return self._issue_tokens(
                session,
                user,
                client_id,
                stored.scope,
                event_action="oauth.token.issued",
            )

    def refresh_access_token(
        self,
        *,
        refresh_token: str,
        client_id: str,
        scope: str | None = None,
    ) -> OAuthTokenResult:
        """Serialize rotation in the supported single-process deployment.

        PostgreSQL's row lock and SQLite write intent remain the database
        defenses. The process mutex is also required by the supported
        single-process deployment and covers test/in-memory engines.
        """
        _validate_client_id(client_id)
        _validate_opaque_token(refresh_token, "refresh token")
        requested_scope = None if scope in (None, "") else normalize_scope(scope)
        with _OAUTH_REFRESH_LOCK:
            return self._refresh_access_token_locked(
                refresh_token=refresh_token,
                client_id=client_id,
                requested_scope=requested_scope,
            )

    def _refresh_access_token_locked(
        self,
        *,
        refresh_token: str,
        client_id: str,
        requested_scope: str | None = None,
    ) -> OAuthTokenResult:
        """Rotate an OAuth refresh token (Sprint_5 Phase_3 Task 3.4 / ADR A5,
        amended by P10.3 T3 section 2.3 with a 30s reuse grace window).

        O4 reuse detection remains row-backed: a plain ``revoked_at IS NOT
        NULL`` (with no ``rotated_to_id``) is ``revoked``. ``rotated_to_id IS
        NOT NULL`` is reuse of an already-rotated row -- section 2.3's ADR A5
        amendment carves out ONE exception: reuse within 30 seconds of that
        row's OWN ``rotated_at`` mints a fresh sibling pair instead of
        revoking the family, exactly Auth0's "reuse interval" pattern (a host
        that double-fires refresh in quick succession -- MCP TypeScript SDK
        #1760 -- gets a second valid pair instead of a lockout). Reuse outside
        the window, or when the first child is itself already revoked, still
        revokes the whole family.

        Transactional model -- two transactions on every known-row refusal:
        * **TX1 (read)** acquires ``with_for_update`` on the refresh row
          and DOES NOT MODIFY any row. A known-row refusal raises the
          internal ``_RefreshRefusal`` to abort cleanly.
        * **TX2 (record)** runs in the ``except`` handler and commits
          the refusal audit plus any required family revoke before the
          public ``InvalidOAuthGrantError`` propagates.

        Between TX1 commit and TX2 begin (microseconds, single
        in-process event loop) a second concurrent reuse of the same
        refresh would see the SAME pre-revoke state and trigger a
        second, idempotent family revoke — the revoke writes are
        ``set revoked_at=now WHERE revoked_at IS NULL`` so concurrent
        revokers converge on the same end state. This microsecond
        TX1→TX2 transition is unrelated to the 30-second reuse-interval
        grace window above; it is the ordinary same-instant-concurrency
        case, still resolved by idempotent family revoke, not by minting
        a new pair.

        Multi-process deployment (out of scope per ADR A12) would break
        this property — see the Sprint_6 Redis catalog entry.
        """

        try:
            with self.session_factory.begin() as session:
                acquire_write_intent(session)
                current = session.scalar(
                    select(OAuthRefreshToken)
                    .where(
                        OAuthRefreshToken.token_lookup_hash == token_hash(refresh_token)
                    )
                    .with_for_update()
                )
                if current is None:
                    raise InvalidOAuthGrantError("refresh token is invalid")
                if current.hashed_token is None:
                    # Legacy row issued before Argon2id rollout. Force re-auth.
                    raise InvalidOAuthGrantError("refresh token is invalid")
                if not self.credential_hasher.verify_secret(
                    refresh_token,
                    current.hashed_token,
                ):
                    raise InvalidOAuthGrantError("refresh token is invalid")
                now = utc_now()
                family_key = current.grant_id or current.id
                token_age_seconds = max(
                    0,
                    int((now - to_aware_utc(current.created_at)).total_seconds()),
                )

                def refuse(
                    reason: str,
                    message: str,
                    *,
                    detected_via: str | None = None,
                ) -> NoReturn:
                    raise _RefreshRefusal(
                        user_id=current.user_id,
                        client_id=current.client_id,
                        reused_refresh_id=current.id,
                        family_key=family_key,
                        reason=reason,
                        token_age_seconds=token_age_seconds,
                        public_message=message,
                        detected_via=detected_via,
                    )

                if current.client_id != client_id:
                    refuse(
                        "client_mismatch",
                        "refresh token client mismatch",
                    )
                # Sprint_5 Phase_3 Task 3.4 (ADR A5, H26) + P10.3 T3 section
                # 2.3 (ADR A5 amendment): reuse of an already-rotated row is
                # normally compromise -- family revoke fans out across every
                # refresh token + access JWT in the same stable grant. The
                # ONE exception is the 30s reuse-interval grace window
                # (exact site + conditions per section 2.3, Codex review
                # R2-1): inserted HERE, at the rotated_to_id check, as a hard
                # return that never falls through to the revoked_at check
                # below -- a grace reissue must not also trigger a "revoked"
                # family-revoke for the same event.
                if current.rotated_to_id is not None:
                    first_child = session.scalar(
                        select(OAuthRefreshToken)
                        .where(OAuthRefreshToken.id == current.rotated_to_id)
                        .with_for_update()
                    )
                    grace_active = (
                        # (a) already implied by entering this branch.
                        current.rotated_at is not None
                        and (now - to_aware_utc(current.rotated_at))
                        <= timedelta(seconds=REFRESH_REUSE_GRACE_WINDOW_SECONDS)
                        # (b) window is anchored to THIS row's own original
                        # rotation timestamp; a grace reissue never rewrites
                        # current.rotated_at, so the window cannot be
                        # extended by repeated reuse.
                        and first_child is not None
                        # (c) an explicitly-revoked family must not be
                        # resurrected by a grace reuse.
                        and first_child.revoked_at is None
                    )
                    if not grace_active:
                        refuse(
                            "rotated_reuse",
                            "refresh token is invalid",
                            detected_via="rotated_to_id",
                        )
                    # Grace path: same prerequisite checks a normal rotation
                    # runs (expiry, user status, live consent) -- the grace
                    # window only waives the REUSE-DETECTION consequence, not
                    # these ordinary issuance gates.
                    grace_effective_scope = current.scope
                    if requested_scope is not None:
                        if not scope_covers(current.scope, requested_scope):
                            raise InvalidOAuthRequestError(
                                "requested scope exceeds the original grant"
                            )
                        grace_effective_scope = requested_scope
                    if to_aware_utc(current.expires_at) <= now:
                        refuse("expired", "refresh token is expired")
                    grace_user = session.get(User, current.user_id)
                    if grace_user is None or grace_user.status != "active":
                        refuse("user_inactive", "refresh token user is invalid")
                    grace_issuance_refusal = token_issuance_refusal(
                        session,
                        user_id=current.user_id,
                        client_id=current.client_id,
                        requested_scope=current.scope,
                    )
                    if grace_issuance_refusal is not None:
                        refuse(*grace_issuance_refusal)
                    # No mutation of current.revoked_at / current.rotated_at /
                    # current.rotated_to_id: the parent stays exactly as the
                    # first rotation left it, so rotated_to_id keeps pointing
                    # at the FIRST child and the 30s window keeps anchoring to
                    # the original rotation instant, never extended by this
                    # reissue.
                    grace_result = self._issue_tokens(
                        session,
                        grace_user,
                        client_id,
                        grace_effective_scope,
                        event_action="oauth.token.refreshed",
                        rotated_from_id=current.id,
                        grant_id=family_key,
                        prior_refresh_expires_at=current.expires_at,
                    )
                    new_child = session.scalar(
                        select(OAuthRefreshToken).where(
                            OAuthRefreshToken.token_lookup_hash
                            == token_hash(grace_result.refresh_token)
                        )
                    )
                    emit_oauth_event(
                        session=session,
                        action="oauth.refresh.grace_reissue",
                        target_id=current.id,
                        target_kind="oauth_refresh_token",
                        workspace_id=None,
                        actor_kind="oauth_client",
                        actor_id=client_id,
                        payload={
                            "parent_refresh_id": str(current.id),
                            "first_child_refresh_id": str(first_child.id),
                            "new_child_refresh_id": (
                                str(new_child.id) if new_child is not None else None
                            ),
                            "family_key": str(family_key),
                            "user_id": str(current.user_id),
                            "client_id": client_id,
                            "token_age_seconds": token_age_seconds,
                        },
                        retention_class="sensitive",
                    )
                    return grace_result
                effective_scope = current.scope
                if requested_scope is not None:
                    if not scope_covers(current.scope, requested_scope):
                        raise InvalidOAuthRequestError(
                            "requested scope exceeds the original grant"
                        )
                    effective_scope = requested_scope
                if to_aware_utc(current.expires_at) <= now:
                    refuse(
                        "expired",
                        "refresh token is expired",
                    )
                user = session.get(User, current.user_id)
                if user is None or user.status != "active":
                    refuse(
                        "user_inactive",
                        "refresh token user is invalid",
                    )
                # Fix-6 Task 1.3 (H-F5-CODEX-2): refresh path must also
                # revalidate consent existence and scope coverage. Pre-fix,
                # only authorization-code exchange checked consent ->
                # refresh tokens kept minting access tokens after consent
                # revoke. Workspace membership is no longer checked here
                # (D-01v3-g): it is a request-time concern now, evaluated by
                # actor_with_target_workspace_scope, not an issuance-time one.
                issuance_refusal = token_issuance_refusal(
                    session,
                    user_id=current.user_id,
                    client_id=current.client_id,
                    requested_scope=current.scope,
                )
                if issuance_refusal is not None:
                    refuse(*issuance_refusal)
                if current.revoked_at is not None:
                    refuse(
                        "revoked",
                        "refresh token is invalid",
                        detected_via="revoked_at",
                    )
                current.revoked_at = now
                current.rotated_at = now
                result = self._issue_tokens(
                    session,
                    user,
                    client_id,
                    effective_scope,
                    event_action="oauth.token.refreshed",
                    rotated_from_id=current.id,
                    grant_id=family_key,
                    prior_refresh_expires_at=current.expires_at,
                )
                rotated = session.scalar(
                    select(OAuthRefreshToken).where(
                        OAuthRefreshToken.token_lookup_hash
                        == token_hash(result.refresh_token)
                    )
                )
                if rotated is not None:
                    current.rotated_to_id = rotated.id
                return result
        except _RefreshRefusal as refusal:
            with self.session_factory.begin() as refusal_session:
                record_refresh_token_refusal(
                    refusal_session,
                    user_id=refusal.user_id,
                    client_id=refusal.client_id,
                    refresh_token_id=refusal.reused_refresh_id,
                    family_key=refusal.family_key,
                    reason=refusal.reason,
                    token_age_seconds=refusal.token_age_seconds,
                    detected_via=refusal.detected_via,
                )
            raise InvalidOAuthGrantError(
                refusal.public_message,
                refusal_reason=refusal.reason,
            ) from None

    # ---------------------------------------------------------------------
    # Sprint_5 Phase_3 Task 3.5 (RFC 7009 + ADR A5): /oauth/revoke service.
    # The endpoint (backend/api/oauth.py) handles auth-policy resolution
    # and calls into ``revoke_token``; the service layer handles the
    # token-type dispatch + deliberate security fan-out + audit emit so the same
    # invariants hold for any future caller (e.g. an admin tool).
    # ---------------------------------------------------------------------

    def revoke_token(
        self,
        *,
        token: str,
        token_type_hint: str = "",
        revoker_user_id: UUID | None = None,
        revoker_kind: str = "oauth_client",
        revoker_actor_id: str | None = None,
        revoker_client_id: str | None = None,
    ) -> str | None:
        """RFC 7009 §2.2 token revocation.

        Tries to match the token as a refresh token first (unless the
        hint says ``access_token``); falls back to access-JWT decode +
        jti lookup. Unknown tokens are no-ops by spec: the endpoint still
        returns 200. This deliberate revocation action fans out across every
        still-live access JWT in the (user, client) security radius. Unlike
        refresh-reuse detection, this is intentionally not grant-scoped.

        P10.3 T3 (section 2.3): the former ``revoker_workspace_ids``
        workspace-scoped restriction is gone -- the family unit is (user,
        client) now, not (user, client, workspace), so "revoke for this
        workspace only" is not an RFC 7009 operation any more. RFC 7009
        revoke always kills the token's WHOLE (user, client) family; a
        workspace-scoped access reduction is expressed purely as a consent-row
        removal, a different operation already implemented by
        ``revoke_workspace_oauth_consent`` (wired into
        ``workspace_membership_service.py``'s member-removal flow).

        Returns one of:
        * ``"refresh_token"`` — revoke matched a refresh token row
        * ``"access_token"`` — revoke matched an access JWT jti row
        * ``None`` — neither matched (caller still returns 200)
        """

        if not ascii_token_is_valid(token, OAUTH_JWT_MAX_BYTES):
            return None
        matched_kind: str | None = None
        with self.session_factory.begin() as session:
            order: list[str]
            if token_type_hint == "access_token":
                order = ["access", "refresh"]
            elif token_type_hint == "refresh_token":
                order = ["refresh", "access"]
            else:
                order = ["refresh", "access"]
            for attempt in order:
                if attempt == "refresh":
                    matched = self._revoke_as_refresh(
                        session,
                        token=token,
                        revoker_user_id=revoker_user_id,
                        revoker_kind=revoker_kind,
                        revoker_actor_id=revoker_actor_id,
                        revoker_client_id=revoker_client_id,
                    )
                    if matched:
                        matched_kind = "refresh_token"
                        break
                else:
                    matched = self._revoke_as_access(
                        session,
                        token=token,
                        revoker_user_id=revoker_user_id,
                        revoker_kind=revoker_kind,
                        revoker_actor_id=revoker_actor_id,
                        revoker_client_id=revoker_client_id,
                    )
                    if matched:
                        matched_kind = "access_token"
                        break
        # MCP-04: invalidate only once the revocation is durable. Clearing
        # (and re-clearing) inside _revoke_as_refresh/_revoke_as_access is
        # harmless with the epoch guard above, but this post-commit clear is
        # what actually closes the race for a verify that started reading
        # while the transaction above was still open.
        if matched_kind is not None:
            _jti_denylist_cache_clear()
        return matched_kind

    def _revoke_as_refresh(
        self,
        session,
        *,
        token: str,
        revoker_user_id: UUID | None,
        revoker_kind: str,
        revoker_actor_id: str | None,
        revoker_client_id: str | None,
    ) -> bool:
        if not ascii_token_is_valid(token, OAUTH_OPAQUE_TOKEN_MAX_BYTES):
            return False
        row = session.scalar(
            select(OAuthRefreshToken).where(
                OAuthRefreshToken.token_lookup_hash == token_hash(token),
            )
        )
        if row is None:
            return False
        # Auth-scope enforcement (RFC 7009 + ADR A4 case 1/2): a PAT or
        # session bearer may only revoke its own user's tokens. Native
        # client-id auth is enforced at the endpoint layer (the endpoint
        # validates client_id form == token.client_id before calling
        # this method); we treat None as "endpoint already authorised".
        if revoker_user_id is not None and row.user_id != revoker_user_id:
            # Wrong-user oracle: behave as if the token were unknown so
            # the endpoint returns 200 and does NOT disclose existence.
            return False
        if revoker_client_id is not None and row.client_id != revoker_client_id:
            return False
        if row.revoked_at is not None:
            # Already revoked: idempotent no-op. Endpoint still returns
            # 200 per RFC 7009 §2.2. No new audit row.
            return True
        now = utc_now()
        row.revoked_at = now
        # Deliberately wide security fan-out: every still-live access JWT in
        # the authorized (user, client) radius must die -- the family unit is
        # (user, client), not (user, client, workspace) (P10.3 T3).
        jti_query = select(OAuthAccessTokenJti).where(
            OAuthAccessTokenJti.user_id == row.user_id,
            OAuthAccessTokenJti.client_id == row.client_id,
            OAuthAccessTokenJti.revoked_at.is_(None),
        )
        jti_rows = list(session.scalars(jti_query).all())
        for jti_row in jti_rows:
            jti_row.revoked_at = now
        _jti_denylist_cache_clear()
        emit_oauth_event(
            session=session,
            action="oauth.revoke.success",
            target_id=row.id,
            target_kind="oauth_refresh_token",
            workspace_id=None,
            actor_kind=revoker_kind,
            actor_id=revoker_actor_id or row.client_id,
            payload={
                "token_type": "refresh_token",
                "user_id": str(row.user_id),
                "client_id": row.client_id,
                "family_access_count": len(jti_rows),
            },
            retention_class="sensitive",
        )
        return True

    def _revoke_as_access(
        self,
        session,
        *,
        token: str,
        revoker_user_id: UUID | None,
        revoker_kind: str,
        revoker_actor_id: str | None,
        revoker_client_id: str | None,
    ) -> bool:
        if not ascii_token_is_valid(token, OAUTH_JWT_MAX_BYTES):
            return False
        try:
            payload = jwt.decode(
                token,
                _public_key_pem(self.settings),
                algorithms=["RS256"],
                audience=self.resource,
                issuer=self.issuer,
                # iat is not a not-before gate here; see verify_access_token.
                options={"verify_exp": False, "verify_iat": False},
            )
        except jwt.PyJWTError:
            return False
        jti = str(payload.get("jti") or "")
        if not jti:
            return False
        jti_row = session.get(OAuthAccessTokenJti, jti)
        if jti_row is None:
            return False
        if revoker_user_id is not None and jti_row.user_id != revoker_user_id:
            return False
        if revoker_client_id is not None and jti_row.client_id != revoker_client_id:
            return False
        if jti_row.revoked_at is not None:
            return True
        jti_row.revoked_at = utc_now()
        _jti_denylist_cache_clear()
        # ADR A11 allows workspace_id=NULL for revoke audit, and the
        # revoke radius here is the single access JWT rather than the grant
        # family its minting refresh row anchors, so NULL stays the honest
        # value for this row.
        emit_oauth_event(
            session=session,
            action="oauth.token.access_revoked",
            target_id=jti_row.refresh_token_id,
            target_kind="oauth_access_token",
            workspace_id=None,
            actor_kind=revoker_kind,
            actor_id=revoker_actor_id or jti_row.client_id,
            payload={
                "token_type": "access_token",
                "jti": jti,
                "user_id": str(jti_row.user_id),
                "client_id": jti_row.client_id,
            },
            retention_class="sensitive",
        )
        return True

    def get_refresh_token_client_id(self, token: str) -> str | None:
        """Return the ``client_id`` recorded against a refresh token, or
        ``None`` if the token is unknown. Used by the /oauth/revoke
        endpoint to enforce the native ``client_id`` form-match rule
        without leaking token existence to unauthenticated callers."""

        if not ascii_token_is_valid(token, OAUTH_OPAQUE_TOKEN_MAX_BYTES):
            return None
        with self.session_factory() as session:
            row = session.scalar(
                select(OAuthRefreshToken).where(
                    OAuthRefreshToken.token_lookup_hash == token_hash(token),
                )
            )
            return row.client_id if row is not None else None

    def get_access_token_client_id(self, token: str) -> str | None:
        """Return the ``client_id`` claim of an access JWT, or ``None``
        if the token cannot be decoded as a signed JWT for this issuer."""

        if not ascii_token_is_valid(token, OAUTH_JWT_MAX_BYTES):
            return None
        try:
            payload = jwt.decode(
                token,
                _public_key_pem(self.settings),
                algorithms=["RS256"],
                audience=self.resource,
                issuer=self.issuer,
                # iat is not a not-before gate here; see verify_access_token.
                options={"verify_exp": False, "verify_iat": False},
            )
        except jwt.PyJWTError:
            return None
        client_id = payload.get("client_id")
        return str(client_id) if client_id else None

    def verify_access_token(self, token: str) -> OAuthAccessTokenActor:
        if not ascii_token_is_valid(token, OAUTH_JWT_MAX_BYTES):
            raise InvalidOAuthGrantError("access token is invalid")
        try:
            payload = jwt.decode(
                token,
                _public_key_pem(self.settings),
                algorithms=["RS256"],
                audience=self.resource,
                issuer=self.issuer,
                # iat is issuance metadata, not an activation time: every token
                # this server mints is valid at once and carries no nbf. PyJWT
                # refuses an iat ahead of the verifier's wall clock, so after a
                # backward clock step (or on a node whose clock trails the
                # issuer's) a token minted moments earlier would be rejected
                # here and by the two revoke-path decodes above. exp still ends
                # the token with no leeway, and nbf is still enforced when a
                # token carries one.
                options={"verify_iat": False},
            )
        except jwt.PyJWTError as exc:
            raise InvalidOAuthGrantError("access token is invalid") from exc
        # Sprint_5 Phase_3 Task 3.4 (ADR A5): jti denylist enforcement. A
        # missing jti is treated as revoked (defense in depth — every JWT
        # this server mints carries a jti). An access JWT whose family was
        # revoked via refresh-reuse compromise or RFC 7009 ``/oauth/revoke``
        # 401s here with the keyword pin ``revoked``.
        jti = str(payload.get("jti") or "")
        if is_access_jti_revoked(jti=jti, session_factory=self.session_factory):
            raise InvalidOAuthGrantError("access token is revoked")
        user_id = UUID(str(payload["sub"]))
        # P10.3 T3 (D-01v2/D-01v3-d): no mandatory workspace claim any more --
        # _issue_tokens stopped minting one. The credential's live
        # allowed-workspace set is read from oauth_consents
        # (removed_at IS NULL) by backend.policy.auth._authenticate_oauth_token
        # instead, per request, not embedded in the token.
        with self.session_factory() as session:
            user = session.get(User, user_id)
            if user is None or user.status != "active":
                raise InvalidOAuthGrantError("access token user is invalid")
            return OAuthAccessTokenActor(
                user=authenticated_user(user),
                client_id=str(payload.get("client_id") or ""),
                scopes=tuple(str(payload.get("scope") or "").split()),
                grant_id=_optional_uuid_claim(payload.get("grant_id")),
            )

    def _issue_tokens(
        self,
        session,
        user: User,
        client_id: str,
        scope: str,
        *,
        event_action: str,
        rotated_from_id: UUID | None = None,
        grant_id: UUID | None = None,
        prior_refresh_expires_at: datetime | None = None,
    ) -> OAuthTokenResult:
        """Mint an access/refresh pair. Control-plane only (D-01v3-g): no
        workspace session is opened here, and the access JWT carries no
        workspace claim (P10.3 T3/D-01v2). Caller has already established
        that a live consent exists for (user, client) -- the TTL/sliding
        settings below come from ANY one live row of that group (the
        (user, client)-common-value invariant, section 2.1/2.3), since which
        specific workspace a request targets is resolved later, per request,
        by ``actor_with_target_workspace_scope`` reading the SAME live
        ``oauth_consents`` set -- never embedded in the token.
        """

        now = utc_now()
        stable_grant_id = grant_id or uuid4()
        expires_at = now + timedelta(
            seconds=self.settings.oauth_access_token_ttl_seconds
        )
        consent = session.scalar(
            select(OAuthConsent)
            .where(
                OAuthConsent.user_id == user.id,
                OAuthConsent.client_id == client_id,
                OAuthConsent.removed_at.is_(None),
            )
            .order_by(OAuthConsent.workspace_id)
            .limit(1)
        )
        if consent is None:
            raise InvalidOAuthGrantError("consent is required")
        refresh_ttl_seconds = effective_consent_refresh_ttl_seconds(
            self.settings,
            consent,
        )
        sliding_enabled = effective_consent_sliding_enabled(
            self.settings,
            consent,
        )
        if prior_refresh_expires_at is not None and not sliding_enabled:
            # ``expires_at`` is the family deadline for a non-sliding grant.
            # A shorter preference may cap the child at the next rotation, but
            # no preference change can push the already-fixed deadline out.
            refresh_expires_at = min(
                to_aware_utc(prior_refresh_expires_at),
                now + timedelta(seconds=refresh_ttl_seconds),
            )
        else:
            refresh_expires_at = now + timedelta(seconds=refresh_ttl_seconds)
        jti = secrets.token_urlsafe(16)
        access_payload = {
            "iss": self.issuer,
            "sub": str(user.id),
            "aud": self.resource,
            "exp": int(expires_at.timestamp()),
            "iat": int(now.timestamp()),
            "jti": jti,
            "client_id": client_id,
            "grant_id": str(stable_grant_id),
            "scope": scope,
        }
        access_token = jwt.encode(
            access_payload,
            _private_key_pem(self.settings),
            algorithm="RS256",
            headers={"kid": JWT_KID},
        )
        require_ascii_token(access_token, OAUTH_JWT_MAX_BYTES, "access token")
        # Refresh-token issuance intentionally remains unconditional. Hosted
        # clients that do not request ``offline_access`` would otherwise
        # re-authorize every access-token lifetime; do not gate issuance on the
        # scope unless client behavior and the compatibility policy both change.
        refresh_token = f"rt_{secrets.token_urlsafe(32)}"
        _validate_opaque_token(refresh_token, "refresh token")
        refresh_model = OAuthRefreshToken(
            token_lookup_hash=token_hash(refresh_token),
            grant_id=stable_grant_id,
            hashed_token=self.credential_hasher.hash_secret(refresh_token),
            user_id=user.id,
            client_id=client_id,
            scope=scope,
            expires_at=refresh_expires_at,
            rotated_from_id=rotated_from_id,
        )
        session.add(refresh_model)
        if grant_id is None:
            # P10.3 GAP-1 same-request-timing fix: this is the FIRST moment
            # this (user, client) family's stable identity (stable_grant_id)
            # has ever existed -- exchange_authorization_code always calls
            # _issue_tokens with grant_id=None (both refresh-rotation call
            # sites above always pass an existing family_key instead, never
            # None). Every consent-commit-time register_fan_out attempt for
            # this (user, client)'s live consent rows so far --
            # issue_authorization_code's own calls, added by this same
            # change -- necessarily ran BEFORE any token was ever exchanged,
            # so resolve_oauth_replica_credential_id had nothing to find yet
            # and every one of those attempts recorded a "not ready" failure
            # (CredentialReplicaFanOutRequest's docstring), leaving every
            # live consent row's workspace_credential_grant replica
            # un-created. Re-deliver the SAME already-committed
            # desired_scopes/revision for every live row now that
            # stable_grant_id is finally resolvable, so the very next
            # request against a just-authorized workspace does not have to
            # wait for the T4.6 reconciler (not built as of this change) to
            # eventually pick up the earlier failed attempt -- this is the
            # single biggest same-request fan-out timing gap this task's own
            # brief warned about. This does not violate D-01v3-g's "no
            # workspace session in the issuance path": register_fan_out only
            # QUEUES an after-commit apply attempt (deferred past this
            # transaction's own commit, on a separate workspace-plane
            # session/transaction opened by the after_commit listener, not
            # here) -- identical to every other register_fan_out call site
            # in this file.
            live_rows = session.scalars(
                select(OAuthConsent).where(
                    OAuthConsent.user_id == user.id,
                    OAuthConsent.client_id == client_id,
                    OAuthConsent.removed_at.is_(None),
                )
            ).all()
            for live_row in live_rows:
                register_fan_out(
                    session,
                    CredentialReplicaFanOutRequest(
                        credential_kind="oauth",
                        credential_id=stable_grant_id,
                        workspace_id=live_row.workspace_id,
                        user_id=user.id,
                        desired_scopes=tuple(live_row.desired_scopes),
                        revision=live_row.revision,
                        removed_at=None,
                        client_id=client_id,
                    ),
                )
        client = session.get(OAuthClient, client_id)
        if client is not None:
            client.last_used_at = now
        session.flush()
        # Sprint_5 Phase_3 Task 3.4 (ADR A5): persist one jti row per access
        # JWT so refresh-reuse compromise can revoke the access JWT in the
        # same transaction. ``refresh_token_id`` ties the access JWT to the
        # refresh token whose chain owns it for cascade revocation.
        session.add(
            OAuthAccessTokenJti(
                jti=jti,
                refresh_token_id=refresh_model.id,
                user_id=user.id,
                client_id=client_id,
                expires_at=expires_at,
            )
        )
        payload: dict[str, Any] = {"client_id": client_id, "scope": scope}
        if rotated_from_id is not None:
            payload["rotated_from_id"] = str(rotated_from_id)
        # P10.3 T3: account-scoped (workspace_id=None), not
        # _emit_user_audit's first-membership-workspace fallback -- a token
        # now spans this credential's whole allowed-workspace set, so no
        # single workspace owns this event (same reasoning as
        # ApiTokenService.create_token's auth.api_token.created).
        # retention_class intentionally left at AuditService.emit's
        # "standard" default -- unchanged from the pre-T3 call through
        # _emit_user_audit, which never passed retention_class either.
        AuditService().emit(
            session=session,
            workspace_id=None,
            actor_kind="human",
            actor_id=str(user.id),
            action=event_action,
            target_kind="oauth_refresh_token",
            target_id=refresh_model.id,
            payload=payload,
        )
        return OAuthTokenResult(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=self.settings.oauth_access_token_ttl_seconds,
            refresh_token_expires_in=max(
                0,
                int((refresh_expires_at - now).total_seconds()),
            ),
            scope=scope,
        )

    def _emit_user_audit(
        self,
        *,
        session,
        user_id: UUID,
        action: str,
        target_kind: str,
        target_id: UUID,
        payload: dict[str, Any],
        workspace_id: UUID | None = None,
    ) -> None:
        if workspace_id is None:
            workspaces = list_user_workspaces(session, user_id)
            if not workspaces:
                return
            workspace_id = workspaces[0][0].id
        AuditService().emit(
            session=session,
            workspace_id=workspace_id,
            actor_kind="human",
            actor_id=str(user_id),
            action=action,
            target_kind=target_kind,
            target_id=target_id,
            payload=payload,
        )


def effective_consent_refresh_ttl_seconds(
    settings: Settings,
    consent: OAuthConsent,
) -> int:
    """Return the consent cap bounded by the current instance maximum."""

    requested = consent.refresh_ttl_seconds
    instance_ttl = settings.oauth_refresh_token_ttl_seconds
    return min(instance_ttl, requested if requested is not None else instance_ttl)


def _validated_cimd_registration(
    metadata: CIMDClientMetadata,
) -> CIMDClientMetadata:
    """Apply the exact DCR/runtime redirect and scope validators to CIMD."""

    redirects = tuple(validate_redirect_uri(uri) for uri in metadata.redirect_uris)
    scope = normalize_scope(metadata.scope)
    return CIMDClientMetadata(
        client_id=metadata.client_id,
        client_name=metadata.client_name,
        redirect_uris=redirects,
        scope=scope,
        grant_types=metadata.grant_types,
        response_types=metadata.response_types,
        application_type=metadata.application_type,
    )


def _cimd_has_established_grant(session: Session, client_id: str) -> bool:
    now = utc_now()
    consent = session.scalar(
        select(OAuthConsent.user_id).where(OAuthConsent.client_id == client_id).limit(1)
    )
    if consent is not None:
        return True
    live_code = session.scalar(
        select(OAuthCode.code_hash)
        .where(
            OAuthCode.client_id == client_id,
            OAuthCode.consumed_at.is_(None),
            OAuthCode.expires_at > now,
        )
        .limit(1)
    )
    if live_code is not None:
        return True
    live_refresh = session.scalar(
        select(OAuthRefreshToken.id)
        .where(
            OAuthRefreshToken.client_id == client_id,
            OAuthRefreshToken.revoked_at.is_(None),
            OAuthRefreshToken.expires_at > now,
        )
        .limit(1)
    )
    return live_refresh is not None


def effective_consent_sliding_enabled(
    settings: Settings,
    consent: OAuthConsent,
) -> bool:
    """An instance-wide disable wins over a consent's preference."""

    return bool(
        settings.oauth_refresh_token_sliding_enabled and consent.sliding_enabled
    )


def normalize_scope(scope: str) -> str:
    try:
        validate_scope_input(scope)
    except ValueError as exc:
        raise InvalidOAuthRequestError(str(exc)) from exc
    requested = [item for item in scope.split() if item]
    if not requested:
        requested = ["mcp:read"]
    unknown = [item for item in requested if item not in ALLOWED_OAUTH_SCOPES]
    if unknown:
        raise InvalidOAuthRequestError(f"unsupported scope: {unknown[0]}")
    return " ".join(dict.fromkeys(requested))


def scope_covers(granted_scope: str, requested_scope: str) -> bool:
    granted = set(normalize_scope(granted_scope).split())
    requested = set(normalize_scope(requested_scope).split())
    return requested.issubset(granted)


def _optional_uuid_claim(value: Any) -> UUID | None:
    if value in (None, ""):
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise InvalidOAuthGrantError("access token grant claim is invalid") from exc


def _validate_token_issuance(
    session,
    *,
    user_id: UUID,
    client_id: str,
    requested_scope: str,
) -> None:
    """Shared lifecycle invariant for authorization-code exchange + refresh.

    Fix-6 Task 1.3 (H-F5-CODEX-2) + P10.3 T3 (D-01v3-g): both token-issuing
    paths must enforce:
      1. a live OAuthConsent row exists for (user, client) -- for ANY of its
         allowed workspaces, not one in particular; issuance stays
         control-plane only and does not know a target workspace yet
      2. the granted consent scope covers the requested scope

    Workspace membership is no longer checked HERE (it was, pre-T3, as a
    third invariant) -- D-01v3-g moved that check to REQUEST time
    (``actor_with_target_workspace_scope``), because issuance happens before
    any target workspace is even chosen. Pre-Fix-6 the refresh path checked
    only token row + client + expiry + user status -> consent revoke
    silently kept minting fresh access tokens; this restores that guard for
    consent without reintroducing a workspace it does not yet have.
    """

    refusal = token_issuance_refusal(
        session,
        user_id=user_id,
        client_id=client_id,
        requested_scope=requested_scope,
    )
    if refusal is not None:
        raise InvalidOAuthGrantError(refusal[1])


def token_issuance_refusal(
    session,
    *,
    user_id: UUID,
    client_id: str,
    requested_scope: str,
) -> tuple[str, str] | None:
    """Return ``(reason, message)`` when this grant may not mint a token.

    The two token-issuing paths need the same answer in different shapes: the
    exchange path raises, while refresh has to attach the reason to its audit
    and metrics. They share this one implementation so the invariant cannot
    drift between them -- which it silently did once already, and is what the
    path-symmetry test exists to prevent.

    P10.3 T3 (D-01v3-g): "any live consent for (user, client)" replaces the
    old 3-tuple (user, client, workspace) existence check -- issuance is
    control-plane only and has no target workspace to check membership
    against yet (that moved to request time, see _validate_token_issuance).
    """

    consent = session.scalar(
        select(OAuthConsent)
        .where(
            OAuthConsent.user_id == user_id,
            OAuthConsent.client_id == client_id,
            OAuthConsent.removed_at.is_(None),
        )
        .limit(1)
    )
    if consent is None or not scope_covers(consent.scope, requested_scope):
        return ("consent_missing", "authorization consent was revoked")
    return None


def merge_scopes(existing_scope: str, requested_scope: str) -> str:
    return " ".join(
        dict.fromkeys(
            normalize_scope(existing_scope).split()
            + normalize_scope(requested_scope).split()
        )
    )


# ---------------------------------------------------------------------------
# Sprint_5 Phase_3 Task 3.4 (ADR A5): refresh-reuse family revoke + access
# JWT jti denylist. Module-level state so the cache lives across
# OAuthService instances — the bearer-token decode path constructs a new
# service per request and we don't want to round-trip the DB for every
# request. Cache key bakes in a 1-minute time bucket so entries naturally
# expire; family revoke explicitly invalidates the cache for zero-latency
# rejection of revoked JWTs (Sprint_6 will swap the dict for Redis).
# ---------------------------------------------------------------------------


_JTI_DENYLIST_CACHE: dict[tuple, bool] = {}
_JTI_DENYLIST_CACHE_MAX = 4096
# Monotonic invalidation generation (MCP-04). ``itertools.count`` advances
# atomically in CPython, so no extra lock is needed on the bearer-decode hot
# path. A bare ``_JTI_DENYLIST_CACHE.clear()`` is not enough to close the
# commit race: a verify that read pre-commit state (``revoked=False``) before
# the clear can still *store* that stale answer into the cache after the
# clear ran, and it would sit there for the rest of the 60-second bucket.
# Capturing the epoch before the DB read and refusing to memoize a read whose
# epoch has since moved on closes that window.
_JTI_DENYLIST_EPOCH_SEQ = itertools.count(1)
_JTI_DENYLIST_EPOCH = next(_JTI_DENYLIST_EPOCH_SEQ)


def _jti_denylist_cache_clear() -> None:
    """Drop every denylist cache entry and open a new invalidation epoch.

    Callers MUST invoke this only after the revoking transaction has
    committed. An earlier (pre-commit) clear does not close the race this
    guards against — see the epoch check in ``is_access_jti_revoked``.
    """

    global _JTI_DENYLIST_EPOCH
    _JTI_DENYLIST_EPOCH = next(_JTI_DENYLIST_EPOCH_SEQ)
    _JTI_DENYLIST_CACHE.clear()


def is_access_jti_revoked(
    *,
    jti: str,
    session_factory,
    time_bucket: int | None = None,
) -> bool:
    """Cached membership test for the access-token denylist.

    Returns True when the supplied ``jti`` is either absent from
    ``oauth_access_token_jti`` (defense-in-depth: a JWT whose row was never
    inserted is treated as compromised) or present with a non-NULL
    ``revoked_at``. Misses are stored in the cache for the 1-minute window
    so the hot bearer-decode path stays DB-light.
    """

    if not jti:
        return True
    if time_bucket is None:
        time_bucket = int(time.monotonic() // 60)
    cache_key = (jti, time_bucket, id(session_factory))
    cached = _JTI_DENYLIST_CACHE.get(cache_key)
    if cached is not None:
        return cached
    epoch = _JTI_DENYLIST_EPOCH
    with session_factory() as session:
        row = session.get(OAuthAccessTokenJti, jti)
        revoked = row is None or row.revoked_at is not None
    # Crude eviction: prevent unbounded growth in long-running processes.
    # This is eviction, not invalidation, so it must not open a new epoch.
    if len(_JTI_DENYLIST_CACHE) >= _JTI_DENYLIST_CACHE_MAX:
        _JTI_DENYLIST_CACHE.clear()
    if revoked or epoch == _JTI_DENYLIST_EPOCH:
        # A revocation committed while this read was in flight (epoch moved):
        # do not memoize the pre-commit "not revoked" answer for the rest of
        # the 60-second bucket.
        _JTI_DENYLIST_CACHE[cache_key] = revoked
    return revoked


def _grant_family_predicate(family_key: UUID):
    """Match every refresh row in one authorization grant.

    A row's family key is its ``grant_id``, falling back to its own ``id`` for
    rows written before ``grant_id`` existed -- rotation stamped the child with
    the legacy parent's id, so the two still land in one family while unrelated
    legacy rows stay apart. Written as an OR rather than
    ``COALESCE(grant_id, id) = key`` so both branches can use an index: the
    coalesce form is unindexable and made every reuse event scan the table,
    which is exactly when the database is least worth stalling.
    """

    return or_(
        OAuthRefreshToken.grant_id == family_key,
        and_(
            OAuthRefreshToken.grant_id.is_(None),
            OAuthRefreshToken.id == family_key,
        ),
    )


def revoke_refresh_token_grant_family(
    session,
    *,
    user_id: UUID,
    client_id: str,
    detected_via: str,
    reused_refresh_id: UUID,
    family_key: UUID,
    reason: str = "rotated_reuse",
    token_age_seconds: int = 0,
) -> tuple[int, int]:
    """Atomically mark every still-live refresh token + access JWT in the
    replayed token's authorization-grant family as revoked. A refresh row's
    family key is ``COALESCE(grant_id, id)`` so pre-grant-id legacy parents
    remain grouped with their rotated children without merging unrelated
    legacy rows. Emits one
    ``oauth.refresh.reuse`` audit row carrying the family sizes and the
    detection signal (``revoked_at`` vs ``rotated_to_id``).

    Returns ``(family_refresh_count, family_access_count)`` for caller
    diagnostics and audit payload pinning. P10.3 T3: no ``workspace_id``
    parameter -- the family is a (user, client) unit now, not scoped to any
    one workspace, so the audit row is account-scoped (``workspace_id=None``).
    """

    family_count, access_count = _revoke_refresh_token_grant_family_rows(
        session,
        family_key=family_key,
        revoked_at=utc_now(),
    )
    emit_oauth_event(
        session=session,
        action="oauth.refresh.reuse",
        target_id=reused_refresh_id,
        target_kind="oauth_refresh_token",
        workspace_id=None,
        actor_kind="oauth_client",
        actor_id=client_id,
        payload={
            "reused_refresh_id": str(reused_refresh_id),
            "user_id": str(user_id),
            "client_id": client_id,
            "family_key": str(family_key),
            "family_refresh_count": family_count,
            "family_access_count": access_count,
            "detected_via": detected_via,
            "reason": reason,
            "token_age_seconds": token_age_seconds,
        },
        retention_class="sensitive",
    )
    return family_count, access_count


def record_refresh_token_refusal(
    session,
    *,
    user_id: UUID,
    client_id: str,
    refresh_token_id: UUID,
    family_key: UUID,
    reason: str,
    token_age_seconds: int,
    detected_via: str | None = None,
) -> tuple[int, int]:
    """Durably record one known-row refresh refusal without token material."""

    if reason not in REFRESH_REFUSAL_REASONS:
        raise ValueError(f"unknown refresh refusal reason: {reason}")
    family_refresh_count = 0
    family_access_count = 0
    if reason == "rotated_reuse":
        family_refresh_count, family_access_count = revoke_refresh_token_grant_family(
            session,
            user_id=user_id,
            client_id=client_id,
            detected_via=detected_via or "rotated_to_id",
            reused_refresh_id=refresh_token_id,
            family_key=family_key,
            reason=reason,
            token_age_seconds=token_age_seconds,
        )
    elif reason in _REFRESH_REFUSALS_REVOKING_FAMILY:
        family_refresh_count, family_access_count = (
            _revoke_refresh_token_grant_family_rows(
                session,
                family_key=family_key,
                revoked_at=utc_now(),
            )
        )

    payload: dict[str, Any] = {
        "reason": reason,
        "family_key": str(family_key),
        "token_age_seconds": token_age_seconds,
        "family_revoked": reason in _REFRESH_REFUSALS_REVOKING_FAMILY,
        "family_refresh_count": family_refresh_count,
        "family_access_count": family_access_count,
    }
    if detected_via is not None:
        payload["detected_via"] = detected_via
    emit_oauth_event(
        session=session,
        action="oauth.refresh.refused",
        target_id=refresh_token_id,
        target_kind="oauth_refresh_token",
        workspace_id=None,
        actor_kind="oauth_client",
        actor_id=client_id,
        payload=payload,
        retention_class="sensitive",
    )
    return family_refresh_count, family_access_count


def _revoke_refresh_token_grant_family_rows(
    session,
    *,
    family_key: UUID,
    revoked_at: datetime,
) -> tuple[int, int]:
    family = list(
        session.scalars(
            select(OAuthRefreshToken).where(
                _grant_family_predicate(family_key),
                OAuthRefreshToken.revoked_at.is_(None),
            )
        ).all()
    )
    for token in family:
        token.revoked_at = revoked_at
    jti_rows = list(
        session.scalars(
            select(OAuthAccessTokenJti)
            .join(
                OAuthRefreshToken,
                OAuthRefreshToken.id == OAuthAccessTokenJti.refresh_token_id,
            )
            .where(
                _grant_family_predicate(family_key),
                OAuthAccessTokenJti.revoked_at.is_(None),
            )
        ).all()
    )
    for jti_row in jti_rows:
        jti_row.revoked_at = revoked_at
    _jti_denylist_cache_clear()
    # MCP-04: this function receives a caller-owned session (consent revoke,
    # reuse detection, account-recovery reset, settings_service disable, ...)
    # and does not control when/whether it commits. The clear above is
    # harmless-but-early; register a post-commit clear too so the epoch that
    # actually closes the race (see is_access_jti_revoked) opens once this
    # revocation is durable, regardless of which caller commits the session.
    # ``once=True`` keeps repeated calls against the same session (e.g. one
    # cleanup pass revoking several grant families) from stacking listeners.
    event.listen(
        session,
        "after_commit",
        lambda _session: _jti_denylist_cache_clear(),
        once=True,
    )
    return len(family), len(jti_rows)


def revoke_all_live_tokens_for_user_client(
    session,
    *,
    user_id: UUID,
    client_id: str,
    revoked_at: datetime | None = None,
) -> tuple[int, int]:
    """Deliberately revoke every live refresh and access token for a client.

    This is the intentionally wide ``(user_id, client_id)`` security action
    used by consent revocation. Refresh-reuse detection must use
    ``revoke_refresh_token_grant_family`` instead.
    """

    return revoke_live_token_grant_families(
        session,
        user_id=user_id,
        client_id=client_id,
        revoked_at=revoked_at,
    )


def revoke_live_token_grant_families(
    session,
    *,
    user_id: UUID,
    client_id: str | None = None,
    revoked_at: datetime | None = None,
) -> tuple[int, int]:
    """Revoke every affected grant via the persisted O4 family key.

    D-01v2/D-01v3 (P10.3 T1/T2): OAuthRefreshToken lost its workspace_id --
    the family unit is now (user, client), not scoped to any one workspace,
    so this can no longer filter by workspace. A caller that wants to revoke
    a user's OAuth access to exactly ONE workspace without touching their
    access to others must delete that (user, client, workspace)'s
    oauth_consents row instead (revoke_workspace_oauth_consent below) -- the
    live per-request allowed-set check is what actually enforces that, not
    the refresh-token family.
    """

    statement = select(OAuthRefreshToken).where(OAuthRefreshToken.user_id == user_id)
    if client_id is not None:
        statement = statement.where(OAuthRefreshToken.client_id == client_id)
    family_keys = {
        token.grant_id or token.id for token in session.scalars(statement).all()
    }
    now = revoked_at or utc_now()
    refresh_count = 0
    access_count = 0
    for family_key in family_keys:
        revoked_refresh, revoked_access = _revoke_refresh_token_grant_family_rows(
            session,
            family_key=family_key,
            revoked_at=now,
        )
        refresh_count += revoked_refresh
        access_count += revoked_access
    return refresh_count, access_count


def revoke_workspace_oauth_consent(
    session,
    *,
    user_id: UUID,
    workspace_id: UUID,
) -> int:
    """D-01v2/D-01v3: remove a user's OAuth access to exactly ONE workspace.

    Deletes every oauth_consents row for (user_id, workspace_id) across all
    clients -- the live per-request allowed-set check (section 2.2) reads
    this table directly, so the workspace becomes unreachable via OAuth on
    the very next request with no token/family revocation needed. Other
    workspaces the same (user, client) pair still has consent for are
    untouched, unlike revoke_live_token_grant_families (whole-family, no
    workspace scoping since OAuthRefreshToken.workspace_id no longer
    exists). Used by workspace_membership_service on member removal.

    T4.5 judgment call, deliberately NOT given a register_fan_out call:
    denial is already immediate via the control-index row's absence (the
    same reasoning as issue_authorization_code's hard-delete branch above),
    so there is nothing a fan-out call would make more correct here -- only
    faster cleanup of the now-orphaned workspace_credential_grant replica
    (if one existed), which is exactly what T4.6's reconciler orphan sweep
    (section 2.6, not built as of T4.5) exists to do instead. Consistent
    with every other hard-delete removal path in this module.
    """

    rows = session.scalars(
        select(OAuthConsent).where(
            OAuthConsent.user_id == user_id,
            OAuthConsent.workspace_id == workspace_id,
        )
    ).all()
    for row in rows:
        session.delete(row)
    return len(rows)


def revoke_all_user_oauth_credentials(
    session,
    *,
    user_id: UUID,
    revoked_at: datetime | None = None,
) -> dict[str, int]:
    """Revoke every live OAuth credential for an account-recovery boundary.

    Consent grants remain in place. The reset invalidates credentials that can
    currently authenticate, while a future authorization still requires the
    recovered account to sign in again.
    """

    now = revoked_at or utc_now()
    refresh_tokens = list(
        session.scalars(
            select(OAuthRefreshToken).where(
                OAuthRefreshToken.user_id == user_id,
                OAuthRefreshToken.revoked_at.is_(None),
            )
        ).all()
    )
    for token in refresh_tokens:
        token.revoked_at = now
    access_tokens = list(
        session.scalars(
            select(OAuthAccessTokenJti).where(
                OAuthAccessTokenJti.user_id == user_id,
                OAuthAccessTokenJti.revoked_at.is_(None),
            )
        ).all()
    )
    for token in access_tokens:
        token.revoked_at = now
    authorization_codes = list(
        session.scalars(
            select(OAuthCode).where(
                OAuthCode.user_id == user_id,
                OAuthCode.consumed_at.is_(None),
                OAuthCode.expires_at > now,
            )
        ).all()
    )
    for code in authorization_codes:
        code.consumed_at = now
    _jti_denylist_cache_clear()
    return {
        "oauth_refresh_tokens": len(refresh_tokens),
        "oauth_access_tokens": len(access_tokens),
        "oauth_authorization_codes": len(authorization_codes),
    }


# ADR A2 (Sprint_5 Phase_3 Task 3.1): canonical loopback host whitelist shared
# by validate_redirect_uri (DCR-time) and redirect_uri_matches (runtime). Plain
# HTTP redirect URIs are permitted only for these three hosts per RFC 8252 §7.3.
_LOOPBACK_CANONICAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def validate_redirect_uri(uri: str) -> str:
    if not utf8_size_is_valid(uri, OAUTH_IDENTIFIER_MAX_BYTES) or any(
        character.isspace() or ord(character) < 32 or 127 <= ord(character) <= 159
        for character in uri
    ):
        raise InvalidClientRegistrationError(
            "redirect_uri must contain 1-2048 UTF-8 bytes "
            "without whitespace or controls"
        )
    try:
        parsed = urlparse(uri)
    except ValueError as exc:
        # urlsplit rejects unbalanced or non-IP brackets and a netloc that
        # NFKC-normalizes to a delimiter; callers expect an OAuth error.
        raise InvalidClientRegistrationError("redirect_uri is malformed") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise InvalidClientRegistrationError("redirect_uri must be absolute http(s)")
    if parsed.fragment:
        raise InvalidClientRegistrationError("redirect_uri fragment not allowed")
    # Userinfo turns the netloc into attacker-chosen leading text:
    # `https://dtxwiki.example.com@evil.tld/cb` reads as dtxwiki.example.com to
    # a person while the code goes to evil.tld. The consent screen names the
    # destination precisely because it is the one claim the flow enforces, so
    # the flow has to keep it unambiguous. No OAuth client needs credentials in
    # a redirect_uri.
    if parsed.username or parsed.password:
        raise InvalidClientRegistrationError("redirect_uri userinfo not allowed")
    # Python keeps a backslash inside the host; browsers follow WHATWG and
    # rewrite it to `/`. `https://shown.example\real-path` therefore displays
    # one host on the consent screen and navigates to another, which is the
    # same display-versus-destination split the userinfo check just closed. No
    # legitimate redirect_uri contains one.
    if "\\" in uri:
        raise InvalidClientRegistrationError("redirect_uri backslash not allowed")
    # Port range guard (ADR A2 (2)): reject port 0 and out-of-range ports.
    # Sprint_5 Phase_3 Task 3.7 (SEC-FR3MID-1): an http loopback URI MUST
    # carry an explicit port. RFC 8252 §7.3 requires the native client to
    # bind a runtime port and put it into ``redirect_uris`` at DCR time;
    # accepting ``port=None`` (e.g. ``http://127.0.0.1/cb``) would let a
    # later ``redirect_uri_matches`` query bypass the loopback-port
    # exception and match ``http://127.0.0.1:1234/cb`` only by virtue of
    # the missing-port wildcard, which is not the RFC's intent. https
    # registrations may omit the port (default 443 is implied).
    try:
        port = parsed.port
    except ValueError as exc:
        raise InvalidClientRegistrationError(
            "redirect_uri loopback port must be in 1-65535"
        ) from exc
    host = parsed.hostname
    if host is not None and host.endswith("."):
        raise InvalidClientRegistrationError(
            "redirect_uri host trailing dot not allowed"
        )
    if port == 0:
        raise InvalidClientRegistrationError("redirect_uri loopback port 0 not allowed")
    if parsed.scheme == "http" and port is None:
        raise InvalidClientRegistrationError(
            "redirect_uri loopback port is required (RFC 8252 §7.3)"
        )
    if parsed.scheme == "http":
        netloc = parsed.netloc
        # ADR A2 (5): reject IPv6 zone identifier (raw `%` or `%25` in netloc).
        if "%" in netloc:
            raise InvalidClientRegistrationError(
                "redirect_uri loopback IPv6 zone identifier not allowed"
            )
        # ADR A2 (4): reject non-canonical IPv4 forms (e.g. `127.000.000.001`,
        # `127.1`) before falling through to the host whitelist so the specific
        # failure mode keyword wins.
        if host is not None and host and host[0].isdigit() and host != "127.0.0.1":
            raise InvalidClientRegistrationError(
                "redirect_uri loopback IPv4 must be canonical 127.0.0.1"
            )
        # ADR A2 (1): host whitelist for plain HTTP.
        if host not in _LOOPBACK_CANONICAL_HOSTS:
            raise InvalidClientRegistrationError(
                "plain http redirect_uri is only allowed for loopback "
                "(localhost, 127.0.0.1, ::1)"
            )
    return uri


def _is_loopback_host(host: str | None) -> bool:
    return host in _LOOPBACK_CANONICAL_HOSTS


def _has_valid_explicit_port(parsed) -> bool:
    try:
        port = parsed.port
    except ValueError:
        return False
    return port is not None and port != 0


def redirect_uri_matches(requested: str, registered_uris: list[str]) -> bool:
    """RFC 8252 §7.3 — loopback redirect URIs may use any port at runtime.

    Returns True if the requested URI exactly matches one of the registered
    URIs, OR if both URIs share scheme + loopback host + path + params + query
    while differing only in port (loopback exception). All other redirects must
    match exactly. Runtime requests and legacy registered values must also pass
    the same safety validation used by dynamic client registration.
    """

    try:
        validate_redirect_uri(requested)
    except InvalidClientRegistrationError:
        return False
    valid_registered_uris = []
    for candidate in registered_uris:
        try:
            validate_redirect_uri(candidate)
        except InvalidClientRegistrationError:
            continue
        valid_registered_uris.append(candidate)
    registered_uris = valid_registered_uris

    parsed_req = urlparse(requested)
    if parsed_req.fragment:
        return False
    if parsed_req.scheme == "http" and _is_loopback_host(parsed_req.hostname):
        if not _has_valid_explicit_port(parsed_req):
            return False
    if requested in registered_uris:
        return True
    if parsed_req.scheme != "http" or not _is_loopback_host(parsed_req.hostname):
        return False
    for candidate in registered_uris:
        parsed_reg = urlparse(candidate)
        if parsed_reg.scheme != "http":
            continue
        if parsed_reg.fragment:
            continue
        if not _is_loopback_host(parsed_reg.hostname):
            continue
        if not _has_valid_explicit_port(parsed_reg):
            continue
        if parsed_reg.path != parsed_req.path:
            continue
        if parsed_reg.params != parsed_req.params:
            continue
        if parsed_reg.query != parsed_req.query:
            continue
        if parsed_reg.hostname != parsed_req.hostname:
            continue
        # Same scheme + loopback host + path + params + query; arbitrary port allowed.
        return True
    return False


def validate_pkce_request(code_challenge: str, code_challenge_method: str) -> None:
    if code_challenge_method != PKCE_METHOD_S256:
        raise InvalidOAuthRequestError("code_challenge_method must be S256")
    validate_code_challenge(code_challenge)


def validate_code_challenge(code_challenge: str) -> None:
    if (
        not isinstance(code_challenge, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{43}", code_challenge) is None
    ):
        raise InvalidOAuthRequestError("code_challenge is invalid")


def validate_state(state: str) -> None:
    if isinstance(state, str) and len(state) < MIN_STATE_LENGTH:
        raise InvalidOAuthRequestError(
            f"state is required and must be at least {MIN_STATE_LENGTH} chars"
        )
    if (
        not isinstance(state, str)
        or len(state) > 512
        or any(not " " <= character <= "~" for character in state)
    ):
        raise InvalidOAuthRequestError(
            "state is required and must contain "
            f"{MIN_STATE_LENGTH}-512 printable ASCII chars"
        )


def challenge_s256(code_verifier: str) -> str:
    validate_code_verifier(code_verifier)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def validate_code_verifier(code_verifier: str) -> None:
    if (
        not isinstance(code_verifier, str)
        or re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", code_verifier) is None
    ):
        raise InvalidOAuthRequestError(
            "code_verifier must contain 43-128 ASCII unreserved chars"
        )


def _validate_client_id(client_id: str) -> None:
    try:
        validate_identifier(client_id)
    except ValueError as exc:
        raise InvalidOAuthRequestError(str(exc)) from exc


def _validate_workspace_ids(workspace_ids: object) -> None:
    if workspace_ids is not None and (
        not isinstance(workspace_ids, list)
        or len(workspace_ids) > WORKSPACE_GRANTS_MAX_COUNT
    ):
        raise InvalidOAuthRequestError("workspace_ids must contain at most 256 entries")


def _validate_client_snapshot(client: OAuthClient) -> OAuthClient:
    try:
        validate_client_name(client.client_name)
        redirects = validate_redirect_list(client.redirect_uris)
        for uri in redirects:
            validate_redirect_uri(uri)
    except (ValueError, InvalidClientRegistrationError) as exc:
        raise InvalidOAuthRequestError(
            "client metadata exceeds supported input limits"
        ) from exc
    return client


def _validate_opaque_token(token: str, label: str) -> None:
    if not ascii_token_is_valid(token, OAUTH_OPAQUE_TOKEN_MAX_BYTES):
        raise InvalidOAuthGrantError(f"{label} is invalid")


def _validate_request_redirect(redirect_uri: str) -> None:
    try:
        validate_redirect_uri(redirect_uri)
    except InvalidClientRegistrationError as exc:
        raise InvalidOAuthRequestError(str(exc)) from exc


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _consume_dcr_slot(client_ip: str) -> None:
    now = time.monotonic()
    bucket = [stamp for stamp in _DCR_BUCKET.get(client_ip, []) if now - stamp < 60]
    if len(bucket) >= 20:
        raise InvalidClientRegistrationError("dynamic client registration rate limited")
    bucket.append(now)
    _DCR_BUCKET[client_ip] = bucket


def consume_token_slot(client_ip: str, client_id: str) -> None:
    """Rate-limit /oauth/token grants (Sprint-4 R-F6-1 + Sprint_5 Phase_3
    Task 3.6 / ADR A12).

    Two-axis bucket: per-IP and per-client_id. Brute-forcing a stolen
    refresh token from one IP across many client_ids is limited by the
    per-IP bucket; abuse of one client_id across many IPs is limited by
    the per-client bucket. Both checks fire on every grant request
    (authorization_code AND refresh_token paths).

    **Algorithm**: sliding-window counter over a 60-second tail. Each
    call (a) prunes timestamps older than 60s from the relevant bucket,
    (b) raises ``OAuthRateLimitError`` if the bucket size already meets
    the threshold, else (c) appends ``time.monotonic()`` and stores
    back. The implementation deliberately uses ``time.monotonic`` so
    NTP step or wall-clock skew during the 60s window cannot leak
    extra slots; tests monkeypatch ``time.monotonic`` directly to
    fast-forward without ``time.sleep``.

    **Single-process invariant** (ADR A12, module docstring above):
    each backend process owns its own ``_TOKEN_*_BUCKET`` dict; running
    more than one worker silently doubles the effective rate. Phase_4's
    systemd unit pins ``Type=simple`` + single ``ExecStart`` to enforce
    this; ``test_single_process_invariant_documented`` guards the
    contract once Phase_4 lands.

    Raises:
        OAuthRateLimitError: when either bucket is full. The endpoint
            layer maps this to HTTP 429.
    """
    now = time.monotonic()
    ip_bucket = [
        stamp for stamp in _TOKEN_IP_BUCKET.get(client_ip, []) if now - stamp < 60
    ]
    if len(ip_bucket) >= TOKEN_RATE_LIMIT_PER_IP:
        raise OAuthRateLimitError("token endpoint rate limit exceeded for client IP")
    ip_bucket.append(now)
    _TOKEN_IP_BUCKET[client_ip] = ip_bucket

    if client_id:
        client_bucket = [
            stamp
            for stamp in _TOKEN_CLIENT_BUCKET.get(client_id, [])
            if now - stamp < 60
        ]
        if len(client_bucket) >= TOKEN_RATE_LIMIT_PER_CLIENT:
            raise OAuthRateLimitError(
                "token endpoint rate limit exceeded for client_id"
            )
        client_bucket.append(now)
        _TOKEN_CLIENT_BUCKET[client_id] = client_bucket


def _private_key_pem(settings: Settings) -> str:
    if settings.oauth_jwt_private_key_path:
        return Path(settings.oauth_jwt_private_key_path).read_text(encoding="utf-8")
    if settings.oauth_jwt_private_key_pem:
        return settings.oauth_jwt_private_key_pem
    global _PRIVATE_KEY_PEM
    if _PRIVATE_KEY_PEM is None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        _PRIVATE_KEY_PEM = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
    return _PRIVATE_KEY_PEM


def _load_private_key(settings: Settings):
    return serialization.load_pem_private_key(
        _private_key_pem(settings).encode(),
        password=None,
    )


def _public_key_pem(settings: Settings) -> str:
    public_key = _load_private_key(settings).public_key()
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def _uint_to_base64url(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def to_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)

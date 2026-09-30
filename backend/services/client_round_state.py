from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

CLIENT_ROUND_STATE_VERSION = 1
MAINTENANCE_STATE_KIND = "dtxwiki-maintenance-state-v1"
RERANK_STATE_KIND = "dtxwiki-rerank-state-v1"
QUERY_REWRITE_STATE_KIND = "dtxwiki-query-rewrite-state-v1"
CITE_VERIFY_STATE_KIND = "dtxwiki-cite-verify-state-v1"


@dataclass(frozen=True)
class ClientRoundStateClaims:
    workspace_id: UUID
    task_ids: tuple[UUID, ...]
    issued_at: int
    expires_at: int
    jti: str
    request_fingerprint: str | None


class ClientRoundStateError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ClientRoundStateCodec:
    secret: str
    kind: str
    ttl_seconds: int = 900
    now: Callable[[], datetime] = lambda: datetime.now(UTC)

    def __post_init__(self) -> None:
        if not self.secret:
            raise ValueError("client round state secret must not be empty")
        if not self.kind:
            raise ValueError("client round state kind must not be empty")
        if self.ttl_seconds <= 0:
            raise ValueError("client round state ttl must be positive")

    def issue(
        self,
        *,
        workspace_id: UUID,
        task_ids: Sequence[UUID],
        request_fingerprint: str | None = None,
    ) -> str:
        issued_at = self.now()
        expires_at = issued_at + timedelta(seconds=self.ttl_seconds)
        payload = {
            "v": CLIENT_ROUND_STATE_VERSION,
            "kind": self.kind,
            "ws": str(workspace_id),
            "tids": sorted(str(task_id) for task_id in task_ids),
            "iat": int(issued_at.timestamp()),
            "exp": int(expires_at.timestamp()),
            # Maintenance replay is rejected by the leased-task transition and
            # rerank replay is read-only. Keep a JTI in the wire contract for
            # auditability and a future shared replay cache without adding one
            # to the Tier-B hot path now.
            "jti": secrets.token_urlsafe(8),
            "req": request_fingerprint,
        }
        encoded_payload = _base64url_encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        )
        signature = _base64url_encode(self._sign(encoded_payload.encode()))
        return f"{encoded_payload}.{signature}"

    def verify(
        self,
        token: str,
        *,
        workspace_id: UUID,
        request_fingerprint: str | None = None,
    ) -> ClientRoundStateClaims:
        try:
            encoded_payload, encoded_signature = token.split(".")
        except ValueError as exc:
            raise ClientRoundStateError("client_state_invalid") from exc
        expected_signature = _base64url_encode(self._sign(encoded_payload.encode()))
        if not hmac.compare_digest(encoded_signature, expected_signature):
            raise ClientRoundStateError("client_state_invalid")
        try:
            payload = json.loads(_base64url_decode(encoded_payload))
        except (ValueError, json.JSONDecodeError) as exc:
            raise ClientRoundStateError("client_state_invalid") from exc
        if (
            payload.get("v") != CLIENT_ROUND_STATE_VERSION
            or payload.get("kind") != self.kind
        ):
            raise ClientRoundStateError("client_state_invalid")
        expires_at = payload.get("exp")
        issued_at = payload.get("iat")
        jti = payload.get("jti")
        task_ids = payload.get("tids")
        claimed_fingerprint = payload.get("req")
        if (
            not isinstance(expires_at, int)
            or not isinstance(issued_at, int)
            or not isinstance(jti, str)
            or not isinstance(task_ids, list)
            or not all(isinstance(task_id, str) for task_id in task_ids)
            or (
                claimed_fingerprint is not None
                and not isinstance(claimed_fingerprint, str)
            )
        ):
            raise ClientRoundStateError("client_state_invalid")
        if self.now().timestamp() >= expires_at:
            raise ClientRoundStateError("client_state_expired")
        try:
            claimed_workspace = UUID(str(payload["ws"]))
            claimed_task_ids = tuple(UUID(task_id) for task_id in task_ids)
        except (KeyError, ValueError) as exc:
            raise ClientRoundStateError("client_state_invalid") from exc
        if claimed_workspace != workspace_id:
            raise ClientRoundStateError("client_state_invalid")
        if claimed_fingerprint != request_fingerprint:
            raise ClientRoundStateError("client_state_invalid")
        return ClientRoundStateClaims(
            workspace_id=claimed_workspace,
            task_ids=claimed_task_ids,
            issued_at=issued_at,
            expires_at=expires_at,
            jti=jti,
            request_fingerprint=claimed_fingerprint,
        )

    def _sign(self, encoded_payload: bytes) -> bytes:
        return hmac.new(
            self.secret.encode(),
            encoded_payload,
            digestmod=hashlib.sha256,
        ).digest()


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)

"""Durable one-use login state; no protocol secrets are stored in the DB."""

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from backend.models.auth_identity import AuthLoginTransaction
from backend.services.db_write_intent import acquire_write_intent
from backend.spi.auth import BeginRequest

LOGIN_TTL_SECONDS = 600


class AuthFlowError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def flow_secrets(binding: str) -> tuple[str, str]:
    nonce = base64url(hashlib.sha256(f"nonce:{binding}".encode()).digest())
    verifier = base64url(hmac.digest(binding.encode(), b"pkce", "sha256"))
    return nonce, verifier


@dataclass
class LoginTransactionStore:
    session_factory: object
    now: object = lambda: datetime.now(UTC)

    def begin(
        self,
        provider,
        *,
        purpose="login",
        return_to,
        remember_me=False,
        user_id=None,
        session_generation=None,
    ):
        state = secrets.token_urlsafe(32)
        binding = secrets.token_urlsafe(32)
        nonce, verifier = flow_secrets(binding)
        capabilities = provider.method.descriptor.capabilities
        request = BeginRequest(
            purpose=purpose,
            state=state,
            nonce=nonce if capabilities.nonce == "supported" else None,
            code_challenge=(
                base64url(hashlib.sha256(verifier.encode()).digest())
                if capabilities.pkce != "unsupported"
                else None
            ),
            force_reauthentication=purpose == "reauth",
        )
        instruction = provider.method.begin(provider.context, request)
        now = self.now()
        with self.session_factory.begin() as session:
            acquire_write_intent(session)
            # Bounded cleanup is unnecessary for correctness; deleting only
            # expired rows leaves in-flight attempts usable on every replica.
            expired = (
                select(AuthLoginTransaction.id)
                .where(AuthLoginTransaction.expires_at <= now)
                .limit(100)
            )
            session.execute(
                delete(AuthLoginTransaction).where(AuthLoginTransaction.id.in_(expired))
            )
            session.add(
                AuthLoginTransaction(
                    state_digest=digest(state),
                    binding_digest=digest(binding),
                    provider_slug=provider.slug,
                    redirect_uri=provider.context.callback_url,
                    purpose=purpose,
                    user_id=user_id,
                    session_generation=session_generation,
                    return_to=return_to,
                    remember_me=remember_me,
                    protocol_request_id=instruction.protocol_request_id,
                    created_at=now,
                    expires_at=now + timedelta(seconds=LOGIN_TTL_SECONDS),
                )
            )
        return instruction, binding

    def consume(self, provider, *, state, binding):
        if (
            not isinstance(state, str)
            or not isinstance(binding, str)
            or not (32 <= len(state) <= 128 and 32 <= len(binding) <= 128)
        ):
            raise AuthFlowError("sso_transaction_invalid")
        with self.session_factory.begin() as session:
            acquire_write_intent(session)
            transaction = session.scalar(
                select(AuthLoginTransaction)
                .where(AuthLoginTransaction.state_digest == digest(state))
                .with_for_update()
            )
            now = self.now()
            if (
                transaction is None
                or transaction.consumed_at is not None
                or transaction.expires_at <= now
                or transaction.provider_slug != provider.slug
                or transaction.redirect_uri != provider.context.callback_url
                or not hmac.compare_digest(transaction.binding_digest, digest(binding))
            ):
                raise AuthFlowError("sso_transaction_invalid")
            transaction.consumed_at = now
            session.flush()
            session.expunge(transaction)
            return transaction

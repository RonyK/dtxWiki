"""Account decisions belong to core; adapter claims never create sessions."""

from dataclasses import dataclass
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.domain.identity_policy import (
    DisplayNamePolicyError,
    normalize_display_name,
    normalize_email,
)
from backend.models.auth_identity import UserIdentity
from backend.models.user import User
from backend.services.auth.login_transactions import AuthFlowError
from backend.utils.database_errors import is_single_column_unique_violation


class ResolvedAccount(NamedTuple):
    user: User
    identity: UserIdentity
    created: bool
    linked: bool


def validate_assertion(provider, assertion):
    accepted = provider.context.config.get("accepted_issuers") or (
        provider.context.config["issuer"],
    )
    if (
        assertion.issuer not in accepted
        or not isinstance(assertion.subject, str)
        or not 0 < len(assertion.subject.encode()) <= 512
        or any(not 0x20 <= ord(char) <= 0x7E for char in assertion.subject)
        or len(assertion.issuer.encode()) > 1024
        or (
            provider.method.descriptor.capabilities.nonce == "supported"
            and assertion.nonce_verified is not True
        )
    ):
        raise AuthFlowError("sso_assertion_invalid")


def assertion_email(assertion):
    if assertion.email is None:
        return None
    try:
        return normalize_email(assertion.email)
    except (ValueError, TypeError):
        return None


def assertion_display_name(assertion, email):
    # An IdP profile name is optional presentation data. A valid external
    # identity must still work when its name exceeds the native account policy.
    try:
        return normalize_display_name(assertion.display_name, email)
    except DisplayNamePolicyError:
        return normalize_display_name(None, email)


def accept_account_email(session, user, email, now, *, linking):
    """Claim a trusted address without moving or blocking an established identity."""
    if not email:
        return
    if user.email == email:
        if user.email_verified_at is None:
            user.email_verified_at = now
        return
    if user.email is not None and not linking:
        return
    owner_id = session.scalar(select(User.id).where(User.email == email))
    if owner_id is not None and owner_id != user.id:
        if linking:
            raise AuthFlowError("account_link_required")
        return
    if user.email is not None:
        return
    try:
        # Another account can claim the address after the lookup. Keep an
        # existing identity usable when that unique constraint wins the race.
        with session.begin_nested():
            user.email = email
            user.email_verified_at = now
            session.flush()
    except IntegrityError as exc:
        if not is_single_column_unique_violation(exc, table="users", column="email"):
            raise
        if linking:
            raise AuthFlowError("account_link_required") from None


@dataclass
class AccountResolver:
    sessions: object

    def resolve(self, session, provider, assertion, transaction, now):
        validate_assertion(provider, assertion)
        tenant = provider.policy.directory_tenant
        if provider.policy.directory_enabled and assertion.tenant_id != tenant:
            raise AuthFlowError("sso_assertion_invalid")
        identity_user_id = session.scalar(
            select(UserIdentity.user_id).where(
                UserIdentity.issuer == assertion.issuer,
                UserIdentity.subject == assertion.subject,
            )
        )
        # All mutation paths lock the user before identity rows. Re-read after
        # taking that lock so a simultaneous unlink cannot leave a stale grant.
        user = None
        if identity_user_id is not None:
            user = session.scalar(
                select(User).where(User.id == identity_user_id).with_for_update()
            )
        identity = session.scalar(
            select(UserIdentity)
            .where(
                UserIdentity.issuer == assertion.issuer,
                UserIdentity.subject == assertion.subject,
            )
            .with_for_update()
        )
        email = assertion_email(assertion)
        trusted_email = (
            email
            if provider.policy.trust_email_verified and assertion.email_verified is True
            else None
        )
        created = False
        linked = False
        if identity is not None:
            if user is None or identity.user_id != user.id:
                # An unlink/relink may have committed between the initial
                # lookup and user lock. Never authorize from that stale lookup.
                raise AuthFlowError("sso_transaction_invalid")
            # A second provider cannot impersonate an already bound identity
            # by returning the same issuer/subject under weaker policy.
            if identity.provider_slug != provider.slug:
                raise AuthFlowError("identity_already_linked")
            if transaction.purpose in {"link", "reauth"} and (
                user is None or user.id != transaction.user_id
            ):
                raise AuthFlowError(
                    "reauthentication_mismatch"
                    if transaction.purpose == "reauth"
                    else "identity_already_linked"
                )
        elif transaction.purpose == "reauth":
            raise AuthFlowError("reauthentication_mismatch")
        elif transaction.purpose == "link":
            user = session.scalar(
                select(User).where(User.id == transaction.user_id).with_for_update()
            )
            linked = True
            via = "user_link"
        else:
            if (
                trusted_email
                and session.scalar(select(User.id).where(User.email == trusted_email))
                is not None
            ):
                raise AuthFlowError("account_link_required")
            if not provider.policy.jit_provisioning:
                raise AuthFlowError("signup_not_allowed")
            # Email never merges accounts. Domain admission and representative
            # email ownership both require verified evidence trusted by policy.
            if provider.policy.allowed_email_domains and not (
                email
                and provider.policy.trust_email_verified
                and assertion.email_verified is True
                and email.rsplit("@", 1)[-1] in provider.policy.allowed_email_domains
            ):
                raise AuthFlowError("signup_not_allowed")
            user = self.sessions.provision_user(
                session,
                email=trusted_email,
                display_name=assertion_display_name(assertion, None),
                email_verified_at=now if trusted_email else None,
            )
            created = True
            via = "jit"
            linked = True
        if user is None or user.status != "active":
            raise AuthFlowError("account_disabled")
        if transaction.purpose in {"link", "reauth"} and (
            transaction.session_generation != user.session_generation
        ):
            raise AuthFlowError("sso_transaction_invalid")
        if not created:
            accept_account_email(
                session,
                user,
                trusted_email,
                now,
                linking=transaction.purpose == "link",
            )
        if linked:
            existing = session.scalar(
                select(UserIdentity.id).where(
                    UserIdentity.user_id == user.id,
                    UserIdentity.provider_slug == provider.slug,
                )
            )
            if existing is not None:
                raise AuthFlowError("identity_already_linked")
            identity = UserIdentity(
                user_id=user.id,
                provider_slug=provider.slug,
                issuer=assertion.issuer,
                subject=assertion.subject,
                email_at_link=email,
                linked_via=via,
            )
            session.add(identity)
        if email is not None:
            identity.last_email = email
            identity.last_email_verified = assertion.email_verified is True
        identity.directory_tenant = (
            tenant if provider.policy.directory_enabled else None
        )
        identity.last_login_at = now
        session.flush()
        return ResolvedAccount(user, identity, created, linked)

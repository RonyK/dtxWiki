"""Names and removal paths shared by account and administrator auth proofs."""

from starlette.responses import Response

from backend.api.cookie_budget import set_bounded_cookie
from backend.app.settings import Settings
from backend.services.session_service import RECENT_REAUTHENTICATION_TTL_SECONDS

ADMIN_PROOF_PATHS = ("/api/v1/admin", "/api/v1/auth/verification")
PENDING_SIGNUP_COOKIE = "dtxwiki_pending_signup"
PENDING_SIGNUP_PATH = "/api/v1/auth/pending-signups"


def account_reauthentication_cookie_name(settings: Settings) -> str:
    return f"{settings.session_cookie_name}_reauth_account"


def set_account_reauthentication_cookie(response, settings, *, secure, result):
    set_bounded_cookie(
        response,
        key=account_reauthentication_cookie_name(settings),
        value=result.proof_token,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/api/v1/auth",
        max_age=RECENT_REAUTHENTICATION_TTL_SECONDS,
        expires=result.reauthenticated_until,
    )


def clear_reauthentication_cookies(
    response: Response, settings: Settings, *, secure: bool
) -> None:
    for key, path in (
        *(
            (f"{settings.session_cookie_name}_reauth", path)
            for path in ADMIN_PROOF_PATHS
        ),
        (account_reauthentication_cookie_name(settings), "/api/v1/auth"),
    ):
        response.delete_cookie(
            key=key,
            httponly=True,
            secure=secure,
            samesite="strict",
            path=path,
        )

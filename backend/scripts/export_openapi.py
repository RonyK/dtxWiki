import json
import sys

from backend.api.auth_sso import BINDING_COOKIE, EMAIL_CONFLICT_COOKIE
from backend.api.auth_verification import email_binding_name
from backend.app.main import create_app
from backend.policy.rest_scopes import (
    REST_REQUIRED_SCOPES,
    REST_SESSION_ONLY_ROUTES,
    REST_SESSION_UNSCOPED_ROUTES,
)

RouteKey = tuple[str, str]

SESSION_COOKIE_SECURITY = [{"SessionCookie": []}]
SESSION_OR_BEARER_SECURITY = [
    {"SessionCookie": []},
    {"HTTPBearer": []},
]
# Fresh primary authentication also satisfies confirmation without a proof
# cookie. OpenAPI describes the required credential; the scope extension below
# records the additional server-side freshness policy.
RECENT_REAUTHENTICATION_SECURITY = SESSION_COOKIE_SECURITY
ACCOUNT_REAUTHENTICATION_SECURITY = SESSION_COOKIE_SECURITY
LOGIN_TRANSACTION_SECURITY = [{"LoginTransactionCookie": []}]
EMAIL_CONFLICT_SECURITY = [{"EmailConflictTicketCookie": []}]
EMAIL_REAUTHENTICATION_SECURITY = [
    {"SessionCookie": [], "AccountEmailBindingCookie": []},
    {"SessionCookie": [], "AdminEmailBindingCookie": []},
]
OPTIONAL_SESSION_COOKIE_SECURITY = [{}, {"SessionCookie": []}]
OAUTH_REVOKE_SECURITY = [
    {"SessionCookie": []},
    {"HTTPBearer": []},
    {},
]

# Python 3.14 updated a few ``http.HTTPStatus`` phrases to RFC 9110 wording,
# while the supported Python 3.12 runtime still exposes the older phrases.
# FastAPI inherits those process-global strings when it builds default response
# descriptions. Pin them so the normative checked-in contract is independent
# of the Python minor version used to export it.
STABLE_HTTP_RESPONSE_DESCRIPTIONS = {
    "413": "Request Entity Too Large",
    "416": "Requested Range Not Satisfiable",
    "422": "Unprocessable Entity",
}
FASTAPI_DEFAULT_HTTP_RESPONSE_DESCRIPTIONS = {
    "413": {"Request Entity Too Large", "Content Too Large"},
    "416": {"Requested Range Not Satisfiable", "Range Not Satisfiable"},
    # "Validation Error" is not an HTTPStatus phrase at all: FastAPI hardcodes
    # it in fastapi/openapi/utils.py for the 422 response it auto-injects on
    # any route with body/query params that doesn't already declare 422/4XX/
    # default itself (see get_openapi_path). It is version-independent and
    # distinct from the HTTPStatus(422).phrase fallback used for a route that
    # *explicitly* declares its own 422 response without a description.
    "422": {"Unprocessable Entity", "Unprocessable Content", "Validation Error"},
}

# These handlers authenticate by reading the session cookie directly instead of
# using RestActorDep, so FastAPI cannot infer their actual security contract.
OPENAPI_SESSION_COOKIE_ROUTES: set[RouteKey] = {
    ("GET", "/api/v1/users/me/deletion-preview"),
    ("DELETE", "/api/v1/users/me"),
    ("GET", "/api/v1/admin/user-deletion-policy"),
    ("GET", "/api/v1/auth/verification"),
    ("POST", "/api/v1/auth/email-reauthentication/request"),
    ("GET", "/api/v1/auth/identities"),
    ("POST", "/api/v1/auth/sso/{slug}/reauthenticate"),
    ("POST", "/api/v1/auth/reauthenticate"),
    ("GET", "/api/v1/auth/me"),
    ("POST", "/api/v1/auth/password"),
    ("POST", "/api/v1/auth/email-verification/request"),
    ("GET", "/api/v1/auth/credential-workspaces"),
    ("GET", "/api/v1/auth/tokens"),
    ("POST", "/api/v1/auth/tokens"),
    ("PATCH", "/api/v1/auth/tokens/{token_id}"),
    ("DELETE", "/api/v1/auth/tokens/{token_id}"),
    ("DELETE", "/api/v1/auth/tokens/{token_id}/record"),
    ("GET", "/oauth/consents"),
    ("PATCH", "/oauth/consents"),
    ("DELETE", "/oauth/consents"),
    ("GET", "/oauth/consents/cleanup"),
    ("POST", "/oauth/consents/cleanup"),
    ("GET", "/api/v1/admin/deployment"),
    ("POST", "/api/v1/admin/deployment/update-check"),
    # Epic-2 Phase 1c (job visibility): same require_server_admin-only gate as
    # /admin/deployment, no recent-reauthentication step -- this is an
    # operations surface, not the protected user-recovery directory.
    ("GET", "/api/v1/admin/jobs/summary"),
    ("GET", "/api/v1/admin/jobs"),
    ("POST", "/api/v1/admin/jobs/{job_id}/retry"),
    # Epic-2 Phase 5c (orphaned Context-media reclamation): same
    # require_server_admin-only gate as the job-visibility routes above.
    ("GET", "/api/v1/admin/context-media/orphans"),
    ("GET", "/api/v1/admin/context-media/attachments"),
    ("DELETE", "/api/v1/admin/context-media/attachments/{asset_id}"),
}

OPENAPI_RECENT_REAUTHENTICATION_ROUTES: set[RouteKey] = {
    ("PATCH", "/api/v1/admin/user-deletion-policy"),
    ("DELETE", "/api/v1/admin/users/{user_id}"),
    ("GET", "/api/v1/admin/users/{user_id}/deletion-preview"),
    ("POST", "/api/v1/admin/users/{user_id}/deletion-restore"),
    ("GET", "/api/v1/admin/workspaces/{workspace_id}/members"),
    ("POST", "/api/v1/admin/workspaces/{workspace_id}/members"),
    ("PATCH", "/api/v1/admin/workspaces/{workspace_id}/members/{user_id}"),
    ("GET", "/api/v1/admin/users"),
    ("POST", "/api/v1/admin/users/{user_id}/password-reset"),
    ("POST", "/api/v1/admin/users/{user_id}/identities/rebind"),
    ("GET", "/api/v1/admin/email-conflicts"),
    ("POST", "/api/v1/admin/email-conflicts/{request_id}/decision"),
    ("GET", "/api/v1/admin/audit-events"),
}

OPENAPI_ACCOUNT_REAUTHENTICATION_ROUTES: set[RouteKey] = {
    ("POST", "/api/v1/auth/sso/{slug}/link"),
    ("DELETE", "/api/v1/auth/identities/{identity_id}"),
    ("POST", "/api/v1/auth/password/set"),
}

OPENAPI_LOGIN_TRANSACTION_ROUTES: set[RouteKey] = {
    ("GET", "/api/v1/auth/sso/{slug}/callback"),
    ("POST", "/api/v1/auth/sso/{slug}/callback/continue"),
}

OPENAPI_EMAIL_REAUTHENTICATION_ROUTES: set[RouteKey] = {
    ("POST", "/api/v1/auth/email-reauthentication/confirm"),
}

# Only the browser that completed a colliding IdP login holds this ticket.
OPENAPI_EMAIL_CONFLICT_ROUTES: set[RouteKey] = {
    ("POST", "/api/v1/auth/sso/email-conflict/request"),
}

OPENAPI_OPTIONAL_SESSION_ROUTES: set[RouteKey] = {
    ("GET", "/oauth/authorize"),
    ("POST", "/oauth/authorize"),
}

OPENAPI_OAUTH_REVOKE_ROUTES: set[RouteKey] = {
    ("POST", "/oauth/revoke"),
}

# Explicitly reviewed unauthenticated operations. Logout is deliberately
# idempotent and public: a missing or invalid cookie still clears browser state.
OPENAPI_PUBLIC_ROUTES: set[RouteKey] = {
    # The external POST landing page only returns a same-origin continuation
    # form; it neither consumes the transaction nor authenticates an account.
    ("POST", "/api/v1/auth/sso/{slug}/callback"),
    ("GET", "/api/v1/capabilities"),
    ("GET", "/api/v1/auth/sso/{slug}/start"),
    ("GET", "/api/v1/auth/methods/{method_id}/icon.png"),
    ("GET", "/health/live"),
    ("GET", "/health/ready"),
    ("GET", "/api/v1/auth/capabilities"),
    ("POST", "/api/v1/auth/signup"),
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/logout"),
    ("POST", "/api/v1/auth/password-reset/request"),
    ("POST", "/api/v1/auth/email-verification/confirm"),
    ("POST", "/api/v1/auth/password-reset/confirm"),
    ("POST", "/api/v1/auth/password-reset/status"),
    ("GET", "/api/v1/public/w/{workspace_slug}"),
    ("GET", "/api/v1/public/w/{workspace_slug}/llms.txt"),
    ("GET", "/api/v1/public/w/{workspace_slug}/pages/{slug}"),
    (
        "GET",
        "/api/v1/public/w/{workspace_slug}/pages/{slug}/media/{asset_id}",
    ),
    ("GET", "/.well-known/oauth-protected-resource"),
    ("GET", "/.well-known/oauth-authorization-server"),
    ("GET", "/.well-known/jwks.json"),
    ("POST", "/oauth/register"),
    ("POST", "/oauth/token"),
}


def reviewed_openapi_security() -> dict[RouteKey, list[dict[str, list]]]:
    """Return the exact reviewed OpenAPI security value for every operation."""

    route_security: dict[RouteKey, list[dict[str, list]]] = {
        route: SESSION_OR_BEARER_SECURITY for route in REST_REQUIRED_SCOPES
    }
    route_security.update(
        {
            route: SESSION_COOKIE_SECURITY
            for route in REST_SESSION_ONLY_ROUTES | REST_SESSION_UNSCOPED_ROUTES
        }
    )
    route_security.update(
        {route: SESSION_COOKIE_SECURITY for route in OPENAPI_SESSION_COOKIE_ROUTES}
    )
    route_security.update(
        {
            route: RECENT_REAUTHENTICATION_SECURITY
            for route in OPENAPI_RECENT_REAUTHENTICATION_ROUTES
        }
    )
    route_security.update(
        {
            route: OPTIONAL_SESSION_COOKIE_SECURITY
            for route in OPENAPI_OPTIONAL_SESSION_ROUTES
        }
    )
    route_security.update(
        {route: OAUTH_REVOKE_SECURITY for route in OPENAPI_OAUTH_REVOKE_ROUTES}
    )
    route_security.update({route: [] for route in OPENAPI_PUBLIC_ROUTES})
    route_security.update(
        {
            route: ACCOUNT_REAUTHENTICATION_SECURITY
            for route in OPENAPI_ACCOUNT_REAUTHENTICATION_ROUTES
        }
    )
    route_security.update(
        {
            route: LOGIN_TRANSACTION_SECURITY
            for route in OPENAPI_LOGIN_TRANSACTION_ROUTES
        }
    )
    route_security.update(
        {
            route: EMAIL_REAUTHENTICATION_SECURITY
            for route in OPENAPI_EMAIL_REAUTHENTICATION_ROUTES
        }
    )
    route_security.update(
        {route: EMAIL_CONFLICT_SECURITY for route in OPENAPI_EMAIL_CONFLICT_ROUTES}
    )
    return route_security


def build_openapi_schema() -> dict:
    app = create_app()
    schema = app.openapi()
    security_schemes = schema.setdefault("components", {}).setdefault(
        "securitySchemes", {}
    )
    security_schemes["SessionCookie"] = {
        "type": "apiKey",
        "in": "cookie",
        "name": app.state.settings.session_cookie_name,
    }
    security_schemes["RecentReauthenticationCookie"] = {
        "type": "apiKey",
        "in": "cookie",
        "name": f"{app.state.settings.session_cookie_name}_reauth",
    }
    security_schemes["AccountReauthenticationCookie"] = {
        "type": "apiKey",
        "in": "cookie",
        "name": f"{app.state.settings.session_cookie_name}_reauth_account",
    }
    security_schemes["LoginTransactionCookie"] = {
        "type": "apiKey",
        "in": "cookie",
        "name": BINDING_COOKIE,
    }
    security_schemes["EmailConflictTicketCookie"] = {
        "type": "apiKey",
        "in": "cookie",
        "name": EMAIL_CONFLICT_COOKIE,
        "description": "Signed ticket from an SSO login whose address collided.",
    }
    security_schemes["AccountEmailBindingCookie"] = {
        "type": "apiKey",
        "in": "cookie",
        "name": email_binding_name("account"),
        "description": "Browser binding for an email confirmation with scope=account.",
    }
    security_schemes["AdminEmailBindingCookie"] = {
        "type": "apiKey",
        "in": "cookie",
        "name": email_binding_name("admin"),
        "description": "Browser binding for an email confirmation with scope=admin.",
    }
    for (method, path), security in reviewed_openapi_security().items():
        operation = schema.get("paths", {}).get(path, {}).get(method.lower())
        if operation is not None:
            operation["security"] = security
            if (method, path) in OPENAPI_RECENT_REAUTHENTICATION_ROUTES:
                operation["x-dtxwiki-confirmation-scope"] = "admin"
            elif (method, path) in OPENAPI_ACCOUNT_REAUTHENTICATION_ROUTES:
                operation["x-dtxwiki-confirmation-scope"] = "account"
    _pin_http_response_descriptions(schema)
    return schema


def _pin_http_response_descriptions(schema: dict) -> None:
    for path_item in schema.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            responses = operation.get("responses")
            if not isinstance(responses, dict):
                continue
            for status_code, description in STABLE_HTTP_RESPONSE_DESCRIPTIONS.items():
                response = responses.get(status_code)
                if (
                    isinstance(response, dict)
                    and response.get("description")
                    in FASTAPI_DEFAULT_HTTP_RESPONSE_DESCRIPTIONS[status_code]
                ):
                    response["description"] = description


def main() -> None:
    json.dump(build_openapi_schema(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()

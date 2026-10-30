"""Isolated HTTPS fake IdP for the single browser SameSite regression.

The adapter supplies canned assertions. All transaction, account, session,
cookie and callback handling comes from the production application.
"""

import argparse
import html
import secrets
from dataclasses import replace

import uvicorn
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from backend.api.middleware.security_headers import (
    CSP_EXTRA_FORM_ACTION_ORIGINS_STATE_KEY,
    CSP_SCRIPT_NONCE_STATE_KEY,
)
from backend.app.db import make_engine, make_session_factory
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.base import Base
from backend.scripts.seed_dev import seed_default_workspace
from backend.services.auth.provider_registry import ProviderRegistry
from backend.services.page_service import PageService
from backend.testing.fake_auth_method import FAKE_ISSUER, FakeAuthMethod


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--database", required=True)
    parser.add_argument("--certificate", required=True)
    parser.add_argument("--key", required=True)
    arguments = parser.parse_args()
    wiki_origin = f"https://127.0.0.1:{arguments.port}"
    idp_origin = f"https://localhost:{arguments.port}"

    class BrowserPostMethod(FakeAuthMethod):
        def begin(self, provider, request):
            instruction = super().begin(provider, request)
            return replace(
                instruction,
                url=instruction.url.replace(FAKE_ISSUER, idp_origin)
                + f"&provider={provider.slug}",
            )

        def complete(self, provider, callback):
            return replace(
                super().complete(provider, callback),
                subject=f"{provider.slug}-subject",
            )

    settings = Settings(
        database_url=f"sqlite+pysqlite:///{arguments.database}",
        public_base_url=wiki_origin,
        session_secret="isolated-browser-samesite-regression",
        session_cookie_secure=True,
        mail_provider="none",
        plugins="",
        auth_providers_file=None,
        auth_password_login_enabled=True,
    )
    engine = make_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory.begin() as session:
        seed_default_workspace(session)
    providers = ProviderRegistry(settings, [BrowserPostMethod()])
    for slug in ("company", "secondary"):
        providers.add(
            {
                "slug": slug,
                "method": "test_oidc",
                "jit_provisioning": True,
                "trust_email_verified": True,
            }
        )
    fixture_router = APIRouter()

    @fixture_router.get("/authorize")
    def authorize(request: Request, state: str, provider: str):
        providers.get(provider)
        nonce = secrets.token_urlsafe(24)
        setattr(request.state, CSP_SCRIPT_NONCE_STATE_KEY, nonce)
        setattr(request.state, CSP_EXTRA_FORM_ACTION_ORIGINS_STATE_KEY, (wiki_origin,))
        action = f"{wiki_origin}/api/v1/auth/sso/{provider}/callback"
        return HTMLResponse(
            "<!doctype html><title>Fixture IdP</title>"
            f'<form method="post" action="{html.escape(action, quote=True)}">'
            f'<input name="state" value="{html.escape(state, quote=True)}">'
            '<input name="code" value="valid">'
            '<button type="submit">Sign in</button></form>'
            f'<script nonce="{nonce}">document.forms[0].submit();</script>',
            headers={
                "Content-Security-Policy": "default-src 'none'; "
                f"script-src 'nonce-{nonce}'; form-action {wiki_origin}",
            },
        )

    @fixture_router.get("/fixture/done")
    def done():
        return HTMLResponse("<!doctype html><title>Flow complete</title>Complete")

    app = create_app(
        settings=settings,
        page_service=PageService(factory),
        extra_mount=lambda app: app.include_router(fixture_router),
    )
    app.state.auth_providers = providers

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=arguments.port,
        ssl_certfile=arguments.certificate,
        ssl_keyfile=arguments.key,
        access_log=False,
    )


if __name__ == "__main__":
    main()

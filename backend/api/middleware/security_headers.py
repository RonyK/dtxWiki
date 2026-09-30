from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
    "content-security-policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "form-action 'self'; "
        "base-uri 'self'; "
        "object-src 'none'"
    ),
}

CSP_EXTRA_FORM_ACTION_ORIGINS_STATE_KEY = "csp_extra_form_action_origins"
CSP_SCRIPT_NONCE_STATE_KEY = "csp_script_nonce"
CSP_SANDBOXED_DOCUMENT_STATE_KEY = "csp_sandboxed_document"

_SANDBOXED_DOCUMENT_CSP = "default-src 'none'; sandbox"

HSTS_HEADER = "strict-transport-security"
# Deliberately without includeSubDomains: dtxWiki cannot know it owns every
# sibling subdomain of the host it is served from, and a hardcoded
# includeSubDomains would pin them for the full max-age. Operators who do own
# the apex can broaden the policy at the TLS-terminating proxy.
DEFAULT_HSTS_VALUE = "max-age=31536000"


class SecurityHeadersMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        enabled: bool,
        hsts: str | None = None,
    ) -> None:
        self.app = app
        self.enabled = enabled
        headers = dict(_HEADER_BYTES)
        if hsts:
            headers[HSTS_HEADER.encode("latin-1")] = hsts.encode("latin-1")
        self._headers = headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self.enabled or scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers_to_apply = self._headers
                sensitive_path = (scope.get("path") or "").rstrip("/")
                if sensitive_path in {"/reset-password", "/verify-email"}:
                    headers_to_apply = dict(headers_to_apply)
                    headers_to_apply[b"referrer-policy"] = b"no-referrer"
                    headers_to_apply[b"cache-control"] = b"no-store"
                    headers_to_apply[b"pragma"] = b"no-cache"
                state = scope.get("state") or {}
                extra_form_action_origins = state.get(
                    CSP_EXTRA_FORM_ACTION_ORIGINS_STATE_KEY
                )
                script_nonce = state.get(CSP_SCRIPT_NONCE_STATE_KEY)
                sandboxed_document = state.get(CSP_SANDBOXED_DOCUMENT_STATE_KEY) is True
                if extra_form_action_origins or script_nonce or sandboxed_document:
                    headers_to_apply = dict(headers_to_apply)
                    if sandboxed_document:
                        headers_to_apply[b"content-security-policy"] = (
                            _SANDBOXED_DOCUMENT_CSP.encode("latin-1")
                        )
                    else:
                        headers_to_apply[b"content-security-policy"] = (
                            _response_content_security_policy(
                                extra_form_action_origins=(
                                    extra_form_action_origins or ()
                                ),
                                script_nonce=script_nonce,
                            ).encode("latin-1")
                        )
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() not in headers_to_apply
                ]
                headers.extend(headers_to_apply.items())
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


_HEADER_BYTES = {
    key.encode("latin-1"): value.encode("latin-1")
    for key, value in SECURITY_HEADERS.items()
}


def _response_content_security_policy(
    *,
    extra_form_action_origins: list[str] | tuple[str, ...],
    script_nonce: str | None,
) -> str:
    directives = []
    for raw_directive in SECURITY_HEADERS["content-security-policy"].split(";"):
        directive = raw_directive.strip()
        if not directive:
            continue
        name, *values = directive.split()
        if name == "form-action":
            values.extend(extra_form_action_origins)
        elif name == "script-src" and script_nonce:
            values.append(f"'nonce-{script_nonce}'")
        directives.append(" ".join((name, *values)))
    return "; ".join(directives)

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Only ``unlisted_web`` pages are excluded from discovery. Successful handlers
# apply these headers after resolving the durable exposure grant. This
# middleware supplies the same fail-closed policy for errors across the public
# API, root llms.txt, and the public-page SPA route, including failures that
# happen before a handler can know a page's scope. Successful public Workspace
# profiles, llms.txt, and ``indexed_web`` page reads remain indexable.
UNLISTED_PUBLIC_SURFACE_HEADERS = {
    "x-robots-tag": "noindex, nofollow",
    "cache-control": "no-store",
}


def _is_public_page_spa(path: str) -> bool:
    parts = path.split("/")
    return (
        len(parts) >= 4
        and parts[1] == "w"
        and parts[3] == "pages"
    )


def _is_public_error_surface(path: str) -> bool:
    return (
        path == "/llms.txt"
        or path == "/api/v1/public"
        or path.startswith("/api/v1/public/")
        or _is_public_page_spa(path)
    )


class PublicSurfaceHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _is_public_error_surface(
            scope.get("path", "")
        ):
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if (
                message["type"] == "http.response.start"
                and message.get("status", 200) >= 400
            ):
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() not in _HEADER_BYTES
                ]
                headers.extend(_HEADER_BYTES.items())
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


_HEADER_BYTES = {
    key.encode("latin-1"): value.encode("latin-1")
    for key, value in UNLISTED_PUBLIC_SURFACE_HEADERS.items()
}

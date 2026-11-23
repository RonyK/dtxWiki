"""Compare browser origins without changing public URL or issuer spelling."""

from urllib.parse import urlsplit

from fastapi import HTTPException, Request


def normalized_origin(value: str | None) -> tuple[str, str, int] | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        if (
            scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            return None
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port
        effective_port = (
            port if port is not None else (443 if scheme == "https" else 80)
        )
        return scheme, host, effective_port
    except (UnicodeError, ValueError):
        return None


def require_same_origin(request: Request) -> None:
    expected = normalized_origin(request.app.state.settings.public_base_url)
    if expected is None or normalized_origin(request.headers.get("origin")) != expected:
        raise HTTPException(
            status_code=403,
            detail={
                "error_code": "origin_required",
                "message": "A same-origin request is required.",
            },
        )

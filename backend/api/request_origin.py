"""Compare browser origins without changing public URL or issuer spelling."""

from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from backend.policy.allowed_hosts import LOOPBACK_HOSTS


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


def require_same_origin(request: Request, *, allow_local_dev: bool = False) -> None:
    settings = request.app.state.settings
    expected = normalized_origin(settings.public_base_url)
    origin = normalized_origin(request.headers.get("origin"))
    if origin is not None and origin == expected:
        return
    if (
        allow_local_dev
        and not settings.public_base_url
        and not settings.is_remote_mode
        and not settings.is_production
        and origin is not None
        and origin[1] in LOOPBACK_HOSTS
        and (
            origin == normalized_origin(str(request.base_url))
            # Vite's proxy forwards the browser origin while rewriting Host.
            or origin
            in {normalized_origin(value) for value in settings.cors_origin_list}
        )
    ):
        return
    raise HTTPException(
        status_code=403,
        detail={
            "error_code": "origin_required",
            "message": "A same-origin request is required.",
        },
    )

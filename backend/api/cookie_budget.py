from starlette.responses import Response

from backend.domain.credential_input import COOKIE_MAX_BYTES


def set_bounded_cookie(response: Response, **options) -> None:
    """Check the complete serialized cookie before adding it to the response."""
    candidate = Response()
    candidate.set_cookie(**options)
    cookie = next(value for key, value in candidate.raw_headers if key == b"set-cookie")
    if len(cookie) > COOKIE_MAX_BYTES:
        raise ValueError("serialized authentication cookie exceeds 4096 bytes")
    response.raw_headers.append((b"set-cookie", cookie))

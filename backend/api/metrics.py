from __future__ import annotations

import hmac
import ipaddress
import socket
import ssl
import threading
from collections import defaultdict
from collections.abc import Mapping
from urllib.parse import urlparse

from cryptography import x509
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from backend.app.settings import Settings
from backend.mcp.scopes import TOOL_REQUIRED_SCOPES
from backend.policy.remote_ip import resolve_remote_ip
from backend.services.oauth_service import REFRESH_REFUSAL_REASONS

metrics_router = APIRouter(tags=["metrics"])

_HELP = {
    "http_requests_total": "Total HTTP requests.",
    "oauth_token_grants_total": "Total OAuth token grant attempts.",
    "mcp_tool_calls_total": "Total MCP tool call attempts.",
}
_GAUGE_HELP = {
    "tls_cert_expiry": "Unix timestamp when the public TLS certificate expires.",
}
_LABELS = {
    "http_requests_total": ("method", "status"),
    "oauth_token_grants_total": ("grant_type", "result", "refusal_reason"),
    "mcp_tool_calls_total": ("tool", "status"),
}
_GRANT_TYPES = {"authorization_code", "refresh_token"}
_RESULTS = {
    "success",
    "invalid_grant",
    "invalid_client",
    "invalid_request",
    "rate_limited",
    "error",
}
_TOOL_STATUSES = {"ok", "error", "denied"}
# Closed label set: the tool label only ever carries registry-known names so
# arbitrary client-supplied names cannot grow the counter dict unboundedly.
_KNOWN_TOOLS = frozenset(TOOL_REQUIRED_SCOPES)
_lock = threading.Lock()
_counters: dict[str, defaultdict[tuple[tuple[str, str], ...], int]] = {
    name: defaultdict(int) for name in _HELP
}


@metrics_router.get("/metrics", include_in_schema=False)
def read_metrics(request: Request):
    settings: Settings = request.app.state.settings
    if not settings.metrics_enabled:
        return JSONResponse(
            {
                "error_code": "metrics_disabled",
                "message": "metrics not enabled",
            },
            status_code=403,
        )
    auth_error = _metrics_auth_error(request, settings)
    if auth_error is not None:
        return auth_error
    sections = [
        _format_counter(name, snapshot)
        for name, snapshot in _counter_snapshot().items()
    ]
    tls_cert_expiry = _tls_cert_expiry_timestamp(settings)
    sections.append(_format_gauge("tls_cert_expiry", tls_cert_expiry))
    body = "\n".join(sections)
    return PlainTextResponse(
        f"{body}\n",
        media_type="text/plain; version=0.0.4",
    )


def inc(metric: str, **labels: str) -> None:
    if metric not in _LABELS:
        raise KeyError(f"unknown metric: {metric}")
    expected = _LABELS[metric]
    if tuple(labels) != expected:
        raise ValueError(f"{metric} labels must be {expected!r}")
    key = tuple((name, str(labels[name])) for name in expected)
    with _lock:
        _counters[metric][key] += 1


def inc_http_request(*, method: str, status: int | str) -> None:
    inc(
        "http_requests_total",
        method=method.upper(),
        status=str(status),
    )


def inc_oauth_token_grant(
    *,
    grant_type: str,
    result: str,
    refusal_reason: str | None = None,
) -> None:
    inc(
        "oauth_token_grants_total",
        grant_type=grant_type if grant_type in _GRANT_TYPES else "other",
        result=result if result in _RESULTS else "error",
        refusal_reason=(
            refusal_reason if refusal_reason in REFRESH_REFUSAL_REASONS else ""
        ),
    )


def inc_mcp_tool_call(*, tool: str, status: str) -> None:
    inc(
        "mcp_tool_calls_total",
        tool=tool if tool in _KNOWN_TOOLS else "unknown",
        status=status if status in _TOOL_STATUSES else "error",
    )


def reset_metrics_for_test() -> None:
    with _lock:
        for counts in _counters.values():
            counts.clear()


def _metrics_auth_error(request: Request, settings: Settings):
    protected = bool(settings.metrics_bearer_token or settings.metrics_allowlist)
    if not protected and not settings.is_production:
        return None
    if settings.metrics_bearer_token and _has_valid_bearer(request, settings):
        return None
    if settings.metrics_allowlist and _remote_allowed(request, settings):
        return None
    if settings.metrics_bearer_token:
        return JSONResponse(
            {"error_code": "unauthorized", "message": "metrics bearer required"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return JSONResponse(
        {"error_code": "forbidden", "message": "metrics remote not allowed"},
        status_code=403,
    )


def _has_valid_bearer(request: Request, settings: Settings) -> bool:
    from backend.domain.credential_input import authorization_header_is_valid

    authorization = request.headers.get("authorization", "")
    if not authorization_header_is_valid(authorization) or not authorization.startswith(
        "Bearer "
    ):
        return False
    supplied = authorization.removeprefix("Bearer ").strip()
    return hmac.compare_digest(supplied, settings.metrics_bearer_token)


def _remote_allowed(request: Request, settings: Settings) -> bool:
    remote_ip = _remote_ip(request, settings)
    if remote_ip is None:
        return False
    try:
        remote = ipaddress.ip_address(remote_ip)
    except ValueError:
        return False
    for entry in settings.metrics_allowlist.split(","):
        value = entry.strip()
        if not value:
            continue
        try:
            if "/" in value:
                if remote in ipaddress.ip_network(value, strict=False):
                    return True
            elif remote == ipaddress.ip_address(value):
                return True
        except ValueError:
            continue
    return False


def _remote_ip(request: Request, settings: Settings) -> str | None:
    return resolve_remote_ip(request, settings)


def _counter_snapshot() -> dict[str, dict[tuple[tuple[str, str], ...], int]]:
    with _lock:
        return {name: dict(counts) for name, counts in _counters.items()}


def _format_counter(
    name: str,
    label_counts: Mapping[tuple[tuple[str, str], ...], int],
) -> str:
    lines = [f"# HELP {name} {_HELP[name]}", f"# TYPE {name} counter"]
    for key, value in sorted(label_counts.items()):
        labels = ",".join(f'{label}="{_escape(raw)}"' for label, raw in key)
        lines.append(f"{name}{{{labels}}} {value}")
    return "\n".join(lines)


def _format_gauge(name: str, value: float | None) -> str:
    lines = [f"# HELP {name} {_GAUGE_HELP[name]}", f"# TYPE {name} gauge"]
    if value is not None:
        lines.append(f"{name} {value:.0f}")
    return "\n".join(lines)


def _tls_cert_expiry_timestamp(settings: Settings) -> float | None:
    parsed = urlparse(settings.public_base_url or "")
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    try:
        der_certificate = _read_tls_peer_certificate(
            parsed.hostname,
            parsed.port or 443,
        )
        certificate = x509.load_der_x509_certificate(der_certificate)
        return certificate.not_valid_after_utc.timestamp()
    except (OSError, ssl.SSLError, ValueError):
        # HTTP-only deployments, an external proxy that is temporarily
        # unreachable, or a malformed/missing certificate must not take down
        # the metrics scrape. In those cases HELP/TYPE remain exposed but the
        # gauge has no sample.
        return None


def _read_tls_peer_certificate(host: str, port: int) -> bytes:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=2.0) as raw_socket:
        with context.wrap_socket(raw_socket, server_hostname=host) as tls_socket:
            certificate = tls_socket.getpeercert(binary_form=True)
    if not certificate:
        raise ValueError("TLS peer did not provide a certificate")
    return certificate


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

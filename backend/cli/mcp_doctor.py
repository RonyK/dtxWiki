from __future__ import annotations

import socket
import ssl
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import anyio
import httpx

from backend.app.settings import Settings, oauth_token_lifetime_report
from backend.cli.mcp_url import normalize_mcp_endpoint_url


@dataclass(frozen=True)
class CheckResult:
    status: str
    detail: Any


def run_http_doctor(
    url: str,
    *,
    token: str | None = None,
    timeout_sec: int = 10,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Check one remote /mcp endpoint with one bearer credential."""
    normalized_url = _normalize_mcp_url(url)
    resolved_settings = settings or Settings()
    checks: dict[str, CheckResult] = {}
    checks["dns"] = check_dns(normalized_url, timeout_sec=timeout_sec)
    checks["tls"] = check_tls(normalized_url, timeout_sec=timeout_sec)
    checks["oauth_metadata"] = check_oauth_metadata(
        normalized_url,
        timeout_sec=timeout_sec,
    )
    checks["list_tools"] = check_list_tools(
        normalized_url,
        token=token,
        timeout_sec=timeout_sec,
    )
    exit_code = 0 if all(check.status == "ok" for check in checks.values()) else 1
    return {
        "url": normalized_url,
        "oauth_token_lifetimes": oauth_token_lifetime_report(resolved_settings),
        "checks": {
            key: {"status": value.status, "detail": value.detail}
            for key, value in checks.items()
        },
        "exit_code": exit_code,
    }


def check_dns(url: str, *, timeout_sec: int) -> CheckResult:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout_sec)
    try:
        return CheckResult("ok", socket.gethostbyname(host))
    except socket.gaierror as exc:
        return CheckResult("fail", {"reason": f"dns: {exc}"})
    except Exception as exc:
        return CheckResult("fail", {"reason": f"dns: {exc}"})
    finally:
        socket.setdefaulttimeout(old_timeout)


def check_tls(url: str, *, timeout_sec: int) -> CheckResult:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return CheckResult("fail", {"reason": "TLS requires https URL"})
    host = parsed.hostname
    if not host:
        return CheckResult("fail", {"reason": "TLS host missing"})
    port = parsed.port or 443
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout_sec) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls_sock:
                cert = tls_sock.getpeercert()
        subject = _format_subject(cert.get("subject", ()))
        return CheckResult(
            "ok",
            {
                "subject": subject,
                "expires_at": cert.get("notAfter"),
            },
        )
    except ssl.SSLCertVerificationError as exc:
        return CheckResult(
            "fail",
            {"reason": f"TLS certificate verification failed: {exc}"},
        )
    except ssl.SSLError as exc:
        return CheckResult("fail", {"reason": f"TLS handshake failed: {exc}"})
    except Exception as exc:
        return CheckResult("fail", {"reason": f"TLS handshake failed: {exc}"})


def check_oauth_metadata(url: str, *, timeout_sec: int) -> CheckResult:
    origin = _origin_from_mcp_url(url)
    expected_resource = url.rstrip("/")
    try:
        with httpx.Client(timeout=timeout_sec) as client:
            resource_resp = client.get(f"{origin}/.well-known/oauth-protected-resource")
            if resource_resp.status_code != 200:
                return CheckResult(
                    "fail",
                    {
                        "reason": (
                            "metadata protected-resource returned "
                            f"{resource_resp.status_code}: {resource_resp.text[:120]}"
                        )
                    },
                )
            resource_payload = resource_resp.json()
            if resource_payload.get("resource") != expected_resource:
                return CheckResult(
                    "fail",
                    {
                        "reason": (
                            "metadata resource mismatch: "
                            f"{resource_payload.get('resource')!r}"
                        )
                    },
                )
            auth_resp = client.get(f"{origin}/.well-known/oauth-authorization-server")
            if auth_resp.status_code != 200:
                return CheckResult(
                    "fail",
                    {
                        "reason": (
                            "metadata authorization-server returned "
                            f"{auth_resp.status_code}: {auth_resp.text[:120]}"
                        )
                    },
                )
            auth_payload = auth_resp.json()
            return CheckResult(
                "ok",
                {
                    "resource": resource_payload.get("resource"),
                    "issuer": auth_payload.get("issuer"),
                },
            )
    except Exception as exc:
        return CheckResult("fail", {"reason": f"metadata fetch failed: {exc}"})


def check_list_tools(
    url: str,
    *,
    token: str | None,
    timeout_sec: int,
) -> CheckResult:
    if not token:
        return CheckResult("fail", {"reason": "unauthorized: --token is required"})
    # The complete header map is assembled here, not inside the coroutine, so a
    # test can see exactly what would go on the wire without opening a socket.
    headers = {
        "Authorization": f"Bearer {token}",
    }
    try:
        return anyio.run(_list_tools_async, url, headers, timeout_sec)
    except Exception as exc:
        reason = str(exc) or exc.__class__.__name__
        return CheckResult("fail", {"reason": reason})


async def _list_tools_async(
    url: str,
    headers: dict[str, str],
    timeout_sec: int,
) -> CheckResult:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(
        url=url,
        headers=headers,
        timeout=timeout_sec,
        sse_read_timeout=timeout_sec,
    ) as (read_stream, write_stream, _get_session_id):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
    names = [tool.name for tool in tools.tools]
    return CheckResult(
        "ok",
        {
            "count": len(names),
            "first_three": names[:3],
        },
    )


def _enabled_word(enabled: bool) -> str:
    return "enabled" if enabled else "disabled"


def format_text_report(result: dict[str, Any]) -> str:
    checks = result["checks"]
    lines = [f"checking {result['url']}"]
    lifetimes = result["oauth_token_lifetimes"]
    lines.extend(
        [
            "effective OAuth token lifetimes (local configuration):",
            (
                "  access token  "
                f"{lifetimes['access_token']['seconds']} seconds "
                f"({lifetimes['access_token']['source']})"
            ),
            (
                "  refresh token "
                f"{lifetimes['refresh_token']['seconds']} seconds "
                f"({lifetimes['refresh_token']['source']})"
            ),
            (
                "  extend while in use "
                f"{_enabled_word(lifetimes['refresh_sliding']['enabled'])} "
                f"({lifetimes['refresh_sliding']['source']})"
            ),
        ]
    )
    label_map = [
        ("dns", "[a] dns resolve"),
        ("tls", "[b] tls handshake"),
        ("oauth_metadata", "[c] oauth metadata"),
        ("list_tools", "[d] list_tools"),
    ]
    for key, label in label_map:
        check = checks[key]
        detail = _plain_detail(key, check["detail"])
        lines.append(f"  {label:<22} {check['status'].upper()} ({detail})")
    if result["exit_code"] == 0:
        lines.append("all checks passed")
    else:
        lines.append("one or more checks failed")
    return "\n".join(lines)


def _plain_detail(key: str, detail: Any) -> str:
    if key == "tls" and isinstance(detail, dict):
        return f"{detail.get('subject')}, expires {detail.get('expires_at')}"
    if key == "list_tools" and isinstance(detail, dict) and "count" in detail:
        names = ", ".join(detail.get("first_three") or [])
        return f"count={detail.get('count')}: {names}"
    if key == "oauth_metadata" and isinstance(detail, dict):
        return f"resource={detail.get('resource')}"
    if isinstance(detail, dict) and "reason" in detail:
        return str(detail["reason"])
    return str(detail)


def _normalize_mcp_url(url: str) -> str:
    return normalize_mcp_endpoint_url(url, allow_http_loopback=True)


def _origin_from_mcp_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _format_subject(subject: tuple) -> str:
    values: list[str] = []
    for item in subject:
        for key, value in item:
            values.append(f"{key}={value}")
    return ", ".join(values)

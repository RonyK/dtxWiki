import json
import socket
import ssl

import pytest
from typer.testing import CliRunner

from backend.cli import main as cli_main
from backend.cli import mcp_doctor
from backend.cli.commands import mcp as cli_mcp


def _ok_result():
    return {
        "url": "https://wiki.example.com/mcp",
        "oauth_token_lifetimes": {
            "access_token": {
                "seconds": 3600,
                "source": "built-in default",
            },
            "refresh_token": {
                "seconds": 15552000,
                "source": "built-in default",
            },
            "refresh_sliding": {
                "enabled": True,
                "source": "built-in default",
            },
        },
        "checks": {
            "dns": {"status": "ok", "detail": "203.0.113.10"},
            "tls": {
                "status": "ok",
                "detail": {
                    "subject": "commonName=wiki.example.com",
                    "expires_at": "May 1",
                },
            },
            "oauth_metadata": {
                "status": "ok",
                "detail": {"resource": "https://wiki.example.com/mcp"},
            },
            "list_tools": {
                "status": "ok",
                "detail": {"count": 3, "first_three": ["guide", "read", "search"]},
            },
        },
        "exit_code": 0,
    }


def test_doctor_happy_path_json(monkeypatch):
    monkeypatch.setattr(
        cli_mcp,
        "run_http_doctor",
        lambda *_args, **_kwargs: _ok_result(),
    )

    result = CliRunner().invoke(
        cli_main.app,
        ["mcp", "doctor", "--http", "https://wiki.example.com/mcp", "--json"],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["checks"]["list_tools"]["detail"]["count"] == 3


def test_doctor_reads_pat_from_environment_without_output_leak(monkeypatch):
    observed = {}

    def fake_doctor(url, *, token, timeout_sec):
        observed.update(url=url, token=token, timeout_sec=timeout_sec)
        return _ok_result()

    monkeypatch.setattr(cli_mcp, "run_http_doctor", fake_doctor)
    monkeypatch.setenv("DTXWIKI_MCP_PAT", "dtxw_doctor_environment_secret")

    result = CliRunner().invoke(
        cli_main.app,
        [
            "mcp",
            "doctor",
            "--http",
            "https://wiki.example.com/mcp",
            "--token-env",
            "DTXWIKI_MCP_PAT",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert observed == {
        "url": "https://wiki.example.com/mcp",
        "token": "dtxw_doctor_environment_secret",
        "timeout_sec": 10,
    }
    assert "dtxw_doctor_environment_secret" not in result.stdout
    assert "dtxw_doctor_environment_secret" not in result.stderr


def test_doctor_happy_path_text(monkeypatch):
    monkeypatch.setattr(
        cli_mcp,
        "run_http_doctor",
        lambda *_args, **_kwargs: _ok_result(),
    )

    result = CliRunner().invoke(
        cli_main.app,
        ["mcp", "doctor", "--http", "https://wiki.example.com/mcp"],
    )

    assert result.exit_code == 0, result.stderr
    assert "dns resolve" in result.stdout
    assert "access token  3600 seconds (built-in default)" in result.stdout
    assert "refresh token 15552000 seconds (built-in default)" in result.stdout
    assert "extend while in use enabled (built-in default)" in result.stdout
    assert "all checks passed" in result.stdout


def test_doctor_dns_fail_returns_nonzero(monkeypatch):
    def fail_dns(_host):
        raise socket.gaierror("name not known")

    monkeypatch.setattr(mcp_doctor.socket, "gethostbyname", fail_dns)

    result = mcp_doctor.check_dns("https://missing.example.invalid/mcp", timeout_sec=1)

    assert result.status == "fail"
    assert "dns" in result.detail["reason"]


def test_doctor_tls_invalid_keyword(monkeypatch):
    def fail_connection(*_args, **_kwargs):
        raise ssl.SSLCertVerificationError("self-signed certificate")

    monkeypatch.setattr(mcp_doctor.socket, "create_connection", fail_connection)

    result = mcp_doctor.check_tls("https://wiki.example.com/mcp", timeout_sec=1)

    assert result.status == "fail"
    assert "TLS" in result.detail["reason"]


def test_doctor_oauth_metadata_404_returns_nonzero(monkeypatch):
    class FakeResponse:
        status_code = 404
        text = "not found"

        def json(self):
            return {}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, _url):
            return FakeResponse()

    monkeypatch.setattr(mcp_doctor.httpx, "Client", FakeClient)

    result = mcp_doctor.check_oauth_metadata(
        "https://wiki.example.com/mcp",
        timeout_sec=1,
    )

    assert result.status == "fail"
    assert "metadata" in result.detail["reason"]


def test_doctor_list_tools_auth_fail():
    result = mcp_doctor.check_list_tools(
        "https://wiki.example.com/mcp",
        token=None,
        timeout_sec=1,
    )

    assert result.status == "fail"
    assert "unauthorized" in result.detail["reason"]


def test_doctor_timeout_per_step(monkeypatch):
    seen = []

    def fake_check(name):
        def _inner(url, *, timeout_sec, token=None):
            seen.append((name, url, timeout_sec, token))
            return mcp_doctor.CheckResult("ok", {})

        return _inner

    monkeypatch.setattr(mcp_doctor, "check_dns", fake_check("dns"))
    monkeypatch.setattr(mcp_doctor, "check_tls", fake_check("tls"))
    monkeypatch.setattr(mcp_doctor, "check_oauth_metadata", fake_check("metadata"))
    monkeypatch.setattr(mcp_doctor, "check_list_tools", fake_check("list_tools"))

    result = mcp_doctor.run_http_doctor(
        "https://wiki.example.com/mcp",
        token="dtxw_token",
        timeout_sec=7,
    )

    assert result["exit_code"] == 0
    assert {item[2] for item in seen} == {7}
    assert seen[-1][-1] == "dtxw_token"


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:21080/mcp",
        "http://localhost.:21080/mcp",
        "http://127.0.0.2:21080/prefix/mcp/",
        "http://[::1]:21080/mcp",
        "http://192.168.1.10:21080/mcp",
        "http://[fd00::1]:21080/mcp",
    ],
)
def test_doctor_accepts_plaintext_for_local_network(url):
    assert mcp_doctor._normalize_mcp_url(url).endswith("/mcp")


def test_doctor_normalizes_prefixed_https_endpoint(monkeypatch):
    seen = []

    def fake_check(url, **_kwargs):
        seen.append(url)
        return mcp_doctor.CheckResult("ok", {})

    monkeypatch.setattr(mcp_doctor, "check_dns", fake_check)
    monkeypatch.setattr(mcp_doctor, "check_tls", fake_check)
    monkeypatch.setattr(mcp_doctor, "check_oauth_metadata", fake_check)
    monkeypatch.setattr(mcp_doctor, "check_list_tools", fake_check)

    result = mcp_doctor.run_http_doctor(
        "  https://wiki.example.com/dtx/mcp/  ",
        token="dtxw_token",
    )

    assert result["url"] == "https://wiki.example.com/dtx/mcp"
    assert seen == ["https://wiki.example.com/dtx/mcp"] * 4


@pytest.mark.parametrize(
    ("invalid_url", "message", "secret"),
    [
        ("https:///mcp", "with a host", None),
        ("http://wiki.example.com/mcp", "https://", None),
        (
            "https://alice:embedded-secret@wiki.example.com/mcp",
            "embedded credentials",
            "embedded-secret",
        ),
        (
            "https://wiki.example.com/mcp?token=query-secret",
            "query string",
            "query-secret",
        ),
        (
            "https://wiki.example.com/mcp#fragment-secret",
            "fragment",
            "fragment-secret",
        ),
        ("https://wiki.example.com/mcp/child", "exact segment /mcp", None),
        ("https://[::1/mcp", "malformed", None),
    ],
)
def test_doctor_rejects_unsafe_endpoint_urls_without_echoing_them(
    invalid_url,
    message,
    secret,
):
    result = CliRunner().invoke(
        cli_main.app,
        ["mcp", "doctor", "--http", invalid_url, "--json"],
    )

    assert result.exit_code == 1
    assert message in result.stderr
    assert invalid_url not in result.stdout
    assert invalid_url not in result.stderr
    if secret is not None:
        assert secret not in result.stdout
        assert secret not in result.stderr


def test_doctor_reports_effective_ttls_and_sources_without_secrets(monkeypatch):
    monkeypatch.setenv("DTXWIKI_OAUTH_ACCESS_TOKEN_TTL_SECONDS", "7200")
    monkeypatch.setenv("DTXWIKI_OAUTH_REFRESH_TOKEN_TTL_SECONDS", "20000000")
    monkeypatch.setenv("DTXWIKI_SESSION_SECRET", "must-not-leak")
    monkeypatch.setattr(
        mcp_doctor,
        "check_dns",
        lambda *_args, **_kwargs: mcp_doctor.CheckResult("ok", {}),
    )
    monkeypatch.setattr(
        mcp_doctor,
        "check_tls",
        lambda *_args, **_kwargs: mcp_doctor.CheckResult("ok", {}),
    )
    monkeypatch.setattr(
        mcp_doctor,
        "check_oauth_metadata",
        lambda *_args, **_kwargs: mcp_doctor.CheckResult("ok", {}),
    )
    monkeypatch.setattr(
        mcp_doctor,
        "check_list_tools",
        lambda *_args, **_kwargs: mcp_doctor.CheckResult("ok", {}),
    )

    result = mcp_doctor.run_http_doctor(
        "https://wiki.example.com/mcp",
        token="must-not-leak-either",
    )
    serialized = json.dumps(result, sort_keys=True)

    assert result["oauth_token_lifetimes"] == {
        "access_token": {"seconds": 7200, "source": "environment override"},
        "refresh_token": {
            "seconds": 20000000,
            "source": "environment override",
        },
        "refresh_sliding": {
            "enabled": True,
            "source": "built-in default",
        },
    }
    assert "must-not-leak" not in serialized

"""Tests for production smoke scripts (Task 4.9)."""

import datetime as dt
import http.client
import ipaddress
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socket import socket

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from backend.tests.utils.environment import child_env_without_dtxwiki_config

PACKAGING_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGING_DIR.parents[1]
SH_SCRIPT = PACKAGING_DIR / "production_smoke.sh"
PS1_SCRIPT = PACKAGING_DIR / "production_smoke.ps1"
PACKAGED_BROWSER_SMOKE = PACKAGING_DIR / "packaged_browser_smoke.py"


def test_smoke_scripts_exist():
    assert SH_SCRIPT.is_file(), "production_smoke.sh missing"
    assert PS1_SCRIPT.is_file(), "production_smoke.ps1 missing"


def test_root_health_paths_present():
    content = SH_SCRIPT.read_text()
    assert "/health/live" in content
    assert "/health/ready" in content


def test_packaged_browser_smoke_waits_on_root_liveness():
    content = PACKAGED_BROWSER_SMOKE.read_text()
    assert 'url = f"http://127.0.0.1:{port}/health/live"' in content
    assert "/api/v1/health" not in content


def test_sh_has_five_checks():
    content = SH_SCRIPT.read_text()
    check_labels = re.findall(r"Check \([a-e]\)", content)
    assert len(check_labels) == 5, (
        f"Expected 5 lettered checks, found {len(check_labels)}"
    )


def test_ps1_has_same_check_count():
    content = PS1_SCRIPT.read_text()
    check_labels = re.findall(r"Check \([a-e]\)", content)
    assert len(check_labels) == 5, (
        f"PS1: Expected 5 lettered checks, found {len(check_labels)}"
    )


def test_smoke_sh_requires_https():
    content = SH_SCRIPT.read_text()
    assert "https://" in content
    assert "ERROR: argument must be https://" in content


def test_smoke_scripts_expect_mcp_resource_metadata():
    assert '"$ORIGIN/mcp"' in SH_SCRIPT.read_text()
    assert "+ '/mcp" in PS1_SCRIPT.read_text()


def test_smoke_sh_normalizes_origin_and_tls_authority():
    content = SH_SCRIPT.read_text()
    assert 'ORIGIN="${ORIGIN%/}"' in content
    assert 'AUTHORITY="${ORIGIN#https://}"' in content
    assert 'TLS_HOST="${AUTHORITY%%:*}"' in content
    assert "DTXWIKI_SMOKE_RESOLVE_IP" in content
    assert '-connect "$TLS_CONNECT_HOST:$TLS_PORT"' in content


def test_smoke_sh_verifies_tls_with_optional_ca_file():
    content = SH_SCRIPT.read_text()
    assert "-verify_return_error" in content
    assert "-CAfile" in content
    assert "DTXWIKI_SMOKE_CACERT" in content


def test_smoke_ps1_supports_ca_file_and_origin_normalization():
    content = PS1_SCRIPT.read_text()
    assert "[string]$CaCertPath" in content
    assert '$Origin = $Origin.TrimEnd("/")' in content
    assert "-verify_return_error" in content
    assert "-CAfile" in content
    assert "$($tls_host):$($tls_port)" in content
    assert "SkipTlsCheck" not in content


@pytest.mark.production_smoke
def test_smoke_script_runs_against_local_https_proxy(tmp_path):
    if shutil.which("curl") is None:
        pytest.skip("curl is unavailable")
    if shutil.which("openssl") is None:
        pytest.skip("openssl is unavailable")

    backend_port = _free_port()
    proxy_port = _free_port()
    origin = f"https://dtxwiki.test:{proxy_port}"
    db_path = tmp_path / "smoke.sqlite"
    jwt_key_path = tmp_path / "jwt.pem"
    cert_path = tmp_path / "tls.crt"
    cert_key_path = tmp_path / "tls.key"
    _write_rsa_private_key(jwt_key_path)
    _write_self_signed_cert(cert_path, cert_key_path)

    backend = _start_backend(
        backend_port=backend_port,
        origin=origin,
        db_path=db_path,
        jwt_key_path=jwt_key_path,
        tmp_path=tmp_path,
    )
    proxy = None
    try:
        _wait_for_url(
            f"http://127.0.0.1:{backend_port}/health/live",
            backend,
            headers={
                "Host": f"dtxwiki.test:{proxy_port}",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": f"dtxwiki.test:{proxy_port}",
            },
        )
        proxy = _start_https_proxy(
            listen_port=proxy_port,
            backend_port=backend_port,
            cert_path=cert_path,
            cert_key_path=cert_key_path,
        )
        result = subprocess.run(
            ["bash", str(SH_SCRIPT), origin, str(cert_path)],
            cwd=REPO_ROOT,
            env={**os.environ, "DTXWIKI_SMOKE_RESOLVE_IP": "127.0.0.1"},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        assert result.returncode == 0
        assert "ALL CHECKS PASSED" in result.stdout
    finally:
        if proxy is not None:
            proxy.shutdown()
            proxy.server_close()
        _stop_process(backend)


def _free_port() -> int:
    with socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_rsa_private_key(path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


def _write_self_signed_cert(cert_path: Path, key_path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1"),
        ]
    )
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=7))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.DNSName("dtxwiki.test"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


def _start_backend(
    *,
    backend_port: int,
    origin: str,
    db_path: Path,
    jwt_key_path: Path,
    tmp_path: Path,
) -> subprocess.Popen:
    env = child_env_without_dtxwiki_config()
    # The spawned server resolves its whole storage tree from DTXWIKI_DATA_DIR.
    # It gets its own subtree rather than the session's, so the server's files
    # land inside this test's tmp_path.
    env["DTXWIKI_DATA_DIR"] = str(tmp_path / "data-dir")
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "DTXWIKI_APP_ENV": "production",
            "DTXWIKI_APP_MODE": "team",
            "DTXWIKI_PUBLIC_BASE_URL": origin,
            "DTXWIKI_SESSION_SECRET": "test-" + "x" * 64,
            "DTXWIKI_CORS_ORIGINS": origin,
            "DTXWIKI_BEHIND_PROXY": "true",
            "DTXWIKI_FORWARDED_ALLOW_IPS": "127.0.0.1",
            "DTXWIKI_DATABASE_URL": f"sqlite+pysqlite:///{db_path}",
            "DTXWIKI_AUTO_BOOTSTRAP": "false",
            "DTXWIKI_OAUTH_JWT_PRIVATE_KEY_PATH": str(jwt_key_path),
            "DTXWIKI_MCP_HTTP_MODE": "streamable",
        }
    )
    # With DTXWIKI_AUTO_BOOTSTRAP=false, serve refuses a database that is not at
    # head, exactly as in production, so the operator step runs first.
    subprocess.run(
        [sys.executable, "-m", "backend.cli.main", "bootstrap"],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "backend.cli.main",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(backend_port),
            "--no-open",
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _start_https_proxy(
    *,
    listen_port: int,
    backend_port: int,
    cert_path: Path,
    cert_key_path: Path,
) -> ThreadingHTTPServer:
    class ProxyHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            conn = http.client.HTTPConnection(
                "127.0.0.1",
                backend_port,
                timeout=10,
            )
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in {"connection", "proxy-connection"}
            }
            headers["Host"] = f"127.0.0.1:{backend_port}"
            headers["X-Forwarded-Proto"] = "https"
            headers["X-Forwarded-Host"] = self.headers.get("Host", "")
            conn.request("GET", self.path, headers=headers)
            upstream = conn.getresponse()
            body = upstream.read()
            self.send_response(upstream.status)
            for key, value in upstream.getheaders():
                if key.lower() in {
                    "connection",
                    "content-length",
                    "transfer-encoding",
                }:
                    continue
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", listen_port), ProxyHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert_path), str(cert_key_path))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _wait_for_url(
    url: str,
    process: subprocess.Popen,
    *,
    headers: dict[str, str] | None = None,
) -> None:
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise AssertionError(f"backend exited early:\n{output}")
        try:
            request = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(request, timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.2)
    raise AssertionError(f"backend did not become ready: {url}")


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)

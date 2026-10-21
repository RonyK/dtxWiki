from __future__ import annotations

import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "tests" / "e2e" / "compose.remote.yml"
DEFAULT_REMOTE_ORIGIN = "https://dtxwiki.test:8443"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--remote-client",
        action="store",
        default="auto",
        help="Remote MCP client label for Phase-5 smoke evidence.",
    )
    parser.addoption(
        "--remote-smoke-mode",
        action="store",
        default=_env("DTXWIKI_REMOTE_SMOKE_MODE", "remote"),
        choices=("remote", "local"),
        help=(
            "Phase-5 smoke mode. 'remote' uses Docker/remote fixtures; "
            "'local' keeps the in-process fallback for non-Docker dev hosts."
        ),
    )


@dataclass(frozen=True)
class RemoteHarnessConfig:
    origin: str
    mcp_url: str
    compose_file: Path
    project_name: str
    ca_file: str | None
    timeout_seconds: int


@dataclass(frozen=True)
class RemoteSeedMaterial:
    alice_email: str
    bob_email: str
    workspace_a: str
    workspace_b: str
    pat_read: str
    pat_agent: str
    workspace_id: str
    page_id: str
    page_slug: str
    foreign_workspace_id: str
    foreign_page_id: str
    restricted_workspace_id: str
    restricted_artifact_id: str
    login_email: str
    login_password: str
    oauth_client_id: str | None
    oauth_client_secret: str | None


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _required_secret(name: str) -> str:
    value = _env(name)
    if value is None:
        pytest.skip(f"{name} is required for remote MCP smoke")
    return value


def _compose_command() -> list[str]:
    if shutil.which("docker"):
        probe = subprocess.run(
            ["docker", "compose", "version"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if probe.returncode == 0:
            return ["docker", "compose"]
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    pytest.skip("Docker Compose is required for remote MCP smoke")


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


@pytest.fixture(scope="session")
def remote_harness_config() -> RemoteHarnessConfig:
    origin = _env("DTXWIKI_REMOTE_PUBLIC_BASE_URL", DEFAULT_REMOTE_ORIGIN)
    assert origin is not None
    return RemoteHarnessConfig(
        origin=origin.rstrip("/"),
        mcp_url=f"{origin.rstrip('/')}/mcp",
        compose_file=COMPOSE_FILE,
        project_name=_env("DTXWIKI_REMOTE_E2E_PROJECT", "dtxwiki-remote-e2e"),
        ca_file=_env("DTXWIKI_REMOTE_E2E_CA_FILE"),
        timeout_seconds=int(_env("DTXWIKI_REMOTE_E2E_TIMEOUT", "120") or "120"),
    )


@pytest.fixture(scope="session")
def remote_compose_project(
    remote_harness_config: RemoteHarnessConfig,
) -> Iterator[RemoteHarnessConfig]:
    if _env("DTXWIKI_REMOTE_E2E_SKIP_COMPOSE") == "1":
        yield remote_harness_config
        return

    cmd = _compose_command()
    project_args = [
        *cmd,
        "-f",
        str(remote_harness_config.compose_file),
        "-p",
        remote_harness_config.project_name,
    ]
    _run([*project_args, "up", "-d"])
    ca_file = _copy_caddy_root_ca(project_args)
    config = replace(remote_harness_config, ca_file=str(ca_file))
    try:
        yield config
    finally:
        _run([*project_args, "down", "-v"], check=False)


@pytest.fixture(scope="session")
def remote_hostname_ready(remote_harness_config: RemoteHarnessConfig) -> None:
    # When compose is skipped and the origin uses 127.0.0.1 directly, the
    # dtxwiki.test hostname check is irrelevant — skip it.
    if _env("DTXWIKI_REMOTE_E2E_SKIP_COMPOSE") == "1":
        origin_host = remote_harness_config.origin.split("://", 1)[-1].split(":")[0]
        if origin_host in ("127.0.0.1", "localhost"):
            return
    try:
        resolved = socket.gethostbyname("dtxwiki.test")
    except socket.gaierror:
        pytest.skip("Add '127.0.0.1 dtxwiki.test' before remote smoke")
    if resolved != "127.0.0.1":
        pytest.skip(f"dtxwiki.test resolves to {resolved}, expected 127.0.0.1")


@pytest.fixture(scope="session")
def remote_seed_material() -> RemoteSeedMaterial:
    return RemoteSeedMaterial(
        alice_email=_env("DTXWIKI_REMOTE_E2E_ALICE", "alice@dtxwiki.test")
        or "alice@dtxwiki.test",
        bob_email=_env("DTXWIKI_REMOTE_E2E_BOB", "bob@dtxwiki.test")
        or "bob@dtxwiki.test",
        workspace_a=_env("DTXWIKI_REMOTE_E2E_WORKSPACE_A", "wsa") or "wsa",
        workspace_b=_env("DTXWIKI_REMOTE_E2E_WORKSPACE_B", "wsb") or "wsb",
        pat_read=_required_secret("DTXWIKI_REMOTE_E2E_PAT_READ"),
        pat_agent=_required_secret("DTXWIKI_REMOTE_E2E_PAT_AGENT"),
        workspace_id=_required_secret("DTXWIKI_REMOTE_E2E_WORKSPACE_ID"),
        page_id=_required_secret("DTXWIKI_REMOTE_E2E_PAGE_ID"),
        page_slug=_env("DTXWIKI_REMOTE_E2E_PAGE_SLUG", "phase5-remote-page")
        or "phase5-remote-page",
        foreign_workspace_id=_required_secret(
            "DTXWIKI_REMOTE_E2E_FOREIGN_WORKSPACE_ID"
        ),
        foreign_page_id=_required_secret("DTXWIKI_REMOTE_E2E_FOREIGN_PAGE_ID"),
        restricted_workspace_id=_required_secret(
            "DTXWIKI_REMOTE_E2E_RESTRICTED_WORKSPACE_ID"
        ),
        restricted_artifact_id=_required_secret(
            "DTXWIKI_REMOTE_E2E_RESTRICTED_ARTIFACT_ID"
        ),
        login_email=_env("DTXWIKI_REMOTE_E2E_LOGIN_EMAIL")
        or _env("DTXWIKI_REMOTE_E2E_ALICE", "alice@dtxwiki.test")
        or "alice@dtxwiki.test",
        login_password=_required_secret("DTXWIKI_REMOTE_E2E_LOGIN_PASSWORD"),
        oauth_client_id=_env("DTXWIKI_REMOTE_E2E_OAUTH_CLIENT_ID"),
        oauth_client_secret=_env("DTXWIKI_REMOTE_E2E_OAUTH_CLIENT_SECRET"),
    )


@pytest.fixture
def remote_pat_read_headers(remote_seed_material: RemoteSeedMaterial) -> dict[str, str]:
    return {"Authorization": f"Bearer {remote_seed_material.pat_read}"}


@pytest.fixture
def remote_pat_agent_headers(
    remote_seed_material: RemoteSeedMaterial,
) -> dict[str, str]:
    return {"Authorization": f"Bearer {remote_seed_material.pat_agent}"}


def wait_for_remote_ready(config: RemoteHarnessConfig) -> None:
    deadline = time.monotonic() + config.timeout_seconds
    request = Request(f"{config.origin}/health/live")
    if config.ca_file:
        tls_context = ssl.create_default_context(cafile=config.ca_file)
    else:
        tls_context = ssl.create_default_context()
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(request, timeout=5, context=tls_context) as response:
                if 200 <= response.status < 500:
                    return
        except URLError as exc:
            last_error = exc
        time.sleep(1)
    raise AssertionError(f"remote harness did not become ready: {last_error}")


def _copy_caddy_root_ca(project_args: list[str]) -> Path:
    ca_path = Path(tempfile.mkdtemp(prefix="dtxwiki-phase5-ca-")) / "caddy-root.crt"
    last_error = ""
    for _attempt in range(30):
        result = _run(
            [
                *project_args,
                "cp",
                "caddy:/data/caddy/pki/authorities/local/root.crt",
                str(ca_path),
            ],
            check=False,
        )
        if result.returncode == 0 and ca_path.exists() and ca_path.stat().st_size > 0:
            return ca_path
        last_error = result.stdout
        time.sleep(1)
    raise AssertionError(
        f"could not copy Caddy root CA for TLS verification: {last_error}"
    )


@pytest.fixture(scope="session")
def remote_ready(
    remote_compose_project: RemoteHarnessConfig,
    remote_hostname_ready: None,
) -> RemoteHarnessConfig:
    wait_for_remote_ready(remote_compose_project)
    return remote_compose_project

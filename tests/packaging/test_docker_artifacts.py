"""Tests for the production Docker self-host artifacts.

The tests marked ``DOCKER_SMOKE`` need a Docker daemon and run only with
``DTXWIKI_DOCKER_BUILD_SMOKE=1`` (release gate): they build the image, start it
with the environment docker-compose.yml gives each service, and drive the
proxy templates with real Caddy and nginx images.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
import yaml

from backend.api.middleware.security_headers import SECURITY_HEADERS
from backend.cli import prod_setup

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "Dockerfile"
DOCKERIGNORE = REPO / ".dockerignore"
INFRA = REPO / "infra" / "prod"
COMPOSE = INFRA / "docker-compose.yml"
ENTRYPOINT = INFRA / "docker-entrypoint.sh"
CADDYFILE = INFRA / "Caddyfile.docker"
ENV_EXAMPLE = INFRA / ".env.docker.example"
NGINX_EXAMPLE = INFRA / "nginx.conf.example"
NGINX_STEP_2_MARKER = "# --- Step 2:"

DTXWIKI_SERVICES = ("init", "app", "worker")
DATA_VOLUME_TARGET = "/var/lib/dtxwiki"
SECRET_FILE = "/var/lib/dtxwiki/session_secret"
JWT_KEY_FILE = "/var/lib/dtxwiki/jwt.pem"

DOCKER_SMOKE = pytest.mark.skipif(
    shutil.which("docker") is None
    or os.environ.get("DTXWIKI_DOCKER_BUILD_SMOKE") != "1",
    reason=(
        "real docker smoke — set DTXWIKI_DOCKER_BUILD_SMOKE=1 with docker "
        "available (release gate); skipped by default as it is slow"
    ),
)
# Image tag and container/volume/network name prefix used by the smoke tests.
SMOKE_IMAGE = os.environ.get("DTXWIKI_DOCKER_SMOKE_IMAGE", "dtxwiki:citest")
SMOKE_PREFIX = os.environ.get("DTXWIKI_DOCKER_SMOKE_PREFIX", "dtxwiki-docker-smoke")
# Ubuntu 24.04 ships Caddy 2.6.2; the official repository and the compose stack
# use the current 2.x. Ubuntu 22.04 ships nginx 1.18, 24.04 nginx 1.24.
CADDY_IMAGES = ("caddy:2", "caddy:2.6.2")
NGINX_IMAGES = ("nginx:1.18", "nginx:1.24-alpine", "nginx:stable-alpine")
UPSTREAM_IMAGE = "python:3.12-slim"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _interpolate(value: str, variables: dict[str, str]) -> str:
    """Resolve compose ``${NAME}``, ``${NAME:-default}`` and ``${NAME:?msg}``."""

    def substitute(match: re.Match[str]) -> str:
        name, operator, default = match.group(1), match.group(2), match.group(3)
        current = variables.get(name, "")
        if operator in {":-", "-"} and not current:
            return default or ""
        return current

    return re.sub(r"\$\{(\w+)(?:(:?[-?])([^}]*))?\}", substitute, value)


def _service_env(service: str, variables: dict[str, str]) -> dict[str, str]:
    raw = _compose()["services"][service]["environment"]
    return {key: _interpolate(str(value), variables) for key, value in raw.items()}


def test_dockerfile_is_multistage_with_frontend():
    text = DOCKERFILE.read_text(encoding="utf-8")
    # frontend build stage kept separate (node toolchain must not ship)
    assert "FROM node:" in text
    assert "AS frontend" in text
    assert "npm" in text and "run build" in text
    # runtime stage copies only the built SPA into backend/static
    assert "COPY --from=frontend" in text
    assert "backend/static" in text
    # multi-stage python runtime
    assert "FROM python:3.12-slim AS runtime" in text
    assert "docker-entrypoint.sh" in text
    assert "--extra-index-url" not in text
    assert text.count('--index-url "$PIP_INDEX_URL"') == 2


def test_compose_prod_stack_shape():
    text = COMPOSE.read_text(encoding="utf-8")
    for service in ("init:", "app:", "worker:", "caddy:"):
        assert service in text, f"missing service {service}"
    assert "caddy:2" in text
    assert "app:21080" not in text  # proxy target lives in the Caddyfile
    assert "sqlite+pysqlite" in text
    assert 'DTXWIKI_AUTO_BOOTSTRAP: "false"' in text
    # Writable state redirected to the data volume (else uploads 500 on the
    # read-only container home), and the skip flag stays out of the DTXWIKI_
    # namespace (a DTXWIKI_ unknown env crash-loops the app via the guard).
    assert "DTXWIKI_DATA_DIR:" in text
    assert 'DTXWIKI_SIGNUP_ENABLED: "${DTXWIKI_SIGNUP_ENABLED:-false}"' in text
    assert 'DTXWIKI_MAIL_PROVIDER: "${DTXWIKI_MAIL_PROVIDER:-none}"' in text
    assert 'DTXWIKI_SMTP_PASSWORD: "${DTXWIKI_SMTP_PASSWORD:-}"' in text
    assert "PIP_EXTRA_INDEX_URL" not in text
    assert "SKIP_BOOTSTRAP:" in text
    assert "DTXWIKI_SKIP_BOOTSTRAP" not in text
    # production stack must NOT bundle the AGPL/SSPL dev sidecars
    for forbidden in ("minio", "postgres:16", "redis:"):
        assert forbidden not in text, f"prod compose must not include {forbidden}"


def test_compose_uses_full_image_var_not_image_tag():
    # Codex #35: compose pins the full image ref via DTXWIKI_IMAGE (default
    # dtxwiki:local). It must NOT use DTXWIKI_IMAGE_TAG appended after "dtxwiki:"
    # (that produced dtxwiki:dtxwiki:<ver> / dtxwiki:registry/dtxwiki:<ver>).
    text = COMPOSE.read_text(encoding="utf-8")
    assert "${DTXWIKI_IMAGE:-dtxwiki:local}" in text
    assert "DTXWIKI_IMAGE_TAG" not in text


@pytest.mark.parametrize("service", DTXWIKI_SERVICES)
def test_compose_worker_mode_does_not_break_0_1_2_image_rollback(service: str):
    # 0.1.2 rejects this unknown DTXWIKI_ variable in production. The image
    # owns the new default, so changing only DTXWIKI_IMAGE back removes it.
    assert "DTXWIKI_WORKER_MODE" not in _service_env(
        service, {"DTXWIKI_DOMAIN": "wiki.example.test"}
    )


@pytest.mark.parametrize("service", DTXWIKI_SERVICES)
def test_compose_gives_every_dtxwiki_service_the_secrets_as_files(service: str):
    """`docker compose exec` never runs the entrypoint; it sees only this env."""

    definition = _compose()["services"][service]
    env = definition["environment"]
    assert env["DTXWIKI_SESSION_SECRET_FILE"] == SECRET_FILE
    assert env["DTXWIKI_OAUTH_JWT_PRIVATE_KEY_PATH"] == JWT_KEY_FILE
    # An operator-pinned secret still wins; the default is empty, never a value.
    assert env["DTXWIKI_SESSION_SECRET"] == "${DTXWIKI_SESSION_SECRET:-}"
    assert f"data:{DATA_VOLUME_TARGET}" in definition["volumes"]


@pytest.mark.parametrize("service", ["app", "worker"])
def test_compose_runs_long_lived_services_under_an_init(service: str):
    assert _compose()["services"][service].get("init") is True


def test_compose_documents_exec_and_run_and_private_backups():
    text = COMPOSE.read_text(encoding="utf-8")
    assert "docker compose exec app dtxwiki users add admin@example.com" in text
    assert "docker compose run --rm app dtxwiki" in text
    # Backups: outside the checkout, root-only (s-11).
    assert "umask 077" in text
    assert "/var/backups/dtxwiki" in text
    assert "install -d -m 0700 /var/backups/dtxwiki" in text
    assert "$PWD/backups" not in text


def test_caddyfile_docker_proxies_app_with_autotls():
    text = CADDYFILE.read_text(encoding="utf-8")
    assert "reverse_proxy app:21080" in text
    assert "{$DTXWIKI_DOMAIN}" in text
    assert "{$DTXWIKI_ADMIN_EMAIL}" in text


def test_env_example_requires_two_inputs():
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "DTXWIKI_DOMAIN=" in text
    assert "DTXWIKI_ADMIN_EMAIL=" in text
    assert "Let's Encrypt notices only" in text
    assert "dtxwiki users add admin@example.com" in text
    assert "users add admin@example.com --role" not in text
    assert "dtxwiki admin grant --email admin@example.com" in text
    assert "docker compose run --rm app dtxwiki" in text
    assert "\nDTXWIKI_SIGNUP_ENABLED=false\n" in text
    assert "\nDTXWIKI_MAIL_PROVIDER=none\n" in text
    assert "# DTXWIKI_SMTP_PASSWORD=" in text


def test_env_example_ships_an_empty_session_secret():
    active = [
        line
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if line.startswith("DTXWIKI_SESSION_SECRET")
    ]
    # Empty: the entrypoint then generates the secret file. A placeholder here
    # would be passed to every container as the real secret.
    assert active == ["DTXWIKI_SESSION_SECRET="]


# --------------------------------------------------------------------------- #
# docker-entrypoint.sh, run locally against a fake `dtxwiki`
# --------------------------------------------------------------------------- #
_FAKE_DTXWIKI = """#!/bin/sh
state=missing
if [ -s "$EXPECTED_SECRET_FILE" ]; then state=present; fi
echo "dtxwiki $* secret=$state" >> "$CALL_LOG"
if [ "$1" = keygen ]; then printf 'PEM' > "$3"; fi
"""
_PROBE = """#!/bin/sh
{
  echo "SECRET=${DTXWIKI_SESSION_SECRET-<unset>}"
  echo "SECRET_FILE=${DTXWIKI_SESSION_SECRET_FILE-<unset>}"
  echo "KEY=${DTXWIKI_OAUTH_JWT_PRIVATE_KEY_PATH-<unset>}"
  echo "UMASK=$(umask)"
} > "$PROBE_OUT"
"""


def _run_entrypoint(tmp_path: Path, **env: str) -> tuple[dict[str, str], list[str]]:
    bin_dir = tmp_path / "bin"
    if not bin_dir.exists():
        bin_dir.mkdir()
        (bin_dir / "python").symlink_to(sys.executable)
        for name, body in (("dtxwiki", _FAKE_DTXWIKI), ("probe", _PROBE)):
            script = bin_dir / name
            script.write_text(body, encoding="utf-8")
            script.chmod(0o755)
    data = tmp_path / "data"
    call_log = tmp_path / "calls.log"
    probe_out = tmp_path / "probe.out"
    call_log.unlink(missing_ok=True)
    probe_out.unlink(missing_ok=True)
    full_env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "DTXWIKI_DATA_DIR": str(data),
        "EXPECTED_SECRET_FILE": env.pop(
            "EXPECTED_SECRET_FILE", str(data / "session_secret")
        ),
        "CALL_LOG": str(call_log),
        "PROBE_OUT": str(probe_out),
        **env,
    }
    subprocess.run(["sh", str(ENTRYPOINT), "probe"], env=full_env, check=True)
    seen = dict(
        line.split("=", 1) for line in probe_out.read_text().splitlines()
    )
    calls = call_log.read_text().splitlines() if call_log.exists() else []
    return seen, calls


def test_entrypoint_creates_the_secret_file_first_and_exports_only_its_path(
    tmp_path: Path,
):
    secret_file = tmp_path / "data" / "session_secret"

    seen, calls = _run_entrypoint(
        tmp_path,
        DTXWIKI_SESSION_SECRET="",
        DTXWIKI_SESSION_SECRET_FILE=str(secret_file),
    )

    secret = secret_file.read_text(encoding="utf-8").strip()
    assert len(secret) >= 64
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600
    # The value is never exported: `docker compose exec` could not see it.
    assert seen["SECRET"] == ""
    assert secret not in "".join(f"{k}={v}" for k, v in seen.items())
    assert seen["SECRET_FILE"] == str(secret_file)
    assert seen["KEY"] == str(tmp_path / "data" / "jwt.pem")
    # Same file-mode policy as the systemd units for the server process.
    assert seen["UMASK"] == "0027"
    # dtxwiki refuses to start without the secret file, so it exists before
    # the first dtxwiki command runs.
    assert calls == [
        f"dtxwiki keygen --output {tmp_path / 'data' / 'jwt.pem'} secret=present",
        "dtxwiki bootstrap secret=present",
    ]


def test_entrypoint_keeps_the_secret_across_restarts(tmp_path: Path):
    secret_file = tmp_path / "data" / "session_secret"
    _run_entrypoint(tmp_path, DTXWIKI_SESSION_SECRET_FILE=str(secret_file))
    first = secret_file.read_text(encoding="utf-8")

    _, calls = _run_entrypoint(
        tmp_path, DTXWIKI_SESSION_SECRET_FILE=str(secret_file), SKIP_BOOTSTRAP="1"
    )

    assert secret_file.read_text(encoding="utf-8") == first
    # The key exists now, and app/worker leave the single bootstrap to init.
    assert calls == []


def test_entrypoint_replaces_an_empty_secret_file(tmp_path: Path):
    secret_file = tmp_path / "data" / "session_secret"
    secret_file.parent.mkdir(parents=True)
    secret_file.write_text("", encoding="utf-8")

    _run_entrypoint(tmp_path, DTXWIKI_SESSION_SECRET_FILE=str(secret_file))

    assert len(secret_file.read_text(encoding="utf-8").strip()) >= 64


def test_entrypoint_lets_an_explicit_secret_win(tmp_path: Path):
    seen, _ = _run_entrypoint(tmp_path, DTXWIKI_SESSION_SECRET="pinned-" + "x" * 60)

    assert not (tmp_path / "data" / "session_secret").exists()
    assert seen["SECRET"] == "pinned-" + "x" * 60
    assert seen["SECRET_FILE"] == "<unset>"


def test_entrypoint_defaults_the_secret_file_when_compose_did_not_set_it(
    tmp_path: Path,
):
    # Plain `docker run` without the compose environment.
    seen, _ = _run_entrypoint(tmp_path)

    default = tmp_path / "data" / "session_secret"
    assert default.read_text(encoding="utf-8").strip()
    assert seen["SECRET_FILE"] == str(default)


def test_entrypoint_is_posix_sh():
    result = subprocess.run(
        ["sh", "-n", str(ENTRYPOINT)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


# --------------------------------------------------------------------------- #
# Real Docker (opt-in)
# --------------------------------------------------------------------------- #
def _docker(*args: str, check: bool = True, timeout: float = 600, **kwargs):
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, **kwargs
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"docker {' '.join(args[:3])} failed ({result.returncode}):\n"
            f"{(result.stdout + result.stderr)[-4000:]}"
        )
    return result


@pytest.fixture(scope="module")
def built_image() -> str:
    # Codex #1: a real `docker build` (a string check of the Dockerfile could
    # not catch the DEPLOYMENT.md COPY regression).
    _docker("build", "-f", str(DOCKERFILE), "-t", SMOKE_IMAGE, str(REPO), timeout=3600)
    return SMOKE_IMAGE


@DOCKER_SMOKE
def test_docker_image_actually_builds(built_image: str):
    config = json.loads(
        _docker("image", "inspect", "--format", "{{json .Config}}", built_image).stdout
    )
    assert config["User"] == "dtxwiki"
    assert config["Entrypoint"] == ["/usr/local/bin/docker-entrypoint.sh"]
    # An older/custom Compose file has no worker-mode override. The image
    # itself must preserve its existing separate-worker topology.
    image_env = dict(entry.split("=", 1) for entry in config["Env"])
    assert image_env["DTXWIKI_WORKER_MODE"] == "external"
    assert (
        config["Labels"]["org.opencontainers.image.licenses"]
        == "AGPL-3.0-only OR Elastic-2.0"
    )


def _env_args(env: dict[str, str]) -> list[str]:
    return [arg for key, value in env.items() for arg in ("-e", f"{key}={value}")]


def _wait_until_live(container: str, *, attempts: int = 60) -> None:
    probe = (
        "import urllib.request;"
        "urllib.request.urlopen('http://127.0.0.1:21080/health/live', timeout=2)"
    )
    for _ in range(attempts):
        result = _docker("exec", container, "python", "-c", probe, check=False)
        if result.returncode == 0:
            return
        time.sleep(1)
    logs = _docker("logs", container, check=False)
    raise AssertionError(f"{container} never became live:\n{logs.stdout}{logs.stderr}")


_FIND_EXPORTED_SECRET = """
import os, sys
secret = sys.argv[1]
leaks = []
for pid in os.listdir('/proc'):
    if not pid.isdigit():
        continue
    try:
        with open(f'/proc/{pid}/environ', 'rb') as handle:
            entries = handle.read().split(b'\\0')
    except OSError:
        continue
    for entry in entries:
        if entry.startswith(b'DTXWIKI_SESSION_SECRET=') and secret.encode() in entry:
            leaks.append(pid)
print(','.join(leaks))
"""

_MANAGED_WORKER_PIDS = """
from pathlib import Path
pids = []
for path in Path('/proc').glob('[0-9]*/cmdline'):
    try:
        args = path.read_bytes().split(b'\\0')
    except OSError:
        continue
    if b'backend.worker.managed' in args:
        pids.append(path.parent.name)
print(','.join(pids))
"""


@DOCKER_SMOKE
def test_docker_exec_cli_works_with_the_compose_environment(built_image: str):
    """B-7: `docker compose exec app dtxwiki users add ...` must succeed.

    The containers get exactly the environment docker-compose.yml gives the
    init, app and worker services; `docker exec` then runs the CLI without the
    entrypoint, which is what `docker compose exec` does.
    """

    variables = {"DTXWIKI_DOMAIN": "wiki.example.test"}
    compose = _compose()["services"]
    name = f"{SMOKE_PREFIX}-{uuid.uuid4().hex[:8]}"
    volume = f"{name}-data"
    containers = [f"{name}-app", f"{name}-worker"]
    _docker("volume", "create", volume)
    try:
        mount = ["-v", f"{volume}:{DATA_VOLUME_TARGET}"]
        _docker(
            "run", "--rm", "--name", f"{name}-init", *mount,
            *_env_args(_service_env("init", variables)),
            built_image, *compose["init"]["command"],
        )
        _docker(
            "run", "-d", "--init", "--name", containers[0], *mount,
            *_env_args(_service_env("app", variables)),
            built_image,
        )
        _docker(
            "run", "-d", "--init", "--name", containers[1], *mount,
            *_env_args(_service_env("worker", variables)),
            built_image, *compose["worker"]["command"],
        )
        app = containers[0]
        _wait_until_live(app)

        worker_component = _docker(
            "exec", app, "python", "-c",
            "import json, urllib.request; "
            "print(json.load(urllib.request.urlopen("
            "'http://127.0.0.1:21080/health/ready'))['components']['worker'])",
        ).stdout.strip()
        # No worker mode is supplied by Compose. This therefore exercises the
        # image default used by existing deployments that only pull an image.
        assert worker_component == "n/a", "app started a second, managed worker"
        managed_pids = _docker(
            "exec", app, "python", "-c", _MANAGED_WORKER_PIDS,
        ).stdout.strip()
        assert managed_pids == "", f"app spawned managed workers: {managed_pids}"

        password = secrets.token_urlsafe(18)
        added = _docker(
            "exec", app, "dtxwiki", "users", "add", "smoke-admin@example.test",
            "--password", password,
        )
        assert "created user smoke-admin@example.test" in added.stdout
        _docker("exec", app, "dtxwiki", "admin", "grant", "--email",
                "smoke-admin@example.test")
        admins = json.loads(
            _docker("exec", app, "dtxwiki", "admin", "list", "--json").stdout
        )
        assert [row["email"] for row in admins] == ["smoke-admin@example.test"]

        owner_mode = _docker(
            "exec", app, "stat", "-c", "%U %a", SECRET_FILE
        ).stdout.split()
        assert owner_mode == ["dtxwiki", "600"]
        secret = _docker("exec", app, "cat", SECRET_FILE).stdout.strip()
        assert len(secret) >= 64
        leaks = _docker(
            "exec", app, "python", "-c", _FIND_EXPORTED_SECRET, secret
        ).stdout.strip()
        assert leaks == "", f"processes exporting the session secret: {leaks}"
        worker_state = _docker(
            "inspect", "--format", "{{.State.Running}} {{.RestartCount}}", containers[1]
        ).stdout.split()
        assert worker_state == ["true", "0"]
    finally:
        _docker("rm", "-f", "-v", *containers, check=False)
        _docker("volume", "rm", volume, check=False)


@DOCKER_SMOKE
def test_dockerignore_keeps_local_secrets_out_of_the_build_context(tmp_path: Path):
    context = tmp_path / "context"
    planted = [
        ".env",
        "infra/prod/.env",
        "infra/prod/.env.local",
        "frontend/.env.production.local",
        "infra/prod/backups/dtxwiki-2026-01-01.tgz",
        "dtxwiki.sqlite",
        "backend/data/dtxwiki.sqlite-wal",
        "backend/cli/__pycache__/main.cpython-312.pyc",
        "backend/app/stale.pyc",
        "dtxwiki.egg-info/PKG-INFO",
        "Makefile.local",
        # The private document roots (no extension: a tracked `root/x.ext`
        # string reads as a citation to tests/static/test_design_source_contract).
        "review/report",
        "doc/plan",
        "docs/design",
        "frontend/node_modules/x/index.js",
    ]
    kept = [
        "infra/prod/.env.example",
        "infra/prod/.env.docker.example",
        "frontend/.env.example",
        "infra/prod/docker-entrypoint.sh",
        "backend/app/main.py",
        "tests/packaging/production_smoke.sh",
    ]
    for relative in (*planted, *kept):
        path = context / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    shutil.copyfile(DOCKERIGNORE, context / ".dockerignore")
    dockerfile = tmp_path / "Dockerfile.context"
    dockerfile.write_text("FROM scratch\nCOPY . /\n", encoding="utf-8")
    exported = tmp_path / "exported"

    _docker(
        "build", "-f", str(dockerfile), "--output", f"type=local,dest={exported}",
        str(context),
    )

    files = {
        path.relative_to(exported).as_posix()
        for path in exported.rglob("*")
        if path.is_file()
    }
    assert set(kept) <= files
    assert sorted(set(planted) & files) == []


# --------------------------------------------------------------------------- #
# Proxy templates against real Caddy and nginx (opt-in)
# --------------------------------------------------------------------------- #
CADDY_TEMPLATES = sorted(path.name for path in INFRA.glob("Caddyfile*"))


def _self_signed(directory: Path, host: str) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), False)
        .sign(key, hashes.SHA256())
    )
    directory.mkdir(parents=True, exist_ok=True)
    cert_path = directory / "fullchain.pem"
    key_path = directory / "privkey.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    for path in (directory, cert_path, key_path):
        path.chmod(0o755 if path.is_dir() else 0o644)
    return cert_path, key_path


def test_caddy_template_list_is_complete():
    assert CADDY_TEMPLATES == [
        "Caddyfile.custom.example",
        "Caddyfile.docker",
        "Caddyfile.docker.internal",
        "Caddyfile.example",
        "Caddyfile.insecure.example",
        "Caddyfile.internal.example",
    ]


@DOCKER_SMOKE
@pytest.mark.parametrize("image", CADDY_IMAGES)
@pytest.mark.parametrize("template", CADDY_TEMPLATES)
def test_caddy_template_validates(tmp_path: Path, template: str, image: str):
    certs = tmp_path / "certs"
    _self_signed(certs, "wiki.example.test")
    rendered = prod_setup.render_caddyfile(
        (INFRA / template).read_text(encoding="utf-8"),
        "wiki.example.test",
        cert="/certs/fullchain.pem",
        key="/certs/privkey.pem",
    )
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text(rendered, encoding="utf-8")
    caddyfile.chmod(0o644)

    result = _docker(
        "run", "--rm",
        "-v", f"{caddyfile}:/etc/caddy/Caddyfile:ro",
        "-v", f"{certs}:/certs:ro",
        "-e", "DTXWIKI_DOMAIN=wiki.example.test",
        "-e", "DTXWIKI_ADMIN_EMAIL=admin@example.test",
        image,
        "sh", "-c",
        "mkdir -p /var/log/caddy && "
        "caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile",
    )
    assert "Valid configuration" in result.stdout + result.stderr


def _nginx_step_two(text: str) -> str:
    head, marker, tail = text.partition(NGINX_STEP_2_MARKER)
    assert marker, "nginx example lost its step-2 marker"
    lines = tail.splitlines()[1:]
    body = [line[1:] if line.startswith("#") else line for line in lines]
    return head + "\n".join(body) + "\n"


@DOCKER_SMOKE
@pytest.mark.parametrize("image", NGINX_IMAGES)
def test_nginx_example_loads_before_and_after_the_certificate(
    tmp_path: Path, image: str
):
    certs = tmp_path / "letsencrypt"
    _self_signed(certs / "live" / "wiki.example.com", "wiki.example.com")
    certs.chmod(0o755)
    (certs / "live").chmod(0o755)
    template = NGINX_EXAMPLE.read_text(encoding="utf-8")

    for phase, text, mounts in (
        ("before certbot", template, []),
        (
            "after certbot",
            _nginx_step_two(template),
            ["-v", f"{certs}:/etc/letsencrypt:ro"],
        ),
    ):
        conf = tmp_path / f"{phase.replace(' ', '-')}.conf"
        conf.write_text(text, encoding="utf-8")
        conf.chmod(0o644)
        result = _docker(
            "run", "--rm", "--entrypoint", "nginx",
            "-v", f"{conf}:/etc/nginx/conf.d/default.conf:ro", *mounts,
            image, "-t",
            check=False,
        )
        assert result.returncode == 0, f"{phase}: {result.stderr}"


_UPSTREAM = """
import http.server

APP_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'nonce-abc'",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=600",
    "X-Content-Type-Options": "nosniff",
}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self):
        length = int(self.headers.get("Content-Length") or 0)
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                break
            remaining -= len(chunk)
        print(f"RECEIVED {self.path} {length - remaining}", flush=True)
        self.send_response(200)
        if self.path.startswith("/app-headers"):
            for name, value in APP_HEADERS.items():
                self.send_header(name, value)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    do_GET = do_POST = _reply

    def log_message(self, *args):
        pass


http.server.ThreadingHTTPServer(("0.0.0.0", 21080), Handler).serve_forever()
"""

_CLIENT = """
import http.client, json, ssl, sys

host, port = sys.argv[1], int(sys.argv[2])


def call(method, path, body=None, headers=None):
    context = ssl._create_unverified_context()
    connection = http.client.HTTPSConnection(host, port, context=context, timeout=60)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        response.read()
        return {"status": response.status, "headers": response.getheaders()}
    except OSError as exc:
        return {"status": None, "error": repr(exc)}
    finally:
        connection.close()


print(json.dumps({
    "app": call(
        "GET", "/app-headers?token=SECRET-TOKEN&page=2",
        headers={"Referer": "https://wiki/reset-password?token=SECRET-REFERER"},
    ),
    "plain": call("GET", "/plain"),
    "small": call("POST", "/upload", body=b"x" * 1024),
    "large": call("POST", "/upload", body=b"x" * 70_000_000),
}))
"""


def _headers(response: dict) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for name, value in response.get("headers", []):
        grouped.setdefault(name.lower(), []).append(value)
    return grouped


APP_POLICY = {
    "content-security-policy": ["default-src 'none'; script-src 'nonce-abc'"],
    "x-frame-options": ["SAMEORIGIN"],
    "referrer-policy": ["no-referrer"],
    "strict-transport-security": ["max-age=600"],
    "x-content-type-options": ["nosniff"],
}


def _security_headers(response: dict) -> dict[str, list[str] | None]:
    grouped = _headers(response)
    return {name: grouped.get(name) for name in APP_POLICY}


@pytest.fixture
def proxy_network():
    name = f"{SMOKE_PREFIX}-{uuid.uuid4().hex[:8]}"
    _docker("network", "create", name)
    started: list[str] = []
    try:
        yield name, started
    finally:
        if started:
            _docker("rm", "-f", "-v", *started, check=False)
        _docker("network", "rm", name, check=False)


def _start_upstream(tmp_path: Path, network: str, started: list[str]) -> str:
    script = tmp_path / "upstream.py"
    script.write_text(_UPSTREAM, encoding="utf-8")
    script.chmod(0o644)
    container = f"{network}-app"
    _docker(
        "run", "-d", "--name", container, "--network", network,
        "--network-alias", "app", "-v", f"{script}:/upstream.py:ro",
        UPSTREAM_IMAGE, "python", "/upstream.py",
    )
    started.append(container)
    return container


LARGE_UPLOAD = 70 * 1000 * 1000


def _upload_sizes(upstream: str) -> list[int]:
    """Body bytes the fake app received for each POST /upload."""

    logs = _docker("logs", upstream).stdout
    return [
        int(line.rsplit(" ", 1)[1])
        for line in logs.splitlines()
        if line.startswith("RECEIVED /upload ")
    ]


def _client(tmp_path: Path, network_args: list[str], host: str) -> dict:
    script = tmp_path / "client.py"
    script.write_text(_CLIENT, encoding="utf-8")
    script.chmod(0o644)
    for _ in range(30):
        result = _docker(
            "run", "--rm", *network_args, "-v", f"{script}:/client.py:ro",
            UPSTREAM_IMAGE, "python", "/client.py", host, "443",
        )
        responses = json.loads(result.stdout)
        if responses["plain"]["status"] is not None:
            return responses
        time.sleep(1)
    raise AssertionError(f"proxy never answered: {responses}")


@DOCKER_SMOKE
@pytest.mark.parametrize("image", CADDY_IMAGES)
def test_caddy_keeps_app_headers_caps_bodies_and_logs_no_tokens(
    tmp_path: Path, proxy_network, image: str
):
    network, started = proxy_network
    upstream = _start_upstream(tmp_path, network, started)
    caddyfile = tmp_path / "Caddyfile"
    shutil.copyfile(INFRA / "Caddyfile.docker.internal", caddyfile)
    caddyfile.chmod(0o644)
    proxy = f"{network}-proxy"
    _docker(
        "run", "-d", "--name", proxy, "--network", network, "--network-alias", "proxy",
        "-e", "DTXWIKI_DOMAIN=proxy",
        "-v", f"{caddyfile}:/etc/caddy/Caddyfile:ro", image,
    )
    started.append(proxy)

    responses = _client(tmp_path, ["--network", network], "proxy")

    # s-1: the app's per-path headers arrive once and unchanged ...
    assert responses["app"]["status"] == 200
    assert _security_headers(responses["app"]) == APP_POLICY
    # ... and the proxy only fills in what a response lacks.
    assert _security_headers(responses["plain"]) == {
        "content-security-policy": [SECURITY_HEADERS["content-security-policy"]],
        "x-frame-options": ["DENY"],
        "referrer-policy": ["strict-origin-when-cross-origin"],
        "strict-transport-security": ["max-age=31536000"],
        "x-content-type-options": ["nosniff"],
    }
    # C5: 64 MB transport cap. The app never receives the 70 MB body; current
    # Caddy answers 413, Caddy 2.6 cuts the stream (502 or a reset).
    assert responses["small"]["status"] == 200
    assert responses["large"]["status"] != 200
    if image == "caddy:2":
        assert responses["large"]["status"] == 413
    sizes = _upload_sizes(upstream)
    assert 1024 in sizes
    assert max(sizes) < LARGE_UPLOAD
    # C6: the access log keeps neither the query token nor the Referer.
    logs = _docker("logs", proxy)
    access = [
        json.loads(line)
        for line in (logs.stdout + logs.stderr).splitlines()
        if '"http.log.access' in line
    ]
    uris = [entry["request"]["uri"] for entry in access]
    assert "/app-headers?page=2" in uris
    assert "SECRET" not in logs.stdout + logs.stderr


@DOCKER_SMOKE
def test_nginx_keeps_app_headers_caps_bodies_and_logs_no_tokens(
    tmp_path: Path, proxy_network
):
    network, started = proxy_network
    upstream = _start_upstream(tmp_path, network, started)
    certs = tmp_path / "letsencrypt"
    _self_signed(certs / "live" / "wiki.example.com", "wiki.example.com")
    certs.chmod(0o755)
    (certs / "live").chmod(0o755)
    step_two = _nginx_step_two(NGINX_EXAMPLE.read_text(encoding="utf-8"))
    configs = {
        "live": step_two,
        # dtxWiki down: nginx answers itself and must add the baseline headers.
        "down": step_two.replace("127.0.0.1:21080", "127.0.0.1:21081"),
    }
    results = {}
    for label, text in configs.items():
        conf = tmp_path / f"{label}.conf"
        conf.write_text(text, encoding="utf-8")
        conf.chmod(0o644)
        proxy = f"{network}-nginx-{label}"
        # Shares the upstream's network namespace: 127.0.0.1:21080 is the app.
        _docker(
            "run", "-d", "--name", proxy, "--network", f"container:{upstream}",
            "-v", f"{conf}:/etc/nginx/conf.d/default.conf:ro",
            "-v", f"{certs}:/etc/letsencrypt:ro", NGINX_IMAGES[-1],
        )
        started.append(proxy)
        results[label] = _client(
            tmp_path, ["--network", f"container:{upstream}"], "127.0.0.1"
        )
        results[label]["log"] = _docker(
            "exec", proxy, "cat", "/var/log/nginx/dtxwiki.access.log"
        ).stdout
        _docker("rm", "-f", "-v", proxy)
        started.remove(proxy)

    live = results["live"]
    assert live["app"]["status"] == 200
    assert _security_headers(live["app"]) == APP_POLICY
    assert live["small"]["status"] == 200
    assert live["large"]["status"] == 413
    assert max(_upload_sizes(upstream)) < LARGE_UPLOAD
    assert '"GET /app-headers HTTP/1.1" 200' in live["log"]
    assert "SECRET" not in live["log"]

    down = results["down"]["plain"]
    assert down["status"] == 503
    assert _security_headers(down)["content-security-policy"] == [
        SECURITY_HEADERS["content-security-policy"]
    ]
    assert _security_headers(down)["x-frame-options"] == ["DENY"]

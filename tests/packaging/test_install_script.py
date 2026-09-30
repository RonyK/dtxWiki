"""Tests for the one-command production installer (infra/prod/install.sh)."""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
import typer.main

from backend.cli.main import app as cli_app

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = REPO_ROOT / "infra" / "prod" / "install.sh"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def test_install_script_exists():
    assert INSTALL_SH.is_file(), "infra/prod/install.sh missing"


def test_install_script_requires_domain_and_email():
    content = INSTALL_SH.read_text(encoding="utf-8")
    assert "--domain" in content
    assert "--email" in content
    assert "ERROR: --domain is required" in content
    assert "ERROR: --email is required" in content


def test_install_script_provisions_then_delegates_to_setup():
    content = INSTALL_SH.read_text(encoding="utf-8")
    # OS provisioning (only doable before the CLI exists)
    assert "python3.12" in content
    assert "caddy" in content
    assert "pip install" in content
    # ...then hands off app configuration to the idempotent CLI
    assert "setup production" in content
    # service user is passed so the JWT key is generated group-readable by it
    assert "--service-user" in content


def test_install_script_is_shipped_in_package_data():
    pyproject = PYPROJECT.read_text(encoding="utf-8")
    assert '"infra/prod/install.sh"' in pyproject


def test_install_script_uses_caddy_reload_or_restart_convention():
    # delegated to setup; the script itself must not hand-roll a bare reload
    content = INSTALL_SH.read_text(encoding="utf-8")
    assert "systemctl reload caddy" not in content


def test_install_fallback_recommends_make_build_wheel_not_raw_build():
    # Codex #22: a raw `python -m build` on a fresh checkout omits the gitignored
    # backend/static SPA, yielding an SPA-less wheel. The fallback hint must point
    # operators at `make build-wheel` (which runs bundle-frontend first).
    content = INSTALL_SH.read_text(encoding="utf-8")
    # The operator-facing build hint must point at `make build-wheel`, not the
    # bare `python -m build` form (matched with its `->` so this does not trip on
    # the explanatory comment that mentions raw `python -m build`).
    assert "make build-wheel  ->" in content
    assert "python -m build  ->" not in content


def test_install_script_drops_untemplated_unit_options():
    # systemd units are static (User=dtxwiki, /opt/dtxwiki); advertising
    # --install-dir/--user without templating produced broken units, so they
    # were removed.
    content = INSTALL_SH.read_text(encoding="utf-8")
    assert "--install-dir" not in content
    assert "--user <name>" not in content


def test_install_script_help_exits_successfully():
    content = INSTALL_SH.read_text(encoding="utf-8")
    assert 'local status="${1:-1}"' in content
    assert "-h|--help) usage 0" in content


def test_install_script_validates_local_wheel_for_service_user():
    content = INSTALL_SH.read_text(encoding="utf-8")
    assert '[[ -f "$WHEEL" ]]' in content
    assert 'sudo -u "$DTXWIKI_USER" test -r "$WHEEL"' in content
    assert "Copy it to /tmp" in content
    assert "one of --wheel or --index-url is required" in content
    assert "--wheel and --index-url are mutually exclusive" in content


def test_install_script_force_reinstalls_local_wheel():
    content = INSTALL_SH.read_text(encoding="utf-8")
    assert '"$PIP" install --force-reinstall "$PIP_WHEEL"' in content


def test_install_script_stages_renamed_wheels_under_a_canonical_filename():
    content = INSTALL_SH.read_text(encoding="utf-8")
    assert 'WHEEL_STAGE_DIR="/usr/local/share/dtxwiki/wheels"' in content
    assert "pip validates wheel filenames" in content
    assert 'WHEEL_BASENAME="$("$PYTHON" - "$WHEEL"' in content
    assert 'install -o root -g root -m 0644 "$WHEEL" "$PIP_WHEEL"' in content


def test_install_script_restores_the_venv_only_until_the_new_code_is_verified():
    content = INSTALL_SH.read_text(encoding="utf-8")
    assert 'PREVIOUS_VENV="${INSTALL_DIR}/.venv.previous.$$"' in content
    assert 'cp -a "$VENV" "$PREVIOUS_VENV"' in content
    assert 'mv "$PREVIOUS_VENV" "$VENV"' in content
    assert "trap 'restore_venv $?' ERR" in content
    verified = content.index('sudo -u "$DTXWIKI_USER" "$DTXWIKI" --version')
    committed = content.index('VENV_MUTATED=""\ntrap - ERR')
    setup = content.index('"$DTXWIKI" setup production "${SETUP_ARGS[@]}"')
    # Once setup may have migrated the database or enabled units pointing at the
    # new venv, swapping the old code back would leave a server that refuses
    # to start; the rollback window closes right after verification.
    assert verified < committed < setup


def test_install_script_leaves_bootstrap_ordering_and_ownership_to_setup():
    # `setup production` bootstraps the database and hands the data directory
    # to the service account BEFORE it enables and restarts the units. The
    # installer must not restart them again afterwards, as it once did after a
    # root-owned bootstrap.
    content = INSTALL_SH.read_text(encoding="utf-8")
    setup = content.index('"$DTXWIKI" setup production "${SETUP_ARGS[@]}"')
    assert "systemctl restart" not in content[setup:]
    assert "systemctl enable" not in content
    assert 'chmod 750 /etc/dtxwiki "$DATA_DIR"' in content


def test_install_script_value_flags_have_friendly_errors():
    content = INSTALL_SH.read_text(encoding="utf-8")
    for flag in (
        "--domain", "--email", "--tls", "--tls-cert", "--tls-key",
        "--forwarded-ip", "--bind-host", "--wheel", "--index-url",
    ):
        assert f'="${{2:?{flag} requires ' in content


def test_install_script_does_not_mix_private_and_public_indexes():
    content = INSTALL_SH.read_text(encoding="utf-8")
    assert 'PIP_INDEX_ARGS=(--index-url "$INDEX_URL")' in content
    assert 'install "${PIP_INDEX_ARGS[@]}" --upgrade pip' in content
    assert "--extra-index-url https://pypi.org/simple" not in content


def test_install_script_installs_sqlite_cli_for_operator_backups():
    content = INSTALL_SH.read_text(encoding="utf-8")
    assert "python3.12-venv sqlite3" in content


def test_install_script_parses_as_bash():
    result = subprocess.run(
        ["bash", "-n", str(INSTALL_SH)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck missing")
def test_install_script_and_its_wrappers_pass_shellcheck(tmp_path: Path):
    wrappers = []
    for name, body in _wrappers().items():
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")
        wrappers.append(str(path))
    result = subprocess.run(
        ["shellcheck", "-x", str(INSTALL_SH), *wrappers],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --------------------------------------------------------------------------- #
# Behaviour: the functions install.sh defines, sourced with fake system tools
# --------------------------------------------------------------------------- #
_FAKES = r"""
apt-get() { echo "apt-get $*" >> "$CALLS"; }
apt-cache() {
    echo "apt-cache $*" >> "$CALLS"
    printf 'caddy:\n  Installed: (none)\n  Candidate: %s\n' "$FAKE_CADDY_CANDIDATE"
}
add-apt-repository() { echo "add-apt-repository $*" >> "$CALLS"; }
curl() { echo "curl $*" >> "$CALLS"; printf 'ARMORED-KEY'; }
gpg() {
    echo "gpg $*" >> "$CALLS"
    local out="${*: -1}"
    { printf 'DEARMORED:'; cat; } > "$out"
}
has_python312() { [[ "$FAKE_HAS_PYTHON312" == 1 ]]; }
"""


def _source_and_run(
    tmp_path: Path, script: str, **env: str
) -> subprocess.CompletedProcess:
    calls = tmp_path / "calls.log"
    calls.write_text("", encoding="utf-8")
    driver = tmp_path / "driver.sh"
    driver.write_text(
        "set -euo pipefail\n"
        f"source {shlex.quote(str(INSTALL_SH))}\n"
        f"{_FAKES}\n"
        f"CADDY_KEYRING={shlex.quote(str(tmp_path / 'caddy.gpg'))}\n"
        f"CADDY_SOURCES={shlex.quote(str(tmp_path / 'caddy.list'))}\n"
        f"{script}\n",
        encoding="utf-8",
    )
    return subprocess.run(
        ["bash", str(driver)],
        env={"PATH": "/usr/bin:/bin", "CALLS": str(calls), **env},
        capture_output=True,
        text=True,
    )


def _calls(tmp_path: Path) -> list[str]:
    return (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()


def test_sourcing_install_sh_only_defines_functions(tmp_path: Path):
    result = _source_and_run(tmp_path, "declare -F install_system_packages")

    assert result.returncode == 0, result.stderr
    assert "==>" not in result.stdout
    assert _calls(tmp_path) == []


def test_ubuntu_2204_gets_caddy_from_the_official_repository(tmp_path: Path):
    """B-9: jammy has no caddy package; deadsnakes alone was not enough."""

    result = _source_and_run(
        tmp_path,
        'TLS=acme; install_system_packages',
        FAKE_CADDY_CANDIDATE="(none)",
        FAKE_HAS_PYTHON312="0",
    )

    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert calls == [
        "apt-get update -y",
        "apt-get install -y software-properties-common",
        "add-apt-repository -y ppa:deadsnakes/ppa",
        "apt-get update -y",
        "apt-cache policy caddy",
        "apt-get install -y ca-certificates curl gnupg",
        "curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key",
        f"gpg --dearmor --yes -o {tmp_path / 'caddy.gpg'}",
        "apt-get update -y",
        "apt-get install -y python3.12 python3.12-venv sqlite3 curl caddy",
    ]
    assert (tmp_path / "caddy.gpg").read_text() == "DEARMORED:ARMORED-KEY"
    # The repository is trusted only through its own keyring.
    assert (tmp_path / "caddy.list").read_text() == (
        f"deb [signed-by={tmp_path / 'caddy.gpg'}] "
        "https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main\n"
    )
    for path in ("caddy.gpg", "caddy.list"):
        assert (tmp_path / path).stat().st_mode & 0o777 == 0o644


def test_distribution_caddy_is_used_when_available(tmp_path: Path):
    result = _source_and_run(
        tmp_path,
        'TLS=internal; install_system_packages',
        FAKE_CADDY_CANDIDATE="2.6.2-6ubuntu0.24.04.3",
        FAKE_HAS_PYTHON312="1",
    )

    assert result.returncode == 0, result.stderr
    assert _calls(tmp_path) == [
        "apt-get update -y",
        "apt-cache policy caddy",
        "apt-get install -y python3.12 python3.12-venv sqlite3 curl caddy",
    ]
    assert not (tmp_path / "caddy.list").exists()


def test_external_tls_installs_no_caddy_at_all(tmp_path: Path):
    result = _source_and_run(
        tmp_path,
        'TLS=external; install_system_packages',
        FAKE_CADDY_CANDIDATE="(none)",
        FAKE_HAS_PYTHON312="1",
    )

    assert result.returncode == 0, result.stderr
    assert _calls(tmp_path) == [
        "apt-get update -y",
        "apt-get install -y python3.12 python3.12-venv sqlite3 curl",
    ]


def test_setup_arguments_carry_every_option(tmp_path: Path):
    result = _source_and_run(
        tmp_path,
        "parse_args --domain wiki.lan --email admin@example.com --tls external "
        "--forwarded-ip 10.0.0.2 --bind-host 0.0.0.0 --wheel /tmp/x.whl\n"
        "setup_production_args\n"
        "printf '%s\\n' \"${SETUP_ARGS[@]}\"",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "--domain", "wiki.lan",
        "--email", "admin@example.com",
        "--service-user", "dtxwiki",
        "--tls", "external",
        "--forwarded-ip", "10.0.0.2",
        "--bind-host", "0.0.0.0",
    ]


# --------------------------------------------------------------------------- #
# Behaviour: the Day-2 PATH wrappers install.sh writes
# --------------------------------------------------------------------------- #
def _wrappers() -> dict[str, str]:
    content = INSTALL_SH.read_text(encoding="utf-8")
    found = dict(
        re.findall(
            r"/dev/stdin /usr/local/bin/(dtxwiki(?:-worker)?) <<'EOF'\n(.*?)\nEOF\n",
            content,
            re.S,
        )
    )
    assert set(found) == {"dtxwiki", "dtxwiki-worker"}
    return {name: body + "\n" for name, body in found.items()}


def _run_wrapper(tmp_path: Path, name: str, *args: str) -> list[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    out = tmp_path / "out.log"
    out.write_text("", encoding="utf-8")
    for fake in ("setpriv", "cli", "worker"):
        script = bin_dir / fake
        script.write_text(
            "#!/bin/sh\n"
            f'echo "{fake} $* [APP_ENV=$DTXWIKI_APP_ENV]" >> "{out}"\n',
            encoding="utf-8",
        )
        script.chmod(0o755)
    prod_env = tmp_path / "dtxwiki.env"
    prod_env.write_text("DTXWIKI_APP_ENV=production\n", encoding="utf-8")
    body = (
        _wrappers()[name]
        .replace("/etc/dtxwiki/dtxwiki.env", str(prod_env))
        .replace("/opt/dtxwiki/.venv/bin/dtxwiki-worker", str(bin_dir / "worker"))
        .replace("/opt/dtxwiki/.venv/bin/dtxwiki", str(bin_dir / "cli"))
    )
    wrapper = tmp_path / name
    wrapper.write_text(body, encoding="utf-8")
    wrapper.chmod(0o755)
    subprocess.run(
        [str(wrapper), *args],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
        check=True,
    )
    return out.read_text(encoding="utf-8").splitlines()


DB_COMMANDS = (
    "serve", "bootstrap", "users", "workspace", "admin", "context", "reindex",
    "eval", "info", "migrate-data",
)
# They write /etc, drive systemctl, or never open the database.
ROOT_COMMANDS = (
    "setup", "config", "keygen", "update", "rollback", "mcp", "db", "embedding",
    "rerank",
)


@pytest.mark.parametrize("command", DB_COMMANDS)
def test_wrapper_runs_database_commands_as_the_service_account(
    tmp_path: Path, command: str
):
    """M-18(c): root-owned SQLite files/WAL sidecars break the service."""

    calls = _run_wrapper(tmp_path, "dtxwiki", command, "--flag")

    cli = tmp_path / "bin" / "cli"
    assert calls == [
        "setpriv --reuid=dtxwiki --regid=dtxwiki --init-groups "
        f"env HOME=/opt/dtxwiki {cli} {command} --flag [APP_ENV=production]"
    ]


@pytest.mark.parametrize("command", ROOT_COMMANDS)
def test_wrapper_keeps_root_for_system_commands(tmp_path: Path, command: str):
    calls = _run_wrapper(tmp_path, "dtxwiki", command, "--flag")

    assert calls == [f"cli {command} --flag [APP_ENV=production]"]


def test_worker_wrapper_always_drops_privileges(tmp_path: Path):
    calls = _run_wrapper(tmp_path, "dtxwiki-worker", "run")

    worker = tmp_path / "bin" / "worker"
    assert calls == [
        "setpriv --reuid=dtxwiki --regid=dtxwiki --init-groups "
        f"env HOME=/opt/dtxwiki {worker} run [APP_ENV=production]"
    ]


def test_wrapper_command_lists_cover_every_cli_command():
    """Every top-level CLI command is classified, so a new one is decided on."""

    click_app = typer.main.get_command(cli_app)
    assert set(click_app.commands) == {*DB_COMMANDS, *ROOT_COMMANDS}

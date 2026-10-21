"""Distribution artifact inclusion tests for Phase_4 operator files."""

import ast
import json
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import zipfile
from pathlib import Path

import pytest

from backend.cli.commands import prod as prod_commands

REPO_ROOT = Path(__file__).resolve().parents[2]


def _tracked_files(directory: str) -> list[str]:
    """Committed files only: an operator's local infra/prod/.env is not one."""

    result = subprocess.run(
        ["git", "ls-files", "-z", "--", directory],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("requires a Git checkout", allow_module_level=True)
    return sorted(filter(None, result.stdout.decode("utf-8").split("\0")))


# Every operator file under infra/prod ships as a data-file: the setup code and
# the documented manual paths read them from share/dtxwiki/infra/prod.
INFRA_FILES = _tracked_files("infra/prod")
# The production deployment runbook now lives at the repo root (discoverable),
# shipped as share/dtxwiki/DEPLOYMENT.md.
ROOT_DOC_FILES = [
    "DEPLOYMENT.md",
]
SMOKE_FILES = [
    "tests/packaging/production_smoke.sh",
    "tests/packaging/production_smoke.ps1",
]
CATALOG_FILES = [
    "backend/catalog/library_family_presets/catalog.v1.json",
    "backend/catalog/mcp_apps/ASSETS.md",
    "backend/catalog/mcp_apps/catalog.v1.json",
    "backend/catalog/mcp_apps/catalog.schema.json",
    "backend/catalog/mcp_apps/icons/antigravity.svg",
    "backend/catalog/mcp_apps/icons/claude-code.svg",
    "backend/catalog/mcp_apps/icons/codex-cli.svg",
    "backend/catalog/mcp_apps/icons/gemini-cli.svg",
    "backend/catalog/mcp_apps/icons/generic.svg",
]
MIGRATION_FILES = sorted(
    path.relative_to(REPO_ROOT).as_posix()
    for path in (REPO_ROOT / "backend" / "migrations" / "versions").rglob("*.py")
)
REVISION_0002_FILES = sorted(
    path.relative_to(REPO_ROOT).as_posix()
    for path in (
        REPO_ROOT
        / "backend"
        / "migrations"
        / "revision_modules/v0_1/efcbcf724889_core_schema"
    ).glob("*.py")
)
# What `dtxwiki setup production` loads through prod_setup.share_file.
SETUP_REFERENCES = sorted(
    {
        *prod_commands._CADDY_TEMPLATE.values(),
        *prod_commands._SYSTEMD_UNITS.values(),
        prod_commands._SMOKE_SCRIPT,
    }
)


def test_infra_file_list_covers_the_templates_setup_needs():
    assert {
        "infra/prod/Caddyfile.internal.example",
        "infra/prod/Caddyfile.custom.example",
        "infra/prod/Caddyfile.insecure.example",
        "infra/prod/Caddyfile.docker.internal",
    } <= set(INFRA_FILES)
    assert set(SETUP_REFERENCES) <= {*INFRA_FILES, *SMOKE_FILES}


def test_setup_loads_packaged_files_only_through_the_listed_constants():
    """A new share_file() call must use a constant SETUP_REFERENCES reads."""

    allowed = {"_CADDY_TEMPLATE", "_SYSTEMD_UNITS", "_SMOKE_SCRIPT"}
    calls = []
    for module in sorted((REPO_ROOT / "backend").rglob("*.py")):
        if "tests" in module.relative_to(REPO_ROOT).parts:
            continue
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", None)
            )
            if name != "share_file":
                continue
            argument = node.args[0]
            base = argument.value if isinstance(argument, ast.Subscript) else argument
            calls.append((module.name, getattr(base, "id", ast.unparse(argument))))
    assert calls, "no share_file() call found; the scan is broken"
    assert {base for _module, base in calls} <= allowed, calls


@pytest.fixture(scope="module")
def built_dist(tmp_path_factory) -> dict[str, Path]:
    pytest.importorskip("build")
    outdir = tmp_path_factory.mktemp("dist")
    generated_paths = [REPO_ROOT / "build", REPO_ROOT / "dtxwiki.egg-info"]
    preexisting_paths = {path for path in generated_paths if path.exists()}
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--sdist",
                "--outdir",
                str(outdir),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        for path in generated_paths:
            if path not in preexisting_paths:
                shutil.rmtree(path, ignore_errors=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return {
        "wheel": next(outdir.glob("*.whl")),
        "sdist": next(outdir.glob("*.tar.gz")),
    }


@pytest.mark.production_smoke
def test_wheel_and_sdist_include_operator_artifacts(tmp_path, built_dist):
    wheel_path = built_dist["wheel"]
    sdist_path = built_dist["sdist"]

    wheel_extract = tmp_path / "wheel-extract"
    with zipfile.ZipFile(wheel_path) as wheel:
        wheel_names = set(wheel.namelist())
        wheel.extractall(wheel_extract)
    for relative_path in [*INFRA_FILES, *ROOT_DOC_FILES, *SMOKE_FILES]:
        assert any(
            name.endswith(f"share/dtxwiki/{relative_path}") for name in wheel_names
        ), f"wheel missing {relative_path}"
    for relative_path in [*CATALOG_FILES, *MIGRATION_FILES, *REVISION_0002_FILES]:
        assert any(name.endswith(relative_path) for name in wheel_names), (
            f"wheel missing {relative_path}"
        )

    packaged_catalog = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                f"sys.path.insert(0, {str(wheel_extract)!r}); "
                "from backend.catalog.mcp_apps import CatalogRepository; "
                "catalog = CatalogRepository().snapshot(); "
                "print(catalog.catalog_revision, "
                "','.join(app.id for app in catalog.apps))"
            ),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert packaged_catalog.returncode == 0, (
        packaged_catalog.stdout + packaged_catalog.stderr
    )
    assert packaged_catalog.stdout.strip() == (
        "2026-09-13.1 claude-code,codex,gemini-cli,antigravity"
    )

    packaged_migrations = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; from pathlib import Path; "
                f"root = Path({str(wheel_extract)!r}); "
                "sys.path.insert(0, str(root)); "
                "from alembic.config import Config; "
                "from alembic.script import ScriptDirectory; "
                "config = Config(); "
                "config.set_main_option('script_location', "
                "str(root / 'backend' / 'migrations')); "
                "config.set_main_option('recursive_version_locations', 'true'); "
                "script = ScriptDirectory.from_config(config); "
                "print(','.join(script.get_heads()))"
            ),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert packaged_migrations.returncode == 0, (
        packaged_migrations.stdout + packaged_migrations.stderr
    )
    assert packaged_migrations.stdout.strip() == "d148b9746ff5"

    with tarfile.open(sdist_path) as sdist:
        sdist_names = set(sdist.getnames())
    for relative_path in [*INFRA_FILES, *ROOT_DOC_FILES, *SMOKE_FILES]:
        assert any(name.endswith(relative_path) for name in sdist_names), (
            f"sdist missing {relative_path}"
        )
    for relative_path in [*CATALOG_FILES, *MIGRATION_FILES, *REVISION_0002_FILES]:
        assert any(name.endswith(relative_path) for name in sdist_names), (
            f"sdist missing {relative_path}"
        )


def _install_wheel_without_dependencies(wheel_path: Path, venv: Path) -> dict:
    """Unpack a wheel into a fresh venv the way an installer lays it out.

    The venv gets no pip and no dependencies: a .pth line borrows them from the
    running environment. A line in a .pth file only extends sys.path, so the
    editable finder of this checkout never activates and `backend` is imported
    from the unpacked wheel alone.
    """

    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True
    )
    python = venv / "bin" / "python"
    paths = json.loads(
        subprocess.run(
            [
                str(python),
                "-c",
                "import json, sysconfig; print(json.dumps(sysconfig.get_paths()))",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    purelib, data = Path(paths["purelib"]), Path(paths["data"])
    with zipfile.ZipFile(wheel_path) as wheel:
        for member in wheel.infolist():
            if member.is_dir():
                continue
            name = member.filename
            head, _, rest = name.partition("/")
            if head.endswith(".data"):
                scheme, _, rest = rest.partition("/")
                if scheme == "data":
                    target = data / rest
                elif scheme in {"purelib", "platlib"}:
                    target = purelib / rest
                else:
                    continue
            else:
                target = purelib / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(wheel.read(member))
    (purelib / "_dependencies_from_test_env.pth").write_text(
        sysconfig.get_paths()["purelib"] + "\n", encoding="utf-8"
    )
    return {"python": python, "purelib": purelib, "data": data}


@pytest.mark.production_smoke
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX venv layout")
def test_setup_templates_resolve_from_an_installed_wheel(tmp_path, built_dist):
    """B-8: every file setup needs comes from the wheel, not a checkout."""

    installed = _install_wheel_without_dependencies(
        built_dist["wheel"], tmp_path / "venv"
    )
    probe = (
        "import json, sys\n"
        "from backend.cli import prod_setup\n"
        "refs = json.loads(sys.argv[1])\n"
        "print(json.dumps({\n"
        "    'module': prod_setup.__file__,\n"
        "    'resolved': {\n"
        "        ref: (str(path) if (path := prod_setup.share_file(ref)) else None)\n"
        "        for ref in refs\n"
        "    },\n"
        "}))\n"
    )
    result = subprocess.run(
        [str(installed["python"]), "-c", probe, json.dumps(SETUP_REFERENCES)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)

    assert Path(report["module"]).is_relative_to(installed["purelib"])
    share = installed["data"] / "share" / "dtxwiki"
    for ref in SETUP_REFERENCES:
        resolved = report["resolved"][ref]
        assert resolved == str(share / ref), f"{ref} does not resolve: {resolved}"
        assert Path(resolved).read_bytes() == (REPO_ROOT / ref).read_bytes()


@pytest.mark.production_smoke
def test_sdist_never_ships_local_env_files_databases_or_backups(tmp_path):
    pytest.importorskip("build")
    source = tmp_path / "source"
    for relative in _tracked_files("."):
        origin = REPO_ROOT / relative
        if origin.is_file():
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, target)
    planted = [
        "infra/prod/.env",
        "infra/prod/.env.local",
        "infra/prod/dtxwiki.env",
        "infra/prod/backups/dtxwiki-2026-01-01.tgz",
        "infra/prod/dtxwiki.sqlite",
        "infra/prod/dtxwiki.sqlite-wal",
        "backend/migrations/.env",
    ]
    for relative in planted:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("DTXWIKI_SESSION_SECRET=live-secret\n", encoding="utf-8")
    outdir = tmp_path / "dist"

    result = subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--outdir", str(outdir)],
        cwd=source,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    with tarfile.open(next(outdir.glob("*.tar.gz"))) as sdist:
        names = {name.split("/", 1)[1] for name in sdist.getnames() if "/" in name}
    assert sorted(set(planted) & names) == []
    assert {"infra/prod/.env.example", "infra/prod/.env.docker.example"} <= names

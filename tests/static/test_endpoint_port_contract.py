from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_local_compose_uses_sprint3_postgres_host_port_env():
    compose = (ROOT / "infra/local/docker-compose.yml").read_text(encoding="utf-8")

    assert '"127.0.0.1:${DTXWIKI_POSTGRES_HOST_PORT:-21432}:5432"' in compose
    assert "POSTGRES_PORT" not in compose


def test_local_env_example_documents_endpoint_port_envs():
    env_example = (ROOT / "infra/local/.env.example").read_text(encoding="utf-8")

    assert "DTXWIKI_BACKEND_PORT=21080" in env_example
    assert "DTXWIKI_FRONTEND_DEV_PORT=21173" in env_example
    assert "DTXWIKI_POSTGRES_HOST_PORT=21432" in env_example
    assert "postgresql+pg8000://" in env_example
    assert "localhost:21432/dtxwiki" in env_example
    assert "postgresql+psycopg://" not in env_example
    assert "POSTGRES_PORT" not in env_example
    assert "DTXWIKI_REDIS_" not in env_example
    assert "DTXWIKI_MINIO_" not in env_example
    assert "MINIO_" not in env_example


def test_runtime_dependencies_use_pg8000_instead_of_psycopg():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lockfile = (ROOT / "uv.lock").read_text(encoding="utf-8")

    assert '"pg8000>=1.31"' in pyproject
    assert "psycopg" not in pyproject.lower()
    assert 'name = "pg8000"' in lockfile
    assert 'name = "psycopg"' not in lockfile
    assert 'name = "psycopg-binary"' not in lockfile


def test_active_postgres_samples_use_pg8000_driver():
    checked_paths = [
        ROOT / "alembic.ini",
        ROOT / "infra/local/.env.example",
        ROOT / "README.md",
    ]
    offenders = []
    for path in checked_paths:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if "postgresql+psycopg" in text or "psycopg[binary]" in text:
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_local_compose_is_postgres_only_dev_db():
    compose = (ROOT / "infra/local/docker-compose.yml").read_text(encoding="utf-8")
    doc = yaml.safe_load(compose)

    assert set(doc["services"]) == {"postgres"}
    assert set(doc["volumes"]) == {"dtxwiki-postgres"}
    assert "redis" not in compose.lower()
    assert "minio" not in compose.lower()
    assert "DTXWIKI_OBJECT_STORE_" not in compose
    assert "DTXWIKI_JOB_QUEUE_" not in compose


def test_frontend_and_playwright_configs_use_sprint3_ports():
    vite_config = (ROOT / "frontend/vite.config.ts").read_text(encoding="utf-8")
    playwright_config = (ROOT / "playwright.config.ts").read_text(encoding="utf-8")
    api_client = (ROOT / "frontend/src/shared/api/client.ts").read_text(
        encoding="utf-8"
    )

    assert "DEFAULT_FRONTEND_DEV_PORT = 21173" in vite_config
    assert "process.env.DTXWIKI_FRONTEND_DEV_PORT" in vite_config
    assert 'process.env.DTXWIKI_BACKEND_PORT ?? "21080"' in playwright_config
    assert 'process.env.DTXWIKI_FRONTEND_DEV_PORT ?? "21173"' in playwright_config
    assert 'process.platform === "win32"' in playwright_config
    assert "DTXWIKI_DATABASE_URL:" in playwright_config
    assert "DTXWIKI_CORS_ORIGINS:" in playwright_config
    assert ".venv/bin/python" not in playwright_config
    assert '"http://127.0.0.1:21080"' in api_client


def test_local_compose_binds_all_published_ports_to_loopback():
    compose_path = ROOT / "infra/local/docker-compose.yml"
    doc = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    offenders = []
    for service_name, service in doc.get("services", {}).items():
        for entry in service.get("ports", []) or []:
            if isinstance(entry, str) and not entry.startswith("127.0.0.1:"):
                offenders.append((service_name, entry))
            elif isinstance(entry, dict) and entry.get("host_ip") != "127.0.0.1":
                offenders.append((service_name, entry))

    assert offenders == [], (
        "All compose ports must bind 127.0.0.1; LAN-exposed entries: "
        f"{offenders}"
    )


def test_e2e_files_do_not_hardcode_default_endpoints():
    e2e_dir = ROOT / "tests/e2e"
    forbidden = ("127.0.0.1:21080", "/tmp/dtxwiki", "C:/tmp/dtxwiki", "/tmp")
    offenders = []
    for path in e2e_dir.rglob("*"):
        if not path.is_file() or path.suffix not in {".ts", ".js", ".mjs", ".py"}:
            continue
        text = path.read_text(encoding="utf-8")
        for needle in forbidden:
            if needle in text:
                offenders.append(f"{path.relative_to(ROOT)}: {needle}")
    assert offenders == [], "\n".join(offenders)


def test_e2e_files_do_not_hardcode_db_filename():
    """The e2e DB path must flow from run-e2e.mjs via env vars."""
    e2e_dir = ROOT / "tests/e2e"
    forbidden = ("dtxwiki-p0-0-e2e.db",)
    offenders = []
    for path in e2e_dir.rglob("*"):
        if not path.is_file() or path.suffix not in {".ts", ".js", ".mjs", ".py"}:
            continue
        text = path.read_text(encoding="utf-8")
        for needle in forbidden:
            if needle in text:
                offenders.append(f"{path.relative_to(ROOT)}: {needle}")
    assert offenders == [], "\n".join(offenders)


def test_run_e2e_mjs_uses_mkdtemp_for_exclusive_dir():
    text = (ROOT / "tests/e2e/run-e2e.mjs").read_text(encoding="utf-8")

    assert "mkdtempSync" in text
    assert "dtxwiki-e2e-" in text


def test_prepare_backend_db_has_no_sqlite_url_fallback():
    source = (ROOT / "tests/e2e/prepare_backend_db.py").read_text(encoding="utf-8")

    assert "sqlite+pysqlite:///" not in source, (
        "prepare_backend_db.py must not synthesize a SQLite URL; "
        "run-e2e.mjs is the single source of truth for DTXWIKI_E2E_DATABASE_URL."
    )


def test_playwright_e2e_serves_the_spa_from_the_backend_origin():
    """E2E must run the packaged single-origin shape, not a Vite dev server.

    Two origins gave every page two Chromium socket pools, so host network
    churn surfaced as ERR_NETWORK_CHANGED failures in unrelated specs.
    """
    playwright_config = (ROOT / "playwright.config.ts").read_text(encoding="utf-8")

    assert "npm --workspace frontend run dev" not in playwright_config
    assert "baseURL: backendBaseUrl" in playwright_config
    # Not a substring grep: `// DTXWIKI_STATIC_ROOT: e2eStaticRoot,` satisfies
    # one, and deleting that single line is the mutation that made
    # single_origin.spec.ts silently serve a stale checked-out
    # backend/static/ bundle instead of the one the run just built.
    static_root_wiring = [
        line
        for line in playwright_config.splitlines()
        if "DTXWIKI_STATIC_ROOT:" in line and not line.lstrip().startswith(("//", "*"))
    ]
    assert len(static_root_wiring) == 1, (
        "expected exactly one live DTXWIKI_STATIC_ROOT assignment in "
        f"playwright.config.ts, found {static_root_wiring}"
    )
    assert "e2eStaticRoot" in static_root_wiring[0]
    assert "${frontendDevPort}" not in playwright_config, (
        "The dev port must not reach the browser: no CORS entry, no webServer "
        "url, no baseURL derived from it."
    )


def test_run_e2e_builds_a_production_spa_into_the_run_temp_dir():
    """The bundle the browser loads is built per run, inside the mkdtemp root.

    Building elsewhere would leak past the finally-block cleanup, and an
    inherited VITE_API_BASE_URL would bake a second origin into the bundle.
    """
    text = (ROOT / "tests/e2e/run-e2e.mjs").read_text(encoding="utf-8")

    assert 'path.join(e2eDir, "spa")' in text
    assert '"vite",' in text
    assert '"build",' in text
    assert '"--emptyOutDir",' in text
    assert 'process.env.VITE_API_BASE_URL = "";' in text
    assert "DTXWIKI_E2E_STATIC_ROOT" in text

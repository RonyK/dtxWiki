from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_public_makefile_keeps_registry_push_out_of_tracked_contract():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "192.168." not in makefile
    assert "DOCKER_REGISTRY" not in makefile
    assert "docker-push" not in makefile
    assert "docker-build-push" not in makefile
    assert "DOCKER_IMAGE ?= dtxwiki:$(DOCKER_TAG)" in makefile
    assert "-include Makefile.local" in makefile


def test_private_makefile_overlay_is_ignored_and_documented():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    readme_dev = (ROOT / "DEVELOPMENT.md").read_text(encoding="utf-8")

    assert "Makefile.local" in gitignore
    assert "Makefile.local" in readme_dev
    assert "registry.example.internal" in readme_dev
    assert "192.168." not in readme_dev


def test_local_dev_docs_use_postgres_only_compose_targets():
    checked_paths = [
        ROOT / "README.md",
        ROOT / "DEVELOPMENT.md",
        ROOT / "infra/local/.env.example",
        ROOT / "infra/local/docker-compose.yml",
    ]

    offenders = []
    for path in checked_paths:
        text = path.read_text(encoding="utf-8")
        for forbidden in (
            "make up",
            "make down",
            "PostgreSQL/Redis/MinIO",
            "DTXWIKI_REDIS_",
            "DTXWIKI_MINIO_",
            "MINIO_ROOT",
        ):
            if forbidden in text:
                offenders.append(f"{path.relative_to(ROOT)}: {forbidden}")

    assert offenders == []


def test_backend_target_runs_sqlite_media_lane_sequentially():
    """The known SQLite media race must not return to the xdist lane unnoticed."""

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert (
        "SQLITE_MEDIA_SERIAL_TESTS := "
        "backend/tests/integration/test_media_blob_gc.py "
        "backend/tests/integration/test_media_install_workflow.py"
    ) in makefile
    assert "SQLITE_MEDIA_PARALLEL_IGNORES := $(foreach test," in makefile
    assert (
        "pytest backend/tests -q -n $(PYTEST_WORKERS) --dist loadgroup "
        "$(SQLITE_MEDIA_PARALLEL_IGNORES)"
    ) in makefile
    assert "pytest $(SQLITE_MEDIA_SERIAL_TESTS) -q" in makefile

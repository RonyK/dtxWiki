from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import pytest

from scripts import deploy_static_bundle


def test_index_is_swapped_only_after_referenced_assets_exist(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "dist"
    destination = tmp_path / "static"
    _write_bundle(source, entry_hash="new")
    _write_bundle(destination, entry_hash="old")
    old_asset = destination / "assets" / "index-old.js"
    original_swap = deploy_static_bundle._swap_index
    swap_observations = []

    def observed_swap(source_index: Path, destination_index: Path) -> None:
        swap_observations.append(
            {
                "old_index_still_live": "index-old.js"
                in destination_index.read_text(encoding="utf-8"),
                "new_script_exists": (
                    destination / "assets" / "index-new.js"
                ).is_file(),
                "new_stylesheet_exists": (
                    destination / "assets" / "index-new.css"
                ).is_file(),
            }
        )
        original_swap(source_index, destination_index)

    monkeypatch.setattr(deploy_static_bundle, "_swap_index", observed_swap)

    deploy_static_bundle.deploy_bundle(source, destination)

    assert swap_observations == [
        {
            "old_index_still_live": True,
            "new_script_exists": True,
            "new_stylesheet_exists": True,
        }
    ]
    assert "index-new.js" in (destination / "index.html").read_text(
        encoding="utf-8"
    )
    assert old_asset.is_file()


def test_missing_index_entry_does_not_replace_live_index(tmp_path):
    source = tmp_path / "dist"
    destination = tmp_path / "static"
    _write_bundle(source, entry_hash="new")
    _write_bundle(destination, entry_hash="old")
    (source / "assets" / "index-new.js").unlink()

    with pytest.raises(FileNotFoundError, match="index-new.js"):
        deploy_static_bundle.deploy_bundle(source, destination)

    assert "index-old.js" in (destination / "index.html").read_text(
        encoding="utf-8"
    )


def test_prune_keeps_latest_manifest_and_removes_expired_assets(tmp_path):
    source = tmp_path / "dist"
    destination = tmp_path / "static"
    _write_bundle(source, entry_hash="old")
    deploy_static_bundle.deploy_bundle(source, destination)

    source = tmp_path / "next-dist"
    _write_bundle(source, entry_hash="new")
    deploy_static_bundle.deploy_bundle(source, destination)

    future = datetime.now(UTC) + timedelta(days=10)
    deleted = deploy_static_bundle.prune_bundle(
        destination,
        retain_days=7,
        retain_manifests=1,
        now=future,
    )

    assert "assets/index-old.js" in deleted["assets"]
    assert "assets/index-old.css" in deleted["assets"]
    assert (destination / "assets" / "index-new.js").is_file()
    assert (destination / "assets" / "index-new.css").is_file()
    assert len(deleted["manifests"]) == 1


def test_prune_waits_for_in_progress_deploy_before_inspecting_manifests(
    tmp_path,
    monkeypatch,
):
    destination = tmp_path / "static"
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_bundle(first, entry_hash="old")
    deploy_static_bundle.deploy_bundle(first, destination)
    _write_bundle(second, entry_hash="new")

    copied_new_entry = Event()
    resume_deploy = Event()
    original_atomic_copy = deploy_static_bundle._atomic_copy

    def paused_atomic_copy(source_file: Path, destination_file: Path) -> None:
        original_atomic_copy(source_file, destination_file)
        if destination_file.name == "index-new.js":
            copied_new_entry.set()
            assert resume_deploy.wait(timeout=5)

    monkeypatch.setattr(deploy_static_bundle, "_atomic_copy", paused_atomic_copy)

    with ThreadPoolExecutor(max_workers=2) as executor:
        deploy_future = executor.submit(
            deploy_static_bundle.deploy_bundle,
            second,
            destination,
        )
        assert copied_new_entry.wait(timeout=5)
        prune_future = executor.submit(
            deploy_static_bundle.prune_bundle,
            destination,
            retain_days=7,
            retain_manifests=1,
            now=datetime.now(UTC) + timedelta(days=10),
        )
        try:
            with pytest.raises(TimeoutError):
                prune_future.result(timeout=0.2)
        finally:
            resume_deploy.set()
        deploy_future.result(timeout=5)
        prune_future.result(timeout=5)

    assert "index-new.js" in (destination / "index.html").read_text(
        encoding="utf-8"
    )
    assert (destination / "assets" / "index-new.js").is_file()
    assert (destination / "assets" / "index-new.css").is_file()


def test_prune_refuses_when_no_manifest_matches_live_index(tmp_path):
    destination = tmp_path / "static"
    _write_bundle(destination, entry_hash="legacy")

    with pytest.raises(RuntimeError, match="safe deploy"):
        deploy_static_bundle.prune_bundle(destination)

    assert (destination / "assets" / "index-legacy.js").is_file()


def test_first_safe_deploy_starts_retention_for_legacy_assets(tmp_path):
    source = tmp_path / "dist"
    destination = tmp_path / "static"
    _write_bundle(source, entry_hash="new")
    _write_bundle(destination, entry_hash="legacy")

    deploy_static_bundle.deploy_bundle(source, destination)

    manifest_directory = (
        destination / deploy_static_bundle.MANIFEST_DIRECTORY
    )
    manifests = [
        path.read_text(encoding="utf-8")
        for path in manifest_directory.glob("*.json")
    ]
    assert len(manifests) == 2
    assert any("assets/index-legacy.js" in manifest for manifest in manifests)
    assert any("assets/index-new.js" in manifest for manifest in manifests)


def _write_bundle(root: Path, *, entry_hash: str) -> None:
    assets = root / "assets"
    assets.mkdir(parents=True)
    (assets / f"index-{entry_hash}.js").write_text(
        "console.log('bundle');",
        encoding="utf-8",
    )
    (assets / f"index-{entry_hash}.css").write_text(
        "body {}",
        encoding="utf-8",
    )
    (root / "index.html").write_text(
        (
            '<link rel="stylesheet" '
            f'href="/assets/index-{entry_hash}.css">'
            '<script type="module" '
            f'src="/assets/index-{entry_hash}.js"></script>'
        ),
        encoding="utf-8",
    )


def test_fresh_deploy_drops_assets_no_longer_referenced(tmp_path):
    """A wheel must carry one build, not the union of every build so far."""

    destination = tmp_path / "static"
    _write_bundle(tmp_path / "first", entry_hash="aaaa")
    deploy_static_bundle.deploy_bundle(tmp_path / "first", destination)
    assert (destination / "assets" / "index-aaaa.js").is_file()

    _write_bundle(tmp_path / "second", entry_hash="bbbb")
    deploy_static_bundle.deploy_bundle(
        tmp_path / "second", destination, fresh=True
    )

    assert (destination / "assets" / "index-bbbb.js").is_file()
    assert not (destination / "assets" / "index-aaaa.js").exists()


def test_merging_deploy_keeps_assets_an_open_tab_may_still_request(tmp_path):
    destination = tmp_path / "static"
    _write_bundle(tmp_path / "first", entry_hash="aaaa")
    deploy_static_bundle.deploy_bundle(tmp_path / "first", destination)

    _write_bundle(tmp_path / "second", entry_hash="bbbb")
    deploy_static_bundle.deploy_bundle(tmp_path / "second", destination)

    assert (destination / "assets" / "index-bbbb.js").is_file()
    assert (destination / "assets" / "index-aaaa.js").is_file()

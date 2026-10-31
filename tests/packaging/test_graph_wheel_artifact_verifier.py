"""Graph assets and explicit release qualification without changing backend fixtures.

Archive, Worker URL, metadata and schema-boundary checks have distinct failure
paths. Metadata cases use extracted inputs directly; only archive/CLI cases
need packaged backend payloads. The existing backend fixture remains generic.
"""

import importlib.util
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIER = REPO_ROOT / "scripts" / "verify_wheel_artifact.py"
_spec = importlib.util.spec_from_file_location(
    "_backend_artifact_fixtures",
    Path(__file__).with_name("test_wheel_artifact_verifier.py"),
)
_backend = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_backend)
CURRENT_HEAD = _backend.CURRENT_HEAD
WHEEL_NAME = "dtxwiki-0.2.1-py3-none-any.whl"
SDIST_NAME = "dtxwiki-0.2.1.tar.gz"
_verifier_module = _backend._verifier_module
_write_wheel = _backend._write_wheel


def _stage(tmp_path, name="stage"):
    # Extend the backend fixture locally: no cross-PR fixture rewrites.
    stage = _backend._stage(tmp_path, name)
    assets = stage / "backend/static/assets"
    (assets / "index-abc123.js").write_text(
        'export const openMap=()=>import("./cosmosAdapter-abc123.js");\n',
        encoding="utf-8",
    )
    (assets / "cosmosAdapter-abc123.js").write_text(
        "export const createCollisionWorker=()=>new Worker(new URL("
        '"/assets/forceAtlas2Collision.worker-abc123.js",import.meta.url),'
        '{type:"module",name:"workspace-fa2-collision"});\n',
        encoding="utf-8",
    )
    (assets / "forceAtlas2Collision.worker-abc123.js").write_text(
        "self.onmessage = () => {};\n", encoding="utf-8"
    )
    return stage


def _write_sdist(target, stage):
    with tarfile.open(target, "w:gz", compresslevel=1) as archive:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                arcname = Path(target.name.removesuffix(".tar.gz")) / path.relative_to(
                    stage
                )
                archive.add(path, arcname.as_posix())
    return target


def _run_verifier(argument, *options):
    return subprocess.run(
        [sys.executable, str(VERIFIER), str(argument), *options],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _dist_with_one_artifact_mutated(tmp_path, artifact_kind, mutate):
    def mutate_one(stage, kind):
        if kind == artifact_kind:
            mutate(stage)

    return _release_dist(tmp_path, mutate=mutate_one)


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
@pytest.mark.parametrize(
    "relative",
    [
        "backend/migrations/versions/v0_2/9aeab6944f25_add_graph_view.py",
        "backend/services/workspace_graph_service.py",
        "backend/utils/markdown_links.py",
    ],
)
def test_verifier_rejects_missing_graph_payload(tmp_path, artifact_kind, relative):
    dist = _dist_with_one_artifact_mutated(
        tmp_path, artifact_kind, lambda stage: (stage / relative).unlink()
    )

    result = _run_verifier(dist)

    assert result.returncode == 1
    artifact = WHEEL_NAME if artifact_kind == "wheel" else SDIST_NAME
    assert f"{artifact}: {relative} is missing" in result.stdout + result.stderr


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
@pytest.mark.parametrize("damage", ["missing", "empty"])
def test_verifier_rejects_a_missing_2k_collision_worker(
    tmp_path, artifact_kind, damage
):
    def damage_worker(stage):
        worker = stage / "backend/static/assets/forceAtlas2Collision.worker-abc123.js"
        if damage == "missing":
            worker.unlink()
        else:
            worker.write_bytes(b"")

    dist = _dist_with_one_artifact_mutated(tmp_path, artifact_kind, damage_worker)

    result = _run_verifier(dist, "--release-version", "0.2.1")

    assert result.returncode == 1, result.stdout + result.stderr
    artifact = WHEEL_NAME if artifact_kind == "wheel" else SDIST_NAME
    assert f"{artifact}: backend/static/assets/forceAtlas2Collision.worker" in (
        result.stdout + result.stderr
    )


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
def test_verifier_rejects_a_stale_2k_worker_instead_of_the_referenced_hash(
    tmp_path, artifact_kind
):
    def keep_only_stale_worker(stage):
        assets = stage / "backend/static/assets"
        (assets / "forceAtlas2Collision.worker-abc123.js").rename(
            assets / "forceAtlas2Collision.worker-old456.js"
        )

    result = _run_verifier(
        _dist_with_one_artifact_mutated(
            tmp_path, artifact_kind, keep_only_stale_worker
        ),
        "--release-version",
        "0.2.1",
    )

    assert result.returncode == 1, result.stdout + result.stderr
    artifact = WHEEL_NAME if artifact_kind == "wheel" else SDIST_NAME
    assert (
        f"{artifact}: backend/static/assets/cosmosAdapter-abc123.js references "
        "backend/static/assets/forceAtlas2Collision.worker-abc123.js"
    ) in result.stdout + result.stderr


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
def test_verifier_rejects_a_worker_file_without_an_actual_bundle_reference(
    tmp_path, artifact_kind
):
    def remove_reference(stage):
        (stage / "backend/static/assets/cosmosAdapter-abc123.js").write_text(
            "export {};\n", encoding="utf-8"
        )

    result = _run_verifier(
        _dist_with_one_artifact_mutated(tmp_path, artifact_kind, remove_reference),
        "--release-version",
        "0.2.1",
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "JavaScript has no 2K collision Worker URL reference" in (
        result.stdout + result.stderr
    )


@pytest.mark.parametrize("quote", ['"', "'"])
def test_verifier_resolves_the_vite_worker_url_literal(tmp_path, quote):
    stage = _stage(tmp_path)
    adapter = stage / "backend/static/assets/cosmosAdapter-abc123.js"
    adapter.write_text(
        "new Worker(new URL( "
        f"{quote}/assets/forceAtlas2Collision.worker-abc123.js{quote}"
        ', import.meta.url ), {type:"module",name:"workspace-fa2-collision"});',
        encoding="utf-8",
    )

    failures = _verifier_module()._check_2k_worker_references(stage / "backend/static")
    assert failures == []


def _release_metadata_path(stage, artifact_kind, version):
    if artifact_kind == "wheel":
        return stage / f"dtxwiki-{version}.dist-info/METADATA"
    return stage / "PKG-INFO"


def _write_release_metadata(stage, artifact_kind, version):
    metadata = _release_metadata_path(stage, artifact_kind, version)
    metadata.parent.mkdir(parents=True, exist_ok=True)
    metadata.write_text(
        f"Metadata-Version: 2.4\nName: dtxwiki\nVersion: {version}\n\n",
        encoding="utf-8",
    )
    if artifact_kind == "sdist":
        (stage / "pyproject.toml").write_text(
            f'[project]\nname = "dtxwiki"\nversion = "{version}"\n',
            encoding="utf-8",
        )
    return metadata


def _append_schema_boundary(stage, version="0.2.1", **overrides):
    path = stage / "backend/migrations/release_manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_releases"][version] = {
        "group": "v0_2",
        "head": CURRENT_HEAD,
        **overrides,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _retain_historical_schema_boundaries(stage):
    # Do not let a later real release silently change what these fixtures are
    # proving: preserve all shipped mappings and append future Graph boundaries
    # explicitly. The candidate versions below are fixture data only.
    path = stage / "backend/migrations/release_manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_releases"] = {
        version: payload["schema_releases"][version]
        for version in ("0.0.1", "0.1.1", "0.1.2", "0.1.3", "0.1.4")
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _release_schema_stage(tmp_path, artifact_kind="wheel", version="0.2.1"):
    """Only the extracted inputs owned by the strict metadata/schema check."""

    stage = tmp_path / "release-stage"
    migrations = stage / "backend/migrations"
    migrations.mkdir(parents=True)
    shutil.copy(
        REPO_ROOT / "backend/migrations/release_manifest.json",
        migrations / "release_manifest.json",
    )
    shutil.copytree(REPO_ROOT / "backend/migrations/versions", migrations / "versions")
    _retain_historical_schema_boundaries(stage)
    _write_release_metadata(stage, artifact_kind, version)
    suffix = "-py3-none-any.whl" if artifact_kind == "wheel" else ".tar.gz"
    return stage, tmp_path / f"dtxwiki-{version}{suffix}"


def _release_dist(tmp_path, version="0.2.1", *, boundary="0.2.1", mutate=None):
    dist = tmp_path / "release-dist"
    dist.mkdir()
    for artifact_kind in ("wheel", "sdist"):
        stage = _stage(tmp_path, name=f"release-{artifact_kind}")
        _retain_historical_schema_boundaries(stage)
        _write_release_metadata(stage, artifact_kind, version)
        if boundary is not None:
            _append_schema_boundary(stage, boundary)
        if mutate is not None:
            mutate(stage, artifact_kind)
        if artifact_kind == "wheel":
            _write_wheel(dist / f"dtxwiki-{version}-py3-none-any.whl", stage)
        else:
            _write_sdist(dist / f"dtxwiki-{version}.tar.gz", stage)
    return dist


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
@pytest.mark.parametrize(
    "version,boundary,head",
    [
        ("0.2.1", "0.2.1", CURRENT_HEAD),
        ("0.2.2", "0.2.1", CURRENT_HEAD),
        ("0.1.2", None, "efcbcf724889"),
    ],
    ids=["explicit-graph-boundary", "patch-inherits-graph", "historical-patch"],
)
def test_release_schema_accepts_the_artifacts_applicable_boundary(
    tmp_path, artifact_kind, version, boundary, head
):
    stage, artifact = _release_schema_stage(tmp_path, artifact_kind, version)
    if boundary is not None:
        _append_schema_boundary(stage, boundary)

    assert (
        _verifier_module()._check_release_schema(stage, artifact, head, version) == []
    )


@pytest.mark.counterexample(
    reason="artifact metadata must reject missing, ambiguous or wrong release identity"
)
@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
@pytest.mark.parametrize(
    "damage",
    [
        "missing-file",
        "missing-version",
        "duplicate-version",
        "wrong-version",
        "wrong-name",
        "duplicate-name",
    ],
)
def test_release_schema_rejects_invalid_artifact_metadata(
    tmp_path, artifact_kind, damage
):
    stage, artifact = _release_schema_stage(tmp_path, artifact_kind)
    _append_schema_boundary(stage)
    metadata = _release_metadata_path(stage, artifact_kind, "0.2.1")
    if damage == "missing-file":
        metadata.unlink()
    else:
        text = metadata.read_text(encoding="utf-8")
        if damage == "missing-version":
            text = text.replace("Version: 0.2.1\n", "")
        elif damage == "duplicate-version":
            text = text.replace("Version: 0.2.1\n", "Version: 0.2.1\nVersion: 0.2.1\n")
        elif damage == "wrong-version":
            text = text.replace("Version: 0.2.1\n", "Version: 0.1.2\n")
        elif damage == "wrong-name":
            text = text.replace("Name: dtxwiki\n", "Name: another-project\n")
        else:
            text = text.replace("Name: dtxwiki\n", "Name: dtxwiki\nName: dtxwiki\n")
        metadata.write_text(text, encoding="utf-8")

    failures = _verifier_module()._check_release_schema(
        stage, artifact, CURRENT_HEAD, "0.2.1"
    )

    assert failures, damage
    assert "metadata" in " ".join(failures).lower(), failures


@pytest.mark.parametrize(
    "damage",
    ["wrong-distribution", "wrong-version", "second-metadata", "second-empty-dir"],
)
def test_release_schema_rejects_invalid_wheel_metadata_directories(tmp_path, damage):
    stage, artifact = _release_schema_stage(tmp_path)
    _append_schema_boundary(stage)
    metadata = _release_metadata_path(stage, "wheel", "0.2.1")
    if damage == "wrong-distribution":
        metadata.parent.rename(stage / "another-0.2.1.dist-info")
    elif damage == "wrong-version":
        metadata.parent.rename(stage / "dtxwiki-0.1.2.dist-info")
    else:
        extra = stage / "another-9.9.9.dist-info"
        extra.mkdir()
        if damage == "second-metadata":
            shutil.copy(metadata, extra / "METADATA")

    failures = _verifier_module()._check_release_schema(
        stage, artifact, CURRENT_HEAD, "0.2.1"
    )

    assert failures, damage
    assert "metadata" in " ".join(failures).lower(), failures


def test_release_schema_rejects_sdist_with_stale_rebuild_metadata(tmp_path):
    stage, artifact = _release_schema_stage(tmp_path, "sdist")
    _append_schema_boundary(stage)
    (stage / "pyproject.toml").write_text(
        '[project]\nname = "dtxwiki"\nversion = "0.1.2"\n', encoding="utf-8"
    )

    failures = _verifier_module()._check_release_schema(
        stage, artifact, CURRENT_HEAD, "0.2.1"
    )

    assert failures
    assert "pyproject" in " ".join(failures).lower(), failures


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
@pytest.mark.parametrize(
    "distribution,version", [("another", "0.2.1"), ("dtxwiki", "0.1.2")]
)
def test_release_schema_rejects_a_filename_for_a_different_release(
    tmp_path, artifact_kind, distribution, version
):
    stage, artifact = _release_schema_stage(tmp_path, artifact_kind)
    _append_schema_boundary(stage)
    suffix = "-py3-none-any.whl" if artifact_kind == "wheel" else ".tar.gz"
    artifact = artifact.with_name(f"{distribution}-{version}{suffix}")

    failures = _verifier_module()._check_release_schema(
        stage, artifact, CURRENT_HEAD, "0.2.1"
    )

    assert failures
    assert "filename" in " ".join(failures).lower(), failures


@pytest.mark.parametrize(
    "boundary,overrides",
    [
        ("not-a-version", {}),
        ("0.1", {}),
        ("00.2.1", {}),
        ("1!0.2.1", {}),
        ("0.2.1+local", {}),
        ("0.2.1", {"group": "v0_1"}),
        ("0.2.1", {"head": "missing_revision"}),
        ("0.2.1", {"head": "bd62193d5cf6"}),
    ],
    ids=[
        "invalid-version",
        "short-version",
        "noncanonical-version",
        "epoch",
        "local",
        "wrong-group",
        "unknown-head",
        "head-in-wrong-group",
    ],
)
def test_release_schema_rejects_invalid_boundaries(tmp_path, boundary, overrides):
    stage, artifact = _release_schema_stage(tmp_path)
    _append_schema_boundary(stage, boundary, **overrides)

    failures = _verifier_module()._check_release_schema(
        stage, artifact, CURRENT_HEAD, "0.2.1"
    )

    assert failures, (boundary, overrides)
    assert any(
        "schema" in failure.lower() or "release_manifest" in failure
        for failure in failures
    )


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
@pytest.mark.parametrize("future_boundary", [None, "0.2.2"])
def test_release_schema_does_not_launder_graph_under_an_older_release(
    tmp_path, artifact_kind, future_boundary
):
    stage, artifact = _release_schema_stage(tmp_path, artifact_kind, "0.1.2")
    if future_boundary is not None:
        _append_schema_boundary(stage, future_boundary)

    failures = _verifier_module()._check_release_schema(
        stage, artifact, CURRENT_HEAD, "0.1.2"
    )

    assert failures
    message = " ".join(failures)
    assert "0.1.2" in message and "efcbcf724889" in message, failures
    assert CURRENT_HEAD in message, failures


def test_release_schema_requires_the_resolved_artifact_head(tmp_path):
    stage, artifact = _release_schema_stage(tmp_path)
    _append_schema_boundary(stage)

    failures = _verifier_module()._check_release_schema(stage, artifact, None, "0.2.1")

    assert failures
    assert "head" in " ".join(failures).lower(), failures


def test_release_cli_uses_the_extracted_mapping_not_the_checkout(tmp_path):
    # A source-root fallback must not turn either artifact's own mapping into
    # this deliberately invalid checkout mapping. Copy the shipped verifier,
    # not its implementation, so the counterexample survives future releases.
    checkout = tmp_path / "unrelated-checkout"
    scripts = checkout / "scripts"
    scripts.mkdir(parents=True)
    for name in (VERIFIER.name, "deploy_static_bundle.py"):
        shutil.copy(VERIFIER.parent / name, scripts / name)
    manifest = checkout / "backend/migrations/release_manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"schema_releases": {}}', encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(scripts / VERIFIER.name),
            str(_release_dist(tmp_path)),
            "--release-version",
            "0.2.1",
        ],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "dtxwiki-0.2.1-py3-none-any.whl:" in result.stdout
    assert "dtxwiki-0.2.1.tar.gz:" in result.stdout


def test_release_cli_rejects_current_graph_without_a_new_schema_boundary(tmp_path):
    dist = _release_dist(tmp_path, "0.1.2", boundary=None)

    result = _run_verifier(dist, "--release-version", "0.1.2")

    assert result.returncode == 1, result.stdout + result.stderr
    for name in ("dtxwiki-0.1.2-py3-none-any.whl", "dtxwiki-0.1.2.tar.gz"):
        assert f"{name}:" in result.stderr
    assert "efcbcf724889" in result.stderr and CURRENT_HEAD in result.stderr


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
def test_release_cli_checks_metadata_in_both_uploaded_artifacts(
    tmp_path, artifact_kind
):
    def corrupt_one(stage, kind):
        if kind == artifact_kind:
            metadata = _release_metadata_path(stage, kind, "0.2.1")
            metadata.write_text(
                "Metadata-Version: 2.4\nName: dtxwiki\nVersion: 0.1.2\n\n",
                encoding="utf-8",
            )

    result = _run_verifier(
        _release_dist(tmp_path, mutate=corrupt_one), "--release-version", "0.2.1"
    )

    assert result.returncode == 1, result.stdout + result.stderr
    suffix = "-py3-none-any.whl" if artifact_kind == "wheel" else ".tar.gz"
    assert f"dtxwiki-0.2.1{suffix}:" in result.stderr
    assert "metadata" in result.stderr.lower()

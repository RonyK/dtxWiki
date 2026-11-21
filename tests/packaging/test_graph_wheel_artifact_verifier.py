"""Graph assets and explicit release qualification without changing backend fixtures.

Archive, Worker URL, metadata and schema-boundary checks have distinct failure
paths. Metadata cases use extracted inputs directly; only archive/CLI cases
need packaged backend payloads. The existing backend fixture remains generic.
"""

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from backend.schemas.common import MCP_RELEASED_TOOL_CONTRACT_DIGEST
from scripts import release_version

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIER = REPO_ROOT / "scripts" / "verify_wheel_artifact.py"
TOOL_CONTRACT = Path("backend/schemas/common.py")
_spec = importlib.util.spec_from_file_location(
    "_backend_artifact_fixtures",
    Path(__file__).with_name("test_wheel_artifact_verifier.py"),
)
_backend = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_backend)
# The release cases qualify the shipped 0.3.1 tree, not dev work since then.
RELEASE_HEAD = "c3d2e4f601ab"
WHEEL_NAME = "dtxwiki-0.3.1-py3-none-any.whl"
SDIST_NAME = "dtxwiki-0.3.1.tar.gz"
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
        "export const createD3Worker=()=>new Worker(new URL("
        '"/assets/d3Layout.worker-abc123.js",import.meta.url),'
        '{type:"module",name:"shared-d3-layout"});\n',
        encoding="utf-8",
    )
    (assets / "d3Layout.worker-abc123.js").write_text(
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
        "backend/migrations/versions/v0_2/5b7e5c149279_release_0_2.py",
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
def test_verifier_rejects_a_missing_2k_d3_worker(tmp_path, artifact_kind, damage):
    def damage_worker(stage):
        worker = stage / "backend/static/assets/d3Layout.worker-abc123.js"
        if damage == "missing":
            worker.unlink()
        else:
            worker.write_bytes(b"")

    dist = _dist_with_one_artifact_mutated(tmp_path, artifact_kind, damage_worker)

    result = _run_verifier(dist, "--release-version", "0.3.1")

    assert result.returncode == 1, result.stdout + result.stderr
    artifact = WHEEL_NAME if artifact_kind == "wheel" else SDIST_NAME
    assert f"{artifact}: backend/static/assets/d3Layout.worker" in (
        result.stdout + result.stderr
    )


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
def test_verifier_rejects_a_stale_2k_worker_instead_of_the_referenced_hash(
    tmp_path, artifact_kind
):
    def keep_only_stale_worker(stage):
        assets = stage / "backend/static/assets"
        (assets / "d3Layout.worker-abc123.js").rename(
            assets / "d3Layout.worker-old456.js"
        )

    result = _run_verifier(
        _dist_with_one_artifact_mutated(
            tmp_path, artifact_kind, keep_only_stale_worker
        ),
        "--release-version",
        "0.3.1",
    )

    assert result.returncode == 1, result.stdout + result.stderr
    artifact = WHEEL_NAME if artifact_kind == "wheel" else SDIST_NAME
    assert (
        f"{artifact}: backend/static/assets/cosmosAdapter-abc123.js references "
        "backend/static/assets/d3Layout.worker-abc123.js"
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
        "0.3.1",
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "JavaScript has no 2K D3 Worker URL reference" in (
        result.stdout + result.stderr
    )


@pytest.mark.parametrize("quote", ['"', "'"])
def test_verifier_resolves_the_vite_worker_url_literal(tmp_path, quote):
    stage = _stage(tmp_path)
    adapter = stage / "backend/static/assets/cosmosAdapter-abc123.js"
    adapter.write_text(
        "new Worker(new URL( "
        f"{quote}/assets/d3Layout.worker-abc123.js{quote}"
        ', import.meta.url ), {type:"module",name:"shared-d3-layout"});',
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


def _replace_schema_boundary(stage, replacement=None, **overrides):
    path = stage / "backend/migrations/release_manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    entry = payload["schema_releases"].pop("0.3.1")
    if replacement is not None:
        payload["schema_releases"][replacement] = {**entry, **overrides}
    path.write_text(json.dumps(payload), encoding="utf-8")


def _drop_unreleased(stage):
    """Remove development revisions and helpers added after the release.

    Development keeps new revisions in unreleased until a release promotes
    them, so a published 0.3.1 artifact contains none of them.
    """
    for tree in ("versions", "revision_modules"):
        shutil.rmtree(
            stage / "backend/migrations" / tree / "unreleased", ignore_errors=True
        )


def _record_staged_tool_contract(stage):
    path = stage / TOOL_CONTRACT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        release_version._released_tool_contract_text(
            REPO_ROOT, MCP_RELEASED_TOOL_CONTRACT_DIGEST
        ),
        encoding="utf-8",
    )


def _release_schema_stage(tmp_path, artifact_kind="wheel", version="0.3.1"):
    """Only the extracted inputs owned by the strict metadata/schema check."""

    stage = tmp_path / "release-stage"
    migrations = stage / "backend/migrations"
    migrations.mkdir(parents=True)
    shutil.copy(
        REPO_ROOT / "backend/migrations/release_manifest.json",
        migrations / "release_manifest.json",
    )
    shutil.copytree(
        REPO_ROOT / "backend/migrations/versions",
        migrations / "versions",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    _drop_unreleased(stage)
    _write_release_metadata(stage, artifact_kind, version)
    _record_staged_tool_contract(stage)
    suffix = "-py3-none-any.whl" if artifact_kind == "wheel" else ".tar.gz"
    return stage, tmp_path / f"dtxwiki-{version}{suffix}"


def _release_dist(tmp_path, version="0.3.1", *, mutate=None):
    dist = tmp_path / "release-dist"
    dist.mkdir()
    for artifact_kind in ("wheel", "sdist"):
        stage = _stage(tmp_path, name=f"release-{artifact_kind}")
        _drop_unreleased(stage)
        _write_release_metadata(stage, artifact_kind, version)
        _record_staged_tool_contract(stage)
        if mutate is not None:
            mutate(stage, artifact_kind)
        if artifact_kind == "wheel":
            _write_wheel(dist / f"dtxwiki-{version}-py3-none-any.whl", stage)
        else:
            _write_sdist(dist / f"dtxwiki-{version}.tar.gz", stage)
    return dist


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
@pytest.mark.parametrize(
    "version,head",
    [
        ("0.3.1", RELEASE_HEAD),
        ("0.3.2", RELEASE_HEAD),
        ("0.1.2", "efcbcf724889"),
    ],
    ids=["explicit-boundary", "patch-inherits-boundary", "historical-patch"],
)
def test_release_schema_accepts_the_artifacts_applicable_boundary(
    tmp_path, artifact_kind, version, head
):
    stage, artifact = _release_schema_stage(tmp_path, artifact_kind, version)

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
    metadata = _release_metadata_path(stage, artifact_kind, "0.3.1")
    if damage == "missing-file":
        metadata.unlink()
    else:
        text = metadata.read_text(encoding="utf-8")
        if damage == "missing-version":
            text = text.replace("Version: 0.3.1\n", "")
        elif damage == "duplicate-version":
            text = text.replace("Version: 0.3.1\n", "Version: 0.3.1\nVersion: 0.3.1\n")
        elif damage == "wrong-version":
            text = text.replace("Version: 0.3.1\n", "Version: 0.1.2\n")
        elif damage == "wrong-name":
            text = text.replace("Name: dtxwiki\n", "Name: another-project\n")
        else:
            text = text.replace("Name: dtxwiki\n", "Name: dtxwiki\nName: dtxwiki\n")
        metadata.write_text(text, encoding="utf-8")

    failures = _verifier_module()._check_release_schema(
        stage, artifact, RELEASE_HEAD, "0.3.1"
    )

    assert failures, damage
    assert "metadata" in " ".join(failures).lower(), failures


@pytest.mark.parametrize(
    "damage",
    ["wrong-distribution", "wrong-version", "second-metadata", "second-empty-dir"],
)
def test_release_schema_rejects_invalid_wheel_metadata_directories(tmp_path, damage):
    stage, artifact = _release_schema_stage(tmp_path)
    metadata = _release_metadata_path(stage, "wheel", "0.3.1")
    if damage == "wrong-distribution":
        metadata.parent.rename(stage / "another-0.3.1.dist-info")
    elif damage == "wrong-version":
        metadata.parent.rename(stage / "dtxwiki-0.1.2.dist-info")
    else:
        extra = stage / "another-9.9.9.dist-info"
        extra.mkdir()
        if damage == "second-metadata":
            shutil.copy(metadata, extra / "METADATA")

    failures = _verifier_module()._check_release_schema(
        stage, artifact, RELEASE_HEAD, "0.3.1"
    )

    assert failures, damage
    assert "metadata" in " ".join(failures).lower(), failures


def test_release_schema_rejects_sdist_with_stale_rebuild_metadata(tmp_path):
    stage, artifact = _release_schema_stage(tmp_path, "sdist")
    (stage / "pyproject.toml").write_text(
        '[project]\nname = "dtxwiki"\nversion = "0.1.2"\n', encoding="utf-8"
    )

    failures = _verifier_module()._check_release_schema(
        stage, artifact, RELEASE_HEAD, "0.3.1"
    )

    assert failures
    assert "pyproject" in " ".join(failures).lower(), failures


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
@pytest.mark.parametrize(
    "distribution,version", [("another", "0.3.1"), ("dtxwiki", "0.1.2")]
)
def test_release_schema_rejects_a_filename_for_a_different_release(
    tmp_path, artifact_kind, distribution, version
):
    stage, artifact = _release_schema_stage(tmp_path, artifact_kind)
    suffix = "-py3-none-any.whl" if artifact_kind == "wheel" else ".tar.gz"
    artifact = artifact.with_name(f"{distribution}-{version}{suffix}")

    failures = _verifier_module()._check_release_schema(
        stage, artifact, RELEASE_HEAD, "0.3.1"
    )

    assert failures
    assert "filename" in " ".join(failures).lower(), failures


@pytest.mark.parametrize(
    "boundary,overrides",
    [
        ("not-a-version", {}),
        # A valid v0_1 group and head leave only the length check to fail.
        ("0.1", {"group": "v0_1", "head": "efcbcf724889"}),
        ("00.3.1", {}),
        ("1!0.3.1", {}),
        ("0.3.1+local", {}),
        ("0.3.1", {"group": "v0_1"}),
        ("0.3.1", {"head": "missing_revision"}),
        ("0.3.1", {"head": "bd62193d5cf6"}),
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
    # Keep the valid 0.3.1 boundary unless it is the damaged entry, so only
    # the mapping validation can produce the failure.
    path = stage / "backend/migrations/release_manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload["schema_releases"]
    entries[boundary] = {**entries["0.3.1"], **overrides}
    path.write_text(json.dumps(payload), encoding="utf-8")

    failures = _verifier_module()._check_release_schema(
        stage, artifact, RELEASE_HEAD, "0.3.1"
    )

    assert any(
        failure.startswith("invalid release_manifest schema mapping")
        for failure in failures
    ), (boundary, overrides, failures)


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
@pytest.mark.parametrize("future_boundary", [None, "0.3.2"])
def test_release_schema_does_not_launder_graph_under_an_older_release(
    tmp_path, artifact_kind, future_boundary
):
    stage, artifact = _release_schema_stage(tmp_path, artifact_kind, "0.1.2")
    _replace_schema_boundary(stage, future_boundary)

    failures = _verifier_module()._check_release_schema(
        stage, artifact, RELEASE_HEAD, "0.1.2"
    )

    assert failures
    message = " ".join(failures)
    assert "0.1.2" in message and "efcbcf724889" in message, failures
    assert RELEASE_HEAD in message, failures


def test_release_schema_requires_the_resolved_artifact_head(tmp_path):
    stage, artifact = _release_schema_stage(tmp_path)

    failures = _verifier_module()._check_release_schema(stage, artifact, None, "0.3.1")

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
            str(_release_dist(tmp_path, "0.3.2")),
            "--release-version",
            "0.3.2",
        ],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "dtxwiki-0.3.2-py3-none-any.whl:" in result.stdout
    assert "dtxwiki-0.3.2.tar.gz:" in result.stdout


def test_release_cli_rejects_current_graph_without_a_new_schema_boundary(tmp_path):
    dist = _release_dist(tmp_path, "0.1.2")

    result = _run_verifier(dist, "--release-version", "0.1.2")

    assert result.returncode == 1, result.stdout + result.stderr
    for name in ("dtxwiki-0.1.2-py3-none-any.whl", "dtxwiki-0.1.2.tar.gz"):
        assert f"{name}:" in result.stderr
    assert "efcbcf724889" in result.stderr and RELEASE_HEAD in result.stderr


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
def test_release_cli_checks_metadata_in_both_uploaded_artifacts(
    tmp_path, artifact_kind
):
    version = "0.3.1"
    migration = Path("backend/migrations/versions/unreleased/0003_stray.py")
    helper = Path("backend/migrations/revision_modules/unreleased/new_schema.py")
    helper_init = helper.with_name("__init__.py")

    def corrupt_one(stage, kind):
        if kind == artifact_kind:
            metadata = _release_metadata_path(stage, kind, version)
            metadata.write_text(
                "Metadata-Version: 2.4\nName: dtxwiki\nVersion: 0.1.2\n\n",
                encoding="utf-8",
            )
            _backend._write_revision(stage, "0003_stray", RELEASE_HEAD)
            (stage / migration).parent.mkdir(exist_ok=True)
            (stage / migration.parent.parent / "0003_stray.py").rename(
                stage / migration
            )
            (stage / helper).parent.mkdir(parents=True)
            (stage / helper).write_text("def upgrade():\n    pass\n", encoding="utf-8")
            (stage / helper_init).touch()
            contract = stage / TOOL_CONTRACT
            contract.write_text(
                re.sub(
                    r"^MCP_RELEASED_TOOL_CONTRACT_REVISION = \d+$",
                    "MCP_RELEASED_TOOL_CONTRACT_REVISION = 0",
                    contract.read_text(encoding="utf-8"),
                    flags=re.MULTILINE,
                ),
                encoding="utf-8",
            )

    dist = _release_dist(tmp_path, version, mutate=corrupt_one)
    result = _run_verifier(dist, "--release-version", version)

    assert result.returncode == 1, result.stdout + result.stderr
    suffix = "-py3-none-any.whl" if artifact_kind == "wheel" else ".tar.gz"
    assert f"dtxwiki-{version}{suffix}:" in result.stderr
    assert "metadata" in result.stderr.lower()
    assert f"unreleased migration file: {migration.as_posix()}" in result.stderr
    assert f"unreleased migration file: {helper.as_posix()}" in result.stderr
    assert "unreleased MCP tool contract revision" in result.stderr
    assert f"make set-version VERSION={version}" in result.stderr

    # Keep the existing wheel/sdist cases: removing the source must not hide
    # helper-only leftovers, including the helper scaffold's empty initializer.
    stage = tmp_path / f"release-{artifact_kind}"
    _write_release_metadata(stage, artifact_kind, version)
    _record_staged_tool_contract(stage)
    (stage / migration).unlink()
    (stage / helper).unlink()
    write_archive = _write_wheel if artifact_kind == "wheel" else _write_sdist
    artifact = dist / f"dtxwiki-{version}{suffix}"
    write_archive(artifact, stage)

    result = _run_verifier(dist, "--release-version", version)

    assert result.returncode == 1, result.stdout + result.stderr
    assert f"unreleased migration file: {helper_init.as_posix()}" in result.stderr
    assert migration.as_posix() not in result.stderr
    assert "metadata" not in result.stderr.lower()

    (stage / helper_init).unlink()
    write_archive(artifact, stage)

    result = _run_verifier(dist, "--release-version", version)

    assert result.returncode == 0, result.stdout + result.stderr

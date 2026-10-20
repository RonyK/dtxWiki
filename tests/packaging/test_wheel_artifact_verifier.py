"""Release-time artifact gate tests for scripts/verify_wheel_artifact.py."""

import importlib.util
import json
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIER = REPO_ROOT / "scripts" / "verify_wheel_artifact.py"
REVISION_DIRECTORY = (
    REPO_ROOT
    / "backend"
    / "migrations"
    / "revision_modules/v0_1/efcbcf724889_core_schema"
)
REVISION_MODULES = len(
    [path for path in REVISION_DIRECTORY.glob("*.py") if path.name != "__init__.py"]
)
WHEEL_NAME = "dtxwiki-0.0.0-py3-none-any.whl"
SDIST_NAME = "dtxwiki-0.0.0.tar.gz"
INDEX_HTML = (
    "<!doctype html><html><head>"
    '<link rel="stylesheet" href="/assets/index-abc123.css">'
    '</head><body><div id="root"></div>'
    '<script type="module" src="/assets/index-abc123.js"></script>'
    "</body></html>"
)

LICENSE_TEXTS = ("LICENSE", "LICENSE-AGPL-3.0", "LICENSE-ELv2", "NOTICE")
THIRD_PARTY_NOTICES = (
    "Third-party software bundled with the dtxWiki web UI\n\n"
    "- pdfjs-dist@5.4.296: Apache-2.0\n\n"
    "--- LICENSE ---\n\nApache License\nVersion 2.0, January 2004\n"
)


def _stage(tmp_path, name="stage"):
    """Mirror the whole packaged backend, not only its migrations.

    Both `uv sync` and `make install` register dtxwiki as an editable install,
    and setuptools' finder maps any `backend.<child>` the extracted tree lacks
    back to this checkout. A partial staging tree would therefore import the
    repository's modules and prove nothing about the artifact.
    """

    stage = tmp_path / name
    shutil.copytree(
        REPO_ROOT / "backend",
        stage / "backend",
        ignore=shutil.ignore_patterns("__pycache__", "tests", "static"),
    )
    assets = stage / "backend" / "static" / "assets"
    assets.mkdir(parents=True)
    (assets.parent / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (assets / "index-abc123.js").write_text("export {};\n", encoding="utf-8")
    (assets / "index-abc123.css").write_text(":root{}\n", encoding="utf-8")
    (assets / "pdf.worker.min-abc123.mjs").write_text("export {};\n", encoding="utf-8")
    (assets.parent / "THIRD_PARTY_NOTICES.txt").write_text(
        THIRD_PARTY_NOTICES, encoding="utf-8"
    )
    for name in LICENSE_TEXTS:
        (stage / name).write_text(f"{name}\n", encoding="utf-8")
    return stage


def _write_wheel(target, stage):
    with zipfile.ZipFile(target, "w") as archive:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(stage).as_posix())
    return target


def _write_sdist(target, stage):
    # `python -m build` nests the sdist payload under `<name>-<version>/`; the
    # verifier has to strip that prefix, so the fixture has to produce it.
    # compresslevel=1: the gate reads this back through transparent
    # decompression, and the fixture is rebuilt once per test.
    with tarfile.open(target, "w:gz", compresslevel=1) as archive:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                arcname = Path("dtxwiki-0.0.0") / path.relative_to(stage)
                archive.add(path, arcname.as_posix())
    return target


def _dist(tmp_path, *, mutate=None, sdist_mutate=None, extra=()):
    """Build the `dist/` tree the release job uploads: one wheel, one sdist.

    `mutate` damages both artifacts; `sdist_mutate` damages only the sdist,
    which is the case the old wheel-only gate could not see.
    """

    stage = _stage(tmp_path)
    if mutate is not None:
        mutate(stage)
    dist = tmp_path / "dist"
    dist.mkdir()
    _write_wheel(dist / WHEEL_NAME, stage)
    if sdist_mutate is None:
        _write_sdist(dist / SDIST_NAME, stage)
    else:
        sdist_stage = _stage(tmp_path, name="sdist-stage")
        sdist_mutate(sdist_stage)
        _write_sdist(dist / SDIST_NAME, sdist_stage)
    for name in extra:
        (dist / name).write_bytes(b"")
    return dist


def _run_verifier(argument):
    # cwd is deliberately the repository root, the directory the release job
    # runs from: it is where an in-process check would silently verify the
    # source tree instead of the artifact.
    return subprocess.run(
        [sys.executable, str(VERIFIER), str(argument)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _verifier_module():
    """Load the shipped verifier so this file pins the released code, not a copy."""

    spec = importlib.util.spec_from_file_location("_verifier_under_test", VERIFIER)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(VERIFIER.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(VERIFIER.parent))
    return module


def _graph_probe_source():
    return _verifier_module()._GRAPH_PROBE


# Parametrized from the gate's own list so the two cannot drift.
RETIRED_MODULE_PATHS = tuple(
    path.as_posix() for path in _verifier_module().RETIRED_MODULES
)


def _write_revision(stage, revision, down_revision):
    (stage / "backend" / "migrations" / "versions" / f"{revision}.py").write_text(
        f'revision = "{revision}"\n'
        f"down_revision = {down_revision!r}\n"
        "branch_labels = None\n"
        "depends_on = None\n"
        "def upgrade() -> None:\n    pass\n"
        "def downgrade() -> None:\n    pass\n",
        encoding="utf-8",
    )


def _write_stray_revision(stage):
    _write_revision(stage, "0003_stray", "19db79d3e622")


def _write_branch_revision(stage):
    """A second head: a revision that forks off the released base."""

    _write_revision(stage, "0003_branch", "bd62193d5cf6")


def _write_merge_revision(stage):
    _write_revision(stage, "0003_one", "efcbcf724889")
    _write_revision(stage, "0003_two", "efcbcf724889")
    _write_revision(stage, "0004_merge", ("0003_one", "0003_two"))


def _detach_core_schema_from_the_baseline(stage):
    """Leave both revision files in place but break the edge between them.

    The versions/ file-name check cannot see this; only resolving the graph out
    of the extracted artifact can.
    """

    revision = (
        stage
        / "backend"
        / "migrations"
        / "versions"
        / "v0_1/efcbcf724889_core_schema.py"
    )
    source = revision.read_text(encoding="utf-8")
    revision.write_text(
        source.replace('down_revision = "bd62193d5cf6"', "down_revision = None"),
        encoding="utf-8",
    )


def _dist_with_one_artifact_mutated(tmp_path, artifact_kind, mutate):
    if artifact_kind == "wheel":
        return _dist(tmp_path, mutate=mutate, sdist_mutate=lambda stage: None)
    return _dist(tmp_path, sdist_mutate=mutate)


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
@pytest.mark.parametrize("relative", RETIRED_MODULE_PATHS)
def test_verifier_rejects_retired_modules(tmp_path, artifact_kind, relative):
    def add_retired_module(stage):
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def main():\n    pass\n", encoding="utf-8")

    dist = _dist_with_one_artifact_mutated(tmp_path, artifact_kind, add_retired_module)

    result = _run_verifier(dist)

    assert result.returncode == 1
    artifact = WHEEL_NAME if artifact_kind == "wheel" else SDIST_NAME
    assert f"{artifact}: {relative} is a retired module" in (
        result.stdout + result.stderr
    )


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
def test_verifier_rejects_retired_mcp_console_script(tmp_path, artifact_kind):
    def add_retired_console_script(stage):
        if artifact_kind == "wheel":
            metadata = stage / "dtxwiki-0.0.0.dist-info" / "entry_points.txt"
            source = "[console_scripts]\ndtxwiki-mcp = backend.mcp.entry:main\n"
        else:
            metadata = stage / "pyproject.toml"
            source = (
                "[project]\n"
                'name = "dtxwiki"\n'
                'version = "0.0.0"\n'
                "[project.scripts]\n"
                'dtxwiki-mcp = "backend.mcp.entry:main"\n'
            )
        metadata.parent.mkdir(parents=True, exist_ok=True)
        metadata.write_text(source, encoding="utf-8")

    dist = _dist_with_one_artifact_mutated(
        tmp_path, artifact_kind, add_retired_console_script
    )

    result = _run_verifier(dist)

    assert result.returncode == 1
    artifact = WHEEL_NAME if artifact_kind == "wheel" else SDIST_NAME
    assert f"{artifact}:" in result.stdout + result.stderr
    assert "defines the retired dtxwiki-mcp console script" in (
        result.stdout + result.stderr
    )


def test_verifier_accepts_a_complete_dist(tmp_path):
    result = _run_verifier(_dist(tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr
    # The success line is the evidence a human reads off a release run, so it
    # is pinned whole -- the revision-module count included. The core schema
    # ships nine revision-owned domain modules plus unique_key_prepass.py, the
    # shared helper that publishes a reciprocal FK pair's parent unique key in
    # the form each dialect needs. efcbcf724889 reaches every one of them, so a
    # count that drifts is a release-contract change, not a cosmetic one.
    assert REVISION_MODULES == 10
    for name in (WHEEL_NAME, SDIST_NAME):
        assert (
            f"{name}: alembic head 19db79d3e622, "
            f"{REVISION_MODULES} packaged core revision modules, "
            "15 frozen migration files intact, "
            "frontend bundle intact"
        ) in result.stdout


def test_verifier_rejects_a_wheel_missing_a_0002_revision_module(tmp_path):
    dist = _dist(
        tmp_path,
        mutate=lambda stage: (
            stage
            / "backend"
            / "migrations"
            / "revision_modules/v0_1/efcbcf724889_core_schema"
            / "library_media_schema.py"
        ).unlink(),
    )

    result = _run_verifier(dist)

    assert result.returncode == 1
    assert "library_media_schema" in result.stdout + result.stderr


def test_verifier_rejects_an_artifact_that_imports_backend_it_does_not_carry(
    tmp_path,
):
    """A dropped `backend/models` must not be papered over by the checkout.

    efcbcf724889 imports backend.models.base through its revision modules, so the
    editable install's finder can resolve it from the repository and hand the
    release a green head for an artifact that cannot import its own migration.
    """

    dist = _dist(
        tmp_path, mutate=lambda stage: shutil.rmtree(stage / "backend" / "models")
    )

    result = _run_verifier(dist)

    assert result.returncode == 1
    assert "backend.models" in result.stdout + result.stderr


def test_the_foreign_scan_not_the_probe_cwd_is_what_isolates_the_extract(tmp_path):
    """Pin the mechanism `_check_migration_graph`'s comment names.

    Run the shipped probe from the repository root -- the worst cwd there is,
    and the one the release job itself runs the verifier from -- against a tree
    whose backend/models was dropped. The Alembic head still resolves green,
    because the editable install's finder is a sys.meta_path fallback that
    answers `backend.models` out of the checkout no matter where the probe runs
    or what precedes '' on sys.path. Only the `foreign` scan sees the leak, so
    deleting it reinstates the false green that cwd never prevented.
    """

    stage = _stage(tmp_path)
    shutil.rmtree(stage / "backend" / "models")

    probe = subprocess.run(
        [sys.executable, "-c", _graph_probe_source(), str(stage)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert probe.returncode == 0, probe.stdout + probe.stderr
    resolved = json.loads(probe.stdout)
    assert resolved["heads"] == ["19db79d3e622"], "cwd bought no isolation at all"
    leaked = {
        name: origin
        for name, origin in resolved["foreign"].items()
        if name.startswith("backend.models")
    }
    assert leaked, resolved["foreign"]
    assert all(Path(origin).is_relative_to(REPO_ROOT) for origin in leaked.values()), (
        leaked
    )


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (
            _write_branch_revision,
            "packaged Alembic heads are ['0003_branch', '19db79d3e622'], expected "
            "exactly one",
        ),
        (
            _write_merge_revision,
            "packaged revision 0004_merge descends from ['0003_one', '0003_two']",
        ),
        (
            lambda stage: (
                stage
                / "backend"
                / "migrations"
                / "versions"
                / "v0_1/efcbcf724889_core_schema.py"
            ).unlink(),
            "backend/migrations/versions/v0_1/efcbcf724889_core_schema.py is missing",
        ),
        (_detach_core_schema_from_the_baseline, "it must start with"),
    ],
    ids=["second-head", "merge", "missing-core-schema", "detached-baseline"],
)
def test_verifier_rejects_an_artifact_whose_chain_is_not_linear_from_core_schema(
    tmp_path, mutate, expected
):
    # Each expectation is one check's own wording: the frozen-file check, the
    # head count and the chain shape all quote the same revision ids, so a
    # bare id would leave the others shadowed rather than pinned.
    result = _run_verifier(_dist(tmp_path, mutate=mutate))

    assert result.returncode == 1
    assert expected in result.stdout + result.stderr


def test_verifier_accepts_a_linear_revision_after_the_released_chain(tmp_path):
    """C11: the gate no longer forces the next schema change into 0002."""

    result = _run_verifier(_dist(tmp_path, mutate=_write_stray_revision))

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"{WHEEL_NAME}: alembic head 0003_stray, " in result.stdout


def _append_to(relative):
    def mutate(stage):
        path = stage / "backend" / "migrations" / relative
        path.write_text(
            path.read_text(encoding="utf-8") + "\n# edited after release\n",
            encoding="utf-8",
        )

    return mutate


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
@pytest.mark.parametrize(
    "relative",
    [
        "versions/v0_0/bd62193d5cf6_initial_schema.py",
        "versions/v0_1/efcbcf724889_core_schema.py",
        "revision_modules/v0_1/efcbcf724889_core_schema/search_schema.py",
        "revision_modules/v0_1/efcbcf724889_core_schema/__init__.py",
    ],
)
def test_verifier_rejects_an_edited_released_migration(
    tmp_path, artifact_kind, relative
):
    dist = _dist_with_one_artifact_mutated(
        tmp_path, artifact_kind, _append_to(relative)
    )

    result = _run_verifier(dist)

    assert result.returncode == 1
    artifact = WHEEL_NAME if artifact_kind == "wheel" else SDIST_NAME
    assert (
        f"{artifact}: backend/migrations/{relative} changed after its release"
        in result.stdout + result.stderr
    )


def test_verifier_rejects_a_module_added_to_the_frozen_revision(tmp_path):
    def add_module(stage):
        (
            stage
            / "backend"
            / "migrations"
            / "revision_modules/v0_1/efcbcf724889_core_schema"
            / "late.py"
        ).write_text("def upgrade() -> None:\n    pass\n", encoding="utf-8")

    result = _run_verifier(_dist(tmp_path, mutate=add_module))

    assert result.returncode == 1
    assert (
        "backend/migrations/revision_modules/v0_1/"
        "efcbcf724889_core_schema/late.py is not part of the frozen "
        "canonical baseline revision"
    ) in result.stdout + result.stderr


def _rehash_manifest(stage):
    """The edit-and-rehash change the second copy in the verifier exists for."""

    manifest = stage / "backend" / "migrations" / "frozen_revisions.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["sha256"]["versions/v0_1/efcbcf724889_core_schema.py"] = "0" * 64
    manifest.write_text(json.dumps(payload), encoding="utf-8")


def test_verifier_rejects_rewritten_historical_release_mapping(tmp_path):
    def rewrite(stage):
        path = stage / "backend/migrations/release_manifest.json"
        payload = json.loads(path.read_text())
        payload["schema_releases"]["0.1.1"]["head"] = "bd62193d5cf6"
        path.write_text(json.dumps(payload))

    result = _run_verifier(_dist(tmp_path, mutate=rewrite))
    assert result.returncode == 1
    assert "release_manifest.json is missing or invalid" in result.stderr


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (
            lambda stage: (
                stage / "backend" / "migrations" / "frozen_revisions.json"
            ).unlink(),
            "backend/migrations/frozen_revisions.json is missing or unreadable",
        ),
        (
            _rehash_manifest,
            "backend/migrations/frozen_revisions.json does not record the frozen "
            "canonical baseline migration digests",
        ),
    ],
    ids=["missing", "rehashed"],
)
def test_verifier_rejects_a_missing_or_rewritten_freeze_manifest(
    tmp_path, mutate, expected
):
    result = _run_verifier(_dist(tmp_path, mutate=mutate))

    assert result.returncode == 1
    assert expected in result.stdout + result.stderr


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
@pytest.mark.parametrize(
    "relative",
    [
        ".env",
        "infra/prod/.env",
        ".env.local",
        "frontend/.env.production",
        "backend/dtxwiki.env",
    ],
)
def test_verifier_rejects_a_local_env_file(tmp_path, artifact_kind, relative):
    """C25: a checkout's secrets must never ride along into PyPI."""

    def add_env_file(stage):
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("DTXWIKI_SMTP_PASSWORD=secret\n", encoding="utf-8")

    dist = _dist_with_one_artifact_mutated(tmp_path, artifact_kind, add_env_file)

    result = _run_verifier(dist)

    assert result.returncode == 1
    artifact = WHEEL_NAME if artifact_kind == "wheel" else SDIST_NAME
    assert f"{artifact}: {relative} is a local env file" in (
        result.stdout + result.stderr
    )


def test_verifier_accepts_env_templates(tmp_path):
    def add_templates(stage):
        for relative in (
            "infra/prod/.env.example",
            "infra/prod/.env.docker.example",
            "frontend/.env.example",
        ):
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("DTXWIKI_SESSION_SECRET=\n", encoding="utf-8")

    result = _run_verifier(_dist(tmp_path, mutate=add_templates))

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "relative",
    [
        "backend/alembic.ini",
        "backend/migrations/release_manifest.json",
        "backend/migrations/revision_modules/v0_1/efcbcf724889_core_schema/__init__.py",
    ],
)
def test_verifier_rejects_an_artifact_missing_alembic_packaging_files(
    tmp_path, relative
):
    """Required configuration, release mapping and module packages must ship."""

    dist = _dist(tmp_path, mutate=lambda stage: (stage / relative).unlink())

    result = _run_verifier(dist)

    assert result.returncode == 1
    assert relative in result.stdout + result.stderr


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (
            lambda stage: (stage / "backend" / "static" / "index.html").unlink(),
            "index.html",
        ),
        (
            lambda stage: (
                stage / "backend" / "static" / "assets" / "index-abc123.js"
            ).unlink(),
            "index-abc123.js",
        ),
    ],
    ids=["no-index", "pruned-asset"],
)
def test_verifier_rejects_an_artifact_without_a_usable_frontend_bundle(
    tmp_path, mutate, expected
):
    result = _run_verifier(_dist(tmp_path, mutate=mutate))

    assert result.returncode == 1
    assert expected in result.stdout + result.stderr


def _index_without_an_assets_entry(stage):
    static = stage / "backend" / "static"
    shutil.rmtree(static / "assets")
    static.joinpath("index.html").write_text(
        "<!doctype html><html><body>"
        '<script type="module" src="/main.js"></script>'
        "</body></html>",
        encoding="utf-8",
    )
    static.joinpath("main.js").write_text("export {};\n", encoding="utf-8")


def test_verifier_rejects_an_index_that_names_no_entry_under_assets(tmp_path):
    """Plan step (b): a bundle entry has to live under backend/static/assets/.

    Every reference in this index resolves, so the "which the artifact does not
    carry" rule passes it. Only the assets/ rule can tell that no
    `make bundle-frontend` output produced it.
    """

    result = _run_verifier(_dist(tmp_path, mutate=_index_without_an_assets_entry))

    assert result.returncode == 1
    assert (
        "references no entry under backend/static/assets/"
        in result.stdout + result.stderr
    )


def test_verifier_rejects_a_dist_whose_sdist_alone_lost_its_package_data(tmp_path):
    """The sdist is published too, so it is verified too.

    `actions/upload-artifact` takes `path: dist/` wholesale and
    `pypa/gh-action-pypi-publish` publishes every file in it, so a package-data
    regression that only reaches the sdist still reaches PyPI. The wheel here
    is intact: a gate handed `$(ls dist/dtxwiki-*.whl)` passes this dist.
    """

    dist = _dist(
        tmp_path,
        sdist_mutate=lambda stage: shutil.rmtree(stage / "backend" / "static"),
    )

    result = _run_verifier(dist)

    assert result.returncode == 1
    output = result.stdout + result.stderr
    assert f"{SDIST_NAME}: backend/static/index.html is missing" in output
    assert f"{WHEEL_NAME}:" not in output, "the wheel in this dist is intact"


@pytest.mark.parametrize(
    "damage, expected",
    [
        (
            lambda dist: (dist / SDIST_NAME).unlink(),
            "holds 0 sdist(s), expected exactly one",
        ),
        (
            lambda dist: (dist / WHEEL_NAME).unlink(),
            "holds 0 wheel(s), expected exactly one",
        ),
        (
            lambda dist: (dist / SDIST_NAME).write_bytes(b"not a tarball"),
            f"{SDIST_NAME} is not a readable sdist",
        ),
    ],
    ids=["no-sdist", "no-wheel", "unreadable-sdist"],
)
def test_verifier_rejects_a_dist_that_is_not_one_wheel_and_one_sdist(
    tmp_path, damage, expected
):
    dist = _dist(tmp_path)
    damage(dist)

    result = _run_verifier(dist)

    assert result.returncode == 1
    assert expected in result.stdout + result.stderr


@pytest.mark.parametrize(
    "name", [f"{WHEEL_NAME}.asc", "prev"], ids=["stray-file", "build-wheel-prev"]
)
def test_verifier_rejects_a_dist_carrying_an_entry_it_cannot_verify(tmp_path, name):
    """Whatever else lands in dist/ is uploaded and offered to twine.

    `make build-wheel` parks the previous build in `dist/prev/`, and
    `path: dist/` uploads that subtree, so it has to block the release rather
    than ride along unchecked.
    """

    dist = _dist(tmp_path)
    if name == "prev":
        (dist / name).mkdir()
    else:
        (dist / name).write_bytes(b"")

    result = _run_verifier(dist)

    assert result.returncode == 1
    assert f"{name} is neither the wheel nor the sdist" in result.stdout + result.stderr


def test_verifier_ignores_the_unpublishable_dot_entries_uv_build_leaves(tmp_path):
    """`uv build` writes `dist/.gitignore`; the publish glob cannot reach it.

    `pypa/gh-action-pypi-publish` hands twine a `dist/*` shell glob, which never
    matches a leading dot, so a dot-entry is not part of the published set and
    must not fail a release that is otherwise clean.
    """

    result = _run_verifier(_dist(tmp_path, extra=(".gitignore",)))

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "make_argument",
    [
        lambda tmp_path: "",
        lambda tmp_path: tmp_path / "dist",
        lambda tmp_path: _write_wheel(
            tmp_path / WHEEL_NAME, _stage(tmp_path, name="lone-wheel")
        ),
    ],
    ids=["empty-argument", "missing-dist", "one-wheel-not-the-dist-tree"],
)
def test_verifier_exits_two_when_the_argument_is_not_the_dist_directory(
    tmp_path, make_argument
):
    # `Path("")` is `.`, an existing directory, and the wheel path is the exact
    # argument the release job used to pass. Both must fail the job instead of
    # verifying the wrong thing -- or, in the wheel case, verifying only half
    # of what publish-pypi uploads.
    result = _run_verifier(make_argument(tmp_path))

    assert result.returncode == 2
    # The interpreter also exits 2 when the script itself is absent, so the
    # diagnosis has to come from the verifier for this to prove anything.
    assert "expected the dist directory to verify" in result.stderr


@pytest.mark.parametrize("artifact_kind", ["wheel", "sdist"])
@pytest.mark.parametrize("name", LICENSE_TEXTS)
def test_verifier_rejects_an_artifact_missing_a_license_text(
    tmp_path, artifact_kind, name
):
    dist = _dist_with_one_artifact_mutated(
        tmp_path, artifact_kind, lambda stage: (stage / name).unlink()
    )

    result = _run_verifier(dist)

    assert result.returncode == 1
    artifact = WHEEL_NAME if artifact_kind == "wheel" else SDIST_NAME
    assert f"{artifact}: {name} is missing" in result.stdout + result.stderr


def test_verifier_accepts_license_texts_under_the_wheel_dist_info(tmp_path):
    def move_licenses_into_dist_info(stage):
        licenses = stage / "dtxwiki-0.0.0.dist-info" / "licenses"
        licenses.mkdir(parents=True)
        for name in LICENSE_TEXTS:
            (stage / name).rename(licenses / name)

    dist = _dist(
        tmp_path, mutate=move_licenses_into_dist_info, sdist_mutate=lambda stage: None
    )

    result = _run_verifier(dist)

    assert result.returncode == 0, result.stdout + result.stderr


def test_verifier_rejects_a_bundle_without_third_party_notices(tmp_path):
    dist = _dist(
        tmp_path,
        mutate=lambda stage: (
            stage / "backend" / "static" / "THIRD_PARTY_NOTICES.txt"
        ).unlink(),
    )

    result = _run_verifier(dist)

    assert result.returncode == 1
    assert "backend/static/THIRD_PARTY_NOTICES.txt is missing" in (
        result.stdout + result.stderr
    )


def test_verifier_rejects_notices_that_omit_the_shipped_pdfjs_worker(tmp_path):
    def drop_pdfjs(stage):
        (stage / "backend" / "static" / "THIRD_PARTY_NOTICES.txt").write_text(
            "Third-party software bundled with the dtxWiki web UI\n\n"
            "- react@19.2.6: MIT\n",
            encoding="utf-8",
        )

    result = _run_verifier(_dist(tmp_path, mutate=drop_pdfjs))

    assert result.returncode == 1
    assert "omits pdfjs-dist" in result.stdout + result.stderr

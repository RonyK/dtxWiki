#!/usr/bin/env python3
"""Verify the exact artifacts a tag is about to publish.

`.github/workflows/release.yml` fires on any ``v*`` tag while ci.yml only runs
on pull requests and pushes to main, so a tag on a commit main CI never built
reaches PyPI with nothing but this check in front of it. It therefore inspects
the ``dist/`` tree that `upload-artifact` hands to `publish-pypi` rather than
building a fresh one, and it inspects *every* file in that tree, because
`pypa/gh-action-pypi-publish` uploads the whole directory: verifying only the
wheel would let a package-data regression reach PyPI inside the sdist.

Problems are collected rather than raised one at a time, so a broken release
does not cost one tag push per defect. The single exception is a packaged tree
whose Alembic graph cannot be imported at all: the probe subprocess reports
that as one aggregated failure, not one failure per broken import.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Sequence
from pathlib import Path

from deploy_static_bundle import _local_entry_files

# Canonical IDs of the historical schema boundaries, base first. Later revisions
# descend from the core boundary and converge to one head. A merge preserves
# independently applied revisions without editing either parent.
RELEASED_CHAIN_BASE = [
    ["bd62193d5cf6", None],
    ["efcbcf724889", "bd62193d5cf6"],
]
# SHA-256 of the renamed baseline and its implementation modules, relative to
# backend/migrations. A deliberate second copy of
# backend/migrations/frozen_revisions.json: editing a released revision and
# re-hashing the manifest in the same change still fails here, and
# tests/packaging/test_migration_chain.py holds the two copies equal.
FROZEN_MIGRATIONS = {
    "revision_modules/__init__.py": (
        "4a10c05f30c23333471d4ce1a16d2d768361fb82aac88f3862dbedff753bf7b6"
    ),
    "revision_modules/v0_1/__init__.py": (
        "4a10c05f30c23333471d4ce1a16d2d768361fb82aac88f3862dbedff753bf7b6"
    ),
    "revision_modules/v0_1/efcbcf724889_core_schema/__init__.py": (
        "b57efc37a5d5c2e8027f42613fdfdb3252376d6784ba4cff95030e74ccd26f09"
    ),
    "revision_modules/v0_1/efcbcf724889_core_schema/context_data.py": (
        "366709f74283479354b738c4a4367f78183d327b184ad2d78654a60db4384ccc"
    ),
    "revision_modules/v0_1/efcbcf724889_core_schema/context_schema.py": (
        "406886a9b7e0963d0ac5e21947371a0824f5dfb1d382e384d69e90011b74ed85"
    ),
    "revision_modules/v0_1/efcbcf724889_core_schema/identity_access_schema.py": (
        "bdd07ca0004aa5973cd593bd6e00d1206781d0116e16ccc2a071aa206248f9f9"
    ),
    "revision_modules/v0_1/efcbcf724889_core_schema/library_media_schema.py": (
        "0026ee1fb15c27692a1888f4521ae2d01d9ddf343d45f9865c41d15e47fe5cfb"
    ),
    "revision_modules/v0_1/efcbcf724889_core_schema/library_policy.py": (
        "ed86943f046fedf08eb7fa0dde1a6674d07243bbcaf0907d49e4e00d8ef3732c"
    ),
    "revision_modules/v0_1/efcbcf724889_core_schema/operations_schema.py": (
        "daa008f631076b9018ef2ff045a06a27258c206dc001d1a23e4a836f988c36eb"
    ),
    "revision_modules/v0_1/efcbcf724889_core_schema/search_schema.py": (
        "13cb7fad720896937887a4692cb67c8afb63397cea120c38af4ae9683d7c62db"
    ),
    "revision_modules/v0_1/efcbcf724889_core_schema/tenancy_schema.py": (
        "e36f7d452028a03a73ce94d50f3b6c2b30c43b60230a332170b23fbc83bc9698"
    ),
    "revision_modules/v0_1/efcbcf724889_core_schema/unique_key_prepass.py": (
        "6d564b038d785c0d551ee103908ec3f61f0c08a3ff6d325053e629b3519e1cec"
    ),
    "revision_modules/v0_1/efcbcf724889_core_schema/workspace_schema.py": (
        "6daaec7d569a6ec8c6f76c444a95e4fc945e218215d9234debcbb5187e2960ec"
    ),
    "versions/v0_0/bd62193d5cf6_initial_schema.py": (
        "1c944d1f3c1d5427ff40470aacf1fbf611092a8e7e1111f19bc4089d13bd45e8"
    ),
    "versions/v0_1/efcbcf724889_core_schema.py": (
        "1e4edcea3733bc4ea0f7407a9cd331a5f2f5e6913eebb289856cf87a45b9cd6b"
    ),
    "versions/v0_1/19db79d3e622_release_0_1_3.py": (
        "f6eade587f72c760ff86a37d2769355e22cbb647b9815c12c3e3ec084dcf5a0c"
    ),
    "revision_modules/v0_1/content_event_backfill_v1.py": (
        "0321e31467fa8112e133b39a197b9456ea39795a684aaae0c8934eeb1f655995"
    ),
    "revision_modules/v0_1/input_restrictions_v1.py": (
        "14f50082552dc8490e2ee2ac239983f4224cbbce80758df6ce498f9cacef26d5"
    ),
    "versions/v0_1/ef53a0a7061c_index_change_operation_change_set_id.py": (
        "4ca7da3416959c22c15d39b453bb7d72024c9efca56e68253ea1e244a2066547"
    ),
}
FROZEN_MANIFEST = Path("backend") / "migrations" / "frozen_revisions.json"
# A local env file can hold SMTP passwords and session secrets. Only the
# documented templates may ship.
ENV_FILE_NAME = re.compile(r"(?:^\.env(?:\..*)?|.*\.env)\Z")
ENV_TEMPLATE_SUFFIX = ".example"
# Modules removed deliberately: the stdio MCP transport and its per-client
# installers, the legacy Pages MCP tools, and two routers that were never
# mounted. A release artifact carrying any of them is shipping code the
# project decided not to have. The names live here so
# tests/static/test_retired_modules_stay_deleted.py can hold the source tree
# to the same list -- the artifact gate alone only fires at `v*` tag time,
# which is far too late to notice a resurrection.
RETIRED_MODULES = (
    Path("backend/api/pages.py"),
    Path("backend/api/uploads.py"),
    Path("backend/cli/integrations/claude.py"),
    Path("backend/cli/integrations/codex.py"),
    Path("backend/cli/integrations/cursor.py"),
    Path("backend/mcp/entry.py"),
    Path("backend/mcp/page_tools.py"),
    Path("backend/mcp/stdio_server.py"),
    Path("backend/schemas/mcp/page_tools.py"),
)
RETIRED_MCP_CONSOLE_SCRIPT = re.compile(r"(?m)^\s*[\"']?dtxwiki-mcp[\"']?\s*=")

# Resolving the head is also the revision-module import check: Alembic executes
# versions/v0_1/efcbcf724889_core_schema.py, whose module-level import pulls in all nine
# revision-owned ``revision_modules/v0_1/efcbcf724889_core_schema`` modules. `foreign`
# closes the hole that makes
# that check
# meaningless -- both `uv sync` and `make install` register dtxwiki as an
# editable install, whose finder maps any missing `backend.<child>` back to the
# checkout, so a wheel that carries none of them would still resolve.
_GRAPH_PROBE = """
import json
import os
import sys
import warnings

root = sys.argv[1]
sys.path.insert(0, root)

from alembic.config import Config
from alembic.script import ScriptDirectory

config = Config(os.path.join(root, "backend", "alembic.ini"))
config.set_main_option(
    "script_location", os.path.join(root, "backend", "migrations")
)
script = ScriptDirectory.from_config(config)
with warnings.catch_warnings():
    # Alembic otherwise warns and silently replaces a duplicate revision ID.
    warnings.simplefilter("error", UserWarning)
    heads = sorted(script.get_heads())
    chain = [[entry.revision, entry.down_revision] for entry in script.walk_revisions()]

contained = os.path.realpath(root)
foreign = {}
for name, module in list(sys.modules.items()):
    if name != "backend" and not name.startswith("backend."):
        continue
    origins = (
        [module.__file__]
        if getattr(module, "__file__", None)
        else list(getattr(module, "__path__", None) or [])
    )
    for origin in origins:
        if not origin or not os.path.isabs(origin):
            continue
        resolved = os.path.realpath(origin)
        if resolved != contained and not resolved.startswith(contained + os.sep):
            foreign[name] = origin

print(json.dumps({"heads": heads, "chain": chain, "foreign": foreign}))
"""


def _check_frontend_bundle(root: Path) -> list[str]:
    """Check the bundle no pytest suite can see.

    `backend/static/` is gitignored and ci.yml's packaging job never runs
    `make bundle-frontend`, so a wheel built without a bundle -- or one whose
    index survives a `--fresh` deploy that pruned the assets it names -- can
    only be caught here, after the release job has built it.
    """

    static = root / "backend" / "static"
    index = static / "index.html"
    if not index.is_file():
        return ["backend/static/index.html is missing (bundle-frontend did not run)"]
    try:
        references = _local_entry_files(index.read_text(encoding="utf-8"))
    except ValueError as error:
        return [f"backend/static/index.html carries an unusable entry: {error}"]
    if not references:
        return ["backend/static/index.html references no local bundle entry"]
    failures = [
        f"backend/static/index.html references {reference.as_posix()}, "
        "which the artifact does not carry"
        for reference in sorted(references)
        if not (static / reference).is_file()
    ]
    # A resolvable reference is not enough on its own: Vite emits the app into
    # backend/static/assets/, so an index whose only entry sits beside it is an
    # index no `make bundle-frontend` produced.
    if not any(reference.parts[:1] == ("assets",) for reference in references):
        failures.append(
            "backend/static/index.html references no entry under backend/static/assets/"
        )
    return failures


LICENSE_TEXTS = ("LICENSE", "LICENSE-AGPL-3.0", "LICENSE-ELv2", "NOTICE")
THIRD_PARTY_NOTICES = Path("backend") / "static" / "THIRD_PARTY_NOTICES.txt"


def _check_license_payload(root: Path) -> list[str]:
    """Check that the artifact carries its own and its bundled components' terms.

    An sdist carries the license texts at its root; a wheel carries them under
    ``*.dist-info/licenses/``. The web UI bundle redistributes npm packages, so
    the notice file the frontend build emits must travel with it -- and the
    PDF.js worker, an Apache-2.0 asset outside the module graph, must be in it.
    """

    failures = [
        f"{name} is missing"
        for name in LICENSE_TEXTS
        if not (root / name).is_file()
        and not any(root.glob(f"*.dist-info/licenses/{name}"))
    ]
    notices = root / THIRD_PARTY_NOTICES
    if not notices.is_file():
        failures.append(f"{THIRD_PARTY_NOTICES.as_posix()} is missing")
        return failures
    text = notices.read_text(encoding="utf-8")
    assets = root / "backend" / "static" / "assets"
    if any(assets.glob("pdf.worker*")) and (
        "\n- pdfjs-dist@" not in text or "Apache License" not in text
    ):
        failures.append(
            f"{THIRD_PARTY_NOTICES.as_posix()} omits pdfjs-dist and its Apache-2.0 "
            "text although the bundle ships the PDF.js worker"
        )
    return failures


def _check_alembic_payload(root: Path) -> list[str]:
    failures: list[str] = []
    if not (root / "backend" / "alembic.ini").is_file():
        failures.append("backend/alembic.ini is missing")
    modules = (
        root
        / "backend"
        / "migrations"
        / "revision_modules/v0_1/efcbcf724889_core_schema"
    )
    if not (modules / "__init__.py").is_file():
        failures.append(
            "backend/migrations/revision_modules/v0_1/"
            "efcbcf724889_core_schema/__init__.py is missing"
        )
    # Release mappings grow with new releases; pin the historical entries,
    # rather than freezing the entire appendable manifest's byte content.
    manifest_path = root / "backend/migrations/release_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = {
            "0.0.1": {"group": "v0_0", "head": "bd62193d5cf6"},
            "0.1.1": {"group": "v0_1", "head": "efcbcf724889"},
        }
        aliases = {
            "0001_initial_release": "bd62193d5cf6",
            "0002_epic1": "efcbcf724889",
        }
        valid = (
            manifest["format_version"] == 1
            and all(
                manifest["schema_releases"][key] == value
                for key, value in expected.items()
            )
            and manifest["legacy_revision_aliases"] == aliases
        )
    except (OSError, ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        failures.append(
            "backend/migrations/release_manifest.json is missing or invalid"
        )
    return failures


def _check_frozen_migrations(root: Path) -> list[str]:
    """Canonical migration files remain byte-identical to the renamed baseline.

    A database stamped ``efcbcf724889`` is only described by that exact
    code; editing a released revision silently diverges every such database
    from a fresh one. New schema work belongs in a new revision.
    """

    migrations = root / "backend" / "migrations"
    failures: list[str] = []
    for relative, expected in sorted(FROZEN_MIGRATIONS.items()):
        path = migrations / relative
        if not path.is_file():
            failures.append(f"backend/migrations/{relative} is missing")
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            failures.append(
                f"backend/migrations/{relative} changed after its release "
                f"(sha256 {actual}, frozen {expected}); add a new revision "
                "instead of editing a released one"
            )
    failures.extend(
        "backend/migrations/revision_modules/v0_1/"
        f"efcbcf724889_core_schema/{path.name} is not part of the "
        "frozen canonical baseline revision"
        for path in sorted(
            (migrations / "revision_modules/v0_1/efcbcf724889_core_schema").glob("*.py")
        )
        if f"revision_modules/v0_1/efcbcf724889_core_schema/{path.name}"
        not in FROZEN_MIGRATIONS
    )
    manifest = root / FROZEN_MANIFEST
    try:
        recorded = json.loads(manifest.read_text(encoding="utf-8"))["sha256"]
    except (OSError, ValueError, KeyError, TypeError):
        failures.append(f"{FROZEN_MANIFEST.as_posix()} is missing or unreadable")
    else:
        if recorded != FROZEN_MIGRATIONS:
            failures.append(
                f"{FROZEN_MANIFEST.as_posix()} does not record the frozen "
                "canonical baseline migration digests"
            )
    return failures


def _check_env_files(root: Path) -> list[str]:
    """Reject local env files; a template ending in ``.example`` may ship."""

    return [
        f"{path.relative_to(root).as_posix()} is a local env file (only "
        f"*{ENV_TEMPLATE_SUFFIX} templates may ship)"
        for path in sorted(root.rglob("*"))
        if ENV_FILE_NAME.match(path.name)
        and not path.name.endswith(ENV_TEMPLATE_SUFFIX)
        and not path.is_dir()
    ]


def _check_retired_mcp_transport(root: Path) -> list[str]:
    """Reject stdio entry points removed from the supported MCP surface."""

    failures = [
        f"{relative.as_posix()} is a retired module"
        for relative in RETIRED_MODULES
        if (root / relative).is_file()
    ]
    metadata_files = [
        root / "pyproject.toml",
        root / "setup.cfg",
        root / "setup.py",
        *root.glob("*.dist-info/entry_points.txt"),
        *root.glob("*.egg-info/entry_points.txt"),
    ]
    for metadata in metadata_files:
        if not metadata.is_file():
            continue
        try:
            source = metadata.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            failures.append(f"{metadata.relative_to(root).as_posix()} is not UTF-8")
            continue
        if RETIRED_MCP_CONSOLE_SCRIPT.search(source):
            failures.append(
                f"{metadata.relative_to(root).as_posix()} defines the retired "
                "dtxwiki-mcp console script"
            )
    failures.extend(
        f"{path.relative_to(root).as_posix()} is a retired dtxwiki-mcp executable"
        for path in root.rglob("dtxwiki-mcp")
        if path.is_file()
    )
    return failures


def _check_migration_graph(root: Path) -> tuple[list[str], str | None]:
    # The `foreign` scan is the isolation here -- not this cwd, and not the
    # probe's `sys.path.insert(0, root)`. Both `uv sync` and `make install`
    # register dtxwiki as an editable install, and its finder sits on
    # sys.meta_path as a fallback for every `backend.<child>` the extracted
    # tree cannot supply, so it answers out of the checkout wherever the probe
    # runs and whatever precedes '' on sys.path. cwd is the extraction parent
    # only to keep an unrelated working directory off the probe's sys.path;
    # tests/packaging/test_wheel_artifact_verifier.py pins the real guarantee
    # by running this probe from the repository root itself and showing the
    # head still resolves green while `foreign` reports the leak.
    probe = subprocess.run(
        [sys.executable, "-c", _GRAPH_PROBE, str(root)],
        cwd=root.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        return [
            "resolving the packaged Alembic head failed:\n"
            + (probe.stdout + probe.stderr).strip()
        ], None
    try:
        resolved = json.loads(probe.stdout)
    except json.JSONDecodeError:
        return [f"the packaged Alembic head probe returned {probe.stdout!r}"], None

    failures = _chain_failures(resolved["heads"], resolved["chain"])
    failures.extend(
        f"{name} resolved from {origin}, outside the artifact"
        for name, origin in sorted(resolved["foreign"].items())
    )
    heads = resolved["heads"]
    return failures, heads[0] if len(heads) == 1 else None


def _chain_failures(heads: list[str], chain: list[list]) -> list[str]:
    """One current head with every later branch descending from frozen core.

    ``chain`` carries Alembic's revision edges. Walk order does not determine
    ancestry: independent branches may converge through a merge revision.
    """

    failures: list[str] = []
    if len(heads) != 1:
        failures.append(f"packaged Alembic heads are {heads}, expected exactly one")
    parents: dict[str, tuple[str, ...]] = {}
    for revision, down_revision in chain:
        if revision in parents:
            failures.append(f"packaged revision {revision} is present more than once")
        if down_revision is None:
            ancestors = ()
        elif isinstance(down_revision, str):
            ancestors = (down_revision,)
        else:
            ancestors = tuple(down_revision)
        if len(set(ancestors)) != len(ancestors):
            failures.append(f"packaged revision {revision} repeats a parent")
        parents[revision] = ancestors

    for revision, down_revision in RELEASED_CHAIN_BASE:
        expected = () if down_revision is None else (down_revision,)
        if parents.get(revision) != expected:
            failures.append(
                "packaged revision graph must preserve released boundaries "
                f"{RELEASED_CHAIN_BASE}; {revision} has parents "
                f"{parents.get(revision)!r}"
            )

    referenced = {parent for ancestors in parents.values() for parent in ancestors}
    for revision, ancestors in parents.items():
        for parent in ancestors:
            if parent not in parents:
                failures.append(
                    f"packaged revision {revision} has missing parent {parent}"
                )
    graph_heads = sorted(set(parents) - referenced)
    if sorted(heads) != graph_heads:
        failures.append(
            f"packaged Alembic heads {heads} do not match graph heads {graph_heads}"
        )

    core = RELEASED_CHAIN_BASE[-1][0]
    visiting: set[str] = set()
    reaches_core: dict[str, bool] = {}

    def descends_from_core(revision: str) -> bool:
        if revision == core:
            return True
        if revision in reaches_core:
            return reaches_core[revision]
        if revision in visiting:
            failures.append(f"packaged revision graph contains a cycle at {revision}")
            return False
        if revision not in parents:
            return False
        visiting.add(revision)
        # Evaluate every parent so a valid path cannot hide a corrupt sibling.
        paths = [descends_from_core(parent) for parent in parents[revision]]
        visiting.remove(revision)
        reaches_core[revision] = bool(paths) and all(paths)
        return reaches_core[revision]

    frozen = {revision for revision, _ in RELEASED_CHAIN_BASE}
    for revision in sorted(set(parents) - frozen):
        if not descends_from_core(revision):
            failures.append(
                f"every ancestry path of packaged revision {revision} "
                f"must descend from released core {core}"
            )
    return failures


def _revision_module_count(root: Path) -> int:
    modules = (
        root
        / "backend"
        / "migrations"
        / "revision_modules/v0_1/efcbcf724889_core_schema"
    )
    return len([path for path in modules.glob("*.py") if path.name != "__init__.py"])


def collect_artifacts(dist: Path) -> tuple[list[Path], list[str]]:
    """Split ``dist`` into the artifacts to verify and what must not be there.

    `upload-artifact` uploads `dist/` wholesale and
    `pypa/gh-action-pypi-publish` publishes every entry it finds, so anything in
    here that this script cannot verify would reach PyPI unchecked. Reporting
    it is what makes "the verified set" and "the published set" the same set.
    """

    entries = sorted(dist.iterdir())
    wheels = [entry for entry in entries if entry.is_file() and entry.suffix == ".whl"]
    sdists = [
        entry for entry in entries if entry.is_file() and entry.name.endswith(".tar.gz")
    ]
    verifiable = {*wheels, *sdists}
    # Dot-entries are the one exemption: the publish action hands twine a
    # `dist/*` shell glob, which never matches a leading dot, so the
    # `.gitignore` `uv build` leaves in its output directory is unpublishable.
    # Everything else is publishable -- `make build-wheel` parks superseded
    # artifacts in `dist/prev/`, and that subtree is uploaded too -- so
    # everything else has to be an artifact this script actually verified.
    failures = [
        f"{entry.name} is neither the wheel nor the sdist, and publish-pypi "
        f"uploads everything in {dist.as_posix()}/"
        for entry in entries
        if entry not in verifiable and not entry.name.startswith(".")
    ]
    if len(wheels) != 1:
        failures.append(
            f"{dist.as_posix()}/ holds {len(wheels)} wheel(s), expected exactly one"
        )
    if len(sdists) != 1:
        failures.append(
            f"{dist.as_posix()}/ holds {len(sdists)} sdist(s), expected exactly one"
        )
    return [*wheels, *sdists], failures


def _unpack(artifact: Path, extract: Path) -> tuple[Path | None, list[str]]:
    """Unpack one artifact and return the directory its ``backend/`` sits in."""

    extract.mkdir(parents=True, exist_ok=True)
    if artifact.suffix == ".whl":
        try:
            with zipfile.ZipFile(artifact) as archive:
                archive.extractall(extract)
        except zipfile.BadZipFile as error:
            return None, [f"is not a readable wheel: {error}"]
        return extract, []
    try:
        with tarfile.open(artifact) as archive:
            archive.extractall(extract, filter="data")
    except (tarfile.TarError, OSError, ValueError) as error:
        return None, [f"is not a readable sdist: {error}"]
    # An sdist nests its payload under `<name>-<version>/`; a wheel does not.
    entries = sorted(extract.iterdir())
    if len(entries) != 1 or not entries[0].is_dir():
        return None, ["does not unpack to a single top-level directory"]
    return entries[0], []


def verify_artifact(artifact: Path, extract: Path) -> tuple[list[str], str | None]:
    """Return this artifact's release-blocking problems and its evidence line."""

    root, failures = _unpack(artifact, extract)
    if root is None:
        return [f"{artifact.name} {failure}" for failure in failures], None
    graph_failures, head = _check_migration_graph(root)
    problems = [
        *_check_frontend_bundle(root),
        *_check_license_payload(root),
        *_check_retired_mcp_transport(root),
        *_check_env_files(root),
        *_check_alembic_payload(root),
        *_check_frozen_migrations(root),
        *graph_failures,
    ]
    if problems:
        return [f"{artifact.name}: {problem}" for problem in problems], None
    return [], (
        f"{artifact.name}: alembic head {head}, "
        f"{_revision_module_count(root)} packaged core revision modules, "
        f"{len(FROZEN_MIGRATIONS)} frozen migration files intact, "
        "frontend bundle intact"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify the artifacts a release tag is about to publish."
    )
    parser.add_argument(
        "dist", help="path to the dist/ directory upload-artifact will publish"
    )
    arguments = parser.parse_args(argv)

    # release.yml hands this the same path `upload-artifact` uploads, so the
    # verified set is the published set by construction. Anything narrower --
    # the `$(ls dist/dtxwiki-*.whl)` this step used to pass -- published the
    # sdist unverified, and an empty or multi-line glob expansion read as a
    # verified artifact. Exiting 2 keeps a mis-aimed argument from passing:
    # note `Path("")` is `.`, which is a directory, so the raw string is
    # checked before it becomes a Path.
    if not arguments.dist or not Path(arguments.dist).is_dir():
        parser.error(f"expected the dist directory to verify, got {arguments.dist!r}")
    dist = Path(arguments.dist)

    artifacts, failures = collect_artifacts(dist)
    evidence: list[str] = []
    for artifact in artifacts:
        with tempfile.TemporaryDirectory() as workspace:
            problems, line = verify_artifact(artifact, Path(workspace) / "artifact")
        failures.extend(problems)
        if line is not None:
            evidence.append(line)

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        print(
            f"{dist.as_posix()}/: {len(failures)} release-blocking problem(s)",
            file=sys.stderr,
        )
        return 1
    for line in evidence:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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

# The revisions 0.1.1 published, base first. Every later release keeps them as
# the start of one linear chain: a new revision descends from the current head
# and never edits a released file (see FROZEN_MIGRATIONS).
RELEASED_CHAIN_BASE = [
    ["0001_initial_release", None],
    ["0002_epic1", "0001_initial_release"],
]
# SHA-256 of every migration file 0.1.1 published, relative to
# backend/migrations. A deliberate second copy of
# backend/migrations/frozen_revisions.json: editing a released revision and
# re-hashing the manifest in the same change still fails here, and
# tests/packaging/test_migration_chain.py holds the two copies equal.
FROZEN_MIGRATIONS = {
    "revision_0002/__init__.py": (
        "71f7a5d9f393e0b419d91f0fd9227cb27e3624785b42c2c7ddab609c80d8aee6"
    ),
    "revision_0002/context_data.py": (
        "075906a2ad4ea9008233509031c73ffde600043ab3894673b631d7b53426bd5d"
    ),
    "revision_0002/context_schema.py": (
        "e452ac330100653c4a05a1bb279954bee83d6f3aee1f70d8a554834ceb297137"
    ),
    "revision_0002/identity_access_schema.py": (
        "7bd29b29460f7b144b5a2f453ae3a83ba764288f8a796693ba7c8fc50bf4dd32"
    ),
    "revision_0002/library_media_schema.py": (
        "d3e2b5666a06219e60bb6a8294d7843db2fc556a9b7b1336c56ec61dce26b532"
    ),
    "revision_0002/library_policy.py": (
        "e850a8316e11c83ada328dba0721efc862b4097a23a3e050aff2345598dd5cf8"
    ),
    "revision_0002/operations_schema.py": (
        "0b7dc03d8fb784c891b1da555e56b1586022590696ee1027b452b76dd1de7d91"
    ),
    "revision_0002/search_schema.py": (
        "64eb67015b20a7569c5e977744b555eb78901bf16c5a26e15f9d8f33db224e94"
    ),
    "revision_0002/tenancy_schema.py": (
        "55426d1c3f84c7a1611f660b3b541181435d76d5674aa93e36c2ab2dd3b0272b"
    ),
    "revision_0002/unique_key_prepass.py": (
        "6d564b038d785c0d551ee103908ec3f61f0c08a3ff6d325053e629b3519e1cec"
    ),
    "revision_0002/workspace_schema.py": (
        "f75ad4453b33a014bec4c40c0042a43e062c2b0f3ac825a81a928c5734461bff"
    ),
    "versions/0001_initial_release.py": (
        "dc68c40730f8ef83544c5c7fb59589806cd6e80b869617eda68cf5757995c9ac"
    ),
    "versions/0002_epic1.py": (
        "1e104864f9d31a51d51906fe7e0f49c37e9605b7f5c675d057b44166f3ffd55d"
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
# versions/0002_epic1.py, whose module-level import pulls in all nine
# revision-owned ``revision_0002`` modules. `foreign` closes the hole that makes
# that check
# meaningless -- both `uv sync` and `make install` register dtxwiki as an
# editable install, whose finder maps any missing `backend.<child>` back to the
# checkout, so a wheel that carries none of them would still resolve.
_GRAPH_PROBE = """
import json
import os
import sys

root = sys.argv[1]
sys.path.insert(0, root)

from alembic.config import Config
from alembic.script import ScriptDirectory

config = Config()
config.set_main_option(
    "script_location", os.path.join(root, "backend", "migrations")
)
script = ScriptDirectory.from_config(config)
heads = list(script.get_heads())
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
    modules = root / "backend" / "migrations" / "revision_0002"
    if not (modules / "__init__.py").is_file():
        failures.append("backend/migrations/revision_0002/__init__.py is missing")
    return failures


def _check_frozen_migrations(root: Path) -> list[str]:
    """Released migration files are byte-identical to what 0.1.1 published.

    A database stamped ``0002_epic1`` by 0.1.1 is only described by that exact
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
        f"backend/migrations/revision_0002/{path.name} is not part of the "
        "frozen 0.1.1 revision"
        for path in sorted((migrations / "revision_0002").glob("*.py"))
        if f"revision_0002/{path.name}" not in FROZEN_MIGRATIONS
    )
    manifest = root / FROZEN_MANIFEST
    try:
        recorded = json.loads(manifest.read_text(encoding="utf-8"))["sha256"]
    except (OSError, ValueError, KeyError, TypeError):
        failures.append(f"{FROZEN_MANIFEST.as_posix()} is missing or unreadable")
    else:
        if recorded != FROZEN_MIGRATIONS:
            failures.append(
                f"{FROZEN_MANIFEST.as_posix()} does not record the frozen 0.1.1 "
                "migration digests"
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
    """One linear chain that starts with the released revisions.

    ``chain`` is Alembic's ``walk_revisions`` order (head first). Revisions
    after ``0002_epic1`` are allowed; a second head, a merge, or a chain that
    does not begin with the released 0001 -> 0002 boundary is not.
    """

    failures: list[str] = []
    if len(heads) != 1:
        failures.append(f"packaged Alembic heads are {heads}, expected exactly one")
    base_first = list(reversed(chain))
    if base_first[: len(RELEASED_CHAIN_BASE)] != RELEASED_CHAIN_BASE:
        failures.append(
            f"packaged revision chain is {chain}; it must start with "
            f"{RELEASED_CHAIN_BASE} (base first)"
        )
    for (previous, _), (revision, down_revision) in zip(
        base_first, base_first[1:], strict=False
    ):
        if down_revision != previous:
            failures.append(
                f"packaged revision {revision} descends from {down_revision!r}, "
                f"not from {previous}; the chain must be linear"
            )
    return failures


def _revision_module_count(root: Path) -> int:
    modules = root / "backend" / "migrations" / "revision_0002"
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
        f"{_revision_module_count(root)} packaged 0002 revision modules, "
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

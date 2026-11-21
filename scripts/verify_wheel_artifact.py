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
import tomllib
import zipfile
from collections.abc import Sequence
from email.parser import Parser
from pathlib import Path

from deploy_static_bundle import _local_entry_files
from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version

# This is Vite's emitted URL literal for our one shared D3 module Worker,
# not a general JavaScript/import parser. Match its quoted content hash so an
# unrelated old Worker file cannot stand in for the adapter's required asset.
_D3_WORKER_URL = re.compile(
    r'new\s+URL\(\s*(?P<quote>["\'])'
    r"(?P<path>/assets/d3Layout\.worker-[A-Za-z0-9_-]+\.js)"
    r"(?P=quote)\s*,\s*import\.meta\.url\s*\)"
)

# Canonical IDs of the historical schema boundaries, base first. Later revisions
# descend from the core boundary and converge to one head. A merge preserves
# independently applied revisions without editing either parent.
RELEASED_CHAIN_BASE = [
    ["bd62193d5cf6", None],
    ["efcbcf724889", "bd62193d5cf6"],
]
# The consolidated 0.2 release owns Graph and the other release additions. A
# core-only artifact can resolve a valid head but cannot serve this release.
GRAPH_MIGRATION = Path("backend/migrations/versions/v0_2/5b7e5c149279_release_0_2.py")
GRAPH_RUNTIME_FILES = tuple(
    Path(path)
    for path in (
        "backend/api/graph.py",
        "backend/api/middleware/workspace_graph_compression.py",
        "backend/cli/commands/graph.py",
        "backend/context_media/domain/rendered_markdown.py",
        "backend/repositories/graph_density_repo.py",
        "backend/schemas/graph.py",
        "backend/schemas/workspace_graph.py",
        "backend/services/context_link_advisory.py",
        "backend/services/graph_density.py",
        "backend/services/graph_observation_service.py",
        "backend/services/graph_query_service.py",
        "backend/services/graph_rank.py",
        "backend/services/graph_reprojection.py",
        "backend/services/graph_semantics.py",
        "backend/services/indexing/co_read.py",
        "backend/services/page_link_ordering.py",
        "backend/services/workspace_graph_capability.py",
        "backend/services/workspace_graph_service.py",
        "backend/utils/markdown_links.py",
    )
)
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

# Inspection must not add files that later checks mistake for archive payload.
sys.dont_write_bytecode = True

root = sys.argv[1]
sys.path.insert(0, root)

from alembic.config import Config
from alembic.script import ScriptDirectory
from backend.services.ops.alembic_migration_graph import (
    INITIALIZATION_REVISION,
    initialization_script_directory,
)

migrations = os.path.join(root, "backend", "migrations")
config = Config(os.path.join(root, "backend", "alembic.ini"))
config.set_main_option("script_location", migrations)
script = ScriptDirectory.from_config(config)
with warnings.catch_warnings():
    # Alembic otherwise warns and silently replaces a duplicate revision ID.
    warnings.simplefilter("error", UserWarning)
    heads = sorted(script.get_heads())
    chain = [[entry.revision, entry.down_revision] for entry in script.walk_revisions()]
    initialization_error = None
    initialization_heads, initialization_chain = [], []
    initialization_sources = []
    try:
        # The release revisions the initialization snapshots stand in for.
        initialization_sources = [
            os.path.relpath(entry.path, migrations).replace(os.sep, "/")
            for entry in script.walk_revisions("base", INITIALIZATION_REVISION)
        ]
        initialization = initialization_script_directory()
        initialization_heads = sorted(initialization.get_heads())
        initialization_chain = [
            [entry.revision, entry.down_revision]
            for entry in initialization.walk_revisions()
        ]
    except Exception as error:
        # Retain canonical graph evidence even when a corrupt branch cannot
        # be composed into the direct-initialization route. The outer gate
        # reports both failures; an alternate-route error must not obscure
        # the independently checked published boundaries.
        initialization_error = str(error)

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

print(json.dumps({
    "heads": heads, "chain": chain, "foreign": foreign,
    "initialization_heads": initialization_heads,
    "initialization_chain": initialization_chain,
    "initialization_error": initialization_error,
    "initialization_revision": INITIALIZATION_REVISION,
    "initialization_sources": initialization_sources,
}))
"""


def _check_frontend_bundle(root: Path) -> list[str]:
    """Check the bundle no pytest suite can see.

    `backend/static/` is gitignored and the packaging job in release-checks.yml
    never runs `make bundle-frontend`, so a wheel built without a bundle -- or one whose
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


def _check_graph_release_bundle(root: Path) -> list[str]:
    """Require the Graph renderer payload when qualifying a release tag.

    Generic artifact checks also support backend-only fixtures. Publishing
    with --release-version additionally requires the final Graph UI assets.
    """

    static = root / "backend" / "static"
    failures: list[str] = []
    # The module Worker is lazy-loaded from the map adapter, not index.html.
    # A missing package-data entry used to pass the direct-entry check above
    # and fail only when the 2K layout started. Check both wheel and sdist: the
    # shared D3 simulation is a separate module asset requested by the proxy.
    if not any(
        path.is_file() and path.stat().st_size > 0
        for path in (static / "assets").glob("d3Layout.worker-*.js")
    ):
        failures.append(
            "backend/static/assets/d3Layout.worker-*.js is missing "
            "or empty (the 2K layout requires its shared D3 module Worker)"
        )
    failures.extend(_check_2k_worker_references(static))
    return failures


def _check_2k_worker_references(static: Path) -> list[str]:
    """Verify the exact Worker hash the built JavaScript requests."""

    references: set[tuple[Path, Path]] = set()
    failures: list[str] = []
    for javascript in sorted((static / "assets").rglob("*.js")):
        try:
            source = javascript.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            failures.append(
                f"backend/static/{javascript.relative_to(static)} "
                "is not UTF-8 JavaScript"
            )
            continue
        references.update(
            (javascript.relative_to(static), Path(match["path"].lstrip("/")))
            for match in _D3_WORKER_URL.finditer(source)
        )
    if not references:
        failures.append(
            "backend/static/assets JavaScript has no 2K D3 Worker URL "
            "reference (expected Vite's new URL(...,import.meta.url) literal)"
        )
    for javascript, reference in sorted(references):
        worker = static / reference
        if not worker.is_file() or worker.stat().st_size == 0:
            failures.append(
                f"backend/static/{javascript.as_posix()} references "
                f"backend/static/{reference.as_posix()}, which is missing or empty"
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
    if not (root / GRAPH_MIGRATION).is_file():
        failures.append(f"{GRAPH_MIGRATION.as_posix()} is missing")
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
            "0.1.2": {"group": "v0_1", "head": "efcbcf724889"},
            "0.1.3": {"group": "v0_1", "head": "19db79d3e622"},
            "0.1.4": {"group": "v0_1", "head": "eb86fdacb1bb"},
            "0.2.1": {"group": "v0_2", "head": "5b7e5c149279"},
            "0.2.3": {"group": "v0_2", "head": "00ba02774c4a"},
            "0.3.1": {"group": "v0_3", "head": "c3d2e4f601ab"},
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


def _check_release_schema(
    root: Path, artifact: Path, head: str | None, release_version: str
) -> list[str]:
    """Tie a tag to the metadata and schema boundary inside each artifact.

    A patch with no database changes inherits its nearest earlier boundary.
    A future entry must not make an older package with a new migration pass.
    This is an explicit publishing check: development artifacts may qualify
    their runtime before the owner chooses the next release version. Release
    artifacts must record the MCP tool contract they ship and contain no
    unreleased migration or helper files.
    """

    try:
        expected = Version(release_version)
    except InvalidVersion:
        return [f"invalid expected release version {release_version!r}"]

    return [
        *_check_release_identity(root, artifact, release_version, expected),
        *_check_schema_boundary(root, head, expected),
        *_check_unreleased_migrations(root),
        *_check_unreleased_tool_contract(root, release_version),
    ]


def _check_unreleased_tool_contract(root: Path, release_version: str) -> list[str]:
    """Require make set-version to record the packaged MCP tool contract."""

    relative = "backend/schemas/common.py"
    missing = f"{relative} does not state both MCP tool contract revisions exactly once"
    try:
        source = (root / relative).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return [missing]

    current = re.findall(r"^MCP_TOOL_CONTRACT_REVISION = (\d+)$", source, re.MULTILINE)
    released = re.findall(
        r"^MCP_RELEASED_TOOL_CONTRACT_REVISION = (\d+)$", source, re.MULTILINE
    )
    if len(current) != 1 or len(released) != 1:
        return [missing]
    if current == released:
        return []
    return [
        f"release artifact contains unreleased MCP tool contract revision "
        f"{current[0]} (last released {released[0]}). Run make set-version "
        f"VERSION={release_version} on the release branch to record it before "
        "publishing"
    ]


def _check_unreleased_migrations(root: Path) -> list[str]:
    """Development revisions and helpers must be promoted before publishing."""

    failures = []
    for relative in (
        "backend/migrations/versions/unreleased",
        "backend/migrations/revision_modules/unreleased",
    ):
        directory = root / relative
        for path in sorted([directory, *directory.rglob("*")]):
            # Empty directories are harmless archive scaffolding, but even an
            # empty __init__.py means the unreleased package was not cleared.
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                failures.append(
                    "release artifact contains unreleased migration file: "
                    f"{path.relative_to(root).as_posix()}; promote or remove it "
                    "before publishing"
                )
    return failures


def _check_release_identity(
    root: Path, artifact: Path, release_version: str, expected: Version
) -> list[str]:
    """Read built metadata, never the checkout's package version."""

    failures: list[str] = []
    try:
        if artifact.suffix == ".whl":
            name, version, _, _ = parse_wheel_filename(artifact.name)
        else:
            name, version = parse_sdist_filename(artifact.name)
    except (InvalidWheelFilename, InvalidSdistFilename) as error:
        failures.append(f"invalid release artifact filename: {error}")
    else:
        if name != "dtxwiki" or version != expected:
            failures.append(
                f"artifact filename identifies {name} version {version}, "
                f"expected dtxwiki release version {release_version}"
            )

    if artifact.suffix == ".whl":
        metadata_dirs = sorted(root.glob("*.dist-info"))
        expected_directory = f"dtxwiki-{expected}.dist-info"
        if (
            len(metadata_dirs) != 1
            or metadata_dirs[0].name != expected_directory
            or not metadata_dirs[0].is_dir()
        ):
            return [
                *failures,
                "wheel release metadata directory must be exactly "
                f"{expected_directory}",
            ]
        metadata_path = metadata_dirs[0] / "METADATA"
    else:
        metadata_path = root / "PKG-INFO"
    if not metadata_path.is_file():
        failures.append("release metadata is missing or ambiguous")
    else:
        try:
            metadata = Parser().parsestr(
                metadata_path.read_text(encoding="utf-8"), headersonly=True
            )
        except (OSError, UnicodeError):
            failures.append("release metadata is unreadable")
        else:
            if metadata.get_all("Name") != ["dtxwiki"]:
                failures.append(
                    "release metadata must identify Name: dtxwiki exactly once"
                )
            if metadata.get_all("Version") != [release_version]:
                failures.append(
                    f"release metadata must identify Version: {release_version} "
                    "exactly once"
                )

    if artifact.suffix != ".whl":
        try:
            project = tomllib.loads(
                (root / "pyproject.toml").read_text(encoding="utf-8")
            )["project"]
            valid = (
                project["name"] == "dtxwiki" and project["version"] == release_version
            )
        except (OSError, ValueError, KeyError, TypeError):
            valid = False
        if not valid:
            failures.append(
                "sdist pyproject metadata name/version does not match "
                f"dtxwiki release version {release_version}"
            )
    return failures


def _check_schema_boundary(
    root: Path, head: str | None, expected: Version
) -> list[str]:
    """Require this version's effective packaged schema boundary to be its head."""

    try:
        manifest = json.loads(
            (root / "backend/migrations/release_manifest.json").read_text(
                encoding="utf-8"
            )
        )
        entries = manifest["schema_releases"]
        if not isinstance(entries, dict) or not entries:
            raise ValueError("schema_releases must be a nonempty mapping")
        applicable: list[tuple[Version, str, str]] = []
        for release, entry in entries.items():
            boundary = Version(release)
            if (
                len(boundary.release) != 3
                or boundary.epoch
                or boundary.local
                or str(boundary) != release
            ):
                raise ValueError(f"noncanonical schema release version {release!r}")
            group = f"v{boundary.major}_{boundary.minor}"
            revision = entry["head"]
            if entry["group"] != group or not isinstance(revision, str):
                raise ValueError(f"invalid schema group/head for {release}")
            if (
                not re.fullmatch(r"[A-Za-z0-9_]+", revision)
                or len(
                    list(
                        (root / "backend/migrations/versions" / group).glob(
                            f"{revision}_*.py"
                        )
                    )
                )
                != 1
            ):
                raise ValueError(f"schema head {revision!r} is not in group {group}")
            if boundary <= expected:
                applicable.append((boundary, release, revision))
    except (OSError, ValueError, KeyError, TypeError) as error:
        return [f"invalid release_manifest schema mapping: {error}"]

    if not applicable:
        return [f"no schema release boundary applies to version {expected}"]
    _, boundary_version, mapped_head = max(applicable)
    if mapped_head != head:
        return [
            f"release version {expected} inherits schema boundary "
            f"{boundary_version} head {mapped_head}, but packaged Alembic "
            f"head is {head}; record the new schema boundary before release"
        ]
    return []


def _check_graph_runtime_payload(root: Path) -> list[str]:
    return [
        f"{relative.as_posix()} is missing"
        for relative in GRAPH_RUNTIME_FILES
        if not (root / relative).is_file()
    ]


def _frozen_digests(root: Path) -> dict[str, str] | None:
    """The artifact's frozen_revisions.json digests, relative to migrations/."""

    try:
        recorded = json.loads((root / FROZEN_MANIFEST).read_text(encoding="utf-8"))[
            "sha256"
        ]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(recorded, dict) or not recorded:
        return None
    if not all(
        isinstance(relative, str) and isinstance(digest, str)
        for relative, digest in recorded.items()
    ):
        return None
    return recorded


def _check_frozen_migrations(root: Path) -> list[str]:
    """Released migration files remain byte-identical to their frozen digests.

    A database stamped at a released revision is described by that exact
    code; editing a released revision silently diverges every such database
    from a fresh one. New schema work belongs in a new revision.
    """

    frozen = _frozen_digests(root)
    if frozen is None:
        return [f"{FROZEN_MANIFEST.as_posix()} is missing or unreadable"]
    migrations = root / "backend" / "migrations"
    failures: list[str] = []
    for relative, expected in sorted(frozen.items()):
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
        if f"revision_modules/v0_1/efcbcf724889_core_schema/{path.name}" not in frozen
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
    if resolved["initialization_error"] is not None:
        failures.append(f"initialization graph: {resolved['initialization_error']}")
    else:
        failures.extend(
            f"initialization graph: {failure}"
            for failure in _chain_failures(
                resolved["initialization_heads"],
                resolved["initialization_chain"],
                baseline=(("c3d2e4f601ab", None),),
            )
        )
        if resolved["initialization_heads"] != resolved["heads"]:
            failures.append(
                "initialization graph does not converge to the packaged head"
            )
        failures.extend(
            _check_initialization_snapshots(
                root,
                resolved["initialization_revision"],
                resolved["initialization_sources"],
            )
        )
    failures.extend(
        f"{name} resolved from {origin}, outside the artifact"
        for name, origin in sorted(resolved["foreign"].items())
    )
    heads = resolved["heads"]
    return failures, heads[0] if len(heads) == 1 else None


def _check_initialization_snapshots(
    root: Path, revision: str, sources: list[str]
) -> list[str]:
    """Each packaged snapshot was captured from this package's release chain.

    The freeze manifest only proves a snapshot is the file that was committed
    with it. A snapshot of an earlier schema boundary, committed with a
    matching digest, passes that check and then breaks fresh installs, so its
    recorded revision, dialect and source revisions must be the packaged ones.
    """

    migrations = root / "backend" / "migrations"
    packaged = {
        relative: hashlib.sha256((migrations / relative).read_bytes()).hexdigest()
        for relative in sources
    }
    failures: list[str] = []
    for dialect in ("sqlite", "postgresql"):
        name = f"backend/migrations/initialization/schema/{dialect}.json"
        try:
            snapshot = json.loads((root / name).read_text(encoding="utf-8"))
            recorded = (snapshot["revision"], snapshot["dialect"])
            recorded_sources = snapshot["source_revision_sha256"]
        except (OSError, ValueError, KeyError, TypeError):
            failures.append(f"{name} is missing or unreadable")
            continue
        if recorded != (revision, dialect):
            failures.append(
                f"{name} records the {recorded[1]} schema at {recorded[0]}, but "
                f"the package initializes {dialect} at {revision}; regenerate "
                "the snapshot"
            )
        if recorded_sources != packaged:
            failures.append(
                f"{name} was not captured from the packaged release revisions "
                f"through {revision}; regenerate the snapshot"
            )
    return failures


def _chain_failures(
    heads: list[str], chain: list[list], *, baseline=RELEASED_CHAIN_BASE
) -> list[str]:
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

    for revision, down_revision in baseline:
        expected = () if down_revision is None else (down_revision,)
        if parents.get(revision) != expected:
            failures.append(
                "packaged revision graph must preserve released boundaries "
                f"{baseline}; {revision} has parents "
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

    core = baseline[-1][0]
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

    frozen = {revision for revision, _ in baseline}
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


def verify_artifact(
    artifact: Path, extract: Path, *, release_version: str | None = None
) -> tuple[list[str], str | None]:
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
        *_check_graph_runtime_payload(root),
        *_check_frozen_migrations(root),
        *graph_failures,
    ]
    if release_version is not None:
        problems.extend(_check_graph_release_bundle(root))
        problems.extend(_check_release_schema(root, artifact, head, release_version))
    if problems:
        return [f"{artifact.name}: {problem}" for problem in problems], None
    return [], (
        f"{artifact.name}: alembic head {head}, "
        f"{_revision_module_count(root)} packaged core revision modules, "
        f"{len(_frozen_digests(root))} frozen migration files intact, "
        "frontend bundle intact"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify the artifacts a release tag is about to publish."
    )
    parser.add_argument(
        "dist", help="path to the dist/ directory upload-artifact will publish"
    )
    parser.add_argument(
        "--release-version",
        help=(
            "require artifact metadata and effective schema boundary to match "
            "this tag version, with a recorded MCP tool contract and no "
            "unreleased migration or helper files"
        ),
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
            problems, line = verify_artifact(
                artifact,
                Path(workspace) / "artifact",
                release_version=arguments.release_version,
            )
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

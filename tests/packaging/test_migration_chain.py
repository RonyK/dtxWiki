"""Canonical release layout, single-head graph and frozen migration contracts."""

import ast
import hashlib
import importlib.util
import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.services.ops.alembic_migration_graph import code_revisions
from backend.services.ops.db_revision_inventory import RETIRED_REVISION_IDS
from backend.services.ops.revision_rename import LEGACY_REVISION_ALIASES
from scripts.new_migration import create_revision

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "backend" / "migrations" / "versions"
CORE_REVISION_DIR = (
    MIGRATIONS_DIR.parent / "revision_modules/v0_1/efcbcf724889_core_schema"
)
FRAGMENTS_DIR = MIGRATIONS_DIR.parent / "fragments"
FREEZE_MANIFEST = MIGRATIONS_DIR.parent / "frozen_revisions.json"
RELEASE_GATE = REPO_ROOT / "scripts" / "verify_wheel_artifact.py"
INITIAL_REVISION = "bd62193d5cf6"
# Frozen core schema boundary of the 0.1 release group.
CORE_REVISION = "efcbcf724889"
PUBLISHED_REVISION = "19db79d3e622"
RELEASE_014_REVISION = "ef53a0a7061c"
CURRENT_HEAD = "7e2b9c4d6a18"
ALEMBIC_REVISION_METADATA = {
    "revision",
    "down_revision",
    "branch_labels",
    "depends_on",
}
EXPECTED_REVISION_MODULES = {
    "context_data.py",
    "context_schema.py",
    "identity_access_schema.py",
    "library_media_schema.py",
    "library_policy.py",
    "operations_schema.py",
    "search_schema.py",
    "tenancy_schema.py",
    # Not a schema area: a shared helper that publishes a reciprocal FK pair's
    # parent unique key in the form each dialect needs.
    "unique_key_prepass.py",
    "workspace_schema.py",
}
SCAFFOLDING_PREFIX = "_dtxwiki_0002_"
LITERAL_TABLE_OPERATIONS = {
    "add_column",
    "batch_alter_table",
    "create_table",
    "drop_column",
    "drop_table",
}


def _script_directory() -> ScriptDirectory:
    cfg = Config(str(resolve_alembic_ini()))
    cfg.set_main_option("script_location", str(resolve_migrations_dir()))
    return ScriptDirectory.from_config(cfg)


def _revision_graph(script: ScriptDirectory) -> dict[str, object]:
    return {
        revision.revision: revision.down_revision
        for revision in script.walk_revisions()
    }


def test_graph_preserves_released_boundaries_and_appends_current_head():
    """Unreleased changes append after immutable published schema boundaries."""

    script = _script_directory()
    graph = _revision_graph(script)
    assert script.get_heads() == [CURRENT_HEAD]
    assert code_revisions() == set(graph)
    assert code_revisions().isdisjoint(RETIRED_REVISION_IDS)

    assert graph == {
        INITIAL_REVISION: None,
        CORE_REVISION: INITIAL_REVISION,
        PUBLISHED_REVISION: CORE_REVISION,
        RELEASE_014_REVISION: PUBLISHED_REVISION,
        CURRENT_HEAD: RELEASE_014_REVISION,
    }

    manifest = json.loads((MIGRATIONS_DIR.parent / "release_manifest.json").read_text())
    assert manifest["legacy_revision_aliases"] == LEGACY_REVISION_ALIASES
    assert manifest["schema_releases"]["0.1.2"]["head"] == CORE_REVISION
    assert manifest["schema_releases"]["0.1.3"]["head"] == PUBLISHED_REVISION
    assert manifest["schema_releases"]["0.1.4"]["head"] == RELEASE_014_REVISION
    for release, entry in manifest["schema_releases"].items():
        major, minor = release.split(".")[:2]
        assert entry["group"] == f"v{major}_{minor}"
        revision = script.get_revision(entry["head"])
        assert Path(revision.path).parent.name == entry["group"]


def test_versions_directory_holds_only_graph_revisions():
    graph = set(_revision_graph(_script_directory()))
    migration_files = {
        path.stem.split("_", 1)[0] for path in MIGRATIONS_DIR.rglob("*.py")
    }

    assert {"bd62193d5cf6", "efcbcf724889"} <= migration_files
    assert migration_files == graph
    for path in MIGRATIONS_DIR.rglob("*.py"):
        assert path.parent.parent == MIGRATIONS_DIR
        assert path.parent.name.startswith("v")
        assert path.stem.split("_", 1)[0] in graph


def _frozen_digests() -> dict[str, str]:
    return json.loads(FREEZE_MANIFEST.read_text(encoding="utf-8"))["sha256"]


def test_released_migration_files_match_the_freeze_manifest():
    """C11: a released revision is never edited in place; add a new one."""

    frozen = _frozen_digests()
    actual = {
        relative: hashlib.sha256(
            (MIGRATIONS_DIR.parent / relative).read_bytes()
        ).hexdigest()
        for relative in frozen
    }

    changed = sorted(
        relative for relative in frozen if actual[relative] != frozen[relative]
    )
    assert changed == [], (
        "frozen canonical migration files changed; revert them and put the "
        f"schema change in a new revision: {changed}"
    )


def test_the_freeze_covers_every_released_migration_file():
    released = {
        "revision_modules/__init__.py",
        "revision_modules/v0_1/__init__.py",
        "versions/v0_0/bd62193d5cf6_initial_schema.py",
        "versions/v0_1/efcbcf724889_core_schema.py",
        "versions/v0_1/19db79d3e622_release_0_1_3.py",
        "revision_modules/v0_1/content_event_backfill_v1.py",
        "revision_modules/v0_1/input_restrictions_v1.py",
        "versions/v0_1/ef53a0a7061c_index_change_operation_change_set_id.py",
        *(
            f"revision_modules/v0_1/efcbcf724889_core_schema/{path.name}"
            for path in CORE_REVISION_DIR.glob("*.py")
        ),
    }

    assert set(_frozen_digests()) == released


def test_the_release_gate_pins_the_same_digests_as_the_manifest():
    """Two copies on purpose: rehashing the manifest alone must not pass."""

    spec = importlib.util.spec_from_file_location("_release_gate", RELEASE_GATE)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(RELEASE_GATE.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(RELEASE_GATE.parent))

    assert module.FROZEN_MIGRATIONS == _frozen_digests()


def test_core_domain_modules_are_not_alembic_revisions():
    revision_files = sorted(CORE_REVISION_DIR.glob("*.py"))

    assert revision_files
    for revision_file in revision_files:
        tree = ast.parse(
            revision_file.read_text(encoding="utf-8"), filename=revision_file.name
        )
        assigned_names = {
            target.id
            for statement in tree.body
            if isinstance(statement, (ast.Assign, ast.AnnAssign))
            for target in (
                statement.targets
                if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            if isinstance(target, ast.Name)
        }
        assert assigned_names.isdisjoint(ALEMBIC_REVISION_METADATA), (
            f"{revision_file.name} must not declare Alembic revision metadata"
        )


def test_core_uses_revision_owned_domain_modules():
    assert {
        path.name
        for path in CORE_REVISION_DIR.glob("*.py")
        if path.name != "__init__.py"
    } == EXPECTED_REVISION_MODULES


def _module_level_string_constants(tree: ast.Module) -> dict[str, str]:
    """NAME -> "literal" for module-level `NAME = "literal"` assignments."""

    constants: dict[str, str] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
        else:
            continue
        value = statement.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                constants[target.id] = value.value
    return constants


def _literal_table_ddl_owners(sources) -> dict[str, set[str]]:
    """table name -> the modules that name it in a literal Alembic call.

    The first argument is resolved through the module's own string constants,
    not just `ast.Constant`: `op.add_column(_ASK_TURN_TABLE, ...)` is every bit
    as much a literal call as `op.add_column("ask_turn", ...)`, and reading
    only Constant nodes made five tables invisible to this guard.

    Names in the transient `_dtxwiki_0002_` namespace are excluded. Those are
    migration scaffolding, not feature tables -- see the module docstring of
    revision_modules/v0_1/efcbcf724889_core_schema/unique_key_prepass.py -- and
    `test_core_leaves_no_migration_scaffolding` checks their removal.
    """

    owners: dict[str, set[str]] = defaultdict(set)
    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=source.name)
        constants = _module_level_string_constants(tree)
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            function = call.func
            if (
                not isinstance(function, ast.Attribute)
                or not isinstance(function.value, ast.Name)
                or function.value.id != "op"
                or function.attr not in LITERAL_TABLE_OPERATIONS
                or not call.args
            ):
                continue
            argument = call.args[0]
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                table_name = argument.value
            elif isinstance(argument, ast.Name) and argument.id in constants:
                table_name = constants[argument.id]
            else:
                continue
            if table_name.startswith(SCAFFOLDING_PREFIX):
                continue
            owners[table_name].add(source.name)
    return owners


def test_core_literal_table_ddl_has_one_feature_owner():
    """Do not reintroduce multiple historical feature owners per table.

    ``tenancy_schema`` is the one deliberate cross-table finalizer: existing
    0001 rows need nullable routing columns populated before NOT NULL and
    workspace-scoped foreign keys can be installed. Its table names come from
    ``_TABLES`` rather than literal Alembic calls and are intentionally outside
    this feature-owner check.
    """

    owners = _literal_table_ddl_owners(
        [
            MIGRATIONS_DIR / "v0_1/efcbcf724889_core_schema.py",
            *CORE_REVISION_DIR.glob("*.py"),
        ]
    )

    duplicates = {
        table_name: sorted(source_names)
        for table_name, source_names in owners.items()
        if len(source_names) > 1
    }
    assert duplicates == {}


def test_core_owned_tables_declare_routing_columns_at_creation():
    """A table born in 0002 must not gain workspace_id from a later module."""

    expected = {
        "ask_turn_citation",
        "library_projection_outbox",
        "library_semantic_overlay",
        "page_provenance",
        "proposal_source_item",
    }
    sources = [
        MIGRATIONS_DIR / "v0_1/efcbcf724889_core_schema.py",
        *CORE_REVISION_DIR.glob("*.py"),
    ]
    declared: set[str] = set()
    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=source.name)
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            function = call.func
            if (
                not isinstance(function, ast.Attribute)
                or not isinstance(function.value, ast.Name)
                or function.value.id != "op"
                or function.attr != "create_table"
                or not call.args
                or not isinstance(call.args[0], ast.Constant)
                or call.args[0].value not in expected
            ):
                continue
            column_names = {
                argument.args[0].value
                for argument in call.args[1:]
                if isinstance(argument, ast.Call)
                and isinstance(argument.func, ast.Attribute)
                and argument.func.attr == "Column"
                and argument.args
                and isinstance(argument.args[0], ast.Constant)
            }
            if "workspace_id" in column_names:
                declared.add(call.args[0].value)

    assert declared == expected


def test_consolidated_revision_has_no_generic_fragments_package():
    assert not FRAGMENTS_DIR.exists()


def test_generated_revisions_group_patch_versions_and_link_in_order(tmp_path):
    """The developer helper creates linked scripts without opening a database."""

    target = tmp_path / "backend"
    target.mkdir()
    shutil.copy2(REPO_ROOT / "backend/alembic.ini", target / "alembic.ini")
    shutil.copytree(
        REPO_ROOT / "backend/migrations",
        target / "migrations",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    first = create_revision("0.2.0", "add workspace settings", root=tmp_path)
    second = create_revision("0.2.1", "add search index", root=tmp_path)
    assert first.down_revision == CURRENT_HEAD
    assert second.down_revision == first.revision
    assert first.revision != second.revision
    assert (
        Path(first.path).parent
        == Path(second.path).parent
        == target / "migrations/versions/v0_2"
    )
    assert Path(first.path).name.endswith("_add_workspace_settings.py")
    assert ScriptDirectory(
        str(target / "migrations"), recursive_version_locations=True
    ).get_heads() == [second.revision]

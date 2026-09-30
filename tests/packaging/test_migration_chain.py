"""Tests for the squashed Alembic baseline and its 0.1.1 freeze."""

import ast
import hashlib
import importlib.util
import json
import sys
from collections import defaultdict
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir
from backend.services.ops.db_revision_inventory import RETIRED_REVISION_IDS

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "backend" / "migrations" / "versions"
REVISION_0002_DIR = MIGRATIONS_DIR.parent / "revision_0002"
FRAGMENTS_DIR = MIGRATIONS_DIR.parent / "fragments"
FREEZE_MANIFEST = MIGRATIONS_DIR.parent / "frozen_revisions.json"
RELEASE_GATE = REPO_ROOT / "scripts" / "verify_wheel_artifact.py"
INITIAL_REVISION = "0001_initial_release"
# The last revision 0.1.1 published; later revisions continue after it.
EPIC1_REVISION = "0002_epic1"
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


def _base_first_chain(script: ScriptDirectory) -> list[tuple[str, object]]:
    return [
        (revision.revision, revision.down_revision)
        for revision in reversed(list(script.walk_revisions()))
    ]


def test_the_script_directory_has_exactly_one_head():
    script = _script_directory()

    assert len(script.get_heads()) == 1


def test_initial_release_has_no_down_revision():
    script = _script_directory()
    revision = script.get_revision(INITIAL_REVISION)

    assert revision.down_revision is None


def test_epic1_revision_is_direct_child_of_initial_release():
    script = _script_directory()
    revision = script.get_revision(EPIC1_REVISION)

    assert revision.down_revision == INITIAL_REVISION


def test_the_chain_is_linear_and_starts_with_the_released_revisions():
    """C11: later revisions extend the chain; none branches, merges or forks."""

    chain = _base_first_chain(_script_directory())

    assert chain[:2] == [
        (INITIAL_REVISION, None),
        (EPIC1_REVISION, INITIAL_REVISION),
    ]
    for (previous, _), (revision, down_revision) in zip(
        chain, chain[1:], strict=False
    ):
        assert down_revision == previous, (
            f"{revision} descends from {down_revision!r}, not {previous}"
        )


def test_versions_directory_holds_only_chain_revisions():
    chain = {revision for revision, _ in _base_first_chain(_script_directory())}
    migration_files = {path.stem for path in MIGRATIONS_DIR.glob("*.py")}

    assert {"0001_initial_release", "0002_epic1"} <= migration_files
    assert migration_files == chain


def test_no_revision_reuses_a_retired_identifier():
    """The preflight refuses retired stamps; reusing one would launder them."""

    chain = {revision for revision, _ in _base_first_chain(_script_directory())}

    assert chain.isdisjoint(RETIRED_REVISION_IDS)


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
        "released migration files changed after 0.1.1; revert them and put the "
        f"schema change in a new revision: {changed}"
    )


def test_the_freeze_covers_every_released_migration_file():
    released = {
        "versions/0001_initial_release.py",
        "versions/0002_epic1.py",
        *(f"revision_0002/{path.name}" for path in REVISION_0002_DIR.glob("*.py")),
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


def test_0002_domain_modules_are_not_alembic_revisions():
    revision_files = sorted(REVISION_0002_DIR.glob("*.py"))

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


def test_0002_uses_revision_owned_domain_modules():
    assert {
        path.name
        for path in REVISION_0002_DIR.glob("*.py")
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
    revision_0002/unique_key_prepass.py -- and
    `test_epic1_leaves_no_migration_scaffolding` is what holds them to account.
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


def test_0002_literal_table_ddl_has_one_feature_owner():
    """Do not reintroduce multiple historical feature owners per table.

    ``tenancy_schema`` is the one deliberate cross-table finalizer: existing
    0001 rows need nullable routing columns populated before NOT NULL and
    workspace-scoped foreign keys can be installed. Its table names come from
    ``_TABLES`` rather than literal Alembic calls and are intentionally outside
    this feature-owner check.
    """

    owners = _literal_table_ddl_owners(
        [MIGRATIONS_DIR / "0002_epic1.py", *REVISION_0002_DIR.glob("*.py")]
    )

    duplicates = {
        table_name: sorted(source_names)
        for table_name, source_names in owners.items()
        if len(source_names) > 1
    }
    assert duplicates == {}


def test_0002_owned_tables_declare_routing_columns_at_creation():
    """A table born in 0002 must not gain workspace_id from a later module."""

    expected = {
        "ask_turn_citation",
        "library_projection_outbox",
        "library_semantic_overlay",
        "page_provenance",
        "proposal_source_item",
    }
    sources = [MIGRATIONS_DIR / "0002_epic1.py", *REVISION_0002_DIR.glob("*.py")]
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

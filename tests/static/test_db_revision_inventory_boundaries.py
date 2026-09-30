from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
USE_CASE = ROOT / "backend" / "services" / "ops" / "db_revision_inventory.py"
SQL_ADAPTER = ROOT / "backend" / "services" / "ops" / "read_only_database.py"


def test_revision_inventory_use_case_has_only_inward_owned_ports():
    tree = ast.parse(USE_CASE.read_text(encoding="utf-8"))
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)

    forbidden = tuple(
        name
        for name in imports
        if name.startswith(("alembic", "sqlalchemy", "backend."))
    )
    assert forbidden == ()


def test_cli_is_the_revision_inventory_composition_root():
    source = (ROOT / "backend" / "cli" / "commands" / "db.py").read_text(
        encoding="utf-8"
    )
    assert "AlembicMigrationGraphSource()" in source
    assert "FilesystemBackupEvidenceSource()" in source
    assert "SqlAlchemyRevisionInventoryProbe()" in source


def test_checkpoint_probe_uses_inward_binding_and_never_immutable_sqlite():
    use_case = USE_CASE.read_text(encoding="utf-8")
    adapter = SQL_ADAPTER.read_text(encoding="utf-8")

    assert "class Sha256FileBinding" in use_case
    assert "class CheckpointProbeRequest" in use_case
    assert "PRAGMA journal_mode" in adapter
    assert "immutable=1" not in adapter

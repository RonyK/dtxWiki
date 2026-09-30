"""Alembic adapter for revision-inventory code-head evidence."""

from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from backend.services.ops.db_revision_inventory import is_revision_id

# backend/services/ops/ -> backend/migrations; resolved here rather than through
# backend.app.bootstrap, which imports the migration preflight that uses this.
_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


@lru_cache(maxsize=1)
def code_revisions() -> frozenset[str]:
    """Every revision in the migration script directory this code ships.

    Cached: the script directory of an installed release does not change while
    it runs.
    """

    return frozenset(
        revision.revision
        for revision in ScriptDirectory(str(_MIGRATIONS_DIR)).walk_revisions()
    )


class AlembicMigrationGraphSource:
    def current_heads(self) -> tuple[str, ...]:
        from backend.app.bootstrap import resolve_alembic_ini, resolve_migrations_dir

        config = Config(str(resolve_alembic_ini()))
        config.set_main_option("script_location", str(resolve_migrations_dir()))
        heads = tuple(sorted(ScriptDirectory.from_config(config).get_heads()))
        if any(not is_revision_id(head) for head in heads):
            raise ValueError("invalid code revision identifier")
        return heads

    def known_revisions(self) -> frozenset[str]:
        return code_revisions()

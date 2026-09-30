"""PEP 440-safe version parsing/comparison — the single source of version ordering.

Replaces the fragile dotted-int ``_version_tuple`` splitter (which threw on any
non-numeric segment like ``rc1``/``dev``/``+local`` and silently degraded to
``(0,)``, mis-ordering pre-releases and tripping the downversion fail-safe).

These helpers are intentionally dependency-light (only ``packaging``) and pure,
so they are reusable by ``bootstrap`` (downversion guard + migration status),
the ``dtxwiki info`` command, the update command, and the admin banner without
any circular import.
"""

from __future__ import annotations

from packaging.version import InvalidVersion, Version

#: All values ``classify_migration_status`` may return.
MIGRATION_STATUSES = frozenset(
    {"unbootstrapped", "synced", "downversion", "upgrade_pending", "drift"}
)


def parse_version(value: str | None) -> Version | None:
    """Parse ``value`` to a :class:`~packaging.version.Version`.

    Returns ``None`` for empty or unparseable input instead of raising, so
    callers can treat "can't tell" conservatively rather than crashing.
    """

    if not value:
        return None
    try:
        return Version(value)
    except (InvalidVersion, TypeError):
        return None


def is_newer(candidate: str | None, baseline: str | None) -> bool:
    """``True`` iff ``candidate`` parses to a *strictly* greater version.

    Unparseable inputs are treated as "not newer" so a non-numeric dev/local
    version never falsely trips a downversion refusal (the M11 fix). Use this for
    the downversion guard (``is_newer(db_version, code_version)``) and for the
    update-available check (``is_newer(latest, current)``).
    """

    parsed_candidate = parse_version(candidate)
    parsed_baseline = parse_version(baseline)
    if parsed_candidate is None or parsed_baseline is None:
        return False
    return parsed_candidate > parsed_baseline


def classify_migration_status(
    *,
    code_version: str,
    db_version: str | None,
    alembic_head: str | None,
    alembic_db: str | None,
) -> str:
    """Pure classification of the DB-vs-code state (no DB/alembic access).

    Mirrors the historical ``dtxwiki info`` logic, but orders versions via
    ``packaging`` instead of the dotted-int tuple. Returns one of
    :data:`MIGRATION_STATUSES`.
    """

    if db_version is None and alembic_db is None:
        return "unbootstrapped"
    if db_version == code_version and alembic_db == alembic_head:
        return "synced"
    if is_newer(db_version, code_version):
        return "downversion"
    if is_newer(code_version, db_version) or alembic_db != alembic_head:
        return "upgrade_pending"
    return "drift"

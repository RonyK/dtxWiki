"""Recognize one expected UNIQUE conflict without hiding other constraints."""

import sqlite3


def is_single_column_unique_violation(error, *, table: str, column: str) -> bool:
    original = error.orig
    if isinstance(original, sqlite3.IntegrityError):
        return (
            getattr(original, "sqlite_errorcode", None)
            == sqlite3.SQLITE_CONSTRAINT_UNIQUE
            and str(original) == f"UNIQUE constraint failed: {table}.{column}"
        )
    details = (
        original.args[0] if original.args and isinstance(original.args[0], dict) else {}
    )
    diagnostic = getattr(original, "diag", None)
    state = (
        getattr(original, "sqlstate", None)
        or getattr(original, "pgcode", None)
        or details.get("C")
    )
    constraint = getattr(diagnostic, "constraint_name", None) or details.get("n")
    return state == "23505" and constraint == f"{table}_{column}_key"

"""Database error classification for reserving an actor-bound review receipt."""

from sqlalchemy.exc import IntegrityError


def decision_id_conflict(error: IntegrityError) -> bool:
    """Recognize only the receipt primary key, without reading another tenant.

    SQLite identifies the failed column; pg8000 exposes the SQLSTATE and
    constraint name in its structured error. Other integrity failures retain
    their original meaning and must not be disguised as an idempotency clash.
    """
    original = error.orig
    if getattr(original, "sqlite_errorcode", None) in {1555, 2067}:
        return str(original) == "UNIQUE constraint failed: review_decision.id"
    details = original.args[0] if original.args else None
    return (
        isinstance(details, dict)
        and details.get("C") == "23505"
        and details.get("n") == "review_decision_pkey"
    )

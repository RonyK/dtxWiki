from sqlalchemy.exc import OperationalError

SCHEMA_MISMATCH_MESSAGE = (
    "Database schema is out of date for this dtxWiki build. For this "
    "pre-release dev build, stop the server, reset the dev database, run "
    "dtxwiki bootstrap, then retry."
)


def is_database_schema_mismatch(exc: Exception) -> bool:
    if not isinstance(exc, OperationalError):
        return False
    message = str(getattr(exc, "orig", exc)).lower()
    return any(
        marker in message
        for marker in (
            "no such column",
            "no such table",
            "has no column named",
            "undefined column",
            "undefined table",
        )
    )

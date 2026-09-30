"""Bound SQL parameters stay out of database errors (readiness minor s-7).

DBAPI errors reach the server log and tracebacks with their parameters
rendered, which for dtxWiki means password hashes, token digests or page text.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.app.db import make_engine


def test_database_errors_do_not_render_bound_parameters(tmp_path):
    engine = make_engine(f"sqlite+pysqlite:///{tmp_path / 'params.sqlite'}")
    secret = "argon2-hash-that-must-not-be-logged"
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE t (value TEXT UNIQUE)"))
            connection.execute(text("INSERT INTO t VALUES (:v)"), {"v": secret})
        with pytest.raises(IntegrityError) as excinfo:
            with engine.begin() as connection:
                connection.execute(text("INSERT INTO t VALUES (:v)"), {"v": secret})
    finally:
        engine.dispose()

    rendered = str(excinfo.value)
    assert "UNIQUE" in rendered
    assert secret not in rendered
    assert "hidden" in rendered

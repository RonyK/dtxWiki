"""Frozen CHECK expressions need one in-memory scenario, no Alembic/bootstrap.

Keep field cases together; do not multiply tests or databases per rule.
"""

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from backend.migrations.revision_modules.v0_1.input_restrictions_v1 import (
    RULES,
    StoredInputCheck,
)


def test_raw_storage_rejects_overflow_nul_and_blob():
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    probes = [
        (
            rule,
            sa.Table(
                f"probe_{rule.table}_{rule.field}",
                metadata,
                sa.Column(rule.field, sa.Text),
                sa.Column("node_kind", sa.Text),
                sa.CheckConstraint(StoredInputCheck(rule), name=rule.name),
            ),
        )
        for rule in RULES
    ]
    try:
        metadata.create_all(engine)
        for rule, table in probes:
            maximum = rule.maximum
            valid = "x" * maximum
            invalid = "x" * (maximum + 1)
            if rule.kind == "email":
                valid = "a" * 64 + "@" + "b" * 189
                invalid = "a" * 64 + "@" + "b" * 190
            with engine.begin() as connection:
                connection.execute(
                    table.insert().values(
                        {rule.field: valid, "node_kind": "saved_view"}
                    )
                )
            for value in (invalid, "x\x00" + "x" * maximum, b"blob"):
                with (
                    pytest.raises(IntegrityError, match=rule.name),
                    engine.begin() as connection,
                ):
                    connection.execute(
                        table.insert().values(
                            {rule.field: value, "node_kind": "saved_view"}
                        )
                    )
            if rule.nullable:
                with engine.begin() as connection:
                    connection.execute(table.insert().values({rule.field: None}))
            if rule.kind in {"line", "tree", "email"}:
                for value in (" x", "x\t", "x\u0085x", "x\u2028x"):
                    with (
                        pytest.raises(IntegrityError, match=rule.name),
                        engine.begin() as connection,
                    ):
                        connection.execute(
                            table.insert().values(
                                {rule.field: value, "node_kind": "saved_view"}
                            )
                        )
    finally:
        engine.dispose()

"""enforce input storage restrictions

Revision ID: 19db79d3e622
Revises: 49d9c0776c5a
Create Date: 2026-10-20 22:58:47.475913
"""

from alembic import op

from backend.migrations.revision_modules.v0_1.input_restrictions_v1 import (
    RULES,
    audit,
    rule_sql,
)

revision = "19db79d3e622"
down_revision = "49d9c0776c5a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    violations = audit(connection)
    if violations:
        raise RuntimeError(
            "input restrictions preflight failed; use inputs audit and explicit "
            f"UUID maintenance repairs before upgrading: {violations}"
        )
    _change_checks(add=True)


def downgrade() -> None:
    _change_checks(add=False)


def _change_checks(*, add: bool) -> None:
    dialect = op.get_bind().dialect.name
    for table in dict.fromkeys(rule.table for rule in RULES):
        rules = [rule for rule in RULES if rule.table == table]
        if dialect == "sqlite":
            with op.batch_alter_table(table, recreate="always") as batch:
                for rule in rules:
                    if add:
                        batch.create_check_constraint(
                            op.f(rule.name), rule_sql(rule, dialect)
                        )
                    else:
                        batch.drop_constraint(op.f(rule.name), type_="check")
        else:
            for rule in rules:
                if add:
                    op.create_check_constraint(
                        op.f(rule.name), table, rule_sql(rule, dialect)
                    )
                else:
                    op.drop_constraint(op.f(rule.name), table, type_="check")

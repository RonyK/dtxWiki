"""Frozen storage contract for the input-restrictions revision.

Keep this version independent of application validators: an old migration must
continue to mean the same thing after the product's next policy change.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import ColumnElement

REVISION = "19db79d3e622"
TRIM = (
    " \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
    "\u2008\u2009\u200a\u202f\u205f\u3000"
)
CONTROLS = (*range(32), *range(127, 160), 0x2028, 0x2029)


@dataclass(frozen=True)
class Rule:
    table: str
    field: str
    maximum: int
    kind: str = "line"
    nullable: bool = False
    minimum: int = 1

    @property
    def name(self) -> str:
        return f"ck_{self.table}_{self.field}_input_v1"


RULES = (
    Rule("users", "display_name", 32),
    Rule("users", "email", 254, "email"),
    Rule("api_tokens", "name", 128),
    Rule("workspace", "title", 128),
    Rule("workspace", "name", 128),
    Rule("workspace", "goal", 512, "text", minimum=0),
    Rule("workspace", "slug", 80, "slug"),
    Rule("workspace_join_requests", "decision_note", 500, "text", True, 0),
    Rule("context_page", "title", 256),
    Rule("agent_page", "title", 256),
    Rule("ask_session", "title", 256),
    Rule("library_item", "title", 256, nullable=True),
    Rule("mcp_chat_session", "title", 120, nullable=True),
    Rule("workspace_tree_node", "title", 256, "tree"),
    Rule("workspace_tree_node", "nav_summary", 160, "text", True, 0),
)


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def rule_sql(rule: Rule, dialect: str) -> str:
    """Return SQL over fixed identifiers, never over operator-provided SQL."""
    column = '"' + rule.field + '"'
    sqlite = dialect == "sqlite"
    position = "instr" if sqlite else "strpos"
    character = "char" if sqlite else "chr"
    def octets(value):
        return f"length(CAST({value} AS BLOB))" if sqlite else f"octet_length({value})"
    parts = [f"typeof({column}) = 'text'"] if sqlite else []
    maximum = (
        "CASE WHEN node_kind = 'folder' THEN 80 ELSE 256 END"
        if rule.kind == "tree"
        else str(rule.maximum)
    )
    parts.append(f"length({column}) BETWEEN {rule.minimum} AND {maximum}")
    if sqlite:
        parts.append(f"instr({column}, char(0)) = 0")
    if rule.kind in {"line", "tree", "email"}:
        # PostgreSQL text rejects NUL and invalid UTF-8 before CHECK execution.
        parts.extend(
            f"{position}({column}, {character}({code})) = 0"
            for code in CONTROLS
            if code != 0
        )
        parts.extend(
            [
                f"substr({column}, 1, 1) NOT IN ({', '.join(map(_literal, TRIM))})",
                f"substr({column}, length({column}), 1) "
                f"NOT IN ({', '.join(map(_literal, TRIM))})",
            ]
        )
    if rule.kind == "email":
        at = f"{position}({column}, '@')"
        parts.extend(
            [
                f"{octets(column)} <= 254",
                f"{at} > 1",
                f"{at} < length({column})",
                f"length({column}) - length(replace({column}, '@', '')) = 1",
                f"{octets(f'substr({column}, 1, {at} - 1)')} <= 64",
            ]
        )
        parts.extend(f"{position}({column}, {_literal(space)}) = 0" for space in TRIM)
        parts.extend(
            f"{position}({column}, {_literal(mark)}) = 0" for mark in '<>,;"'
        )
    if rule.kind == "slug":
        parts.extend(
            [
                f"{column} NOT GLOB '*[^a-z0-9-]*'"
                if sqlite
                else f"{column} ~ '^[a-z0-9-]+$'",
                f"substr({column}, 1, 1) != '-'",
                f"substr({column}, length({column}), 1) != '-'",
                f"{position}({column}, '--') = 0",
            ]
        )
    expression = " AND ".join(parts)
    return f"({column} IS NULL OR ({expression}))" if rule.nullable else expression


class StoredInputCheck(ColumnElement):
    type = sa.Boolean()
    inherit_cache = False

    def __init__(self, rule: Rule):
        self.rule = rule


@compiles(StoredInputCheck)
def _compile_check(element, compiler, **_kwargs):
    return rule_sql(element.rule, compiler.dialect.name)


def constraints_for(table: str):
    return [
        sa.CheckConstraint(StoredInputCheck(rule), name=rule.name)
        for rule in RULES
        if rule.table == table
    ]


def check_encoding(connection) -> None:
    if connection.dialect.name == "sqlite":
        encoding = connection.exec_driver_sql("PRAGMA encoding").scalar_one()
    elif connection.dialect.name == "postgresql":
        encoding = connection.exec_driver_sql("SHOW server_encoding").scalar_one()
    else:
        raise RuntimeError("input storage restrictions require SQLite or PostgreSQL")
    if encoding.upper().replace("-", "") != "UTF8":
        raise RuntimeError("input storage restrictions require a UTF-8 database")


def audit(connection, *, allow_missing: bool = False) -> list[dict]:
    """Count violations; only bounded IDs and rule names leave the database."""
    check_encoding(connection)
    tables = set(sa.inspect(connection).get_table_names())
    result = []
    for rule in RULES:
        if rule.table not in tables and allow_missing:
            continue
        expression = rule_sql(rule, connection.dialect.name)
        predicate = f"NOT ({expression})"
        count = connection.execute(
            sa.text(f'SELECT count(*) FROM "{rule.table}" WHERE {predicate}')
        ).scalar_one()
        if count:
            ids = (
                connection.execute(
                    sa.text(
                        f'SELECT id FROM "{rule.table}" WHERE {predicate} '
                        "ORDER BY id LIMIT 5"
                    )
                )
                .scalars()
                .all()
            )
            result.append(
                {"rule": rule.name, "count": count, "ids": [str(i) for i in ids]}
            )
    return result

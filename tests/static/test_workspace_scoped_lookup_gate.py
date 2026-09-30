"""Phase E characterization gate for workspace-plane ORM lookups.

The baseline is intentionally not an authorization allowlist. It is a stable
inventory that must shrink as Phase E moves reads behind scoped repositories.
Unlike the first gate, it preserves multiplicity and a normalized AST/SQL
fingerprint within each lexical scope, so adding another lookup or weakening
an existing predicate is a new violation. It also inventories the common
``select``/``query``/``where``/``join`` and raw-SQL shapes required by Epic-3
D-6, including physical search tables outside ``Base.metadata``.

Composite-primary-key ``Session.get``/``get_one`` calls are excluded only when
the supplied identity is statically a complete tuple/list or mapping that
includes the ``workspace_id`` primary-key component. Workspace singleton rows
whose sole primary key is ``workspace_id`` remain intrinsically scoped.
Control- and mixed-plane models are excluded from this workspace-repository
gate and governed by their own locator/placement contracts in
``backend.tenancy_manifest``.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

import backend.models  # noqa: F401 -- populate Base.registry/Base.metadata
from backend.models.base import Base
from backend.tenancy_manifest import WORKSPACE_PLANE_TABLES, WORKSPACE_RAW_SQL_TABLES
from tests.static._python_ast import ROOT, parse_python_file
from tests.static.test_architecture_boundaries import (
    iter_python_files,
    write_fixture,
)

BASELINE_PATH = Path(__file__).with_name("workspace_lookup_baseline.json")
_QUERY_METHODS = frozenset(
    {
        "filter",
        "filter_by",
        "join",
        "outerjoin",
        "query",
        "select_from",
        "where",
    }
)
_QUERY_FUNCTIONS = frozenset({"query", "select"})
_GET_METHODS = frozenset({"get", "get_one"})


def _workspace_models() -> dict[str, object]:
    return {
        mapper.class_.__name__: mapper
        for mapper in Base.registry.mappers
        if mapper.local_table.name in WORKSPACE_PLANE_TABLES
    }


WORKSPACE_MODEL_MAPPERS = _workspace_models()
WORKSPACE_MODEL_NAMES = frozenset(WORKSPACE_MODEL_MAPPERS)
WORKSPACE_MODEL_PRIMARY_KEYS = {
    name: tuple(column.name for column in mapper.primary_key)
    for name, mapper in WORKSPACE_MODEL_MAPPERS.items()
}
SINGLE_ID_WORKSPACE_MODEL_NAMES = frozenset(
    name
    for name, primary_key in WORKSPACE_MODEL_PRIMARY_KEYS.items()
    if primary_key == ("id",)
)
WORKSPACE_SQL_TABLE_TO_TARGET = {
    mapper.local_table.name: name for name, mapper in WORKSPACE_MODEL_MAPPERS.items()
}
WORKSPACE_SQL_TABLE_TO_TARGET.update(
    {table_name: table_name for table_name in WORKSPACE_RAW_SQL_TABLES}
)
_SQL_TABLE_REFERENCE_PREFIX = (
    r"(?:\bfrom|\bjoin|\bupdate|\binto|\bdelete\s+from|"
    r"\bcreate\s+(?:virtual\s+)?table(?:\s+if\s+not\s+exists)?|"
    r"\bdrop\s+table(?:\s+if\s+exists)?)"
)


def workspace_lookup_call_sites(*, root: Path = ROOT) -> Counter[str]:
    """Return stable ``path|scope|kind|target|fingerprint`` counts."""

    hits: Counter[str] = Counter()
    for source_file in iter_python_files("backend", root=root):
        relative = source_file.relative_to(root).as_posix()
        parts = relative.split("/")
        if "tests" in parts or "migrations" in parts:
            continue
        tree = parse_python_file(source_file, root=root)
        parents = _parent_map(tree)
        aliases = _model_aliases(tree)
        query_function_aliases = _query_function_aliases(tree)
        string_constants = _module_string_constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            scope = _lexical_scope(node, parents)
            for kind, target, fingerprint in _lookup_targets(
                node, aliases, query_function_aliases, string_constants
            ):
                hits[f"{relative}|{scope}|{kind}|{target}|{fingerprint}"] += 1
    return hits


def _lookup_targets(
    node: ast.Call,
    aliases: dict[str, str],
    query_function_aliases: dict[str, str],
    string_constants: dict[str, str],
) -> set[tuple[str, str, str]]:
    matches: set[tuple[str, str, str]] = set()
    function_name = _query_call_name(node.func, query_function_aliases)
    call_fingerprint = _ast_fingerprint(node)

    if function_name in _GET_METHODS:
        entity = _call_argument(node, position=0, keyword="entity")
        identity = _call_argument(node, position=1, keyword="ident")
        if entity is not None:
            for model in _models_in_expression(entity, aliases):
                if model in SINGLE_ID_WORKSPACE_MODEL_NAMES or not (
                    _is_complete_workspace_primary_key(model, identity)
                ):
                    matches.add((function_name, model, call_fingerprint))

    if function_name in _QUERY_FUNCTIONS or function_name in _QUERY_METHODS:
        expressions = [*node.args, *[item.value for item in node.keywords]]
        # ``filter_by`` names mapped attributes rather than spelling the model
        # in its predicate. Its receiver is therefore the only local evidence
        # of the query root, and is part of the fingerprint as well.
        if function_name == "filter_by" and isinstance(node.func, ast.Attribute):
            expressions.append(node.func.value)
        for expression in expressions:
            matches.update(
                (function_name, model, call_fingerprint)
                for model in _models_in_expression(expression, aliases)
            )

    if function_name in {"exec_driver_sql", "text"} and node.args:
        sql = _static_string(node.args[0], string_constants)
        if sql is not None:
            lowered = sql.lower()
            sql_fingerprint = hashlib.sha256(sql.encode("utf-8")).hexdigest()[:16]
            for table_name, target in WORKSPACE_SQL_TABLE_TO_TARGET.items():
                table_pattern = (
                    rf"{_SQL_TABLE_REFERENCE_PREFIX}\s+[\"`\[]?"
                    rf"{re.escape(table_name)}(?![a-z0-9_])"
                )
                if re.search(table_pattern, lowered):
                    matches.add(("raw_sql", target, sql_fingerprint))

    return matches


def _call_argument(
    node: ast.Call,
    *,
    position: int,
    keyword: str,
) -> ast.AST | None:
    if len(node.args) > position:
        return node.args[position]
    return next(
        (item.value for item in node.keywords if item.arg == keyword),
        None,
    )


def _is_complete_workspace_primary_key(
    model: str,
    identity: ast.AST | None,
) -> bool:
    """Whether ``identity`` statically supplies every workspace PK column.

    SQLAlchemy accepts composite identities as a positional tuple/list or as
    a mapping keyed by mapped primary-key attribute. Dynamic containers are
    intentionally not assumed safe: the characterization gate must inventory
    a shape it cannot prove includes ``workspace_id``.
    """

    primary_key = WORKSPACE_MODEL_PRIMARY_KEYS.get(model, ())
    if "workspace_id" not in primary_key:
        return False

    if primary_key == ("workspace_id",):
        return identity is not None and _mentions_workspace_identity(identity)

    if isinstance(identity, (ast.Tuple, ast.List)):
        return (
            len(identity.elts) == len(primary_key)
            and not any(isinstance(item, ast.Starred) for item in identity.elts)
            and _mentions_workspace_identity(
                identity.elts[primary_key.index("workspace_id")]
            )
        )

    mapping: dict[str, ast.AST] | None = None
    if isinstance(identity, ast.Dict):
        mapping = {}
        for key, value in zip(identity.keys, identity.values, strict=True):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                return False
            mapping[key.value] = value
    elif (
        isinstance(identity, ast.Call)
        and isinstance(identity.func, ast.Name)
        and identity.func.id == "dict"
        and not identity.args
        and all(item.arg is not None for item in identity.keywords)
    ):
        mapping = {
            item.arg: item.value for item in identity.keywords if item.arg is not None
        }

    return (
        mapping is not None
        and set(mapping) == set(primary_key)
        and _mentions_workspace_identity(mapping["workspace_id"])
    )


def _mentions_workspace_identity(expression: ast.AST) -> bool:
    """Whether an explicit key expression visibly carries workspace scope.

    This is intentionally conservative. A dynamic tuple may be correct at
    runtime, but Phase E must inventory identities whose workspace component
    cannot be reviewed from the call site.
    """

    for node in ast.walk(expression):
        if isinstance(node, ast.Name) and (
            node.id == "workspace_id" or node.id.endswith("_workspace_id")
        ):
            return True
        if isinstance(node, ast.Attribute):
            if node.attr == "workspace_id" or node.attr.endswith("_workspace_id"):
                return True
            if (
                node.attr == "id"
                and isinstance(node.value, ast.Name)
                and node.value.id.endswith("workspace")
            ):
                return True
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == "workspace_id"
        ):
            return True
    return False


def _ast_fingerprint(node: ast.AST) -> str:
    normalized = ast.dump(node, annotate_fields=True, include_attributes=False)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    constants: dict[str, str] = {}
    changed = True
    while changed:
        changed = False
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = _static_string(node.value, constants)
            if value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and constants.get(target.id) != value:
                    constants[target.id] = value
                    changed = True
    return constants


def _static_string(node: ast.AST | None, constants: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_string(node.left, constants)
        right = _static_string(node.right, constants)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                resolved = _static_string(value.value, constants)
                if resolved is None:
                    return None
                parts.append(resolved)
            else:
                return None
        return "".join(parts)
    return None


def _call_name(function: ast.expr) -> str | None:
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        return function.attr
    return None


def _query_call_name(
    function: ast.expr,
    aliases: dict[str, str],
) -> str | None:
    if isinstance(function, ast.Name):
        return aliases.get(function.id, function.id)
    if isinstance(function, ast.Attribute):
        return "query" if function.attr == "Query" else function.attr
    return None


def _models_in_expression(expression: ast.AST, aliases: dict[str, str]) -> set[str]:
    models = set()
    for node in ast.walk(expression):
        if isinstance(node, ast.Name):
            model = aliases.get(node.id)
            if model is not None:
                models.add(model)
        elif isinstance(node, ast.Attribute) and node.attr in WORKSPACE_MODEL_NAMES:
            models.add(node.attr)
    return models


def _model_aliases(tree: ast.AST) -> dict[str, str]:
    aliases = {name: name for name in WORKSPACE_MODEL_NAMES}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for imported in node.names:
                if imported.name in WORKSPACE_MODEL_NAMES:
                    aliases[imported.asname or imported.name] = imported.name

    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            model_name = None
            if isinstance(value, ast.Name) and value.id in aliases:
                model_name = aliases[value.id]
            elif (
                isinstance(value, ast.Call)
                and _call_name(value.func) == "aliased"
                and value.args
            ):
                referenced = _models_in_expression(value.args[0], aliases)
                if len(referenced) == 1:
                    model_name = referenced.pop()
            if model_name is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in aliases:
                    aliases[target.id] = model_name
                    changed = True
    return aliases


def _query_function_aliases(tree: ast.AST) -> dict[str, str]:
    """Return local names for imported SQLAlchemy query constructors."""

    aliases = {name: name for name in _QUERY_FUNCTIONS}
    aliases["Query"] = "query"
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module is None or not (
            node.module == "sqlalchemy" or node.module.startswith("sqlalchemy.")
        ):
            continue
        for imported in node.names:
            canonical_name = "query" if imported.name == "Query" else imported.name
            if canonical_name in _QUERY_FUNCTIONS:
                aliases[imported.asname or imported.name] = canonical_name

    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            canonical_name = None
            if isinstance(value, ast.Name) and value.id in aliases:
                canonical_name = aliases[value.id]
            elif isinstance(value, ast.Attribute):
                called_name = "query" if value.attr == "Query" else value.attr
                if called_name in _QUERY_FUNCTIONS:
                    canonical_name = called_name
            if canonical_name is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in aliases:
                    aliases[target.id] = canonical_name
                    changed = True
    return aliases


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }


def _lexical_scope(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    names = []
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(current.name)
        current = parents.get(current)
    return ".".join(reversed(names)) or "<module>"


def _load_baseline() -> Counter[str]:
    payload = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    return Counter({str(key): int(value) for key, value in payload.items()})


def test_workspace_lookup_inventory_matches_phase_e_baseline():
    expected = _load_baseline()
    actual = workspace_lookup_call_sites()
    added = actual - expected
    removed = expected - actual
    assert not added and not removed, (
        "Workspace lookup inventory changed. New lookups must use a scoped "
        "repository; removed lookups must be pruned from the Phase E baseline. "
        f"added={dict(added)}, removed={dict(removed)}"
    )


def test_gate_detects_another_lookup_for_an_existing_file_model_pair(
    tmp_path: Path,
):
    path = "backend/services/existing.py"
    write_fixture(
        tmp_path,
        path,
        "def one(session, page_id):\n    return session.get(ContextPage, page_id)\n",
    )
    before = workspace_lookup_call_sites(root=tmp_path)
    write_fixture(
        tmp_path,
        path,
        "def one(session, page_id):\n"
        "    first = session.get(ContextPage, page_id)\n"
        "    second = session.get(ContextPage, page_id)\n"
        "    return first or second\n",
    )
    after = workspace_lookup_call_sites(root=tmp_path)
    assert sum(after.values()) == sum(before.values()) + 1


def test_gate_detects_get_and_get_one_keyword_entity_arguments(tmp_path: Path):
    write_fixture(
        tmp_path,
        "backend/services/keyword_get.py",
        "def read(session, page_id):\n"
        "    one = session.get(entity=ContextPage, ident=page_id)\n"
        "    two = session.get_one(entity=ContextPage, ident=page_id)\n"
        "    return one, two\n",
    )
    hits = workspace_lookup_call_sites(root=tmp_path)
    assert sum(hits.values()) == 2
    assert any("|get|ContextPage|" in key for key in hits)
    assert any("|get_one|ContextPage|" in key for key in hits)


def test_gate_detects_select_clause_and_literal_raw_sql(tmp_path: Path):
    write_fixture(
        tmp_path,
        "backend/services/unsafe_queries.py",
        "def read(session, page_id):\n"
        "    one = session.scalar(\n"
        "        select(ContextPage).where(ContextPage.id == page_id)\n"
        "    )\n"
        "    two = session.execute(text('SELECT * FROM context_page'))\n"
        "    return one, two\n",
    )
    hits = workspace_lookup_call_sites(root=tmp_path)
    assert any("|select|ContextPage" in key for key in hits)
    assert any("|where|ContextPage" in key for key in hits)
    assert any("|raw_sql|ContextPage" in key for key in hits)


def test_gate_detects_same_count_predicate_weakening(tmp_path: Path):
    path = "backend/services/predicate_change.py"
    write_fixture(
        tmp_path,
        path,
        "def read(session, workspace_id):\n"
        "    return session.scalar(\n"
        "        select(ContextPage).where(\n"
        "            ContextPage.workspace_id == workspace_id\n"
        "        )\n"
        "    )\n",
    )
    before = workspace_lookup_call_sites(root=tmp_path)
    write_fixture(
        tmp_path,
        path,
        "def read(session, page_id):\n"
        "    return session.scalar(\n"
        "        select(ContextPage).where(ContextPage.id == page_id)\n"
        "    )\n",
    )
    after = workspace_lookup_call_sites(root=tmp_path)
    assert sum(before.values()) == sum(after.values())
    assert before != after
    assert after - before


def test_gate_fingerprints_filter_by_receiver_and_workspace_predicate(
    tmp_path: Path,
):
    path = "backend/services/filter_by_change.py"
    write_fixture(
        tmp_path,
        path,
        "def read(session, page_id, workspace_id):\n"
        "    return session.query(ContextPage).filter_by(\n"
        "        id=page_id, workspace_id=workspace_id\n"
        "    ).one_or_none()\n",
    )
    before = workspace_lookup_call_sites(root=tmp_path)
    write_fixture(
        tmp_path,
        path,
        "def read(session, page_id):\n"
        "    return session.query(ContextPage).filter_by(\n"
        "        id=page_id\n"
        "    ).one_or_none()\n",
    )
    after = workspace_lookup_call_sites(root=tmp_path)
    assert sum(before.values()) == sum(after.values())
    assert any("|filter_by|ContextPage|" in key for key in before)
    assert any("|filter_by|ContextPage|" in key for key in after)
    assert after - before


def test_gate_detects_raw_search_tables_and_exec_driver_sql(tmp_path: Path):
    write_fixture(
        tmp_path,
        "backend/services/raw_search.py",
        'SEARCH_TABLE = "search_doc_fts"\n'
        'QUERY = f"SELECT * FROM {SEARCH_TABLE}"\n'
        "def read(connection):\n"
        "    one = connection.exec_driver_sql(QUERY)\n"
        "    two = connection.execute(\n"
        "        text('SELECT * FROM page_name_trgm')\n"
        "    )\n"
        "    return one, two\n",
    )
    hits = workspace_lookup_call_sites(root=tmp_path)
    assert any("|raw_sql|search_doc_fts|" in key for key in hits)
    assert any("|raw_sql|page_name_trgm|" in key for key in hits)


def test_gate_does_not_treat_sql_string_values_as_table_references(tmp_path: Path):
    write_fixture(
        tmp_path,
        "backend/services/raw_string_values.py",
        "def read(session):\n"
        "    return session.execute(text(\n"
        '        "SELECT * FROM search_doc_fts "\n'
        "        \"WHERE source_kind IN ('context_claim', 'agent_page')\"\n"
        "    ))\n",
    )
    hits = workspace_lookup_call_sites(root=tmp_path)
    assert sum(hits.values()) == 1
    assert any("|raw_sql|search_doc_fts|" in key for key in hits)
    assert not any("|ContextClaim|" in key for key in hits)
    assert not any("|AgentPage|" in key for key in hits)


def test_gate_resolves_sqlalchemy_aliased_models(tmp_path: Path):
    write_fixture(
        tmp_path,
        "backend/services/aliased_query.py",
        "def read(session, page_id):\n"
        "    page = aliased(ContextPage)\n"
        "    return session.scalar(select(page).where(page.id == page_id))\n",
    )
    hits = workspace_lookup_call_sites(root=tmp_path)
    assert any("|select|ContextPage" in key for key in hits)
    assert any("|where|ContextPage" in key for key in hits)


def test_gate_resolves_imported_query_constructor_aliases(tmp_path: Path):
    write_fixture(
        tmp_path,
        "backend/services/query_aliases.py",
        "import sqlalchemy as sa\n"
        "from sqlalchemy import select as sa_select\n"
        "from sqlalchemy.orm import Query as ORMQuery\n"
        "make_select = sa_select\n"
        "make_attribute_select = sa.select\n"
        "def read(session):\n"
        "    one = session.scalars(sa_select(ContextPage))\n"
        "    two = session.scalars(make_select(ContextPage))\n"
        "    three = ORMQuery(ContextPage, session=session).all()\n"
        "    four = session.scalars(make_attribute_select(ContextPage))\n"
        "    make_query = session.query\n"
        "    five = make_query(ContextPage).all()\n"
        "    return one, two, three, four, five\n",
    )
    hits = workspace_lookup_call_sites(root=tmp_path)
    assert sum(hits.values()) == 5
    assert (
        sum(count for key, count in hits.items() if "|select|ContextPage|" in key) == 3
    )
    assert (
        sum(count for key, count in hits.items() if "|query|ContextPage|" in key) == 2
    )


def test_gate_excludes_complete_workspace_composite_primary_key_gets(
    tmp_path: Path,
):
    write_fixture(
        tmp_path,
        "backend/services/composite_lookup.py",
        "def read(session, workspace_id, asset_id):\n"
        "    one = session.get(\n"
        "        ContextMediaAsset, (workspace_id, asset_id)\n"
        "    )\n"
        "    two = session.get(\n"
        "        entity=ContextMediaAsset,\n"
        "        ident={'workspace_id': workspace_id, 'id': asset_id},\n"
        "    )\n"
        "    three = session.get_one(\n"
        "        ContextMediaAsset,\n"
        "        dict(workspace_id=workspace_id, id=asset_id),\n"
        "    )\n"
        "    return one, two, three\n",
    )
    assert workspace_lookup_call_sites(root=tmp_path) == Counter()


def test_gate_excludes_workspace_primary_key_singleton_get(tmp_path: Path):
    write_fixture(
        tmp_path,
        "backend/services/workspace_singleton_lookup.py",
        "def read(session, workspace_id):\n"
        "    return session.get(WorkspaceMediaWriteIntent, workspace_id)\n",
    )
    assert workspace_lookup_call_sites(root=tmp_path) == Counter()


def test_gate_detects_unproved_workspace_composite_primary_key_gets(
    tmp_path: Path,
):
    write_fixture(
        tmp_path,
        "backend/services/incomplete_composite_lookup.py",
        "def read(session, asset_id, identity, workspace_id):\n"
        "    one = session.get(ContextMediaAsset, asset_id)\n"
        "    two = session.get(ContextMediaAsset, (asset_id,))\n"
        "    three = session.get(\n"
        "        entity=ContextMediaAsset, ident={'id': asset_id}\n"
        "    )\n"
        "    four = session.get_one(ContextMediaAsset, identity)\n"
        "    five = session.get(\n"
        "        ContextMediaAsset, (asset_id, workspace_id)\n"
        "    )\n"
        "    six = session.get(\n"
        "        ContextMediaAsset,\n"
        "        {'workspace_id': asset_id, 'id': asset_id},\n"
        "    )\n"
        "    return one, two, three, four, five, six\n",
    )
    hits = workspace_lookup_call_sites(root=tmp_path)
    assert sum(hits.values()) == 6
    assert (
        sum(count for key, count in hits.items() if "|get|ContextMediaAsset|" in key)
        == 5
    )
    assert any("|get_one|ContextMediaAsset|" in key for key in hits)


def test_gate_ignores_backend_tests_and_migrations(tmp_path: Path):
    source = (
        "def read(session, page_id):\n    return session.get(ContextPage, page_id)\n"
    )
    write_fixture(tmp_path, "backend/tests/unit/test_x.py", source)
    write_fixture(tmp_path, "backend/migrations/versions/9999_x.py", source)
    assert workspace_lookup_call_sites(root=tmp_path) == Counter()

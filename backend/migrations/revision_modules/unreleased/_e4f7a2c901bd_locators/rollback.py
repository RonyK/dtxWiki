"""Private, workspace-owned deltas for reversing the locator-only conversion."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from uuid import UUID, uuid4

import sqlalchemy as sa

TABLE = "document_locator_rollback"
_COLUMNS = {
    "client_delegated_task": ("payload",),
    "lint_finding": ("message_args", "suggested_action", "target_id"),
    "knowledge_coverage_gap": ("nearest_candidates", "rewrite_attempts"),
    "ask_turn": ("retrieval_trace",),
    "mcp_write_idempotency": ("result_payload",),
}
_MISSING = object()


def _json(value):
    if isinstance(value, (UUID, datetime)):
        return str(value)
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return value


def _table(connection, name):
    return sa.Table(name, sa.MetaData(), autoload_with=connection)


def _key(connection, value):
    return (
        UUID(str(value)).hex
        if connection.dialect.name == "sqlite"
        else UUID(str(value))
    )


def _values(table, row):
    result = dict(row)
    for name, value in row.items():
        if value is None:
            continue
        if isinstance(table.c[name].type, sa.DateTime):
            result[name] = datetime.fromisoformat(value)
        elif isinstance(table.c[name].type, sa.Uuid):
            result[name] = UUID(value)
    return result


def _diff(before, after, path=()):
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        changes = []
        for name in before.keys() | after.keys():
            old, new = before.get(name, _MISSING), after.get(name, _MISSING)
            if old is not _MISSING and new is not _MISSING:
                changes.extend(_diff(old, new, (*path, name)))
            elif old != new:
                changes.append(
                    {
                        "path": [*path, name],
                        "before_present": old is not _MISSING,
                        "before": None if old is _MISSING else old,
                        "after_present": new is not _MISSING,
                        "after": None if new is _MISSING else new,
                    }
                )
        guard = {
            name: after[name]
            for name in (
                "id",
                "kind",
                "type",
                "page_id",
                "library_item_id",
                "expected_id",
                "expected_kind",
                "stable_url",
                "locator",
            )
            if name in after
        }
        for change in changes:
            if len(change["path"]) == len(path) + 1:
                change["guard_path"], change["guard"] = list(path), guard
        return changes
    if (
        isinstance(before, list)
        and isinstance(after, list)
        and len(before) == len(after)
    ):
        return [
            change
            for index, (old, new) in enumerate(zip(before, after, strict=True))
            for change in _diff(old, new, (*path, index))
        ]
    return [
        {
            "path": list(path),
            "before_present": True,
            "before": before,
            "after_present": True,
            "after": after,
        }
    ]


def _undo(value, changes):
    result = copy.deepcopy(value)
    for change in changes:
        path = change["path"]
        parent = result
        try:
            if "guard" in change:
                guarded = value
                for part in change["guard_path"]:
                    guarded = guarded[part]
                if not isinstance(guarded, dict):
                    continue
                if any(
                    guarded.get(name, _MISSING) != expected
                    for name, expected in change["guard"].items()
                ):
                    continue
            for part in path[:-1]:
                parent = parent[part]
            name = path[-1]
            present = (
                name in parent if isinstance(parent, dict) else 0 <= name < len(parent)
            )
            if present != change["after_present"]:
                continue
            if present and parent[name] != change["after"]:
                continue
            if change["before_present"]:
                parent[name] = change["before"]
            elif isinstance(parent, dict):
                parent.pop(name, None)
        except (KeyError, IndexError, TypeError):
            # Later structural edits own their data; an old array position must
            # never replace a newly authored object at that position.
            continue
    return result


def _snapshot(connection):
    records = {}
    for name in _COLUMNS:
        table = _table(connection, name)
        columns = (
            list(table.c)
            if name == "lint_finding"
            else [
                table.c.id,
                table.c.workspace_id,
                *(table.c[field] for field in _COLUMNS[name]),
            ]
        )
        for row in connection.execute(sa.select(*columns)).mappings():
            records[name, str(row["id"])] = _json(dict(row))
    jobs = _table(connection, "job")
    for row in connection.execute(
        sa.select(jobs).where(jobs.c.idempotency_key.like("locator-index:%"))
    ).mappings():
        records["job", str(row["id"])] = _json(dict(row))
    if connection.dialect.name == "sqlite":
        for row in connection.execute(
            sa.text(
                "SELECT rowid, context_header, workspace_id, source_kind, entity_id, "
                "version_id FROM search_doc_fts"
            )
        ).mappings():
            records["search_doc_fts", str(row["rowid"])] = dict(row)
    return records


def convert(connection, conversion):
    before = _snapshot(connection)
    conversion.convert_locators(connection)
    conversion.convert_command_receipts(connection)
    conversion.enqueue_reindex(connection, generation=str(uuid4()))
    after = _snapshot(connection)
    journal = _table(connection, TABLE)
    for name, identity in before.keys() | after.keys():
        old, new = before.get((name, identity)), after.get((name, identity))
        if old == new:
            continue
        sample = old or new
        changes, text_format = [], None
        if old is not None and new is not None:
            fields = _COLUMNS.get(name, ("context_header",))
            for field in fields:
                left, right = old[field], new[field]
                if field == "suggested_action" and left != right:
                    try:
                        left, right = json.loads(left), json.loads(right)
                        text_format = {
                            "keys": list(left) if isinstance(left, dict) else None,
                            "ascii": "\\u" in old[field],
                        }
                        ordinary = json.dumps(left, ensure_ascii=text_format["ascii"])
                        if ordinary != old[field]:
                            # Exceptional finding serialization only; receipts
                            # and Ask messages never enter this field.
                            text_format["original"] = old[field]
                            text_format["semantic_hash"] = hashlib.sha256(
                                json.dumps(left, sort_keys=True).encode()
                            ).hexdigest()
                    except (TypeError, ValueError):
                        text_format = None
                changes.extend(_diff(left, right, (field,)))
        guard = (
            {
                field: sample[field]
                for field in ("source_kind", "entity_id", "version_id")
            }
            if name == "search_doc_fts"
            else {}
        )
        connection.execute(
            journal.insert().values(
                workspace_id=_key(connection, sample["workspace_id"]),
                table_name=name,
                record_id=identity,
                operation="delete"
                if new is None
                else "insert"
                if old is None
                else "update",
                deltas=changes,
                row_state=old if new is None else new if old is None else None,
                identity_guard=guard,
                text_format=text_format,
            )
        )


def restore(connection):
    journal = _table(connection, TABLE)
    for entry in connection.execute(sa.select(journal)).mappings():
        name, identity = entry["table_name"], entry["record_id"]
        if name == "search_doc_fts":
            current = (
                connection.execute(
                    sa.text(
                        "SELECT rowid, context_header, source_kind, entity_id, "
                        "version_id "
                        "FROM search_doc_fts WHERE rowid = :id "
                        "AND workspace_id = :workspace"
                    ),
                    {
                        "id": int(identity),
                        "workspace": str(UUID(str(entry["workspace_id"]))),
                    },
                )
                .mappings()
                .first()
            )
            if current and all(
                current[key] == value for key, value in entry["identity_guard"].items()
            ):
                restored = _undo(dict(current), entry["deltas"])
                connection.execute(
                    sa.text(
                        "UPDATE search_doc_fts SET context_header = :header "
                        "WHERE rowid = :id"
                    ),
                    {"header": restored["context_header"], "id": int(identity)},
                )
            continue
        table = _table(connection, name)
        current = (
            connection.execute(sa.select(table).where(table.c.id == identity))
            .mappings()
            .first()
        )
        if entry["operation"] == "delete":
            # Do not resurrect content removed by workspace/lint-run teardown.
            state = entry["row_state"]
            runs = _table(connection, "lint_run")
            if current is None and connection.scalar(
                sa.select(runs.c.id).where(runs.c.id == state["lint_run_id"])
            ):
                connection.execute(table.insert().values(**_values(table, state)))
        elif current is not None and entry["operation"] == "insert":
            if _json(dict(current)) == entry["row_state"]:
                connection.execute(table.delete().where(table.c.id == identity))
        elif current is not None:
            current = dict(current)
            text_format = entry["text_format"]
            if text_format is not None:
                try:
                    current["suggested_action"] = json.loads(
                        current["suggested_action"]
                    )
                except (ValueError, TypeError):
                    continue
            restored = _undo(_json(current), entry["deltas"])
            fields = {change["path"][0] for change in entry["deltas"]}
            if text_format is not None:
                action = restored["suggested_action"]
                if isinstance(action, dict) and text_format["keys"]:
                    action = {
                        key: action[key] for key in text_format["keys"] if key in action
                    } | {
                        key: value
                        for key, value in action.items()
                        if key not in text_format["keys"]
                    }
                digest = hashlib.sha256(
                    json.dumps(action, sort_keys=True).encode()
                ).hexdigest()
                restored["suggested_action"] = (
                    text_format["original"]
                    if text_format.get("semantic_hash") == digest
                    else json.dumps(action, ensure_ascii=text_format["ascii"])
                )
            connection.execute(
                table.update()
                .where(table.c.id == identity)
                .values(**_values(table, {field: restored[field] for field in fields}))
            )
    restore_new_records(connection)


def restore_new_records(connection):
    """Give records authored after upgrade the previous locator wire fields."""
    paths, urls = {}, {}
    for name, kind in (
        ("context_page", "context_page"),
        ("library_item", "library_item"),
    ):
        table = _table(connection, name)
        for row in connection.execute(sa.select(table)).mappings():
            workspace, identity = (
                str(UUID(str(row["workspace_id"]))),
                str(UUID(str(row["id"]))),
            )
            locator = (
                f"/context/{row['slug']}.md"
                if kind == "context_page"
                else f"/library/{identity}.md"
            )
            paths[workspace, kind, identity] = locator
            url = (
                f"/p/{workspace}/{identity}"
                if kind == "context_page"
                else f"/workspaces/{workspace}/library/items/{identity}"
            )
            urls[workspace, url] = locator

    agents = _table(connection, "agent_page")
    for row in connection.execute(sa.select(agents.c.workspace_id, agents.c.id)):
        workspace, identity = str(UUID(str(row.workspace_id))), str(UUID(str(row.id)))
        paths[workspace, "agent_page", identity] = f"agent_page:{identity}"

    def rewrite(value, workspace, receipt=False):
        if isinstance(value, list):
            return [rewrite(item, workspace, receipt) for item in value]
        if not isinstance(value, dict):
            return value
        if receipt:
            return value
        result = {
            name: rewrite(item, workspace, receipt) for name, item in value.items()
        }
        golden = paths.get(
            (workspace, result.get("expected_kind"), str(result.get("expected_id")))
        )
        if golden:
            result.pop("expected_kind", None)
            result.pop("expected_id", None)
            result["expected_concept_path"] = golden
        locator = urls.get((workspace, result.get("stable_url")))
        if not locator and result.get("verdict") == "augment_existing":
            locator = paths.get((workspace, "context_page", str(result.get("page_id"))))
        if locator and "concept_path" not in result:
            result["concept_path"] = locator
            if not receipt:
                result.pop("stable_url", None)
            if result.get("verdict") == "augment_existing":
                result.pop("page_id", None)
        return result

    for name, fields in _COLUMNS.items():
        table = _table(connection, name)
        for row in connection.execute(sa.select(table)).mappings():
            workspace = str(UUID(str(row["workspace_id"])))
            changes = {}
            for field in fields:
                if field == "target_id":
                    continue
                original = row[field]
                value = original
                if field == "suggested_action":
                    try:
                        value = json.loads(value)
                    except (ValueError, TypeError):
                        continue
                rewritten = rewrite(value, workspace, name == "mcp_write_idempotency")
                if rewritten == value:
                    continue
                value = rewritten
                if field == "suggested_action":
                    value = json.dumps(value, ensure_ascii=False, sort_keys=True)
                if value != original:
                    changes[field] = value
            if changes:
                connection.execute(
                    table.update().where(table.c.id == row["id"]).values(**changes)
                )

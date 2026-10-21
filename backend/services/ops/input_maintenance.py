"""Explicit UUID repairs that work before the input-constraints upgrade.

This path never bootstraps, upgrades, stamps, or uses today's full ORM against
an older database. Identifiers are fixed below; values remain bound parameters.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from packaging.version import Version

from backend.app.runtime_resources import RuntimeResources
from backend.app.settings import Settings
from backend.app.version import package_version
from backend.domain.identity_policy import normalize_email, validate_display_name
from backend.domain.input_policy import normalize_short_text, validate_text
from backend.migrations.revision_modules.v0_1.input_restrictions_v1 import (
    REVISION,
    RULES,
    audit,
)
from backend.models.normalized_keys import normalize_search_key
from backend.repositories.input_metadata_repair import InputMetadataRepairRepository
from backend.services.ops.input_history_audit import audit_preserved_inputs
from backend.services.ops.migration_preflight import (
    evaluate_connection_preflight,
    evaluate_migration_preflight,
    make_non_mutating_engine,
)

SUPPORTED = frozenset(
    {
        "bd62193d5cf6",
        "efcbcf724889",
        "d148b9746ff5",
        "c7a4e91b2d60",
        "49d9c0776c5a",
        REVISION,
    }
)
FIELDS = {
    "api-token": ("api_tokens", {"name": 128}),
    "workspace": ("workspace", {"title": 128, "name": 128, "goal": 512, "slug": 80}),
    "workspace-join-request": ("workspace_join_requests", {"decision_note": 500}),
    "context-page": ("context_page", {"title": 256}),
    "agent-page": ("agent_page", {"title": 256}),
    "ask-session": ("ask_session", {"title": 256}),
    "library-item": ("library_item", {"title": 256}),
    "mcp-chat": ("mcp_chat_session", {"title": 120}),
    "tree-folder": ("workspace_tree_node", {"title": 80, "nav_summary": 160}),
    "tree-node": ("workspace_tree_node", {"title": 256}),
}


class InputMaintenanceError(ValueError):
    pass


def _preflight(connection):
    preflight = evaluate_connection_preflight(connection)
    if preflight.verdict != "ready" or preflight.revision not in SUPPORTED:
        raise InputMaintenanceError(
            "input maintenance requires a supported, consistent stamped database"
        )
    inspector = sa.inspect(connection)
    present = set(inspector.get_table_names())
    expected = {rule.name for rule in RULES}
    actual = {
        check["name"]
        for table in {rule.table for rule in RULES} & present
        for check in inspector.get_check_constraints(table)
        if check.get("name") in expected
    }
    if (preflight.revision == REVISION and actual != expected) or (
        preflight.revision != REVISION and actual
    ):
        raise InputMaintenanceError(
            "input maintenance refused: stamp/schema constraint conflict"
        )
    if "app_metadata" in present:
        table = _table(connection, "app_metadata")
        recorded = connection.execute(
            sa.select(table.c.value).where(table.c.key == "dtxwiki_version")
        ).scalar_one_or_none()
        if recorded and Version(recorded) > Version(package_version()):
            raise InputMaintenanceError(
                "input maintenance refuses a database written by newer code"
            )
    return preflight.revision


def _table(connection, name):
    if name not in sa.inspect(connection).get_table_names():
        raise InputMaintenanceError(
            "requested entity does not exist in this supported schema"
        )
    return sa.Table(name, sa.MetaData(), autoload_with=connection)


def _key(connection, identity: UUID):
    return identity.hex if connection.dialect.name == "sqlite" else identity


def audit_inputs(database_url: str) -> list[dict]:
    preflight = evaluate_migration_preflight(database_url)
    if preflight.verdict != "ready" or preflight.revision not in SUPPORTED:
        raise InputMaintenanceError(
            "input audit requires a supported, consistent stamped database"
        )
    engine = make_non_mutating_engine(database_url)
    try:
        with engine.connect() as connection:
            _preflight(connection)
            report = audit(connection, allow_missing=True)
            report.extend(audit_preserved_inputs(connection))
            if "email_outbox" in sa.inspect(connection).get_table_names():
                outbox = _table(connection, "email_outbox")
                invalid_ids = []
                count = 0
                for identity, recipient in connection.execute(
                    sa.select(
                        outbox.c.id,
                        outbox.c.recipient_email,
                    )
                ):
                    try:
                        normalize_email(recipient)
                    except ValueError:
                        count += 1
                        if len(invalid_ids) < 5:
                            invalid_ids.append(str(identity))
                if count:
                    report.append(
                        {
                            "rule": "historical_outbox_recipient",
                            "classification": "preserved_history",
                            "count": count,
                            "ids": invalid_ids,
                        }
                    )
            return report
    finally:
        engine.dispose()


def repair_input(
    database_url: str, entity: str, identity: UUID, field: str, value: str | None
) -> None:
    if entity == "user":
        if field not in {"display_name", "email"} or value is None:
            raise InputMaintenanceError("unsupported user repair field")
        value = (
            normalize_email(value) if field == "email" else validate_display_name(value)
        )
        table_name = "users"
    else:
        if entity not in FIELDS or field not in FIELDS[entity][1]:
            raise InputMaintenanceError("unsupported entity/field repair combination")
        table_name, limits = FIELDS[entity]
        maximum = limits[field]
        if value is None:
            if field not in {"decision_note", "nav_summary"} and (
                entity,
                field,
            ) not in {("library-item", "title"), ("mcp-chat", "title")}:
                raise InputMaintenanceError(
                    "NULL is only supported for optional notes/summaries/titles"
                )
        elif field in {"goal", "decision_note", "nav_summary"}:
            value = validate_text(
                value,
                field=field,
                max_length=maximum,
                min_length=1 if field == "goal" else 0,
            )
            if field == "goal" and not value.strip():
                raise InputMaintenanceError("goal cannot be empty")
        elif field == "slug":
            import re

            if (
                not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value)
                or len(value) > maximum
            ):
                raise InputMaintenanceError(
                    "slug must contain 1..80 lowercase ASCII letters, "
                    "digits and single hyphens"
                )
        else:
            value = normalize_short_text(value, field=field, max_length=maximum)

    # An absent SQLite file must never be created by this recovery tool.
    preflight = evaluate_migration_preflight(database_url)
    if preflight.verdict != "ready" or preflight.revision not in SUPPORTED:
        raise InputMaintenanceError(
            "input maintenance requires a supported, consistent stamped database"
        )
    resources = RuntimeResources.for_settings(Settings(database_url=database_url))
    with resources.acquire_activity("exclusive"):
        engine = make_non_mutating_engine(database_url, query_only=False)
        if engine.dialect.name == "sqlite":

            @sa.event.listens_for(engine, "connect")
            def _configure(dbapi_connection, _record):
                dbapi_connection.isolation_level = None
                dbapi_connection.execute("PRAGMA foreign_keys=ON")

            @sa.event.listens_for(engine, "begin")
            def _begin(connection):
                connection.exec_driver_sql("BEGIN IMMEDIATE")

        try:
            with engine.begin() as connection:
                _preflight(connection)
                table = _table(connection, table_name)
                key = _key(connection, identity)
                row = (
                    connection.execute(sa.select(table).where(table.c.id == key))
                    .mappings()
                    .one_or_none()
                )
                if row is None:
                    raise InputMaintenanceError("target UUID does not exist")
                if entity == "tree-folder" and row["node_kind"] != "folder":
                    raise InputMaintenanceError("target is not a folder")
                if entity == "tree-node" and (
                    row["node_kind"] == "folder"
                    or row.get("target_kind")
                    in {"context_page", "agent_page", "ask_session"}
                ):
                    raise InputMaintenanceError(
                        "repair the canonical target or use tree-folder"
                    )
                now = datetime.now(UTC)
                changes = {field: value}
                if field == "title" and "title_user_edited" in table.c:
                    changes["title_user_edited"] = value is not None
                if "updated_at" in table.c:
                    changes["updated_at"] = now
                if entity == "user":
                    name = value if field == "display_name" else row["display_name"]
                    email = value if field == "email" else row["email"]
                    changes["member_search_key"] = (
                        normalize_search_key(f"{name} {email}")
                        if "member_search_key" in table.c
                        else None
                    )
                    if "member_search_key" not in table.c:
                        changes.pop("member_search_key")
                    if field == "email":
                        _invalidate_email_credentials(
                            connection, table, key, changes, now
                        )
                if entity == "workspace" and field in {"title", "name"}:
                    changes.update(title=value, name=value)
                    if "directory_title_key" in table.c:
                        changes["directory_title_key"] = normalize_search_key(value)
                connection.execute(
                    table.update().where(table.c.id == key).values(**changes)
                )
                if field == "title" and table_name in {
                    "context_page",
                    "agent_page",
                    "ask_session",
                    "library_item",
                }:
                    _sync_title(
                        connection,
                        table_name,
                        key,
                        value,
                        now,
                        row["workspace_id"],
                        row,
                    )
                if table_name == "workspace_tree_node" and field == "title":
                    _sync_breadcrumbs(connection, row["workspace_id"])
                # Never update the Alembic stamp. Verify it remains identical.
                if _preflight(connection) != preflight.revision:
                    raise InputMaintenanceError(
                        "database revision changed during repair"
                    )
        finally:
            engine.dispose()


def _invalidate_email_credentials(connection, users, key, changes, now):
    if "email_verified_at" in users.c:
        changes["email_verified_at"] = None
    if "session_generation" in users.c:
        changes["session_generation"] = users.c.session_generation + 1
    present = set(sa.inspect(connection).get_table_names())
    if "password_reset_tokens" in present:
        tokens = _table(connection, "password_reset_tokens")
        connection.execute(
            tokens.update()
            .where(
                tokens.c.user_id == key,
                tokens.c.used_at.is_(None),
                tokens.c.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
    if "email_outbox" in present:
        outbox = _table(connection, "email_outbox")
        connection.execute(
            outbox.update()
            .where(outbox.c.recipient_user_id == key, outbox.c.status != "sent")
            .values(
                status="failed",
                next_attempt_at=None,
                encrypted_token=None,
                last_error="recipient_changed",
                updated_at=now,
            )
        )


def _sync_title(connection, kind, key, value, now, workspace_id, previous):
    present = set(sa.inspect(connection).get_table_names())
    repository = InputMetadataRepairRepository(connection, workspace_id)
    if "workspace_tree_node" in present and value is not None:
        tree = _table(connection, "workspace_tree_node")
        connection.execute(
            tree.update()
            .where(
                tree.c.workspace_id == workspace_id,
                tree.c.target_kind == kind,
                tree.c.target_id == key,
            )
            .values(title=value, updated_at=now)
        )
    if kind == "context_page" and "page_name_index" in present:
        _sync_page_titles(connection, key, workspace_id, value, now, present)
    if "search_doc_fts" in present:
        display_title = value if value is not None else previous.get("uri", "")
        repository.update_document_title(kind=kind, key=key, title=display_title)
    if kind == "context_page":
        _sync_breadcrumbs(connection, workspace_id)


def _sync_page_titles(connection, key, workspace_id, value, now, present):
    """Rebuild title projections without replacing a draft's staged title."""
    import json
    from uuid import uuid4

    from backend.services.entity_ref import normalize_entity_name

    names = _table(connection, "page_name_index")
    rows = (
        connection.execute(
            sa.select(names).where(
                names.c.workspace_id == workspace_id,
                names.c.page_id == key,
            )
        )
        .mappings()
        .all()
    )
    scopes = {(row["source_version_id"], row["visibility_scope"]) for row in rows}
    patches = {}
    if "change_operation" in present:
        operations = _table(connection, "change_operation")
        for operation in connection.execute(
            sa.select(operations)
            .where(
                operations.c.workspace_id == workspace_id,
                operations.c.target_type == "context_page",
                operations.c.target_id == key,
                operations.c.operation == "metadata_patch",
            )
            .order_by(operations.c.created_at, operations.c.id)
        ).mappings():
            try:
                patch = json.loads(operation["diff_summary"] or "")
            except (ValueError, TypeError):
                continue
            if isinstance(patch, dict):
                patches.setdefault(operation["diff_ref"], {}).update(patch)
    connection.execute(
        names.delete().where(
            names.c.workspace_id == workspace_id,
            names.c.page_id == key,
            names.c.name_kind == "title",
        )
    )
    for version_id, scope in scopes:
        titles = [value]
        if scope == "draft":
            reference = f"context_page_version:{UUID(str(version_id))}"
            pending = patches.get(reference, {}).get("title")
            if isinstance(pending, str):
                titles.append(pending)
        seen = set()
        for title in titles:
            normalized = normalize_entity_name(title)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            connection.execute(
                names.insert().values(
                    id=_key(connection, uuid4()),
                    workspace_id=workspace_id,
                    page_id=key,
                    source_version_id=version_id,
                    visibility_scope=scope,
                    name_kind="title",
                    name=title.strip(),
                    name_norm=normalized,
                    created_at=now,
                )
            )
    if "page_name_trgm" in present:
        all_rows = (
            connection.execute(
                sa.select(names).where(
                    names.c.workspace_id == workspace_id,
                    names.c.page_id == key,
                )
            )
            .mappings()
            .all()
        )
        InputMetadataRepairRepository(connection, workspace_id).replace_page_names(
            key=key,
            rows=all_rows,
        )


def _sync_breadcrumbs(connection, workspace_id):
    present = set(sa.inspect(connection).get_table_names())
    if not {"workspace_tree_node", "search_doc_fts"} <= present:
        return
    tree = _table(connection, "workspace_tree_node")
    nodes = (
        connection.execute(
            sa.select(tree).where(
                tree.c.workspace_id == workspace_id,
            )
        )
        .mappings()
        .all()
    )
    by_id = {str(row["id"]).replace("-", ""): row for row in nodes}
    paths = {}
    for row in nodes:
        if (
            row["target_kind"] != "context_page"
            or not row["primary_placement"]
            or row["visibility_status"] != "visible"
        ):
            continue
        path = []
        current = row
        seen = set()
        while current is not None:
            identity = str(current["id"]).replace("-", "")
            if identity in seen or current["visibility_status"] != "visible":
                path = []
                break
            seen.add(identity)
            path.append(current["title"])
            parent = current["parent_id"]
            if parent is None:
                break
            current = by_id.get(str(parent).replace("-", ""))
            if current is None:
                path = []
        paths[str(row["target_id"]).replace("-", "")] = " / ".join(reversed(path))
    InputMetadataRepairRepository(connection, workspace_id).update_breadcrumbs(paths)

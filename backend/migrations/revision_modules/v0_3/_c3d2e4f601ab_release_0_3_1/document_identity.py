"""Frozen document identity conversion, independent of draft state changes."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import sqlalchemy as sa


def _id(value):
    return UUID(str(value)) if value is not None else None


def convert_locators(connection):
    """Convert known document identities without discarding unrelated payloads."""
    metadata = sa.MetaData()
    pages = sa.Table("context_page", metadata, autoload_with=connection)
    library = sa.Table("library_item", metadata, autoload_with=connection)
    identities = {}
    paths = {}
    for table, kind, slug_column in (
        (pages, "context_page", "slug"),
        (library, "library_item", "uri"),
    ):
        for row in connection.execute(
            sa.select(table.c.workspace_id, table.c.id, table.c[slug_column])
        ):
            workspace_id, identity = str(_id(row.workspace_id)), str(_id(row.id))
            identities[workspace_id, kind, identity] = (
                f"/p/{workspace_id}/{identity}"
                if kind == "context_page"
                else f"/workspaces/{workspace_id}/library/items/{identity}"
            )
            locator = f"/context/{row.slug}.md" if kind == "context_page" else row.uri
            paths[workspace_id, locator] = kind, identity

    agents = sa.Table("agent_page", metadata, autoload_with=connection)
    for row in connection.execute(sa.select(agents.c.workspace_id, agents.c.id)):
        workspace_id, identity = str(_id(row.workspace_id)), str(_id(row.id))
        identities[workspace_id, "agent_page", identity] = ""
        paths[workspace_id, f"agent_page:{identity}"] = "agent_page", identity

    def resolve(locator, workspace_id, preferred=None):
        # Durable target IDs survive a title/slug rename. Only a same-workspace
        # existing target can supply a canonical replacement.
        if not isinstance(locator, str):
            return None
        if preferred is not None and (workspace_id, *preferred) in identities:
            return preferred
        if (workspace_id, locator) in paths:
            return paths[workspace_id, locator]
        try:
            path = urlsplit(locator).path.rstrip("/")
        except ValueError:
            return None
        match = re.fullmatch(r"/p/([^/]+)/([^/]+)", path)
        if match and match[1] == workspace_id:
            candidate = "context_page", match[2]
        else:
            match = re.fullmatch(r"/workspaces/([^/]+)/library/items/([^/]+)", path)
            if match and match[1] == workspace_id:
                candidate = "library_item", match[2]
            else:
                match = re.fullmatch(r"/library/([^/]+)\.md", path)
                if match:
                    candidate = "library_item", match[1]
                else:
                    try:
                        identity = str(_id(locator))
                    except (ValueError, TypeError):
                        return None
                    candidate = next(
                        (
                            (kind, identity)
                            for kind in ("context_page", "library_item")
                            if (workspace_id, kind, identity) in identities
                        ),
                        None,
                    )
        return (
            candidate
            if candidate and (workspace_id, *candidate) in identities
            else None
        )

    def rewrite(value, workspace_id, target=None):
        if isinstance(value, list):
            return [rewrite(item, workspace_id) for item in value]
        if not isinstance(value, dict):
            return value
        result = {
            name: rewrite(item, workspace_id)
            for name, item in value.items()
            if name not in {"concept_path", "expected_concept_path"}
        }
        for field, golden in (("concept_path", False), ("expected_concept_path", True)):
            locator = value.get(field)
            if locator is None:
                continue
            own_id = value.get("expected_id") if golden else value.get("page_id")
            own_kind = value.get("expected_kind") if golden else "context_page"
            if not golden and value.get("library_item_id"):
                own_id, own_kind = value["library_item_id"], "library_item"
            try:
                preferred = (
                    (own_kind, str(_id(own_id)))
                    if own_id
                    and own_kind in ("context_page", "library_item", "agent_page")
                    else target
                )
            except (ValueError, TypeError):
                preferred = target
            resolved = resolve(locator, workspace_id, preferred)
            if resolved is None:
                # Opaque evidence survives; this is not an input accepted by a
                # runtime legacy-path reader and never fabricates an identity.
                locator = rewrite(locator, workspace_id)
                old = result.get("locator")
                result["locator"] = (
                    locator
                    if old is None or old == locator
                    else ([*old, locator] if isinstance(old, list) else [old, locator])
                )
                continue
            kind, identity = resolved
            if golden:
                result.update(expected_kind=kind, expected_id=identity)
            else:
                result.update(
                    {
                        "page_id"
                        if kind == "context_page"
                        else "library_item_id": identity
                    },
                    stable_url=identities[workspace_id, kind, identity],
                )
        return result

    for name, field, kind_column in (
        ("client_delegated_task", "payload", "target_kind"),
        ("lint_finding", "message_args", "target_type"),
    ):
        table = sa.Table(name, metadata, autoload_with=connection)
        for row in connection.execute(sa.select(table)).mappings():
            workspace_id = str(_id(row["workspace_id"]))
            target = (
                (row[kind_column], str(_id(row["target_id"])))
                if row["target_id"]
                else None
            )
            maintenance = name == "lint_finding" and row["rule_id"].startswith(
                "maintenance."
            )
            updates = {}
            action = None
            if maintenance:
                # Maintenance answers are JSON text; ordinary prose stays intact.
                try:
                    action = json.loads(row["suggested_action"] or "")
                except (ValueError, TypeError):
                    pass
                if (
                    row[kind_column] == "context_page"
                    and target is not None
                    and (workspace_id, *target) not in identities
                ):
                    # A deleted durable identity must never retarget to a new
                    # page that happens to reuse its former slug.
                    connection.execute(table.delete().where(table.c.id == row["id"]))
                    continue
                if row[kind_column] == "context_page" and target is None:
                    fields = [
                        item for item in (row[field], action) if isinstance(item, dict)
                    ]
                    recovered = next(
                        (
                            resolved
                            for item in fields
                            if (
                                resolved := resolve(
                                    item.get("concept_path"), workspace_id
                                )
                            )
                        ),
                        None,
                    )
                    if recovered is None or recovered[0] != "context_page":
                        connection.execute(
                            table.delete().where(table.c.id == row["id"])
                        )
                        continue
                    target = recovered
                    identity = _id(recovered[1])
                    updates["target_id"] = (
                        identity.hex
                        if connection.dialect.name == "sqlite"
                        else identity
                    )
            converted = rewrite(row[field], workspace_id, target)
            if converted != row[field]:
                updates[field] = converted
            if maintenance and isinstance(action, (dict, list)):
                converted_action = rewrite(action, workspace_id, target)
                # Coverage answers address their suggested page rather than the
                # finding's knowledge-coverage-gap target. A dangling suggestion
                # cannot remain actionable after its legacy locator is retired.
                if (
                    isinstance(action, dict)
                    and action.get("verdict") == "augment_existing"
                    and (
                        not converted_action.get("page_id")
                        or (
                            workspace_id,
                            "context_page",
                            str(converted_action["page_id"]),
                        )
                        not in identities
                    )
                ):
                    connection.execute(table.delete().where(table.c.id == row["id"]))
                    continue
                if converted_action != action:
                    updates["suggested_action"] = json.dumps(
                        converted_action, ensure_ascii=False, sort_keys=True
                    )
            if updates:
                connection.execute(
                    table.update().where(table.c.id == row["id"]).values(**updates)
                )

    # Coverage candidates and Ask traces are persisted read responses, so the
    # same release must convert them even when their source document was renamed.
    versions = sa.Table("library_item_version", metadata, autoload_with=connection)
    artifacts = sa.Table("library_artifact", metadata, autoload_with=connection)
    artifact_owners = {
        (str(_id(row.workspace_id)), str(_id(row.id))): (
            "library_item",
            str(_id(row.library_item_id)),
        )
        for row in connection.execute(
            sa.select(
                artifacts.c.workspace_id, artifacts.c.id, versions.c.library_item_id
            ).join(
                versions,
                sa.and_(
                    versions.c.workspace_id == artifacts.c.workspace_id,
                    versions.c.id == artifacts.c.library_item_version_id,
                ),
            )
        )
    }

    def response(value, workspace_id):
        if isinstance(value, list):
            return [response(item, workspace_id) for item in value]
        if not isinstance(value, dict):
            return value
        converted = {
            name: response(item, workspace_id)
            for name, item in value.items()
            if name != "concept_path"
        }
        if "concept_path" not in value:
            return converted
        kind, identity = value.get("type", value.get("kind")), value.get("id")
        preferred = (
            (kind, str(identity))
            if kind in {"context_page", "library_item"}
            else artifact_owners.get((workspace_id, str(identity)))
            if kind == "library_artifact"
            else None
        )
        resolved = resolve(value.get("concept_path"), workspace_id, preferred)
        converted["stable_url"] = (
            identities[workspace_id, *resolved] if resolved else ""
        )
        return converted

    for table_name, columns in (
        ("knowledge_coverage_gap", ("nearest_candidates", "rewrite_attempts")),
        ("ask_turn", ("retrieval_trace",)),
    ):
        table = sa.Table(table_name, metadata, autoload_with=connection)
        for row in connection.execute(sa.select(table)).mappings():
            workspace_id = str(_id(row["workspace_id"]))
            changes = {name: response(row[name], workspace_id) for name in columns}
            changes = {
                name: value for name, value in changes.items() if value != row[name]
            }
            if changes:
                connection.execute(
                    table.update().where(table.c.id == row["id"]).values(**changes)
                )


def convert_command_receipts(connection):
    """Keep replay identity and draft state while replacing document locators."""
    ledger = sa.Table("mcp_write_idempotency", sa.MetaData(), autoload_with=connection)

    def rewrite(value):
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if not isinstance(value, dict):
            return value
        # Receipts already carry their page/version IDs and canonical URL.
        # Only the retired redundant locator leaves this contract in PR3-A.
        return {
            name: rewrite(item)
            for name, item in value.items()
            if name != "concept_path"
        }

    for row in connection.execute(sa.select(ledger.c.id, ledger.c.result_payload)):
        converted = rewrite(row.result_payload)
        if converted != row.result_payload:
            connection.execute(
                ledger.update()
                .where(ledger.c.id == row.id)
                .values(result_payload=converted)
            )


def enqueue_reindex(connection):
    """Use the existing embed worker to refresh each changed document."""
    metadata = sa.MetaData()
    jobs = sa.Table("job", metadata, autoload_with=connection)
    now = datetime.now(UTC)

    def key(value):
        return value.hex if connection.dialect.name == "sqlite" else value

    for table_name, kind in (
        ("context_page", "context_page"),
        ("library_item", "library_item"),
    ):
        table = sa.Table(table_name, metadata, autoload_with=connection)
        for row in connection.execute(sa.select(table.c.id, table.c.workspace_id)):
            identity = str(_id(row.id))
            job_key = f"locator-index:{kind}:{identity}"
            exists = connection.scalar(
                sa.select(jobs.c.id)
                .where(
                    jobs.c.workspace_id == row.workspace_id,
                    jobs.c.job_type == "embed",
                    jobs.c.idempotency_key == job_key,
                )
                .limit(1)
            )
            if exists is None:
                connection.execute(
                    jobs.insert().values(
                        id=key(uuid4()),
                        workspace_id=row.workspace_id,
                        created_at=now,
                        updated_at=now,
                        job_type="embed",
                        trigger_type="migration",
                        status="queued",
                        progress=0,
                        target_ref={"kind": kind, "id": identity},
                        idempotency_key=job_key,
                        retry_count=0,
                        next_attempt_at=now,
                    )
                )
    if (
        connection.dialect.name == "sqlite"
        and "search_doc_fts" in sa.inspect(connection).get_table_names()
    ):
        rows = connection.execute(
            sa.text("SELECT rowid, context_header FROM search_doc_fts")
        ).all()
        for rowid, header in rows:
            updated = "\n".join(
                line
                for line in (header or "").splitlines()
                if not line.startswith("Path:")
            )
            if updated != header:
                connection.execute(
                    sa.text(
                        "UPDATE search_doc_fts SET context_header = :header "
                        "WHERE rowid = :rowid"
                    ),
                    {"header": updated, "rowid": rowid},
                )

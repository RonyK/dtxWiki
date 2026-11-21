"""Frozen one-pass conversion; never imports the runtime draft implementation."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pygit2
import sqlalchemy as sa

from . import context_content_to_git as git

_LOG = logging.getLogger(__name__)
_FIELDS = ("title", "page_type", "tags", "aliases", "placement_parent_node_id")
_OPEN = {"pending", "changes_requested"}
_TERMINAL = {"approved", "rejected", "withdrawn", "superseded", "cancelled"}


def _id(value):
    return UUID(str(value)) if value is not None else None


def _stamp(value):
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)


def convert(connection, attributes):
    """Complete each page, repairing uncertain parents and opening a review."""
    root = git.repository_root(attributes)
    metadata = sa.MetaData()
    tables = {
        name: sa.Table(name, metadata, autoload_with=connection)
        for name in (
            "context_page",
            "context_page_version",
            "context_version_parent",
            "change_set",
            "change_operation",
            "review_item",
            "audit_event",
        )
    }

    def key(value):
        return value.hex if connection.dialect.name == "sqlite" else value

    counts = dict(
        drafts=0,
        promoted_parents=0,
        closed=0,
        metadata_fallbacks=0,
        repaired_parents=0,
        reviews=0,
    )
    pages, versions = tables["context_page"], tables["context_page_version"]
    pygit2.settings.enable_fsync_gitdir(True)
    for page in connection.execute(sa.select(pages).order_by(pages.c.id)).mappings():
        rows = [
            dict(row)
            for row in connection.execute(
                sa.select(versions)
                .where(
                    versions.c.page_id == page["id"],
                    versions.c.workspace_id == page["workspace_id"],
                )
                .order_by(versions.c.created_at, versions.c.version_no, versions.c.id)
            ).mappings()
        ]
        if not rows:
            continue
        for row in rows:
            row["original_parent_version_id"] = row["parent_version_id"]
        by_id = {_id(row["id"]): row for row in rows}
        workspace_id, page_id = _id(page["workspace_id"]), _id(page["id"])
        operations = [
            dict(operation)
            for operation in connection.execute(
                sa.select(tables["change_operation"])
                .where(
                    tables["change_operation"].c.workspace_id == page["workspace_id"],
                    tables["change_operation"].c.target_id == page["id"],
                )
                .order_by(
                    tables["change_operation"].c.created_at,
                    tables["change_operation"].c.id,
                )
            ).mappings()
        ]
        reviews = [
            dict(review)
            for review in connection.execute(
                sa.select(tables["review_item"]).where(
                    tables["review_item"].c.workspace_id == page["workspace_id"],
                    tables["review_item"].c.change_set_id.in_(
                        [row["change_set_id"] for row in operations]
                    ),
                )
            ).mappings()
        ]
        # Retire the old branch-notification workflow in place. The request
        # and its decision history keep their identities; it now reviews the
        # same version through the ordinary mutable-draft flow.
        for review in reviews:
            if (
                review["review_type"] != "context_reconciliation"
                or review["status"] not in _OPEN
            ):
                continue
            connection.execute(
                tables["review_item"]
                .update()
                .where(tables["review_item"].c.id == review["id"])
                .values(
                    review_type="context_update",
                    decision_summary="Context draft requires review",
                    decision_summary_code="context_draft_requires_review",
                )
            )
            review["review_type"] = "context_update"
            connection.execute(
                tables["change_set"]
                .update()
                .where(tables["change_set"].c.id == review["change_set_id"])
                .values(
                    status="pending_review"
                    if review["status"] == "pending"
                    else "changes_requested"
                )
            )
            for operation in operations:
                if (
                    operation["change_set_id"] == review["change_set_id"]
                    and operation["operation"] == "patch_draft_diverged_manifest"
                ):
                    connection.execute(
                        tables["change_operation"]
                        .update()
                        .where(tables["change_operation"].c.id == operation["id"])
                        .values(operation="update")
                    )
                    operation["operation"] = "update"
        reviews_by_version = {}
        metadata_by_version = {}
        for operation in operations:
            ref = operation["diff_ref"] or ""
            if not ref.startswith("context_page_version:"):
                continue
            try:
                version_id = _id(ref.split(":", 1)[1])
            except ValueError:
                continue
            if operation["operation"] in {"metadata_patch", "metadata_snapshot"}:
                try:
                    patch = json.loads(operation["diff_summary"] or "{}")
                except (ValueError, TypeError):
                    continue
                if isinstance(patch, dict):
                    metadata_by_version.setdefault(version_id, {}).update(
                        {field: patch[field] for field in _FIELDS if field in patch}
                    )
            else:
                reviews_by_version.setdefault(version_id, []).extend(
                    review
                    for review in reviews
                    if review["change_set_id"] == operation["change_set_id"]
                )
        baseline = {field: page[field] for field in _FIELDS}
        baseline["placement_parent_node_id"] = (
            str(_id(baseline["placement_parent_node_id"]))
            if baseline["placement_parent_node_id"]
            else None
        )
        for row in rows:
            version_id = _id(row["id"])
            snapshot = dict(baseline)
            recovered = metadata_by_version.get(version_id, {})
            snapshot.update(recovered)
            row["page_metadata"] = snapshot
            if len(recovered) < len(_FIELDS):
                counts["metadata_fallbacks"] += 1
            statuses = {
                review["status"] for review in reviews_by_version.get(version_id, [])
            }
            if row["version_status"] not in {
                "published_snapshot",
                "superseded_snapshot",
                "discarded",
            }:
                ended = bool(statuses & _TERMINAL) and not statuses & _OPEN
                ended = ended or (
                    not statuses
                    and row["graph_status"]
                    in {"discarded", "merged", "branched", "superseded"}
                )
                row["version_status"] = "closed" if ended else "draft"
                counts["closed" if ended else "drafts"] += 1
        path = root / f"{workspace_id}.git"
        repository = None
        try:
            if any(
                row["commit_oid"] or row["version_status"] == "closed" for row in rows
            ):
                if path.is_symlink():
                    raise RuntimeError(
                        "Context draft migration requires an ordinary bare repository"
                    )
                if path.exists():
                    repository = pygit2.Repository(
                        str(path), flags=pygit2.enums.RepositoryOpenFlag.NO_SEARCH
                    )
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    repository = pygit2.init_repository(str(path), bare=True)
                if not repository.is_bare:
                    raise RuntimeError(
                        "Context draft migration requires a bare repository"
                    )
            by_oid = {row["commit_oid"]: row for row in rows if row["commit_oid"]}
            # Existing Git history is authoritative and its OIDs never change.
            for row in rows:
                if not row["commit_oid"]:
                    continue
                commit = repository[row["commit_oid"]]
                parent_rows = [by_oid[str(oid)] for oid in commit.parent_ids]
                row["parent_version_id"] = parent_rows[0]["id"] if parent_rows else None
                for ordinal, parent in enumerate(parent_rows[1:], 1):
                    connection.execute(
                        tables["context_version_parent"]
                        .insert()
                        .values(
                            workspace_id=key(workspace_id),
                            page_id=key(page_id),
                            version_id=key(_id(row["id"])),
                            ordinal=ordinal,
                            parent_version_id=key(_id(parent["id"])),
                        )
                    )
            pending = [
                row
                for row in rows
                if row["version_status"] == "closed" and not row["commit_oid"]
            ]
            while pending:
                progressed = False
                for row in list(pending):
                    parent_id, repaired = _parent(
                        row, by_id, rows, wait_for_closed=True
                    )
                    parent = by_id.get(parent_id)
                    if (
                        parent is not None
                        and not parent["commit_oid"]
                        and parent in pending
                        and not repaired
                    ):
                        continue
                    _set_parent(row, parent_id, counts)
                    if repaired:
                        _review(
                            connection,
                            tables,
                            page,
                            row,
                            reviews_by_version,
                            counts,
                            key,
                        )
                    body = (row["body_md"] or "").encode("utf-8")
                    parents = (
                        [repository[parent["commit_oid"]].id]
                        if parent and parent["commit_oid"]
                        else []
                    )
                    oid = git._write(
                        repository,
                        page_id,
                        _id(row["id"]),
                        row["created_at"],
                        body,
                        parents,
                        row["block_map"],
                    )
                    git._verify(
                        repository,
                        oid,
                        page_id,
                        _id(row["id"]),
                        row["created_at"],
                        body,
                        parents,
                        row["block_map"],
                    )
                    row.update(
                        commit_oid=str(oid),
                        content_byte_size=len(body),
                        body_md=None,
                        block_map=None,
                    )
                    pending.remove(row)
                    progressed = True
                if not progressed:
                    # A remaining closed cycle is handled by the same dated fallback.
                    row = pending[0]
                    row["parent_version_id"] = row["id"]
            for row in rows:
                if row["version_status"] == "draft":
                    parent_id, repaired = _parent(row, by_id, rows)
                    _set_parent(row, parent_id, counts)
                    if repaired:
                        _review(
                            connection,
                            tables,
                            page,
                            row,
                            reviews_by_version,
                            counts,
                            key,
                        )
                connection.execute(
                    versions.update()
                    .where(versions.c.id == row["id"])
                    .values(
                        page_metadata=row["page_metadata"],
                        parent_version_id=row["parent_version_id"],
                        version_status=row["version_status"],
                        commit_oid=row["commit_oid"],
                        content_byte_size=row["content_byte_size"],
                        body_md=row["body_md"],
                        # Reflected JSON does not preserve none_as_null=True;
                        # keep an absent map SQL NULL rather than JSON null.
                        block_map=sa.null()
                        if row["block_map"] is None
                        else row["block_map"],
                    )
                )
        finally:
            if repository is not None:
                repository.free()
    convert_command_receipts(connection)
    retire_reconciliation_jobs(connection)
    enqueue_reindex(connection)
    _LOG.info("Context draft conversion completed: %s", counts)
    return counts


def _parent(row, by_id, rows, *, wait_for_closed=False):
    original = _id(row["parent_version_id"])
    cursor, seen = original, {_id(row["id"])}
    while cursor is not None:
        if cursor in seen or cursor not in by_id:
            break
        seen.add(cursor)
        parent = by_id[cursor]
        if parent["commit_oid"] or (
            wait_for_closed and parent["version_status"] == "closed"
        ):
            return cursor, False
        cursor = _id(parent["parent_version_id"])
    else:
        return None, False
    candidates = [
        candidate
        for candidate in rows
        if candidate["commit_oid"]
        and _stamp(candidate["created_at"]) < _stamp(row["created_at"])
    ]
    return (_id(candidates[-1]["id"]) if candidates else None), True


def _set_parent(row, parent_id, counts):
    old = _id(row["parent_version_id"])
    if old != parent_id:
        counts["promoted_parents"] += 1
    row["parent_version_id"] = by_key = (
        parent_id.hex if isinstance(row["id"], str) and parent_id else parent_id
    )
    return by_key


def _review(connection, tables, page, row, existing, counts, key):
    now = datetime.now(UTC)
    version_id = _id(row["id"])
    correction = {
        "version_id": str(version_id),
        "original_parent_version_id": str(_id(row["original_parent_version_id"]))
        if row["original_parent_version_id"]
        else None,
        "parent_version_id": str(_id(row["parent_version_id"]))
        if row["parent_version_id"]
        else None,
        "reason": "migration_parent_repaired",
    }
    review = next(
        (
            review
            for review in existing.get(version_id, [])
            if review["status"] in _OPEN
        ),
        None,
    )
    # A request the author already opened keeps deciding publication; a
    # notice the migration opens only acknowledges the repaired ancestry.
    correction["inspection_only"] = review is None
    if review is None:
        change_set_id, review_id = uuid4(), uuid4()
        connection.execute(
            tables["change_set"]
            .insert()
            .values(
                id=key(change_set_id),
                workspace_id=page["workspace_id"],
                created_at=now,
                updated_at=now,
                reason="Context migration parent correction requires review",
                trigger_type="migration",
                actor_type="system",
                status="draft_created",
                risk_level="high",
                cite_call_skipped=False,
            )
        )
        connection.execute(
            tables["change_operation"]
            .insert()
            .values(
                id=key(uuid4()),
                workspace_id=page["workspace_id"],
                change_set_id=key(change_set_id),
                created_at=now,
                target_type="context_page",
                target_id=page["id"],
                operation="update",
                diff_ref=f"context_page_version:{version_id}",
                diff_summary=json.dumps(correction),
            )
        )
        connection.execute(
            tables["review_item"]
            .insert()
            .values(
                id=key(review_id),
                workspace_id=page["workspace_id"],
                change_set_id=key(change_set_id),
                created_at=now,
                updated_at=now,
                review_type="context_update",
                status="pending",
                risk_level="high",
                risk_signals={
                    # caller_needs_review is the request-intent signal saves
                    # keep; the resolver derives human_review_required from it.
                    "caller_needs_review": True,
                    "human_review_required": True,
                    "migration_parent_repaired": correction,
                },
                decision_summary=(
                    "Context migration repaired the parent; please confirm the ancestry"
                ),
            )
        )
        counts["reviews"] += 1
    else:
        review_id = _id(review["id"])
        connection.execute(
            tables["review_item"]
            .update()
            .where(tables["review_item"].c.id == review["id"])
            .values(
                risk_signals={
                    **(review["risk_signals"] or {}),
                    "caller_needs_review": True,
                    "human_review_required": True,
                    "migration_parent_repaired": correction,
                },
            )
        )
    connection.execute(
        tables["audit_event"]
        .insert()
        .values(
            id=key(uuid4()),
            workspace_id=page["workspace_id"],
            created_at=now,
            actor_kind="system",
            actor_id="migration",
            actor_type="system",
            action="context.migration_parent_repaired",
            target_kind="context_page",
            target_type="context_page",
            target_id=page["id"],
            payload={**correction, "review_item_id": str(review_id)},
            retention_class="standard",
        )
    )
    counts["repaired_parents"] += 1


def convert_command_receipts(connection):
    """Preserve replay identities while removing fields of the retired contract."""
    metadata = sa.MetaData()
    ledger = sa.Table("mcp_write_idempotency", metadata, autoload_with=connection)
    versions = sa.Table("context_page_version", metadata, autoload_with=connection)
    statuses = {
        str(_id(row.id)): row.version_status
        for row in connection.execute(
            sa.select(versions.c.id, versions.c.version_status)
        )
    }
    removed = {
        "concept_path",
        "alternative_draft_id",
        "alternative_drafts",
        "reconciles",
        "reopened_submission",
        "pending_draft_warnings",
        "stale_candidate",
        "latest_draft_version_id",
    }

    def rewrite(value):
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {
            name: rewrite(item) for name, item in value.items() if name not in removed
        }
        if result.get("version_status") == "editing":
            result["version_status"] = statuses.get(
                str(result.get("version_id")), "draft"
            )
        return result

    for row in connection.execute(sa.select(ledger.c.id, ledger.c.result_payload)):
        converted = rewrite(row.result_payload)
        if converted != row.result_payload:
            connection.execute(
                ledger.update()
                .where(ledger.c.id == row.id)
                .values(result_payload=converted)
            )


def retire_reconciliation_jobs(connection):
    """Complete retired reminder work through the existing Job terminal state."""
    jobs = sa.Table("job", sa.MetaData(), autoload_with=connection)
    now = datetime.now(UTC)
    connection.execute(
        jobs.update()
        .where(
            jobs.c.job_type == "context_reconciliation_reminder",
            jobs.c.status.in_(("queued", "running")),
        )
        .values(
            status="completed",
            stage=None,
            progress=100,
            completed_at=now,
            updated_at=now,
            next_attempt_at=None,
        )
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
            job_key = f"pr3-index:{kind}:{identity}"
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

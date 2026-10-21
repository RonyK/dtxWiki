"""Add durable content-event times and personal workspace favorites.

Revision ID: c7a4e91b2d60
Revises: efcbcf724889
"""

import json
import unicodedata
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.sql.expression import Grouping

revision = "c7a4e91b2d60"
down_revision = "efcbcf724889"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("context_page", "agent_page", "library_item"):
        op.add_column(name, sa.Column("content_changed_at", sa.DateTime(timezone=True)))
    op.create_table(
        "user_favorite",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "target_kind IN ('context_page', 'library_item', 'agent_page')",
            name="user_favorite_target_kind_check",
        ),
        sa.PrimaryKeyConstraint("user_id", "workspace_id", "target_kind", "target_id"),
    )
    # Seek each exact version, including when a long chain has unchanged
    # content. A target/time index alone rescans all newer target events at
    # every predecessor. This index adds no ongoing product write cost.
    connection = op.get_bind()
    audits = sa.Table("audit_event", sa.MetaData(), autoload_with=connection)
    lookup_index = _publication_lookup_index(audits)
    lookup_index.create(connection)
    try:
        _backfill_proven_publications(audits)
    finally:
        lookup_index.drop(connection)


def _audit_version_id(audits):
    # SQLite expression indexes require the query expression to have exactly
    # the same constant JSON path as the index. A normal JSON key bind would
    # leave JSON_EXTRACT(payload, ?) unseekable. literal_execute renders this
    # migration-owned constant portably on SQLite and PostgreSQL.
    key = sa.literal("version_id", type_=sa.JSON.JSONStrIndexType, literal_execute=True)
    version_id = audits.c.payload[key].as_string()
    # Only these two canonical UUID forms can match a proven pointer. Bound
    # index keys also tolerate malformed historical payloads with huge values
    # that would exceed PostgreSQL's btree entry limit.
    return Grouping(
        sa.case(
            (
                sa.func.length(version_id).in_(
                    [sa.literal(length, literal_execute=True) for length in (32, 36)]
                ),
                version_id,
            )
        )
    )


def _publication_lookup_index(audits):
    return sa.Index(
        "ix_tree_rail_backfill_audit_target",
        audits.c.workspace_id,
        audits.c.target_kind,
        audits.c.target_id,
        audits.c.action,
        _audit_version_id(audits),
        audits.c.created_at,
        audits.c.id,
    )


def _id(value):
    try:
        return UUID(str(value)).hex if value is not None else None
    except (TypeError, ValueError):
        return None


def _json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value or {}


def _body(value, *, context):
    # Match the published reader's frontmatter-free body. This revision owns
    # its normalization so later application changes cannot alter a replay.
    value = unicodedata.normalize("NFC", value)
    lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if context and len(lines) >= 3 and lines[0].strip() == "---":
        for index, line in enumerate(lines[1:], 1):
            if line.strip() in {"---", "..."}:
                lines = lines[index + 1 :]
                if lines and lines[0] == "":
                    lines = lines[1:]
                break
    return "\n".join(lines).rstrip("\n")


def _version_id(column, normalized_id):
    # Reflection preserves PostgreSQL's UUID binder, while SQLite exposes
    # Alembic's UUID storage as CHAR(32).
    return UUID(normalized_id) if isinstance(column.type, sa.Uuid) else normalized_id


def _visible_agent_meta(row):
    return {
        key: value
        for key, value in _json(row["frontmatter"]).items()
        if key not in {"__proposed_page_meta__", "title", "slug"}
    }


def _publication_time(
    connection, versions, audits, page, kind, owner, pointer, action, previous_key
):
    candidate_id = _id(page[pointer])
    seen = set()
    while candidate_id is not None and candidate_id not in seen:
        seen.add(candidate_id)
        # The exact event must prove this current pointer or one of its prior
        # published/active heads. No audit/history collection is materialized.
        event = (
            connection.execute(
                sa.select(audits.c.created_at, audits.c.payload)
                .where(
                    audits.c.workspace_id == page["workspace_id"],
                    audits.c.target_kind == kind,
                    audits.c.target_id == page["id"],
                    audits.c.action == action,
                    _audit_version_id(audits).in_(
                        (candidate_id, str(UUID(candidate_id)))
                    ),
                )
                .order_by(audits.c.created_at.desc(), audits.c.id.desc())
                .limit(1)
            )
            .mappings()
            .first()
        )
        if event is None:
            return None
        payload = _json(event["payload"])
        if previous_key not in payload:
            return None
        previous_id = _id(payload[previous_key])
        if payload[previous_key] is not None and previous_id is None:
            return None
        pair_ids = {candidate_id}
        if previous_id is not None:
            pair_ids.add(previous_id)
        # At most the two bodies whose visible content is being compared are
        # fetched. Private, unreachable, and unrelated history is never read.
        pair = {
            _id(row["id"]): row
            for row in connection.execute(
                sa.select(versions)
                .where(
                    versions.c.workspace_id == page["workspace_id"],
                    versions.c[owner] == page["id"],
                    versions.c.id.in_(
                        [
                            _version_id(versions.c.id, version_id)
                            for version_id in pair_ids
                        ]
                    ),
                )
                .limit(2)
            ).mappings()
        }
        candidate = pair.get(candidate_id)
        previous = pair.get(previous_id)
        if candidate is None or (previous_id is not None and previous is None):
            return None
        body_column = "body_md" if kind == "context_page" else "body_markdown"
        changed = previous is None or _body(
            previous[body_column], context=kind == "context_page"
        ) != _body(candidate[body_column], context=kind == "context_page")
        if previous is not None and kind == "context_page":
            changed = (
                changed
                or previous["rendering_profile"] != candidate["rendering_profile"]
            )
        if previous is not None and kind == "agent_page":
            changed = changed or _visible_agent_meta(previous) != _visible_agent_meta(
                candidate
            )
        if changed:
            return event["created_at"]
        candidate_id = previous_id
    return None


def _backfill_proven_publications(audits):
    connection = op.get_bind()
    metadata = audits.metadata
    for kind, pointer, owner, action, previous_key in (
        (
            "context_page",
            "current_published_version_id",
            "page_id",
            "context_page.draft.publish",
            "previous_published_version_id",
        ),
        (
            "agent_page",
            "current_active_version_id",
            "agent_page_id",
            "agent_page.activated",
            "previous_active_version_id",
        ),
    ):
        pages = sa.Table(kind, metadata, autoload_with=connection)
        versions = sa.Table(f"{kind}_version", metadata, autoload_with=connection)
        after_id = None
        while True:
            statement = (
                sa.select(pages.c.id, pages.c.workspace_id, pages.c[pointer])
                .where(pages.c[pointer].is_not(None))
                .order_by(pages.c.id)
                .limit(128)
            )
            if after_id is not None:
                statement = statement.where(pages.c.id > after_id)
            batch = connection.execute(statement).mappings().all()
            if not batch:
                break
            for page in batch:
                changed_at = _publication_time(
                    connection,
                    versions,
                    audits,
                    page,
                    kind,
                    owner,
                    pointer,
                    action,
                    previous_key,
                )
                if changed_at is not None:
                    connection.execute(
                        pages.update()
                        .where(
                            pages.c.workspace_id == page["workspace_id"],
                            pages.c.id == page["id"],
                        )
                        .values(content_changed_at=changed_at)
                    )
            after_id = batch[-1]["id"]
    # Library's old upload event lacks the prior current source, and overlays
    # can replace extraction output. Its latest content event is unprovable
    # from version creation alone; unknown historical times remain NULL.


def downgrade() -> None:
    op.drop_table("user_favorite")
    for name in ("library_item", "agent_page", "context_page"):
        op.drop_column(name, "content_changed_at")

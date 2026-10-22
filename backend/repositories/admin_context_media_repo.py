"""Scoped operator metadata queries. Caller authorization stays in /admin."""

from functools import lru_cache

from sqlalchemy import Integer, and_, bindparam, or_, select

from backend.models.context import ContextPage
from backend.models.context_media import ContextMediaAsset
from backend.repositories.context_media_orphans import context_media_orphan_clause
from backend.repositories.context_visibility import foreign_editing_version_exists


@lru_cache(maxsize=16)
def _list_rows_statement(*, mine, search, orphan_only, after):
    """Reuse only the SQL shape, never workspace or request values.

    A sparse administrator search visits the full workspace roster. Rebuilding
    the same correlated visibility/orphan expression for each workspace costs
    far more than executing it against an empty workspace. The four boolean
    filters have at most sixteen immutable, parameterized statement shapes.
    Every execution still goes through that workspace's routed session.
    """
    workspace_id = bindparam("workspace_id")
    orphan = context_media_orphan_clause()
    conditions = [
        ContextMediaAsset.workspace_id == workspace_id,
        ContextPage.workspace_id == workspace_id,
        ContextMediaAsset.association_state != "tombstoned",
        ~foreign_editing_version_exists(ContextMediaAsset.created_for_version_id, None),
    ]
    if mine:
        conditions += [
            ContextMediaAsset.created_by_actor_kind == "human",
            ContextMediaAsset.created_by_actor_id == bindparam("actor_id"),
        ]
    if orphan_only:
        conditions.append(orphan)
    if search:
        conditions.append(
            ContextMediaAsset.original_filename.ilike(
                bindparam("filename_pattern"), escape="\\"
            )
        )
    if after:
        conditions.append(
            or_(
                ContextMediaAsset.created_at < bindparam("after_created_at"),
                and_(
                    ContextMediaAsset.created_at == bindparam("after_created_at"),
                    ContextMediaAsset.id < bindparam("after_id"),
                ),
            )
        )
    return (
        select(ContextMediaAsset, ContextPage.title, orphan.label("is_orphan"))
        .join(ContextPage, ContextPage.id == ContextMediaAsset.page_id)
        .where(*conditions)
        .order_by(ContextMediaAsset.created_at.desc(), ContextMediaAsset.id.desc())
        .limit(bindparam("row_limit", type_=Integer))
    )


class AdminAttachmentRepository:
    def __init__(self, session, workspace_id):
        self.session = session
        self.workspace_id = workspace_id

    def list_rows(self, *, actor_id, q, mine, orphan_only, after, limit):
        query = q.strip()
        statement = _list_rows_statement(
            mine=bool(mine),
            search=bool(query),
            orphan_only=bool(orphan_only),
            after=after is not None,
        )
        parameters = {"workspace_id": self.workspace_id, "row_limit": limit}
        if mine:
            parameters["actor_id"] = str(actor_id)
        if query:
            pattern = (
                query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            parameters["filename_pattern"] = f"%{pattern}%"
        if after is not None:
            parameters.update(
                after_created_at=after[0],
                after_id=after[1],
            )
        return self.session.execute(statement, parameters).all()

    def get_asset(self, asset_id):
        return self.session.scalar(
            select(ContextMediaAsset).where(
                ContextMediaAsset.workspace_id == self.workspace_id,
                ContextMediaAsset.id == asset_id,
                ContextMediaAsset.association_state != "tombstoned",
            )
        )

    def is_orphan(self, asset_id):
        return (
            self.session.scalar(
                select(ContextMediaAsset.id).where(
                    ContextMediaAsset.workspace_id == self.workspace_id,
                    ContextMediaAsset.id == asset_id,
                    context_media_orphan_clause(),
                )
            )
            is not None
        )

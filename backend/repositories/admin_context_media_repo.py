"""Scoped operator metadata queries. Caller authorization stays in /admin."""

from sqlalchemy import and_, or_, select

from backend.models.context import ContextPage
from backend.models.context_media import ContextMediaAsset
from backend.repositories.context_media_orphans import context_media_orphan_clause
from backend.repositories.context_visibility import foreign_editing_version_exists


class AdminAttachmentRepository:
    def __init__(self, session, workspace_id):
        self.session = session
        self.workspace_id = workspace_id

    def list_rows(self, *, actor_id, q, mine, orphan_only, after, limit):
        workspace_id = self.workspace_id
        conditions = [
            ContextMediaAsset.workspace_id == workspace_id,
            ContextPage.workspace_id == workspace_id,
            ContextMediaAsset.association_state != "tombstoned",
            ~foreign_editing_version_exists(
                ContextMediaAsset.created_for_version_id, None
            ),
        ]
        if mine:
            conditions += [
                ContextMediaAsset.created_by_actor_kind == "human",
                ContextMediaAsset.created_by_actor_id == str(actor_id),
            ]
        if orphan_only:
            conditions.append(context_media_orphan_clause())
        if q.strip():
            pattern = (
                q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            conditions.append(
                ContextMediaAsset.original_filename.ilike(f"%{pattern}%", escape="\\")
            )
        if after is not None:
            conditions.append(
                or_(
                    ContextMediaAsset.created_at < after[0],
                    and_(
                        ContextMediaAsset.created_at == after[0],
                        ContextMediaAsset.id < after[1],
                    ),
                )
            )
        return self.session.execute(
            select(
                ContextMediaAsset,
                ContextPage.title,
                context_media_orphan_clause().label("is_orphan"),
            )
            .join(ContextPage, ContextPage.id == ContextMediaAsset.page_id)
            .where(*conditions)
            .order_by(ContextMediaAsset.created_at.desc(), ContextMediaAsset.id.desc())
            .limit(limit)
        ).all()

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

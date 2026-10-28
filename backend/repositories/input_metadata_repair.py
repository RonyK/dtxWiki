"""Workspace-bound, reflected-schema projection writes for offline repairs."""

from uuid import UUID

import sqlalchemy as sa


def _key(value):
    return str(value).replace("-", "")


def _identity(column):
    return sa.func.replace(sa.cast(column, sa.String), "-", "")


def _header_field(header, field, value):
    prefix = f"{field}: "
    lines = header.split("\n")
    updated = []
    replaced = False
    for line in lines:
        if line.startswith(prefix):
            if value and not replaced:
                updated.append(prefix + value)
            replaced = True
        else:
            updated.append(line)
    if value and not replaced:
        updated.append(prefix + value)
    return "\n".join(updated)


class InputMetadataRepairRepository:
    def __init__(self, connection, workspace_id):
        self.connection = connection
        self.workspace_key = _key(workspace_id)
        self.metadata = sa.MetaData()
        self.present = set(sa.inspect(connection).get_table_names())

    def _table(self, name):
        if name not in self.metadata.tables:
            sa.Table(
                name, self.metadata, autoload_with=self.connection, resolve_fks=False
            )
        return self.metadata.tables[name]

    def _workspace(self, table):
        return _identity(table.c.workspace_id) == self.workspace_key

    def replace_page_names(self, *, key, rows):
        """Mirror all live name rows, including the draft's additional title."""
        names = self._table("page_name_trgm")
        page_key = _key(key)
        values = []
        for row in rows:
            if (
                _key(row["workspace_id"]) != self.workspace_key
                or _key(row["page_id"]) != page_key
            ):
                raise ValueError("page-name projection belongs to another owner")
            values.append(
                {
                    "name": row["name"],
                    "name_norm": row["name_norm"],
                    "name_kind": row["name_kind"],
                    "visibility_scope": row["visibility_scope"],
                    "workspace_id": str(UUID(self.workspace_key)),
                    "page_id": str(UUID(page_key)),
                    "source_version_id": str(UUID(_key(row["source_version_id"]))),
                }
            )
        self.connection.execute(
            names.delete().where(
                self._workspace(names), _identity(names.c.page_id) == page_key
            )
        )
        if values:
            self.connection.execute(names.insert(), values)

    def _related_versions(self, table_name, owner_column, key):
        versions = self._table(table_name)
        return sa.select(_identity(versions.c.id)).where(
            self._workspace(versions), _identity(versions.c[owner_column]) == _key(key)
        )

    def update_document_title(self, *, kind, key, title):
        documents = self._table("search_doc_fts")
        predicate = sa.and_(
            documents.c.source_kind == kind,
            _identity(documents.c.entity_id) == _key(key),
        )
        related = {
            "context_page": ("context_page_version", "page_id", "context_claim"),
            "library_item": (
                "library_item_version",
                "library_item_id",
                "library_artifact",
            ),
        }.get(kind)
        if related and related[0] in self.present:
            table_name, owner_column, source_kind = related
            predicate = sa.or_(
                predicate,
                sa.and_(
                    documents.c.source_kind == source_kind,
                    _identity(documents.c.version_id).in_(
                        self._related_versions(table_name, owner_column, key)
                    ),
                ),
            )
        uri = None
        if kind == "library_item" and "library_item" in self.present:
            item = self._table("library_item")
            uri = self.connection.execute(
                sa.select(item.c.uri).where(
                    self._workspace(item), _identity(item.c.id) == _key(key)
                )
            ).scalar_one()
        rows = self.connection.execute(
            sa.select(
                sa.literal_column("rowid"),
                documents.c.context_header,
                documents.c.source_kind,
            ).where(self._workspace(documents), predicate)
        ).all()
        for rowid, header, source_kind in rows:
            changes = {"context_header": _header_field(header, "Title", title)}
            if source_kind == "library_item" and uri is not None:
                changes["body"] = f"{uri}\n{title}"
            self.connection.execute(
                documents.update()
                .where(self._workspace(documents), sa.literal_column("rowid") == rowid)
                .values(**changes)
            )

    def update_breadcrumbs(self, paths):
        documents = self._table("search_doc_fts")
        version_owners = {}
        if "context_page_version" in self.present:
            versions = self._table("context_page_version")
            version_owners = {
                _key(version_id): _key(page_id)
                for version_id, page_id in self.connection.execute(
                    sa.select(versions.c.id, versions.c.page_id).where(
                        self._workspace(versions)
                    )
                )
            }
        rows = self.connection.execute(
            sa.select(
                sa.literal_column("rowid"),
                documents.c.context_header,
                documents.c.source_kind,
                documents.c.entity_id,
                documents.c.version_id,
            ).where(
                self._workspace(documents),
                documents.c.source_kind.in_(("context_page", "context_claim")),
            )
        ).all()
        for rowid, header, kind, entity_id, version_id in rows:
            owner = (
                _key(entity_id)
                if kind == "context_page"
                else version_owners.get(_key(version_id))
            )
            if owner not in paths:
                continue
            self.connection.execute(
                documents.update()
                .where(self._workspace(documents), sa.literal_column("rowid") == rowid)
                .values(
                    context_header=_header_field(header, "Breadcrumb", paths[owner])
                )
            )

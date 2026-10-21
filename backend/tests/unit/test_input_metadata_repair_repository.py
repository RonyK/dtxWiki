"""Offline repairs operate on reflected tables and preserve authored history."""

from uuid import uuid4

import pytest
import sqlalchemy as sa

from backend.repositories.input_metadata_repair import InputMetadataRepairRepository


@pytest.fixture
def connection():
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    for table, owner in (
        ("context_page_version", "page_id"),
        ("library_item_version", "library_item_id"),
    ):
        sa.Table(
            table,
            metadata,
            sa.Column("id", sa.Text, primary_key=True),
            sa.Column("workspace_id", sa.Text),
            sa.Column(owner, sa.Text),
            sa.Column("body", sa.Text),
        )
    sa.Table(
        "library_item",
        metadata,
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("workspace_id", sa.Text),
        sa.Column("uri", sa.Text),
    )
    metadata.create_all(engine)
    with engine.begin() as database:
        database.exec_driver_sql(
            "CREATE VIRTUAL TABLE search_doc_fts USING fts5("
            "context_header, body, workspace_id UNINDEXED, source_kind UNINDEXED, "
            "entity_id UNINDEXED, version_id UNINDEXED, "
            "visibility_scope UNINDEXED, category UNINDEXED)"
        )
        database.exec_driver_sql(
            "CREATE VIRTUAL TABLE page_name_trgm USING fts5("
            "name, workspace_id UNINDEXED, page_id UNINDEXED, "
            "source_version_id UNINDEXED, visibility_scope UNINDEXED, "
            "name_kind UNINDEXED, name_norm UNINDEXED, tokenize='trigram')"
        )
        yield database
    engine.dispose()


def _table(connection, name):
    return sa.Table(name, sa.MetaData(), autoload_with=connection)


def _version(connection, table, workspace, owner, identity=None):
    identity = identity or uuid4()
    owner_column = "page_id" if table == "context_page_version" else "library_item_id"
    connection.execute(
        _table(connection, table)
        .insert()
        .values(
            id=identity.hex,
            workspace_id=workspace.hex,
            **{owner_column: owner.hex},
            body="# Unchanged historic H1\nUnchanged historic content",
        )
    )
    return identity


def _document(connection, workspace, kind, entity, version, *, body="authored body"):
    connection.execute(
        _table(connection, "search_doc_fts")
        .insert()
        .values(
            workspace_id=str(workspace),
            source_kind=kind,
            entity_id=str(entity),
            version_id=str(version),
            visibility_scope="published",
            category="original category",
            context_header=(
                "Title: Old title\nPath: /original/path\n"
                "Breadcrumb: UniqueOldBreadcrumb / Old title\n"
                "Heading: Historic H1\nClaim type: original"
            ),
            body=body,
        )
    )


def _documents(connection):
    return {
        (row["workspace_id"], row["entity_id"]): dict(row)
        for row in connection.execute(sa.select(_table(connection, "search_doc_fts")))
        .mappings()
        .all()
    }


def test_context_title_updates_claim_headers_without_touching_history_or_tenants(
    connection,
):
    workspace, other_workspace, page, other_page = [uuid4() for _ in range(4)]
    version = _version(connection, "context_page_version", workspace, page)
    other_version = _version(connection, "context_page_version", workspace, other_page)
    claim, other_claim = uuid4(), uuid4()
    _document(connection, workspace, "context_page", page, version)
    _document(connection, workspace, "context_claim", claim, version)
    _document(connection, workspace, "context_claim", other_claim, other_version)
    _document(connection, other_workspace, "context_claim", claim, version)
    original = _documents(connection)

    InputMetadataRepairRepository(connection, workspace).update_document_title(
        kind="context_page", key=page.hex, title="Repaired title"
    )

    updated = _documents(connection)
    for identity in (page, claim):
        before, after = (
            original[(str(workspace), str(identity))],
            updated[(str(workspace), str(identity))],
        )
        assert after == {
            **before,
            "context_header": before["context_header"].replace(
                "Title: Old title", "Title: Repaired title"
            ),
        }
    for tenant, identity in ((workspace, other_claim), (other_workspace, claim)):
        assert (
            updated[(str(tenant), str(identity))]
            == original[(str(tenant), str(identity))]
        )
    assert connection.execute(
        sa.select(_table(connection, "context_page_version").c.body)
    ).scalars().all() == [
        "# Unchanged historic H1\nUnchanged historic content",
        "# Unchanged historic H1\nUnchanged historic content",
    ]


def test_library_repair_updates_generated_body_and_related_header_only(connection):
    workspace, other_workspace, item, artifact = [uuid4() for _ in range(4)]
    uri, title = "https://item.example/source", "Repaired title"
    connection.execute(
        _table(connection, "library_item")
        .insert()
        .values(id=item.hex, workspace_id=workspace.hex, uri=uri)
    )
    version = _version(connection, "library_item_version", workspace, item)
    _document(
        connection, workspace, "library_item", item, version, body="old uri/title"
    )
    _document(connection, workspace, "library_artifact", artifact, version)
    _document(connection, other_workspace, "library_artifact", artifact, version)
    original = _documents(connection)

    InputMetadataRepairRepository(connection, workspace).update_document_title(
        kind="library_item", key=item, title=title
    )

    updated = _documents(connection)
    assert updated[(str(workspace), str(item))]["body"] == f"{uri}\n{title}"
    assert updated[(str(workspace), str(artifact))]["body"] == "authored body"
    for identity in (item, artifact):
        assert updated[(str(workspace), str(identity))]["context_header"] == original[
            (str(workspace), str(identity))
        ]["context_header"].replace("Title: Old title", f"Title: {title}")
    assert (
        updated[(str(other_workspace), str(artifact))]
        == original[(str(other_workspace), str(artifact))]
    )


@pytest.mark.parametrize("breadcrumb", ["Root / Repaired folder / Page", ""])
def test_breadcrumb_repair_updates_page_and_claim_without_changing_body(
    connection, breadcrumb
):
    workspace, other_workspace, page, claim = [uuid4() for _ in range(4)]
    version = _version(connection, "context_page_version", workspace, page)
    _document(connection, workspace, "context_page", page, version)
    _document(connection, workspace, "context_claim", claim, version)
    _document(connection, other_workspace, "context_claim", claim, version)
    original = _documents(connection)

    InputMetadataRepairRepository(connection, workspace).update_breadcrumbs(
        {page.hex: breadcrumb}
    )

    updated = _documents(connection)
    for identity in (page, claim):
        row = updated[(str(workspace), str(identity))]
        assert row["body"] == "authored body"
        assert "Title: Old title" in row["context_header"]
        assert "Heading: Historic H1" in row["context_header"]
        assert "UniqueOldBreadcrumb" not in row["context_header"]
        if breadcrumb:
            assert f"Breadcrumb: {breadcrumb}" in row["context_header"]
        else:
            assert "Breadcrumb:" not in row["context_header"]
    assert (
        updated[(str(other_workspace), str(claim))]
        == original[(str(other_workspace), str(claim))]
    )


def test_name_projection_preserves_pending_draft_names_and_validates_before_delete(
    connection,
):
    workspace, other_workspace, page, version = [uuid4() for _ in range(4)]
    names = _table(connection, "page_name_trgm")
    connection.execute(
        names.insert(),
        [
            dict(
                workspace_id=str(tenant),
                page_id=str(page),
                source_version_id=str(version),
                visibility_scope="draft",
                name="Old title",
                name_norm="old title",
                name_kind="title",
            )
            for tenant in (workspace, other_workspace)
        ],
    )
    rows = [
        dict(
            workspace_id=workspace.hex,
            page_id=page.hex,
            source_version_id=version.hex,
            visibility_scope="draft",
            name=name,
            name_norm=name.lower(),
            name_kind=kind,
        )
        for name, kind in (
            ("Canonical repaired", "title"),
            ("Pending separate", "title"),
            ("Alias retained", "alias"),
            ("slug-retained", "slug"),
        )
    ]
    repository = InputMetadataRepairRepository(connection, workspace)
    repository.replace_page_names(key=page, rows=rows)
    snapshot = connection.execute(sa.select(names)).mappings().all()
    own_names = [
        row["name"] for row in snapshot if row["workspace_id"] == str(workspace)
    ]
    assert own_names == [row["name"] for row in rows]
    assert [
        row["name"] for row in snapshot if row["workspace_id"] == str(other_workspace)
    ] == ["Old title"]
    invalid_rows = [{**rows[0], "workspace_id": other_workspace.hex}]
    with pytest.raises(ValueError, match="another owner"):
        repository.replace_page_names(key=page, rows=invalid_rows)
    assert connection.execute(sa.select(names)).mappings().all() == snapshot

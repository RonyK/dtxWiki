"""Reference title projection never hydrates cited documents' full bodies."""

from sqlalchemy import event, inspect, select

from backend.models.context import ContextPage, ContextPageVersion
from backend.models.workspace import Workspace
from backend.services.document_reference_titles import context_reference_titles


def test_reference_title_batch_uses_version_metadata_then_bounded_body(session_factory):
    # Budget exception: a real ORM relationship can silently load every full
    # cited body. The migration traversal and Markdown unit checks cannot
    # observe that failure. One mixed batch checks the title precedence and SQL
    # bound together, without another dialect or reader matrix.
    with session_factory.begin() as session:
        workspace_id = session.scalar(select(Workspace.id))
        ledger = ContextPage(
            workspace_id=workspace_id, slug="ledger", title="Later private title"
        )
        root = ContextPage(
            workspace_id=workspace_id, slug="root", title="First editing title"
        )
        published = ContextPage(
            workspace_id=workspace_id, slug="published", title="Private draft title"
        )
        session.add_all([ledger, root, published])
        session.flush()

        def version(page, status, body, **kwargs):
            row = ContextPageVersion(
                workspace_id=workspace_id,
                page_id=page.id,
                body_md=body + "\n" + "large body " * 10_000,
                content_hash="test",
                actor_type="human",
                created_by_actor_kind="human",
                created_by_actor_id="reference-title-test",
                version_status=status,
                **kwargs,
            )
            session.add(row)
            session.flush()
            return row

        ledger_version = version(ledger, "draft", "# Ledger body")
        root_version = version(root, "draft", "# Root body")
        published_version = version(published, "published_snapshot", "# Public title")
        private_version = version(
            published,
            "draft",
            "# Private draft body",
            version_no=2,
            parent_version_id=published_version.id,
        )
        ledger.current_version_id = ledger_version.id
        root.current_version_id = root_version.id
        published.current_version_id = private_version.id
        published.current_published_version_id = published_version.id
        ledger_version.page_metadata = {"title": "Selected version title"}
        root_version.page_metadata = {"title": "First editing title"}
        visible = {
            ledger.id: ledger_version.id,
            root.id: root_version.id,
            published.id: published_version.id,
        }
        expected = {
            ledger.id: "Selected version title",
            root.id: "First editing title",
            published.id: "Public title",
        }

    statements = []

    def capture(_connection, _cursor, statement, parameters, *_args):
        statements.append((statement, parameters))

    engine = session_factory.kw["bind"]
    with session_factory() as session:
        pages = {
            page.id: page
            for page in session.scalars(
                select(ContextPage).where(ContextPage.id.in_(visible))
            )
        }
        event.listen(engine, "before_cursor_execute", capture)
        try:
            assert (
                context_reference_titles(
                    session,
                    workspace_id=workspace_id,
                    pages=pages,
                    visible_versions=visible,
                )
                == expected
            )
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert all(
            "current_version" in inspect(page).unloaded for page in pages.values()
        )

    assert len(statements) == 2
    assert "page_metadata" in statements[0][0]
    assert "body_md" not in statements[0][0]
    body_reads = [sql for sql, _parameters in statements if "body_md" in sql]
    assert len(body_reads) == 1
    assert "substr(context_page_version.body_md" in body_reads[0]

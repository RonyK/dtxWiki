"""G0 relationship normalization inside the consolidated Epic-1 migration
(efcbcf724889): legacy-shape data laid down at the 0001 baseline must land in
the join tables, and downgrade must reproduce the JSON encodings."""

from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from backend.tests.utils.migrated_sqlite_templates import shared_templates

pytestmark = pytest.mark.integration

BASELINE = "bd62193d5cf6"


@pytest.fixture(scope="session")
def sqlite_templates(tmp_path_factory):
    return shared_templates(tmp_path_factory)


def _config(tmp_path, templates):
    """A config on a fresh copy of the 0001 baseline."""
    database_url = templates.clone("base_0001", tmp_path / "g0.db")
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config, database_url


def _seed_workspace(connection):
    # Uuid columns store 32-char hex on SQLite; JSON payloads written by the
    # ORM hold dashed str(UUID) values — mirror both storage formats.
    workspace_id = uuid4().hex
    connection.execute(
        text(
            "INSERT INTO workspace (id, created_at, updated_at, slug, title,"
            " name, goal, default_language, library_policy, write_policy,"
            " citation_policy, agent_injection_policy, review_policy,"
            " lint_policy, auto_fix_enabled, visibility, created_by_actor_id,"
            " owner_actor_id)"
            " VALUES (:id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'g0-ws',"
            " 'G0', 'G0', 'test', 'en', '{}', '{}', '{}', '{}', '{}', '{}',"
            " 0, 'private', :actor, :actor)"
        ),
        {"id": workspace_id, "actor": uuid4().hex},
    )
    return workspace_id


def _seed_folder_node(connection, workspace_id):
    node_id = uuid4().hex
    connection.execute(
        text(
            "INSERT INTO workspace_tree_node (id, created_at, updated_at,"
            " workspace_id, section, node_kind, parent_id, sort_key, title,"
            " slug, target_kind, target_id, is_system_node, can_have_children,"
            " primary_placement, visibility_status, created_by_actor_type,"
            " created_by_actor_id)"
            " VALUES (:id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, :ws, 'ask',"
            " 'folder', NULL, 'g0', 'G0 Root', 'g0-root', NULL, NULL, 1, 1,"
            " 0, 'visible', NULL, NULL)"
        ),
        {"id": node_id, "ws": workspace_id},
    )
    return node_id


def _seed_library_item(connection, workspace_id, item_id, *, suffix):
    connection.execute(
        text(
            "INSERT INTO library_item (id, created_at, updated_at, workspace_id,"
            " connector_type, format_hint, uri, metadata, trust_level,"
            " lifecycle_status)"
            " VALUES (:id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, :ws, 'url',"
            " 'html', :uri, '{}', 'normal', 'ready')"
        ),
        {
            "id": UUID(item_id).hex,
            "ws": workspace_id,
            "uri": f"https://example.com/g0/{suffix}",
        },
    )


def _seed_workspace_session_turn(connection, *, cited_pages, cited_artifacts):
    workspace_id = _seed_workspace(connection)
    node_id = _seed_folder_node(connection, workspace_id)
    session_id = uuid4().hex
    turn_id = uuid4().hex
    connection.execute(
        text(
            "INSERT INTO ask_session (id, created_at, workspace_id, title,"
            " title_user_edited, actor_kind, default_topic_tags,"
            " placement_tree_node_id, started_at, updated_at)"
            " VALUES (:id, CURRENT_TIMESTAMP, :ws, 'G0 Session', 0, 'human',"
            " '[]', :node, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        {"id": session_id, "ws": workspace_id, "node": node_id},
    )
    connection.execute(
        text(
            "INSERT INTO ask_turn (id, created_at, ask_session_id, turn_no,"
            " user_message, assistant_message, cited_context_page_ids,"
            " cited_library_artifact_ids, topic_tags,"
            " context_ingress_proposal_ids, promotion_state)"
            " VALUES (:id, CURRENT_TIMESTAMP, :sid, 1, 'q', 'a', :pages,"
            " :artifacts, '[]', '[]', 'not_promoted')"
        ),
        {
            "id": turn_id,
            "sid": session_id,
            "pages": cited_pages,
            "artifacts": cited_artifacts,
        },
    )
    return turn_id


def _seed_proposal(connection, *, workspace_id, turn_id, source_kind, item_ids):
    proposal_id = uuid4().hex
    node_id = connection.execute(
        text(
            "SELECT id FROM workspace_tree_node WHERE workspace_id = :ws "
            "ORDER BY id LIMIT 1"
        ),
        {"ws": workspace_id},
    ).scalar_one()
    connection.execute(
        text(
            "INSERT INTO context_ingress_proposal (id, created_at, updated_at,"
            " workspace_id, source_kind, library_item_ids,"
            " proposed_context_page_type, proposed_parent_tree_node_id,"
            " proposed_title, proposed_save_mode, placement_confidence,"
            " alternatives, status)"
            " VALUES (:id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, :ws, :kind,"
            " :items, 'overview', :node, 'T', 'create_page', 'medium', '[]',"
            " 'proposed')"
        ),
        {
            "id": proposal_id,
            "ws": workspace_id,
            "kind": source_kind,
            "items": item_ids,
            "node": node_id,
        },
    )
    if turn_id is not None:
        connection.execute(
            text(
                "UPDATE ask_turn SET context_ingress_proposal_ids = :pids"
                " WHERE id = :tid"
            ),
            {"pids": f'["{UUID(proposal_id)}"]', "tid": turn_id},
        )
    return proposal_id


def test_backfills_and_drops_citation_arrays(tmp_path, sqlite_templates):
    config, database_url = _config(tmp_path, sqlite_templates)

    page_a, page_b, artifact = str(uuid4()), str(uuid4()), str(uuid4())
    engine = create_engine(database_url)
    with engine.begin() as connection:
        # Duplicate page_a inside the array: backfill must collapse it while
        # preserving first-occurrence rank order.
        turn_id = _seed_workspace_session_turn(
            connection,
            cited_pages=f'["{page_a}", "{page_b}", "{page_a}"]',
            cited_artifacts=f'["{artifact}"]',
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT target_kind, target_id, rank FROM ask_turn_citation"
                " WHERE ask_turn_id = :tid ORDER BY target_kind, rank"
            ),
            {"tid": UUID(turn_id).hex},
        ).all()
        turn_columns = {
            column["name"] for column in inspect(engine).get_columns("ask_turn")
        }

    assert [(kind, UUID(tid), rank) for kind, tid, rank in rows] == [
        ("context_page", UUID(page_a), 0),
        ("context_page", UUID(page_b), 1),
        ("library_artifact", UUID(artifact), 0),
    ]
    assert "cited_context_page_ids" not in turn_columns
    assert "cited_library_artifact_ids" not in turn_columns

    # Downgrade restores the JSON encoding from the join rows.
    command.downgrade(config, BASELINE)
    with engine.connect() as connection:
        restored = connection.execute(
            text(
                "SELECT cited_context_page_ids, cited_library_artifact_ids"
                " FROM ask_turn WHERE id = :tid"
            ),
            {"tid": turn_id},
        ).one()
    assert page_a.lower() in restored[0].lower()
    assert artifact.lower() in restored[1].lower()


def test_backfills_proposal_sources(tmp_path, sqlite_templates):
    config, database_url = _config(tmp_path, sqlite_templates)

    engine = create_engine(database_url)
    item_a, item_b = str(uuid4()), str(uuid4())
    with engine.begin() as connection:
        turn_id = _seed_workspace_session_turn(
            connection, cited_pages="[]", cited_artifacts="[]"
        )
        ws = connection.execute(text("SELECT id FROM workspace")).scalar_one()
        _seed_library_item(connection, ws, item_a, suffix="item-a")
        _seed_library_item(connection, ws, item_b, suffix="item-b")
        # Legacy ask_turn misuse: library_item_ids holds the source turn id.
        ask_proposal = _seed_proposal(
            connection,
            workspace_id=ws,
            turn_id=turn_id,
            source_kind="ask_turn",
            item_ids=f'["{UUID(turn_id)}"]',
        )
        library_proposal = _seed_proposal(
            connection,
            workspace_id=ws,
            turn_id=None,
            source_kind="library_item",
            item_ids=f'["{item_a}", "{item_b}", "{item_a}"]',
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        source_turn = connection.execute(
            text(
                "SELECT source_ask_turn_id FROM context_ingress_proposal"
                " WHERE id = :pid"
            ),
            {"pid": ask_proposal},
        ).scalar_one()
        items = connection.execute(
            text(
                "SELECT library_item_id, rank FROM proposal_source_item"
                " WHERE proposal_id = :pid ORDER BY rank"
            ),
            {"pid": library_proposal},
        ).all()
        proposal_columns = {
            column["name"]
            for column in inspect(engine).get_columns("context_ingress_proposal")
        }
        turn_columns = {
            column["name"] for column in inspect(engine).get_columns("ask_turn")
        }

    assert UUID(source_turn) == UUID(turn_id)
    assert [(UUID(item), rank) for item, rank in items] == [
        (UUID(item_a), 0),
        (UUID(item_b), 1),
    ]
    assert "library_item_ids" not in proposal_columns
    assert "context_ingress_proposal_ids" not in turn_columns

    # Downgrade restores both legacy JSON encodings.
    command.downgrade(config, BASELINE)
    with engine.connect() as connection:
        restored_items = connection.execute(
            text(
                "SELECT library_item_ids FROM context_ingress_proposal WHERE id = :pid"
            ),
            {"pid": library_proposal},
        ).scalar_one()
        restored_turn = connection.execute(
            text("SELECT context_ingress_proposal_ids FROM ask_turn WHERE id = :tid"),
            {"tid": turn_id},
        ).scalar_one()
    assert item_a.lower() in restored_items.lower()
    assert item_b.lower() in restored_items.lower()
    assert str(UUID(ask_proposal)) in restored_turn.lower()


def test_backfills_page_provenance(tmp_path, sqlite_templates):
    config, database_url = _config(tmp_path, sqlite_templates)

    engine = create_engine(database_url)
    turn_a, turn_b = str(uuid4()), str(uuid4())
    page_id = uuid4().hex
    with engine.begin() as connection:
        ws = _seed_workspace(connection)
        # Duplicate turn_a: backfill must dedup while keeping both sources.
        connection.execute(
            text(
                "INSERT INTO context_page (id, created_at, updated_at,"
                " workspace_id, slug, title, page_type, lifecycle_status,"
                " aliases, tags, created_from_ask_turn_ids)"
                " VALUES (:id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, :ws,"
                " 'g0-page', 'G0 Page', 'concept', 'draft', '[]', '[]',"
                " :turns)"
            ),
            {
                "id": page_id,
                "ws": ws,
                "turns": f'["{turn_a}", "{turn_b}", "{turn_a}"]',
            },
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT source_kind, source_id FROM page_provenance"
                " WHERE page_id = :pid ORDER BY source_id"
            ),
            {"pid": page_id},
        ).all()
        page_columns = {
            column["name"] for column in inspect(engine).get_columns("context_page")
        }

    assert sorted(UUID(source_id) for _, source_id in rows) == sorted(
        [UUID(turn_a), UUID(turn_b)]
    )
    assert all(kind == "ask_turn" for kind, _ in rows)
    assert "created_from_ask_turn_ids" not in page_columns

    # Downgrade restores the JSON encoding from the join rows.
    command.downgrade(config, BASELINE)
    with engine.connect() as connection:
        restored = connection.execute(
            text("SELECT created_from_ask_turn_ids FROM context_page WHERE id = :pid"),
            {"pid": page_id},
        ).scalar_one()
    assert turn_a.lower() in restored.lower()
    assert turn_b.lower() in restored.lower()


def test_backfills_sync_run_lineage(tmp_path, sqlite_templates):
    config, database_url = _config(tmp_path, sqlite_templates)

    engine = create_engine(database_url)
    item_id, version_id, run_id = uuid4().hex, uuid4().hex, uuid4().hex
    with engine.begin() as connection:
        ws = _seed_workspace(connection)
        connection.execute(
            text(
                "INSERT INTO library_item (id, created_at, updated_at,"
                " workspace_id, connector_type, format_hint, uri, metadata,"
                " trust_level, lifecycle_status)"
                " VALUES (:id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, :ws,"
                " 'url', 'html', 'https://example.com/g0', '{}', 'normal',"
                " 'ready')"
            ),
            {"id": item_id, "ws": ws},
        )
        connection.execute(
            text(
                "INSERT INTO library_item_version (id, created_at, updated_at,"
                " library_item_id, version_no, content_hash, fetched_at,"
                " raw_storage_ref, processing_status)"
                " VALUES (:id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, :item, 1,"
                " 'hash', CURRENT_TIMESTAMP, 'ref', 'ready')"
            ),
            {"id": version_id, "item": item_id},
        )
        connection.execute(
            text(
                "INSERT INTO library_sync_run (id, created_at, updated_at,"
                " workspace_id, library_item_id, trigger_type, status,"
                " changed_count, noop_count, failed_count,"
                " created_library_item_version_ids)"
                " VALUES (:id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, :ws,"
                " :item, 'manual', 'completed', 1, 0, 0, :versions)"
            ),
            {
                "id": run_id,
                "ws": ws,
                "item": item_id,
                "versions": f'["{UUID(version_id)}"]',
            },
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        linked_run = connection.execute(
            text("SELECT sync_run_id FROM library_item_version WHERE id = :vid"),
            {"vid": version_id},
        ).scalar_one()
        run_columns = {
            column["name"] for column in inspect(engine).get_columns("library_sync_run")
        }

    assert UUID(linked_run) == UUID(run_id)
    assert "created_library_item_version_ids" not in run_columns

    # Downgrade restores the JSON encoding from the FK.
    command.downgrade(config, BASELINE)
    with engine.connect() as connection:
        restored = connection.execute(
            text(
                "SELECT created_library_item_version_ids FROM library_sync_run"
                " WHERE id = :rid"
            ),
            {"rid": run_id},
        ).scalar_one()
    assert version_id.lower() in restored.lower().replace("-", "")


def test_raises_on_broken_pairing(tmp_path, sqlite_templates):
    config, database_url = _config(tmp_path, sqlite_templates)

    engine = create_engine(database_url)
    with engine.begin() as connection:
        turn_id = _seed_workspace_session_turn(
            connection, cited_pages="[]", cited_artifacts="[]"
        )
        ws = connection.execute(text("SELECT id FROM workspace")).scalar_one()
        _seed_proposal(
            connection,
            workspace_id=ws,
            turn_id=None,  # turn never records the proposal back
            source_kind="ask_turn",
            item_ids=f'["{UUID(turn_id)}"]',
        )

    with pytest.raises(Exception, match="does not"):
        command.upgrade(config, "head")

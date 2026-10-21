"""Legacy history reports use reflected partial schemas and never mutate data."""

import json
from uuid import UUID

import sqlalchemy as sa

from backend.services.ops import input_history_audit
from backend.services.ops.input_history_audit import audit_preserved_inputs


def identity(number):
    return UUID(int=number).hex


def make_table(metadata, name, *fields):
    return sa.Table(
        name,
        metadata,
        sa.Column("id", sa.Text, primary_key=True),
        *(sa.Column(field, kind) for field, kind in fields),
    )


def report_map(connection):
    return {item["rule"]: item for item in audit_preserved_inputs(connection)}


def test_scalar_history_units_bounded_ids_and_read_only_file(tmp_path):
    path = tmp_path / "legacy.sqlite"
    engine = sa.create_engine(f"sqlite+pysqlite:///{path}")
    metadata = sa.MetaData()
    context = make_table(metadata, "context_page_version", ("body_md", sa.Text))
    overlay = make_table(
        metadata, "library_semantic_overlay", ("body_markdown", sa.Text)
    )
    # A historical table need not have all current columns.
    make_table(metadata, "agent_page_version", ("old_body", sa.Text))
    changes = make_table(metadata, "change_set", ("reason", sa.Text))
    review = make_table(
        metadata, "review_item", ("review_type", sa.Text), ("decision_summary", sa.Text)
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            context.insert(),
            [
                {"id": identity(1), "body_md": "한" * 1_000_000},
                {"id": identity(2), "body_md": "a" * 1_000_001},
            ],
        )
        connection.execute(
            overlay.insert(),
            [
                {"id": identity(3), "body_markdown": "한" * 333_333 + "a"},
                {"id": identity(4), "body_markdown": "한" * 333_334},
            ],
        )
        connection.execute(
            changes.insert(),
            [
                {"id": identity(index), "reason": "private-reason-" + "r" * 2000}
                for index in range(1, 9)
            ],
        )
        connection.execute(
            review.insert(),
            [
                {
                    "id": identity(12),
                    "review_type": "page_inline_comment",
                    "decision_summary": "n" * 5000,
                },
                {
                    "id": identity(13),
                    "review_type": "page_inline_comment",
                    "decision_summary": "n" * 5001,
                },
                {
                    "id": identity(14),
                    "review_type": "draft",
                    "decision_summary": "n" * 2001,
                },
            ],
        )
    before = path.read_bytes()
    statements = []
    sa.event.listen(
        engine, "before_cursor_execute", lambda _c, _u, sql, *_: statements.append(sql)
    )
    with engine.connect() as connection:
        reports = report_map(connection)
    assert reports["preserved_context_page_version_body_md"]["ids"] == [
        str(UUID(int=2))
    ]
    assert reports["preserved_library_semantic_overlay_body_markdown"]["ids"] == [
        str(UUID(int=4))
    ]
    assert reports["preserved_change_set_reason"]["count"] == 8
    assert len(reports["preserved_change_set_reason"]["ids"]) == 5
    assert reports["preserved_review_item_decision_summary"]["count"] == 2
    assert all(
        item["classification"] == "preserved_history" for item in reports.values()
    )
    assert "private-reason" not in json.dumps(reports)
    assert all(
        sql.lstrip().upper().startswith(("SELECT", "PRAGMA")) for sql in statements
    )
    assert path.read_bytes() == before
    engine.dispose()


def test_labels_and_nested_request_snapshots_without_limiting_generated_outputs():
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    metadata = sa.MetaData()
    context = make_table(
        metadata, "context_page", ("tags", sa.JSON), ("aliases", sa.JSON)
    )
    audit = make_table(metadata, "audit_event", ("payload", sa.JSON))
    bundle = make_table(metadata, "agent_input_bundle", ("invalidation_keys", sa.JSON))
    agent = make_table(metadata, "agent_page_version", ("frontmatter", sa.JSON))
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            context.insert(),
            [
                {
                    "id": identity(1),
                    "tags": ["😀" * 48] * 32,
                    "aliases": ["a" * 80] * 32,
                },
                {"id": identity(2), "tags": ["a"] * 33, "aliases": ["a" * 81]},
            ],
        )
        connection.execute(
            audit.insert(),
            {
                "id": identity(3),
                "payload": {
                    "reason": "secret" + "r" * 2000,
                    "request": {
                        "operations": [{}] * 21,
                        "context_refs": [identity(1)] * 129,
                    },
                },
            },
        )
        connection.execute(
            bundle.insert(),
            {
                "id": identity(4),
                "invalidation_keys": {
                    "context_version_refs": [identity(1)] * 257,
                },
            },
        )
        connection.execute(
            agent.insert(),
            {
                "id": identity(5),
                "frontmatter": {
                    "reason": "arbitrary document metadata" * 1000,
                    "tags": ["a"] * 33,
                },
            },
        )
        reports = report_map(connection)
    assert reports["preserved_context_page_tags"]["count"] == 1
    assert reports["preserved_context_page_aliases"]["count"] == 1
    assert reports["preserved_audit_event_payload_operations"]["count"] == 1
    assert reports["preserved_audit_event_payload_context_refs"]["count"] == 1
    assert reports["preserved_agent_page_version_frontmatter_tags"]["count"] == 1
    assert not any(
        "invalidation_keys" in rule or "frontmatter_reason" in rule for rule in reports
    )


def test_large_live_grants_are_reported_without_revocation_or_identity_leak():
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    metadata = sa.MetaData()
    pat = sa.Table(
        "api_token_workspaces",
        metadata,
        sa.Column("token_id", sa.Text),
        sa.Column("workspace_id", sa.Text),
        sa.Column("removed_at", sa.Text),
    )
    oauth = sa.Table(
        "oauth_consents",
        metadata,
        sa.Column("user_id", sa.Text),
        sa.Column("client_id", sa.Text),
        sa.Column("workspace_id", sa.Text),
        sa.Column("removed_at", sa.Text),
    )
    metadata.create_all(engine)
    private_client = "https://private-client.example/secret-registration"
    with engine.begin() as connection:
        connection.execute(
            pat.insert(),
            [
                {
                    "token_id": identity(1),
                    "workspace_id": identity(index + 100),
                    "removed_at": None,
                }
                for index in range(257)
            ]
            + [
                {
                    "token_id": identity(2),
                    "workspace_id": identity(index + 100),
                    "removed_at": None if index < 256 else "removed",
                }
                for index in range(257)
            ],
        )
        connection.execute(
            oauth.insert(),
            [
                {
                    "user_id": identity(3),
                    "client_id": private_client,
                    "workspace_id": identity(index + 100),
                    "removed_at": None,
                }
                for index in range(257)
            ],
        )
        reports = report_map(connection)
        assert connection.scalar(sa.select(sa.func.count()).select_from(pat)) == 514
        assert connection.scalar(sa.select(sa.func.count()).select_from(oauth)) == 257
    assert reports["preserved_api_token_workspaces_workspace_grants"]["ids"] == [
        str(UUID(int=1))
    ]
    assert reports["preserved_oauth_consents_workspace_grants"]["count"] == 1
    assert (
        reports["preserved_oauth_consents_workspace_grants"]["classification"]
        == "preserved_grants"
    )
    assert private_client not in json.dumps(reports)


def test_oauth_reports_types_controls_and_empty_metadata_without_raw_client_ids():
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    metadata = sa.MetaData()
    clients = make_table(
        metadata,
        "oauth_clients",
        ("client_name", sa.Text),
        ("redirect_uris", sa.JSON),
        ("grant_types", sa.JSON),
    )
    metadata.create_all(engine)
    base = {
        "client_name": "Client",
        "redirect_uris": ["https://client.example/cb"],
        "grant_types": ["authorization_code"],
    }
    with engine.begin() as connection:
        connection.execute(
            clients.insert(),
            [
                {"id": "https://private.example/" + str(index), **base, **bad}
                for index, bad in enumerate(
                    [
                        {"client_name": "bad\nname"},
                        {"redirect_uris": []},
                        {"grant_types": "authorization_code"},
                    ]
                )
            ]
            + [{"id": "valid-client", **base}],
        )
        reports = report_map(connection)
        assert connection.scalar(sa.select(sa.func.count()).select_from(clients)) == 4
    report = reports["oauth_client_protocol_budget"]
    assert report["count"] == 3
    assert len(report["id_fingerprints"]) == 3
    assert report["classification"] == "preserved_oauth_metadata"
    assert "private.example" not in json.dumps(reports)


def test_invalid_legacy_json_is_reported_without_loading_current_models(monkeypatch):
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    metadata = sa.MetaData()
    audit = make_table(metadata, "audit_event", ("payload", sa.Text))
    metadata.create_all(engine)
    deep = "[" * 2000 + "0" + "]" * 2000
    decode = input_history_audit._json_value

    def nesting_limited_decoder(value):
        # JSON parser nesting ceilings differ by Python/platform. Exercise its
        # documented exception directly instead of depending on that ceiling.
        if value == deep:
            raise RecursionError("JSON parser nesting limit")
        return decode(value)

    monkeypatch.setattr(input_history_audit, "_json_value", nesting_limited_decoder)
    with engine.begin() as connection:
        connection.execute(
            audit.insert(),
            [
                {"id": identity(1), "payload": "private invalid JSON"},
                {"id": identity(2), "payload": deep},
            ],
        )
        reports = report_map(connection)
    assert reports["preserved_audit_event_payload_invalid_json"]["count"] == 2
    assert "private invalid JSON" not in json.dumps(reports)


def test_library_policy_list_and_name_budgets_preserve_old_policy_json():
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    metadata = sa.MetaData()
    workspace = make_table(metadata, "workspace", ("library_policy", sa.JSON))
    metadata.create_all(engine)
    valid = {
        "format_families": [{"name": "😀" * 80}] * 6,
        "custom_formats": [{"name": "Custom", "extensions": ["." + "x" * 32] * 16}]
        * 64,
    }
    invalid = {
        "format_families": [{"name": "a" * 81}] * 7,
        "custom_formats": [{"name": "Custom", "extensions": [".x"] * 17}] * 65,
    }
    with engine.begin() as connection:
        connection.execute(
            workspace.insert(),
            [
                {"id": identity(1), "library_policy": valid},
                {"id": identity(2), "library_policy": invalid},
            ],
        )
        reports = report_map(connection)
        assert (
            connection.scalar(
                sa.select(workspace.c.library_policy).where(
                    workspace.c.id == identity(2)
                )
            )
            == invalid
        )
    for field in ("format_families", "custom_formats", "format_names", "extensions"):
        report = reports[f"preserved_workspace_library_policy_{field}"]
        assert report["count"] == 1
        assert report["ids"] == [str(UUID(int=2))]

"""Distinct operator error paths, without per-value migration databases."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from typer.testing import CliRunner

from backend.catalog.mcp_apps import get_mcp_app_catalog_repository
from backend.cli.commands import admin, inputs
from backend.cli.main import app
from backend.domain.input_policy import InputPolicyError
from backend.migrations.revision_modules.v0_1.input_restrictions_v1 import audit
from backend.models.user import User
from backend.models.workspace import Workspace
from backend.schemas.auth import ApiTokenCreateRequest
from backend.schemas.read import WorkspaceCreateRequest
from backend.services.api_token_service import resolve_catalog_app_association
from backend.services.ops.input_maintenance import repair_input
from backend.services.session_service import SessionService, SessionTokenCodec
from backend.services.settings_service import _normalize_workspace_slug
from backend.services.user_admin_service import UserAdminService
from backend.services.workspace_write import (
    WorkspaceWriteService,
    normalize_workspace_slug,
)
from backend.utils.database_errors import is_single_column_unique_violation


def test_invalid_cli_emails_and_pat_names_exit_two_before_bootstrap(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("invalid CLI input reached database provisioning")

    monkeypatch.setattr(admin, "bootstrap_app", forbidden)
    monkeypatch.setattr(admin, "make_workspace_membership_service", forbidden)
    monkeypatch.setattr(admin, "password_recovery_context", forbidden)
    runner = CliRunner()
    for email in ("a@@example.test", "a" * 65 + "@example.test", "a@" + "b" * 253):
        for command in (
            ["workspace", "members", "add", email, "--workspace", "default"],
            ["workspace", "members", "remove", email, "--workspace", "default"],
            ["users", "issue-password-reset", email],
        ):
            result = runner.invoke(app, command)
            assert result.exit_code == 2, result.output
            assert "Traceback" not in result.output
    for name in ("x" * 129, "line\nbreak", ""):
        result = runner.invoke(
            app,
            [
                "users",
                "issue-pat",
                "--user",
                "a@example.test",
                "--workspace",
                "default",
                "--name",
                name,
                "--scope",
                "mcp:read",
            ],
        )
        assert result.exit_code == 2, result.output
        assert "issue-pat failed" in result.output


def test_audit_cli_distinguishes_preserved_findings_blockers_and_refusal(monkeypatch):
    monkeypatch.setattr(inputs, "_url", lambda value: value)
    preserved = {"rule": "history", "classification": "preserved_history", "count": 1}
    blocker = {"rule": "scalar", "classification": "blocking_scalar", "count": 1}
    runner = CliRunner()
    for report, expected in (
        ([], 0),
        ([preserved], 0),
        ([blocker], 1),
        ([preserved, blocker], 1),
    ):
        monkeypatch.setattr(inputs, "audit_inputs", lambda _url, result=report: result)
        result = runner.invoke(app, ["inputs", "audit"])
        assert result.exit_code == expected, result.output
        payload = json.loads(result.output)
        assert payload["blocking"] == ([blocker] if blocker in report else [])
        assert payload["preserved"] == ([preserved] if preserved in report else [])

    def refused(_url):
        raise RuntimeError("private database details")

    monkeypatch.setattr(inputs, "audit_inputs", refused)
    result = runner.invoke(app, ["inputs", "audit"])
    assert result.exit_code == 2
    assert "private database details" not in result.output


def test_older_schema_audit_skips_missing_columns_instead_of_sqlite_string_literals():
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    tree = sa.Table(
        "workspace_tree_node",
        metadata,
        sa.Column("id", sa.Text),
        sa.Column("title", sa.Text),
        sa.Column("node_kind", sa.Text),
    )
    metadata.create_all(engine)
    statements = []

    @sa.event.listens_for(engine, "before_cursor_execute")
    def track(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    try:
        with engine.begin() as connection:
            connection.execute(
                tree.insert().values(id="one", title="x" * 81, node_kind="folder")
            )
            findings = audit(connection, allow_missing=True)
        assert len(findings) == 1
        assert findings[0]["rule"] == "ck_workspace_tree_node_title_input_v1"
        assert findings[0]["classification"] == "blocking_scalar"
        assert not any('"nav_summary"' in statement for statement in statements)
    finally:
        engine.dispose()


def test_explicit_slugs_reject_lowercase_expansion_and_generated_slugs_stay_bounded():
    for normalize in (normalize_workspace_slug, _normalize_workspace_slug):
        assert normalize("Normal Workspace") == "normal-workspace"
        assert normalize("a" * 80) == "a" * 80
        for invalid in ("a" * 81, "İ" * 80, "nul\x00slug"):
            with pytest.raises(InputPolicyError):
                normalize(invalid)
    assert len(normalize_workspace_slug("İ" * 80, generated=True)) <= 80


def test_blank_custom_labels_keep_generic_fallback_without_accepting_controls():
    repository = get_mcp_app_catalog_repository()
    for label in ("", "   ", "\u00a0\u3000", None):
        request = ApiTokenCreateRequest(
            name="Access key",
            scopes=["mcp:read"],
            workspace_grants=[{"workspace_id": uuid4()}],
            custom_app_label=label,
        )
        assert request.custom_app_label is None
        association = resolve_catalog_app_association(
            repository,
            catalog_app_id=None,
            custom_app_label=label,
            legacy_mcp_client_type="generic",
        )
        assert association.label_snapshot == "Generic MCP client"
    for invalid in ("\t", "\n", "\x00", "x" * 161):
        with pytest.raises(ValueError):
            ApiTokenCreateRequest(
                name="Access key",
                scopes=["mcp:read"],
                workspace_grants=[{"workspace_id": uuid4()}],
                custom_app_label=invalid,
            )
        with pytest.raises(InputPolicyError):
            resolve_catalog_app_association(
                repository,
                catalog_app_id=None,
                custom_app_label=invalid,
                legacy_mcp_client_type="generic",
            )


def test_empty_goal_repair_fails_before_opening_a_database(tmp_path):
    path = tmp_path / "must-not-exist.sqlite"
    for value in ("", " \n "):
        with pytest.raises(ValueError, match="goal"):
            repair_input(f"sqlite:///{path}", "workspace", uuid4(), "goal", value)
    assert not path.exists()


def test_check_failures_are_not_reported_as_duplicate_identity(session_factory):
    hasher = SimpleNamespace(hash_password=lambda _value: "existing password hash")
    services = (
        SessionService(
            session_factory,
            hasher,
            SessionTokenCodec(secret="test-secret", ttl_seconds=3600),
            signup_enabled=True,
        ),
        UserAdminService(session_factory, hasher),
    )

    def force_user_check(_mapper, _connection, user):
        user.__dict__["display_name"] = "x" * 33

    sa.event.listen(User, "before_insert", force_user_check)
    try:
        for service in services:
            create = (
                service.signup
                if isinstance(service, SessionService)
                else service.add_user
            )
            with pytest.raises(IntegrityError, match="ck_users_display_name_input_v1"):
                create(email=f"{uuid4().hex}@example.test", password="correct-password")
    finally:
        sa.event.remove(User, "before_insert", force_user_check)

    def force_workspace_check(_mapper, _connection, workspace):
        workspace.__dict__["title"] = "x" * 129

    sa.event.listen(Workspace, "before_insert", force_workspace_check)
    try:
        with (
            pytest.raises(IntegrityError, match="ck_workspace_title_input_v1"),
            session_factory.begin() as session,
        ):
            WorkspaceWriteService().create_workspace(
                session=session,
                request=WorkspaceCreateRequest(title="Valid", slug="valid-workspace"),
                actor=SimpleNamespace(user_id=uuid4()),
            )
    finally:
        sa.event.remove(Workspace, "before_insert", force_workspace_check)


def test_postgres_unique_error_detection_requires_the_actual_constraint():
    for state, constraint, expected in (
        ("23505", "users_email_key", True),
        ("23514", "users_email_key", False),
        ("23505", "uq_users_personal_workspace_id", False),
    ):
        for driver_error in (
            SimpleNamespace(args=({"C": state, "n": constraint},)),
            SimpleNamespace(
                args=(),
                sqlstate=state,
                diag=SimpleNamespace(constraint_name=constraint),
            ),
        ):
            assert (
                is_single_column_unique_violation(
                    SimpleNamespace(orig=driver_error), table="users", column="email"
                )
                is expected
            )

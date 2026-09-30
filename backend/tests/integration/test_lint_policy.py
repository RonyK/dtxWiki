import pytest
from sqlalchemy import func, select

from backend.models.context import ContextPage
from backend.models.lint import LintFinding
from backend.models.workspace import Workspace
from backend.schemas.lint import LintRunCreateRequest
from backend.services.lint import base
from backend.services.lint.base import LintFindingDraft
from backend.services.lint_service import LintService
from backend.services.settings_service import (
    SettingsPolicyUpdateRejected,
    SettingsService,
)

pytestmark = pytest.mark.integration


class _Rule:
    rule_id = "test.policy_rule"
    layer = "format"
    default_enabled = True
    applies_to = frozenset({"context_page"})

    def check(self, *, session, workspace_id, target):
        return [
            LintFindingDraft(
                rule_id=self.rule_id,
                severity="warning",
                target_type="context_page",
                target_id=target.id,
            )
        ]


class _ExternalRule(_Rule):
    rule_id = "test.external_rule"
    layer = "maintenance"
    runtime_checkable = False


def _ws(session):
    return session.scalar(select(Workspace).where(Workspace.slug == "default"))


def test_get_policies_includes_lint_card_default_empty(session_factory):
    svc = SettingsService()
    with session_factory.begin() as session:
        policies = svc.get_policies(
            control_session=session,
            workspace_session=session,
            workspace_id=_ws(session).id,
        )
    assert policies.lint.name == "lint"
    # Rule toggles are empty by default; the auto-fix mode flag defaults to off.
    assert policies.lint.policy == {"auto_fix": False}


def test_auto_fix_toggle_via_lint_settings(session_factory):
    # F2b: the reserved "auto_fix" key drives workspace.auto_fix_enabled and is
    # surfaced on the lint card; non-bool is rejected.
    svc = SettingsService()
    with session_factory.begin() as session:
        ws = _ws(session)
        assert ws.auto_fix_enabled is False
        svc._update_lint_policy(ws, "auto_fix", True)
        assert ws.auto_fix_enabled is True
        policies = svc.get_policies(
            control_session=session,
            workspace_session=session,
            workspace_id=ws.id,
        )
        assert policies.lint.policy["auto_fix"] is True
        with pytest.raises(SettingsPolicyUpdateRejected, match="boolean"):
            svc._update_lint_policy(ws, "auto_fix", "yes")


def test_update_lint_policy_validates_rule_id_and_bool(session_factory):
    base.register_rule(_Rule())
    try:
        svc = SettingsService()
        with session_factory.begin() as session:
            ws = _ws(session)
            svc._update_lint_policy(ws, "test.policy_rule", False)
            assert ws.lint_policy == {"test.policy_rule": False}
            with pytest.raises(
                SettingsPolicyUpdateRejected, match="unknown lint rule_id"
            ):
                svc._update_lint_policy(ws, "nope.rule", True)
            with pytest.raises(SettingsPolicyUpdateRejected, match="boolean"):
                svc._update_lint_policy(ws, "test.policy_rule", "yes")
    finally:
        base._REGISTRY.pop("test.policy_rule", None)


def test_update_lint_policy_rejects_external_rules(session_factory):
    base.register_rule(_ExternalRule())
    try:
        svc = SettingsService()
        with session_factory.begin() as session:
            ws = _ws(session)
            with pytest.raises(
                SettingsPolicyUpdateRejected, match="supplied externally"
            ):
                svc._update_lint_policy(ws, "test.external_rule", False)
            assert "test.external_rule" not in (ws.lint_policy or {})
    finally:
        base._REGISTRY.pop("test.external_rule", None)


def test_create_run_honors_lint_policy_toggle(session_factory):
    base.register_rule(_Rule())
    try:
        lint = LintService()
        with session_factory.begin() as session:
            ws = _ws(session)
            session.add(
                ContextPage(
                    workspace_id=ws.id, slug="lpp", title="LPP", page_type="concept"
                )
            )
            session.flush()
            req = LintRunCreateRequest(lint_scope="workspace", trigger_type="manual")

            # default_enabled -> the dummy rule fires.
            run_on = lint.create_run(session, ws.id, req, "human", "t")
            on_rows = session.scalars(
                select(LintFinding).where(
                    LintFinding.lint_run_id == run_on.id,
                    LintFinding.rule_id == "test.policy_rule",
                )
            ).all()
            assert len(on_rows) == 1

            # Disabled via policy -> the dummy rule does not fire (other rules,
            # if any, are unaffected; we scope the assertion to this rule+run).
            ws.lint_policy = {"test.policy_rule": False}
            session.flush()
            run_off = lint.create_run(session, ws.id, req, "human", "t")
            off_rows = session.scalars(
                select(LintFinding).where(
                    LintFinding.lint_run_id == run_off.id,
                    LintFinding.rule_id == "test.policy_rule",
                )
            ).all()
            assert len(off_rows) == 0
    finally:
        base._REGISTRY.pop("test.policy_rule", None)


def test_list_findings_hides_disabled_rule_findings(session_factory):
    # M11/DEC-4: disabling a rule hides its existing findings from the list
    # (rows preserved), without marking them fixed.
    base.register_rule(_Rule())
    try:
        lint = LintService()
        req = LintRunCreateRequest(lint_scope="workspace", trigger_type="manual")
        with session_factory.begin() as session:
            ws = _ws(session)
            session.add(
                ContextPage(
                    workspace_id=ws.id, slug="lf", title="LF", page_type="concept"
                )
            )
            session.flush()
            lint.create_run(session, ws.id, req, "human", "t")
            listed = lint.list_findings(session, ws.id, rule="test.policy_rule")
            assert listed.total == 1
            # disable the rule -> its findings are hidden (but the row remains)
            ws.lint_policy = {"test.policy_rule": False}
            session.flush()
            hidden = lint.list_findings(session, ws.id, rule="test.policy_rule")
            assert hidden.total == 0
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(LintFinding)
                    .where(LintFinding.rule_id == "test.policy_rule")
                )
                == 1
            )
    finally:
        base._REGISTRY.pop("test.policy_rule", None)

from types import SimpleNamespace

import pytest

from backend.services.settings_service import (
    SettingsPolicyUpdateRejected,
    SettingsService,
)


def test_required_publication_check_cannot_be_disabled():
    workspace = SimpleNamespace(lint_policy={})
    with pytest.raises(SettingsPolicyUpdateRejected, match="required publication"):
        SettingsService()._update_lint_policy(workspace, "md.code_fence_balance", False)
    assert workspace.lint_policy == {}

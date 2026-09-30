from backend.policy.rbac import role_allows


def test_role_allows_unknown_actual_denies():
    assert role_allows("none", "editor") is False
    assert role_allows("mcp_client", "editor") is False
    assert role_allows("typo", "viewer") is False


def test_role_allows_keeps_required_role_strict():
    try:
        role_allows("owner", "typo")
    except ValueError as exc:
        assert "role must be one of" in str(exc)
    else:
        raise AssertionError("unknown required role should stay strict")

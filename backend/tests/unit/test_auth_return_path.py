from backend.services.auth.return_path import safe_return_path


def test_return_path_accepts_local_oauth_query_and_rejects_browser_escape_paths():
    for path in (
        "/dashboard",
        "/account?linked=company",
        "/oauth/authorize?scope=mcp%3Aread%20mcp%3Awrite",
    ):
        assert safe_return_path(path) == path
    for path in (
        None,
        "https://attacker.test",
        "https://attacker.test/dashboard",
        "//attacker.test",
        "//attacker.test/dashboard",
        "/\\attacker.test",
        "/%5cattacker.test",
        "/%2fattacker.test",
        "/api/v1/auth/logout",
        "/%61pi/v1/auth/me",
        "/a/../api/v1/auth/me",
        "/foo\r\nLocation:x",
        "/" + "x" * 2048,
    ):
        assert safe_return_path(path) == "/dashboard"

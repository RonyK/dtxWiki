from backend.app.settings import Settings


def test_settings_exposes_default_endpoint_ports():
    settings = Settings()

    assert settings.backend_port == 21080
    assert settings.frontend_dev_port == 21173


def test_settings_ports_are_env_backed(monkeypatch):
    monkeypatch.setenv("DTXWIKI_BACKEND_PORT", "22080")
    monkeypatch.setenv("DTXWIKI_FRONTEND_DEV_PORT", "22173")

    settings = Settings()

    assert settings.backend_port == 22080
    assert settings.frontend_dev_port == 22173


def test_settings_exposes_default_off_provider_env_fields(monkeypatch):
    settings = Settings()

    assert settings.llm_provider == "none"
    assert settings.embedding_provider == "none"
    assert settings.anthropic_api_key is None
    assert settings.openai_api_key is None

    monkeypatch.setenv("DTXWIKI_LLM_PROVIDER", "openai")
    monkeypatch.setenv("DTXWIKI_EMBEDDING_PROVIDER", "none")
    monkeypatch.setenv("DTXWIKI_OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("DTXWIKI_ANTHROPIC_API_KEY", "anthropic-key")

    settings = Settings()

    assert settings.llm_provider == "openai"
    assert settings.embedding_provider == "none"
    assert settings.openai_api_key == "openai-key"
    assert settings.anthropic_api_key == "anthropic-key"


def test_public_base_url_env_override(monkeypatch):
    """NEW-6: admins can pin the server-side base URL with
    ``DTXWIKI_PUBLIC_BASE_URL``. The Settings env_prefix already exposes the
    field; this test pins the contract so downstream consumers (OAuth
    metadata, cookie-secure derivation, MCP onboarding snippet) can rely on
    it.
    """

    settings = Settings()
    assert settings.public_base_url is None

    monkeypatch.setenv("DTXWIKI_PUBLIC_BASE_URL", "https://wiki.example.lan")
    settings = Settings()
    assert settings.public_base_url == "https://wiki.example.lan"
    # When public_base_url is https, session cookies must be marked Secure
    # even outside production for LAN-TLS deployments.
    assert settings.effective_session_cookie_secure is True

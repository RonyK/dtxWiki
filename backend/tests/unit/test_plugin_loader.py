"""The opt-in plugin boundary rejects invalid deployments before hooks run."""

from dataclasses import replace
from traceback import format_exception
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, Field, SecretStr

from backend.app import plugins
from backend.app.settings import Settings
from backend.spi.plugins import DtxwikiPlugin
from backend.tests.utils.environment import clear_ambient_dtxwiki_env


def test_plugin_allowlist_compatibility_and_settings_are_fail_closed(
    monkeypatch, tmp_path
):
    clear_ambient_dtxwiki_env(monkeypatch)
    loaded = []
    configured = []

    class PluginSettings(BaseModel):
        client_secret: SecretStr
        timeout: int = Field(default=30, gt=0)

    plugin = DtxwikiPlugin(
        name="sso",
        version="1.0.0",
        requires_core=">=0.1,<1",
        requires_spi=">=1.1,<2",
        settings_model=PluginSettings,
        configure=configured.append,
    )

    def entry(name, value):
        def load():
            loaded.append(name)
            return value

        return SimpleNamespace(name=name, load=load)

    entries = [entry("sso", plugin), entry("disabled", object())]
    monkeypatch.setattr(plugins.metadata, "entry_points", lambda **_: entries)

    def settings(names="sso"):
        return Settings(plugins=names, app_version="0.1.3", _env_file=None)

    assert plugins.load_plugins(settings("")).plugins == ()
    assert loaded == []
    monkeypatch.setenv("DTXWIKI_PLUGIN_SSO_CLIENT_SECRET", "secret-value")
    monkeypatch.setenv("DTXWIKI_PLUGIN_SSO_TIMEOUT", "5")
    current = settings()
    registry = plugins.load_plugins(current)
    assert registry.capabilities() == ("sso",)
    assert configured[0].timeout == 5
    assert configured[0].client_secret.get_secret_value() == "secret-value"
    assert plugins.load_plugins(current) is registry
    assert loaded == ["sso"]
    assert len(configured) == 1

    # The production unknown-env guard must admit exactly the fields the
    # enabled plugin validated, while all ordinary production guards still run.
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = tmp_path / "production.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    production = current.model_copy(
        update={
            "app_env": "production",
            "public_base_url": "https://wiki.example.test",
            "session_secret": "s" * 64,
            "oauth_jwt_private_key_path": str(key_path),
            "auto_bootstrap": False,
            "metrics_enabled": False,
        }
    )
    production.validate_for_startup()

    for names, message in (
        ("sso,sso", "duplicate enabled"),
        ("missing", "not installed"),
        ("sso,", "lowercase plugin names"),
        ("p" * 48, "lowercase plugin names"),
    ):
        with pytest.raises(plugins.PluginConfigurationError, match=message):
            plugins.load_plugins(settings(names))
    entries.append(entry("sso", plugin))
    with pytest.raises(plugins.PluginConfigurationError, match="duplicate plugin"):
        plugins.load_plugins(settings())
    entries.pop()
    for change in (
        {"name": "other"},
        {"requires_core": ">=99"},
        {"requires_spi": "<1.1"},
        {"requires_core": "not-a-specifier"},
    ):
        entries[0] = entry("sso", replace(plugin, **change))
        with pytest.raises(plugins.PluginConfigurationError):
            plugins.load_plugins(settings())
    entries[0] = entry("sso", plugin)

    class OtherSettings(BaseModel):
        secret: SecretStr

    other = replace(plugin, name="sso_client", settings_model=OtherSettings)
    entries.append(entry("sso_client", other))
    configured_before_collision = len(configured)
    with pytest.raises(plugins.PluginConfigurationError, match="key collides"):
        plugins.load_plugins(settings("sso,sso_client"))
    assert len(configured) == configured_before_collision
    entries.pop()

    # Even a previously cached registry must reject later env typos. Values
    # from rejected settings must never appear in startup diagnostics.
    monkeypatch.setenv("DTXWIKI_PLUGIN_SSO_TIMOUT", "999")
    with pytest.raises(plugins.PluginConfigurationError, match="SSO_TIMOUT"):
        plugins.load_plugins(current)
    monkeypatch.delenv("DTXWIKI_PLUGIN_SSO_TIMOUT")
    monkeypatch.setenv("DTXWIKI_PLUGIN_DISABLED_TOKEN", "private-value")
    with pytest.raises(plugins.PluginConfigurationError, match="DISABLED_TOKEN"):
        plugins.load_plugins(settings())
    monkeypatch.delenv("DTXWIKI_PLUGIN_DISABLED_TOKEN")
    monkeypatch.setenv("DTXWIKI_PLUGIN_SSO_TIMEOUT", "private-invalid-value")
    with pytest.raises(plugins.PluginConfigurationError) as invalid:
        plugins.load_plugins(settings())
    assert "private-invalid-value" not in "".join(format_exception(invalid.value))
    monkeypatch.delenv("DTXWIKI_PLUGIN_SSO_TIMEOUT")
    monkeypatch.delenv("DTXWIKI_PLUGIN_SSO_CLIENT_SECRET")
    with pytest.raises(plugins.PluginConfigurationError, match="invalid settings"):
        plugins.load_plugins(settings())
    assert "disabled" not in loaded

    # The update entry point must source its deployment env before plugin
    # discovery; otherwise plugin-specific env keys look like unknown keys
    # under the default Settings and configure never sees the deployment.
    from typer.testing import CliRunner

    from backend.cli import main as cli_main
    from backend.cli.commands import update

    for key in (
        "DTXWIKI_PLUGINS",
        "DTXWIKI_PLUGIN_SSO_CLIENT_SECRET",
        "DTXWIKI_PLUGIN_SSO_TIMEOUT",
    ):
        monkeypatch.setenv(key, "temporary")
        monkeypatch.delenv(key)
    env_file = tmp_path / "plugins.env"
    env_file.write_text(
        "DTXWIKI_PLUGINS=sso\n"
        "DTXWIKI_PLUGIN_SSO_CLIENT_SECRET=deployment-secret\n"
        "DTXWIKI_PLUGIN_SSO_TIMEOUT=9\n"
    )
    configured.clear()
    monkeypatch.setattr(
        update.update_service, "run_check", lambda *args, **kwargs: None
    )
    result = CliRunner().invoke(
        cli_main.app,
        ["update", "--check", "--method", "venv", "--env-file", str(env_file)],
    )
    assert result.exit_code == 0, result.output
    assert len(configured) == 1
    assert configured[0].timeout == 9
    assert configured[0].client_secret.get_secret_value() == "deployment-secret"

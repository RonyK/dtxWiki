import io
from dataclasses import replace

import pytest
from PIL import Image

from backend.app.settings import Settings
from backend.services.auth.provider_registry import ProviderRegistry
from backend.spi.auth import Branding
from backend.testing.fake_auth_method import FakeAuthMethod


def test_provider_registry_rejects_unsafe_contracts_and_serves_sanitized_branding(
    tmp_path, monkeypatch
):
    settings = Settings(public_base_url="https://wiki.example.test")
    method = FakeAuthMethod()
    for method_id in ("password", "core.password", "core.oidc", "invalid/method"):
        method.descriptor = replace(FakeAuthMethod.descriptor, method_id=method_id)
        with pytest.raises(ValueError):
            ProviderRegistry(settings, [method])
    method.descriptor = FakeAuthMethod.descriptor
    with pytest.raises(ValueError):
        ProviderRegistry(settings, [method, method])
    method.descriptor = replace(method.descriptor, interaction="credential")
    with pytest.raises(ValueError, match="credential"):
        ProviderRegistry(settings, [method]).add(
            {"slug": "test", "method": "test_oidc"}
        )
    method.descriptor = FakeAuthMethod.descriptor
    for public_url in (None, "http://wiki.example.test"):
        with pytest.raises(ValueError, match="HTTPS"):
            ProviderRegistry(Settings(public_base_url=public_url), [method]).add(
                {"slug": "test", "method": "test_oidc"}
            )
    # Icons are decoded and emitted anew; arbitrary input bytes cannot reach
    # an image endpoint, and unreadable color combinations use the core theme.
    png = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(png, format="PNG")
    branding = Branding(
        icon=png.getvalue(),
        label={"ko": "예제 로그인", "en": "Example login"},
        background="#FFFFFF",
        foreground="#000000",
    )
    method.descriptor = replace(method.descriptor, branding=branding)
    registry = ProviderRegistry(settings, [method])
    registry.add({"slug": "test", "method": "test_oidc"})
    registry.add({"slug": "test-", "method": "test_oidc"})
    assert registry.get("test-").context.callback_url.endswith("/test-/callback")
    exposed = registry.login_methods()[0]
    assert exposed["label"]["ko"] == "예제 로그인"
    assert exposed["background"] == "#FFFFFF" and exposed["foreground"] == "#000000"
    with Image.open(io.BytesIO(registry.icons["test_oidc"])) as icon:
        assert icon.mode == "RGBA" and icon.size == (16, 16)
    for invalid in (b"not a PNG", b"x" * 16385):
        method.descriptor = replace(
            method.descriptor, branding=replace(branding, icon=invalid)
        )
        with pytest.raises((ValueError, OSError)):
            ProviderRegistry(settings, [method])
    for foreground in ("#FFFFFF", "red;display:none"):
        method.descriptor = replace(
            method.descriptor, branding=replace(branding, foreground=foreground)
        )
        rejected_color = ProviderRegistry(settings, [method])
        rejected_color.add({"slug": "test", "method": "test_oidc"})
        assert rejected_color.login_methods()[0]["background"] is None
    secret = tmp_path / "client-secret"
    secret.write_text("test-private-secret\n")
    method.descriptor = FakeAuthMethod.descriptor
    registry = ProviderRegistry(settings, [method])
    registry.add(
        {
            "slug": "file-secret",
            "method": "test_oidc",
            "client_secret_ref": f"file:{secret}",
        }
    )
    assert "test-private-secret" not in str(registry.login_methods())
    monkeypatch.delenv("DTXWIKI_TEST_MISSING_LOGIN_SECRET", raising=False)
    for secret_ref in ("inline-secret", "env:DTXWIKI_TEST_MISSING_LOGIN_SECRET"):
        with pytest.raises(ValueError):
            registry.add(
                {
                    "slug": "missing-secret",
                    "method": "test_oidc",
                    "client_secret_ref": secret_ref,
                }
            )

# Budget exception: protocol capabilities, directory admission, workspace-role
# allowlists and branding are distinct configuration failures. Count loop inputs;
# keep them in one unit case rather than repeating HTTP login traversals.
import io
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from PIL import Image

from backend.app.settings import Settings
from backend.services.auth.provider_registry import ProviderRegistry
from backend.spi.auth import BeginRequest, Branding, CallbackInput
from backend.testing.fake_auth_method import FAKE_ISSUER, FakeAuthMethod, FakeSamlMethod
from backend.testing.spi_conformance.auth import assert_auth_method_contract


def test_provider_registry_rejects_unsafe_contracts_and_serves_sanitized_branding(
    tmp_path, monkeypatch
):
    from backend.app.main import create_app

    # Serving must refuse a lockout before creating storage. This guard does
    # not belong to Settings/bootstrap: CLI recovery must remain available.
    disabled = Settings(
        auth_password_login_enabled=False,
        database_url=f"sqlite:///{tmp_path / 'must-not-exist.sqlite'}",
        session_secret="s" * 64,
        _env_file=None,
    )
    with pytest.raises(ValueError, match="no active external login provider"):
        create_app(settings=disabled)
    assert not (tmp_path / "must-not-exist.sqlite").exists()
    settings = Settings(public_base_url="https://wiki.example.test")
    method = FakeAuthMethod()
    for method_id in ("password", "core.password", "invalid/method"):
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
    assert exposed["display_name"] == ""
    assert exposed["label"]["ko"] == "예제 로그인"
    assert exposed["background"] == "#FFFFFF" and exposed["foreground"] == "#000000"
    configured = ProviderRegistry(settings, [method])
    workspace_id = str(uuid4())
    configured.add(
        {
            "slug": "company",
            "method": "test_oidc",
            "display_name": " Company ",
            "directory_enabled": True,
            "directory_tenant": "company.example",
            "provisioning_workspace_roles": {workspace_id: ["viewer", "editor"]},
        }
    )
    assert configured.login_methods()[0]["display_name"] == "Company"
    assert configured.get("company").policy.directory_tenant == "company.example"
    for extra in (
        {"link_policy": "auto_verified_email"},
        {"directory_enabled": True},
        {"directory_tenant": " "},
        {"provisioning_workspace_roles": {workspace_id: ["owner"]}},
        {"provisioning_workspace_roles": {"not-a-uuid": ["viewer"]}},
    ):
        with pytest.raises(ValueError):
            ProviderRegistry(settings, [method]).add(
                {
                    "slug": "invalid-policy",
                    "method": "test_oidc",
                    **extra,
                }
            )
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
    # SAML uses a signed assertion and InResponseTo, with neither OIDC nonce
    # nor PKCE/client-secret requirements. Fresh AuthnInstant supports reauth.
    saml = FakeSamlMethod()
    registry = ProviderRegistry(settings, [saml])
    registry.add({"slug": "saml", "method": "test_saml"})
    provider = registry.get("saml")
    assert registry.login_methods()[0]["supports_reauthentication"] is True
    for field, value in (
        ("nonce", "supported"),
        ("pkce", "required"),
        ("id_token", "signed"),
    ):
        invalid = FakeSamlMethod()
        invalid.descriptor = replace(
            invalid.descriptor,
            capabilities=replace(invalid.descriptor.capabilities, **{field: value}),
        )
        with pytest.raises(ValueError, match="capabilities"):
            ProviderRegistry(settings, [invalid])
    invalid.descriptor = replace(saml.descriptor, interaction="credential")
    with pytest.raises(ValueError, match="capabilities"):
        ProviderRegistry(settings, [invalid])

    def saml_callback(changes=None):
        def callback(instruction):
            state = parse_qs(urlsplit(instruction.url).query)["RelayState"][0]
            return CallbackInput(
                http_method="POST",
                query={},
                form={
                    "RelayState": state,
                    "InResponseTo": instruction.protocol_request_id,
                    "SAMLResponse": "valid",
                    **(changes or {}),
                },
                protocol_request_id=instruction.protocol_request_id,
            )

        return callback

    assert_auth_method_contract(
        saml,
        provider.context,
        begin=BeginRequest(
            purpose="reauth", state="core-state", force_reauthentication=True
        ),
        successful_callback=saml_callback(),
        rejected_callbacks={
            "signature": saml_callback({"SAMLResponse": "bad-signature"}),
            "audience": saml_callback({"SAMLResponse": "bad-audience"}),
            "correlation": saml_callback({"InResponseTo": "other-request"}),
        },
        expected_issuer=FAKE_ISSUER,
        expected_subject="fake-saml-subject",
    )

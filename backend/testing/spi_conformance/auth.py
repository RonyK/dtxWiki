"""Reusable adapter contract check; protocol-negative fixtures belong to the IdP."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from urllib.parse import parse_qs, urlsplit

from jsonschema import Draft202012Validator

from backend.spi.auth import (
    AUTH_METHOD_SPI_VERSION,
    AuthMethodDescriptor,
    AuthMethodError,
    BeginRequest,
    CallbackInput,
    ExternalAssertion,
    LoginContext,
    ProviderContext,
    ProvisioningResult,
    RedirectInstruction,
    WorkspaceRoleRequest,
)

CallbackFactory = Callable[[RedirectInstruction], CallbackInput]


def assert_auth_method_contract(
    method,
    provider: ProviderContext,
    *,
    begin: BeginRequest,
    successful_callback: CallbackFactory,
    rejected_callbacks: Mapping[str, CallbackFactory],
    expected_issuer: str,
    expected_subject: str,
    expected_tenant_id: str | None = None,
    login: LoginContext | None = None,
) -> None:
    """Run successful and rejected flows against caller-supplied IdP fixtures.

    Signed OIDC plugins must supply independent ``signature`` and ``audience``
    cases. Factories should arrange the fake IdP's next response and return the
    resulting callback. This keeps the check usable with real private adapters
    rather than asserting implementation details or requiring network access.
    """
    descriptor = method.descriptor
    assert isinstance(descriptor, AuthMethodDescriptor), "invalid descriptor"
    assert descriptor.spi_version == AUTH_METHOD_SPI_VERSION, "SPI version mismatch"
    assert descriptor.method_id and descriptor.interaction == "redirect"
    assert descriptor.capabilities.pkce in {"required", "supported", "unsupported"}
    assert descriptor.capabilities.nonce in {"supported", "unsupported"}
    assert descriptor.capabilities.id_token in {"signed", "none"}
    assert descriptor.branding.label and all(
        isinstance(locale, str) and isinstance(label, str) and label.strip()
        for locale, label in descriptor.branding.label.items()
    ), "invalid branding"
    Draft202012Validator.check_schema(descriptor.config_schema)
    Draft202012Validator(descriptor.config_schema).validate(dict(provider.config))
    assert isinstance(method.validate_config(provider.config), Mapping)
    if descriptor.protocol == "oidc" and descriptor.capabilities.id_token == "signed":
        assert {"signature", "audience"} <= set(rejected_callbacks), (
            "signed OIDC conformance requires bad-signature and bad-audience fixtures"
        )
    if descriptor.protocol == "saml2":
        assert descriptor.capabilities.pkce == "unsupported"
        assert descriptor.capabilities.nonce == "unsupported"
        assert descriptor.capabilities.id_token == "none"
        assert begin.nonce is None and begin.code_challenge is None
        assert {"signature", "audience", "correlation"} <= set(rejected_callbacks), (
            "SAML conformance requires signature, audience and InResponseTo fixtures"
        )

    def start() -> RedirectInstruction:
        redirect = method.begin(provider, begin)
        assert isinstance(redirect, RedirectInstruction)
        parsed = urlsplit(redirect.url)
        assert parsed.scheme == "https" and parsed.hostname, (
            "HTTPS IdP redirect required"
        )
        assert redirect.http_method in {"GET", "POST"}
        if descriptor.protocol == "saml2":
            assert redirect.http_method == "GET", "SAML requires a Redirect request"
            request_id = redirect.protocol_request_id
            assert (
                isinstance(request_id, str)
                and 0 < len(request_id) <= 512
                and all(0x20 <= ord(character) <= 0x7E for character in request_id)
            ), "SAML requires a bounded printable request ID"
        fields = (
            parse_qs(parsed.query)
            if redirect.http_method == "GET"
            else {key: [value] for key, value in redirect.form_fields.items()}
        )
        if descriptor.protocol in {"oidc", "oauth2"}:
            assert fields.get("state") == [begin.state], "adapter changed core state"
        if (
            descriptor.protocol == "oidc"
            and descriptor.capabilities.nonce == "supported"
        ):
            assert fields.get("nonce") == [begin.nonce], "adapter changed core nonce"
        if (
            descriptor.protocol in {"oidc", "oauth2"}
            and descriptor.capabilities.pkce == "required"
        ):
            assert fields.get("code_challenge") == [begin.code_challenge]
            assert fields.get("code_challenge_method") == ["S256"]
        return redirect

    assertion = method.complete(provider, successful_callback(start()))
    assert isinstance(assertion, ExternalAssertion)
    assert (assertion.issuer, assertion.subject) == (expected_issuer, expected_subject)
    if expected_tenant_id is not None:
        assert assertion.tenant_id == expected_tenant_id, "unexpected verified tenant"
    if descriptor.capabilities.nonce == "supported":
        assert assertion.nonce_verified, "adapter did not verify the sent nonce"
    for name, factory in rejected_callbacks.items():
        callback = factory(start())
        try:
            method.complete(provider, callback)
        except AuthMethodError:
            continue
        raise AssertionError(f"adapter accepted invalid IdP response: {name}")
    hook = getattr(method, "on_login", None)
    if hook is not None:
        assert isinstance(login, LoginContext), "provisioning needs a login fixture"
        provisioned = hook(login)
        assert isinstance(provisioned, ProvisioningResult), (
            "invalid provisioning result"
        )
        assert isinstance(provisioned.workspace_roles, tuple)
        assert all(
            isinstance(request, WorkspaceRoleRequest)
            and request.role in {"viewer", "editor"}
            for request in provisioned.workspace_roles
        ), "invalid workspace role request"

"""Public helpers for separately installed OIDC and OAuth2 login adapters.

No provider is registered by importing this package. A private adapter may set
``descriptor = OidcMethodBase.describe(method_id="company_sso", ...)`` and reuse
the base implementation, or use ``OidcClient`` directly for protocol variation.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from backend.spi.auth import (
    AuthMethodDescriptor,
    AuthMethodError,
    BeginRequest,
    Branding,
    CallbackInput,
    ExternalAssertion,
    MethodCapabilities,
    ProviderContext,
    RedirectInstruction,
)
from backend.spi.auth_oidc.client import (
    DEFAULT_SIGNING_ALGORITHMS,
    SAFE_AUTHORIZATION_PARAMETERS,
    SAFE_SIGNING_ALGORITHMS,
    OidcClient,
)
from backend.spi.auth_oidc.transport import JsonTransport, https_url

__all__ = ["JsonTransport", "OidcClient", "OidcMethodBase"]


class OidcMethodBase:
    """Authorization Code + S256 PKCE adapter; core owns transaction/session state."""

    @staticmethod
    def describe(
        *,
        method_id: str = "oidc",
        branding: Branding | None = None,
        accepted_issuers: tuple[str, ...] = (),
        default_scopes: tuple[str, ...] = ("openid", "email", "profile"),
        capabilities: MethodCapabilities | None = None,
    ) -> AuthMethodDescriptor:
        return AuthMethodDescriptor(
            method_id=method_id,
            branding=branding or Branding(),
            capabilities=capabilities or MethodCapabilities(reauthentication=True),
            config_schema={
                "type": "object",
                "required": ["issuer", "client_id"],
                "additionalProperties": False,
                "properties": {
                    "issuer": {"type": "string", "format": "uri"},
                    "client_id": {"type": "string", "minLength": 1},
                    "client_secret_ref": {"type": "string"},
                    "scopes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": list(default_scopes),
                    },
                    "accepted_issuers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": list(accepted_issuers),
                    },
                    "trusted_audiences": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                    },
                    "signing_algorithms": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": list(DEFAULT_SIGNING_ALGORITHMS),
                    },
                    "token_endpoint_auth_method": {
                        "type": "string",
                        "enum": ["client_secret_basic", "client_secret_post", "none"],
                        "default": "client_secret_basic",
                    },
                    "authorization_params": {"type": "object"},
                    "allow_private_network": {"type": "boolean", "default": False},
                },
            },
        )

    def __init__(self, *, client: OidcClient | None = None) -> None:
        self.client = client or OidcClient()
        if not isinstance(getattr(self, "descriptor", None), AuthMethodDescriptor):
            self.descriptor = self.describe()

    def validate_config(self, raw: Mapping) -> Mapping:
        properties = self.descriptor.config_schema["properties"]
        if not isinstance(raw, Mapping) or set(raw) - set(properties):
            raise AuthMethodError("invalid_oidc_configuration")
        issuer = https_url(raw.get("issuer"), issuer=True)
        client_id = raw.get("client_id")
        if not isinstance(client_id, str) or not 1 <= len(client_id) <= 1024:
            raise AuthMethodError("invalid_oidc_client_id")
        method = raw.get("token_endpoint_auth_method", "client_secret_basic")
        if method not in {"client_secret_basic", "client_secret_post", "none"}:
            raise AuthMethodError("unsupported_oidc_client_authentication")
        if method == "none" and self.descriptor.capabilities.pkce == "unsupported":
            raise AuthMethodError("oidc_without_pkce_requires_confidential_client")
        secret_ref = raw.get("client_secret_ref")
        if method != "none" and (not isinstance(secret_ref, str) or not secret_ref):
            raise AuthMethodError("missing_oidc_client_secret_reference")
        if secret_ref is not None and (
            not isinstance(secret_ref, str) or len(secret_ref) > 1024
        ):
            raise AuthMethodError("invalid_oidc_client_secret_reference")
        scopes = raw.get("scopes", properties["scopes"]["default"])
        if (
            not isinstance(scopes, (list, tuple))
            or not 1 <= len(scopes) <= 32
            or any(
                not isinstance(scope, str)
                or not scope
                or len(scope) > 128
                or any(
                    ord(char) < 33 or ord(char) > 126 or char in {'"', "\\"}
                    for char in scope
                )
                for scope in scopes
            )
            or "openid" not in scopes
        ):
            raise AuthMethodError("invalid_oidc_scopes")
        algorithms = raw.get("signing_algorithms", DEFAULT_SIGNING_ALGORITHMS)
        if (
            not isinstance(algorithms, (list, tuple))
            or not algorithms
            or any(not isinstance(alg, str) for alg in algorithms)
            or set(algorithms) - SAFE_SIGNING_ALGORITHMS
        ):
            raise AuthMethodError("unsafe_oidc_signing_algorithm")
        declared_aliases = tuple(properties["accepted_issuers"]["default"])
        aliases = raw.get("accepted_issuers", declared_aliases)
        if (
            not isinstance(aliases, (list, tuple))
            or len(aliases) > 16
            or any(not isinstance(alias, str) or not alias for alias in aliases)
        ):
            raise AuthMethodError("invalid_oidc_accepted_issuers")
        for alias in aliases:
            # Non-URL issuer aliases require a deliberate plugin declaration.
            if alias not in declared_aliases:
                https_url(alias, issuer=True)
        trusted_audiences = raw.get("trusted_audiences", ())
        if (
            not isinstance(trusted_audiences, (list, tuple))
            or len(trusted_audiences) > 32
            or any(
                not isinstance(aud, str) or not aud or len(aud) > 1024
                for aud in trusted_audiences
            )
        ):
            raise AuthMethodError("invalid_oidc_trusted_audiences")
        params = raw.get("authorization_params", {})
        if (
            not isinstance(params, Mapping)
            or set(params) - SAFE_AUTHORIZATION_PARAMETERS
        ):
            raise AuthMethodError("unsafe_oidc_authorization_parameter")
        params = dict(params)
        for key, value in params.items():
            if (
                not isinstance(value, str)
                or len(value) > 1024
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
            ):
                raise AuthMethodError("invalid_oidc_authorization_parameter")
            if key == "max_age" and (
                not value.isascii() or not value.isdecimal() or int(value) > 2**31 - 1
            ):
                raise AuthMethodError("invalid_oidc_max_age")
        private_network = raw.get("allow_private_network", False)
        if not isinstance(private_network, bool):
            raise AuthMethodError("invalid_oidc_network_configuration")
        return {
            "issuer": issuer,
            "client_id": client_id,
            "client_secret_ref": secret_ref,
            "scopes": tuple(dict.fromkeys(scopes)),
            "accepted_issuers": tuple(dict.fromkeys((issuer, *aliases))),
            "trusted_audiences": tuple(trusted_audiences),
            "signing_algorithms": tuple(algorithms),
            "token_endpoint_auth_method": method,
            "authorization_params": params,
            "allow_private_network": private_network,
        }

    def begin(
        self, provider: ProviderContext, request: BeginRequest
    ) -> RedirectInstruction:
        config = self.validate_config(provider.config)
        caps = self.descriptor.capabilities
        if (caps.nonce == "supported" and not request.nonce) or (
            caps.pkce == "required" and not request.code_challenge
        ):
            raise AuthMethodError("missing_oidc_transaction_binding")
        metadata = self.client.discovery(
            config["issuer"], allow_private_network=config["allow_private_network"]
        )
        params = dict(config["authorization_params"])
        if request.force_reauthentication:
            params.update({"prompt": "login", "max_age": "0"})
        return RedirectInstruction(
            url=self.client.authorization_url(
                metadata,
                client_id=config["client_id"],
                redirect_uri=provider.callback_url,
                state=request.state,
                nonce=request.nonce if caps.nonce == "supported" else None,
                code_challenge=request.code_challenge
                if caps.pkce != "unsupported"
                else None,
                scopes=config["scopes"],
                parameters=params,
            )
        )

    def complete(
        self, provider: ProviderContext, callback: CallbackInput
    ) -> ExternalAssertion:
        config = self.validate_config(provider.config)
        caps = self.descriptor.capabilities
        if (caps.nonce == "supported" and not callback.expected_nonce) or (
            caps.pkce == "required" and not callback.pkce_verifier
        ):
            raise AuthMethodError("missing_oidc_transaction_binding")
        if callback.http_method not in {"GET", "POST"}:
            raise AuthMethodError("unsupported_oidc_callback_method")
        payload = callback.query if callback.http_method == "GET" else callback.form
        if payload.get("error") or not isinstance(payload.get("code"), str):
            raise AuthMethodError("invalid_oidc_callback")
        if "iss" in payload and payload["iss"] != config["issuer"]:
            raise AuthMethodError("oidc_callback_issuer_mismatch")
        metadata = self.client.discovery(
            config["issuer"], allow_private_network=config["allow_private_network"]
        )
        secret = (
            provider.secrets(config["client_secret_ref"])
            if config["token_endpoint_auth_method"] != "none"
            else None
        )
        tokens = self.client.exchange_code(
            metadata,
            code=payload["code"],
            client_id=config["client_id"],
            redirect_uri=provider.callback_url,
            code_verifier=callback.pkce_verifier
            if caps.pkce != "unsupported"
            else None,
            client_secret=secret,
            token_endpoint_auth_method=config["token_endpoint_auth_method"],
            allow_private_network=config["allow_private_network"],
        )
        configured_max_age = config["authorization_params"].get("max_age")
        claims = self.client.validate_id_token(
            tokens["id_token"],
            metadata,
            issuer=config["issuer"],
            accepted_issuers=config["accepted_issuers"],
            trusted_audiences=config["trusted_audiences"],
            client_id=config["client_id"],
            nonce=callback.expected_nonce if caps.nonce == "supported" else None,
            algorithms=config["signing_algorithms"],
            max_age=int(configured_max_age) if configured_max_age is not None else None,
            allow_private_network=config["allow_private_network"],
        )

        def text_claim(name):
            value = claims.get(name)
            return value if isinstance(value, str) and len(value) <= 1024 else None

        def list_claim(name):
            value = claims.get(name, [])
            return (
                tuple(value)
                if isinstance(value, list)
                and len(value) <= 256
                and all(isinstance(item, str) and len(item) <= 255 for item in value)
                else ()
            )

        auth_time = claims.get("auth_time")
        try:
            authenticated_at = (
                datetime.fromtimestamp(auth_time, tz=UTC)
                if auth_time is not None
                else None
            )
        except (ValueError, OverflowError, OSError):
            raise AuthMethodError("invalid_oidc_authentication_time") from None
        return ExternalAssertion(
            issuer=claims["iss"],
            subject=claims["sub"],
            email=text_claim("email"),
            email_verified=claims.get("email_verified") is True,
            display_name=text_claim("name"),
            authenticated_at=authenticated_at,
            amr=list_claim("amr"),
            idp_session_id=text_claim("sid"),
            groups=list_claim("groups"),
            nonce_verified=caps.nonce == "supported",
        )

"""Explicit, immutable provider configuration shared by every login route."""

import io
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from PIL import Image
from pydantic import BaseModel, ConfigDict

from backend.domain.auth_policy import provider_slug_valid
from backend.spi.auth import AuthMethodPlugin, ProviderContext

METHOD_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.-]{0,126}\Z")


def resolve_secret(reference: str) -> str:
    if reference.startswith("env:"):
        value = os.environ.get(reference[4:], "")
    elif reference.startswith("file:"):
        path = Path(reference[5:])
        if not path.is_absolute() or path.stat().st_size > 16384:
            raise ValueError("invalid login secret file")
        value = path.read_text().rstrip("\r\n")
    else:
        raise ValueError("login secrets require an env: or file: reference")
    if not value or len(value.encode()) > 16384:
        raise ValueError("login secret is unavailable")
    return value


class ProviderPolicy(BaseModel):
    slug: str
    method: str
    display_name: str | None = None
    enabled: bool = True
    jit_provisioning: bool = False
    allowed_email_domains: list[str] = []
    link_policy: str = "confirm"
    trust_email_verified: bool = False
    config: dict = {}
    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True)
class AuthProvider:
    policy: ProviderPolicy
    method: AuthMethodPlugin
    context: ProviderContext

    @property
    def slug(self):
        return self.policy.slug


class ProviderRegistry:
    def __init__(self, settings, methods=()):
        self.methods = {}
        self.icons = {}
        self.colors = {}
        self.providers = {}
        self.settings = settings
        for method in methods:
            descriptor = method.descriptor
            if (
                not METHOD_PATTERN.fullmatch(descriptor.method_id)
                or descriptor.method_id == "password"
                or descriptor.method_id.startswith("core.")
                or descriptor.method_id in self.methods
                or descriptor.spi_version != "1.0"
                or descriptor.interaction not in {"redirect", "credential"}
                or descriptor.protocol not in {"oidc", "oauth2", "saml2", "ldap"}
            ):
                raise ValueError("invalid or duplicate auth method descriptor")
            capabilities = descriptor.capabilities
            is_saml = descriptor.protocol == "saml2"
            if (
                capabilities.pkce not in {"required", "supported", "unsupported"}
                or capabilities.nonce not in {"supported", "unsupported"}
                or capabilities.id_token not in {"signed", "none"}
                or (
                    capabilities.id_token == "none"
                    and capabilities.reauthentication
                    and not is_saml
                )
                or (
                    is_saml
                    and (
                        descriptor.interaction != "redirect"
                        or capabilities.pkce != "unsupported"
                        or capabilities.nonce != "unsupported"
                        or capabilities.id_token != "none"
                    )
                )
            ):
                raise ValueError("invalid auth method capabilities")
            Draft202012Validator.check_schema(dict(descriptor.config_schema))
            if not descriptor.branding.label or any(
                not isinstance(label, str) or not 0 < len(label) <= 200
                for label in descriptor.branding.label.values()
            ):
                raise ValueError("invalid auth method label")
            if descriptor.branding.icon is not None:
                self.icons[descriptor.method_id] = sanitize_icon(
                    descriptor.branding.icon
                )
            self.colors[descriptor.method_id] = branding_colors(descriptor.branding)
            self.methods[descriptor.method_id] = method
        if settings.auth_providers_file:
            path = Path(settings.auth_providers_file)
            if path.stat().st_size > 1024 * 1024:
                raise ValueError("auth provider file exceeds 1 MiB")
            document = tomllib.loads(path.read_text())
            if set(document) != {"providers"} or not isinstance(
                document["providers"], list
            ):
                raise ValueError("auth provider file requires [[providers]]")
            for raw in document["providers"]:
                self.add(raw)

    def add(self, raw: Mapping):
        # Flat adapter fields keep the deployment file concise. Core policies
        # are removed before adapter validation; nested config is also accepted.
        raw = dict(raw)
        policy_fields = set(ProviderPolicy.model_fields)
        adapter_config = {
            key: raw.pop(key) for key in tuple(raw) if key not in policy_fields
        }
        if "config" in raw and adapter_config:
            raise ValueError("use flat provider fields or config, not both")
        raw.setdefault("config", adapter_config)
        policy = ProviderPolicy.model_validate(raw)
        if (
            not provider_slug_valid(policy.slug)
            or policy.slug in self.providers
            or policy.link_policy not in {"confirm", "auto_verified_email"}
            or (
                policy.display_name is not None
                and not 0 < len(policy.display_name) <= 200
            )
        ):
            raise ValueError("invalid or duplicate auth provider")
        if not policy.enabled:
            return
        method = self.methods.get(policy.method)
        if method is None:
            raise ValueError("auth provider references an unavailable method")
        if method.descriptor.interaction != "redirect":
            raise ValueError(
                "credential auth providers require a credential login route"
            )
        base = (self.settings.public_base_url or "").rstrip("/")
        parsed = urlsplit(base)
        if parsed.scheme != "https" and not (
            self.settings.app_mode == "solo_local"
            and parsed.scheme == "http"
            and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError(
                "external login requires a canonical HTTPS public_base_url"
            )
        config = dict(method.validate_config(policy.config))
        if not config.get("issuer"):
            raise ValueError("auth provider must declare its expected issuer")
        for key, value in config.items():
            if key.endswith("secret_ref") and value:
                resolve_secret(value)
        capabilities = method.descriptor.capabilities
        if method.descriptor.protocol != "saml2":
            if capabilities.pkce == "unsupported" and not config.get(
                "client_secret_ref"
            ):
                raise ValueError("login without PKCE requires a confidential client")
            if (
                capabilities.nonce == "unsupported"
                and capabilities.pkce == "unsupported"
            ):
                raise ValueError("login without nonce requires PKCE")
        domains = [domain.strip().lower() for domain in policy.allowed_email_domains]
        if any(not domain or "@" in domain or "/" in domain for domain in domains):
            raise ValueError("invalid allowed email domain")
        policy = policy.model_copy(update={"allowed_email_domains": domains})
        context = ProviderContext(
            slug=policy.slug,
            config=MappingProxyType(config),
            secrets=resolve_secret,
            callback_url=f"{base}/api/v1/auth/sso/{policy.slug}/callback",
        )
        self.providers[policy.slug] = AuthProvider(policy, method, context)

    def get(self, slug: str) -> AuthProvider:
        return self.providers[slug]

    def login_methods(self):
        return [
            {
                "slug": provider.slug,
                "method_id": provider.policy.method,
                "protocol": provider.method.descriptor.protocol,
                "display_name": provider.policy.display_name or provider.slug,
                "label": dict(provider.method.descriptor.branding.label),
                **self.colors[provider.policy.method],
                "icon_url": (
                    f"/api/v1/auth/methods/{provider.policy.method}/icon.png"
                    if provider.policy.method in self.icons
                    else None
                ),
                "start_url": f"/api/v1/auth/sso/{provider.slug}/start",
                "supports_reauthentication": (
                    provider.method.descriptor.capabilities.reauthentication
                ),
            }
            for provider in self.providers.values()
            if provider.method.descriptor.interaction == "redirect"
        ]


def sanitize_icon(data: bytes) -> bytes:
    if len(data) > 16384:
        raise ValueError("auth icon exceeds 16 KiB")
    with Image.open(io.BytesIO(data)) as image:
        if image.format != "PNG" or not (
            0 < image.width <= 128 and 0 < image.height <= 128
        ):
            raise ValueError("auth icon must be PNG at most 128 by 128")
        output = io.BytesIO()
        image.convert("RGBA").save(output, format="PNG")
        return output.getvalue()


def branding_colors(branding):
    fallback = {"background": None, "foreground": None}
    colors = (branding.background, branding.foreground)
    if not all(
        isinstance(value, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", value)
        for value in colors
    ):
        return fallback

    def luminance(color):
        values = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        values = [
            value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
            for value in values
        ]
        return sum(
            value * weight
            for value, weight in zip(values, (0.2126, 0.7152, 0.0722), strict=True)
        )

    a, b = sorted(luminance(color) for color in colors)
    if (b + 0.05) / (a + 0.05) < 4.5:
        return fallback
    return {"background": branding.background, "foreground": branding.foreground}

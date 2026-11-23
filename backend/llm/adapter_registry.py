"""Validated catalog of LLM adapters and the one place that builds them.

Built-in and plugin adapters register the same way. Core code looks adapters
up by ``adapter_id``; it never branches on a provider.
"""

from __future__ import annotations

import copy
import inspect
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from packaging.version import InvalidVersion, Version

from backend.spi.llm import (
    COMMON_OPTIONS,
    LLM_ADAPTER_SPI_VERSION,
    CanonicalTextRequest,
    CanonicalTextResult,
    ExecutionContext,
    LLMAdapterDescriptor,
    LLMAdapterError,
    LLMAdapterFactory,
    LLMConnection,
    LLMModelCapabilities,
    LLMTransport,
    TextGenerationPort,
    TransportResponse,
)

BUILTIN_NAMESPACE = "builtin"
_ADAPTER_ID = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class LLMAdapterRegistryError(ValueError):
    """An adapter registration or connection cannot be served.

    Messages name the adapter, never a submitted value: schema errors would
    otherwise echo credentials.
    """


@dataclass(frozen=True)
class _Registration:
    factory: LLMAdapterFactory
    descriptor: LLMAdapterDescriptor
    # Built from private copies: nothing outside can change what is enforced.
    config: Draft202012Validator
    credential: Draft202012Validator


class LLMAdapterRegistry:
    def __init__(
        self,
        builtin: Iterable[LLMAdapterFactory] = (),
        plugins: Mapping[str, Iterable[LLMAdapterFactory]] | None = None,
    ) -> None:
        self._registrations: dict[str, _Registration] = {}
        for factory in builtin:
            self._register(factory, namespace=BUILTIN_NAMESPACE)
        for namespace, factories in (plugins or {}).items():
            if namespace == BUILTIN_NAMESPACE:
                raise LLMAdapterRegistryError(
                    "plugins cannot register in the builtin namespace"
                )
            for factory in factories:
                self._register(factory, namespace=namespace)

    def descriptor(self, adapter_id: str) -> LLMAdapterDescriptor:
        snapshot = self._registration(adapter_id).descriptor
        # Each caller gets its own copy, so editing one cannot change what is
        # enforced or what other callers see.
        return replace(
            snapshot,
            display_name=MappingProxyType(dict(snapshot.display_name)),
            config_schema=MappingProxyType(copy.deepcopy(dict(snapshot.config_schema))),
            credential_schema=MappingProxyType(
                copy.deepcopy(dict(snapshot.credential_schema))
            ),
        )

    def descriptors(self) -> tuple[LLMAdapterDescriptor, ...]:
        return tuple(self.descriptor(key) for key in sorted(self._registrations))

    def validate_registration(
        self,
        adapter_id: str,
        *,
        config: Mapping[str, object],
        credential: Mapping[str, str] | None,
        model_options: Iterable[str],
    ) -> str:
        """Validate stored input and resolve the adapter's default endpoint."""
        registration = self._registration(adapter_id)
        descriptor = registration.descriptor
        # Provider options are returned as metadata. Credential fields must
        # enter storage only through the write-only vault input.
        credential_fields = descriptor.credential_schema.get("properties", {})
        if _contains_write_only(descriptor.config_schema) or (
            isinstance(credential_fields, Mapping)
            and config.keys() & credential_fields.keys()
        ):
            raise LLMAdapterRegistryError(
                f"LLM adapter {adapter_id!r} cannot expose secret configuration fields"
            )
        self._validate_fields(registration, config, credential, model_options)
        return self._resolve_endpoint(registration, None).url

    def create(
        self,
        adapter_id: str,
        *,
        base_url: str | None,
        config: Mapping[str, object],
        credential: Mapping[str, str],
        model: str,
        capabilities: LLMModelCapabilities,
        transport: LLMTransport,
    ) -> TextGenerationPort:
        """Build one execution's adapter for one model from validated inputs.

        Every caller gets the same Core rules around the adapter: requests
        must name ``model`` and are checked against its capabilities before
        anything is sent, calls stay under the connection's endpoint, and provider
        request IDs are kept only when opaque and free of credentials.
        """

        registration = self._registration(adapter_id)
        self._validate_fields(registration, config, credential, capabilities.options)
        endpoint = self._resolve_endpoint(registration, base_url)
        connection = LLMConnection(
            base_url=endpoint.url,
            config=MappingProxyType(copy.deepcopy(dict(config))),
            credential=MappingProxyType(dict(credential)),
        )
        secrets = tuple(value for value in credential.values() if value)
        port = registration.factory.create(
            connection, _EndpointScopedTransport(transport, endpoint, secrets=secrets)
        )
        if not _binds_two_arguments(getattr(port, "generate", None), coroutine=True):
            raise LLMAdapterRegistryError(
                f"LLM adapter {adapter_id!r} did not build a text generation port"
            )
        return _GuardedPort(port, model, capabilities, secrets=secrets)

    @staticmethod
    def _validate_fields(
        registration: _Registration,
        config: Mapping[str, object],
        credential: Mapping[str, str] | None,
        model_options: Iterable[str],
    ) -> None:
        adapter_id = registration.descriptor.adapter_id
        if not set(model_options) <= registration.descriptor.options:
            raise LLMAdapterRegistryError(
                f"LLM adapter {adapter_id!r} cannot encode the model's options"
            )
        for validator, instance, name in (
            (registration.config, config, "config"),
            (registration.credential, credential, "credential"),
        ):
            if instance is not None and not validator.is_valid(dict(instance)):
                raise LLMAdapterRegistryError(
                    f"LLM adapter {adapter_id!r} rejected the {name}"
                )

    @staticmethod
    def _resolve_endpoint(
        registration: _Registration, base_url: str | None
    ) -> _Endpoint:
        endpoint = _Endpoint.parse(
            (base_url or registration.descriptor.default_base_url or "").rstrip("/")
        )
        if endpoint is None:
            raise LLMAdapterRegistryError(
                f"LLM adapter {registration.descriptor.adapter_id!r} "
                "needs an https base URL"
            )
        return endpoint

    def _registration(self, adapter_id: str) -> _Registration:
        try:
            return self._registrations[adapter_id]
        except KeyError:
            raise LLMAdapterRegistryError(
                f"LLM adapter {adapter_id!r} is not installed"
            ) from None

    def _register(self, factory: LLMAdapterFactory, *, namespace: str) -> None:
        descriptor = getattr(factory, "descriptor", None)
        if not isinstance(descriptor, LLMAdapterDescriptor) or not _binds_two_arguments(
            getattr(factory, "create", None), coroutine=False
        ):
            raise LLMAdapterRegistryError(
                "LLM adapter does not implement LLMAdapterFactory"
            )
        adapter_id = descriptor.adapter_id
        if not isinstance(adapter_id, str) or not _ADAPTER_ID.fullmatch(adapter_id):
            raise LLMAdapterRegistryError("LLM adapter id must be namespaced")
        if not adapter_id.startswith(f"{namespace}."):
            raise LLMAdapterRegistryError(
                f"LLM adapter {adapter_id!r} must use the {namespace!r} namespace"
            )
        if adapter_id in self._registrations:
            raise LLMAdapterRegistryError(f"duplicate LLM adapter {adapter_id!r}")
        snapshot = _validated_snapshot(descriptor)
        self._registrations[adapter_id] = _Registration(
            factory=factory,
            descriptor=snapshot,
            config=Draft202012Validator(copy.deepcopy(dict(snapshot.config_schema))),
            credential=Draft202012Validator(
                copy.deepcopy(dict(snapshot.credential_schema))
            ),
        )


@dataclass(frozen=True)
class _Endpoint:
    """An unambiguous https location: one origin and literal path segments."""

    url: str
    origin: tuple[str, int]
    segments: tuple[str, ...]

    @classmethod
    def parse(cls, url: str, *, query_allowed: bool = False) -> _Endpoint | None:
        # urlsplit silently drops tabs and newlines that the HTTP client would
        # reject, so such a URL is not the one being checked. Hosts are ASCII
        # (punycode) for the same reason.
        if (
            not url.isascii()
            or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in url)
            # An empty "?" or "#" parses as no query or fragment but still
            # changes what is appended to the URL.
            or "#" in url
            or ("?" in url and not query_allowed)
        ):
            return None
        try:
            parts = urlsplit(url)
            port = 443 if parts.port is None else parts.port
        except ValueError:
            return None
        if (
            port == 0
            or parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
        ):
            return None
        segments = tuple(parts.path.split("/")[1:])
        # Dot segments and separators, literal or percent-encoded, could make
        # the server resolve a different path than the one checked here.
        if any(
            unquote(segment) in {".", ".."}
            or "/" in unquote(segment)
            or "\\" in unquote(segment)
            for segment in segments
        ):
            return None
        return cls(url=url, origin=(parts.hostname, port), segments=segments)

    def contains(self, other: _Endpoint) -> bool:
        return (
            other.origin == self.origin
            and other.segments[: len(self.segments)] == self.segments
        )


class _EndpointScopedTransport:
    """Keeps every call of one execution under its connection's endpoint, so
    an adapter cannot send the credential anywhere else or put it in a URL."""

    def __init__(
        self, transport: LLMTransport, endpoint: _Endpoint, *, secrets: tuple[str, ...]
    ) -> None:
        self._transport = transport
        self._endpoint = endpoint
        self._secrets = secrets

    async def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
        timeout_seconds: float,
    ) -> TransportResponse:
        # A request may carry query parameters (an API version, say); the
        # credential check below keeps secrets out of them.
        target = _Endpoint.parse(url, query_allowed=True)
        if (
            target is None
            or not self._endpoint.contains(target)
            or any(name.lower() == "host" for name in headers)
            or any(secret in url or secret in unquote(url) for secret in self._secrets)
        ):
            raise LLMAdapterError("invalid_request", delivery="not_sent")
        return await self._transport.post_json(
            url, headers=headers, payload=payload, timeout_seconds=timeout_seconds
        )


class _GuardedPort:
    """The Core rules around one execution's adapter (see ``create``)."""

    def __init__(
        self,
        port: TextGenerationPort,
        model: str,
        capabilities: LLMModelCapabilities,
        *,
        secrets: tuple[str, ...],
    ) -> None:
        self._port = port
        self._model = model
        self._capabilities = capabilities
        self._secrets = secrets

    async def generate(
        self, request: CanonicalTextRequest, context: ExecutionContext
    ) -> CanonicalTextResult:
        # The capabilities describe one model; another model is not covered.
        if request.model != self._model:
            raise LLMAdapterError("invalid_request", delivery="not_sent")
        self._capabilities.require_supported(request)
        try:
            result = await self._port.generate(request, context)
        except LLMAdapterError as exc:
            exc.provider_request_id = self._opaque(exc.provider_request_id)
            raise
        if not isinstance(result, CanonicalTextResult):
            raise LLMAdapterError("invalid_response", delivery="unknown")
        return replace(
            result, provider_request_id=self._opaque(result.provider_request_id)
        )

    def _opaque(self, request_id: str | None) -> str | None:
        # Provider IDs come from response headers or bodies a server controls.
        if (
            isinstance(request_id, str)
            and _REQUEST_ID.fullmatch(request_id)
            and not any(secret in request_id for secret in self._secrets)
        ):
            return request_id
        return None


def _binds_two_arguments(function: object, *, coroutine: bool) -> bool:
    if not callable(function) or inspect.iscoroutinefunction(function) != coroutine:
        return False
    try:
        inspect.signature(function).bind(None, None)
    except (TypeError, ValueError):
        return False
    return True


def _validated_snapshot(descriptor: LLMAdapterDescriptor) -> LLMAdapterDescriptor:
    adapter_id = descriptor.adapter_id
    if descriptor.spi_version != LLM_ADAPTER_SPI_VERSION:
        raise LLMAdapterRegistryError(
            f"LLM adapter {adapter_id!r} targets an incompatible SPI version"
        )
    try:
        Version(descriptor.adapter_version)
    except (InvalidVersion, TypeError):
        raise LLMAdapterRegistryError(
            f"LLM adapter {adapter_id!r} needs a PEP 440 adapter_version"
        ) from None
    options = frozenset(descriptor.options)
    labels = descriptor.display_name
    if (
        not options <= COMMON_OPTIONS
        or type(descriptor.config_schema_version) is not int
        or descriptor.config_schema_version < 1
        or not isinstance(labels, Mapping)
        or not labels
        or not all(
            isinstance(locale, str) and isinstance(label, str) and label.strip()
            for locale, label in labels.items()
        )
        or (
            descriptor.default_base_url is not None
            and _Endpoint.parse(descriptor.default_base_url) is None
        )
    ):
        raise LLMAdapterRegistryError(f"LLM adapter {adapter_id!r} is malformed")
    schemas = {}
    for name in ("config_schema", "credential_schema"):
        schema = copy.deepcopy(dict(getattr(descriptor, name)))
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError:
            raise LLMAdapterRegistryError(
                f"LLM adapter {adapter_id!r} has an invalid {name}"
            ) from None
        if _uses_references(schema):
            raise LLMAdapterRegistryError(
                f"LLM adapter {adapter_id!r} {name} must be self-contained"
            )
        schemas[name] = MappingProxyType(schema)
    return replace(
        descriptor,
        display_name=MappingProxyType(dict(labels)),
        options=options,
        **schemas,
    )


# References can fetch, miss or loop, and "$schema" can switch to a dialect
# with other reference keywords; connection and credential forms need none.
_REFERENCE_KEYWORDS = frozenset(
    {
        "$schema",
        "$ref",
        "$dynamicRef",
        "$recursiveRef",
        "$id",
        "$anchor",
        "$dynamicAnchor",
        "$recursiveAnchor",
    }
)


def _uses_references(schema: object) -> bool:
    if isinstance(schema, Mapping):
        return any(
            key in _REFERENCE_KEYWORDS or _uses_references(value)
            for key, value in schema.items()
        )
    if isinstance(schema, list):
        return any(_uses_references(item) for item in schema)
    return False


def _contains_write_only(schema: object) -> bool:
    if isinstance(schema, Mapping):
        return schema.get("writeOnly") is True or any(
            _contains_write_only(value) for value in schema.values()
        )
    if isinstance(schema, list):
        return any(_contains_write_only(item) for item in schema)
    return False


__all__ = ["BUILTIN_NAMESPACE", "LLMAdapterRegistry", "LLMAdapterRegistryError"]

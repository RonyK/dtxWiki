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

    def require_capabilities(
        self, adapter_id: str, capabilities: LLMModelCapabilities
    ) -> None:
        """A model can only use options its adapter knows how to encode."""

        if not capabilities.options <= self.descriptor(adapter_id).options:
            raise LLMAdapterRegistryError(
                f"LLM adapter {adapter_id!r} cannot encode the model's options"
            )

    def create(
        self,
        adapter_id: str,
        *,
        base_url: str | None,
        config: Mapping[str, object],
        credential: Mapping[str, str],
        transport: LLMTransport,
    ) -> TextGenerationPort:
        """Build one execution's adapter from validated inputs."""

        registration = self._registration(adapter_id)
        for validator, instance, name in (
            (registration.config, config, "config"),
            (registration.credential, credential, "credential"),
        ):
            if not validator.is_valid(dict(instance)):
                raise LLMAdapterRegistryError(
                    f"LLM adapter {adapter_id!r} rejected the {name}"
                )
        endpoint = _Endpoint.parse(
            (base_url or registration.descriptor.default_base_url or "").rstrip("/")
        )
        if endpoint is None:
            raise LLMAdapterRegistryError(
                f"LLM adapter {adapter_id!r} needs an https base URL"
            )
        connection = LLMConnection(
            base_url=endpoint.url,
            config=MappingProxyType(copy.deepcopy(dict(config))),
            credential=MappingProxyType(dict(credential)),
        )
        port = registration.factory.create(
            connection,
            _EndpointScopedTransport(
                transport,
                endpoint,
                secrets=tuple(value for value in credential.values() if value),
            ),
        )
        if not _is_generation_port(port):
            raise LLMAdapterRegistryError(
                f"LLM adapter {adapter_id!r} did not build a text generation port"
            )
        return port

    def _registration(self, adapter_id: str) -> _Registration:
        try:
            return self._registrations[adapter_id]
        except KeyError:
            raise LLMAdapterRegistryError(
                f"LLM adapter {adapter_id!r} is not installed"
            ) from None

    def _register(self, factory: LLMAdapterFactory, *, namespace: str) -> None:
        descriptor = getattr(factory, "descriptor", None)
        if not isinstance(descriptor, LLMAdapterDescriptor) or not callable(
            getattr(factory, "create", None)
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
    def parse(cls, url: str) -> _Endpoint | None:
        # urlsplit silently drops tabs and newlines that the HTTP client would
        # reject, so such a URL is not the one being checked. Hosts are ASCII
        # (punycode) for the same reason.
        if not url.isascii() or any(
            ord(char) <= 0x20 or ord(char) == 0x7F for char in url
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
            or parts.query
            or parts.fragment
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
            and len(other.segments) > len(self.segments)
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
        target = _Endpoint.parse(url)
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


def _is_generation_port(port: object) -> bool:
    generate = getattr(port, "generate", None)
    if not inspect.iscoroutinefunction(generate):
        return False
    try:
        inspect.signature(generate).bind(None, None)
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
        if not _references_stay_local(schema):
            raise LLMAdapterRegistryError(
                f"LLM adapter {adapter_id!r} {name} references another schema"
            )
        schemas[name] = MappingProxyType(schema)
    return replace(
        descriptor,
        display_name=MappingProxyType(dict(labels)),
        options=options,
        **schemas,
    )


# Keywords that would let a reference resolve somewhere other than this
# schema's own definitions.
_RESOLUTION_KEYWORDS = frozenset({"$id", "$anchor", "$dynamicAnchor", "$dynamicRef"})


def _references_stay_local(root: Mapping[str, object]) -> bool:
    """Every ``$ref`` is a JSON pointer to an existing part of this schema, so
    validation never fetches or fails to find a schema."""

    def local(node: object) -> bool:
        if isinstance(node, Mapping):
            return all(
                key not in _RESOLUTION_KEYWORDS
                and (key != "$ref" or _points_into(root, value))
                and local(value)
                for key, value in node.items()
            )
        if isinstance(node, list):
            return all(local(item) for item in node)
        return True

    return local(root)


def _points_into(root: Mapping[str, object], reference: object) -> bool:
    if not isinstance(reference, str) or not reference.startswith("#"):
        return False
    pointer = unquote(reference[1:])
    if not pointer:
        return True
    if not pointer.startswith("/"):
        return False
    node: object = root
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping) and token in node:
            node = node[token]
        elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
            node = node[int(token)]
        else:
            return False
    # The target must itself be a schema.
    return isinstance(node, (Mapping, bool))


__all__ = ["BUILTIN_NAMESPACE", "LLMAdapterRegistry", "LLMAdapterRegistryError"]

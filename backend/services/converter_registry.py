"""Deterministic worker-side converter discovery and selection.

The registry is a security boundary.  Converter descriptors and operator
manifests are treated as untrusted configuration: values are checked without
truthy/int coercion, descriptors are snapshotted once, and external providers
are admitted only after explicit sandbox, network, licence, and SBOM gates.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import sys
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from backend.converters.builtin_structured import BuiltinStructuredConverter
from backend.converters.builtin_text import BuiltinTextConverter
from backend.converters.external_protocol import ExternalProcessConverter
from backend.converters.ooxml_adapter import OOXMLConverter
from backend.spi.conversion import (
    CONVERSION_OUTPUT_SCHEMA_VERSION,
    CONVERTER_SPI_VERSION,
    ConverterDescriptor,
    ConverterPlugin,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
_VERSION = re.compile(r"^[0-9A-Za-z](?:[0-9A-Za-z._+-]{0,126}[0-9A-Za-z])?$")
_MEDIA_TYPE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$"
)
_SPDX_EXPRESSION = re.compile(r"^[A-Za-z0-9.+() -]{1,256}$")
_EXECUTION_MODES = frozenset({"builtin", "subprocess", "sidecar"})
_CAPABILITIES = frozenset(
    {"assets", "locations", "markdown", "metadata", "ocr", "preview", "tables"}
)
_PLACEHOLDER_VERSIONS = frozenset(
    {"dev", "development", "latest", "main", "master", "not-installed", "unknown"}
)
_PLACEHOLDER_LICENSES = frozenset(
    {"none", "noassertion", "placeholder", "see license", "todo", "unknown"}
)
_PLACEHOLDER_DIGESTS = frozenset(
    {
        "0" * 64,
        "f" * 64,
        hashlib.sha256(b"").hexdigest(),
        hashlib.sha256(b"placeholder").hexdigest(),
        hashlib.sha256(b"todo").hexdigest(),
    }
)
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_MANIFESTS = 128
_MAX_COMMAND_TOKENS = 128
_MAX_COMMAND_BYTES = 32 * 1024
_MAX_DESCRIPTOR_VALUES = 256
_MAX_PRIORITY = 1_000_000
_NONDETERMINISTIC_RESUME_REASON = "converter_nondeterministic_resume_unsupported"
_MANIFEST_KEYS = frozenset(
    {
        "capabilities",
        "command",
        "converter_id",
        "converter_version",
        "deterministic",
        "distribution_digest",
        "enabled",
        "execution_mode",
        "formats",
        "license_approved",
        "license_expression",
        "media_types",
        "network_approved",
        "network_required",
        "output_schema_version",
        "priority",
        "sandbox_approved",
        "sandbox_profile",
        "sbom_approved",
        "sbom_sha256",
        "signature_approved",
        "signature_identity",
        "source_url",
        "spi_version",
    }
)


class ConverterRegistryError(ValueError):
    """Safe configuration error raised before a converter can execute."""


@dataclass(frozen=True, slots=True)
class ConverterHealth:
    converter_id: str
    converter_version: str
    formats: tuple[str, ...]
    execution_mode: str
    enabled: bool
    ready: bool
    reason_code: str | None
    license_expression: str
    source_url: str
    distribution_digest: str | None
    sbom_sha256: str | None
    signature_identity: str | None
    sandbox_profile: str | None


class ConverterRegistry:
    """A deterministic collection of validated converter snapshots."""

    def __init__(self, plugins: tuple[ConverterPlugin, ...] = ()) -> None:
        self._plugins: dict[str, ConverterPlugin] = {}
        self._descriptors: dict[str, ConverterDescriptor] = {}
        self._health_cache: dict[str, tuple[bool, str | None]] = {}
        self._health_lock = threading.Lock()
        for plugin in plugins:
            self.register(plugin)

    def register(self, plugin: ConverterPlugin) -> None:
        if (
            not callable(getattr(plugin, "descriptor", None))
            or not callable(getattr(plugin, "probe", None))
            or not callable(getattr(plugin, "convert", None))
        ):
            raise ConverterRegistryError("converter does not implement the SPI")
        try:
            descriptor = plugin.descriptor()
        except Exception as exc:
            raise ConverterRegistryError("converter descriptor is unavailable") from exc
        try:
            _validate_descriptor(descriptor)
        except ConverterRegistryError:
            raise
        except Exception as exc:
            raise ConverterRegistryError("converter descriptor is invalid") from exc
        if descriptor.converter_id in self._plugins:
            raise ConverterRegistryError(
                f"duplicate converter id: {descriptor.converter_id}"
            )
        for existing in self._descriptors.values():
            collisions = descriptor.formats.intersection(existing.formats)
            if collisions and descriptor.priority == existing.priority:
                format_id = min(collisions)
                raise ConverterRegistryError(
                    "converter priority collision for "
                    f"{format_id}: {existing.converter_id} and "
                    f"{descriptor.converter_id}"
                )
        # Store the exact validated value once.  A plugin whose descriptor()
        # later changes cannot perturb ordering or provenance snapshots.
        self._plugins[descriptor.converter_id] = plugin
        self._descriptors[descriptor.converter_id] = descriptor

    def plugin(self, converter_id: str) -> ConverterPlugin:
        try:
            return self._plugins[converter_id]
        except KeyError as exc:
            raise ConverterRegistryError(
                f"converter is not available: {converter_id}"
            ) from exc

    def descriptor(self, converter_id: str) -> ConverterDescriptor:
        """Return the immutable descriptor snapshot captured at registration."""

        try:
            return self._descriptors[converter_id]
        except KeyError as exc:
            raise ConverterRegistryError(
                f"converter is not available: {converter_id}"
            ) from exc

    def available_formats(self) -> set[str]:
        """Return admitted formats, including providers not yet health-checked."""

        return {
            format_id
            for descriptor in self._descriptors.values()
            for format_id in descriptor.formats
        }

    def ready_formats(self) -> set[str]:
        """Return formats with at least one currently executable provider."""

        ready_ids = {health.converter_id for health in self.health() if health.ready}
        return {
            format_id
            for converter_id, descriptor in self._descriptors.items()
            if converter_id in ready_ids
            for format_id in descriptor.formats
        }

    def ready_input_limit(self, format_id: str) -> int | None:
        """Return a fail-closed immutable cap across ready providers.

        The orchestrator may begin with any provider in the ready chain, so the
        advertised limit must be safe for every bounded provider. ``None``
        means no ready provider declares a provider-specific cap; deployment
        and Workspace limits still apply.
        """

        chain = self.select_chain(format_id, ready_only=True)
        limits = [
            item.max_input_bytes
            for item in chain
            if item.max_input_bytes is not None
        ]
        if not limits:
            return None
        return min(limits)

    def select_chain(
        self,
        format_id: str,
        *,
        preferred_ids: tuple[str, ...] = (),
        allow_network: bool = False,
        ready_only: bool = False,
    ) -> tuple[ConverterDescriptor, ...]:
        if type(format_id) is not str or not _IDENTIFIER.fullmatch(format_id):
            raise ConverterRegistryError("format id is invalid")
        if type(preferred_ids) is not tuple or not all(
            type(value) is str for value in preferred_ids
        ):
            raise ConverterRegistryError("preferred converter ids must be a tuple")
        if len(set(preferred_ids)) != len(preferred_ids):
            raise ConverterRegistryError("preferred converter ids contain duplicates")
        if type(allow_network) is not bool:
            raise ConverterRegistryError("allow_network must be a boolean")
        if type(ready_only) is not bool:
            raise ConverterRegistryError("ready_only must be a boolean")
        candidates = [
            descriptor
            for descriptor in self._descriptors.values()
            if format_id in descriptor.formats
            and descriptor.deterministic
            and (allow_network or not descriptor.network_required)
        ]
        if ready_only:
            candidates = [
                descriptor
                for descriptor in candidates
                if self._health_state(descriptor.converter_id)[0]
            ]
        preference = {
            converter_id: index for index, converter_id in enumerate(preferred_ids)
        }
        return tuple(
            sorted(
                candidates,
                key=lambda descriptor: (
                    preference.get(descriptor.converter_id, len(preference)),
                    -descriptor.priority,
                    descriptor.converter_id,
                ),
            )
        )

    def provider_chain_snapshot(
        self,
        format_id: str,
        *,
        preferred_ids: tuple[str, ...] = (),
        allow_network: bool = False,
        ready_only: bool = False,
    ) -> list[dict[str, Any]]:
        return [
            {
                "converter_id": descriptor.converter_id,
                "converter_version": descriptor.converter_version,
                "spi_version": descriptor.spi_version,
                "priority": descriptor.priority,
                "execution_mode": descriptor.execution_mode,
                "network_required": descriptor.network_required,
                "deterministic": descriptor.deterministic,
                "license_expression": descriptor.license_expression,
                "distribution_digest": descriptor.distribution_digest,
                "sbom_sha256": descriptor.sbom_sha256,
                "signature_identity": descriptor.signature_identity,
                "sandbox_profile": descriptor.sandbox_profile,
                "max_input_bytes": descriptor.max_input_bytes,
                "output_schema_version": CONVERSION_OUTPUT_SCHEMA_VERSION,
            }
            for descriptor in self.select_chain(
                format_id,
                preferred_ids=preferred_ids,
                allow_network=allow_network,
                ready_only=ready_only,
            )
        ]

    def health(self) -> tuple[ConverterHealth, ...]:
        return tuple(
            ConverterHealth(
                converter_id=descriptor.converter_id,
                converter_version=descriptor.converter_version,
                formats=tuple(sorted(descriptor.formats)),
                execution_mode=descriptor.execution_mode,
                enabled=True,
                ready=self._health_state(descriptor.converter_id)[0],
                reason_code=self._health_state(descriptor.converter_id)[1],
                license_expression=descriptor.license_expression,
                source_url=descriptor.source_url,
                distribution_digest=descriptor.distribution_digest,
                sbom_sha256=descriptor.sbom_sha256,
                signature_identity=descriptor.signature_identity,
                sandbox_profile=descriptor.sandbox_profile,
            )
            for descriptor in sorted(
                self._descriptors.values(), key=lambda value: value.converter_id
            )
        )

    def _health_state(self, converter_id: str) -> tuple[bool, str | None]:
        with self._health_lock:
            cached = self._health_cache.get(converter_id)
            if cached is not None:
                return cached
            descriptor = self._descriptors[converter_id]
            plugin = self._plugins[converter_id]
            # Reclaimed attempts must converge on the exact bytes installed by
            # an earlier execution. Until converter results have a durable
            # resume manifest, rerunning a nondeterministic provider cannot
            # uphold that idempotency contract. Keep it visible to operators,
            # but never advertise or select it as executable.
            if not descriptor.deterministic:
                result = (False, _NONDETERMINISTIC_RESUME_REASON)
            elif descriptor.execution_mode == "builtin":
                result = (True, None)
            else:
                checker = getattr(plugin, "healthcheck", None)
                if not callable(checker):
                    result = (False, "converter_health_unavailable")
                else:
                    try:
                        checked = checker()
                    except Exception:
                        checked = (False, "converter_health_unavailable")
                    result = (
                        checked
                        if (
                            type(checked) is tuple
                            and len(checked) == 2
                            and type(checked[0]) is bool
                            and (checked[1] is None or type(checked[1]) is str)
                        )
                        else (False, "converter_health_invalid")
                    )
            self._health_cache[converter_id] = result
            return result


def build_converter_registry(
    *,
    external_manifests_json: str | None = None,
    os_sandboxed: bool = False,
    network_allowed: bool = False,
    extra_plugins: tuple[ConverterPlugin, ...] = (),
) -> ConverterRegistry:
    if type(os_sandboxed) is not bool or type(network_allowed) is not bool:
        raise ConverterRegistryError("converter policy flags must be booleans")
    if type(extra_plugins) is not tuple:
        raise ConverterRegistryError("extra_plugins must be a tuple")
    plugins: list[ConverterPlugin] = [
        BuiltinTextConverter(),
        BuiltinStructuredConverter(),
        _bundled_subprocess(OOXMLConverter()),
        *extra_plugins,
    ]
    if external_manifests_json:
        try:
            plugins.extend(
                _external_plugins_from_json(
                    external_manifests_json,
                    os_sandboxed=os_sandboxed,
                    network_allowed=network_allowed,
                )
            )
        except ConverterRegistryError:
            raise
        except Exception as exc:
            raise ConverterRegistryError("converter manifest is invalid") from exc
    return ConverterRegistry(tuple(plugins))


def _bundled_subprocess(plugin: ConverterPlugin) -> ConverterPlugin:
    descriptor = plugin.descriptor()
    descriptor = replace(
        descriptor,
        distribution_digest=_bundled_distribution_digest(plugin),
    )
    host_command = (
        ("converter-host",)
        if getattr(sys, "frozen", False)
        else (
            "-I",
            "-s",
            str(
                (Path(__file__).resolve().parents[1] / "worker" / "converter_host.py")
                .resolve(strict=True)
            ),
        )
    )
    return ExternalProcessConverter(
        descriptor,
        command=(
            sys.executable,
            *host_command,
            "--converter",
            descriptor.converter_id,
        ),
        preserve_executable_path=True,
    )


def _bundled_distribution_digest(plugin: ConverterPlugin) -> str:
    """Hash the first-party subprocess execution closure available on disk.

    This is intentionally not the wheel checksum: it pins the source modules
    that implement the built-in host recipe.  Third-party/native dependencies
    still require their release SBOM digest through an external manifest.
    """

    backend_root = Path(__file__).resolve().parents[1]
    plugin_source = inspect.getsourcefile(type(plugin))
    if plugin_source is None:
        raise ConverterRegistryError("bundled converter source is unavailable")
    files = {
        Path(plugin_source).resolve(),
        (backend_root / "app" / "process_tree.py").resolve(),
        (backend_root / "converters" / "builtin_text.py").resolve(),
        (backend_root / "converters" / "external_protocol.py").resolve(),
        (backend_root / "spi" / "conversion.py").resolve(),
        (backend_root / "worker" / "converter_host.py").resolve(),
    }
    digest = hashlib.sha256()
    try:
        for path in sorted(files, key=lambda value: value.as_posix()):
            relative = path.relative_to(backend_root).as_posix()
            content = path.read_bytes()
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative.encode("utf-8"))
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
    except (OSError, ValueError) as exc:
        raise ConverterRegistryError("bundled converter digest is unavailable") from exc
    return digest.hexdigest()


def _external_plugins_from_json(
    raw_json: str,
    *,
    os_sandboxed: bool,
    network_allowed: bool,
) -> list[ConverterPlugin]:
    try:
        raw_size = len(raw_json.encode("utf-8")) if type(raw_json) is str else 0
    except UnicodeError as exc:
        raise ConverterRegistryError("converter manifest JSON is invalid") from exc
    if type(raw_json) is not str or raw_size > _MAX_MANIFEST_BYTES:
        raise ConverterRegistryError("converter manifest JSON exceeds its limit")
    try:
        manifests = json.loads(raw_json, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
        raise ConverterRegistryError("converter manifest JSON is invalid") from exc
    if type(manifests) is not list:
        raise ConverterRegistryError("converter manifests must be a JSON list")
    if len(manifests) > _MAX_MANIFESTS:
        raise ConverterRegistryError("converter manifest count exceeds its limit")
    plugins: list[ConverterPlugin] = []
    for manifest in manifests:
        if type(manifest) is not dict:
            raise ConverterRegistryError("converter manifest must be an object")
        if not all(type(key) is str for key in manifest):
            raise ConverterRegistryError("converter manifest keys must be strings")
        if set(manifest).difference(_MANIFEST_KEYS):
            raise ConverterRegistryError("converter manifest contains unknown fields")
        enabled = _optional_bool(manifest, "enabled", default=True)
        if not enabled:
            continue
        if not os_sandboxed:
            raise ConverterRegistryError(
                "external converters require an operator-confirmed OS sandbox"
            )
        if not _required_bool(manifest, "sandbox_approved"):
            raise ConverterRegistryError("external converter sandbox is not approved")
        sandbox_profile = _required_string(manifest, "sandbox_profile", max_bytes=128)
        if sandbox_profile.casefold() in {
            "none",
            "python",
            "python-timeout",
            "timeout",
            "unsandboxed",
        }:
            raise ConverterRegistryError(
                "external converter sandbox profile is invalid"
            )

        network_required = _required_bool(manifest, "network_required")
        network_approved = _required_bool(manifest, "network_approved")
        if network_required and (not network_allowed or not network_approved):
            raise ConverterRegistryError(
                "network-required converter is disabled by instance policy"
            )
        if network_approved and not network_required:
            raise ConverterRegistryError(
                "network approval is invalid for a network-disabled converter"
            )
        if not _required_bool(manifest, "license_approved"):
            raise ConverterRegistryError("external converter license is not approved")
        if not _required_bool(manifest, "sbom_approved"):
            raise ConverterRegistryError("external converter SBOM is not approved")
        sbom_sha256 = _required_sha256(manifest, "sbom_sha256")
        if not _required_bool(manifest, "signature_approved"):
            raise ConverterRegistryError("external converter signature is not approved")
        signature_identity = _required_string(
            manifest, "signature_identity", max_bytes=512
        )

        execution_mode = _required_string(manifest, "execution_mode", max_bytes=32)
        if execution_mode != "subprocess":
            raise ConverterRegistryError(
                "external process manifests support only subprocess execution"
            )
        spi_version = _required_string(manifest, "spi_version", max_bytes=32)
        if spi_version != CONVERTER_SPI_VERSION:
            raise ConverterRegistryError(
                f"unsupported converter SPI version: {spi_version}"
            )
        output_schema_version = _required_string(
            manifest, "output_schema_version", max_bytes=32
        )
        if output_schema_version != CONVERSION_OUTPUT_SCHEMA_VERSION:
            raise ConverterRegistryError("unsupported converter output schema version")
        command = _required_command(manifest)
        descriptor = ConverterDescriptor(
            converter_id=_required_string(manifest, "converter_id", max_bytes=128),
            converter_version=_required_string(
                manifest, "converter_version", max_bytes=128
            ),
            spi_version=spi_version,
            formats=frozenset(_required_string_list(manifest, "formats")),
            media_types=frozenset(_required_string_list(manifest, "media_types")),
            priority=_required_int(manifest, "priority"),
            execution_mode="subprocess",
            network_required=network_required,
            deterministic=_required_bool(manifest, "deterministic"),
            license_expression=_required_string(
                manifest, "license_expression", max_bytes=256
            ),
            source_url=_required_string(manifest, "source_url", max_bytes=2048),
            capabilities=frozenset(_required_string_list(manifest, "capabilities")),
            distribution_digest=_required_sha256(manifest, "distribution_digest"),
            sbom_sha256=sbom_sha256,
            signature_identity=signature_identity,
            sandbox_profile=sandbox_profile,
        )
        _validate_descriptor(descriptor)
        try:
            plugins.append(ExternalProcessConverter(descriptor, command=tuple(command)))
        except ValueError as exc:
            raise ConverterRegistryError(
                "external converter command is invalid"
            ) from exc
    return plugins


def _validate_descriptor(descriptor: ConverterDescriptor) -> None:
    if type(descriptor) is not ConverterDescriptor:
        raise ConverterRegistryError("converter descriptor has an invalid type")
    if descriptor.spi_version != CONVERTER_SPI_VERSION:
        raise ConverterRegistryError(
            f"unsupported converter SPI version: {descriptor.spi_version}"
        )
    _validate_identifier(descriptor.converter_id, field="converter id")
    if (
        type(descriptor.converter_version) is not str
        or not _VERSION.fullmatch(descriptor.converter_version)
        or descriptor.converter_version.casefold() in _PLACEHOLDER_VERSIONS
    ):
        raise ConverterRegistryError("converter version is invalid or unpinned")
    _validate_string_set(descriptor.formats, field="formats", validator=_IDENTIFIER)
    _validate_string_set(
        descriptor.media_types, field="media types", validator=_MEDIA_TYPE
    )
    _validate_string_set(
        descriptor.capabilities,
        field="capabilities",
        allowed_values=_CAPABILITIES,
    )
    if type(descriptor.priority) is not int or not (
        -_MAX_PRIORITY <= descriptor.priority <= _MAX_PRIORITY
    ):
        raise ConverterRegistryError("converter priority is invalid")
    if (
        type(descriptor.execution_mode) is not str
        or descriptor.execution_mode not in _EXECUTION_MODES
    ):
        raise ConverterRegistryError("converter execution mode is invalid")
    if type(descriptor.network_required) is not bool:
        raise ConverterRegistryError("converter network_required must be a boolean")
    if type(descriptor.deterministic) is not bool:
        raise ConverterRegistryError("converter deterministic must be a boolean")
    if descriptor.network_required and descriptor.execution_mode == "builtin":
        raise ConverterRegistryError("builtin converters cannot require network")
    if descriptor.max_input_bytes is not None and (
        isinstance(descriptor.max_input_bytes, bool)
        or not isinstance(descriptor.max_input_bytes, int)
        or descriptor.max_input_bytes <= 0
    ):
        raise ConverterRegistryError("converter max_input_bytes must be positive")
    _validate_license_expression(descriptor.license_expression)
    _validate_source_url(descriptor.source_url)
    digest = descriptor.distribution_digest
    if digest is not None and not _is_valid_digest(digest):
        raise ConverterRegistryError(
            "converter distribution digest must be a non-placeholder SHA-256"
        )
    if descriptor.execution_mode != "builtin" and digest is None:
        raise ConverterRegistryError(
            "external converter descriptors require a distribution digest"
        )
    if descriptor.sbom_sha256 is not None and not _is_valid_digest(
        descriptor.sbom_sha256
    ):
        raise ConverterRegistryError("converter SBOM digest is invalid")
    for value, field in (
        (descriptor.signature_identity, "signature identity"),
        (descriptor.sandbox_profile, "sandbox profile"),
    ):
        if value is not None and (
            type(value) is not str
            or not value.strip()
            or value != value.strip()
            or len(value.encode("utf-8")) > 512
            or any(ord(character) < 32 for character in value)
        ):
            raise ConverterRegistryError(f"converter {field} is invalid")


def _validate_identifier(value: object, *, field: str) -> None:
    if type(value) is not str or not _IDENTIFIER.fullmatch(value):
        raise ConverterRegistryError(f"{field} is invalid")


def _validate_string_set(
    value: object,
    *,
    field: str,
    validator: re.Pattern[str] | None = None,
    allowed_values: frozenset[str] | None = None,
) -> None:
    if type(value) is not frozenset or not value or len(value) > _MAX_DESCRIPTOR_VALUES:
        raise ConverterRegistryError(f"converter {field} must be a non-empty frozenset")
    for item in value:
        if type(item) is not str or len(item.encode("utf-8")) > 256:
            raise ConverterRegistryError(f"converter {field} contains an invalid value")
        if validator is not None and not validator.fullmatch(item):
            raise ConverterRegistryError(f"converter {field} contains an invalid value")
        if allowed_values is not None and item not in allowed_values:
            raise ConverterRegistryError(f"converter {field} contains an invalid value")


def _validate_license_expression(value: object) -> None:
    if (
        type(value) is not str
        or value != value.strip()
        or not _SPDX_EXPRESSION.fullmatch(value)
        or value.casefold() in _PLACEHOLDER_LICENSES
    ):
        raise ConverterRegistryError("converter license expression is invalid")


def _validate_source_url(value: object) -> None:
    if type(value) is not str or len(value.encode("utf-8")) > 2048:
        raise ConverterRegistryError("converter source URL is invalid")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError as exc:
        raise ConverterRegistryError("converter source URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConverterRegistryError("converter source URL must be a public HTTPS URL")


def _is_valid_digest(value: object) -> bool:
    return (
        type(value) is str
        and _SHA256.fullmatch(value) is not None
        and value not in _PLACEHOLDER_DIGESTS
        and len(set(value)) > 1
    )


def _required_sha256(manifest: dict[str, Any], key: str) -> str:
    value = manifest.get(key)
    if not _is_valid_digest(value):
        raise ConverterRegistryError(
            f"external converter {key} must be a non-placeholder SHA-256"
        )
    return value


def _required_string(manifest: dict[str, Any], key: str, *, max_bytes: int) -> str:
    value = manifest.get(key)
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > max_bytes
        or any(ord(character) < 32 for character in value)
    ):
        raise ConverterRegistryError(f"converter manifest {key} is invalid")
    return value


def _required_string_list(manifest: dict[str, Any], key: str) -> list[str]:
    value = manifest.get(key)
    if (
        type(value) is not list
        or not value
        or len(value) > _MAX_DESCRIPTOR_VALUES
        or not all(
            type(item) is str
            and item
            and item == item.strip()
            and len(item.encode("utf-8")) <= 256
            and not any(ord(character) < 32 for character in item)
            for item in value
        )
    ):
        raise ConverterRegistryError(
            f"converter manifest {key} must be a bounded non-empty string list"
        )
    if len(set(value)) != len(value):
        raise ConverterRegistryError(f"converter manifest {key} contains duplicates")
    return value


def _required_bool(manifest: dict[str, Any], key: str) -> bool:
    value = manifest.get(key)
    if type(value) is not bool:
        raise ConverterRegistryError(f"converter manifest {key} must be a boolean")
    return value


def _optional_bool(manifest: dict[str, Any], key: str, *, default: bool) -> bool:
    value = manifest.get(key, default)
    if type(value) is not bool:
        raise ConverterRegistryError(f"converter manifest {key} must be a boolean")
    return value


def _required_int(manifest: dict[str, Any], key: str) -> int:
    value = manifest.get(key)
    if type(value) is not int or not -_MAX_PRIORITY <= value <= _MAX_PRIORITY:
        raise ConverterRegistryError(f"converter manifest {key} must be an integer")
    return value


def _required_command(manifest: dict[str, Any]) -> list[str]:
    value = manifest.get("command")
    if (
        type(value) is not list
        or not value
        or len(value) > _MAX_COMMAND_TOKENS
        or not all(
            type(token) is str
            and token
            and "\x00" not in token
            and not any(ord(character) < 32 for character in token)
            for token in value
        )
        or sum(len(token.encode("utf-8")) for token in value) > _MAX_COMMAND_BYTES
        or not Path(value[0]).is_absolute()
    ):
        raise ConverterRegistryError(
            "external converter command must be a bounded absolute token list"
        )
    return value


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


__all__ = [
    "ConverterHealth",
    "ConverterRegistry",
    "ConverterRegistryError",
    "build_converter_registry",
]

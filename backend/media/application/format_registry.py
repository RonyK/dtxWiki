"""Policy-neutral format catalog and validator dispatch.

The registry describes byte formats only.  Library conversion, Context render,
workspace policy, browser readiness, and runtime provider health are projected
outside this service instead of accumulating mutable booleans here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType

from backend.media.domain import FormatDescriptor, FormatFacts
from backend.media.ports.contracts import UnsupportedCapabilityError
from backend.media.ports.inspection import FormatValidator
from backend.media.ports.storage import ReadHandle


class FormatRegistryContractError(ValueError):
    """Raised when a registered validator violates its inward contract."""


def _version(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("registry_version must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > 80:
        raise ValueError("registry_version must be non-blank and bounded")
    return normalized


class FormatRegistry:
    def __init__(
        self,
        *,
        registry_version: str,
        descriptors: Iterable[FormatDescriptor],
        validators: Mapping[str, FormatValidator] | None = None,
    ) -> None:
        self._registry_version = _version(registry_version)
        ordered = tuple(descriptors)
        by_id: dict[str, FormatDescriptor] = {}
        extension_index: dict[str, list[str]] = {}
        media_type_index: dict[str, list[str]] = {}
        for descriptor in ordered:
            if not isinstance(descriptor, FormatDescriptor):
                raise ValueError("descriptors must contain FormatDescriptor values")
            if descriptor.format_id in by_id:
                raise ValueError(f"duplicate format_id: {descriptor.format_id}")
            by_id[descriptor.format_id] = descriptor
            for extension in descriptor.extensions:
                extension_index.setdefault(extension, []).append(descriptor.format_id)
            for media_type in descriptor.media_types:
                media_type_index.setdefault(media_type, []).append(
                    descriptor.format_id
                )

        configured = dict(validators or {})
        unknown = sorted(
            validator_id
            for validator_id in configured
            if validator_id
            not in {descriptor.validator_id for descriptor in ordered}
        )
        if unknown:
            raise ValueError(f"validators have no descriptor: {', '.join(unknown)}")

        self._ordered = ordered
        self._by_id = MappingProxyType(by_id)
        self._validators = MappingProxyType(configured)
        self._extension_index = MappingProxyType(
            {key: tuple(value) for key, value in extension_index.items()}
        )
        self._media_type_index = MappingProxyType(
            {key: tuple(value) for key, value in media_type_index.items()}
        )

    @property
    def registry_version(self) -> str:
        return self._registry_version

    @property
    def descriptors(self) -> tuple[FormatDescriptor, ...]:
        return self._ordered

    def descriptor(self, format_id: str) -> FormatDescriptor:
        normalized = format_id.strip().lower()
        try:
            return self._by_id[normalized]
        except KeyError as exc:
            raise KeyError(f"unknown format_id: {normalized}") from exc

    def candidates(
        self,
        *,
        extension: str | None = None,
        declared_media_type: str | None = None,
    ) -> tuple[FormatDescriptor, ...]:
        """Return hint candidates without declaring a canonical format.

        Conflicting extension and MIME hints intentionally produce the union.
        A bounded detector and exact validator must decide or reject the bytes.
        """

        candidate_ids: set[str] = set()
        if extension is not None:
            normalized_extension = extension.strip().lower().removeprefix(".")
            candidate_ids.update(
                self._extension_index.get(normalized_extension, ())
            )
        if declared_media_type is not None:
            normalized_media_type = declared_media_type.strip().lower()
            candidate_ids.update(
                self._media_type_index.get(normalized_media_type, ())
            )
        return tuple(
            descriptor
            for descriptor in self._ordered
            if descriptor.format_id in candidate_ids
        )

    def validate(
        self,
        format_id: str,
        content: ReadHandle,
        *,
        byte_budget: int,
    ) -> FormatFacts:
        if (
            isinstance(byte_budget, bool)
            or not isinstance(byte_budget, int)
            or byte_budget <= 0
        ):
            raise ValueError("byte_budget must be a positive integer")
        descriptor = self.descriptor(format_id)
        validator = self._validators.get(descriptor.validator_id)
        if validator is None:
            raise UnsupportedCapabilityError(
                f"format_validator:{descriptor.validator_id}"
            )
        facts = validator.validate(content, byte_budget=byte_budget)
        if not isinstance(facts, FormatFacts):
            raise FormatRegistryContractError(
                "validator returned a non-FormatFacts result"
            )
        if facts.format_id != descriptor.format_id:
            raise FormatRegistryContractError("validator changed format identity")
        if facts.validator_id != descriptor.validator_id:
            raise FormatRegistryContractError("validator identity mismatch")
        if facts.canonical_media_type not in descriptor.media_types:
            raise FormatRegistryContractError(
                "validator returned a media type outside the descriptor"
            )
        return facts

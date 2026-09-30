"""Server-owned Library format and runtime capability projection."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from backend.services.converter_registry import ConverterRegistry
from backend.services.library_formats import (
    SOURCE_PREVIEW_IMAGE_PROFILES,
    SOURCE_PREVIEW_MEDIA_PROFILES,
    FormatDescriptor,
    FormatRegistry,
)
from backend.services.library_policy import (
    LibraryFamilyPolicyService,
)
from backend.services.preview_provider_registry import (
    PreviewProviderHealth,
    PreviewProviderRegistry,
)
from backend.services.settings_service import (
    library_format_is_enabled,
    library_upload_limit_sources,
    normalized_library_policy,
)

MAX_LIBRARY_FILES_PER_BATCH = 20

class LibraryCapabilityService:
    def __init__(
        self,
        *,
        format_registry: FormatRegistry,
        converter_registry: ConverterRegistry,
        ingest_enabled: bool,
        conversion_enabled: bool,
        instance_hard_cap_bytes: int,
        preview_provider_registry: PreviewProviderRegistry | None = None,
        preview_ready_formats: set[str] | None = None,
        family_policy: LibraryFamilyPolicyService | None = None,
    ) -> None:
        self.format_registry = format_registry
        self.converter_registry = converter_registry
        self.ingest_enabled = ingest_enabled
        self.conversion_enabled = conversion_enabled
        self.instance_hard_cap_bytes = instance_hard_cap_bytes
        self.preview_provider_registry = preview_provider_registry
        self.family_policy = family_policy or LibraryFamilyPolicyService(
            format_registry=format_registry
        )
        # Explicit injection remains useful for compatibility tests. Production
        # derives generated Preview readiness from the provider registry below.
        self.preview_ready_formats = (
            frozenset(preview_ready_formats)
            if preview_ready_formats is not None
            else None
        )

    def project(self, policy: dict[str, Any] | None) -> dict[str, Any]:
        normalized = normalized_library_policy(
            policy,
            family_policy=self.family_policy,
        )
        converter_formats = self.converter_registry.ready_formats()
        preview_provider_health = (
            self.preview_provider_registry.catalog_health()
            if self.preview_provider_registry is not None
            else ()
        )
        allow_store = normalized["allow_store_without_converter"]
        custom_formats_by_id = {
            str(item["format_id"]): item for item in normalized["custom_formats"]
        }
        builtin_descriptor_overrides = (
            self.family_policy.builtin_descriptor_overrides(
                normalized["builtin_format_overrides"]
            )
        )
        custom_descriptors = self.family_policy.custom_descriptors(
            normalized["custom_formats"],
            builtin_format_overrides=normalized["builtin_format_overrides"],
        )
        package_descriptors = {
            descriptor.format_id: descriptor
            for descriptor in self.format_registry.descriptors()
        }
        formats: list[dict[str, Any]] = []
        for descriptor in (
            *self.format_registry.descriptors(
                builtin_descriptor_overrides=builtin_descriptor_overrides
            ),
            *custom_descriptors,
        ):
            policy_enabled = library_format_is_enabled(
                normalized,
                descriptor.format_id,
                family_policy=self.family_policy,
            )
            provider_chain = (
                self.converter_registry.select_chain(
                    descriptor.format_id,
                    ready_only=True,
                )
                if self.conversion_enabled
                else ()
            )
            provider_available = (
                descriptor.admission == "convert"
                and descriptor.format_id in converter_formats
                and bool(provider_chain)
            )
            validator_ready = descriptor.admission != "deny"
            deployment_available = self.ingest_enabled and validator_ready
            admission_state = (
                "denied"
                if not self.ingest_enabled
                else "ready"
                if validator_ready
                else "validator_unavailable"
            )
            upload_limit_sources = library_upload_limit_sources(
                normalized,
                format_id=descriptor.format_id,
                instance_hard_cap_bytes=self.instance_hard_cap_bytes,
                family_policy=self.family_policy,
            )
            max_upload_bytes = min(upload_limit_sources.values())
            upload_limiting_source = next(
                key
                for key, value in upload_limit_sources.items()
                if value == max_upload_bytes
            )
            converter_limit = (
                self.converter_registry.ready_input_limit(descriptor.format_id)
                if provider_available
                else None
            )
            max_convertible_bytes = (
                min(max_upload_bytes, converter_limit)
                if converter_limit is not None
                else (max_upload_bytes if provider_available else None)
            )
            upload = (
                deployment_available
                and policy_enabled
                and (provider_available or allow_store)
            )
            if not upload:
                legacy_conversion = "denied"
            elif provider_available:
                legacy_conversion = "available"
            else:
                legacy_conversion = "stored_unconverted"

            conversion_state = (
                "available"
                if provider_available
                else "stored_unconverted"
                if validator_ready and allow_store
                else "unavailable"
            )
            format_preview_providers = tuple(
                health
                for health in preview_provider_health
                if descriptor.format_id in health.formats
            )
            generated_preview_ready = any(
                health.ready for health in format_preview_providers
            )
            preview_ready = self._preview_ready(
                descriptor,
                provider_available=provider_available,
                generated_preview_ready=generated_preview_ready,
            )
            preview_state = (
                "ready"
                if preview_ready
                else "degraded"
                if format_preview_providers
                else "unavailable"
            )
            storage_state = (
                "denied"
                if not self.ingest_enabled
                else "available"
                if validator_ready
                else "available_after_validation"
            )
            reason = _capability_reason(
                ingest_enabled=self.ingest_enabled,
                validator_ready=validator_ready,
                policy_enabled=policy_enabled,
                provider_available=provider_available,
                allow_store=allow_store,
            )
            renderer_contract = _renderer_contract(descriptor)
            formats.append(
                {
                    "format_id": descriptor.format_id,
                    "display_name": str(
                        custom_formats_by_id.get(descriptor.format_id, {}).get(
                            "display_name",
                            _format_label(descriptor.format_id),
                        )
                    ),
                    "category": descriptor.category,
                    "extensions": list(descriptor.extensions),
                    "default_extensions": list(
                        package_descriptors.get(
                            descriptor.format_id,
                            descriptor,
                        ).extensions
                    ),
                    "extensions_overridden": (
                        descriptor.format_id in package_descriptors
                        and descriptor.extensions
                        != package_descriptors[descriptor.format_id].extensions
                    ),
                    "media_types": list(descriptor.media_types),
                    "search_aliases": _search_aliases(descriptor),
                    "provider_ids": [
                        f"core.detector.{descriptor.container_validator or 'text'}",
                        *(item.converter_id for item in provider_chain),
                        *(health.provider_id for health in format_preview_providers),
                    ],
                    "upload": upload,
                    "policy_enabled": policy_enabled,
                    "deployment_available": deployment_available,
                    "converter_available": provider_available,
                    "admission_state": admission_state,
                    "storage_state": storage_state,
                    "conversion_state": conversion_state,
                    "preview_state": preview_state,
                    "render_state": "available",
                    "reason": reason,
                    "conversion": legacy_conversion,
                    "preview_kind": descriptor.preview_kind,
                    "renderer_contract": renderer_contract,
                    # max_bytes remains for older clients; the named fields are
                    # the authoritative split contract.
                    "max_bytes": max_upload_bytes,
                    "max_upload_bytes": max_upload_bytes,
                    "max_convertible_bytes": max_convertible_bytes,
                    "upload_limit_sources": upload_limit_sources,
                    "upload_limiting_source": upload_limiting_source,
                }
            )

        catalog_revision = _policy_revision(
            _catalog_projection(
                self.format_registry.descriptors(),
                preview_provider_health=preview_provider_health,
                family_presets=self.family_policy.preset_payloads(),
            )
        )
        families = self._configured_family_projection(normalized, formats)
        summary = {
            "total": len(formats),
            "uploadable": sum(item["upload"] for item in formats),
            "preview_ready": sum(item["preview_state"] == "ready" for item in formats),
            "download_only": sum(
                item["upload"] and item["preview_state"] != "ready"
                for item in formats
            ),
            "unavailable": sum(
                item["policy_enabled"] and not item["upload"] for item in formats
            ),
        }
        payload = {
            "catalog_revision": catalog_revision,
            "policy_revision": _policy_revision(_upload_policy_projection(normalized)),
            "policy_schema_version": normalized["policy_schema_version"],
            "max_files_per_batch": MAX_LIBRARY_FILES_PER_BATCH,
            "family_presets": self.family_policy.preset_payloads(),
            "families": families,
            "summary": summary,
            "formats": formats,
        }
        return {**payload, "capability_revision": _policy_revision(payload)}

    def _preview_ready(
        self,
        descriptor: FormatDescriptor,
        *,
        provider_available: bool,
        generated_preview_ready: bool,
    ) -> bool:
        if self.preview_ready_formats is not None:
            return descriptor.format_id in self.preview_ready_formats
        if generated_preview_ready:
            return True
        if descriptor.format_id in {
            "pdf",
            *SOURCE_PREVIEW_IMAGE_PROFILES,
            *SOURCE_PREVIEW_MEDIA_PROFILES,
        } and descriptor.admission != "deny":
            return True
        return provider_available and descriptor.preview_kind in {"markdown", "table"}

    def _configured_family_projection(
        self,
        policy: dict[str, Any],
        formats: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        by_format = {str(item["format_id"]): item for item in formats}
        projection: list[dict[str, Any]] = []
        configured = self.family_policy.configured_family_payloads(
            policy["format_families"],
            custom_formats=policy["custom_formats"],
            builtin_format_overrides=policy["builtin_format_overrides"],
        )
        for family in configured:
            entries = [
                by_format[format_id]
                for format_id in family["format_ids"]
                if format_id in by_format
            ]
            configured_limit = family["max_upload_bytes"]
            preference_source = (
                "family" if isinstance(configured_limit, int) else "workspace"
            )
            preference = (
                configured_limit
                if isinstance(configured_limit, int)
                else policy["max_upload_bytes"]
            )
            sources = {
                preference_source: preference,
                "instance": self.instance_hard_cap_bytes,
            }
            effective_limit = min(sources.values())
            limiting_source = next(
                key for key, value in sources.items() if value == effective_limit
            )
            blockers = sorted(
                {
                    str(item["reason"])
                    for item in entries
                    if item["policy_enabled"] and not item["upload"] and item["reason"]
                }
            )
            projection.append(
                {
                    **family,
                    "uploadable_count": sum(item["upload"] for item in entries),
                    "preview_ready_count": sum(
                        item["preview_state"] == "ready" for item in entries
                    ),
                    "unavailable_count": sum(
                        item["policy_enabled"] and not item["upload"]
                        for item in entries
                    ),
                    "max_upload_bytes": configured_limit,
                    "effective_max_upload_bytes": effective_limit,
                    "upload_limiting_source": limiting_source,
                    "blockers": blockers,
                }
            )
        return projection


def _capability_reason(
    *,
    ingest_enabled: bool,
    validator_ready: bool,
    policy_enabled: bool,
    provider_available: bool,
    allow_store: bool,
) -> str | None:
    if not ingest_enabled:
        return "deployment_ingest_disabled"
    if not validator_ready:
        return "validator_unavailable"
    if not policy_enabled:
        return "policy_disabled"
    if not provider_available and not allow_store:
        return "converter_unavailable"
    if not provider_available:
        return "stored_without_converter"
    return None


def _renderer_contract(descriptor: FormatDescriptor) -> str:
    kind = (
        descriptor.category
        if descriptor.category in {"audio", "video"}
        else descriptor.preview_kind
    )
    return f"core.{kind}.v1"


def _format_label(format_id: str) -> str:
    return {
        "markdown": "Markdown",
        "text": "Text",
        "jpeg": "JPEG",
        "hdf5": "HDF5",
        "hwpml": "HWPML",
        "hwpx": "HWPX",
    }.get(format_id, format_id.upper())


def _search_aliases(descriptor: FormatDescriptor) -> list[str]:
    return list(
        dict.fromkeys(
            (
                descriptor.format_id,
                _format_label(descriptor.format_id),
                *(extension.removeprefix(".") for extension in descriptor.extensions),
                *descriptor.media_types,
                descriptor.category,
            )
        )
    )


def _catalog_projection(
    descriptors: tuple[FormatDescriptor, ...],
    *,
    preview_provider_health: tuple[PreviewProviderHealth, ...] = (),
    family_presets: list[dict[str, object]] | None = None,
) -> dict[str, Any]:
    return {
        "family_presets": family_presets or [],
        "preview_providers": [
            {
                "provider_id": health.provider_id,
                "provider_version": health.provider_version,
                "formats": health.formats,
                "renderer_contracts": health.renderer_contracts,
                "execution_mode": health.execution_mode,
                "distribution_digest": health.distribution_digest,
            }
            for health in preview_provider_health
        ],
        "formats": [
            {
                "format_id": descriptor.format_id,
                "category": descriptor.category,
                "extensions": descriptor.extensions,
                "media_types": descriptor.media_types,
                "admission": descriptor.admission,
                "validator": descriptor.container_validator,
                "renderer_contract": _renderer_contract(descriptor),
            }
            for descriptor in descriptors
        ],
    }


def _policy_revision(policy: dict[str, Any]) -> str:
    payload = json.dumps(
        policy,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _upload_policy_projection(policy: dict[str, Any]) -> dict[str, Any]:
    return {
        key: policy[key]
        for key in (
            "policy_schema_version",
            "builtin_format_overrides",
            "format_families",
            "custom_formats",
            "max_upload_bytes",
            "format_overrides",
            "allow_store_without_converter",
        )
    }


__all__ = ["MAX_LIBRARY_FILES_PER_BATCH", "LibraryCapabilityService"]

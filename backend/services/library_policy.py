"""Workspace Library format definitions and family-limit policy.

The immutable server registry remains authoritative for formats with semantic
capabilities.  A Workspace may additionally name bounded extension aliases for
opaque, download-only storage.  Those declarations never install executable
detector, converter, Preview, or renderer code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from functools import cached_property, lru_cache
from typing import Any

from backend.catalog.library_family_presets.repository import (
    LibraryFamilyPresetCatalogRepository,
    get_library_family_preset_catalog_repository,
)
from backend.services.library_formats import FormatDescriptor, FormatRegistry

LIBRARY_POLICY_SCHEMA_VERSION = 5
MAX_LIBRARY_FORMAT_FAMILIES = 6
MAX_LIBRARY_FAMILY_NAME_CHARS = 80
MAX_LIBRARY_CUSTOM_FORMATS = 64
MAX_LIBRARY_CUSTOM_FORMAT_EXTENSIONS = 16
MAX_LIBRARY_CUSTOM_FORMAT_NAME_CHARS = 80
MIN_LIBRARY_UPLOAD_BYTES = 1_000
MAX_LIBRARY_WORKSPACE_UPLOAD_BYTES = 50_000_000
# This matches the largest instance hard cap accepted by ``Settings``. Family
# and exact-format values are preferences, so they may exceed both the
# Workspace default and a particular deployment cap; runtime resolution still
# applies the instance cap as the absolute effective ceiling.
MAX_LIBRARY_FORMAT_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024

_IDENTIFIER_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_CUSTOM_FORMAT_ID_PATTERN = re.compile(r"custom-[a-z0-9][a-z0-9_-]{0,55}")
_EXTENSION_PATTERN = re.compile(r"\.[a-z0-9][a-z0-9_+-]{0,31}")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff\u2028\u2029]")
_FORMAT_ID_ALIASES = {"md": "markdown", "txt": "text"}
_UNSAFE_CUSTOM_EXTENSIONS = frozenset(
    {
        # Executables, installers, shortcuts, and operating-system launchers.
        ".app",
        ".apk",
        ".bat",
        ".cab",
        ".class",
        ".cmd",
        ".com",
        ".cpl",
        ".desktop",
        ".dll",
        ".dmg",
        ".exe",
        ".hta",
        ".ipa",
        ".iso",
        ".jar",
        ".lnk",
        ".msi",
        ".msix",
        ".reg",
        ".scr",
        ".url",
        ".war",
        # Script and active Web-document formats.
        ".bash",
        ".cjs",
        ".fish",
        ".htm",
        ".html",
        ".js",
        ".jse",
        ".mjs",
        ".php",
        ".pl",
        ".ps1",
        ".psm1",
        ".py",
        ".pyw",
        ".rb",
        ".sh",
        ".svg",
        ".vbe",
        ".vbs",
        ".wsf",
        ".wsh",
        ".xhtml",
        ".zsh",
        # Generic archive envelopes remain closed until a bounded validator
        # owns their extraction and anti-bomb contract.
        ".7z",
        ".ar",
        ".bz2",
        ".cpio",
        ".gz",
        ".lz4",
        ".rar",
        ".tar",
        ".tgz",
        ".xz",
        ".zip",
        ".zst",
        ".zstd",
        # Macro-enabled Office packages are active-content formats, not aliases
        # for the passive OOXML profiles.
        ".docm",
        ".dotm",
        ".potm",
        ".ppam",
        ".ppsm",
        ".pptm",
        ".xlam",
        ".xlsb",
        ".xlsm",
        ".xltm",
    }
)


class LibraryFamilyPolicyError(ValueError):
    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


@dataclass(frozen=True, slots=True)
class LibraryFamilyMatch:
    family_id: str
    name: str
    max_upload_bytes: int | None


class LibraryFamilyPolicyService:
    """Validate family snapshots against one server-owned format catalog."""

    def __init__(
        self,
        *,
        format_registry: FormatRegistry | None = None,
        preset_catalog: LibraryFamilyPresetCatalogRepository | None = None,
    ) -> None:
        self.format_registry = format_registry or FormatRegistry()
        self.preset_catalog = (
            preset_catalog or get_library_family_preset_catalog_repository()
        )

    @cached_property
    def _descriptors(self) -> dict[str, FormatDescriptor]:
        return {
            descriptor.format_id: descriptor
            for descriptor in self.format_registry.descriptors()
        }

    @cached_property
    def _formats_by_extension(self) -> dict[str, tuple[str, ...]]:
        formats: dict[str, list[str]] = {}
        for descriptor in self.format_registry.descriptors():
            for extension in descriptor.extensions:
                formats.setdefault(extension.casefold(), []).append(
                    descriptor.format_id
                )
        return {
            extension: tuple(dict.fromkeys(format_ids))
            for extension, format_ids in formats.items()
        }

    def coerce_builtin_format_overrides(
        self,
        value: Any,
    ) -> dict[str, dict[str, list[str]]]:
        """Return sparse, extension-only overrides for package formats.

        An override may change only the effective aliases. Detector identity,
        admission, media types, converters, Preview, and renderer contracts
        remain package-owned through ``dataclasses.replace`` below.
        """

        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise LibraryFamilyPolicyError(
                "library_builtin_format_override_payload_invalid",
                "Built-in format overrides must be an object.",
            )

        normalized: dict[str, dict[str, list[str]]] = {}
        for raw_format_id, raw_override in value.items():
            if not isinstance(raw_format_id, str):
                raise LibraryFamilyPolicyError(
                    "library_builtin_format_override_payload_invalid",
                    "Built-in format override keys must be format identifiers.",
                )
            candidate = raw_format_id.strip().casefold()
            format_id = _FORMAT_ID_ALIASES.get(candidate, candidate)
            descriptor = self._descriptors.get(format_id)
            if descriptor is None or format_id in normalized:
                raise LibraryFamilyPolicyError(
                    "library_builtin_format_override_payload_invalid",
                    f"Built-in format override '{raw_format_id}' is invalid.",
                )
            if not isinstance(raw_override, dict) or set(raw_override) != {
                "extensions"
            }:
                raise LibraryFamilyPolicyError(
                    "library_builtin_format_override_payload_invalid",
                    f"Built-in format override '{format_id}' must contain extensions.",
                )
            raw_extensions = raw_override.get("extensions")
            if (
                not isinstance(raw_extensions, list)
                or not 1 <= len(raw_extensions) <= MAX_LIBRARY_CUSTOM_FORMAT_EXTENSIONS
            ):
                raise LibraryFamilyPolicyError(
                    "library_builtin_format_override_payload_invalid",
                    f"Built-in format override '{format_id}' has invalid extensions.",
                )
            extensions: list[str] = []
            for raw_extension in raw_extensions:
                extension = _coerce_builtin_extension(raw_extension)
                if extension in extensions:
                    raise LibraryFamilyPolicyError(
                        "library_builtin_format_override_payload_invalid",
                        f"Built-in format override '{format_id}' repeats an extension.",
                    )
                extensions.append(extension)
            if set(extensions) != set(descriptor.extensions):
                normalized[format_id] = {"extensions": extensions}

        effective_owners: dict[str, set[str]] = {}
        for descriptor in self.format_registry.descriptors():
            effective_extensions = normalized.get(descriptor.format_id, {}).get(
                "extensions",
                descriptor.extensions,
            )
            for extension in effective_extensions:
                effective_owners.setdefault(extension, set()).add(descriptor.format_id)
        for extension, owners in effective_owners.items():
            if len(owners) <= 1:
                continue
            package_owners = set(self._formats_by_extension.get(extension, ()))
            if not owners.issubset(package_owners):
                raise LibraryFamilyPolicyError(
                    "library_format_extension_conflict",
                    f"Extension '{extension}' belongs to more than one format.",
                )
        return normalized

    def builtin_descriptor_overrides(
        self,
        value: Any,
    ) -> tuple[FormatDescriptor, ...]:
        normalized = self.coerce_builtin_format_overrides(value)
        return tuple(
            replace(
                descriptor,
                extensions=tuple(normalized[descriptor.format_id]["extensions"]),
            )
            for descriptor in self.format_registry.descriptors()
            if descriptor.format_id in normalized
        )

    def effective_builtin_descriptors(
        self,
        value: Any,
    ) -> tuple[FormatDescriptor, ...]:
        replacements = {
            descriptor.format_id: descriptor
            for descriptor in self.builtin_descriptor_overrides(value)
        }
        return tuple(
            replacements.get(descriptor.format_id, descriptor)
            for descriptor in self.format_registry.descriptors()
        )

    def _effective_formats_by_extension(
        self,
        builtin_format_overrides: Any,
    ) -> dict[str, tuple[str, ...]]:
        formats: dict[str, list[str]] = {}
        for descriptor in self.effective_builtin_descriptors(
            builtin_format_overrides
        ):
            for extension in descriptor.extensions:
                formats.setdefault(extension.casefold(), []).append(
                    descriptor.format_id
                )
        return {
            extension: tuple(dict.fromkeys(format_ids))
            for extension, format_ids in formats.items()
        }

    def extension_owners(
        self,
        *,
        builtin_format_overrides: Any = None,
        custom_formats: Any = None,
    ) -> dict[str, frozenset[str]]:
        """Return effective alias owners for stable-transfer validation."""

        normalized_builtin = self.coerce_builtin_format_overrides(
            builtin_format_overrides
        )
        normalized_custom = self.coerce_custom_formats(
            custom_formats,
            builtin_format_overrides=normalized_builtin,
        )
        owners: dict[str, set[str]] = {}
        for descriptor in self.effective_builtin_descriptors(normalized_builtin):
            for extension in descriptor.extensions:
                owners.setdefault(extension, set()).add(descriptor.format_id)
        for custom_format in normalized_custom:
            for extension in custom_format["extensions"]:
                owners.setdefault(str(extension), set()).add(
                    str(custom_format["format_id"])
                )
        return {
            extension: frozenset(format_ids)
            for extension, format_ids in owners.items()
        }

    def coerce_custom_formats(
        self,
        value: Any,
        *,
        builtin_format_overrides: Any = None,
    ) -> list[dict[str, object]]:
        if value is None:
            value = []
        if not isinstance(value, list):
            raise LibraryFamilyPolicyError(
                "library_custom_format_payload_invalid",
                "Custom formats must be a list.",
            )
        if len(value) > MAX_LIBRARY_CUSTOM_FORMATS:
            raise LibraryFamilyPolicyError(
                "library_custom_format_count_exceeded",
                f"At most {MAX_LIBRARY_CUSTOM_FORMATS} custom formats are allowed.",
            )

        formats_by_extension = self._effective_formats_by_extension(
            builtin_format_overrides
        )
        built_in_extensions = set(formats_by_extension)
        format_ids: set[str] = set()
        display_names: set[str] = set()
        extension_owners: dict[str, str] = {}
        normalized: list[dict[str, object]] = []
        for index, raw_format in enumerate(value, start=1):
            custom_format = self._coerce_custom_format(raw_format, index=index)
            format_id = str(custom_format["format_id"])
            display_name = str(custom_format["display_name"])
            if format_id in format_ids or format_id in self._descriptors:
                raise LibraryFamilyPolicyError(
                    "library_custom_format_id_conflict",
                    f"Custom format id '{format_id}' is already in use.",
                )
            normalized_name = display_name.casefold()
            if normalized_name in display_names:
                raise LibraryFamilyPolicyError(
                    "library_custom_format_name_conflict",
                    f"Custom format name '{display_name}' is already in use.",
                )
            for extension in custom_format["extensions"]:
                canonical = str(extension)
                if canonical in built_in_extensions:
                    owner = ", ".join(formats_by_extension[canonical])
                    raise LibraryFamilyPolicyError(
                        "library_custom_format_extension_conflict",
                        f"Extension '{canonical}' is owned by server format '{owner}'.",
                    )
                owner = extension_owners.get(canonical)
                if owner is not None:
                    raise LibraryFamilyPolicyError(
                        "library_custom_format_extension_conflict",
                        f"Extension '{canonical}' is already owned by '{owner}'.",
                    )
                extension_owners[canonical] = format_id
            format_ids.add(format_id)
            display_names.add(normalized_name)
            normalized.append(custom_format)
        return normalized

    def coerce_custom_formats_for_update(
        self,
        value: Any,
        *,
        existing_custom_formats: Any,
        builtin_format_overrides: Any = None,
        existing_builtin_format_overrides: Any = None,
    ) -> list[dict[str, object]]:
        """Validate a list replacement without treating an id edit as a rename.

        Deleting an id and later creating a genuinely new format is allowed,
        but one atomic update cannot transfer an existing name or extension to
        a different id. This makes accidental client-side id regeneration fail
        explicitly rather than changing the meaning of policy references.
        """

        existing = self.coerce_custom_formats(
            existing_custom_formats,
            builtin_format_overrides=existing_builtin_format_overrides,
        )
        normalized = self.coerce_custom_formats(
            value,
            builtin_format_overrides=builtin_format_overrides,
        )
        existing_ids = {str(item["format_id"]) for item in existing}
        existing_name_owners = {
            str(item["display_name"]).casefold(): str(item["format_id"])
            for item in existing
        }
        existing_extension_owners = {
            str(extension): str(item["format_id"])
            for item in existing
            for extension in item["extensions"]
        }
        for item in normalized:
            format_id = str(item["format_id"])
            if format_id in existing_ids:
                continue
            prior_id = existing_name_owners.get(str(item["display_name"]).casefold())
            if prior_id is None:
                prior_id = next(
                    (
                        existing_extension_owners[str(extension)]
                        for extension in item["extensions"]
                        if str(extension) in existing_extension_owners
                    ),
                    None,
                )
            if prior_id is not None and prior_id != format_id:
                raise LibraryFamilyPolicyError(
                    "library_custom_format_id_immutable",
                    f"Custom format id '{prior_id}' cannot be renamed to "
                    f"'{format_id}'.",
                )
        return normalized

    def custom_descriptors(
        self,
        value: Any,
        *,
        builtin_format_overrides: Any = None,
    ) -> tuple[FormatDescriptor, ...]:
        return tuple(
            FormatDescriptor(
                format_id=str(item["format_id"]),
                extensions=tuple(str(ext) for ext in item["extensions"]),
                media_types=("application/octet-stream",),
                category="data",
                admission="store_only",
                preview_kind="metadata",
                default_limits_profile="custom_store_only",
                container_validator="workspace-extension-store-only",
            )
            for item in self.coerce_custom_formats(
                value,
                builtin_format_overrides=builtin_format_overrides,
            )
        )

    def coerce_families(
        self,
        value: Any,
        *,
        custom_formats: Any = None,
        builtin_format_overrides: Any = None,
    ) -> list[dict[str, object]]:
        """Normalize persisted family snapshots without consulting presets.

        A preset is copied into Workspace policy at selection time.  Once the
        copied ``format_ids`` are present, the package catalog is provenance
        metadata only: a later catalog revision must not make that snapshot
        unreadable or change its effective upload limit.
        """

        normalized_builtin = self.coerce_builtin_format_overrides(
            builtin_format_overrides
        )
        normalized_custom = self.coerce_custom_formats(
            custom_formats,
            builtin_format_overrides=normalized_builtin,
        )
        return self._coerce_families(
            value,
            existing_by_id=None,
            custom_formats=normalized_custom,
            builtin_format_overrides=normalized_builtin,
        )

    def coerce_families_for_update(
        self,
        value: Any,
        *,
        existing_families: Any,
        custom_formats: Any = None,
        builtin_format_overrides: Any = None,
    ) -> list[dict[str, object]]:
        """Normalize a Settings write and validate new preset selections.

        Existing preset-backed families retain their copied snapshot without a
        live catalog dependency.  A new family, or a family switched to a
        different preset/revision, is resolved against the current catalog.
        """

        normalized_builtin = self.coerce_builtin_format_overrides(
            builtin_format_overrides
        )
        normalized_custom = self.coerce_custom_formats(
            custom_formats,
            builtin_format_overrides=normalized_builtin,
        )
        # ``custom_formats`` is intentionally the final replacement catalog.
        # A valid CAS request may remove a custom format and every reference to
        # it together, so validating the old family snapshots against the new
        # catalog here would make correctness depend on per-key batch order.
        # Stored policy is normalized before this write path; use it only for
        # immutable preset-snapshot comparison and validate the replacement
        # below against the final catalog.
        existing = (
            [item for item in existing_families if isinstance(item, dict)]
            if isinstance(existing_families, list)
            else []
        )
        return self._coerce_families(
            value,
            existing_by_id={
                str(family["family_id"]): family
                for family in existing
                if isinstance(family.get("family_id"), str)
            },
            custom_formats=normalized_custom,
            builtin_format_overrides=normalized_builtin,
        )

    def _coerce_families(
        self,
        value: Any,
        *,
        existing_by_id: dict[str, dict[str, object]] | None,
        custom_formats: list[dict[str, object]],
        builtin_format_overrides: dict[str, dict[str, list[str]]],
    ) -> list[dict[str, object]]:
        if not isinstance(value, list):
            raise LibraryFamilyPolicyError(
                "library_family_payload_invalid",
                "Format families must be a list.",
            )
        if len(value) > MAX_LIBRARY_FORMAT_FAMILIES:
            raise LibraryFamilyPolicyError(
                "library_family_count_exceeded",
                f"At most {MAX_LIBRARY_FORMAT_FAMILIES} format families are allowed.",
            )

        normalized: list[dict[str, object]] = []
        family_ids: set[str] = set()
        family_names: set[str] = set()
        format_owners: dict[str, str] = {}
        for index, raw_family in enumerate(value, start=1):
            family = self._coerce_family(
                raw_family,
                index=index,
                existing_by_id=existing_by_id,
                custom_formats=custom_formats,
                builtin_format_overrides=builtin_format_overrides,
            )
            family_id = str(family["family_id"])
            family_name = str(family["name"])
            normalized_name = family_name.casefold()
            if family_id in family_ids:
                raise LibraryFamilyPolicyError(
                    "library_family_id_conflict",
                    f"Format family id '{family_id}' is duplicated.",
                )
            if normalized_name in family_names:
                raise LibraryFamilyPolicyError(
                    "library_family_name_conflict",
                    f"Format family name '{family_name}' is duplicated.",
                )
            family_ids.add(family_id)
            family_names.add(normalized_name)

            for format_id in family["format_ids"]:
                owner = format_owners.get(str(format_id))
                if owner is not None:
                    raise LibraryFamilyPolicyError(
                        "library_family_format_conflict",
                        f"Format '{format_id}' already belongs to family '{owner}'.",
                    )
                format_owners[str(format_id)] = family_id
            normalized.append(family)
        return normalized

    def family_for_format(
        self,
        families: Any,
        format_id: str,
        *,
        custom_formats: Any = None,
        builtin_format_overrides: Any = None,
    ) -> LibraryFamilyMatch | None:
        normalized_custom = self.coerce_custom_formats(
            custom_formats,
            builtin_format_overrides=builtin_format_overrides,
        )
        canonical = self.canonical_format_id(
            format_id,
            custom_formats=normalized_custom,
            builtin_format_overrides=builtin_format_overrides,
        )
        for family in self.coerce_families(
            families,
            custom_formats=normalized_custom,
            builtin_format_overrides=builtin_format_overrides,
        ):
            if canonical in family["format_ids"]:
                limit = family["max_upload_bytes"]
                return LibraryFamilyMatch(
                    family_id=str(family["family_id"]),
                    name=str(family["name"]),
                    max_upload_bytes=limit if isinstance(limit, int) else None,
                )
        return None

    def configured_family_payloads(
        self,
        families: Any,
        *,
        custom_formats: Any = None,
        builtin_format_overrides: Any = None,
    ) -> list[dict[str, object]]:
        normalized_custom = self.coerce_custom_formats(
            custom_formats,
            builtin_format_overrides=builtin_format_overrides,
        )
        return [
            self._family_payload(
                item,
                custom_formats=normalized_custom,
                builtin_format_overrides=builtin_format_overrides,
            )
            for item in self.coerce_families(
                families,
                custom_formats=normalized_custom,
                builtin_format_overrides=builtin_format_overrides,
            )
        ]

    def preset_payloads(self) -> list[dict[str, object]]:
        return [
            {
                "preset_id": preset.preset_id,
                "revision": preset.revision,
                "name": preset.name,
                "description": preset.description,
                "format_ids": list(preset.format_ids),
                "extensions": self.extensions_for_formats(preset.format_ids),
                "format_extensions": {
                    format_id: list(self._descriptors[format_id].extensions)
                    for format_id in preset.format_ids
                },
            }
            for preset in self.preset_catalog.snapshot().presets
        ]

    def extensions_for_formats(
        self,
        format_ids,
        *,
        custom_formats: Any = None,
        builtin_format_overrides: Any = None,
    ) -> list[str]:
        custom_by_id = {
            str(item["format_id"]): item
            for item in self.coerce_custom_formats(
                custom_formats,
                builtin_format_overrides=builtin_format_overrides,
            )
        }
        effective_by_id = {
            descriptor.format_id: descriptor
            for descriptor in self.effective_builtin_descriptors(
                builtin_format_overrides
            )
        }
        extensions: list[str] = []
        for format_id in format_ids:
            descriptor = effective_by_id.get(str(format_id))
            source_extensions = (
                descriptor.extensions
                if descriptor is not None
                else custom_by_id.get(str(format_id), {}).get("extensions", ())
            )
            for extension in source_extensions:
                canonical = extension.casefold()
                if canonical not in extensions:
                    extensions.append(canonical)
        return extensions

    def canonical_format_id(
        self,
        value: Any,
        *,
        custom_formats: Any = None,
        builtin_format_overrides: Any = None,
    ) -> str:
        if not isinstance(value, str):
            raise LibraryFamilyPolicyError(
                "library_family_format_unknown",
                "A format family contains an invalid format identifier.",
            )
        candidate = value.strip().casefold()
        normalized = _FORMAT_ID_ALIASES.get(candidate, candidate)
        custom_ids = {
            str(item["format_id"])
            for item in self.coerce_custom_formats(
                custom_formats,
                builtin_format_overrides=builtin_format_overrides,
            )
        }
        if normalized not in self._descriptors and normalized not in custom_ids:
            raise LibraryFamilyPolicyError(
                "library_family_format_unknown",
                f"Format '{value}' is not in the server format catalog.",
            )
        return normalized

    def resolve_extension(
        self,
        value: Any,
        *,
        custom_formats: Any = None,
        builtin_format_overrides: Any = None,
    ) -> str:
        if not isinstance(value, str):
            raise LibraryFamilyPolicyError(
                "library_family_extension_invalid",
                "Format-family extensions must be strings.",
            )
        normalized = value.strip().casefold()
        if normalized and not normalized.startswith("."):
            normalized = f".{normalized}"
        if not _EXTENSION_PATTERN.fullmatch(normalized):
            raise LibraryFamilyPolicyError(
                "library_family_extension_invalid",
                f"Extension '{value}' is invalid.",
            )
        normalized_custom = self.coerce_custom_formats(
            custom_formats,
            builtin_format_overrides=builtin_format_overrides,
        )
        matches = list(
            self._effective_formats_by_extension(builtin_format_overrides).get(
                normalized,
                (),
            )
        )
        matches.extend(
            str(item["format_id"])
            for item in normalized_custom
            if normalized in item["extensions"]
        )
        if not matches:
            raise LibraryFamilyPolicyError(
                "library_family_extension_unsupported",
                f"Extension '{normalized}' is not in the server format catalog.",
            )
        if len(matches) != 1:
            raise LibraryFamilyPolicyError(
                "library_family_extension_ambiguous",
                f"Extension '{normalized}' maps to more than one server format.",
            )
        return matches[0]

    def _coerce_family(
        self,
        value: Any,
        *,
        index: int,
        existing_by_id: dict[str, dict[str, object]] | None,
        custom_formats: list[dict[str, object]],
        builtin_format_overrides: dict[str, dict[str, list[str]]],
    ) -> dict[str, object]:
        if not isinstance(value, dict):
            raise LibraryFamilyPolicyError(
                "library_family_payload_invalid",
                f"Format family {index} must be an object.",
            )
        allowed = {
            "family_id",
            "name",
            "origin",
            "format_ids",
            "extensions",
            "max_upload_bytes",
            "preset_id",
            "preset_revision",
            "preset_baseline",
        }
        unknown = set(value) - allowed
        if unknown:
            raise LibraryFamilyPolicyError(
                "library_family_payload_invalid",
                f"Format family {index} contains unsupported fields.",
            )
        family_id = _coerce_identifier(value.get("family_id"), field="family_id")
        name = _coerce_family_name(value.get("name"), index=index)
        limit = _coerce_family_limit(value.get("max_upload_bytes"))
        origin = value.get("origin", "custom")
        if origin not in {"custom", "preset"}:
            raise LibraryFamilyPolicyError(
                "library_family_payload_invalid",
                "Format-family origin must be custom or preset.",
            )
        preset_id = value.get("preset_id")
        preset_revision = value.get("preset_revision")

        normalized_preset_id: str | None = None
        if preset_id is not None or preset_revision is not None:
            if (
                not isinstance(preset_id, str)
                or isinstance(preset_revision, bool)
                or not isinstance(preset_revision, int)
                or preset_revision < 1
            ):
                raise LibraryFamilyPolicyError(
                    "library_family_preset_invalid",
                    "Preset id and revision must be supplied together.",
                )
            normalized_preset_id = _coerce_identifier(preset_id, field="preset_id")

        raw_format_ids = value.get("format_ids")
        raw_extensions = value.get("extensions")
        if raw_format_ids is not None and raw_extensions is not None:
            raise LibraryFamilyPolicyError(
                "library_family_payload_invalid",
                "Supply format_ids or extensions for a family, not both.",
            )
        if raw_extensions is not None:
            if not isinstance(raw_extensions, list):
                raise LibraryFamilyPolicyError(
                    "library_family_payload_invalid",
                    "Format-family extensions must be a list.",
                )
            format_ids = [
                self.resolve_extension(
                    item,
                    custom_formats=custom_formats,
                    builtin_format_overrides=builtin_format_overrides,
                )
                for item in raw_extensions
            ]
        elif raw_format_ids is not None:
            if not isinstance(raw_format_ids, list):
                raise LibraryFamilyPolicyError(
                    "library_family_payload_invalid",
                    "Format-family format_ids must be a list.",
                )
            format_ids = [
                self.canonical_format_id(
                    item,
                    custom_formats=custom_formats,
                    builtin_format_overrides=builtin_format_overrides,
                )
                for item in raw_format_ids
            ]
        else:
            format_ids = []

        preset_baseline = None
        raw_preset_baseline = value.get("preset_baseline")
        if raw_preset_baseline is not None:
            preset_baseline = self._coerce_preset_baseline(raw_preset_baseline)
            if (
                normalized_preset_id is None
                or preset_baseline["preset_id"] != normalized_preset_id
                or preset_baseline["preset_revision"] != preset_revision
            ):
                raise LibraryFamilyPolicyError(
                    "library_family_preset_baseline_conflict",
                    "Preset baseline identity must match the format family.",
                )

        retain_preset = False
        if normalized_preset_id is not None:
            existing = (
                existing_by_id.get(family_id)
                if existing_by_id is not None
                else None
            )
            existing_matches = bool(
                existing is not None
                and existing.get("origin") == "preset"
                and existing.get("preset_id") == normalized_preset_id
                and existing.get("preset_revision") == preset_revision
            )

            if existing_by_id is not None:
                authoritative_baseline = None
                if existing_matches and existing is not None:
                    raw_existing_baseline = existing.get("preset_baseline")
                    if raw_existing_baseline is not None:
                        authoritative_baseline = self._coerce_preset_baseline(
                            raw_existing_baseline
                        )
                    elif existing.get("format_ids"):
                        # The v5 migration supplies this field. Keep a bounded
                        # fallback for a pre-v5 snapshot passed directly to the
                        # service, including after its package preset retires.
                        authoritative_baseline = self._preset_baseline(
                            preset_id=normalized_preset_id,
                            preset_revision=int(preset_revision),
                            format_ids=existing["format_ids"],
                        )
                if authoritative_baseline is None:
                    preset = self.preset_catalog.find(
                        normalized_preset_id,
                        revision=preset_revision,
                    )
                    if preset is None:
                        current = self.preset_catalog.find(normalized_preset_id)
                        code = (
                            "library_family_preset_not_found"
                            if current is None
                            else "library_family_preset_revision_conflict"
                        )
                        raise LibraryFamilyPolicyError(
                            code,
                            "The selected format-family preset is no longer "
                            "available at that revision.",
                        )
                    authoritative_baseline = self._preset_baseline(
                        preset_id=preset.preset_id,
                        preset_revision=preset.revision,
                        format_ids=preset.format_ids,
                    )
                if (
                    preset_baseline is not None
                    and preset_baseline != authoritative_baseline
                ):
                    raise LibraryFamilyPolicyError(
                        "library_family_preset_baseline_conflict",
                        "Preset baseline does not match the authoritative snapshot.",
                    )
                preset_baseline = authoritative_baseline
                if not format_ids:
                    format_ids = list(preset_baseline["format_ids"])
                retain_preset = True
            elif preset_baseline is not None:
                # Read normalization accepts the immutable snapshot already in
                # Workspace policy without consulting a mutable live catalog.
                if not format_ids:
                    format_ids = list(preset_baseline["format_ids"])
                retain_preset = True
            elif origin == "preset" and format_ids:
                # Persisted pre-v5 snapshots remain readable even if their
                # package preset has since been retired. The consolidated
                # migration normally supplies this baseline explicitly.
                preset_baseline = self._preset_baseline(
                    preset_id=normalized_preset_id,
                    preset_revision=int(preset_revision),
                    format_ids=format_ids,
                )
                retain_preset = True
            else:
                preset = self.preset_catalog.find(
                    normalized_preset_id,
                    revision=preset_revision,
                )
                if preset is not None:
                    if not format_ids:
                        format_ids = list(preset.format_ids)
                    preset_baseline = self._preset_baseline(
                        preset_id=preset.preset_id,
                        preset_revision=preset.revision,
                        format_ids=preset.format_ids,
                    )
                    retain_preset = True
                else:
                    current = self.preset_catalog.find(normalized_preset_id)
                    code = (
                        "library_family_preset_not_found"
                        if current is None
                        else "library_family_preset_revision_conflict"
                    )
                    raise LibraryFamilyPolicyError(
                        code,
                        "The selected format-family preset is no longer available at "
                        "that revision.",
                    )
        elif preset_baseline is not None:
            raise LibraryFamilyPolicyError(
                "library_family_preset_invalid",
                "A custom format family cannot contain a preset baseline.",
            )

        if not format_ids:
            raise LibraryFamilyPolicyError(
                "library_family_empty",
                "A format family must contain at least one format.",
            )
        if len(format_ids) != len(set(format_ids)):
            raise LibraryFamilyPolicyError(
                "library_family_format_duplicate",
                "A format family contains the same canonical format more than once.",
            )
        result: dict[str, object] = {
            "family_id": family_id,
            "name": name,
            "origin": "custom",
            "format_ids": format_ids,
            "max_upload_bytes": limit,
        }
        if (
            normalized_preset_id is not None
            and preset_baseline is not None
            and retain_preset
        ):
            result["origin"] = "preset"
            result["preset_id"] = normalized_preset_id
            result["preset_revision"] = preset_revision
            result["preset_baseline"] = preset_baseline
        return result

    def _preset_baseline(
        self,
        *,
        preset_id: str,
        preset_revision: int,
        format_ids,
    ) -> dict[str, object]:
        normalized_ids = [
            self.canonical_format_id(format_id) for format_id in format_ids
        ]
        return {
            "preset_id": preset_id,
            "preset_revision": preset_revision,
            "format_ids": normalized_ids,
            "format_extensions": {
                format_id: list(self._descriptors[format_id].extensions)
                for format_id in normalized_ids
            },
        }

    def _coerce_preset_baseline(self, value: Any) -> dict[str, object]:
        if not isinstance(value, dict) or set(value) != {
            "preset_id",
            "preset_revision",
            "format_ids",
            "format_extensions",
        }:
            raise LibraryFamilyPolicyError(
                "library_family_preset_invalid",
                "Preset baseline must contain identity, formats, and aliases.",
            )
        preset_id = _coerce_identifier(value.get("preset_id"), field="preset_id")
        preset_revision = value.get("preset_revision")
        raw_format_ids = value.get("format_ids")
        raw_format_extensions = value.get("format_extensions")
        if (
            isinstance(preset_revision, bool)
            or not isinstance(preset_revision, int)
            or preset_revision < 1
            or not isinstance(raw_format_ids, list)
            or not raw_format_ids
            or not isinstance(raw_format_extensions, dict)
        ):
            raise LibraryFamilyPolicyError(
                "library_family_preset_invalid",
                "Preset baseline fields are invalid.",
            )
        format_ids = [self.canonical_format_id(item) for item in raw_format_ids]
        if len(format_ids) != len(set(format_ids)) or set(raw_format_extensions) != set(
            format_ids
        ):
            raise LibraryFamilyPolicyError(
                "library_family_preset_invalid",
                "Preset baseline formats and aliases must match exactly.",
            )
        format_extensions: dict[str, list[str]] = {}
        for format_id in format_ids:
            raw_extensions = raw_format_extensions.get(format_id)
            if (
                not isinstance(raw_extensions, list)
                or not 1 <= len(raw_extensions) <= MAX_LIBRARY_CUSTOM_FORMAT_EXTENSIONS
            ):
                raise LibraryFamilyPolicyError(
                    "library_family_preset_invalid",
                    "Preset baseline aliases are invalid.",
                )
            extensions: list[str] = []
            for raw_extension in raw_extensions:
                extension = _coerce_extension(raw_extension)
                if extension in extensions:
                    raise LibraryFamilyPolicyError(
                        "library_family_preset_invalid",
                        "Preset baseline aliases must be unique.",
                    )
                extensions.append(extension)
            format_extensions[format_id] = extensions
        return {
            "preset_id": preset_id,
            "preset_revision": preset_revision,
            "format_ids": format_ids,
            "format_extensions": format_extensions,
        }

    def _family_payload(
        self,
        family: dict[str, object],
        *,
        custom_formats: Any = None,
        builtin_format_overrides: Any = None,
    ) -> dict[str, object]:
        return {
            **family,
            "format_ids": list(family["format_ids"]),
            "extensions": self.extensions_for_formats(
                family["format_ids"],
                custom_formats=custom_formats,
                builtin_format_overrides=builtin_format_overrides,
            ),
        }

    def _coerce_custom_format(
        self,
        value: Any,
        *,
        index: int,
    ) -> dict[str, object]:
        if not isinstance(value, dict) or set(value) != {
            "format_id",
            "display_name",
            "extensions",
        }:
            raise LibraryFamilyPolicyError(
                "library_custom_format_payload_invalid",
                f"Custom format {index} must contain format_id, display_name, "
                "and extensions.",
            )
        raw_format_id = value.get("format_id")
        if not isinstance(raw_format_id, str):
            raise LibraryFamilyPolicyError(
                "library_custom_format_payload_invalid",
                f"Custom format {index} requires a format_id.",
            )
        format_id = raw_format_id.strip().casefold()
        if not _CUSTOM_FORMAT_ID_PATTERN.fullmatch(format_id):
            raise LibraryFamilyPolicyError(
                "library_custom_format_payload_invalid",
                "Custom format ids must start with 'custom-' and use lowercase "
                "letters, digits, underscores, or hyphens.",
            )
        display_name = _coerce_custom_format_name(
            value.get("display_name"),
            index=index,
        )
        raw_extensions = value.get("extensions")
        if not isinstance(raw_extensions, list):
            raise LibraryFamilyPolicyError(
                "library_custom_format_payload_invalid",
                f"Custom format {index} extensions must be a list.",
            )
        if not 1 <= len(raw_extensions) <= MAX_LIBRARY_CUSTOM_FORMAT_EXTENSIONS:
            raise LibraryFamilyPolicyError(
                "library_custom_format_payload_invalid",
                "A custom format must contain between 1 and "
                f"{MAX_LIBRARY_CUSTOM_FORMAT_EXTENSIONS} extensions.",
            )
        extensions = [_coerce_custom_extension(item) for item in raw_extensions]
        if len(extensions) != len(set(extensions)):
            raise LibraryFamilyPolicyError(
                "library_custom_format_extension_conflict",
                "A custom format contains the same extension more than once.",
            )
        return {
            "format_id": format_id,
            "display_name": display_name,
            "extensions": extensions,
        }


def _coerce_identifier(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise LibraryFamilyPolicyError(
            "library_family_payload_invalid",
            f"{field} must be a string.",
        )
    normalized = value.strip().casefold()
    if not _IDENTIFIER_PATTERN.fullmatch(normalized):
        raise LibraryFamilyPolicyError(
            "library_family_payload_invalid",
            f"{field} is invalid.",
        )
    return normalized


def _coerce_family_name(value: Any, *, index: int) -> str:
    if not isinstance(value, str):
        raise LibraryFamilyPolicyError(
            "library_family_payload_invalid",
            f"Format family {index} requires a name.",
        )
    if _CONTROL_CHARACTERS.search(value):
        raise LibraryFamilyPolicyError(
            "library_family_name_invalid",
            "Format-family names cannot contain control characters.",
        )
    normalized = " ".join(value.split())
    if (
        not normalized
        or len(normalized) > MAX_LIBRARY_FAMILY_NAME_CHARS
    ):
        raise LibraryFamilyPolicyError(
            "library_family_name_invalid",
            "Format-family names must contain 1 to "
            f"{MAX_LIBRARY_FAMILY_NAME_CHARS} characters.",
        )
    return normalized


def _coerce_custom_format_name(value: Any, *, index: int) -> str:
    if not isinstance(value, str):
        raise LibraryFamilyPolicyError(
            "library_custom_format_name_invalid",
            f"Custom format {index} requires a display name.",
        )
    if _CONTROL_CHARACTERS.search(value):
        raise LibraryFamilyPolicyError(
            "library_custom_format_name_invalid",
            "Custom format names cannot contain control characters.",
        )
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > MAX_LIBRARY_CUSTOM_FORMAT_NAME_CHARS:
        raise LibraryFamilyPolicyError(
            "library_custom_format_name_invalid",
            "Custom format names must contain 1 to "
            f"{MAX_LIBRARY_CUSTOM_FORMAT_NAME_CHARS} characters.",
        )
    return normalized


def _coerce_extension(value: Any) -> str:
    if not isinstance(value, str):
        raise LibraryFamilyPolicyError(
            "library_format_extension_invalid",
            "Format extensions must be strings.",
        )
    normalized = value.strip().casefold()
    if normalized and not normalized.startswith("."):
        normalized = f".{normalized}"
    if not _EXTENSION_PATTERN.fullmatch(normalized):
        raise LibraryFamilyPolicyError(
            "library_format_extension_invalid",
            f"Extension '{value}' is invalid.",
        )
    return normalized


def _coerce_custom_extension(value: Any) -> str:
    try:
        normalized = _coerce_extension(value)
    except LibraryFamilyPolicyError as exc:
        raise LibraryFamilyPolicyError(
            "library_custom_format_extension_invalid",
            exc.safe_message.replace("Format extensions", "Custom format extensions"),
        ) from exc
    if normalized in _UNSAFE_CUSTOM_EXTENSIONS:
        raise LibraryFamilyPolicyError(
            "library_custom_format_extension_unsafe",
            f"Extension '{normalized}' is executable, active content, or an "
            "unsupported archive and cannot be registered.",
        )
    return normalized


def _coerce_builtin_extension(value: Any) -> str:
    normalized = _coerce_extension(value)
    if normalized in _UNSAFE_CUSTOM_EXTENSIONS:
        raise LibraryFamilyPolicyError(
            "library_builtin_format_extension_unsafe",
            f"Extension '{normalized}' is executable, active content, or an "
            "unsupported archive and cannot be assigned to a built-in format.",
        )
    return normalized


def _coerce_family_limit(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise LibraryFamilyPolicyError(
            "library_family_limit_invalid",
            "A format-family limit must be an integer or null.",
        )
    if (
        value < MIN_LIBRARY_UPLOAD_BYTES
        or value > MAX_LIBRARY_FORMAT_UPLOAD_BYTES
    ):
        raise LibraryFamilyPolicyError(
            "library_family_limit_invalid",
            f"A format-family limit must be between {MIN_LIBRARY_UPLOAD_BYTES} "
            f"and {MAX_LIBRARY_FORMAT_UPLOAD_BYTES} bytes.",
        )
    return value


@lru_cache(maxsize=1)
def get_default_library_family_policy_service() -> LibraryFamilyPolicyService:
    return LibraryFamilyPolicyService()


__all__ = [
    "LIBRARY_POLICY_SCHEMA_VERSION",
    "MAX_LIBRARY_CUSTOM_FORMATS",
    "MAX_LIBRARY_FORMAT_UPLOAD_BYTES",
    "MAX_LIBRARY_FORMAT_FAMILIES",
    "MAX_LIBRARY_WORKSPACE_UPLOAD_BYTES",
    "LibraryFamilyMatch",
    "LibraryFamilyPolicyError",
    "LibraryFamilyPolicyService",
    "get_default_library_family_policy_service",
]

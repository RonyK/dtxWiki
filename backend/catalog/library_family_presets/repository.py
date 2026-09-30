"""Validated read-only repository for packaged Library family presets."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from backend.catalog.library_family_presets.models import (
    LibraryFamilyPresetCatalogV1,
    LibraryFamilyPresetDefinition,
)
from backend.services.library_formats import FormatRegistry

CATALOG_RESOURCE = "catalog.v1.json"


class LibraryFamilyPresetCatalogError(RuntimeError):
    """A packaged preset catalog is malformed or inconsistent with formats."""


class CatalogResourceRoot(Protocol):
    def joinpath(self, *descendants: str): ...


class LibraryFamilyPresetCatalogRepository:
    """Expose one completely validated package-owned catalog snapshot."""

    def __init__(
        self,
        *,
        format_registry: FormatRegistry | None = None,
        resource_root: CatalogResourceRoot | Path | None = None,
    ) -> None:
        self._format_registry = format_registry or FormatRegistry()
        self._resource_root = resource_root or resources.files(
            "backend.catalog.library_family_presets"
        )
        self._snapshot: LibraryFamilyPresetCatalogV1 | None = None

    def snapshot(self) -> LibraryFamilyPresetCatalogV1:
        if self._snapshot is None:
            self._snapshot = self._load_and_validate()
        return self._snapshot

    def find(
        self,
        preset_id: str,
        *,
        revision: int | None = None,
    ) -> LibraryFamilyPresetDefinition | None:
        normalized = preset_id.strip().casefold()
        return next(
            (
                preset
                for preset in self.snapshot().presets
                if preset.preset_id == normalized
                and (revision is None or preset.revision == revision)
            ),
            None,
        )

    def _load_and_validate(self) -> LibraryFamilyPresetCatalogV1:
        try:
            raw = self._resource_root.joinpath(CATALOG_RESOURCE).read_text(
                encoding="utf-8"
            )
            payload = json.loads(raw, parse_constant=_reject_non_json_constant)
            catalog = LibraryFamilyPresetCatalogV1.model_validate(payload)
        except (OSError, ValueError, ValidationError) as exc:
            raise LibraryFamilyPresetCatalogError(
                f"invalid Library family preset catalog: {exc}"
            ) from exc

        descriptors = {
            descriptor.format_id: descriptor
            for descriptor in self._format_registry.descriptors()
        }
        preset_ids: set[str] = set()
        owned_formats: dict[str, str] = {}
        for preset in catalog.presets:
            if preset.preset_id in preset_ids:
                raise LibraryFamilyPresetCatalogError(
                    f"duplicate Library family preset id: {preset.preset_id}"
                )
            preset_ids.add(preset.preset_id)
            seen: set[str] = set()
            for format_id in preset.format_ids:
                if (
                    format_id != format_id.strip().casefold()
                    or format_id not in descriptors
                ):
                    raise LibraryFamilyPresetCatalogError(
                        f"preset {preset.preset_id} references unknown canonical "
                        f"format id: {format_id}"
                    )
                if format_id in seen:
                    raise LibraryFamilyPresetCatalogError(
                        f"preset {preset.preset_id} repeats format id: {format_id}"
                    )
                previous = owned_formats.get(format_id)
                if previous is not None:
                    raise LibraryFamilyPresetCatalogError(
                        f"format {format_id} is owned by presets {previous} and "
                        f"{preset.preset_id}"
                    )
                seen.add(format_id)
                owned_formats[format_id] = preset.preset_id
        return catalog


def _reject_non_json_constant(value: str):
    raise ValueError(f"non-standard JSON constant is forbidden: {value}")


@lru_cache(maxsize=1)
def get_library_family_preset_catalog_repository():
    return LibraryFamilyPresetCatalogRepository()


__all__ = [
    "LibraryFamilyPresetCatalogError",
    "LibraryFamilyPresetCatalogRepository",
    "get_library_family_preset_catalog_repository",
]

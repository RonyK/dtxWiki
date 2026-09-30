"""Closed models for the packaged Library family-preset catalog."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LibraryFamilyPresetDefinition(BaseModel):
    preset_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    revision: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=240)
    format_ids: list[str] = Field(min_length=1, max_length=64)

    model_config = ConfigDict(extra="forbid", frozen=True)


class LibraryFamilyPresetCatalogV1(BaseModel):
    schema_version: Literal[1]
    catalog_revision: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}\.\d+$")
    presets: list[LibraryFamilyPresetDefinition] = Field(min_length=1, max_length=6)

    model_config = ConfigDict(extra="forbid", frozen=True)


__all__ = ["LibraryFamilyPresetCatalogV1", "LibraryFamilyPresetDefinition"]

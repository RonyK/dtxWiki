"""Policy-neutral format descriptors and detected facts."""

from dataclasses import dataclass
from enum import StrEnum


class FormatCategory(StrEnum):
    TEXT = "text"
    DOCUMENT = "document"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    DATA = "data"


def _identifier(value: str, name: str, *, extra: str = "") -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string identifier")
    normalized = value.strip().lower()
    allowed = "._-+" + extra
    if (
        not normalized
        or len(normalized) > 80
        or not all(char.isalnum() or char in allowed for char in normalized)
    ):
        raise ValueError(f"{name} must be a bounded identifier")
    return normalized


@dataclass(frozen=True)
class FormatDescriptor:
    format_id: str
    category: FormatCategory
    extensions: tuple[str, ...]
    media_types: tuple[str, ...]
    detector_id: str
    validator_id: str
    limit_profile: str

    def __post_init__(self) -> None:
        if not isinstance(self.category, FormatCategory):
            raise ValueError("category must be a FormatCategory")
        if not isinstance(self.extensions, tuple) or not isinstance(
            self.media_types, tuple
        ):
            raise ValueError("extensions and media_types must be tuples")
        object.__setattr__(self, "format_id", _identifier(self.format_id, "format_id"))
        object.__setattr__(
            self, "detector_id", _identifier(self.detector_id, "detector_id")
        )
        object.__setattr__(
            self, "validator_id", _identifier(self.validator_id, "validator_id")
        )
        object.__setattr__(
            self, "limit_profile", _identifier(self.limit_profile, "limit_profile")
        )
        extensions = tuple(
            _identifier(item.removeprefix("."), "extension") for item in self.extensions
        )
        media_types = tuple(
            _identifier(item, "media_type", extra="/") for item in self.media_types
        )
        if not extensions or len(set(extensions)) != len(extensions):
            raise ValueError("extensions must be non-empty and unique")
        if not media_types or len(set(media_types)) != len(media_types):
            raise ValueError("media_types must be non-empty and unique")
        object.__setattr__(self, "extensions", extensions)
        object.__setattr__(self, "media_types", media_types)


@dataclass(frozen=True)
class FormatFacts:
    format_id: str
    canonical_media_type: str
    detector_id: str
    detector_version: str
    validator_id: str
    validation_schema_version: str

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            extra = "/" if name == "canonical_media_type" else ""
            object.__setattr__(
                self, name, _identifier(getattr(self, name), name, extra=extra)
            )

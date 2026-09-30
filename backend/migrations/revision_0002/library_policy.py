"""Normalize the final Library policy document for revision ``0002_epic1``."""

from __future__ import annotations

import re
from typing import Any

import sqlalchemy as sa
from alembic import op

# Final Library policy normalization.
_CURRENT_SCHEMA = 5
_PREVIOUS_SCHEMA = 2
_PRESERVED_SCHEMAS = frozenset({3, 4, 5})
_MIN_UPLOAD_BYTES = 1_000
_MAX_WORKSPACE_UPLOAD_BYTES = 50_000_000
_MAX_FORMAT_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024
_DEFAULT_UPLOAD_BYTES = 20_000_000
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_CUSTOM_FORMAT_ID = re.compile(r"custom-[a-z0-9][a-z0-9_-]{0,55}")
_EXTENSION = re.compile(r"\.[a-z0-9][a-z0-9_+-]{0,31}")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
_MAX_CUSTOM_FORMATS = 64
_MAX_CUSTOM_FORMAT_EXTENSIONS = 16
_MAX_CUSTOM_FORMAT_NAME_CHARS = 80
_BUILT_IN_DEFAULT_EXTENSIONS = {
    "markdown": [".md", ".markdown"],
    "text": [".txt"],
    "html": [".html", ".htm", ".xhtml"],
    "javascript": [".js", ".mjs", ".cjs"],
    "csv": [".csv"],
    "tsv": [".tsv"],
    "json": [".json"],
    "yaml": [".yaml", ".yml"],
    "pdf": [".pdf"],
    "docx": [".docx"],
    "pptx": [".pptx"],
    "xlsx": [".xlsx"],
    "doc": [".doc"],
    "ppt": [".ppt"],
    "xls": [".xls"],
    "jpeg": [".jpg", ".jpeg"],
    "png": [".png"],
    "gif": [".gif"],
    "webp": [".webp"],
    "tiff": [".tif", ".tiff"],
    "mp3": [".mp3"],
    "ogg": [".ogg", ".oga"],
    "wav": [".wav"],
    "mp4": [".mp4", ".m4v"],
    "webm": [".webm"],
    "hdf5": [".h5", ".hdf5", ".hdf"],
    "hdf4": [".hdf4", ".hdf"],
    "parquet": [".parquet"],
    "hwp": [".hwp"],
    "hwpx": [".hwpx"],
    "hwpml": [".hwpml"],
}
_BUILT_IN_FORMAT_IDS = frozenset(_BUILT_IN_DEFAULT_EXTENSIONS)
_POLICY_FORMAT_IDS = frozenset(
    {
        "md",
        "txt",
        "html",
        "javascript",
        "csv",
        "tsv",
        "json",
        "yaml",
        "pdf",
        "docx",
        "xlsx",
        "pptx",
        "doc",
        "xls",
        "ppt",
        "jpeg",
        "png",
        "gif",
        "webp",
        "tiff",
        "mp3",
        "ogg",
        "wav",
        "mp4",
        "webm",
        "hdf5",
        "hdf4",
        "parquet",
        "hwp",
        "hwpx",
        "hwpml",
    }
)
_COMMON_FORMATS = sorted(_POLICY_FORMAT_IDS - {"hdf4"})
_BUILT_IN_EXTENSIONS = frozenset(
    extension
    for extensions in _BUILT_IN_DEFAULT_EXTENSIONS.values()
    for extension in extensions
)
_UNSAFE_CUSTOM_EXTENSIONS = frozenset(
    {
        # Keep migration-time sanitation aligned with the v3 passive-storage
        # contract without importing current runtime modules.
        ".7z",
        ".ar",
        ".apk",
        ".app",
        ".bash",
        ".bat",
        ".bz2",
        ".cab",
        ".cjs",
        ".class",
        ".cmd",
        ".com",
        ".cpl",
        ".cpio",
        ".desktop",
        ".dll",
        ".dmg",
        ".docm",
        ".dotm",
        ".exe",
        ".fish",
        ".gz",
        ".hta",
        ".htm",
        ".html",
        ".ipa",
        ".iso",
        ".jar",
        ".js",
        ".jse",
        ".lnk",
        ".lz4",
        ".mjs",
        ".msi",
        ".msix",
        ".php",
        ".pl",
        ".potm",
        ".ppam",
        ".ppsm",
        ".pptm",
        ".ps1",
        ".psm1",
        ".py",
        ".pyw",
        ".rar",
        ".rb",
        ".reg",
        ".scr",
        ".sh",
        ".svg",
        ".tar",
        ".tgz",
        ".url",
        ".vbe",
        ".vbs",
        ".war",
        ".wsf",
        ".wsh",
        ".xhtml",
        ".xlam",
        ".xlsb",
        ".xlsm",
        ".xltm",
        ".xz",
        ".zip",
        ".zsh",
        ".zst",
        ".zstd",
    }
)
_DEFAULT_UNAVAILABLE_BEHAVIOR = {
    "preview": "store_and_download",
    "semantic_conversion": "store_and_download",
}

_workspace = sa.table(
    "workspace",
    sa.column("id", sa.Uuid()),
    sa.column("library_policy", sa.JSON()),
)


def _upgrade_library_policy() -> None:
    connection = op.get_bind()
    rows = (
        connection.execute(sa.select(_workspace.c.id, _workspace.c.library_policy))
        .mappings()
        .all()
    )
    for row in rows:
        connection.execute(
            sa.update(_workspace)
            .where(_workspace.c.id == row["id"])
            .values(library_policy=_upgrade_policy(row["library_policy"]))
        )


def _downgrade_library_policy() -> None:
    connection = op.get_bind()
    rows = (
        connection.execute(sa.select(_workspace.c.id, _workspace.c.library_policy))
        .mappings()
        .all()
    )
    for row in rows:
        policy = row["library_policy"]
        if isinstance(policy, dict) and (
            policy.get("custom_formats") or policy.get("builtin_format_overrides")
        ):
            raise RuntimeError(
                "Cannot downgrade Library policy v5 while format aliases exist"
            )
        downgraded = dict(policy) if isinstance(policy, dict) else {}
        downgraded["policy_schema_version"] = _PREVIOUS_SCHEMA
        downgraded.pop("custom_formats", None)
        downgraded.pop("builtin_format_overrides", None)
        downgraded["format_families"] = [
            {key: item for key, item in family.items() if key != "preset_baseline"}
            for family in downgraded.get("format_families", [])
            if isinstance(family, dict)
        ]
        downgraded["category_limits"] = {}
        downgraded["profile_id"] = "common_media_documents"
        downgraded["profile_revision"] = 1
        downgraded["family_modes"] = {}
        downgraded["format_exceptions"] = {}
        downgraded["enabled_formats"] = list(_COMMON_FORMATS)
        connection.execute(
            sa.update(_workspace)
            .where(_workspace.c.id == row["id"])
            .values(library_policy=downgraded)
        )


def _upgrade_policy(value: Any) -> dict[str, Any]:
    """Return one bounded current policy without importing runtime models.

    Schema v1 was never released. Its category/family compatibility data is
    deliberately reset, while its explicit Workspace upload value and
    processing behavior survive. Schema v2 was also development-only and
    receives the same reset. Schemas v3-v5 are sanitized and upgraded in place
    so retrying a partial migration is safe.
    """

    source = value if isinstance(value, dict) else {}
    prior_schema = source.get("policy_schema_version")
    preserve_current_preferences = prior_schema in _PRESERVED_SCHEMAS
    max_upload_bytes = source.get("max_upload_bytes")
    if (
        isinstance(max_upload_bytes, bool)
        or not isinstance(max_upload_bytes, int)
        or not _MIN_UPLOAD_BYTES <= max_upload_bytes <= _MAX_WORKSPACE_UPLOAD_BYTES
    ):
        max_upload_bytes = _DEFAULT_UPLOAD_BYTES

    allow_store = source.get("allow_store_without_converter", True)
    if not isinstance(allow_store, bool):
        allow_store = True
    manual_approval = source.get("manual_approval_required", True)
    if not isinstance(manual_approval, bool):
        manual_approval = True
    unavailable_behavior = source.get("unavailable_behavior")
    if not isinstance(unavailable_behavior, dict):
        unavailable_behavior = dict(_DEFAULT_UNAVAILABLE_BEHAVIOR)

    builtin_format_overrides = (
        _sanitize_builtin_format_overrides(source.get("builtin_format_overrides"))
        if preserve_current_preferences
        else {}
    )
    effective_builtin_extensions = _effective_builtin_extensions(
        builtin_format_overrides
    )
    custom_formats = (
        _sanitize_custom_formats(
            source.get("custom_formats"),
            built_in_extensions=effective_builtin_extensions,
        )
        if preserve_current_preferences
        else []
    )
    custom_format_ids = {str(item["format_id"]) for item in custom_formats}
    format_families = (
        _sanitize_families(
            source.get("format_families"),
            custom_format_ids=custom_format_ids,
        )
        if preserve_current_preferences
        else []
    )
    format_overrides = (
        _sanitize_format_overrides(
            source.get("format_overrides"),
            custom_format_ids=custom_format_ids,
        )
        if preserve_current_preferences
        else {}
    )
    return {
        "manual_approval_required": manual_approval,
        "policy_schema_version": _CURRENT_SCHEMA,
        "max_upload_bytes": max_upload_bytes,
        "format_overrides": format_overrides,
        "builtin_format_overrides": builtin_format_overrides,
        "allow_store_without_converter": allow_store,
        "format_families": format_families,
        "custom_formats": custom_formats,
        "unavailable_behavior": dict(unavailable_behavior),
    }


def _sanitize_builtin_format_overrides(
    value: Any,
) -> dict[str, dict[str, list[str]]]:
    if not isinstance(value, dict):
        return {}
    sanitized: dict[str, dict[str, list[str]]] = {}
    for raw_format_id, raw_override in value.items():
        if not isinstance(raw_format_id, str) or not isinstance(raw_override, dict):
            continue
        candidate = raw_format_id.strip().casefold()
        format_id = {"md": "markdown", "txt": "text"}.get(candidate, candidate)
        raw_extensions = raw_override.get("extensions")
        if (
            format_id not in _BUILT_IN_FORMAT_IDS
            or format_id in sanitized
            or set(raw_override) != {"extensions"}
            or not isinstance(raw_extensions, list)
            or not 1 <= len(raw_extensions) <= _MAX_CUSTOM_FORMAT_EXTENSIONS
        ):
            continue
        extensions: list[str] = []
        for raw_extension in raw_extensions:
            extension = _sanitize_extension(raw_extension)
            if (
                extension is None
                or extension in _UNSAFE_CUSTOM_EXTENSIONS
                or extension in extensions
            ):
                extensions = []
                break
            extensions.append(extension)
        if extensions and set(extensions) != set(
            _BUILT_IN_DEFAULT_EXTENSIONS[format_id]
        ):
            sanitized[format_id] = {"extensions": extensions}

    package_owners = _extension_owners({})
    effective_owners = _extension_owners(sanitized)
    if any(
        len(owners) > 1 and not owners.issubset(package_owners.get(extension, set()))
        for extension, owners in effective_owners.items()
    ):
        return {}
    return sanitized


def _effective_builtin_extensions(
    overrides: dict[str, dict[str, list[str]]],
) -> frozenset[str]:
    return frozenset(_extension_owners(overrides))


def _extension_owners(
    overrides: dict[str, dict[str, list[str]]],
) -> dict[str, set[str]]:
    owners: dict[str, set[str]] = {}
    for format_id, defaults in _BUILT_IN_DEFAULT_EXTENSIONS.items():
        extensions = overrides.get(format_id, {}).get("extensions", defaults)
        for extension in extensions:
            owners.setdefault(extension, set()).add(format_id)
    return owners


def _sanitize_extension(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    extension = value.strip().casefold()
    if extension and not extension.startswith("."):
        extension = f".{extension}"
    return extension if _EXTENSION.fullmatch(extension) else None


def _sanitize_custom_formats(
    value: Any,
    *,
    built_in_extensions: frozenset[str] = _BUILT_IN_EXTENSIONS,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    sanitized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    extension_owners: set[str] = set()
    for candidate in value[:_MAX_CUSTOM_FORMATS]:
        custom_format = _sanitize_custom_format(
            candidate,
            built_in_extensions=built_in_extensions,
        )
        if custom_format is None:
            continue
        format_id = str(custom_format["format_id"])
        display_name = str(custom_format["display_name"]).casefold()
        extensions = {str(item) for item in custom_format["extensions"]}
        if (
            format_id in seen_ids
            or display_name in seen_names
            or extensions & extension_owners
        ):
            continue
        seen_ids.add(format_id)
        seen_names.add(display_name)
        extension_owners.update(extensions)
        sanitized.append(custom_format)
    return sanitized


def _sanitize_custom_format(
    value: Any,
    *,
    built_in_extensions: frozenset[str] = _BUILT_IN_EXTENSIONS,
) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != {
        "format_id",
        "display_name",
        "extensions",
    }:
        return None
    raw_format_id = value.get("format_id")
    raw_display_name = value.get("display_name")
    raw_extensions = value.get("extensions")
    if (
        not isinstance(raw_format_id, str)
        or not isinstance(raw_display_name, str)
        or not isinstance(raw_extensions, list)
        or not 1 <= len(raw_extensions) <= _MAX_CUSTOM_FORMAT_EXTENSIONS
    ):
        return None
    format_id = raw_format_id.strip().casefold()
    display_name = " ".join(raw_display_name.split())
    if (
        not _CUSTOM_FORMAT_ID.fullmatch(format_id)
        or format_id in _BUILT_IN_FORMAT_IDS
        or not 1 <= len(display_name) <= _MAX_CUSTOM_FORMAT_NAME_CHARS
        or _CONTROL_CHARACTERS.search(display_name)
    ):
        return None
    extensions: list[str] = []
    for raw_extension in raw_extensions:
        if not isinstance(raw_extension, str):
            return None
        extension = raw_extension.strip().casefold()
        if extension and not extension.startswith("."):
            extension = f".{extension}"
        if (
            not _EXTENSION.fullmatch(extension)
            or extension in built_in_extensions
            or extension in _UNSAFE_CUSTOM_EXTENSIONS
            or extension in extensions
        ):
            return None
        extensions.append(extension)
    return {
        "format_id": format_id,
        "display_name": display_name,
        "extensions": extensions,
    }


def _sanitize_format_overrides(
    value: Any,
    *,
    custom_format_ids: set[str] | None = None,
) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    sanitized: dict[str, int] = {}
    for raw_format_id, raw_limit in value.items():
        if not isinstance(raw_format_id, str):
            continue
        format_id = raw_format_id.strip().casefold()
        if format_id in {"markdown", "text"}:
            format_id = {"markdown": "md", "text": "txt"}[format_id]
        if (
            format_id not in _POLICY_FORMAT_IDS
            and format_id not in (custom_format_ids or set())
        ) or (
            isinstance(raw_limit, bool)
            or not isinstance(raw_limit, int)
            or not _MIN_UPLOAD_BYTES <= raw_limit <= _MAX_FORMAT_UPLOAD_BYTES
        ):
            continue
        sanitized[format_id] = raw_limit
    return sanitized


def _sanitize_families(
    value: Any,
    *,
    custom_format_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    families: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    format_owners: set[str] = set()
    for candidate in value[:6]:
        family = _sanitize_family(
            candidate,
            custom_format_ids=custom_format_ids or set(),
        )
        if family is None:
            continue
        family_id = str(family["family_id"])
        normalized_name = str(family["name"]).casefold()
        format_ids = set(family["format_ids"])
        if (
            family_id in seen_ids
            or normalized_name in seen_names
            or format_ids & format_owners
        ):
            continue
        seen_ids.add(family_id)
        seen_names.add(normalized_name)
        format_owners.update(format_ids)
        families.append(family)
    return families


def _sanitize_family(
    value: Any,
    *,
    custom_format_ids: set[str],
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    raw_family_id = value.get("family_id")
    raw_name = value.get("name")
    raw_format_ids = value.get("format_ids")
    raw_limit = value.get("max_upload_bytes")
    if (
        not isinstance(raw_family_id, str)
        or not isinstance(raw_name, str)
        or not isinstance(raw_format_ids, list)
    ):
        return None
    family_id = raw_family_id.strip().casefold()
    name = " ".join(raw_name.split())
    if (
        not _IDENTIFIER.fullmatch(family_id)
        or not 1 <= len(name) <= 80
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        return None
    format_ids: list[str] = []
    for raw_format_id in raw_format_ids:
        if not isinstance(raw_format_id, str):
            return None
        format_id = raw_format_id.strip().casefold()
        format_id = {"md": "markdown", "txt": "text"}.get(format_id, format_id)
        if (
            format_id not in _BUILT_IN_FORMAT_IDS and format_id not in custom_format_ids
        ) or format_id in format_ids:
            return None
        format_ids.append(format_id)
    if not format_ids or (
        raw_limit is not None
        and (
            isinstance(raw_limit, bool)
            or not isinstance(raw_limit, int)
            or not _MIN_UPLOAD_BYTES <= raw_limit <= _MAX_FORMAT_UPLOAD_BYTES
        )
    ):
        return None
    result: dict[str, Any] = {
        "family_id": family_id,
        "name": name,
        "origin": "custom",
        "format_ids": format_ids,
        "max_upload_bytes": raw_limit,
    }
    if (
        value.get("origin") == "preset"
        and isinstance(value.get("preset_id"), str)
        and _IDENTIFIER.fullmatch(value["preset_id"].strip().casefold())
        and isinstance(value.get("preset_revision"), int)
        and not isinstance(value.get("preset_revision"), bool)
        and value["preset_revision"] >= 1
    ):
        preset_id = value["preset_id"].strip().casefold()
        preset_revision = value["preset_revision"]
        preset_baseline = _sanitize_preset_baseline(
            value.get("preset_baseline"),
            expected_preset_id=preset_id,
            expected_preset_revision=preset_revision,
        )
        if preset_baseline is None and all(
            format_id in _BUILT_IN_DEFAULT_EXTENSIONS for format_id in format_ids
        ):
            preset_baseline = {
                "preset_id": preset_id,
                "preset_revision": preset_revision,
                "format_ids": list(format_ids),
                "format_extensions": {
                    format_id: list(_BUILT_IN_DEFAULT_EXTENSIONS[format_id])
                    for format_id in format_ids
                },
            }
        if preset_baseline is None:
            return result
        result["origin"] = "preset"
        result["preset_id"] = preset_id
        result["preset_revision"] = preset_revision
        result["preset_baseline"] = preset_baseline
    return result


def _sanitize_preset_baseline(
    value: Any,
    *,
    expected_preset_id: str,
    expected_preset_revision: int,
) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != {
        "preset_id",
        "preset_revision",
        "format_ids",
        "format_extensions",
    }:
        return None
    raw_preset_id = value.get("preset_id")
    raw_preset_revision = value.get("preset_revision")
    raw_format_ids = value.get("format_ids")
    raw_format_extensions = value.get("format_extensions")
    if (
        not isinstance(raw_preset_id, str)
        or raw_preset_id.strip().casefold() != expected_preset_id
        or raw_preset_revision != expected_preset_revision
        or isinstance(raw_preset_revision, bool)
        or not isinstance(raw_format_ids, list)
        or not raw_format_ids
        or not isinstance(raw_format_extensions, dict)
    ):
        return None
    format_ids: list[str] = []
    for raw_format_id in raw_format_ids:
        if not isinstance(raw_format_id, str):
            return None
        candidate = raw_format_id.strip().casefold()
        format_id = {"md": "markdown", "txt": "text"}.get(candidate, candidate)
        if format_id not in _BUILT_IN_FORMAT_IDS or format_id in format_ids:
            return None
        format_ids.append(format_id)
    if set(raw_format_extensions) != set(format_ids):
        return None
    format_extensions: dict[str, list[str]] = {}
    for format_id in format_ids:
        raw_extensions = raw_format_extensions.get(format_id)
        if (
            not isinstance(raw_extensions, list)
            or not 1 <= len(raw_extensions) <= _MAX_CUSTOM_FORMAT_EXTENSIONS
        ):
            return None
        extensions: list[str] = []
        for raw_extension in raw_extensions:
            extension = _sanitize_extension(raw_extension)
            if extension is None or extension in extensions:
                return None
            extensions.append(extension)
        format_extensions[format_id] = extensions
    return {
        "preset_id": expected_preset_id,
        "preset_revision": expected_preset_revision,
        "format_ids": format_ids,
        "format_extensions": format_extensions,
    }


def upgrade() -> None:
    _upgrade_library_policy()


def downgrade() -> None:
    _downgrade_library_policy()


__all__ = ["_upgrade_policy", "downgrade", "upgrade"]

from pathlib import Path

import pytest
from sqlalchemy import select

from backend.models.workspace import Workspace
from backend.policy.auth import ActorContext
from backend.schemas.read import LibraryCapabilitiesPayload
from backend.services.converter_registry import build_converter_registry
from backend.services.library_capabilities import LibraryCapabilityService
from backend.services.library_formats import FormatRegistry, LibraryFormatError
from backend.services.library_policy import (
    LibraryFamilyPolicyError,
    LibraryFamilyPolicyService,
)
from backend.services.settings_service import (
    SettingsPolicyUpdateRejected,
    SettingsService,
    library_upload_limit_bytes,
)


def _owner(workspace_id):
    return ActorContext(
        actor_kind="human",
        actor_id="owner",
        token_kind="session",
        scopes=("workspace-admin",),
        workspace_id=workspace_id,
        workspace_role="owner",
        role="owner",
    )


def _custom_formats():
    return [
        {
            "format_id": "custom-cad",
            "display_name": "CAD drawing",
            "extensions": [" DWG ", ".DXF"],
        }
    ]


def _minimal_pdf() -> bytes:
    header = b"%PDF-1.4\n"
    catalog_offset = len(header)
    catalog = b"1 0 obj\n<< /Type /Catalog >>\nendobj\n"
    xref_offset = catalog_offset + len(catalog)
    xref = (
        b"xref\n0 2\n0000000000 65535 f \n"
        + f"{catalog_offset:010d} 00000 n \n".encode()
    )
    trailer = (
        b"trailer\n<< /Size 2 /Root 1 0 R >>\nstartxref\n"
        + str(xref_offset).encode()
        + b"\n%%EOF\n"
    )
    return header + catalog + xref + trailer


def test_custom_format_normalizes_multiple_aliases_and_family_owns_one_format():
    service = LibraryFamilyPolicyService()

    custom_formats = service.coerce_custom_formats(_custom_formats())
    families = service.coerce_families(
        [
            {
                "family_id": "engineering",
                "name": "Engineering",
                "extensions": [".dwg"],
                "max_upload_bytes": 45_000_000,
            }
        ],
        custom_formats=custom_formats,
    )

    assert custom_formats[0]["extensions"] == [".dwg", ".dxf"]
    assert families[0]["format_ids"] == ["custom-cad"]
    assert service.resolve_extension(
        ".dxf",
        custom_formats=custom_formats,
    ) == "custom-cad"


@pytest.mark.parametrize(
    ("extension", "code"),
    [
        (".md", "library_custom_format_extension_conflict"),
        (".EXE", "library_custom_format_extension_unsafe"),
        (".html", "library_custom_format_extension_unsafe"),
        (".js", "library_custom_format_extension_unsafe"),
        ("../dwg", "library_custom_format_extension_invalid"),
    ],
)
def test_custom_format_extension_registration_fails_closed(extension, code):
    with pytest.raises(LibraryFamilyPolicyError) as rejected:
        LibraryFamilyPolicyService().coerce_custom_formats(
            [
                {
                    "format_id": "custom-example",
                    "display_name": "Example",
                    "extensions": [extension],
                }
            ]
        )

    assert rejected.value.code == code


def test_custom_extension_ownership_is_unique_across_custom_formats():
    with pytest.raises(LibraryFamilyPolicyError) as rejected:
        LibraryFamilyPolicyService().coerce_custom_formats(
            [
                {
                    "format_id": "custom-one",
                    "display_name": "One",
                    "extensions": [".foo"],
                },
                {
                    "format_id": "custom-two",
                    "display_name": "Two",
                    "extensions": ["FOO"],
                },
            ]
        )

    assert rejected.value.code == "library_custom_format_extension_conflict"


def test_custom_id_cannot_be_regenerated_during_one_update():
    with pytest.raises(LibraryFamilyPolicyError) as rejected:
        LibraryFamilyPolicyService().coerce_custom_formats_for_update(
            [
                {
                    "format_id": "custom-new",
                    "display_name": "CAD drawing",
                    "extensions": [".dwg", ".dxf"],
                }
            ],
            existing_custom_formats=_custom_formats(),
        )

    assert rejected.value.code == "library_custom_format_id_immutable"


def test_custom_format_detection_is_passive_attachment_only(tmp_path: Path):
    service = LibraryFamilyPolicyService()
    descriptors = service.custom_descriptors(_custom_formats())
    source = tmp_path / "drawing.dwg"
    source.write_bytes(b"opaque passive drawing bytes")

    result = FormatRegistry().detect(
        source,
        filename="drawing.DWG",
        declared_media_type="application/x-dwg",
        custom_descriptors=descriptors,
    )

    assert result.format_id == "custom-cad"
    assert result.detected_media_type == "application/octet-stream"
    assert result.detector == "workspace-extension-store-only-v1"
    assert result.container["presentation"] == "attachment_only"


@pytest.mark.parametrize(
    ("content", "code"),
    [
        (_minimal_pdf(), "library_custom_format_content_conflict"),
        (b"MZ" + b"\0" * 32, "library_custom_format_executable"),
        (b"#!/bin/sh\necho no\n", "library_custom_format_executable"),
        (
            b"<!doctype html><script>x()</script>",
            "library_custom_format_active_content",
        ),
    ],
)
def test_custom_format_rejects_known_executable_or_active_content(
    tmp_path: Path,
    content: bytes,
    code: str,
):
    descriptors = LibraryFamilyPolicyService().custom_descriptors(_custom_formats())
    source = tmp_path / "payload.dwg"
    source.write_bytes(content)

    with pytest.raises(LibraryFormatError) as rejected:
        FormatRegistry().detect(
            source,
            filename=source.name,
            declared_media_type="application/octet-stream",
            custom_descriptors=descriptors,
        )
    assert rejected.value.code == code


@pytest.mark.parametrize(
    "content",
    [
        b"PK\x03\x04" + b"\0" * 32,
        b"\x1f\x8b\x08" + b"\0" * 32,
        b"7z\xbc\xaf'\x1c" + b"\0" * 32,
        b"Rar!\x1a\x07\x01\x00" + b"\0" * 32,
        b"\0" * 257 + b"ustar\0" + b"\0" * 32,
        b"MSCF" + b"\0" * 32,
        b"!<arch>\n" + b"\0" * 32,
        b"\x28\xb5\x2f\xfd" + b"\0" * 32,
        b"\x04\x22\x4d\x18" + b"\0" * 32,
        b"070701" + b"\0" * 32,
        b"\0" * 32_769 + b"CD001" + b"\0" * 32,
    ],
    ids=(
        "zip",
        "gzip",
        "7z",
        "rar",
        "tar",
        "cab",
        "ar",
        "zstd",
        "lz4",
        "cpio",
        "iso9660",
    ),
)
def test_custom_format_rejects_generic_archive_payloads(
    tmp_path: Path,
    content: bytes,
):
    descriptors = LibraryFamilyPolicyService().custom_descriptors(_custom_formats())
    source = tmp_path / "archive.dwg"
    source.write_bytes(content)

    with pytest.raises(LibraryFormatError) as rejected:
        FormatRegistry().detect(
            source,
            filename=source.name,
            declared_media_type="application/octet-stream",
            custom_descriptors=descriptors,
        )

    assert rejected.value.code == "library_custom_format_archive"


def test_custom_format_rejects_declared_archive_media_type(tmp_path: Path):
    descriptors = LibraryFamilyPolicyService().custom_descriptors(_custom_formats())
    source = tmp_path / "archive.dwg"
    source.write_bytes(b"opaque bytes whose full body is not parsed")

    with pytest.raises(LibraryFormatError) as rejected:
        FormatRegistry().detect(
            source,
            filename=source.name,
            declared_media_type="application/x-tar",
            custom_descriptors=descriptors,
        )

    assert rejected.value.code == "library_custom_format_archive"


def test_custom_capability_is_store_only_and_family_may_exceed_workspace():
    policy = {
        "policy_schema_version": 3,
        "max_upload_bytes": 20_000_000,
        "format_overrides": {},
        "format_families": [
            {
                "family_id": "engineering",
                "name": "Engineering",
                "origin": "custom",
                "format_ids": ["custom-cad"],
                "max_upload_bytes": 45_000_000,
            }
        ],
        "custom_formats": _custom_formats(),
        "allow_store_without_converter": True,
    }
    service = LibraryCapabilityService(
        format_registry=FormatRegistry(),
        converter_registry=build_converter_registry(),
        ingest_enabled=True,
        conversion_enabled=True,
        instance_hard_cap_bytes=50_000_000,
    )

    projected = LibraryCapabilitiesPayload.model_validate(service.project(policy))
    custom = next(
        item for item in projected.formats if item.format_id == "custom-cad"
    )
    assert custom.display_name == "CAD drawing"
    assert custom.extensions == [".dwg", ".dxf"]
    assert custom.upload is True
    assert custom.conversion_state == "stored_unconverted"
    assert custom.preview_kind == "metadata"
    assert custom.max_upload_bytes == 45_000_000
    assert custom.upload_limit_sources == {
        "family": 45_000_000,
        "instance": 50_000_000,
    }


def test_custom_removal_requires_reference_cleanup_in_same_cas_batch(
    session_factory,
):
    service = SettingsService()
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        before = service.get_policies(
            control_session=session,
            workspace_session=session,
            workspace_id=workspace_id,
        ).library
        added = service.update_policy_batch(
            control_session=session,
            workspace_session=session,
            workspace_id=workspace_id,
            actor=_owner(workspace_id),
            group="library",
            changes={
                "custom_formats": _custom_formats(),
                "format_families": [
                    {
                        "family_id": "engineering",
                        "name": "Engineering",
                        "format_ids": ["custom-cad"],
                        "max_upload_bytes": 40_000_000,
                    }
                ],
                "format_overrides": {"custom-cad": 42_000_000},
            },
            expected_revision=before.revision,
        ).library
        assert library_upload_limit_bytes(
            added.policy,
            format_id="custom-cad",
        ) == 42_000_000

    with session_factory.begin() as session:
        with pytest.raises(SettingsPolicyUpdateRejected) as rejected:
            service.update_policy_batch(
                control_session=session,
                workspace_session=session,
                workspace_id=workspace_id,
                actor=_owner(workspace_id),
                group="library",
                changes={"custom_formats": []},
                expected_revision=added.revision,
            )
        assert rejected.value.error_code == "library_family_format_unknown"

    with session_factory.begin() as session:
        current = service.get_policies(
            control_session=session,
            workspace_session=session,
            workspace_id=workspace_id,
        ).library
        assert current == added
        removed = service.update_policy_batch(
            control_session=session,
            workspace_session=session,
            workspace_id=workspace_id,
            actor=_owner(workspace_id),
            group="library",
            # Deliberately submit custom_formats first: validation belongs to
            # the final merged state, not eager per-key reference checks.
            changes={
                "custom_formats": [],
                "format_families": [],
                "format_overrides": {},
            },
            expected_revision=current.revision,
        ).library
        assert removed.policy["custom_formats"] == []
        assert removed.policy["format_families"] == []
        assert removed.policy["format_overrides"] == {}

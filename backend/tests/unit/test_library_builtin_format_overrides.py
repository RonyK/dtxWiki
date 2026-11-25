from dataclasses import replace

import pytest
from sqlalchemy import select

from backend.models.workspace import Workspace, default_library_policy
from backend.policy.auth import ActorContext
from backend.schemas.read import LibraryCapabilitiesPayload
from backend.services.converter_registry import build_converter_registry
from backend.services.library_capabilities import LibraryCapabilityService
from backend.services.library_formats import FormatRegistry, LibraryFormatError
from backend.services.library_policy import LibraryFamilyPolicyService
from backend.services.settings_service import (
    SettingsPolicyUpdateRejected,
    SettingsService,
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


def test_sparse_override_normalizes_aliases_and_preserves_descriptor_identity():
    service = LibraryFamilyPolicyService()
    registry = FormatRegistry()
    package = registry.descriptor("pdf")

    assert service.coerce_builtin_format_overrides(
        {"pdf": {"extensions": [" PDF "]}}
    ) == {}
    overrides = service.coerce_builtin_format_overrides(
        {"pdf": {"extensions": ["paper", ".PDFX"]}}
    )
    assert overrides == {"pdf": {"extensions": [".paper", ".pdfx"]}}

    effective = service.builtin_descriptor_overrides(overrides)[0]
    assert effective == replace(package, extensions=(".paper", ".pdfx"))
    assert effective.media_types == package.media_types
    assert effective.admission == package.admission
    assert effective.container_validator == package.container_validator

    with pytest.raises(ValueError) as unsafe:
        service.coerce_builtin_format_overrides(
            {"pdf": {"extensions": [".exe"]}}
        )
    assert getattr(unsafe.value, "code", None) == (
        "library_builtin_format_extension_unsafe"
    )


def test_effective_alias_enables_upload_detection_and_removed_default_is_rejected(
    tmp_path,
):
    service = LibraryFamilyPolicyService()
    registry = FormatRegistry()
    descriptor_overrides = service.builtin_descriptor_overrides(
        {"pdf": {"extensions": [".paper"]}}
    )
    renamed = tmp_path / "source.paper"
    renamed.write_bytes(_minimal_pdf())

    detected = registry.detect(
        renamed,
        filename=renamed.name,
        declared_media_type="application/pdf",
        builtin_descriptor_overrides=descriptor_overrides,
    )
    assert detected.format_id == "pdf"
    assert registry.descriptor(
        "pdf", builtin_descriptor_overrides=descriptor_overrides
    ).extensions == (".paper",)

    removed_default = tmp_path / "source.pdf"
    removed_default.write_bytes(_minimal_pdf())
    with pytest.raises(LibraryFormatError) as rejected:
        registry.detect(
            removed_default,
            filename=removed_default.name,
            declared_media_type="application/pdf",
            builtin_descriptor_overrides=descriptor_overrides,
        )
    assert rejected.value.code == "unsupported_library_upload"


def test_capabilities_project_effective_and_package_aliases_with_preset_snapshot():
    policy = default_library_policy()
    policy["builtin_format_overrides"] = {
        "docx": {"extensions": [".word", ".docx2"]}
    }
    policy["format_families"] = [
        {
            "family_id": "documents",
            "name": "Documents",
            "origin": "preset",
            "preset_id": "document",
            "preset_revision": 1,
            "format_ids": ["docx", "xlsx"],
            "max_upload_bytes": None,
            "preset_baseline": {
                "preset_id": "document",
                "preset_revision": 1,
                "format_ids": ["docx", "xlsx"],
                "format_extensions": {
                    "docx": [".docx"],
                    "xlsx": [".xlsx"],
                },
            },
        }
    ]
    projected = LibraryCapabilitiesPayload.model_validate(
        LibraryCapabilityService(
            format_registry=FormatRegistry(),
            converter_registry=build_converter_registry(),
            ingest_enabled=True,
            conversion_enabled=True,
            instance_hard_cap_bytes=50_000_000,
        ).project(policy)
    )

    docx = next(item for item in projected.formats if item.format_id == "docx")
    assert docx.extensions == [".word", ".docx2"]
    assert docx.default_extensions == [".docx"]
    assert docx.extensions_overridden is True
    assert set(projected.families[0].extensions) >= {".word", ".docx2", ".xlsx"}
    assert projected.families[0].preset_baseline is not None
    assert projected.families[0].preset_baseline.format_extensions == {
        "docx": [".docx"],
        "xlsx": [".xlsx"],
    }
    document_preset = next(
        item for item in projected.family_presets if item.preset_id == "document"
    )
    assert document_preset.format_extensions["docx"] == [".docx"]


def test_same_cas_builtin_transfers_reject_and_two_revisions_release_then_claim(
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
            workspace_id=workspace_id
        ).library

        with pytest.raises(SettingsPolicyUpdateRejected) as builtin_rejected:
            service.update_policy_batch(
                control_session=session,
                workspace_session=session,
                workspace_id=workspace_id,
                actor=_owner(workspace_id),
                group="library",
                changes={
                    "builtin_format_overrides": {
                        "docx": {"extensions": [".word"]},
                        "xlsx": {"extensions": [".xlsx", ".docx"]},
                    }
                },
                expected_revision=before.revision,
            )
        assert (
            builtin_rejected.value.error_code
            == "library_format_extension_transfer_requires_release"
        )

        with pytest.raises(SettingsPolicyUpdateRejected) as custom_rejected:
            service.update_policy_batch(
                control_session=session,
                workspace_session=session,
                workspace_id=workspace_id,
                actor=_owner(workspace_id),
                group="library",
                changes={
                    "builtin_format_overrides": {
                        "pdf": {"extensions": [".paper"]}
                    },
                    "custom_formats": [
                        {
                            "format_id": "custom-pdf-alias",
                            "display_name": "PDF alias",
                            "extensions": [".pdf"],
                        }
                    ],
                },
                expected_revision=before.revision,
            )
        assert (
            custom_rejected.value.error_code
            == "library_format_extension_transfer_requires_release"
        )

        released = service.update_policy_batch(
            control_session=session,
            workspace_session=session,
            workspace_id=workspace_id,
            actor=_owner(workspace_id),
            group="library",
            changes={
                "builtin_format_overrides": {
                    "docx": {"extensions": [".word"]},
                    "pdf": {"extensions": [".paper"]},
                }
            },
            expected_revision=before.revision,
        ).library
        claimed = service.update_policy_batch(
            control_session=session,
            workspace_session=session,
            workspace_id=workspace_id,
            actor=_owner(workspace_id),
            group="library",
            changes={
                "builtin_format_overrides": {
                    "docx": {"extensions": [".word"]},
                    "xlsx": {"extensions": [".xlsx", ".docx"]},
                    "pdf": {"extensions": [".paper"]},
                },
                "custom_formats": [
                    {
                        "format_id": "custom-pdf-alias",
                        "display_name": "PDF alias",
                        "extensions": [".pdf"],
                    }
                ],
            },
            expected_revision=released.revision,
        ).library

        assert claimed.policy["builtin_format_overrides"]["xlsx"] == {
            "extensions": [".xlsx", ".docx"]
        }
        assert claimed.policy["custom_formats"][0]["extensions"] == [".pdf"]

        with pytest.raises(SettingsPolicyUpdateRejected) as reverse_rejected:
            service.update_policy_batch(
                control_session=session,
                workspace_session=session,
                workspace_id=workspace_id,
                actor=_owner(workspace_id),
                group="library",
                changes={
                    "builtin_format_overrides": {
                        "docx": {"extensions": [".word"]},
                        "xlsx": {"extensions": [".xlsx", ".docx"]},
                    },
                    "custom_formats": [],
                },
                expected_revision=claimed.revision,
            )
        assert (
            reverse_rejected.value.error_code
            == "library_format_extension_transfer_requires_release"
        )


def test_workspace_override_survives_family_removal_and_explicit_restore_clears_it(
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
            workspace_id=workspace_id
        ).library
        configured = service.update_policy_batch(
            control_session=session,
            workspace_session=session,
            workspace_id=workspace_id,
            actor=_owner(workspace_id),
            group="library",
            changes={
                "builtin_format_overrides": {
                    "docx": {"extensions": [".word"]}
                },
                "format_families": [
                    {
                        "family_id": "documents",
                        "name": "Documents",
                        "origin": "preset",
                        "preset_id": "document",
                        "preset_revision": 1,
                        "max_upload_bytes": None,
                    }
                ],
            },
            expected_revision=before.revision,
        ).library
        baseline = configured.policy["format_families"][0]["preset_baseline"]

        removed = service.update_policy_batch(
            control_session=session,
            workspace_session=session,
            workspace_id=workspace_id,
            actor=_owner(workspace_id),
            group="library",
            changes={"format_families": []},
            expected_revision=configured.revision,
        ).library
        assert removed.policy["builtin_format_overrides"] == {
            "docx": {"extensions": [".word"]}
        }

        restored = service.update_policy_batch(
            control_session=session,
            workspace_session=session,
            workspace_id=workspace_id,
            actor=_owner(workspace_id),
            group="library",
            changes={
                "builtin_format_overrides": {},
                "format_families": [
                    {
                        "family_id": "documents",
                        "name": "Documents",
                        "origin": "preset",
                        "preset_id": "document",
                        "preset_revision": 1,
                        "format_ids": baseline["format_ids"],
                        "preset_baseline": baseline,
                        "max_upload_bytes": None,
                    }
                ],
            },
            expected_revision=removed.revision,
        ).library
        assert restored.policy["builtin_format_overrides"] == {}
        assert restored.policy["format_families"][0]["preset_baseline"] == baseline


def test_historical_preset_alias_snapshot_remains_an_explicit_override():
    service = LibraryFamilyPolicyService()
    historical_baseline = {
        "preset_id": "document",
        "preset_revision": 1,
        "format_ids": ["docx"],
        "format_extensions": {"docx": [".historical-doc"]},
    }
    family = service.coerce_families(
        [
            {
                "family_id": "documents",
                "name": "Documents",
                "origin": "preset",
                "preset_id": "document",
                "preset_revision": 1,
                "format_ids": historical_baseline["format_ids"],
                "preset_baseline": historical_baseline,
                "max_upload_bytes": None,
            }
        ],
        builtin_format_overrides={
            "docx": {
                "extensions": historical_baseline["format_extensions"]["docx"]
            }
        },
    )[0]

    assert family["preset_baseline"] == historical_baseline
    assert service.coerce_builtin_format_overrides(
        {"docx": {"extensions": [".historical-doc"]}}
    ) == {"docx": {"extensions": [".historical-doc"]}}

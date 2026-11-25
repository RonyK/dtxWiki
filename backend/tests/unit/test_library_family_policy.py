import json

import pytest
from sqlalchemy import select

from backend.catalog.library_family_presets.repository import (
    LibraryFamilyPresetCatalogError,
    LibraryFamilyPresetCatalogRepository,
)
from backend.models.workspace import Workspace, default_library_policy
from backend.policy.auth import ActorContext
from backend.schemas.read import LibraryCapabilitiesPayload
from backend.services.converter_registry import build_converter_registry
from backend.services.library_capabilities import LibraryCapabilityService
from backend.services.library_formats import FormatRegistry
from backend.services.library_policy import (
    LibraryFamilyPolicyError,
    LibraryFamilyPolicyService,
)
from backend.services.settings_service import (
    SettingsPolicyUpdateRejected,
    SettingsService,
    library_upload_limit_bytes,
    library_upload_limit_sources,
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


def test_packaged_family_presets_are_valid_disjoint_snapshots():
    policy = LibraryFamilyPolicyService()

    presets = policy.preset_payloads()

    assert [item["preset_id"] for item in presets] == [
        "text",
        "document",
        "image",
        "audio",
        "video",
        "data",
    ]
    assert ".docx" in presets[1]["extensions"]
    assert ".m4v" in presets[4]["extensions"]
    owned = [format_id for item in presets for format_id in item["format_ids"]]
    assert len(owned) == len(set(owned))


def test_preset_catalog_rejects_duplicate_format_ownership(tmp_path):
    payload = {
        "schema_version": 1,
        "catalog_revision": "2026-08-10.99",
        "presets": [
            {
                "preset_id": "one",
                "revision": 1,
                "name": "One",
                "description": "First preset.",
                "format_ids": ["markdown"],
            },
            {
                "preset_id": "two",
                "revision": 1,
                "name": "Two",
                "description": "Second preset.",
                "format_ids": ["markdown"],
            },
        ],
    }
    (tmp_path / "catalog.v1.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    with pytest.raises(LibraryFamilyPresetCatalogError, match="owned by presets"):
        LibraryFamilyPresetCatalogRepository(
            format_registry=FormatRegistry(),
            resource_root=tmp_path,
        ).snapshot()


def _family_policy_with_catalog(tmp_path, *, document_revision: int | None):
    resource_root = tmp_path / (
        "missing-document"
        if document_revision is None
        else f"document-r{document_revision}"
    )
    resource_root.mkdir()
    presets = (
        [
            {
                "preset_id": "image",
                "revision": 1,
                "name": "Images",
                "description": "Image formats.",
                "format_ids": ["jpeg"],
            }
        ]
        if document_revision is None
        else [
            {
                "preset_id": "document",
                "revision": document_revision,
                "name": "Documents",
                "description": "Document formats.",
                "format_ids": (
                    ["docx", "xlsx"]
                    if document_revision == 1
                    else ["docx", "xlsx", "pptx"]
                ),
            }
        ]
    )
    (resource_root / "catalog.v1.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "catalog_revision": (
                    "2026-08-10.1"
                    if document_revision == 1
                    else "2026-08-10.2"
                ),
                "presets": presets,
            }
        ),
        encoding="utf-8",
    )
    registry = FormatRegistry()
    return LibraryFamilyPolicyService(
        format_registry=registry,
        preset_catalog=LibraryFamilyPresetCatalogRepository(
            format_registry=registry,
            resource_root=resource_root,
        ),
    )


def test_extension_input_owns_the_complete_canonical_format():
    policy = LibraryFamilyPolicyService()

    families = policy.coerce_families(
        [
            {
                "family_id": "photos",
                "name": "Photos",
                "extensions": [" JPG "],
                "max_upload_bytes": None,
            }
        ]
    )

    assert families[0]["format_ids"] == ["jpeg"]
    assert policy.configured_family_payloads(families)[0]["extensions"] == [
        ".jpg",
        ".jpeg",
    ]


@pytest.mark.parametrize(
    ("extension", "code"),
    [
        ("exe", "library_family_extension_unsupported"),
        (".hdf", "library_family_extension_ambiguous"),
        ("../docx", "library_family_extension_invalid"),
    ],
)
def test_extension_resolution_fails_closed(extension, code):
    with pytest.raises(LibraryFamilyPolicyError) as rejected:
        LibraryFamilyPolicyService().coerce_families(
            [
                {
                    "family_id": "unsafe",
                    "name": "Unsafe",
                    "extensions": [extension],
                    "max_upload_bytes": None,
                }
            ]
        )

    assert rejected.value.code == code


def test_duplicate_canonical_format_across_families_is_rejected():
    with pytest.raises(LibraryFamilyPolicyError) as rejected:
        LibraryFamilyPolicyService().coerce_families(
            [
                {
                    "family_id": "jpg",
                    "name": "JPG",
                    "extensions": [".jpg"],
                    "max_upload_bytes": None,
                },
                {
                    "family_id": "jpeg",
                    "name": "JPEG",
                    "extensions": [".jpeg"],
                    "max_upload_bytes": None,
                },
            ]
        )

    assert rejected.value.code == "library_family_format_conflict"


def test_preset_membership_customization_retains_immutable_baseline():
    policy = LibraryFamilyPolicyService()
    copied = policy.coerce_families_for_update(
        [
            {
                "family_id": "docs",
                "name": "Docs",
                "preset_id": "document",
                "preset_revision": 1,
                "max_upload_bytes": None,
            }
        ],
        existing_families=[],
    )[0]
    customized = policy.coerce_families_for_update(
        [
            {
                "family_id": "docs",
                "name": "Docs",
                "preset_id": "document",
                "preset_revision": 1,
                "extensions": [".docx"],
                "max_upload_bytes": None,
            }
        ],
        existing_families=[],
    )[0]

    assert copied["preset_id"] == "document"
    assert "docx" in copied["format_ids"]
    assert customized["format_ids"] == ["docx"]
    assert customized["origin"] == "preset"
    assert customized["preset_id"] == "document"
    assert customized["preset_revision"] == 1
    assert customized["preset_baseline"] == copied["preset_baseline"]
    assert set(customized["preset_baseline"]["format_ids"]) > {"docx"}

    normalized = policy.coerce_families([customized])[0]
    assert normalized == customized

    workspace_policy = default_library_policy()
    workspace_policy["format_families"] = [customized]
    projected = LibraryCapabilitiesPayload.model_validate(
        LibraryCapabilityService(
            format_registry=FormatRegistry(),
            converter_registry=build_converter_registry(),
            ingest_enabled=True,
            conversion_enabled=True,
            instance_hard_cap_bytes=50_000_000,
            family_policy=policy,
        ).project(workspace_policy)
    )
    family = projected.families[0]
    assert family.origin == "preset"
    assert family.format_ids == ["docx"]
    assert family.preset_baseline is not None
    assert family.preset_baseline.model_dump() == copied["preset_baseline"]


@pytest.mark.parametrize("existing_family", [False, True], ids=["new", "existing"])
@pytest.mark.parametrize(
    "forgery",
    ["format_ids", "format_extensions"],
)
def test_client_cannot_forge_preset_baseline(existing_family, forgery):
    policy = LibraryFamilyPolicyService()
    copied = policy.coerce_families_for_update(
        [
            {
                "family_id": "docs",
                "name": "Docs",
                "preset_id": "document",
                "preset_revision": 1,
                "max_upload_bytes": None,
            }
        ],
        existing_families=[],
    )[0]
    forged = json.loads(json.dumps(copied["preset_baseline"]))
    if forgery == "format_ids":
        forged["format_ids"] = ["docx"]
        forged["format_extensions"] = {"docx": [".docx"]}
    else:
        forged["format_extensions"]["docx"] = [".forged-docx"]

    with pytest.raises(LibraryFamilyPolicyError) as rejected:
        policy.coerce_families_for_update(
            [
                {
                    "family_id": "docs",
                    "name": "Docs",
                    "origin": "preset",
                    "preset_id": "document",
                    "preset_revision": 1,
                    "preset_baseline": forged,
                    "format_ids": ["docx"],
                    "max_upload_bytes": None,
                }
            ],
            existing_families=[copied] if existing_family else [],
        )
    assert rejected.value.code == "library_family_preset_baseline_conflict"


def test_saved_preset_membership_customization_round_trips_and_restores(
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
        selected = service.update_policy_batch(
            control_session=session,
            workspace_session=session,
            workspace_id=workspace_id,
            actor=_owner(workspace_id),
            group="library",
            changes={
                "format_families": [
                    {
                        "family_id": "documents",
                        "name": "My documents",
                        "origin": "preset",
                        "preset_id": "document",
                        "preset_revision": 1,
                        "max_upload_bytes": 5_000_000,
                    }
                ]
            },
            expected_revision=before.revision,
        ).library
        baseline = selected.policy["format_families"][0]["preset_baseline"]
        forged_baseline = json.loads(json.dumps(baseline))
        forged_baseline["format_extensions"]["docx"] = [".forged-docx"]
        with pytest.raises(SettingsPolicyUpdateRejected) as forged:
            service.update_policy_batch(
                control_session=session,
                workspace_session=session,
                workspace_id=workspace_id,
                actor=_owner(workspace_id),
                group="library",
                changes={
                    "format_families": [
                        {
                            **selected.policy["format_families"][0],
                            "preset_baseline": forged_baseline,
                        }
                    ]
                },
                expected_revision=selected.revision,
            )
        assert forged.value.error_code == "library_family_preset_baseline_conflict"

        customized = service.update_policy_batch(
            control_session=session,
            workspace_session=session,
            workspace_id=workspace_id,
            actor=_owner(workspace_id),
            group="library",
            changes={
                "format_families": [
                    {
                        "family_id": "documents",
                        "name": "My documents",
                        "origin": "preset",
                        "preset_id": "document",
                        "preset_revision": 1,
                        "format_ids": ["docx"],
                        "max_upload_bytes": 5_000_000,
                    }
                ]
            },
            expected_revision=selected.revision,
        ).library
        modified = customized.policy["format_families"][0]
        assert modified["origin"] == "preset"
        assert modified["format_ids"] == ["docx"]
        assert modified["preset_baseline"] == baseline

        round_tripped = service.get_policies(
            control_session=session,
            workspace_session=session,
            workspace_id=workspace_id,
        ).library
        assert round_tripped == customized

        restored = service.update_policy_batch(
            control_session=session,
            workspace_session=session,
            workspace_id=workspace_id,
            actor=_owner(workspace_id),
            group="library",
            changes={
                "format_families": [
                    {
                        **modified,
                        "format_ids": baseline["format_ids"],
                    }
                ]
            },
            expected_revision=round_tripped.revision,
        ).library
        restored_family = restored.policy["format_families"][0]
        assert restored_family["format_ids"] == baseline["format_ids"]
        assert restored_family["preset_baseline"] == baseline
        assert restored_family["name"] == "My documents"
        assert restored_family["max_upload_bytes"] == 5_000_000


def test_family_limit_is_optional_and_workspace_is_the_default():
    policy = default_library_policy()
    assert library_upload_limit_bytes(
        policy,
        format_id="markdown",
    ) == 20_000_000

    policy["format_families"] = [
        {
            "family_id": "documents",
            "name": "Documents",
            "format_ids": ["docx", "xlsx"],
            "max_upload_bytes": 5_000_000,
        }
    ]

    assert library_upload_limit_bytes(policy, format_id="docx") == 5_000_000
    assert library_upload_limit_bytes(policy, format_id="pdf") == 20_000_000
    assert library_upload_limit_sources(policy, format_id="xlsx") == {
        "family": 5_000_000,
    }


def test_capability_projection_exposes_presets_and_configured_family():
    policy = default_library_policy()
    policy["format_families"] = [
        {
            "family_id": "office",
            "name": "Office",
            "origin": "custom",
            "format_ids": ["docx", "xlsx"],
            "max_upload_bytes": 5_000_000,
        }
    ]
    service = LibraryCapabilityService(
        format_registry=FormatRegistry(),
        converter_registry=build_converter_registry(),
        ingest_enabled=True,
        conversion_enabled=True,
        instance_hard_cap_bytes=50_000_000,
    )

    projected = LibraryCapabilitiesPayload.model_validate(service.project(policy))

    assert projected.policy_schema_version == 5
    assert len(projected.family_presets) == 6
    assert len(projected.families) == 1
    family = projected.families[0]
    assert family.family_id == "office"
    assert family.format_ids == ["docx", "xlsx"]
    assert family.extensions == [".docx", ".xlsx"]
    assert family.max_upload_bytes == 5_000_000
    assert family.effective_max_upload_bytes == 5_000_000
    assert family.upload_limiting_source == "family"


@pytest.mark.parametrize(
    ("replacement_revision", "selection_error"),
    [
        (None, "library_family_preset_not_found"),
        (2, "library_family_preset_revision_conflict"),
    ],
    ids=("preset-removed", "preset-advanced"),
)
def test_saved_preset_snapshot_survives_catalog_change(
    session_factory,
    tmp_path,
    monkeypatch,
    replacement_revision,
    selection_error,
):
    original_policy = _family_policy_with_catalog(
        tmp_path,
        document_revision=1,
    )
    original_settings = SettingsService(library_family_policy=original_policy)
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        before = original_settings.get_policies(
            control_session=session,
            workspace_session=session,
            workspace_id=workspace_id,
        ).library
        saved = original_settings.update_policy_batch(
            control_session=session,
            workspace_session=session,
            workspace_id=workspace_id,
            actor=_owner(workspace_id),
            group="library",
            changes={
                "format_families": [
                    {
                        "family_id": "documents",
                        "name": "My documents",
                        "origin": "preset",
                        "preset_id": "document",
                        "preset_revision": 1,
                        "format_ids": ["docx", "xlsx"],
                        "max_upload_bytes": 5_000_000,
                    }
                ]
            },
            expected_revision=before.revision,
        ).library

    replacement_policy = _family_policy_with_catalog(
        tmp_path,
        document_revision=replacement_revision,
    )
    monkeypatch.setattr(
        "backend.services.settings_service.get_default_library_family_policy_service",
        lambda: replacement_policy,
    )
    replacement_settings = SettingsService(library_family_policy=replacement_policy)
    with session_factory.begin() as session:
        current = replacement_settings.get_policies(
            control_session=session,
            workspace_session=session,
            workspace_id=workspace_id,
        ).library

        assert current == saved
        assert current.policy["format_families"] == [
            {
                "family_id": "documents",
                "name": "My documents",
                "origin": "preset",
                "preset_id": "document",
                "preset_revision": 1,
                "format_ids": ["docx", "xlsx"],
                "max_upload_bytes": 5_000_000,
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
        baseline = current.policy["format_families"][0]["preset_baseline"]
        modified = replacement_settings.update_policy_batch(
            control_session=session,
            workspace_session=session,
            workspace_id=workspace_id,
            actor=_owner(workspace_id),
            group="library",
            changes={
                "format_families": [
                    {
                        "family_id": "documents",
                        "name": "My documents",
                        "origin": "preset",
                        "preset_id": "document",
                        "preset_revision": 1,
                        "format_ids": ["docx"],
                        "max_upload_bytes": 5_000_000,
                    }
                ]
            },
            expected_revision=current.revision,
        ).library
        modified_family = modified.policy["format_families"][0]
        assert modified_family["format_ids"] == ["docx"]
        assert modified_family["preset_baseline"] == baseline

        current = replacement_settings.update_policy_batch(
            control_session=session,
            workspace_session=session,
            workspace_id=workspace_id,
            actor=_owner(workspace_id),
            group="library",
            changes={
                "format_families": [
                    {
                        **modified_family,
                        "format_ids": baseline["format_ids"],
                    }
                ]
            },
            expected_revision=modified.revision,
        ).library
        assert current.policy["format_families"][0]["preset_baseline"] == baseline

        with pytest.raises(SettingsPolicyUpdateRejected) as rejected:
            replacement_settings.update_policy_batch(
                control_session=session,
                workspace_session=session,
                workspace_id=workspace_id,
                actor=_owner(workspace_id),
                group="library",
                changes={
                    "format_families": [
                        {
                            "family_id": "documents-reselected",
                            "name": "Documents reselected",
                            "origin": "preset",
                            "preset_id": "document",
                            "preset_revision": 1,
                            "format_ids": ["docx", "xlsx"],
                            "max_upload_bytes": 5_000_000,
                        }
                    ]
                },
                expected_revision=current.revision,
            )
        assert rejected.value.error_code == selection_error

        after_rejection = replacement_settings.get_policies(
            control_session=session,
            workspace_session=session,
            workspace_id=workspace_id,
        ).library
        assert after_rejection == current

    capability_service = LibraryCapabilityService(
        format_registry=FormatRegistry(),
        converter_registry=build_converter_registry(),
        ingest_enabled=True,
        conversion_enabled=True,
        instance_hard_cap_bytes=50_000_000,
        family_policy=replacement_policy,
    )
    capabilities = LibraryCapabilitiesPayload.model_validate(
        capability_service.project(current.policy)
    )
    configured = capabilities.families[0]
    assert configured.origin == "preset"
    assert configured.preset_id == "document"
    assert configured.preset_revision == 1
    assert configured.format_ids == ["docx", "xlsx"]
    assert configured.max_upload_bytes == 5_000_000
    assert library_upload_limit_sources(current.policy, format_id="docx") == {
        "family": 5_000_000,
    }


def test_family_conflict_rejects_the_complete_cas_batch(session_factory):
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
        with pytest.raises(SettingsPolicyUpdateRejected) as rejected:
            service.update_policy_batch(
                control_session=session,
                workspace_session=session,
                workspace_id=workspace_id,
                actor=_owner(workspace_id),
                group="library",
                changes={
                    "max_upload_bytes": 7_000_000,
                    "format_families": [
                        {
                            "family_id": "one",
                            "name": "One",
                            "extensions": [".jpg"],
                            "max_upload_bytes": None,
                        },
                        {
                            "family_id": "two",
                            "name": "Two",
                            "extensions": [".jpeg"],
                            "max_upload_bytes": None,
                        },
                    ],
                },
                expected_revision=before.revision,
            )
        assert rejected.value.error_code == "library_family_format_conflict"

        after = service.get_policies(
            control_session=session,
            workspace_session=session,
            workspace_id=workspace_id,
        ).library
        assert after.policy == before.policy
        assert after.revision == before.revision


def test_family_limit_above_workspace_is_a_valid_override(
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
        updated = service.update_policy_batch(
            control_session=session,
            workspace_session=session,
            workspace_id=workspace_id,
            actor=_owner(workspace_id),
            group="library",
            changes={
                "max_upload_bytes": 7_000_000,
                "format_families": [
                    {
                        "family_id": "office",
                        "name": "Office",
                        "extensions": [".docx", ".xlsx"],
                "max_upload_bytes": 80_000_000,
                    }
                ],
            },
            expected_revision=before.revision,
        ).library
        assert updated.policy["max_upload_bytes"] == 7_000_000
        assert library_upload_limit_bytes(
            updated.policy,
            format_id="docx",
        ) == 80_000_000

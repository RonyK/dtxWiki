from copy import deepcopy

import pytest
from sqlalchemy import select

from backend.models.workspace import (
    NEW_LIBRARY_MAX_UPLOAD_BYTES,
    Workspace,
    default_library_policy,
)
from backend.policy.auth import ActorContext
from backend.services.library_formats import DEFAULT_FORMATS
from backend.services.settings_service import (
    DEFAULT_LIBRARY_UPLOAD_BYTES,
    LibrarySourceRulesRetired,
    SettingsPolicyRevisionConflict,
    SettingsPolicyUpdateRejected,
    SettingsService,
    library_format_is_enabled,
    library_upload_limit_bytes,
    library_upload_limit_sources,
    normalized_library_policy,
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


def test_new_workspace_policy_is_one_simple_v5_shape():
    first = default_library_policy()
    second = default_library_policy()

    assert first["max_upload_bytes"] == NEW_LIBRARY_MAX_UPLOAD_BYTES == 20_000_000
    assert DEFAULT_LIBRARY_UPLOAD_BYTES == 20_000_000
    assert first["policy_schema_version"] == 5
    assert first["format_overrides"] == {}
    assert first["builtin_format_overrides"] == {}
    assert first["format_families"] == []
    assert first["custom_formats"] == []
    assert first["allow_store_without_converter"] is True
    for removed in (
        "category_limits",
        "enabled_formats",
        "family_modes",
        "format_exceptions",
        "profile_id",
        "profile_revision",
    ):
        assert removed not in first

    first["format_families"].append({"family_id": "changed"})
    first["custom_formats"].append({"format_id": "custom-changed"})
    assert second["format_families"] == []
    assert second["custom_formats"] == []


def test_new_workspace_row_persists_the_simple_v5_policy(session_factory):
    with session_factory.begin() as session:
        workspace = Workspace(
            slug="expanded-policy",
            title="Expanded policy",
            name="Expanded policy",
        )
        session.add(workspace)
        session.flush()

        assert workspace.library_policy == default_library_policy()


def test_runtime_normalization_has_no_legacy_mode_and_does_not_mutate_input():
    stored = {
        "max_upload_bytes": 2_000_000,
        "category_limits": {"document": 50_000_000},
        "profile_id": "restricted",
        "enabled_formats": ["md"],
        "allowed_sources": [{"type": "web", "pattern": "https://old.test/*"}],
        "future_policy": {"mode": "keep"},
    }
    before = deepcopy(stored)

    normalized = normalized_library_policy(stored)

    assert stored == before
    assert normalized["policy_schema_version"] == 5
    assert normalized["max_upload_bytes"] == 2_000_000
    assert normalized["future_policy"] == {"mode": "keep"}
    for removed in (
        "category_limits",
        "profile_id",
        "enabled_formats",
        "allowed_sources",
    ):
        assert removed not in normalized
    assert library_upload_limit_bytes({}) == 20_000_000


def test_preference_precedence_replaces_workspace_then_obeys_hard_cap():
    policy = default_library_policy()
    policy["format_families"] = [
        {
            "family_id": "large-documents",
            "name": "Large documents",
            "origin": "custom",
            "format_ids": ["docx", "xlsx"],
            "max_upload_bytes": 45_000_000,
        }
    ]

    assert library_upload_limit_bytes(policy, format_id="docx") == 45_000_000
    assert library_upload_limit_sources(policy, format_id="docx") == {
        "family": 45_000_000
    }
    policy["format_overrides"] = {"docx": 35_000_000}
    assert library_upload_limit_bytes(policy, format_id="docx") == 35_000_000
    assert library_upload_limit_sources(policy, format_id="docx") == {
        "format": 35_000_000
    }
    assert (
        library_upload_limit_bytes(
            policy,
            format_id="docx",
            instance_hard_cap_bytes=30_000_000,
        )
        == 30_000_000
    )
    assert library_upload_limit_bytes(policy, format_id="pdf") == 20_000_000

    with pytest.raises(ValueError, match="positive integer"):
        library_upload_limit_bytes(policy, instance_hard_cap_bytes=True)


def test_exact_preference_may_exceed_workspace_bound_but_not_instance_cap():
    policy = default_library_policy()
    policy["format_overrides"] = {"mp4": 300_000_000}

    assert library_upload_limit_sources(
        policy,
        format_id="mp4",
        instance_hard_cap_bytes=200_000_000,
    ) == {
        "format": 300_000_000,
        "instance": 200_000_000,
    }
    assert (
        library_upload_limit_bytes(
            policy,
            format_id="mp4",
            instance_hard_cap_bytes=200_000_000,
        )
        == 200_000_000
    )


def test_persisted_current_values_are_bounded_and_legacy_keys_are_removed():
    policy = normalized_library_policy(
        {
            "max_upload_bytes": "invalid",
            "category_limits": {"text": 1_000},
            "format_overrides": {
                "PDF": 9_000,
                "markdown": 4_000,
                "invalid": False,
            },
            "manual_approval_required": 0,
            "allow_store_without_converter": "true",
            "future_policy": "retained",
        }
    )

    assert policy["max_upload_bytes"] == DEFAULT_LIBRARY_UPLOAD_BYTES
    assert policy["format_overrides"] == {"pdf": 9_000, "md": 4_000}
    assert policy["manual_approval_required"] is True
    assert policy["allow_store_without_converter"] is True
    assert policy["future_policy"] == "retained"
    assert "category_limits" not in policy


def test_every_server_format_is_policy_enabled_but_validator_remains_the_gate():
    policy = default_library_policy()
    for descriptor in DEFAULT_FORMATS:
        assert library_format_is_enabled(policy, descriptor.format_id) is True


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("category_limits", {"text": 1_000}, "cannot be edited"),
        ("profile_id", "restricted", "cannot be edited"),
        ("enabled_formats", ["pdf"], "cannot be edited"),
        ("format_overrides", {"exe": 1_000}, "unknown key"),
        ("format_overrides.pdf", "1000", "integer"),
        ("allow_store_without_converter", 1, "boolean"),
    ],
)
def test_settings_rejects_removed_or_invalid_policy_fields(
    session_factory,
    key,
    value,
    message,
):
    service = SettingsService()
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        with pytest.raises(SettingsPolicyUpdateRejected, match=message):
            service.update_policy(
                control_session=session,
                workspace_session=session,
                workspace_id=workspace_id,
                actor=_owner(workspace_id),
                group="library",
                key=key,
                value=value,
            )


@pytest.mark.parametrize(
    "key",
    ["allowed_sources", "denied_sources", "source_patterns"],
)
def test_retired_library_source_rules_keep_the_specific_error(
    session_factory,
    key,
):
    service = SettingsService()
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        with pytest.raises(LibrarySourceRulesRetired) as rejected:
            service.update_policy(
                control_session=session,
                workspace_session=session,
                workspace_id=workspace_id,
                actor=_owner(workspace_id),
                group="library",
                key=key,
                value=[],
            )
        assert rejected.value.error_code == "library_source_rules_retired"


def test_library_batch_update_is_atomic_and_revision_guarded(session_factory):
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
                "format_overrides": {"pdf": 6_000_000},
            },
            expected_revision=before.revision,
        ).library

        assert updated.policy["max_upload_bytes"] == 7_000_000
        assert updated.policy["format_overrides"] == {"pdf": 6_000_000}
        with pytest.raises(SettingsPolicyRevisionConflict):
            service.update_policy_batch(
                control_session=session,
                workspace_session=session,
                workspace_id=workspace_id,
                actor=_owner(workspace_id),
                group="library",
                changes={"max_upload_bytes": 8_000_000},
                expected_revision=before.revision,
            )


def test_settings_separates_workspace_and_format_preference_ceilings(
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
            changes={"format_overrides": {"mp4": 300_000_000}},
            expected_revision=before.revision,
        ).library
        assert updated.policy["format_overrides"] == {"mp4": 300_000_000}

        with pytest.raises(SettingsPolicyUpdateRejected, match="at most 50000000"):
            service.update_policy_batch(
                control_session=session,
                workspace_session=session,
                workspace_id=workspace_id,
                actor=_owner(workspace_id),
                group="library",
                changes={"max_upload_bytes": 50_000_001},
                expected_revision=updated.revision,
            )


def test_library_batch_rejection_does_not_partially_mutate_policy(session_factory):
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

        with pytest.raises(SettingsPolicyUpdateRejected):
            service.update_policy_batch(
                control_session=session,
                workspace_session=session,
                workspace_id=workspace_id,
                actor=_owner(workspace_id),
                group="library",
                changes={
                    "max_upload_bytes": 7_000_000,
                    "format_overrides.exe": 2_000_000,
                },
                expected_revision=before.revision,
            )

        after = service.get_policies(
            control_session=session,
            workspace_session=session,
            workspace_id=workspace_id,
        ).library
        assert after == before

from uuid import uuid4

from backend.app.settings import Settings
from backend.mcp.maintenance_wire import (
    MrtrPreviewWireAdapter,
    StableWireAdapter,
    select_adapter,
)
from backend.services.maintenance_rounds import (
    UNTRUSTED_CONTEXT_NOTICE,
    DelegatedTaskCard,
    MaintenanceRound,
)


def _round():
    workspace_id = uuid4()
    task_id = uuid4()
    card = DelegatedTaskCard(
        task_id=task_id,
        task_type="folder_nav_summary",
        target_kind="workspace_tree_node",
        target_id=uuid4(),
        instruction="Write a summary.",
        untrusted_context=f"{UNTRUSTED_CONTEXT_NOTICE}\nFolder title: Payments",
        response_schema={"type": "object"},
        estimated_tokens=12,
        truncated=False,
    )
    return MaintenanceRound(
        workspace_id=workspace_id,
        tasks=(card,),
        resume_token="signed-state",
        remaining_pending=3,
    )


def test_stable_wire_shape_is_completion_fallback():
    rendered = StableWireAdapter().render_round(_round())

    assert set(rendered) == {
        "wire_contract",
        "maintenance_tasks",
        "instructions",
        "resume_token",
        "remaining_pending",
    }
    assert rendered["wire_contract"] == "stable_2025_11_25"
    assert set(rendered["maintenance_tasks"][0]) == {
        "task_id",
        "task_type",
        "instruction",
        "untrusted_context",
        "response_schema",
    }
    assert rendered["resume_token"] == "signed-state"


def test_preview_wire_shape_is_isolated_to_input_required_block():
    round_ = _round()
    rendered = MrtrPreviewWireAdapter().render_round(round_)

    assert set(rendered) == {
        "wire_contract",
        "input_required",
        "remaining_pending",
    }
    assert rendered["wire_contract"] == "mrtr_preview_2026_07_28"
    assert set(rendered["input_required"]) == {"inputRequests", "requestState"}
    request = rendered["input_required"]["inputRequests"][0]
    assert set(request) == {
        "id",
        "kind",
        "taskType",
        "instruction",
        "untrustedContext",
        "responseSchema",
    }
    assert request["id"] == str(round_.tasks[0].task_id)
    assert request["untrustedContext"].startswith(UNTRUSTED_CONTEXT_NOTICE)
    assert rendered["input_required"]["requestState"] == "signed-state"


def test_preview_requires_feature_flag_and_falls_back_to_stable():
    adapter, warnings = select_adapter(
        Settings(mcp_preview_2026_07_28=False),
        "mrtr_preview",
    )

    assert isinstance(adapter, StableWireAdapter)
    assert warnings == ["mrtr_preview_unavailable"]

    adapter, warnings = select_adapter(
        Settings(mcp_preview_2026_07_28=True),
        "mrtr_preview",
    )
    assert isinstance(adapter, MrtrPreviewWireAdapter)
    assert warnings == []

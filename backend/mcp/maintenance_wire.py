from __future__ import annotations

from typing import Any, Protocol

from backend.app.settings import Settings
from backend.services.maintenance_rounds import MaintenanceRound

STABLE_INSTRUCTIONS = (
    "Perform each task with your own reasoning. Then call wiki_maintenance "
    "again with the SAME workspace_id, resume_token exactly as given, and "
    "input_responses keyed by task_id. Batch <= 8 tasks; ~2,000 tokens context "
    "per task; token cost is borne by you (the client). Answers are staged for "
    "human review - nothing is applied automatically."
)


class MaintenanceWireAdapter(Protocol):
    wire_contract: str

    def render_round(self, round_: MaintenanceRound) -> dict[str, Any]: ...

    def render_drained(self, remaining: int) -> dict[str, Any]: ...


class StableWireAdapter:
    wire_contract = "stable_2025_11_25"

    def render_round(self, round_: MaintenanceRound) -> dict[str, Any]:
        if not round_.tasks:
            return self.render_drained(round_.remaining_pending)
        return {
            "wire_contract": self.wire_contract,
            "maintenance_tasks": [
                {
                    "task_id": str(card.task_id),
                    "task_type": card.task_type,
                    "instruction": card.instruction,
                    "untrusted_context": card.untrusted_context,
                    "response_schema": card.response_schema,
                }
                for card in round_.tasks
            ],
            "instructions": STABLE_INSTRUCTIONS,
            "resume_token": round_.resume_token,
            "remaining_pending": round_.remaining_pending,
        }

    def render_drained(self, remaining: int) -> dict[str, Any]:
        return {
            "wire_contract": self.wire_contract,
            "maintenance_tasks": [],
            "instructions": "No maintenance tasks pending.",
            "resume_token": None,
            "remaining_pending": remaining,
        }


class MrtrPreviewWireAdapter:
    wire_contract = "mrtr_preview_2026_07_28"

    def render_round(self, round_: MaintenanceRound) -> dict[str, Any]:
        if not round_.tasks:
            return self.render_drained(round_.remaining_pending)
        return {
            "wire_contract": self.wire_contract,
            "input_required": {
                "inputRequests": [
                    {
                        "id": str(card.task_id),
                        "kind": "generation",
                        "taskType": card.task_type,
                        "instruction": card.instruction,
                        "untrustedContext": card.untrusted_context,
                        "responseSchema": card.response_schema,
                    }
                    for card in round_.tasks
                ],
                "requestState": round_.resume_token,
            },
            "remaining_pending": round_.remaining_pending,
        }

    def render_drained(self, remaining: int) -> dict[str, Any]:
        return {
            "wire_contract": self.wire_contract,
            "input_required": None,
            "remaining_pending": remaining,
        }


def select_adapter(
    settings: Settings,
    wire: str,
) -> tuple[MaintenanceWireAdapter, list[str]]:
    if wire == "mrtr_preview" and settings.mcp_preview_2026_07_28:
        return MrtrPreviewWireAdapter(), []
    if wire == "mrtr_preview":
        return StableWireAdapter(), ["mrtr_preview_unavailable"]
    return StableWireAdapter(), []

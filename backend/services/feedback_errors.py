"""Typed exceptions raised by feedback services."""

from __future__ import annotations


class FeedbackError(Exception):
    """Base class for feedback subsystem failures."""


class FeedbackItemNotFoundError(FeedbackError):
    """Raised after deterministic lookup confirms the item is absent."""

    def __init__(self, feedback_item_id: str, workspace_id: str) -> None:
        super().__init__(
            f"feedback item {feedback_item_id} not found in workspace {workspace_id}"
        )
        self.feedback_item_id = feedback_item_id
        self.workspace_id = workspace_id

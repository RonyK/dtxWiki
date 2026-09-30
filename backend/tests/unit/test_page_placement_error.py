from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.services import page_service as page_service_module
from backend.services.page_service import PageService, PageValidationError
from backend.services.read_errors import RejectedByPolicyError


def test_page_placement_preserves_tree_error_code(monkeypatch):
    def reject_placement(**_kwargs):
        raise RejectedByPolicyError(
            "system node cannot be used as a placement parent",
            error_code="system_tree_node_protected",
        )

    monkeypatch.setattr(
        page_service_module, "ensure_primary_tree_placement", reject_placement
    )

    with pytest.raises(PageValidationError) as caught:
        PageService._ensure_context_tree_placement(
            session=object(),
            workspace_id=uuid4(),
            page=SimpleNamespace(id=uuid4(), title="Page", slug="page"),
            parent_id=uuid4(),
            actor_kind="human",
            actor_id=str(uuid4()),
        )

    # The wire code stays the one a 422 carries; the tree's own code is kept
    # beside it so the reason is not lost.
    assert caught.value.error_code == "validation_failed"
    assert caught.value.cause_error_code == "system_tree_node_protected"
    assert "page write was rolled back" in str(caught.value)

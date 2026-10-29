from unittest.mock import Mock
from uuid import uuid4

from sqlalchemy.orm import Session

from backend.models.change import ChangeSet
from backend.models.tree import WorkspaceTreeNode
from backend.schemas.tree import TreeReorderRequest
from backend.services import tree_service


def test_move_completed_while_waiting_for_locks_becomes_a_sibling_reorder(monkeypatch):
    workspace_id, source_id, destination_id = uuid4(), uuid4(), uuid4()

    def node(title, parent_id, sort_key):
        return WorkspaceTreeNode(
            id=uuid4(),
            workspace_id=workspace_id,
            section="context",
            node_kind="folder",
            title=title,
            parent_id=parent_id,
            sort_key=sort_key,
            is_system_node=False,
            visibility_status="visible",
            can_have_children=True,
        )

    destination = node("Destination", source_id, "0001")
    destination.id = destination_id
    first = node("First anchor", destination_id, "0001")
    last = node("Last anchor", destination_id, "0002")
    moving = node("Moving", source_id, "0002")
    nodes = {value.id: value for value in (destination, first, last, moving)}
    session = Mock(spec=Session)
    session.get_bind.return_value.dialect.name = "postgresql"
    service = tree_service.TreeService(audit_service=Mock(), tree_read_service=Mock())
    service.tree_read_service.get_node.return_value = moving
    service._record_tree_move = Mock(return_value=moving)
    monkeypatch.setattr(
        tree_service,
        "require_visible_tree_node",
        lambda session, workspace_id, node_id, actor, reader: nodes[node_id],
    )
    monkeypatch.setattr(
        tree_service,
        "create_tree_change_set",
        lambda **kwargs: ChangeSet(id=uuid4(), workspace_id=workspace_id),
    )
    monkeypatch.setattr(tree_service, "is_descendant", lambda *args: False)

    def lock_after_competing_move(*args):
        # Model the refreshed destination after another transaction places the
        # same node last while this request waits for the parent locks.
        moving.parent_id = destination_id
        moving.sort_key = "0003"
        return [first, last, moving]

    monkeypatch.setattr(
        tree_service, "_lock_ordered_children", lock_after_competing_move
    )

    service.reorder_node(
        session,
        workspace_id,
        TreeReorderRequest.model_validate(
            {
                "node_id": moving.id,
                "anchor_node_id": first.id,
                "placement": "before",
            }
        ),
        actor_kind="human",
        actor_id="tester",
    )

    ordered = sorted((first, last, moving), key=lambda value: value.sort_key)
    assert [value.id for value in ordered] == [moving.id, first.id, last.id]
    assert [value.sort_key for value in ordered] == ["0001", "0002", "0003"]
    assert service.audit_service.emit.call_args.kwargs["action"] == "tree.node.reorder"

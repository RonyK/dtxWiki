from types import SimpleNamespace
from uuid import UUID

from backend.services.context_reconciliation import _lineage_distance


class _LineageSession:
    def __init__(self, rows):
        self.rows = rows
        self.info = {}
        self.execute_count = 0
        self.transaction = object()

    def execute(self, _statement):
        self.execute_count += 1
        return self.rows

    def get_transaction(self):
        return self.transaction


def test_lineage_distance_accepts_long_valid_history_with_one_query():
    workspace_id = UUID("00000000-0000-4000-8000-000000000001")
    page_id = UUID("00000000-0000-4000-8000-000000000002")
    version_ids = [UUID(int=index + 1) for index in range(10_002)]
    rows = [
        (
            version_id,
            version_ids[index - 1] if index else None,
            "linear",
        )
        for index, version_id in enumerate(version_ids)
    ]
    session = _LineageSession(rows)
    ancestor = SimpleNamespace(
        id=version_ids[0],
        workspace_id=workspace_id,
        page_id=page_id,
    )
    descendant = SimpleNamespace(
        id=version_ids[-1],
        workspace_id=workspace_id,
        page_id=page_id,
    )

    assert (
        _lineage_distance(
            session,
            ancestor=ancestor,
            descendant=descendant,
        )
        == 10_001
    )
    assert session.execute_count == 1

    # A second branch on the same page reuses the transaction-local projection.
    assert (
        _lineage_distance(
            session,
            ancestor=ancestor,
            descendant=descendant,
        )
        == 10_001
    )
    assert session.execute_count == 1

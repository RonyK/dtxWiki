from types import SimpleNamespace
from uuid import uuid4

from backend.services.context_lifecycle import (
    CLOSE_REASON_MERGED,
    CLOSE_REASON_REJECTED,
    CLOSE_REASON_WITHDRAWN,
    context_closed_reasons,
)
from backend.services.context_version_refs import context_version_ref


class _Session:
    """Answers the close-operation query with fixed (diff_ref, summary) rows."""

    def __init__(self, rows):
        self.rows = rows

    def execute(self, _statement):
        return list(self.rows)


def test_closed_reasons_come_from_close_records_then_rejected_reviews():
    workspace_id = uuid4()

    def version(status="closed"):
        return SimpleNamespace(
            id=uuid4(), workspace_id=workspace_id, version_status=status
        )

    discarded, merged, rejected, unknown, rejected_review, plain, draft = (
        version(),
        version(),
        version(),
        version(),
        version(),
        version(),
        version("draft"),
    )
    session = _Session(
        [
            (context_version_ref(discarded.id), CLOSE_REASON_WITHDRAWN),
            (context_version_ref(merged.id), CLOSE_REASON_MERGED),
            (context_version_ref(rejected.id), CLOSE_REASON_REJECTED),
            (context_version_ref(unknown.id), "Some other close"),
        ]
    )
    reasons = context_closed_reasons(
        session,
        [discarded, merged, rejected, unknown, rejected_review, plain, draft],
        # Closed without a record (converted history, an undone approval):
        # only a rejected review gives a reason.
        dispositions={rejected_review.id: "rejected", plain.id: "approved"},
    )
    assert reasons == {
        discarded.id: "discarded",
        merged.id: "merged",
        rejected.id: "rejected",
        rejected_review.id: "rejected",
    }

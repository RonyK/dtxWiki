import inspect

import pytest

from backend.services.agent_page_service import AgentPageService
from backend.services.change_review_audit import ChangeReviewAuditRecorder
from backend.services.context_write_service import ContextWriteService
from backend.services.page_service import PageService
from backend.services.review_risk_policy import ReviewRiskResolver
from backend.services.review_service import ReviewService


@pytest.mark.parametrize(
    ("method", "parameter"),
    [
        (PageService.restore_version, "actor_provenance"),
        (PageService.create_inline_comment, "actor_provenance"),
        (PageService.write_page, "actor_provenance"),
        (PageService.update_page, "actor_provenance"),
        (PageService.autosave_page, "actor_provenance"),
        (AgentPageService.write_draft, "actor_provenance"),
        (AgentPageService.restore_version, "actor_provenance"),
        (AgentPageService.activate, "actor_role"),
        (ContextWriteService.write_draft, "actor_role"),
        (ReviewService.apply_decision, "actor_provenance"),
        (ChangeReviewAuditRecorder.record_context_page_write, "actor_provenance"),
        (
            ChangeReviewAuditRecorder.record_context_page_alternative,
            "actor_provenance",
        ),
        (
            ChangeReviewAuditRecorder.record_context_page_inline_comment,
            "actor_provenance",
        ),
        (ChangeReviewAuditRecorder.record_library_item_deleted, "actor_provenance"),
    ],
)
def test_authenticated_write_boundary_has_no_permissive_default(method, parameter):
    assert (
        inspect.signature(method).parameters[parameter].default
        is inspect.Parameter.empty
    )


def test_internal_risk_resolver_default_is_fail_closed():
    parameter = inspect.signature(ReviewRiskResolver.resolve).parameters[
        "actor_provenance"
    ]
    assert parameter.default == "internal"

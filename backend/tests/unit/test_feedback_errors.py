from backend.services.feedback_errors import FeedbackItemNotFoundError


def test_feedback_item_not_found_error_carries_ids():
    err = FeedbackItemNotFoundError(
        feedback_item_id="abc",
        workspace_id="xyz",
    )

    assert err.feedback_item_id == "abc"
    assert err.workspace_id == "xyz"
    assert "abc" in str(err)
    assert "xyz" in str(err)

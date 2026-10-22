class ReadServiceError(RuntimeError):
    error_code = "internal_error"


class ReadNotFoundError(ReadServiceError):
    error_code = "not_found"


class ScopeDeniedError(ReadServiceError):
    error_code = "scope_denied"


class DuplicateSlugError(ReadServiceError):
    error_code = "duplicate_slug_under_parent"


class PageNameOccupiedError(ReadServiceError):
    """A Context name is occupied by a page the writer cannot see."""

    error_code = "page_name_occupied"

    def __init__(self) -> None:
        super().__init__(
            "page name is occupied by content this actor is not allowed to edit"
        )


class RejectedByPolicyError(ReadServiceError):
    error_code = "rejected_by_policy"

    def __init__(
        self,
        message: str = "workspace access denied",
        *,
        error_code: str | None = None,
        warnings: list | None = None,
    ) -> None:
        super().__init__(message)
        if error_code is not None:
            self.error_code = error_code
        # A refusal the caller can act on carries the defects it found, in the
        # caller's own coordinates. The REST layer relays these through the
        # error body's warnings channel so a client can render them instead of
        # re-deriving why the write was refused.
        self.warnings = list(warnings or [])


class ReadValidationError(ReadServiceError):
    error_code = "validation_failed"

    def __init__(
        self,
        message: str = "validation failed",
        *,
        error_code: str | None = None,
        warnings: list | None = None,
        details: list | None = None,
    ) -> None:
        super().__init__(message)
        if error_code is not None:
            self.error_code = error_code
        # Same channel as RejectedByPolicyError: a refusal the caller can act on
        # carries the facts it found. A refused patch uses this to report the
        # actual match count and locations, so the caller can retry without
        # re-reading a document it never held.
        self.warnings = list(warnings or [])
        # MCPErrorDetail entries naming the offending operation(s) of a batch.
        self.details = list(details or [])


class StaleBaseVersionError(ReadServiceError):
    error_code = "stale_base_version"

    def __init__(
        self,
        message: str = "base version is stale",
        latest_version_id=None,
        latest_content_hash: str | None = None,
    ) -> None:
        super().__init__(message)
        self.latest_version_id = latest_version_id
        self.latest_content_hash = latest_content_hash


class StaleContextDraftError(RejectedByPolicyError):
    """A Context publication target is not the latest linear eligible draft."""

    error_code = "stale_context_draft"

    def __init__(
        self,
        message: str = "context draft is stale or is not the latest linear draft",
    ) -> None:
        super().__init__(message)


class IdempotencyKeyConflictError(ReadServiceError):
    error_code = "idempotency_key_conflict"

    def __init__(self) -> None:
        super().__init__(
            "idempotency_key was already used for different arguments within 24 hours"
        )


class OperationOutcomeUnknownError(ReadServiceError):
    """``retry_only`` found no valid (unexpired) receipt for the given key.

    This is a read-only answer, not a license to execute: a caller in doubt
    about whether its own prior send landed must not treat "no receipt" as
    "safe to try for real" -- it re-sends the same idempotency_key WITHOUT
    ``retry_only`` only once it has decided that on its own terms.
    """

    error_code = "operation_outcome_unknown"


class BundleExpiredError(ReadServiceError):
    error_code = "bundle_expired"


class UnsupportedRetrievalModeError(ReadServiceError):
    error_code = "unsupported_retrieval_mode"


class ChatOwnerUnresolvableError(ReadServiceError):
    """request_changes routed to chat_owner but no owner could be resolved
    (no origin chat session, or its owner_user_id is NULL). Maps to 422."""

    error_code = "chat_owner_unresolvable"


class ReviewAlreadyClaimedError(ReadServiceError):
    """A second actor tried to claim a review item already claimed by another
    principal within the claim TTL. Maps to 409."""

    error_code = "review_already_claimed"


class ReviewDecisionImpactChangedError(ReadServiceError):
    """A rejection would now do more or less than the actor confirmed."""

    error_code = "review_decision_impact_changed"

    def __init__(self) -> None:
        super().__init__(
            "the rejection's impact changed after it was previewed; "
            "check the updated impact and decide again"
        )


class ReviewRejectBlockedError(ReadServiceError):
    """A private edit sits on the drafts a rejection would retire."""

    error_code = "review_reject_blocked_by_private_edit"

    def __init__(self) -> None:
        super().__init__(
            "a private edit is in progress on this draft; it must be submitted "
            "or discarded before the review can be rejected"
        )


class ReviewDecisionIdConflictError(ReadServiceError):
    """A decision receipt ID was reused for a different authenticated request."""

    error_code = "decision_id_conflict"

    def __init__(self) -> None:
        super().__init__(
            "decision_id was already used for a different review decision"
        )


class RevisionSubmissionRequiredError(ReadServiceError):
    error_code = "revision_submission_required"

    def __init__(self, review_item_id) -> None:
        super().__init__(
            "this page has an active change request; submit a revision through "
            "submit_revision_for_review"
        )
        self.review_item_id = review_item_id

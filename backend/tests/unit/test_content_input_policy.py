import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.domain.input_policy import (
    InputPolicyError,
    fit_generated_title,
    validate_title,
)
from backend.domain.prepared_body import PreparedBody
from backend.models.context import ContextPage, ContextPageVersion
from backend.schemas.agent import AgentPageDraftRequest, BuildBundleRequest
from backend.schemas.ask import AskTurnCreate
from backend.schemas.context_ingress import ContextIngressAcceptRequest
from backend.schemas.context_write import SubmitFeedbackRequest
from backend.schemas.library_semantic import LibrarySemanticMarkdownUpdateRequest
from backend.schemas.mcp.review_tools import SubmitRevisionForReviewInput
from backend.schemas.page import ContextPageCreateRequest
from backend.schemas.tree import TreeFolderCreateRequest, TreeNodeUpdate
from backend.services import context_ingress_service
from backend.services.ask_service import InvalidUserMessageError, _validate_user_message
from backend.services.body_preparer import (
    BodyInput,
    BodyPreparer,
    PageMetadataInput,
    WriteContext,
)
from backend.services.bundle_service import AgentInputBundleService
from backend.services.context_write_service import ContextWriteService
from backend.services.library_semantic_overlay_service import (
    LibrarySemanticOverlayService,
)
from backend.services.page_service import PageService
from backend.services.review_service import ReviewService
from backend.services.tree_service import TreeService
from backend.utils.page_frontmatter import (
    parse_page_frontmatter,
    validate_frontmatter_input,
    validate_page_markdown_input,
)


def test_titles_count_code_points_and_preserve_explicit_unicode():
    assert validate_title(" " + "😀" * 256 + " ") == "😀" * 256
    with pytest.raises(InputPolicyError):
        validate_title("😀" * 257)
    for value in ("a\nb", "a\ud800b"):
        with pytest.raises(InputPolicyError):
            validate_title(value)


def test_generated_titles_fit_policy_without_splitting_display_sequences():
    family = "👨‍👩‍👧‍👦"
    assert fit_generated_title("a" * 95 + " trailing", max_length=96) == "a" * 95
    assert fit_generated_title("a" * 115 + family, max_length=120) == "a" * 115
    for cluster in (
        "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",
        "\u1112\u1161\u11ab",  # decomposed Hangul syllable
        "\u0e01\u0e33",  # Thai consonant + SARA AM
    ):
        prefix = "a" * (120 - len(cluster) + 1)
        assert fit_generated_title(prefix + cluster, max_length=120) == prefix
    assert fit_generated_title("\t\x00\ud800\n", fallback="Fallback") == "Fallback"
    assert fit_generated_title("one\ntwo\tthree") == "one two three"
    # A promoted Ask turn without an explicit title names the page after the
    # question, which may span lines.
    session = SimpleNamespace(title="Session")
    turn = SimpleNamespace(user_message="Summarize:\n" + "x" * 60, turn_no=2)
    assert context_ingress_service.inferred_title(session, turn) == (
        "Summarize: " + "x" * 49
    )


def test_tree_folder_and_linked_title_requests_have_distinct_budgets():
    TreeNodeUpdate(display_name="가" * 256)
    with pytest.raises(ValidationError):
        TreeFolderCreateRequest(section="context", title="가" * 81)


def test_direct_service_checks_constructed_requests_before_database_work():
    request = TreeNodeUpdate.model_construct(display_name="x" * 257)
    with pytest.raises(InputPolicyError):
        TreeService().update_node(
            session=None,
            workspace_id=uuid4(),
            node_id=uuid4(),
            request=request,
            actor_kind="human",
            actor_id=str(uuid4()),
        )


def test_library_overlay_rejects_invalid_unicode_before_encoding_or_database():
    payload = dict(
        body_markdown="text\ud800tail",
        expected_revision=0,
        expected_base_artifact_id=uuid4(),
        expected_base_content_sha256="a" * 64,
    )
    with pytest.raises(ValidationError):
        LibrarySemanticMarkdownUpdateRequest(**payload)
    with pytest.raises(InputPolicyError):
        LibrarySemanticOverlayService().update_current(
            session=None,
            workspace_id=uuid4(),
            version_id=uuid4(),
            actor=None,
            **payload,
        )


def test_agent_request_rejects_oversized_authored_fields():
    payload = dict(
        category="skill",
        title="Skill",
        slug="skill",
        body_markdown="Body",
        reason="Create",
    )
    for field, value in (
        ("title", "x" * 257),
        ("reason", "x" * 2001),
        ("body_markdown", "x" * 1_000_001),
    ):
        with pytest.raises(ValidationError):
            AgentPageDraftRequest(**{**payload, field: value})


def test_feedback_and_bundle_request_boundaries():
    SubmitFeedbackRequest(
        target_type="context_page", target_id=uuid4(), body="가" * 10000
    )
    with pytest.raises(ValidationError):
        SubmitFeedbackRequest(
            target_type="context_page", target_id=uuid4(), body="가" * 10001
        )
    payload = dict(
        task_intent="Read",
        client_id=uuid4(),
        token_budget=512,
        context_refs=[uuid4() for _ in range(128)],
    )
    BuildBundleRequest(**payload)
    payload["context_refs"].append(uuid4())
    with pytest.raises(ValidationError):
        BuildBundleRequest(**payload)
    payload["context_refs"].pop()
    payload["requested_categories"] = ["agent_guide", "persona", "skill", "memory"]
    BuildBundleRequest(**payload)
    payload["requested_categories"].append("persona")
    with pytest.raises(ValidationError):
        BuildBundleRequest(**payload)
    payload["requested_categories"] = ["skills"]
    with pytest.raises(ValidationError):
        BuildBundleRequest(**payload)
    with pytest.raises(InputPolicyError):
        AgentInputBundleService().build(
            session=None,
            workspace_id=uuid4(),
            request=BuildBundleRequest.model_construct(**payload),
            actor=None,
        )


def test_frontmatter_rejects_raw_count_before_read_parser_deduplicates():
    markdown = "---\ntags: [" + ",".join(['"same"'] * 33) + "]\n---\nBody"
    with pytest.raises(InputPolicyError):
        validate_frontmatter_input(markdown)
    with pytest.raises(ValidationError):
        ContextPageCreateRequest(body_md=markdown)


def test_valid_frontmatter_quoted_commas_and_escapes_round_trip_without_loss():
    values = [f'alpha{index},"beta"\\suffix' for index in range(31)]
    for field in ("tags", "aliases"):
        markdown = f"---\n{field}: {json.dumps(values)}\n---\nBody"
        validate_frontmatter_input(markdown)
        assert getattr(parse_page_frontmatter(markdown), field) == values
        markdown = f"---\n{field}: [2024, true, 1e3, 1.00]\n---\nBody"
        validate_frontmatter_input(markdown)
        assert getattr(parse_page_frontmatter(markdown), field) == [
            "2024",
            "true",
            "1e3",
            "1.00",
        ]
    with pytest.raises(InputPolicyError):
        validate_frontmatter_input("---\ntags: [[2024]]\n---\nBody")
    # Spellings the reader treats as empty or absent; an empty item is not.
    for header, expected in (
        ("tags: [ ]", []),
        ("tags: null", []),
        ("tags: [a, b,]", ["a", "b"]),
        ("title: null", []),
    ):
        markdown = f"---\n{header}\n---\nBody"
        validate_frontmatter_input(markdown)
        assert (parse_page_frontmatter(markdown).tags or []) == expected
    with pytest.raises(InputPolicyError):
        validate_frontmatter_input('---\ntags: [a, ""]\n---\nBody')


def test_final_managed_frontmatter_counts_against_stored_body_budget():
    with pytest.raises(InputPolicyError):
        BodyPreparer().prepare(
            BodyInput(
                "x" * 1_000_000,
                PageMetadataInput("Title", "title", "guide", [], []),
            ),
            WriteContext(
                workspace=SimpleNamespace(id=uuid4(), auto_fix_enabled=False),
                actor_kind="human",
                actor_id="fixture",
                actor_provenance="browser_session",
            ),
        )
    with pytest.raises(InputPolicyError, match="body must contain"):
        BodyPreparer().prepare(
            BodyInput("x" * 1_000_001, PageMetadataInput("x" * 257, "title")),
            WriteContext(
                workspace=SimpleNamespace(id=uuid4(), auto_fix_enabled=False),
                actor_kind="human",
                actor_id="fixture",
                actor_provenance="browser_session",
            ),
        )


def test_long_h1_cannot_bypass_title_limit():
    with pytest.raises(InputPolicyError):
        BodyPreparer().prepare(
            BodyInput(
                "# " + "x" * 257,
                PageMetadataInput("Title", "title", "guide", [], []),
            ),
            WriteContext(
                workspace=SimpleNamespace(id=uuid4(), auto_fix_enabled=False),
                actor_kind="human",
                actor_id="fixture",
                actor_provenance="browser_session",
            ),
        )
    # A longer closing fence, tildes and indented fences all keep code out of
    # the derived title. Prose after the fence still receives title validation.
    for opener, closer in (("```python", "````"), ("~~~", "~~~~"), ("   ```", "```")):
        body = f"{opener}\n# code\t{'x' * 257}\n{closer}\n# Prose title"
        validate_page_markdown_input(body)
        with pytest.raises(InputPolicyError):
            validate_page_markdown_input(body + "x" * 257)


def test_ask_schema_and_service_count_the_same_stripped_message():
    raw = " \n" + "가" * 8000 + "\t "
    assert AskTurnCreate(user_message=raw).user_message == "가" * 8000
    assert _validate_user_message(raw) == "가" * 8000
    for value in ("x" * 8001, " \x00 message "):
        with pytest.raises(ValidationError):
            AskTurnCreate(user_message=value)
        with pytest.raises(InvalidUserMessageError):
            _validate_user_message(value)
    empty_request = AskTurnCreate(user_message=" \n\t")
    assert empty_request.user_message == ""
    with pytest.raises(InvalidUserMessageError, match="must not be empty"):
        _validate_user_message(empty_request.user_message)


def test_context_edit_invalid_metadata_does_not_mutate_body_or_staged_title():
    version = ContextPageVersion(
        id=uuid4(), body_md="# Original", content_hash="original", block_map=[]
    )
    for body in ("# " + "x" * 257, "---\ntags: [" + "a" * 49 + "]\n---\nBody"):
        with pytest.raises(InputPolicyError):
            # Deliberately forge a token to pin the final mutation guard as
            # well as the preparer's ordinary authored-input validation.
            PageService._rewrite_body_in_place(version, PreparedBody(body, "changed"))
        assert (version.body_md, version.content_hash, version.block_map) == (
            "# Original",
            "original",
            [],
        )
    page = ContextPage(title="Original", tags=[])
    with pytest.raises(InputPolicyError):
        ContextWriteService._apply_metadata_patch(
            page, {"title": "x" * 257, "tags": ["changed"]}
        )
    assert page.title == "Original" and page.tags == []


def test_revision_notes_reject_invalid_unicode_before_review_lookup():
    for note in ("a\x00b", "a" * 2001):
        with pytest.raises(ValidationError):
            SubmitRevisionForReviewInput(
                workspace_id=uuid4(),
                review_item_id=uuid4(),
                note=note,
                create={
                    "title": "Title",
                    "page_type": "overview",
                    "body_markdown": "Body",
                    "reason": "Revise",
                },
            )
        with pytest.raises(InputPolicyError):
            ReviewService().submit_revision_for_review(
                session=None,
                workspace_id=uuid4(),
                review_item_id=uuid4(),
                principal=None,
                actor_kind="human",
                actor_id=str(uuid4()),
                context_write_service=None,
                note=note,
            )


def test_feedback_only_rejects_oversized_selected_body_before_any_changes(monkeypatch):
    proposal = SimpleNamespace(
        status="proposed",
        source_kind="manual",
        source_ask_turn_id=None,
        alternatives=[{"body_markdown": "x" * 10001}],
    )
    monkeypatch.setattr(context_ingress_service, "acquire_write_intent", lambda _: None)
    monkeypatch.setattr(
        context_ingress_service, "require_proposal_for_update", lambda *args: proposal
    )
    monkeypatch.setattr(
        context_ingress_service, "require_context_parent", lambda **kwargs: None
    )
    service = context_ingress_service.ContextIngressService()
    monkeypatch.setattr(service, "_source_turn", lambda *args: None)
    for explicit_body in ("x" * 10001, None):
        with pytest.raises(InputPolicyError):
            service.accept_proposal(
                session=None,
                workspace_id=uuid4(),
                proposal_id=uuid4(),
                request=ContextIngressAcceptRequest(
                    final_save_mode="feedback_only",
                    final_parent_node_id=uuid4(),
                    final_title="Feedback",
                    body_markdown=explicit_body,
                ),
                actor_kind="human",
                actor_id=str(uuid4()),
            )
    assert proposal.status == "proposed"

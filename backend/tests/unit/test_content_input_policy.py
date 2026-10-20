import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from backend.domain.input_policy import InputPolicyError, validate_title
from backend.schemas.agent import AgentPageDraftRequest, BuildBundleRequest
from backend.schemas.context_write import SubmitFeedbackRequest
from backend.schemas.input_policy import validate_request_inputs
from backend.schemas.library_semantic import LibrarySemanticMarkdownUpdateRequest
from backend.schemas.page import ContextPageCreateRequest
from backend.schemas.read import WorkspaceCreateRequest
from backend.schemas.tree import TreeFolderCreateRequest, TreeNodeUpdate
from backend.services.library_semantic_overlay_service import (
    LibrarySemanticOverlayService,
)
from backend.services.page_service import prepare_page_write
from backend.services.tree_service import TreeService
from backend.utils.page_frontmatter import (
    parse_page_frontmatter,
    validate_frontmatter_input,
)


def test_titles_count_code_points_and_preserve_explicit_unicode():
    assert validate_title(" " + "😀" * 256 + " ") == "😀" * 256
    with pytest.raises(InputPolicyError):
        validate_title("😀" * 257)
    for value in ("a\nb", "a\x00b", "a\ud800b"):
        with pytest.raises(InputPolicyError):
            validate_title(value)


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


def test_constructed_workspace_slug_is_rejected_before_normalizer_can_truncate():
    request = WorkspaceCreateRequest.model_construct(title="Title", slug="a" * 81)
    with pytest.raises(InputPolicyError):
        validate_request_inputs(request)


@pytest.mark.parametrize("body", ["text\x00tail", "text\ud800tail"])
def test_library_overlay_rejects_invalid_unicode_before_encoding_or_database(body):
    payload = dict(
        body_markdown=body,
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


@pytest.mark.parametrize(
    "field,value",
    [("title", "x" * 257), ("reason", "x" * 2001), ("body_markdown", "x" * 1_000_001)],
)
def test_agent_request_rejects_oversized_authored_fields(field, value):
    payload = dict(
        category="skill",
        title="Skill",
        slug="skill",
        body_markdown="Body",
        reason="Create",
    )
    payload[field] = value
    with pytest.raises(ValidationError):
        AgentPageDraftRequest(**payload)


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


def test_frontmatter_rejects_raw_count_before_read_parser_deduplicates():
    markdown = "---\ntags: [" + ",".join(['"same"'] * 33) + "]\n---\nBody"
    with pytest.raises(InputPolicyError):
        validate_frontmatter_input(markdown)
    with pytest.raises(ValidationError):
        ContextPageCreateRequest(body_md=markdown)


@pytest.mark.parametrize("field", ["tags", "aliases"])
def test_valid_frontmatter_quoted_commas_and_escapes_round_trip_without_loss(field):
    values = [f'alpha{index},"beta"\\suffix' for index in range(31)]
    markdown = f"---\n{field}: {json.dumps(values)}\n---\nBody"
    validate_frontmatter_input(markdown)
    assert getattr(parse_page_frontmatter(markdown), field) == values


def test_final_managed_frontmatter_counts_against_stored_body_budget():
    with pytest.raises(InputPolicyError):
        prepare_page_write(
            body_md="x" * 1_000_000,
            title="Title",
            slug="title",
            page_type="guide",
            tags=[],
            aliases=[],
        )


def test_long_h1_cannot_bypass_title_limit():
    with pytest.raises(InputPolicyError):
        prepare_page_write(
            body_md="# " + "x" * 257,
            title="Title",
            slug="title",
            page_type="guide",
            tags=[],
            aliases=[],
        )

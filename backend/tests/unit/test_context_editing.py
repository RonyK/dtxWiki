from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from backend.schemas.context_edit import (
    AppendDocumentOperation,
    DeleteSectionOperation,
    EditOperation,
    InsertSectionOperation,
    InsertTextOperation,
    PatchDraftRequest,
    ReplaceDocumentOperation,
    ReplaceSectionOperation,
    ReplaceTextOperation,
)
from backend.schemas.context_write import UpdateDraftRequest
from backend.services import context_editing
from backend.services.anchor_resolver import AnchorStatus, ResolvedAnchor
from backend.services.context_editing import (
    ContextEditError,
    apply_operations,
    resolve_edit,
    section_content_hash,
    validate_operation_registry,
)
from backend.services.lint.autofix import safe_analyze_markdown, safe_normalize_markdown
from backend.utils.markdown_outline import parse_outline


def _section(body: str, title: str):
    return next(item for item in parse_outline(body).sections if item.title == title)


def _hash(body: str, title: str) -> str:
    section = _section(body, title)
    return section_content_hash(body[section.char_start : section.subtree_char_end])


def test_replace_text_replaces_only_the_resolved_scope():
    body = "# One\n\nold\n\n# Two\n\nold\n"
    result, edits = apply_operations(
        body,
        parse_outline(body),
        [
            ReplaceTextOperation(
                op="replace_text",
                at={"by": "heading_path", "heading_path": ["One"]},
                old_text="old",
                new_text="new",
            )
        ],
    )

    assert result == "# One\n\nnew\n\n# Two\n\nold\n"
    assert edits[0].operation == "replace_text"


def test_heading_path_separator_and_occurrence_select_repeated_path():
    body = "# Same\n\nfirst\n\n# Same\n\nsecond\n"
    operation = ReplaceTextOperation(
        op="replace_text",
        at={"by": "heading_path", "heading_path": "Same", "occurrence": 2},
        old_text="second",
        new_text="changed",
    )

    result, _ = apply_operations(body, parse_outline(body), [operation])

    assert result == "# Same\n\nfirst\n\n# Same\n\nchanged\n"


def test_replace_text_rejects_occurrence_mismatch():
    body = "old old"
    operation = ReplaceTextOperation(
        op="replace_text",
        old_text="old",
        new_text="new",
        expected_occurrences=1,
    )

    with pytest.raises(ContextEditError) as captured:
        resolve_edit(body, parse_outline(body), operation)

    assert captured.value.error_code == "text_occurrence_mismatch"


def test_insert_text_before_and_after_anchor():
    body = "alpha target omega"
    before, _ = apply_operations(
        body,
        parse_outline(body),
        [
            InsertTextOperation(
                op="insert_text",
                at={
                    "by": "anchor",
                    "anchor_text": "target",
                    "char_offset_hint": 6,
                },
                new_text="BEFORE ",
                side="before",
            )
        ],
    )
    after, _ = apply_operations(
        body,
        parse_outline(body),
        [
            InsertTextOperation(
                op="insert_text",
                at={
                    "by": "anchor",
                    "anchor_text": "target",
                    "char_offset_hint": 6,
                },
                new_text=" AFTER",
                side="after",
            )
        ],
    )

    assert before == "alpha BEFORE target omega"
    assert after == "alpha target AFTER omega"


def test_append_and_replace_document_operations():
    appended, _ = apply_operations(
        "# Existing\n",
        parse_outline("# Existing\n"),
        [AppendDocumentOperation(op="append_document", content="# Added\n")],
    )
    replaced, _ = apply_operations(
        appended,
        parse_outline(appended),
        [
            ReplaceDocumentOperation(
                op="replace_document",
                content="# Entirely new\n",
                expected_content_hash=section_content_hash(appended),
            )
        ],
    )

    assert appended == "# Existing\n\n# Added\n"
    assert replaced == "# Entirely new\n"


def test_replace_section_uses_subtree_bounds():
    body = "# Parent\n\nparent\n\n## Child\n\nchild\n\n# Sibling\n\nsibling\n"
    operation = ReplaceSectionOperation(
        op="replace_section",
        at={"by": "section_id", "section_id": "parent"},
        content="# Parent replacement\n\nnew\n",
        expected_section_hash=_hash(body, "Parent"),
    )

    result, _ = apply_operations(body, parse_outline(body), [operation])

    assert "Child" not in result
    assert result.endswith("# Sibling\n\nsibling\n")


@pytest.mark.parametrize("position", ["before", "after", "append_child"])
def test_insert_section_uses_outline_relative_position(position):
    body = "# Parent\n\nparent\n\n# Sibling\n\nsibling\n"
    operation = InsertSectionOperation(
        op="insert_section",
        at={"by": "heading_path", "heading_path": "Parent"},
        position=position,
        title="Inserted",
        content="inserted body",
    )

    result, _ = apply_operations(body, parse_outline(body), [operation])

    expected_heading = "## Inserted" if position == "append_child" else "# Inserted"
    assert expected_heading in result
    if position == "before":
        assert result.index(expected_heading) < result.index("# Parent")
    else:
        assert result.index(expected_heading) > result.index("# Parent")


def test_delete_section_removes_the_entire_subtree():
    body = "# Parent\n\nparent\n\n## Child\n\nchild\n\n# Keep\n\nkeep\n"
    operation = DeleteSectionOperation(
        op="delete_section",
        at={"by": "heading_path", "heading_path": ["Parent"]},
        expected_section_hash=_hash(body, "Parent"),
    )

    result, _ = apply_operations(body, parse_outline(body), [operation])

    assert result == "# Keep\n\nkeep\n"


@pytest.mark.parametrize("op", ["replace_section", "delete_section"])
def test_destructive_section_operations_require_hash(op):
    payload = {
        "op": op,
        "at": {"by": "section_id", "section_id": "one"},
    }
    if op == "replace_section":
        payload["content"] = "# replacement"

    with pytest.raises(ValidationError):
        TypeAdapter(EditOperation).validate_python(payload)


@pytest.mark.parametrize("op", ["replace_section", "delete_section"])
def test_destructive_section_operations_reject_hash_mismatch(op):
    body = "# One\n\nbody\n"
    payload = {
        "op": op,
        "at": {"by": "section_id", "section_id": "one"},
        "expected_section_hash": "0" * 64,
    }
    if op == "replace_section":
        payload["content"] = "# replacement"
    operation = TypeAdapter(EditOperation).validate_python(payload)

    with pytest.raises(ContextEditError) as captured:
        resolve_edit(body, parse_outline(body), operation)

    assert captured.value.error_code == "section_hash_mismatch"


def test_overlapping_operations_reject_the_whole_batch():
    body = "# One\n\nbody\n"
    operations = [
        ReplaceDocumentOperation(
            op="replace_document",
            content="# replacement\n",
            expected_content_hash=section_content_hash(body),
        ),
        DeleteSectionOperation(
            op="delete_section",
            at={"by": "section_id", "section_id": "one"},
            expected_section_hash=_hash(body, "One"),
        ),
    ]

    with pytest.raises(ContextEditError) as captured:
        apply_operations(body, parse_outline(body), operations)

    assert captured.value.error_code == "operations_overlap"


def test_non_overlapping_batch_applies_in_descending_offset_order():
    body = "# One\n\na\n\n# Two\n\nb\n"
    operations = [
        ReplaceTextOperation(
            op="replace_text",
            at={"by": "section_id", "section_id": "one"},
            old_text="a",
            new_text="first replacement is much longer",
        ),
        ReplaceTextOperation(
            op="replace_text",
            at={"by": "section_id", "section_id": "two"},
            old_text="b",
            new_text="second replacement",
        ),
    ]

    result, _ = apply_operations(body, parse_outline(body), operations)

    assert "first replacement is much longer" in result
    assert result.endswith("# Two\n\nsecond replacement\n")


def test_distant_fuzzy_anchor_requires_explicit_acceptance(monkeypatch):
    monkeypatch.setattr(
        context_editing,
        "resolve_anchor",
        lambda *args, **kwargs: ResolvedAnchor(
            char_offset=0,
            end_offset=6,
            status=AnchorStatus.FUZZY_DISTANT,
            resolved_text="target",
        ),
    )
    operation = InsertTextOperation(
        op="insert_text",
        at={"by": "anchor", "anchor_text": "target", "char_offset_hint": 0},
        new_text="new",
        side="after",
    )

    with pytest.raises(ContextEditError) as captured:
        resolve_edit("target", parse_outline("target"), operation)

    assert captured.value.error_code == "anchor_confidence_low"
    accepted = operation.model_copy(update={"accept_fuzzy": True})
    assert resolve_edit("target", parse_outline("target"), accepted).char_start == 6


def test_missing_anchor_rejects_without_producing_an_edit():
    operation = InsertTextOperation(
        op="insert_text",
        at={"by": "anchor", "anchor_text": "missing", "char_offset_hint": 0},
        new_text="new",
        side="after",
    )

    with pytest.raises(ContextEditError) as captured:
        resolve_edit("body", parse_outline("body"), operation)

    assert captured.value.error_code == "anchor_not_found"


def test_stale_anchor_hint_prefers_the_verbatim_text_over_a_lookalike():
    filler = "Filler sentence number xxxxxxxxxx. " * 12
    body = (
        "# T\n\nSet retry to low for the api client.\n\n"
        + filler
        + "\n\nSet retry to low for the web client.\n"
    )
    operation = ReplaceTextOperation(
        op="replace_text",
        old_text="low",
        new_text="high",
        at={
            "by": "anchor",
            "anchor_text": "Set retry to low for the web client.",
            "char_offset_hint": 5,
        },
    )

    result, _ = apply_operations(body, parse_outline(body), [operation])

    assert "Set retry to low for the api client." in result
    assert "Set retry to high for the web client." in result

    twice = body + "\nSet retry to low for the web client.\n"
    with pytest.raises(ContextEditError) as captured:
        apply_operations(twice, parse_outline(twice), [operation])
    assert captured.value.error_code == "anchor_confidence_low"


def test_level_six_section_cannot_receive_append_child():
    body = "###### Deepest\n\nbody\n"
    operation = InsertSectionOperation(
        op="insert_section",
        at={"by": "section_id", "section_id": "deepest"},
        position="append_child",
        title="Too deep",
        content="body",
    )

    with pytest.raises(ContextEditError) as captured:
        resolve_edit(body, parse_outline(body), operation)

    assert captured.value.error_code == "heading_level_invalid"


@pytest.mark.parametrize(
    "payload",
    [
        {"op": "replace_text", "old_text": "x", "new_text": "   "},
        {
            "op": "insert_text",
            "at": {"by": "anchor", "anchor_text": "x", "char_offset_hint": 0},
            "new_text": "   ",
            "side": "after",
        },
        {"op": "append_document", "content": "   "},
        {"op": "replace_document", "content": "   "},
        {
            "op": "replace_section",
            "at": {"by": "section_id", "section_id": "x"},
            "content": "   ",
            "expected_section_hash": "0" * 64,
        },
        {
            "op": "insert_section",
            "at": {"by": "section_id", "section_id": "x"},
            "position": "after",
            "title": "title",
            "content": "   ",
        },
    ],
)
def test_i13_rejects_whitespace_only_edit_content(payload):
    with pytest.raises(ValidationError):
        TypeAdapter(EditOperation).validate_python(payload)


def test_registry_completeness_uses_runtime_error():
    incomplete = dict(context_editing.OPERATION_HANDLERS)
    incomplete.pop("delete_section")

    with pytest.raises(RuntimeError):
        validate_operation_registry(incomplete)


def test_last_operation_failure_keeps_twenty_operation_batch_atomic(monkeypatch):
    body = "".join(f"# S{index}\n\nvalue-{index}\n" for index in range(1, 20))
    operations = [
        ReplaceTextOperation(
            op="replace_text",
            at={"by": "heading_path", "heading_path": [f"S{index}"]},
            old_text=f"value-{index}",
            new_text=f"changed-{index}",
        )
        for index in range(1, 20)
    ]
    operations.append(
        ReplaceTextOperation(
            op="replace_text",
            at={"by": "heading_path", "heading_path": ["Missing"]},
            old_text="missing",
            new_text="changed",
        )
    )
    splice_called = False

    def fail_if_spliced(*args, **kwargs):
        nonlocal splice_called
        splice_called = True
        raise AssertionError("batch spliced before every operation resolved")

    monkeypatch.setattr(context_editing, "apply_resolved_edits", fail_if_spliced)

    with pytest.raises(ContextEditError) as captured:
        apply_operations(body, parse_outline(body), operations)

    assert captured.value.error_code == "section_not_found"
    assert splice_called is False
    assert "changed-1" not in body


def test_replace_text_containing_old_text_does_not_require_an_explicit_key():
    """A self-containing replace no longer needs the caller to supply a key:
    the MCP layer (backend/mcp/context_tools.py) derives one from the
    conversation and this request's own content whenever the field is
    omitted, so a retry is always replayed from the ledger regardless of
    operation shape -- see PatchDraftRequest.idempotency_key's description."""
    operation = {
        "op": "replace_text",
        "old_text": "old",
        "new_text": "old-new",
    }

    request = PatchDraftRequest(
        page_id=uuid4(),
        operations=[operation],
        reason="self-containing replacement",
    )
    assert request.idempotency_key is None

    keyed = PatchDraftRequest(
        page_id=uuid4(),
        operations=[operation],
        reason="self-containing replacement",
        idempotency_key="replace-old-once",
    )
    assert keyed.idempotency_key == "replace-old-once"


def test_patch_request_is_a_strict_sibling_with_bounded_operations():
    assert not issubclass(PatchDraftRequest, UpdateDraftRequest)

    with pytest.raises(ValidationError):
        PatchDraftRequest(page_id=uuid4(), operations=[], reason="empty batch")
    with pytest.raises(ValidationError):
        PatchDraftRequest(
            page_id=uuid4(),
            operations=[
                {"op": "append_document", "content": f"item-{index}"}
                for index in range(21)
            ],
            reason="oversized batch",
        )
    with pytest.raises(ValidationError):
        PatchDraftRequest(
            page_id=uuid4(),
            operations=[{"op": "append_document", "content": "valid"}],
            reason="reject unknown fields",
            unknown_field=True,
        )


def test_patch_request_accepts_explicit_identity_retirement_resolution():
    branch_id = uuid4()
    request = PatchDraftRequest(
        page_id=uuid4(),
        operations=[
            {
                "op": "resolve_conflict",
                "block": "b_retired",
                "resolution": "keep_head",
            }
        ],
        reason="accept current head for retired identity",
        reconciles=[branch_id],
    )

    resolution = request.operations[0]
    assert resolution.op == "resolve_conflict"
    assert resolution.block == "b_retired"
    assert resolution.resolution == "keep_head"

    with pytest.raises(ValidationError):
        PatchDraftRequest(
            page_id=uuid4(),
            operations=[
                {
                    "op": "resolve_conflict",
                    "block": "b_retired",
                    "resolution": "keep_deleted",
                }
            ],
            reason="reject ambiguous retired resolution",
            reconciles=[branch_id],
        )


# Retry replay belongs to the write ledger, so insert operations always apply here.
def test_insert_text_never_reports_a_no_op():
    body = "alpha target omega"
    operation = InsertTextOperation(
        op="insert_text",
        at={"by": "anchor", "anchor_text": "target", "char_offset_hint": 6},
        new_text=" inserted",
        side="after",
    )

    once, first_edits = apply_operations(body, parse_outline(body), [operation])
    twice, second_edits = apply_operations(once, parse_outline(once), [operation])

    assert once == "alpha target inserted omega"
    assert twice == "alpha target inserted inserted omega"
    assert first_edits[0].no_op is False
    assert second_edits[0].no_op is False


def test_append_document_never_reports_a_no_op():
    body = "# Base\n"
    operation = AppendDocumentOperation(op="append_document", content="# Added\n")

    once, first_edits = apply_operations(body, parse_outline(body), [operation])
    twice, second_edits = apply_operations(once, parse_outline(once), [operation])

    assert once.count("# Added") == 1
    assert twice.count("# Added") == 2
    assert first_edits[0].no_op is False
    assert second_edits[0].no_op is False


def test_insert_section_never_reports_a_no_op():
    body = "# Root\n\n# Tail\n\ntail\n"
    operation = InsertSectionOperation(
        op="insert_section",
        at={"by": "heading_path", "heading_path": ["Tail"]},
        position="before",
        title="Inserted",
        content="inserted body",
    )

    once, first_edits = apply_operations(body, parse_outline(body), [operation])
    twice, second_edits = apply_operations(once, parse_outline(once), [operation])

    assert once.count("# Inserted") == 1
    assert twice.count("# Inserted") == 2
    assert first_edits[0].no_op is False
    assert second_edits[0].no_op is False


def test_insert_section_no_longer_needs_a_stable_markdown_boundary():
    body = "# Parent\n\n```md\ncode\n"
    operation = InsertSectionOperation(
        op="insert_section",
        at={"by": "heading_path", "heading_path": ["Parent"]},
        position="append_child",
        title="Child",
        content="x",
    )

    result, edits = apply_operations(body, parse_outline(body), [operation])

    assert "## Child\n\nx\n" in result
    assert edits[0].no_op is False


def test_fresh_key_readdresses_a_wide_table_row_but_applies_the_repeat():
    body = "| A   | B      |\n| --- | ------ |\n| old | anchor |\n"
    anchor_row = "| old | anchor |"
    wide_value = "x" * 500
    new_text = f"\n| H | V |\n| --- | --- |\n| {wide_value} | y |"
    operation = InsertTextOperation(
        op="insert_text",
        at={
            "by": "anchor",
            "anchor_text": anchor_row,
            "char_offset_hint": body.index(anchor_row),
        },
        new_text=new_text,
        side="after",
    )
    first, _ = apply_operations(
        body,
        parse_outline(body),
        [operation],
        markdown_analysis=safe_analyze_markdown(body),
    )
    landed = safe_normalize_markdown(first)
    repeated, edits = apply_operations(
        landed,
        parse_outline(landed),
        [operation],
        markdown_analysis=safe_analyze_markdown(landed),
    )

    assert anchor_row not in landed
    assert repeated.count(wide_value) == 2
    assert edits[0].no_op is False


def test_fresh_key_semantic_table_anchor_does_not_hijack_a_raw_fuzzy_line():
    filler = "f" * 320
    body = (
        "| old | anch0r |\n"
        f"{filler}\n\n"
        "| H   | V      |\n"
        "| --- | ------ |\n"
        "| old | anchor |\n"
    )
    operation = InsertTextOperation(
        op="insert_text",
        at={
            "by": "anchor",
            "anchor_text": "| old | anchor |",
            "char_offset_hint": 0,
        },
        new_text="\nnew",
        side="after",
        accept_fuzzy=True,
    )

    result, edits = apply_operations(
        body,
        parse_outline(body),
        [operation],
        markdown_analysis=safe_analyze_markdown(body),
    )

    assert result.index("new") < result.index(filler)
    assert edits[0].anchor_preview == "| old | anch0r |"


def test_fresh_key_uses_raw_hint_to_disambiguate_duplicate_semantic_rows():
    body = "| A   | B      |\n| --- | ------ |\n| old | anchor |\n| old | anchor |\n"
    inner_anchor = "old | anchor"
    first_row_start = body.index("| old | anchor |")
    operation = InsertTextOperation(
        op="insert_text",
        at={
            "by": "anchor",
            "anchor_text": inner_anchor,
            "char_offset_hint": first_row_start + 2,
        },
        new_text="\nfirst-row-only",
        side="after",
    )

    result, edits = apply_operations(
        body,
        parse_outline(body),
        [operation],
        markdown_analysis=safe_analyze_markdown(body),
    )

    assert result.index("first-row-only") < result.rindex("| old | anchor |")
    assert result.count("first-row-only") == 1
    assert edits[0].anchor_preview == "| old | anchor |"


@pytest.mark.parametrize("use_second_occurrence", [False, True])
@pytest.mark.parametrize("inserted_width", [6, 500])
def test_fresh_key_fails_closed_for_duplicate_rows_after_wide_padding(
    use_second_occurrence,
    inserted_width,
):
    body = "| A | B |\n| --- | --- |\n| old | anchor |\n| old | anchor |\n"
    anchor_row = "| old | anchor |"
    wide_value = "x" * inserted_width
    new_text = f"\n| H | V |\n| --- | --- |\n| {wide_value} | y |"
    target_hint = (
        body.rindex(anchor_row) if use_second_occurrence else body.index(anchor_row)
    )
    operation = InsertTextOperation(
        op="insert_text",
        at={
            "by": "anchor",
            "anchor_text": anchor_row,
            "char_offset_hint": target_hint,
        },
        new_text=new_text,
        side="after",
    )
    first, _ = apply_operations(
        body,
        parse_outline(body),
        [operation],
        markdown_analysis=safe_analyze_markdown(body),
    )
    landed = safe_normalize_markdown(first)

    with pytest.raises(ContextEditError) as captured:
        apply_operations(
            landed,
            parse_outline(landed),
            [operation],
            markdown_analysis=safe_analyze_markdown(landed),
        )

    assert captured.value.error_code == "anchor_confidence_low"


def test_fresh_key_nonexact_pipe_anchor_fails_if_analysis_is_unavailable(monkeypatch):
    body = "| A" + (" " * 500) + "| B |\n| ---" + ("-" * 498) + "| --- |\n"
    operation = InsertTextOperation(
        op="insert_text",
        at={
            "by": "anchor",
            "anchor_text": "| A | B |",
            "char_offset_hint": 0,
        },
        new_text="\nnew",
        side="after",
    )
    monkeypatch.setattr(context_editing, "safe_analyze_markdown", lambda _body: None)

    with pytest.raises(ContextEditError) as captured:
        apply_operations(
            body,
            parse_outline(body),
            [operation],
        )

    assert captured.value.error_code == "anchor_confidence_low"


def test_fresh_key_preserves_distant_fuzzy_consent_outside_the_table_block():
    body = f"{'p' * 320}\n\n| A   | B      |\n| --- | ------ |\n| old | anchor |\n"
    operation = InsertTextOperation(
        op="insert_text",
        at={
            "by": "anchor",
            "anchor_text": "| old | anchor |",
            "char_offset_hint": 0,
        },
        new_text="\nnew",
        side="after",
    )

    with pytest.raises(ContextEditError) as captured:
        apply_operations(
            body,
            parse_outline(body),
            [operation],
            markdown_analysis=safe_analyze_markdown(body),
        )

    assert captured.value.error_code == "anchor_confidence_low"
    accepted = operation.model_copy(update={"accept_fuzzy": True})
    result, edits = apply_operations(
        body,
        parse_outline(body),
        [accepted],
        markdown_analysis=safe_analyze_markdown(body),
    )
    assert result.endswith("| old | anchor |\nnew\n")
    assert edits[0].anchor_status == AnchorStatus.FUZZY_DISTANT.value


def test_pipe_run_anchor_keeps_raw_semantics_and_is_not_promoted_to_a_table_row():
    body = "| alpha | beta |\n"
    inner_anchor = "alpha | beta"
    operation = InsertTextOperation(
        op="insert_text",
        at={
            "by": "anchor",
            "anchor_text": inner_anchor,
            "char_offset_hint": body.index(inner_anchor),
        },
        new_text="X",
        side="after",
    )

    result, edits = apply_operations(body, parse_outline(body), [operation])

    assert result == "| alpha | betaX |\n"
    assert edits[0].anchor_preview == inner_anchor


def test_fresh_key_mode_applies_intentional_duplicate_insert():
    body = "alpha target inserted omega"
    operation = InsertTextOperation(
        op="insert_text",
        at={"by": "anchor", "anchor_text": "target", "char_offset_hint": 6},
        new_text=" inserted",
        side="after",
    )

    result, edits = apply_operations(body, parse_outline(body), [operation])

    assert result.count("inserted") == 2
    assert edits[0].no_op is False


def test_fresh_key_mode_applies_intentional_duplicate_append():
    body = "# Base\n\n# Added\n"
    operation = AppendDocumentOperation(op="append_document", content="# Added\n")

    result, edits = apply_operations(body, parse_outline(body), [operation])

    assert result.count("# Added") == 2
    assert edits[0].no_op is False


@pytest.mark.parametrize(
    "new_text",
    [
        *(f"\nplain-{index:02d}" for index in range(8)),
        *(f"\n* item-{index:02d}" for index in range(8)),
        *(f"\n## Heading {index:02d}" for index in range(8)),
    ],
)
def test_generated_fresh_key_operations_apply_intentional_repeats(new_text):
    """The edit engine applies the same insertion on every call."""

    body = "# Doc\n\nanchor\n"
    operation = InsertTextOperation(
        op="insert_text",
        at={"by": "anchor", "anchor_text": "anchor", "char_offset_hint": 7},
        new_text=new_text,
        side="after",
    )

    first, _ = apply_operations(
        body,
        parse_outline(body),
        [operation],
    )
    landed = safe_normalize_markdown(first)
    second, edits = apply_operations(
        landed,
        parse_outline(landed),
        [operation],
    )

    assert second != landed
    assert edits[0].no_op is False


def test_insert_section_at_a_replace_section_boundary_is_rejected_not_corrupted():
    # A-2: inserting a section immediately before/after a range that a sibling
    # operation replaces used to be treated as non-overlapping (a zero-width
    # point at the boundary of a non-empty range), so both edits applied and
    # the result silently corrupted the document (the inserted heading was
    # lost and "A" ended up duplicated). The whole batch must instead be
    # refused so the caller can re-issue it as two separate requests.
    body = "# Doc\n\n## A\n\naaa\n\n## B\n\nbbb\n"
    operations = [
        InsertSectionOperation(
            op="insert_section",
            at={"by": "heading_path", "heading_path": ["Doc", "A"]},
            position="before",
            title="NEW",
            content="new body",
        ),
        ReplaceSectionOperation(
            op="replace_section",
            at={"by": "heading_path", "heading_path": ["Doc", "A"]},
            content="## A\n\nreplaced\n",
            expected_section_hash=_hash(body, "A"),
        ),
    ]

    with pytest.raises(ContextEditError) as captured:
        apply_operations(body, parse_outline(body), operations)

    assert captured.value.error_code == "operations_overlap"


def test_replace_document_requires_expected_content_hash_to_match_the_head():
    # A-3: replace_document had no precondition at all, so a stale caller
    # could silently overwrite a concurrent edit. It must behave like every
    # other destructive operation and refuse on a hash mismatch.
    body = "# One\n\noriginal\n"
    operation = ReplaceDocumentOperation(
        op="replace_document",
        content="# One\n\nreplacement\n",
        expected_content_hash="0" * 64,
    )

    with pytest.raises(ContextEditError) as captured:
        resolve_edit(body, parse_outline(body), operation)

    assert captured.value.error_code == "stale_base_version"


def test_replace_document_applies_when_expected_content_hash_matches():
    body = "# One\n\noriginal\n"
    operation = ReplaceDocumentOperation(
        op="replace_document",
        content="# One\n\nreplacement\n",
        expected_content_hash=section_content_hash(body),
    )

    result, _ = apply_operations(body, parse_outline(body), [operation])

    assert result == "# One\n\nreplacement\n"

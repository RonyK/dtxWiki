"""replace_text through the tiered matcher: forgiving where the caller named
the target, refusing with evidence everywhere else.

The matcher's own rules are covered in test_text_matching.py. These tests pin
what the edit pipeline does with them: which addresses may use the fuzzy tier,
what a refusal carries, and that an accepted match rewrites only its span.
"""

import hashlib
import json
import unicodedata

import pytest
from pydantic import TypeAdapter, ValidationError

from backend.schemas.context_edit import EditOperation
from backend.services.context_editing import (
    ContextEditError,
    apply_operations,
)
from backend.services.edit_receipt import patch_operation_receipts
from backend.services.text_matching import MATCH_POLICY
from backend.utils.block_identity import compute_block_map_with_ids
from backend.utils.markdown_outline import parse_outline

pytestmark = pytest.mark.unit

_OPERATION = TypeAdapter(EditOperation)

BODY = (
    "# Guide\n\n"
    "## Setup\n\n"
    "Install the package and restart the service afterwards.\n\n"
    "Keep the default port unless the network team says otherwise.\n\n"
    "## Usage\n\n"
    "Open the dashboard and pick the workspace you want to inspect.\n"
)
SENTENCE = "Install the package and restart the service afterwards."
# One dropped letter ("restart" -> "restat") in a 56-character sentence.
TYPO = SENTENCE.replace("restart", "restat")


def _map(body: str) -> list[dict]:
    return compute_block_map_with_ids(
        body, previous_body_md=None, previous_block_map=None, identity_seed="t"
    )


def _block(block_map: list[dict], body: str, needle: str) -> dict:
    from backend.utils.block_map import compute_blocks

    for entry, block in zip(block_map, compute_blocks(body), strict=True):
        lines = body.splitlines()[block.line_start : block.line_end]
        if needle in "\n".join(lines):
            return entry
    raise AssertionError(needle)


def _apply(body: str, operations: list[dict], block_map: list[dict] | None = None):
    parsed = [_OPERATION.validate_python(operation) for operation in operations]
    return apply_operations(
        body,
        parse_outline(body),
        parsed,
        block_map=block_map if block_map is not None else _map(body),
    )


def _refusal(body: str, operations: list[dict]) -> ContextEditError:
    with pytest.raises(ContextEditError) as captured:
        _apply(body, operations)
    return captured.value


def _at_block(body: str, needle: str) -> dict:
    entry = _block(_map(body), body, needle)
    return {"by": "block", "block": entry["id"], "expected": entry["hash"]}


def test_a_copy_slip_is_forgiven_inside_a_named_block():
    result, edits = _apply(
        BODY,
        [
            {
                "op": "replace_text",
                "old_text": TYPO,
                "new_text": "Install the package, then restart the service.",
                "at": _at_block(BODY, "Install the package"),
            }
        ],
    )
    assert result == BODY.replace(
        "Install the package and restart the service afterwards.",
        "Install the package, then restart the service.",
    )
    assert edits[0].match_kind == "fuzzy"
    assert 0.9 < edits[0].match_similarity < 1


def test_a_copy_slip_is_forgiven_inside_a_named_section():
    result, edits = _apply(
        BODY,
        [
            {
                "op": "replace_text",
                "old_text": TYPO,
                "new_text": "Replaced.",
                "at": {"by": "heading_path", "heading_path": ["Guide", "Setup"]},
            }
        ],
    )
    assert "Replaced.\n\nKeep the default port" in result
    assert edits[0].match_kind == "fuzzy"


def test_the_same_slip_without_a_scope_is_refused_with_evidence_only():
    error = _refusal(
        BODY, [{"op": "replace_text", "old_text": TYPO, "new_text": "Replaced."}]
    )
    assert error.error_code == "text_match_not_found"
    detail = error.details[0]
    assert detail.operation_index == 0
    assert detail.field == "operations[0].old_text"
    assert detail.candidates, "the nearest text must be located, not applied"
    nearest = detail.candidates[0]
    assert (nearest.line_start, nearest.line_end) == (5, 5)
    assert nearest.block_id == _block(_map(BODY), BODY, "Install")["id"]
    assert nearest.match_kind == "fuzzy"
    # Evidence is positional: neither the caller's text nor the page's.
    payload = json.dumps(
        [str(error), [w.message for w in error.warnings], detail.model_dump()],
        ensure_ascii=False,
    )
    assert "restat" not in payload
    assert "Install the package" not in payload


def test_an_anchor_scope_never_enables_the_fuzzy_tier():
    error = _refusal(
        BODY,
        [
            {
                "op": "replace_text",
                "old_text": TYPO,
                "new_text": "Replaced.",
                # The anchor spans exactly the sentence the slip was copied
                # from, yet an anchor's own resolution is approximate, so it
                # does not count as naming the target.
                "at": {
                    "by": "anchor",
                    "anchor_text": SENTENCE,
                    "char_offset_hint": BODY.index(SENTENCE),
                },
            }
        ],
    )
    assert error.error_code == "text_match_not_found"


def test_invisible_differences_match_even_without_a_scope():
    body = (
        "# Notes\n\n" + unicodedata.normalize("NFD", "결정 사항은 다음과 같다") + "\n"
    )
    result, edits = _apply(
        body,
        [
            {
                "op": "replace_text",
                "old_text": "결정 사항은  다음과 같다",
                "new_text": "결정 사항: 없음",
            }
        ],
    )
    assert result == "# Notes\n\n결정 사항: 없음\n"
    assert edits[0].match_kind == "normalized"
    assert edits[0].match_similarity is None
    assert edits[0].matched_sha256 is not None


def test_two_forgiven_slips_in_one_batch_both_apply():
    result, edits = _apply(
        BODY,
        [
            {
                "op": "replace_text",
                "old_text": TYPO,
                "new_text": "First.",
                "at": _at_block(BODY, "Install the package"),
            },
            {
                "op": "replace_text",
                "old_text": (
                    "Open the dashbord and pick the workspace you want to inspect."
                ),
                "new_text": "Second.",
                "at": _at_block(BODY, "Open the dashboard"),
            },
        ],
    )
    assert "First.\n\nKeep the default port" in result
    assert result.endswith("Second.\n")
    assert [edit.match_kind for edit in edits] == ["fuzzy", "fuzzy"]


def test_two_equally_close_places_in_a_section_are_ambiguous():
    body = (
        "# Log\n\n## Week\n\n"
        "The deploy finished without errors today.\n\n"
        "The deploy finished without errors today.\n"
    )
    error = _refusal(
        body,
        [
            {
                "op": "replace_text",
                "old_text": "The deploy finishd without errors today.",
                "new_text": "x",
                "at": {"by": "heading_path", "heading_path": ["Log", "Week"]},
            }
        ],
    )
    assert error.error_code == "text_match_ambiguous"
    blocks = {candidate.block_id for candidate in error.details[0].candidates}
    assert len(blocks) == 2 and None not in blocks


def test_a_changed_number_is_never_guessed():
    body = "# Perf\n\nThe median latency was 21 milliseconds in the last run.\n"
    error = _refusal(
        body,
        [
            {
                "op": "replace_text",
                "old_text": "The median latency was 27 milliseconds in the last run.",
                "new_text": "x",
                "at": _at_block(body, "median latency"),
            }
        ],
    )
    assert error.error_code == "text_match_low_confidence"
    assert "digit" in str(error) or "number" in str(error)


def test_a_stale_block_hash_fails_instead_of_searching_elsewhere():
    address = _at_block(BODY, "Install the package")
    address["expected"] = "0" * 64
    error = _refusal(
        BODY,
        [
            {
                "op": "replace_text",
                "old_text": "Install the package",
                "new_text": "x",
                "at": address,
            }
        ],
    )
    assert error.error_code == "block_hash_mismatch"


def test_exact_mode_refuses_every_loosened_tier():
    error = _refusal(
        BODY,
        [
            {
                "op": "replace_text",
                "old_text": TYPO,
                "new_text": "x",
                "match_mode": "exact",
                "at": _at_block(BODY, "Install the package"),
            }
        ],
    )
    assert error.error_code == "text_match_not_found"


def test_code_blocks_only_match_exactly():
    # Whitespace in code can be meaningful; collapsing it would be a guess.
    body = "# Code\n\n```python\nvalue  =  compute(items)\n```\n"
    error = _refusal(
        body,
        [
            {
                "op": "replace_text",
                "old_text": "value = compute(items)",
                "new_text": "x",
                "at": _at_block(body, "compute(items)"),
            }
        ],
    )
    assert error.error_code in {"text_match_not_found", "text_match_low_confidence"}
    # The exact text still works.
    result, _edits = _apply(
        body,
        [
            {
                "op": "replace_text",
                "old_text": "value  =  compute(items)",
                "new_text": "value = compute(items)",
                "at": _at_block(body, "compute(items)"),
            }
        ],
    )
    assert "value = compute(items)" in result


def test_a_hit_inside_a_grapheme_does_not_skip_code_protection():
    # old_text is a raw substring of the code line, but it ends inside the
    # decomposed e-acute, so the exact tier rejects it. The protected zone
    # must still apply to the tiers after it: the code block is not rewritten.
    body = (
        "# Code\n\n```text\nthis is a very long plain phrase ending cafe"
        + chr(0x301)
        + "\n```\n"
    )
    error = _refusal(
        body,
        [
            {
                "op": "replace_text",
                "old_text": "this is a very long plain phrase ending cafe",
                "new_text": "REPLACED",
                "at": _at_block(body, "plain phrase"),
            }
        ],
    )
    assert error.error_code in {"text_match_not_found", "text_match_low_confidence"}


def test_a_markdown_hard_break_is_not_an_invisible_difference():
    # Two trailing spaces before a non-blank line render as a line break, so
    # old_text without them is not "the same text with invisible spacing".
    body = "# Card\n\nAddress line one  \nAddress line two\n"
    error = _refusal(
        body,
        [
            {
                "op": "replace_text",
                "old_text": "Address line one\nAddress line two",
                "new_text": "Address line 1\nAddress line two",
            }
        ],
    )
    assert error.error_code == "text_match_not_found"
    # Spaces before the END of a paragraph render as nothing: still forgiven.
    trailing = "# Card\n\nFirst paragraph here  \n\nSecond paragraph\n"
    result, edits = _apply(
        trailing,
        [
            {
                "op": "replace_text",
                "old_text": "First paragraph here\n\nSecond",
                "new_text": "Replaced\n\nSecond",
            }
        ],
    )
    assert edits[0].match_kind == "normalized"
    assert result == "# Card\n\nReplaced\n\nSecond paragraph\n"


def test_a_fuzzy_match_never_drops_a_hard_break():
    body = (
        "# Card\n\nThe delivery address for every order is  \n"
        "the warehouse on the north side of town.\n"
    )
    error = _refusal(
        body,
        [
            {
                "op": "replace_text",
                "old_text": (
                    "The delivery adress for every order is\n"
                    "the warehouse on the north side of town."
                ),
                "new_text": "x",
                "at": {"by": "heading_path", "heading_path": ["Card"]},
            }
        ],
    )
    assert error.error_code in {"text_match_not_found", "text_match_low_confidence"}


def test_a_crlf_page_keeps_crlf_after_a_normalized_match():
    body = "# Notes\r\n\r\nPara one\r\nstill para\r\n\r\nNext\r\n"
    result, edits = _apply(
        body,
        [
            {
                "op": "replace_text",
                "old_text": "Para one\nstill para",
                "new_text": "Para 1\nstill para",
            }
        ],
    )
    assert edits[0].match_kind == "normalized"
    assert result == "# Notes\r\n\r\nPara 1\r\nstill para\r\n\r\nNext\r\n"
    # No bare LF anywhere: the page does not end up with mixed endings.
    assert "\n" not in result.replace("\r\n", "")


def test_code_nested_in_a_list_is_as_protected_as_top_level_code():
    # A tilde fence: a backtick fence is also caught by the inline-code
    # pattern, so only this form proves the nested block itself is protected.
    fence = "~~~"
    body = (
        "# Steps\n\n1. Run:\n\n"
        f"   {fence}python\n   if x:\n              return   1\n   {fence}\n\n"
        f"2. Then:\n\n   {fence}sh\n   echo 'please restart the server now'\n"
        f"   {fence}\n"
    )
    # Indentation inside code is not "repeated spaces".
    collapsed = _refusal(
        body,
        [{"op": "replace_text", "old_text": "if x:\n return 1", "new_text": "y"}],
    )
    assert collapsed.error_code == "text_match_not_found"
    # And a slip inside code is never guessed, even scoped to the section.
    slipped = _refusal(
        body,
        [
            {
                "op": "replace_text",
                "old_text": "echo 'please restart the servr now'",
                "new_text": "echo 'done'",
                "at": {"by": "heading_path", "heading_path": ["Steps"]},
            }
        ],
    )
    assert slipped.error_code in {"text_match_not_found", "text_match_low_confidence"}


def test_code_nested_in_a_blockquote_is_protected():
    # A tilde fence: backtick fences are also caught by the inline-code
    # pattern, so only this form proves the block itself is protected.
    body = "# Q\n\n> Note:\n>\n> ~~~sh\n> run   --all   now\n> ~~~\n"
    error = _refusal(
        body,
        [{"op": "replace_text", "old_text": "run --all now", "new_text": "x"}],
    )
    assert error.error_code == "text_match_not_found"


def test_frontmatter_is_never_normalized():
    body = "---\ntitle:  Weekly  report\n---\n\n# Weekly report\n\nBody text.\n"
    error = _refusal(
        body,
        [{"op": "replace_text", "old_text": "title: Weekly report", "new_text": "x"}],
    )
    assert error.error_code == "text_match_not_found"


def test_a_failing_entry_is_named_by_its_batch_index():
    error = _refusal(
        BODY,
        [
            {"op": "replace_text", "old_text": "Keep the default", "new_text": "Use"},
            {
                "op": "replace_text",
                "old_text": "no such sentence here",
                "new_text": "x",
            },
        ],
    )
    assert str(error).startswith("operations[1] (replace_text): ")
    assert [detail.operation_index for detail in error.details] == [1]


def test_every_operation_error_names_its_index_even_without_a_detail():
    body = "# Guide\n\nText.\n"
    error = _refusal(
        body,
        [
            {"op": "replace_text", "old_text": "Text.", "new_text": "Done."},
            {
                "op": "replace_section",
                "at": {"by": "heading_path", "heading_path": ["Missing"]},
                "expected_section_hash": "0" * 64,
                "content": "x",
            },
        ],
    )
    assert error.error_code == "section_not_found"
    assert error.details[0].operation_index == 1
    assert error.details[0].field == "operations[1]"


@pytest.mark.parametrize(
    "operation",
    [
        {"op": "replace_block", "block": "b", "expected": "0" * 64, "content": "x"},
        {"op": "append_document", "content": "x"},
        {"op": "delete_block", "block": "b", "expected": "0" * 64},
    ],
)
def test_accept_fuzzy_is_not_offered_where_it_has_nothing_to_loosen(operation):
    with pytest.raises(ValidationError):
        _OPERATION.validate_python({**operation, "accept_fuzzy": True})


def test_the_receipt_says_a_match_was_forgiven():
    block_map = _map(BODY)
    operations = [
        {
            "op": "replace_text",
            "old_text": TYPO,
            "new_text": "Replaced.",
            "at": _at_block(BODY, "Install the package"),
        }
    ]
    _result, edits = _apply(BODY, operations, block_map)
    receipts = patch_operation_receipts(
        body=BODY,
        operations=[_OPERATION.validate_python(item) for item in operations],
        edits=edits,
        block_map=block_map,
    )
    assert receipts[0].match_kind == "fuzzy"
    assert receipts[0].match_similarity == edits[0].match_similarity
    assert receipts[0].match_policy == MATCH_POLICY
    # The hash of what was really replaced -- the page's sentence, not the
    # caller's slipped copy of it.
    assert (
        receipts[0].matched_sha256
        == hashlib.sha256(SENTENCE.encode("utf-8")).hexdigest()
    )
    assert receipts[0].line_range == (5, 5)


def test_the_legacy_field_list_matches_the_operations_that_keep_accept_fuzzy():
    from backend.schemas import context_edit

    declared = {
        cls.model_fields["op"].annotation.__args__[0]
        for cls in vars(context_edit).values()
        if isinstance(cls, type)
        and issubclass(cls, context_edit._Operation)
        and "accept_fuzzy" in cls.model_fields
        and "op" in cls.model_fields
    }
    assert declared == context_edit.ANCHOR_SCOPED_OPERATION_KINDS

    # A corrupted value is left for validation to reject.
    corrupt = context_edit.without_legacy_operation_fields(
        [{"op": "replace_block", "accept_fuzzy": {"corrupt": True}}]
    )
    assert corrupt[0]["accept_fuzzy"] == {"corrupt": True}

    stored = [
        {
            "op": "replace_block",
            "block": "b",
            "expected": "0" * 64,
            "content": "x",
            "accept_fuzzy": False,
        },
        {"op": "replace_text", "old_text": "a", "new_text": "b", "accept_fuzzy": True},
    ]
    cleaned = context_edit.without_legacy_operation_fields(stored)
    assert "accept_fuzzy" not in cleaned[0]
    assert cleaned[1]["accept_fuzzy"] is True
    for operation in cleaned:
        _OPERATION.validate_python(operation)


# A Markdown hard break: two trailing spaces before a non-blank line.
_CARD = "# Card\n\nAddress line one on this street  \nAddress line two\n"
_POEM = (
    "# Poem\n\nThis is the first line of the verse  \n"
    "This is the second line of the verse\n"
)


def test_a_section_scoped_slip_keeps_the_hard_break_it_does_not_name():
    # M-7. old_text ends in a tab where the page has the break's spaces. The
    # normalized tier refuses that hit, and the fuzzy tier took the same span
    # back at similarity 1.0, splicing new_text over the break-spaces: the
    # two lines rendered as one. Fuzzy candidates obey the same rule now.
    result, edits = _apply(
        _CARD,
        [
            {
                "op": "replace_text",
                "old_text": "Address line one on this street\t",
                "new_text": "Address line 1 on this street",
                "at": {"by": "heading_path", "heading_path": ["Card"]},
            }
        ],
    )
    assert result == "# Card\n\nAddress line 1 on this street  \nAddress line two\n"
    assert edits[0].match_kind == "fuzzy"


def test_a_block_scoped_slip_ending_in_a_space_never_eats_a_hard_break():
    error = _refusal(
        _POEM,
        [
            {
                "op": "replace_text",
                "old_text": "This is the firss line of the verse ",
                "new_text": "This is the opening line of the poem ",
                "at": _at_block(_POEM, "first line"),
            }
        ],
    )
    assert error.error_code == "text_match_low_confidence"


def test_curly_quotes_and_a_trailing_space_keep_the_hard_break():
    body = (
        "# Poem\n\nThis is the ‘first’ line of the verse  \nThis is the second line\n"
    )
    result, _edits = _apply(
        body,
        [
            {
                "op": "replace_text",
                "old_text": "This is the 'first' line of the verse ",
                "new_text": "Opening line ",
                "at": _at_block(body, "first"),
            }
        ],
    )
    assert result == "# Poem\n\nOpening line   \nThis is the second line\n"


def test_an_unscoped_trailing_tab_names_the_hard_break_it_would_eat():
    error = _refusal(
        _CARD,
        [
            {
                "op": "replace_text",
                "old_text": "Address line one on this street\t",
                "new_text": "Address line 1 on this street",
            }
        ],
    )
    assert error.error_code == "text_match_low_confidence"
    assert "hard break" in str(error)


def test_a_hit_refused_for_a_hard_break_still_counts_as_an_occurrence():
    # Left out of the count, the hard-break hit let the second occurrence
    # pass as the only one, and the edit landed on the line the caller had
    # not copied from.
    body = "# T\n\n‘foo’ bar baz  \nnext line\n\n‘foo’ bar baz qux\n"
    error = _refusal(
        body,
        [{"op": "replace_text", "old_text": "'foo' bar baz ", "new_text": "x "}],
    )
    assert error.error_code == "text_occurrence_mismatch"
    assert [candidate.line_start for candidate in error.details[0].candidates] == [
        3,
        6,
    ]


def test_new_lines_inside_one_line_of_a_crlf_page_are_written_as_crlf():
    # The matched span has no line break of its own; new_text's breaks then
    # follow the page instead of arriving as bare LF in a CRLF page.
    body = "# Notes\r\n\r\nOne  line here.\r\n\r\nNext\r\n"
    result, edits = _apply(
        body,
        [
            {
                "op": "replace_text",
                "old_text": "One line here.",
                "new_text": "First line.\nSecond line.",
            }
        ],
    )
    assert edits[0].match_kind == "normalized"
    assert result == "# Notes\r\n\r\nFirst line.\r\nSecond line.\r\n\r\nNext\r\n"

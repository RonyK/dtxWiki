"""MCP-07: ``redact_tool_arguments`` must be idempotent for bounded summaries.

``redact_tool_capture`` runs three times per call (runner.py calls it twice,
then ``persist_mcp_tool_trace`` calls it a third time against the *already
redacted* summary). Derived "bytes"/"sha256" must survive those internal
passes, while identically named extras on raw JSON-RPC input must never be
trusted as the fingerprint of a failed upload.
"""

import base64
import hashlib
import json

import pytest

from backend.services.mcp_trace_redaction import (
    redact_tool_arguments,
    redact_tool_capture,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"not-a-real-png-but-thats-fine-here"


def _upload_arguments() -> dict:
    return {
        "workspace_id": "11111111-1111-1111-1111-111111111111",
        "filename": "diagram.png",
        "mime_type": "image/png",
        "marker_uuid": "22222222-2222-2222-2222-222222222222",
        "page_id": "55555555-5555-5555-5555-555555555555",
        "editing_version_id": "44444444-4444-4444-4444-444444444444",
        "content_base64": base64.b64encode(PNG_BYTES).decode("ascii"),
    }


def test_redact_tool_arguments_derives_bytes_and_sha256_on_first_pass():
    summary = redact_tool_arguments("upload_image", _upload_arguments())
    assert summary["bytes"] == len(PNG_BYTES)
    assert summary["sha256"] == hashlib.sha256(PNG_BYTES).hexdigest()
    assert "content_base64" not in summary


def test_redact_tool_arguments_is_idempotent_on_reentry_without_payload():
    """No ``payload["data"]`` to restore from -- the shape of a rejected
    upload, which has no success envelope to recover the fingerprint from."""

    first_pass = redact_tool_arguments("upload_image", _upload_arguments())
    second_pass = redact_tool_arguments("upload_image", first_pass)
    third_pass = redact_tool_arguments("upload_image", second_pass)

    assert second_pass == first_pass
    assert third_pass == first_pass
    assert second_pass["bytes"] == len(PNG_BYTES)
    assert second_pass["sha256"] == hashlib.sha256(PNG_BYTES).hexdigest()


def test_redact_tool_arguments_is_idempotent_with_success_payload():
    arguments = _upload_arguments()
    payload = {
        "data": {
            "asset_id": "33333333-3333-3333-3333-333333333333",
            "logical_ref": ("dtx-media://asset/33333333-3333-3333-3333-333333333333"),
            "content_url": (
                "/api/v1/pages/55555555-5555-5555-5555-555555555555/versions/"
                "44444444-4444-4444-4444-444444444444/media/"
                "33333333-3333-3333-3333-333333333333/content"
            ),
            "mime_type": "image/png",
            "bytes": len(PNG_BYTES),
            "sha256": hashlib.sha256(PNG_BYTES).hexdigest(),
            "marker_uuid": arguments["marker_uuid"],
            "reused": False,
        }
    }
    first_pass = redact_tool_arguments("upload_image", arguments, payload=payload)
    second_pass = redact_tool_arguments("upload_image", first_pass, payload=payload)

    assert second_pass == first_pass
    assert "url" not in second_pass
    assert second_pass["logical_ref"].startswith("dtx-media://asset/")
    assert (
        second_pass["editing_version_id"] == _upload_arguments()["editing_version_id"]
    )


def test_redact_tool_arguments_overwrites_raw_forged_fingerprint():
    arguments = {
        **_upload_arguments(),
        "bytes": 0,
        "sha256": "0" * 64,
    }

    summary = redact_tool_arguments("upload_image", arguments)

    assert summary["bytes"] == len(PNG_BYTES)
    assert summary["sha256"] == hashlib.sha256(PNG_BYTES).hexdigest()


def test_redact_tool_arguments_drops_raw_forged_fingerprint_when_content_invalid():
    arguments = {
        **_upload_arguments(),
        "content_base64": "not-valid-base64!",
        "bytes": 0,
        "sha256": "0" * 64,
    }

    first_pass = redact_tool_arguments("upload_image", arguments)
    second_pass = redact_tool_arguments("upload_image", first_pass)

    assert "bytes" not in first_pass
    assert "sha256" not in first_pass
    assert second_pass == first_pass


WRITE_BODY_CASES = (
    ("create_context_page", (), "body_md"),
    ("update_context_edit", (), "body_md"),
    ("write_agent_page_draft", (), "body_markdown"),
    ("submit_revision_for_review", ("update",), "body_markdown"),
    ("submit_revision_for_review", ("create",), "body_markdown"),
)


BODY_TAIL_SECRET = "BODY-TAIL-SECRET-past-the-preview-window"


def _write_arguments(tool_name: str, path: tuple[str, ...], body_field: str) -> dict:
    body_container = {
        body_field: "# Durable trace secret\n\n"
        + "large body " * 60
        + BODY_TAIL_SECRET,
        "reason": "Keep this short identifier",
    }
    if tool_name == "create_context_page":
        body_container.update(title="Trace test", page_type="guide")
    elif tool_name == "update_context_edit":
        body_container.update(
            page_id="11111111-1111-1111-1111-111111111111",
            base_version_id="22222222-2222-2222-2222-222222222222",
            base_content_hash="a" * 64,
            expected_editing_revision=1,
        )
    elif tool_name == "write_agent_page_draft":
        body_container.update(
            workspace_id="33333333-3333-3333-3333-333333333333",
            category="memory",
            title="Trace",
            slug="trace",
        )

    if not path:
        return body_container
    return {
        "workspace_id": "33333333-3333-3333-3333-333333333333",
        "review_item_id": "44444444-4444-4444-4444-444444444444",
        path[0]: body_container,
        "note": "Revision note",
    }


def _at_path(value: dict, path: tuple[str, ...]) -> dict:
    for part in path:
        value = value[part]
    return value


@pytest.mark.parametrize(("tool_name", "path", "body_field"), WRITE_BODY_CASES)
def test_write_body_arguments_are_bounded_and_idempotent(
    tool_name: str,
    path: tuple[str, ...],
    body_field: str,
) -> None:
    arguments = _write_arguments(tool_name, path, body_field)
    raw_body = _at_path(arguments, path)[body_field]

    first_pass = redact_tool_arguments(tool_name, arguments)
    second_pass = redact_tool_arguments(tool_name, first_pass)
    third_pass, payload = redact_tool_capture(tool_name, second_pass, {"data": {}})

    summary = _at_path(first_pass, path)
    encoded = raw_body.encode("utf-8")
    assert body_field not in summary
    assert summary[f"{body_field}_digest"] == {
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "chars": len(raw_body),
        "bytes": len(encoded),
    }
    # Only a bounded prefix may survive; never the whole body.
    serialized = json.dumps(first_pass, ensure_ascii=False)
    assert raw_body not in serialized
    assert BODY_TAIL_SECRET not in serialized
    if tool_name == "update_context_edit":
        # A whole-body autosave always starts with the same document preamble,
        # so its prefix says nothing about the edit: digest only.
        assert f"{body_field}_preview" not in summary
    else:
        preview = summary[f"{body_field}_preview"]
        assert preview["truncated"] is True
        assert preview["text"].startswith("# Durable trace secret\n\nlarge body")
        assert preview["text"].endswith("…")
        assert len(preview["text"]) <= 301
    assert second_pass == first_pass
    assert third_pass == first_pass
    assert payload == {"data": {}}


@pytest.mark.parametrize(("tool_name", "path", "body_field"), WRITE_BODY_CASES)
def test_write_body_arguments_overwrite_forged_digest(
    tool_name: str,
    path: tuple[str, ...],
    body_field: str,
) -> None:
    arguments = _write_arguments(tool_name, path, body_field)
    body_container = _at_path(arguments, path)
    raw_body = body_container[body_field]
    body_container[f"{body_field}_digest"] = {
        "sha256": "0" * 64,
        "chars": 0,
        "bytes": 0,
    }
    body_container[f"{body_field}_preview"] = {
        "text": "FORGED-PREVIEW",
        "truncated": False,
    }
    arguments["preview_budget_exhausted"] = 99

    redacted = redact_tool_arguments(tool_name, arguments)
    summary = _at_path(redacted, path)

    assert (
        summary[f"{body_field}_digest"]["sha256"]
        == hashlib.sha256(raw_body.encode("utf-8")).hexdigest()
    )
    # A caller-supplied preview is never trusted: it is recomputed from the
    # raw body, or dropped where this tool keeps a digest only.
    assert "FORGED-PREVIEW" not in json.dumps(redacted)
    assert "preview_budget_exhausted" not in redacted
    if tool_name == "update_context_edit":
        assert f"{body_field}_preview" not in summary
    else:
        assert summary[f"{body_field}_preview"]["text"].startswith("# Durable")


PATCH_DRAFT_SECRET = "TRACE-SECRET-6a1f9d3c-live-page-content"


def _patch_operations_arguments() -> dict:
    """Argument shape for ``patch_context_edit``, which reuses the exact same
    typed operation/address redaction as the now-deregistered ``patch_draft``
    MCP tool (see ``_redact_patch_draft_operation``/``_redact_patch_draft_address``
    in mcp_trace_redaction.py)."""

    secret = PATCH_DRAFT_SECRET
    return {
        "workspace_id": "11111111-1111-1111-1111-111111111111",
        "page_id": "22222222-2222-2222-2222-222222222222",
        "version_id": "55555555-5555-5555-5555-555555555555",
        "expected_editing_revision": 1,
        "operations": [
            {
                "op": "replace_text",
                "old_text": f"{secret}-old",
                "new_text": f"{secret}-new",
                "expected_occurrences": 1,
                "accept_fuzzy": False,
                "at": {
                    "by": "anchor",
                    "anchor_text": f"{secret}-anchor",
                    "context_before": f"{secret}-before",
                    "context_after": f"{secret}-after",
                    "char_offset_hint": 12,
                },
            },
            {
                "op": "insert_text",
                "at": {
                    "by": "heading_path",
                    "heading_path": ["Doc", "Section A"],
                    "occurrence": 1,
                },
                "new_text": f"{secret}-inserted",
                "side": "after",
                "accept_fuzzy": False,
            },
            {
                "op": "append_document",
                "content": f"{secret}-appended",
                "accept_fuzzy": False,
            },
            {
                "op": "replace_document",
                "content": f"{secret}-replaced-doc",
                "expected_content_hash": "a" * 64,
                "accept_fuzzy": False,
            },
            {
                "op": "replace_section",
                "at": {"by": "section_id", "section_id": "sec-123"},
                "content": f"{secret}-replaced-section",
                "expected_section_hash": "b" * 64,
                "accept_fuzzy": False,
            },
            {
                "op": "insert_section",
                "at": {"by": "section_id", "section_id": "sec-456"},
                "position": "after",
                "title": f"{secret}-title",
                "content": f"{secret}-new-section",
                "accept_fuzzy": False,
            },
            {
                "op": "delete_section",
                "at": {"by": "section_id", "section_id": "sec-789"},
                "expected_section_hash": "c" * 64,
                "accept_fuzzy": False,
            },
        ],
    }


def test_patch_context_edit_content_fields_are_redacted() -> None:
    """Every content-bearing field across every operation shape -- old_text,
    new_text, content, title, and the anchor-address trio -- is reduced to a
    digest, no matter which operation carried it. The edited fields also keep
    a bounded preview; the landmark trio only locates the edit in surrounding
    text the caller did not change, so it keeps nothing readable."""

    arguments = _patch_operations_arguments()

    summary = redact_tool_arguments("patch_context_edit", arguments)
    serialized = json.dumps(summary)

    for landmark in ("anchor", "before", "after"):
        assert f"{PATCH_DRAFT_SECRET}-{landmark}" not in serialized

    replace_text_op = summary["operations"][0]
    assert "old_text" not in replace_text_op
    assert "new_text" not in replace_text_op
    old_text = arguments["operations"][0]["old_text"]
    encoded = old_text.encode("utf-8")
    assert replace_text_op["old_text_digest"] == {
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "chars": len(old_text),
        "bytes": len(encoded),
    }
    assert replace_text_op["old_text_preview"] == {
        "text": f"{PATCH_DRAFT_SECRET}-old",
        "truncated": False,
    }
    assert replace_text_op["new_text_preview"]["text"] == f"{PATCH_DRAFT_SECRET}-new"

    anchor = replace_text_op["at"]
    assert "anchor_text" not in anchor
    assert "context_before" not in anchor
    assert "context_after" not in anchor
    assert "anchor_text_digest" in anchor
    assert not any(key.endswith("_preview") for key in anchor)

    ops = summary["operations"]
    assert ops[1]["new_text_preview"]["text"] == f"{PATCH_DRAFT_SECRET}-inserted"
    assert ops[2]["content_preview"]["text"] == f"{PATCH_DRAFT_SECRET}-appended"
    assert ops[5]["title_preview"]["text"] == f"{PATCH_DRAFT_SECRET}-title"
    assert "preview_budget_exhausted" not in summary


def test_patch_context_edit_keeps_audit_metadata_verbatim() -> None:
    """Fields that describe *where* and *how* an edit happened -- not the
    document content itself -- stay durable so a trace remains useful for
    audit: op name, addressing (section_id / heading_path / occurrence /
    char_offset_hint), expected_*_hash preconditions, and top-level request
    metadata (page_id, version_id, expected_editing_revision)."""

    arguments = _patch_operations_arguments()

    summary = redact_tool_arguments("patch_context_edit", arguments)

    assert summary["page_id"] == arguments["page_id"]
    assert summary["version_id"] == arguments["version_id"]
    assert (
        summary["expected_editing_revision"] == arguments["expected_editing_revision"]
    )

    ops = summary["operations"]
    assert ops[0]["op"] == "replace_text"
    assert ops[0]["expected_occurrences"] == 1
    assert ops[0]["at"]["by"] == "anchor"
    assert ops[0]["at"]["char_offset_hint"] == 12

    assert ops[1]["at"]["by"] == "heading_path"
    assert ops[1]["at"]["heading_path"] == ["Doc", "Section A"]
    assert ops[1]["at"]["occurrence"] == 1
    assert ops[1]["side"] == "after"

    assert ops[3]["expected_content_hash"] == "a" * 64

    assert ops[4]["at"]["section_id"] == "sec-123"
    assert ops[4]["expected_section_hash"] == "b" * 64

    assert ops[5]["position"] == "after"

    assert ops[6]["expected_section_hash"] == "c" * 64


def test_patch_context_edit_block_operations_metadata_is_allowlisted() -> None:
    secret = f"{PATCH_DRAFT_SECRET}-block-content"
    arguments = {
        "workspace_id": "11111111-1111-1111-1111-111111111111",
        "page_id": "22222222-2222-2222-2222-222222222222",
        "version_id": "55555555-5555-5555-5555-555555555555",
        "expected_editing_revision": 1,
        "operations": [
            {
                "op": "replace_block",
                "block": "b_" + "a1" * 16,
                "expected": "a" * 64,
                "content": secret,
                "accept_fuzzy": False,
            },
            {
                "op": "insert_after",
                "block": "b_" + "a2" * 16,
                "expected": "b" * 64,
                "content": secret,
                "accept_fuzzy": False,
            },
            {
                "op": "insert_row",
                "row": "r_" + "a3" * 16,
                "expected": "c" * 64,
                "content": secret,
                "accept_fuzzy": False,
            },
            {
                "op": "delete_block",
                "block": "b_" + "a4" * 64,
                "expected": "d" * 64,
                "accept_fuzzy": False,
            },
            {
                "op": "resolve_conflict",
                "block": "b_retired",
                "resolution": "keep_head",
            },
        ],
    }

    summary = redact_tool_arguments("patch_context_edit", arguments)

    assert all("content" not in operation for operation in summary["operations"])
    assert [operation["op"] for operation in summary["operations"]] == [
        "replace_block",
        "insert_after",
        "insert_row",
        "delete_block",
        "resolve_conflict",
    ]
    assert summary["operations"][0]["block"] == "b_" + "a1" * 16
    assert summary["operations"][2]["row"] == "r_" + "a3" * 16
    # Only server-minted ids are kept verbatim; any other value in an id
    # field is caller text and is digested like content.
    assert "block" not in summary["operations"][4]
    assert "block_digest" in summary["operations"][4]
    assert summary["operations"][3]["expected"] == "d" * 64
    assert summary["operations"][4]["resolution"] == "keep_head"
    assert "content_digest" in summary["operations"][0]
    assert summary["operations"][0]["content_preview"]["text"] == secret


def test_patch_context_edit_redacts_unknown_operation_fields_by_default() -> None:
    """Guards the allowlist design itself. Redaction here keeps a fixed set of
    known-safe metadata fields and treats everything else as content, rather
    than the reverse (a blocklist of known content fields). A field the
    schema grows tomorrow that this module has never heard of must still be
    redacted today -- proving that forgetting to update this module when a
    new content-bearing field is added fails safe instead of leaking it."""

    unknown_secret = "UNKNOWN-FIELD-SECRET-2c9e"
    arguments = _patch_operations_arguments()
    arguments["operations"][0]["future_content_field"] = unknown_secret
    # A preview-shaped key is just another unknown field on the wire: it must
    # not be mistaken for (or overwrite) the preview this module derives.
    arguments["operations"][0]["old_text_preview"] = {
        "text": unknown_secret,
        "truncated": False,
    }

    summary = redact_tool_arguments("patch_context_edit", arguments)

    assert unknown_secret not in json.dumps(summary)
    op = summary["operations"][0]
    assert "future_content_field" not in op
    assert "future_content_field_digest" not in op
    assert op["old_text_preview"]["text"] == f"{PATCH_DRAFT_SECRET}-old"
    assert len(op["unknown_fields"]) == 2
    unknown = op["unknown_fields"][0]
    expected_key = json.dumps(
        "future_content_field",
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    expected_value = json.dumps(
        unknown_secret,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert unknown["key_digest"]["sha256"] == hashlib.sha256(expected_key).hexdigest()
    assert (
        unknown["value_digest"]["sha256"] == hashlib.sha256(expected_value).hexdigest()
    )


def test_patch_context_edit_redaction_is_idempotent() -> None:
    arguments = _patch_operations_arguments()

    first_pass = redact_tool_arguments("patch_context_edit", arguments)
    second_pass = redact_tool_arguments("patch_context_edit", first_pass)
    third_pass, payload = redact_tool_capture("patch_context_edit", second_pass, {})

    assert second_pass == first_pass
    assert third_pass == first_pass
    assert payload == {}


def test_non_write_tool_arguments_remain_an_exact_deepcopy() -> None:
    arguments = {
        "page_id": "11111111-1111-1111-1111-111111111111",
        "nested": {"body_markdown": "read tools still pass through"},
    }

    summary = redact_tool_arguments("read", arguments)

    assert summary == arguments
    assert summary is not arguments
    assert summary["nested"] is not arguments["nested"]


def test_body_preview_skips_frontmatter_and_leading_media_embeds() -> None:
    asset = "dtx-media://asset/33333333-3333-3333-3333-333333333333"
    body = (
        "---\n"
        "title: FRONTMATTER-SECRET\n"
        "tags: [a, b]\n"
        "---\n"
        f"![diagram]({asset})\n"
        "\n"
        f'![second]({asset} "caption")\n'
        "# Real heading\n\nFirst paragraph."
    )

    summary = redact_tool_arguments(
        "create_context_page", {"title": "T", "body_md": body}
    )

    assert summary["body_md_preview"] == {
        "text": "# Real heading\n\nFirst paragraph.",
        "truncated": False,
    }
    assert "FRONTMATTER-SECRET" not in json.dumps(summary)
    assert summary["body_md_digest"]["chars"] == len(body)


def test_preview_cut_never_splits_a_grapheme_cluster() -> None:
    zwj = chr(0x200D)
    family = chr(0x1F468) + zwj + chr(0x1F469) + zwj + chr(0x1F467)
    text = "a" * 298 + family + "tail"

    summary = redact_tool_arguments(
        "patch_context_edit",
        {"operations": [{"op": "append_document", "content": text}]},
    )

    preview = summary["operations"][0]["content_preview"]
    assert preview["truncated"] is True
    # The five-code-point family does not fit in the two remaining characters,
    # so the cut lands before it instead of inside it.
    assert preview["text"] == "a" * 298 + "…"


def test_preview_budget_caps_a_call_and_counts_the_fields_it_skipped() -> None:
    operations = [
        {
            "op": "replace_text",
            "old_text": f"old-{index}-" + "x" * 400,
            "new_text": f"new-{index}-" + "y" * 400,
            "at": {"by": "anchor", "anchor_text": "landmark " * 50},
        }
        for index in range(12)
    ]

    summary = redact_tool_arguments("patch_context_edit", {"operations": operations})

    previews = [
        operation[field]
        for operation in summary["operations"]
        for field in ("old_text_preview", "new_text_preview")
        if field in operation
    ]
    assert sum(len(preview["text"]) for preview in previews) <= 6000
    # 301 characters per truncated field: 19 fit in 6,000, the other 5 do not.
    assert len(previews) == 19
    assert summary["preview_budget_exhausted"] == 24 - 19
    last = summary["operations"][-1]
    assert "old_text_preview" not in last
    assert "new_text_preview" not in last
    assert last["old_text_digest"]["chars"] == len(operations[-1]["old_text"])
    # Landmarks never spend the budget.
    assert all(
        not any(key.endswith("_preview") for key in operation["at"])
        for operation in summary["operations"]
    )
    assert redact_tool_arguments("patch_context_edit", summary) == summary


def test_preview_masks_credentials_and_url_parameters() -> None:
    jwt = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.payload.signature"
    text = (
        "Call with Bearer abcdefghijklmnop123 and dtxw_live_secret_token; "
        f"jwt {jwt} at https://example.test/cb?code=URL-SECRET#frag "
        # Split so the source holds no `password: <value>` literal for a
        # secret scanner to trip on; the runtime string is unchanged.
        "pass" + "word: hunter2-SECRET"
    )

    summary = redact_tool_arguments(
        "patch_context_edit",
        {"operations": [{"op": "insert_text", "new_text": text}]},
    )

    preview = summary["operations"][0]["new_text_preview"]["text"]
    for secret in (
        "abcdefghijklmnop123",
        "dtxw_live_secret_token",
        jwt,
        "URL-SECRET",
        "frag",
        "hunter2-SECRET",
    ):
        assert secret not in preview
    assert "https://example.test/cb" in preview
    assert "[redacted]" in preview


def test_non_string_content_fields_keep_a_digest_but_no_preview() -> None:
    summary = redact_tool_arguments(
        "patch_context_edit",
        {"operations": [{"op": "insert_text", "new_text": ["not", "a", "string"]}]},
    )

    operation = summary["operations"][0]
    assert "new_text_digest" in operation
    assert "new_text_preview" not in operation


def _edit_receipt() -> dict:
    return {
        "edit_mode": "patch",
        "editing_revision_before": 3,
        "editing_revision_after": 4,
        "chars_added": 12,
        "chars_removed": 5,
        "lines_added": 1,
        "lines_removed": 0,
        "changed_sections": [["Guide", "Setup"]],
        "first_change": {
            "line": 7,
            "before_preview": "token=RECEIPT-SECRET old line",
            "after_preview": "x" * 500,
        },
        "operations": [
            {
                "index": 0,
                "op": "replace_text",
                "line_range": [7, 7],
                "heading_path": ["Guide", "Setup"],
                "affected_block_ids": ["b_1"],
                "chars_added": 12,
                "chars_removed": 5,
                "smuggled": "UNLISTED-RECEIPT-FIELD",
            }
        ],
        "smuggled_top_level": "UNLISTED-RECEIPT-FIELD",
    }


def test_edit_receipt_is_allowlisted_and_its_previews_are_masked() -> None:
    from backend.services.mcp_trace_redaction import redact_edit_receipt

    receipt = redact_edit_receipt(_edit_receipt())

    assert receipt is not None
    assert "UNLISTED-RECEIPT-FIELD" not in json.dumps(receipt)
    assert "RECEIPT-SECRET" not in receipt["first_change"]["before_preview"]
    assert receipt["first_change"]["after_preview"].endswith("…")
    assert len(receipt["first_change"]["after_preview"]) <= 301
    assert receipt["operations"] == [
        {
            "index": 0,
            "op": "replace_text",
            "line_range": [7, 7],
            "heading_path": ["Guide", "Setup"],
            "affected_block_ids": ["b_1"],
            "chars_added": 12,
            "chars_removed": 5,
        }
    ]
    assert receipt["changed_sections"] == [["Guide", "Setup"]]
    assert receipt["editing_revision_after"] == 4
    assert redact_edit_receipt(receipt) == receipt
    assert redact_edit_receipt("not a receipt") is None
    assert redact_edit_receipt({"edit_mode": "bogus"}) is None


def test_edit_receipt_accepts_the_in_process_shape() -> None:
    """The MCP handler serializes the receipt with ``model_dump()`` (not JSON
    mode), so a line range may still be a tuple and a revision may be None."""

    from backend.services.mcp_trace_redaction import redact_edit_receipt

    receipt = redact_edit_receipt(
        {
            "edit_mode": "create",
            "editing_revision_before": None,
            "editing_revision_after": None,
            "chars_added": 5,
            "chars_removed": 0,
            "lines_added": 1,
            "lines_removed": 0,
            "changed_sections": [("Intro",)],
            "first_change": None,
            "operations": [
                {
                    "index": 0,
                    "op": "insert_after",
                    "line_range": (2, 3),
                    "heading_path": ("Intro",),
                    "affected_block_ids": ["b_9"],
                    "chars_added": 5,
                    "chars_removed": 0,
                }
            ],
            "changed_blocks": [{"id": "b_9", "kind": "paragraph", "hash": "f" * 64}],
        }
    )

    assert receipt is not None
    assert receipt["editing_revision_after"] is None
    assert receipt["changed_sections"] == [["Intro"]]
    assert receipt["operations"][0]["line_range"] == [2, 3]
    assert receipt["operations"][0]["heading_path"] == ["Intro"]
    # Block ids/kinds/hashes are addresses, not content: kept as sent, but
    # still typed -- an entry that is not a well-formed block is dropped.
    assert receipt["changed_blocks"] == [
        {"id": "b_9", "kind": "paragraph", "hash": "f" * 64}
    ]


def test_edit_receipt_drops_malformed_changed_blocks() -> None:
    from backend.services.mcp_trace_redaction import redact_edit_receipt

    receipt = redact_edit_receipt(
        {
            "edit_mode": "patch",
            "changed_blocks": [
                {"id": "b_1", "kind": "heading", "hash": "a" * 64},
                {"id": "b_2", "kind": "paragraph", "hash": "BLOCK TEXT SECRET"},
                {"id": "b 3 secret", "kind": "paragraph", "hash": "b" * 64},
                {"id": "b_4", "kind": "paragraph", "hash": "c" * 64, "text": "x"},
                "not a block",
            ],
        }
    )

    assert receipt["changed_blocks"] == [
        {"id": "b_1", "kind": "heading", "hash": "a" * 64},
        {"id": "b_4", "kind": "paragraph", "hash": "c" * 64},
    ]


def test_error_details_keep_their_structure_and_never_their_prose() -> None:
    from backend.services.mcp_trace_redaction import redact_error_details

    details = redact_error_details(
        [
            {
                "field": "operations[2]",
                "code": "operations_overlap",
                "message": (
                    "operations[0] (replace_text, lines 4-6) and operations[2] "
                    "(insert_after, line 5) target overlapping text: "
                    "one range contains the other"
                ),
                "fix": "Merge the two operations into one.",
            },
            {
                "field": "operations[1].old_text",
                "code": "text_not_found",
                "message": "old_text 'DETAIL-MESSAGE-SECRET' was not found",
                "fix": "Re-read the section.",
            },
            {"field": "has spaces SECRET", "code": "Bad Code", "message": "x"},
            {"field": None, "code": "stale_base_version", "message": "stale"},
            "not a detail",
        ]
    )

    assert details == [
        {
            "code": "operations_overlap",
            "field": "operations[2]",
            "operation_index": 2,
            "conflict": {
                "left_op_index": 0,
                "right_op_index": 2,
                "left_lines": [4, 6],
                "right_lines": [5, 5],
                "overlap_kind": "contains",
            },
        },
        {
            "code": "text_not_found",
            "field": "operations[1].old_text",
            "operation_index": 1,
            "conflict": None,
        },
        {
            "code": "stale_base_version",
            "field": None,
            "operation_index": None,
            "conflict": None,
        },
    ]
    assert "SECRET" not in json.dumps(details)


def test_patch_match_settings_and_block_scope_are_kept_as_metadata() -> None:
    """How an operation asked to be matched, and the block it named, describe
    where the edit went -- not document content -- so they stay readable."""

    summary = redact_tool_arguments(
        "patch_context_edit",
        {
            "page_id": "55555555-5555-5555-5555-555555555555",
            "version_id": "44444444-4444-4444-4444-444444444444",
            "expected_editing_revision": 3,
            "operations": [
                {
                    "op": "replace_text",
                    "old_text": "OLD-SECRET",
                    "new_text": "NEW-SECRET",
                    "match_mode": "exact",
                    "at": {
                        "by": "block",
                        "block": "b_" + "7" * 32,
                        "expected": "d" * 64,
                    },
                },
                {
                    "op": "replace_text",
                    "old_text": "x",
                    "new_text": "y",
                    "match_mode": "MATCH-MODE-SECRET",
                    "at": {
                        "by": "block",
                        "block": "CALLER-SECRET-AS-BLOCK-ID",
                        "expected": "not-a-hash",
                    },
                },
            ],
        },
    )

    first, second = summary["operations"]
    assert first["match_mode"] == "exact"
    assert first["at"] == {
        "by": "block",
        "block": "b_" + "7" * 32,
        "expected": "d" * 64,
    }
    # Values outside the allowed shapes are digested, never kept verbatim.
    assert "match_mode" not in second
    assert "expected" not in second["at"]
    assert "block" not in second["at"]
    # (old_text/new_text keep their own short previews; that is covered by
    # the preview tests.)
    dumped = json.dumps(summary)
    for secret in ("MATCH-MODE-SECRET", "not-a-hash", "CALLER-SECRET"):
        assert secret not in dumped


def test_edit_receipt_keeps_how_a_replace_text_matched() -> None:
    from backend.services.mcp_trace_redaction import redact_edit_receipt

    receipt = _edit_receipt()
    receipt["operations"][0] |= {
        "match_kind": "fuzzy",
        "match_similarity": 0.982,
        "match_policy": "safe-v1",
        "matched_sha256": "e" * 64,
    }
    receipt["operations"].append(
        {
            "index": 1,
            "op": "replace_text",
            "match_kind": "guessed",
            "match_similarity": 7.0,
        }
    )

    redacted = redact_edit_receipt(receipt)

    assert redacted is not None
    first, second = redacted["operations"]
    assert (first["match_kind"], first["match_similarity"]) == ("fuzzy", 0.982)
    assert first["matched_sha256"] == "e" * 64
    assert "match_policy" not in first
    assert "match_kind" not in second and "match_similarity" not in second
    assert redact_edit_receipt(redacted) == redacted


def test_error_details_keep_match_candidates_as_positions_only() -> None:
    from backend.services.mcp_trace_redaction import redact_error_details

    details = redact_error_details(
        [
            {
                "field": "operations[1].old_text",
                "code": "text_match_ambiguous",
                "message": "CANDIDATE-PROSE-SECRET",
                "operation_index": 1,
                "candidates": [
                    {
                        "line_start": 7,
                        "line_end": 7,
                        "block_id": "b_7",
                        "similarity": 0.974,
                        "match_kind": "fuzzy",
                    },
                    {
                        "line_start": 9,
                        "line_end": 10,
                        "block_id": "not a block id SECRET",
                        "similarity": 0.97,
                        "match_kind": "fuzzy",
                        "text": "CANDIDATE-TEXT-SECRET",
                    },
                    {
                        "line_start": 0,
                        "line_end": 1,
                        "similarity": 2,
                        "match_kind": "x",
                    },
                ],
            },
            {
                # An explicit index wins over one the field does not name.
                "field": "block:b_1",
                "code": "block_reflow",
                "message": "m",
                "operation_index": 3,
            },
        ]
    )

    assert details[0]["operation_index"] == 1
    assert details[0]["candidates"] == [
        {
            "line_start": 7,
            "line_end": 7,
            "block_id": "b_7",
            "similarity": 0.974,
            "match_kind": "fuzzy",
        },
        {
            "line_start": 9,
            "line_end": 10,
            "block_id": None,
            "similarity": 0.97,
            "match_kind": "fuzzy",
        },
    ]
    assert details[1]["operation_index"] == 3
    assert "candidates" not in details[1]
    assert "SECRET" not in json.dumps(details)


def test_previews_keep_line_breaks_and_still_mask_across_them() -> None:
    """Lists, tables and code only read as what they are line by line, so a
    preview keeps its line breaks (CRLF/CR normalised to LF). Masking still
    sees the whole text: a credential whose value sits on the line after its
    label, or a PEM body spread over lines, is still removed."""

    from backend.services.mcp_trace_redaction import redact_edit_receipt

    code = "```python\r\nprint('x')\r\n```"
    listed = "- one\n- two\n\n| a | b |\n|---|---|"
    leaky = (
        "config:\naccess token:\nLINEBREAK-TOKEN-SECRET\nBearer\n"
        "abcdefghijklmnop-SECRET\n-----BEGIN PRIVATE KEY-----\nPEM-SECRET\n"
        "-----END PRIVATE KEY-----\nend"
    )
    summary = redact_tool_arguments(
        "patch_context_edit",
        {
            "operations": [
                {"op": "replace_block", "block": "b_1", "content": code},
                {"op": "insert_after", "block": "b_2", "content": listed},
                {"op": "append_document", "content": leaky},
            ]
        },
    )
    receipt = redact_edit_receipt(
        {
            "edit_mode": "whole_body",
            "first_change": {
                "line": 3,
                "before_preview": "- old\n- items",
                "after_preview": "- new\r\n- items",
            },
        }
    )

    operations = summary["operations"]
    assert operations[0]["content_preview"]["text"] == "```python\nprint('x')\n```"
    assert operations[1]["content_preview"]["text"] == listed
    masked = operations[2]["content_preview"]["text"]
    assert masked.startswith("config:\n")
    assert masked.endswith("\nend")
    for secret in ("LINEBREAK-TOKEN-SECRET", "abcdefghijklmnop", "PEM-SECRET"):
        assert secret not in masked
    assert receipt["first_change"]["before_preview"] == "- old\n- items"
    assert receipt["first_change"]["after_preview"] == "- new\n- items"


def _masked_preview(text: str) -> str:
    summary = redact_tool_arguments(
        "patch_context_edit",
        {"operations": [{"op": "insert_text", "new_text": text}]},
    )
    return summary["operations"][0]["new_text_preview"]["text"]


# Provider credential shapes. Each value is assembled at runtime so the source
# never holds a token-shaped literal for a secret scanner to trip on.
_ALNUM = "Ab3dE6gH9jK2mN5pQ8sT1vW4yZ7bC0eF"
PROVIDER_TOKEN_CASES = (
    ("openai_project", "sk-" + "proj-" + _ALNUM),
    ("openai_legacy", "sk-" + _ALNUM),
    ("anthropic", "sk-" + "ant-api03-" + _ALNUM),
    ("openai_service_account", "sk-" + "svcacct-" + _ALNUM),
    ("github_classic", "gh" + "p_" + _ALNUM + "1234"),
    ("github_oauth", "gh" + "o_" + _ALNUM + "1234"),
    ("github_fine_grained", "github" + "_pat_" + _ALNUM + "_" + _ALNUM),
    ("gitlab", "gl" + "pat-" + _ALNUM[:20]),
    ("aws_access_key", "AK" + "IA" + "IOSFODNN7EXAMPLE"),
    ("aws_session_key", "AS" + "IA" + "Q3EXAMPLEKEY1234"),
    ("google_api_key", "AI" + "za" + (_ALNUM + "123")[:35]),
    ("slack_bot", "xo" + "xb-" + "1234567890-" + _ALNUM),
    (
        "slack_webhook",
        "https://hooks.slack" + ".com/services/T00000000/B00000000/" + _ALNUM,
    ),
    ("stripe_live", "sk" + "_live_" + _ALNUM),
    ("stripe_restricted", "rk" + "_test_" + _ALNUM),
    ("hugging_face", "h" + "f_" + _ALNUM + "xyz"),
    ("npm", "np" + "m_" + _ALNUM + "wxyz"),
    ("sendgrid", "S" + "G." + _ALNUM[:22] + "." + _ALNUM + "abcdefghijk"),
)


@pytest.mark.parametrize(
    ("shape", "token"), PROVIDER_TOKEN_CASES, ids=[c[0] for c in PROVIDER_TOKEN_CASES]
)
def test_preview_masks_provider_credential_shapes(shape: str, token: str) -> None:
    preview = _masked_preview(f"configure it with {token} before deploying")

    assert token not in preview
    assert _ALNUM[:16] not in preview
    assert "[redacted]" in preview
    assert preview.startswith("configure it with ")
    assert preview.endswith(" before deploying")


def test_preview_masks_url_userinfo_but_keeps_the_host() -> None:
    preview = _masked_preview(
        "clone https://deploy:" + "S3cretPassw0rd@git.example.com/r"
    )

    assert "S3cretPassw0rd" not in preview
    assert "deploy:" not in preview
    assert "git.example.com/r" in preview


@pytest.mark.parametrize(
    "line",
    (
        "aws_secret_access_key = " + _ALNUM,
        "GITHUB_TOKEN=" + _ALNUM,
        "db.password: " + _ALNUM,
        "PRIVATE_KEY: " + _ALNUM,
        "x-api-key: " + _ALNUM,
    ),
)
def test_preview_masks_values_of_secret_named_identifiers(line: str) -> None:
    preview = _masked_preview(line)

    assert _ALNUM not in preview
    assert "[redacted]" in preview


def test_masking_leaves_ordinary_prose_and_identifiers_alone() -> None:
    text = (
        "The skill-set and task_id fields stay; a tokenizer = BPE setting too.\n"
        "비밀번호와 토큰은 설정 화면에서 발급합니다.\n"
        "Ask for a key, then run the task. sk-ip the risky step."
    )

    assert _masked_preview(text) == text

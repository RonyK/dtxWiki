---
name: dtxwiki-authoring-review
description: Create or update grounded dtxWiki Context pages, upload images safely, and handle requested-change resubmission without crossing the human approval boundary. Use for every dtxWiki authoring or review-response workflow.
---

# dtxWiki Authoring and Review

Read `dtxwiki-conventions` before writing. Call `guide` for the current write policy, citation policy, placement candidates, and effective capabilities; do not hardcode values it reports.

## Protect the base

Never write against a base you did not read. `update_context_edit` and `patch_context_edit` require the `expected_editing_revision` you last saw (from `create_context_page`, `begin_context_edit`, or the prior `update_context_edit`/`patch_context_edit` response). A stale value always returns a conflict instead of silently overwriting a newer save. On that failure, call `begin_context_edit` again to resume your session on its current state, reconsider the change, and retry with the fresh `expected_editing_revision`. Never force an overwrite.

## Choose the write shape

1. For a new Context page, call `create_context_page`. Title/body/tags/aliases are all optional; an omitted title defaults to a placeholder. If `placement_parent_node_id` is omitted, the page goes to Inbox -- to choose a location, read `guide.placement_parent_candidates` and pass its node id as `placement_parent_node_id`.
2. For an existing page, call `begin_context_edit` to idempotently open or resume your own private "editing" session. Passing `base_version_id`/`base_content_hash` asserts the base explicitly and returns a conflict instead of silently building on a head that moved.
3. Prefer a partial edit over a full rewrite:
   - Call `read_structure_map` for the target page to obtain block ids, row ids, and content hashes. Call `read_context_outline` when you need heading text or one section's body.
   - Call `patch_context_edit` with typed operations addressed by block or row id and hash -- this tool always applies against your own open row, never a shared reconciled head, so there is no conflict-resolution operation to send.
   - Requires the `expected_editing_revision` you last saw as a CAS input; a stale value returns a conflict rather than silently overwriting a newer save.
4. Use `update_context_edit` for a full-document autosave -- title/body/tags/aliases/placement together, always sending the complete `body_md`. There is no metadata-only or citation-only write mode: changing tags, aliases, or placement without also resending the full body currently requires `update_context_edit` with the unchanged body included.
5. Call `submit_context_edit_for_review` when the edit is ready for human review ("Save draft"). Once submitted, that version is never mutated again -- further changes need a new `begin_context_edit`. A submission never publishes on its own; read `data.publish_blocked_reasons` to learn why human review was required.
6. Call `discard_context_edit` to abandon your own open editing session (terminal, not a hard-delete: audit/History is preserved). A page with no prior published/submitted history is archived when its only version is discarded; a previously published/shared page keeps its existing head.

## Turn evidence into citations

`cite` finds policy-allowed candidates and returns a write-ready `write_citation` object per candidate. **Known gap:** the current Context-editing-state-machine tools (`create_context_page`/`update_context_edit`/`patch_context_edit`) have no `citations` field to attach that object to. Use this flow to select evidence and cite it in prose (a Markdown link/reference to the source) until a structured attachment path exists:

1. Find relevant evidence with the retrieval workflow.
2. Read the complete evidence before relying on it.
3. Call `cite` for the claim that needs support.
4. Evaluate the returned candidates and select only evidence that supports the claim under the live citation policy.
5. Reference the selected evidence in the page body itself (do not manufacture citation identifiers, upgrade a weak recommendation, or describe preview verification as persisted citation status).

## Use the safe image flow

Call `upload_image` first. It always requires an existing `page_id` and your own currently-open `editing_version_id` for that page -- there is no page-less upload. Verify that a successful result contains the expected `data.logical_ref` and matching `data.markdown` before storing either value. Store only `data.markdown` or `data.logical_ref` in the draft. Treat `data.content_url` and every `/api/v1/...` value as preview transport only; never persist them in Markdown.

For a brand-new page, call `create_context_page` first to obtain its `page_id` and editing version, then call `upload_image` with those, then insert the returned `data.markdown` with `update_context_edit` or `patch_context_edit`. For an existing page, call `begin_context_edit` to obtain your own open editing version, pass its `page_id` and `editing_version_id` to `upload_image`, then insert the returned `data.markdown` with `patch_context_edit` or `update_context_edit`.

If upload or verification fails, stop the image path and report the failure.

Never fall back to a `data:` URI. The sanitizer strips it, and the Context edit path refuses raw data images outright.

## Follow the review lifecycle

After each write, inspect the envelope plus `data.publish_blocked_reasons`. Fix objective defects instead of treating them as something another reviewer can waive. Apply the `needs_review` rule from `dtxwiki-conventions`.

When a human requests changes:

1. Use `list_requested_changes` to read the request, evidence, target, and claim state routed to the caller.
2. Read the latest editable target and prepare a revision against its current base.
3. Call `submit_revision_for_review` with exactly one revision source.
4. Confirm the returned new review item is pending for a human decision.

Submitting a revision never approves it. No MCP approval action exists. Never claim that a human approved, accepted, or published the work unless returned human-authored state proves it.

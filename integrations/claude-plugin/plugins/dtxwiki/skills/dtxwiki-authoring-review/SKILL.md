---
name: dtxwiki-authoring-review
description: Create or update grounded dtxWiki Context pages, upload images safely, handle requested changes, and preview authorized publication under the workspace review policy. Use for every dtxWiki authoring or review-response workflow.
---

# dtxWiki Authoring and Review

Generate and retain an idempotency_key for each write. If its response is lost, call get_operation_status with that key and follow next_action. Keep every original argument on a resend; changed content uses a new key. Successful receipts confirm saving without a read. Use page_id and version_id from the result for subsequent writes. Partial reads use top-level page_id and optional version_id.

Read `dtxwiki-conventions` before writing. Call `guide` for the current write policy, citation policy, and effective capabilities; do not hardcode values it reports.

## Protect the base

Never write against a base you did not read. `update_context_edit` and `patch_context_edit` require the `expected_editing_revision` you last saw (from `create_context_page`, `begin_context_edit`, or the prior `update_context_edit`/`patch_context_edit` response). A stale value always returns a conflict instead of silently overwriting a newer save. On that failure, read the same `version_id` again, reconsider the change, and retry with the fresh `expected_editing_revision`. Never force an overwrite.

## Choose the write shape

1. For a new Context page, call `create_context_page`. Title/body/tags/aliases are all optional; an omitted title defaults to a placeholder. If `placement_parent_node_id` is omitted, the page goes to Inbox -- to choose a location, walk `get_index(format='nodes')` from `/context` (pass a child's `node_id` as `node` to descend) and pass the chosen `node_id` as `placement_parent_node_id`.
2. For an existing page, call `begin_context_edit`. Pass `from` with an open draft version to edit that same shared draft; pass a commit version to start a new draft from it. Omitting `from` starts from the published version. Keep the returned `version_id` for the task: patch that draft as often as needed and submit once at the end. Drafts are readable and editable by workspace actors allowed to edit Context pages.
3. Prefer a partial edit over a full rewrite:
   - Call `read_structure_map` for the target page to obtain block ids, row ids, and content hashes. Call `read_context_outline` when you need heading text or one section's body.
   - Call `patch_context_edit` with typed operations addressed by block or row id and hash -- this tool applies against the shared draft identified by `version_id`; there is no conflict-resolution operation to send.
   - Requires the `expected_editing_revision` you last saw as a CAS input; a stale value returns a conflict rather than silently overwriting a newer save.
   - The response does not repeat the body. Take the next `editing_revision` and the new block ids and hashes (`edit_receipt.changed_blocks`) from it, and check the result in `edit_receipt.changes`, which quotes each replaced region as stored. Do not re-read the page after a successful patch; read the necessary range with `page_id` and that exact `version_id` only when text or a target hash needed for your next task is missing from the receipt.
4. Use `update_context_edit` for a full-document autosave -- title/body/tags/aliases/placement together, always sending the complete `body_md`. Changing tags, aliases, or placement requires the unchanged body too. Use `cite` for reference-only additions, note changes, or removals; it does not rewrite the body.
5. Call `submit_context_edit_for_review` once, when the whole task is ready for human review ("Save draft"), not after each change. The requested draft stays editable at that same `version_id`; use its current revision for later saves. An unsubmitted draft remains readable as work in progress. Submission follows the workspace review and automatic-approval policy; inspect returned review/publication state and `data.publish_blocked_reasons`.
6. Call `discard_context_edit` to withdraw the specified shared draft. It becomes a closed commit with its history preserved. A draft-only page follows the existing page lifecycle rules; a published page retains its published head.

## Preview and publish an authorized result

Use this flow when the user requested publication and the caller's workspace permissions allow it. Saving or requesting review remains a separate action; do not turn an ordinary draft task into publication.

1. Call `preview_context_merge` with the page and draft `version_id`. It calculates publication against the current published version without saving. Keep its `editing_revision`, `published_version_id`, and the intended candidate's `result_hash`.
2. For a clean preview, use `result`. An unedited conflict preview has `kind` set to `conflict` and `result` set to null: inspect `keep_both` and `latest` and make an explicit choice within the user's instructions. Use the chosen candidate's hash and send that choice as `on_conflict`; there is no default conflict choice. Passing only `on_conflict` to preview does not move that candidate into `result`.
3. To edit the result, preview again with `body_md` and/or `page_metadata`, and the chosen `on_conflict` when needed. Use the edited candidate in `result` and its server-computed hash, even if `kind` remains `conflict`; do not calculate a replacement hash yourself.
4. Call `publish_context_edit` with `expected_editing_revision` from the preview, `expected_published_version_id` set to its published ID (explicit null when none), the candidate's `result_hash`, a `reason`, and a retained `idempotency_key`. If the result was edited, send those same edits as `result_body_md` and/or `result_page_metadata`. Preserve the chosen `on_conflict` for publication, including after an edited conflict preview. Publication still follows the existing workspace and review policies.
5. If the preview is stale, request a new preview, reconsider the result, and use a new key for the revised request. If the response is lost, use `get_operation_status` with the original key and follow `next_action`; a resend keeps every original argument. Use the successful result's `version_id`, which can differ from the source draft after a merge publication.

Use `bring_in_context_version` to merge an immutable `commit_version_id` into an existing shared draft before further editing; omitting it uses the current published version. Send the draft's current `expected_editing_revision` and a retained `idempotency_key`, and use the returned revision afterward. A conflict without `on_conflict` saves nothing and offers `keep_both` and `latest`. This command does not accept a replacement body; use the existing update or patch tools for later edits. `preview_context_merge` previews publication against the current published version, not an arbitrary bring-in commit.

## Register document references

1. Search for relevant Context, Library or Agent documents and read them before relying on them.
2. Create or select a shared Context draft.
3. Call `cite` with the owning page/version, its latest editing revision, an idempotency key, and explicit `add` or `remove` document references. Targets use document IDs, not target version IDs. The tool records your selection; it does not search or judge support.
4. For inline use, write `@cite{<canonical internal document address>}` in the body. Plain Markdown links are ordinary links. Inline citations and registered references are combined only at final publication.
5. Read back the body and separate `references` metadata, then submit once for review. Readers display numbered citations and References outside the stored body. Do not paste a generated footer into the body. A registration in Chats is an editing action, not proof that the citing document was published.

## Use the safe image flow

Call `upload_image` first. It always requires an existing `page_id` and the currently-open draft `version_id` for that page -- there is no page-less upload. Verify that a successful result contains the expected `data.logical_ref` and matching `data.markdown` before storing either value. Store only `data.markdown` or `data.logical_ref` in the draft. Treat `data.content_url` and every `/api/v1/...` value as preview transport only; never persist them in Markdown.

For a brand-new page, call `create_context_page` first to obtain its `page_id` and editing version, then call `upload_image` with those, then insert the returned `data.markdown` with `update_context_edit` or `patch_context_edit`. For an existing page, call `begin_context_edit` to obtain or select a shared draft version, pass its `page_id` and `version_id` to `upload_image`, then insert the returned `data.markdown` with `patch_context_edit` or `update_context_edit`.

If upload or verification fails, stop the image path and report the failure.

Never fall back to a `data:` URI. The sanitizer strips it, and the Context edit path refuses raw data images outright.

## Follow the review lifecycle

After each write, inspect the envelope plus `data.publish_blocked_reasons`. Fix objective defects instead of treating them as something another reviewer can waive. Apply the `needs_review` rule from `dtxwiki-conventions`.

When a human requests changes:

1. Use `list_requested_changes` to read the request, evidence, target, and claim state routed to the caller.
2. Read the requested draft with its exact `version_id` and current revision.
3. Save the changes to that shared draft with `update_context_edit` or `patch_context_edit`.
4. Keep the existing open review request attached to the draft; inspect the returned review state.

Submitting a revision never approves it. No MCP review-decision action exists. `publish_context_edit` uses the caller's existing publication permissions and review policy. Describe saved, requested, approved, and published states only as the returned result establishes them; never claim a human approved or published the work unless returned human-authored state proves it.

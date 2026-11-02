---
name: dtxwiki-authoring-review
description: Create or update grounded dtxWiki Context pages, upload images safely, and handle requested-change resubmission without crossing the human approval boundary. Use for every dtxWiki authoring or review-response workflow.
---

# dtxWiki Authoring and Review

Read `dtxwiki-conventions` before writing. Call `guide` for the current write policy, citation policy, and effective capabilities; do not hardcode values it reports.

## Protect the base

Never write against a base you did not read. `update_context_edit` and `patch_context_edit` require the `expected_editing_revision` you last saw (from `create_context_page`, `begin_context_edit`, or the prior `update_context_edit`/`patch_context_edit` response). A stale value always returns a conflict instead of silently overwriting a newer save. On that failure, call `begin_context_edit` again to resume your session on its current state, reconsider the change, and retry with the fresh `expected_editing_revision`. Never force an overwrite.

## Choose the write shape

1. For a new Context page, call `create_context_page`. Title/body/tags/aliases are all optional; an omitted title defaults to a placeholder. If `placement_parent_node_id` is omitted, the page goes to Inbox -- to choose a location, walk `get_index(format='nodes')` from `/context` (pass a child's `node_id` as `node` to descend) and pass the chosen `node_id` as `placement_parent_node_id`.
2. For an existing page, call `begin_context_edit` to idempotently open or resume your own private "editing" session. Keep that one edit for the whole task: patch it as often as needed and submit once at the end. Passing `base_version_id`/`base_content_hash` asserts the base explicitly and returns a conflict instead of silently building on a head that moved.
3. Prefer a partial edit over a full rewrite:
   - Call `read_structure_map` for the target page to obtain block ids, row ids, and content hashes. Call `read_context_outline` when you need heading text or one section's body.
   - Call `patch_context_edit` with typed operations addressed by block or row id and hash -- this tool always applies against your own open row, never a shared reconciled head, so there is no conflict-resolution operation to send.
   - Requires the `expected_editing_revision` you last saw as a CAS input; a stale value returns a conflict rather than silently overwriting a newer save.
   - The response does not repeat the body. Take the next `editing_revision` and the new block ids and hashes (`edit_receipt.changed_blocks`) from it, and check the result in `edit_receipt.changes`, which quotes each replaced region as stored. Do not re-read the page after a successful patch; read it with `version='my_edit'` only when `changes` is null or `changes_omitted_count` or `changed_blocks_omitted_count` is above 0.
4. Use `update_context_edit` for a full-document autosave -- title/body/tags/aliases/placement together, always sending the complete `body_md`. Changing tags, aliases, or placement requires the unchanged body too. Use `cite` for reference-only additions, note changes, or removals; it does not rewrite the body.
5. Call `submit_context_edit_for_review` once, when the whole task is ready for human review ("Save draft"), not after each change. To change it again from the same chat before it is reviewed, call `begin_context_edit`: it reopens that same version (no new version), and the next submit replaces its review. An edit you never submit stays private and is never reviewed. A submission never publishes on its own; read `data.publish_blocked_reasons` to learn why human review was required.
6. Call `discard_context_edit` to abandon your own open editing session (terminal, not a hard-delete: audit/History is preserved). A page with no prior published/submitted history is archived when its only version is discarded; a previously published/shared page keeps its existing head.

## Register document references

1. Search for relevant Context, Library or Agent documents and read them before relying on them.
2. Create or open your own Context editing version.
3. Call `cite` with the owning page/version, its latest editing revision, an idempotency key, and explicit `add` or `remove` document references. Targets use document IDs, not target version IDs. The tool records your selection; it does not search or judge support.
4. For inline use, write `@cite{<canonical internal document address>}` in the body. Plain Markdown links are ordinary links. Inline citations and registered references are combined only at final publication.
5. Read back the body and separate `references` metadata, then submit once for review. Readers display numbered citations and References outside the stored body. Do not paste a generated footer into the body. A registration in Chats is an editing action, not proof that the citing document was published.

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

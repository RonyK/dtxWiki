---
name: dtxwiki-conventions
description: Apply dtxWiki MCP conversation continuity, result-envelope, scope, review-intent, and human-approval rules. Use for every dtxWiki MCP workflow and before interpreting or retrying a dtxWiki tool result.
---

# dtxWiki Conventions

Call `guide` when starting work in a workspace and whenever current capabilities or policy are unclear. It has three detail levels: `brief` (the default: the workspace, your permissions, how to find pages and the read/write workflow, enough to read, edit and explore), `normal` (adds taxonomies, prompt and resource entry points, the AgentPage injection policy, hot concepts and your full tool list) and `detailed` (adds the full workspace summary and field explanations). Start with `brief` and ask for more only when a task needs it. Treat what `guide` reports as authoritative; do not cache or hardcode it.

## Carry the conversation token

Prefer the plain `conversation_token` tool argument. It is canonical because the model can see and control it.

Inspect carriers in this order: plain argument, namespaced `_meta`, then legacy `_meta`. The order identifies the canonical carrier; it never permits resolving a disagreement by precedence.

| Carriers supplied | Action |
| --- | --- |
| None on discovery or read | The call may proceed in a new dtxWiki conversation. Save `envelope.conversation_token` for follow-up calls. |
| None on a content workflow | The call is refused. Retry with the returned `envelope.conversation_token` or the literal `new`. |
| Plain `conversation_token` | Use it. |
| `_meta["com.dtxwiki/conversationToken"]` only | Accept it as a compatibility carrier. |
| Legacy `_meta.conversation_token` only | Accept it for legacy compatibility. Prefer the plain argument on later calls. |
| Multiple carriers with the same value | Accept the shared value. |
| Multiple carriers with different values | Refuse the call. Do not choose a winner or retry by guessing. |

Use the literal `new` only to start a new dtxWiki conversation deliberately. Otherwise echo the latest `envelope.conversation_token` on later calls, including reads, so one client conversation does not split across activity records.

## Read the result envelope first

Inspect `envelope.result_status` before using payload data.

- On failure, read `envelope.error_code`, `envelope.error_details`, and `envelope.error_message`. Honor `envelope.retryable`; do not resend unchanged arguments when it is false.
- Treat `envelope.warnings` as structured outcomes that may require a different read, more evidence, disambiguation, or a corrected draft.
- Use `data` only as the tool-specific payload. Other top-level fields such as candidates or input requests belong to the tool contract and do not override the envelope status.
- Preserve `request_id` when reporting a failure, but never present it as content evidence.

## Set review intent precisely

Leave `needs_review` false for routine edits. Also leave it false for objective defects such as lint findings; the server detects those independently and reports what must be fixed.

Set `needs_review` true only for a genuine exception that requires human attention: uncertain content, a contradiction with existing content, or a high-stakes change. It expresses caution; it does not claim that review or approval occurred.

## Respect effective visibility

Workspace membership and role checks still apply in addition to OAuth scopes. Use `guide.actor_context` for your write permission and `tools/list` (or `available_tools` in `guide` at `normal`) for the tools you can call.

- `mcp:read` grants workspace discovery and actor-visible Context, Library, and Agent-page retrieval. It does not expose audit history, the review queue, or writes.
- `mcp:write` adds audit and review reads plus Context draft, citation, feedback, review-resubmission, and maintenance capabilities.
- `mcp:agent` adds Agent input-bundle and Agent-page-draft capabilities. It does not by itself grant workspace membership or the capabilities carried by `mcp:read` or `mcp:write`.

Never infer hidden content from a not-found result or disclose candidates the actor cannot see.

## Preserve the human boundary

Never say or imply that a human approved, reviewed, accepted, or published a change unless a human action is present in the returned dtxWiki state. An agent may prepare a draft, request review, or submit a revision. Approval is a human act.

"""Tool descriptors that bridge Pydantic input schemas to MCP tools."""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache

from mcp.types import Tool, ToolAnnotations

from backend.mcp.protocol.instructions import CONTEXT_LINKING_INSTRUCTION
from backend.mcp.protocol.output_validation import WIRE_ENVELOPE_FIELDS
from backend.mcp.protocol.schema_projection import host_compatible_schema
from backend.mcp.scopes import TOOL_REQUIRED_ROLES
from backend.schemas.common import MCP_SCHEMA_VERSION, MCP_TOOL_CONTRACT_REVISION
from backend.scripts.export_mcp_schemas import build_mcp_schemas
from backend.spi.tool_presentation import (
    KOREAN_TOOL_TITLES,
    MCP_CONTEXT_LINK_GUIDANCE,
    TOOL_ACTIONS,
)

# Re-exported: this module is where callers inside backend/mcp look for tool
# presentation data. The data itself lives in the shared layer because
# backend/services needs it too and may not import backend.mcp.
__all__ = [
    "KOREAN_TOOL_TITLES",
    "TOOL_ACTIONS",
    "TOOL_META",
    "list_dtxwiki_tools",
    "mcp_server_version",
    "tool_contract_digest",
    "tool_contract_version",
]
from backend.schemas.mcp.agent_tools import (
    BuildAgentInputBundleInput,
    BuildAgentInputBundleToolOutput,
    WriteAgentPageDraftInput,
    WriteAgentPageDraftToolOutput,
)
from backend.schemas.mcp.context_tools import (
    BeginContextEditInput,
    BeginContextEditToolOutput,
    BringInContextVersionInput,
    BringInContextVersionToolOutput,
    CiteInput,
    CiteToolOutput,
    CreateContextPageInput,
    CreateContextPageToolOutput,
    DiscardContextEditInput,
    DiscardContextEditToolOutput,
    ListMyContextEditsInput,
    ListMyContextEditsToolOutput,
    PatchContextEditInput,
    PatchContextEditToolOutput,
    PreviewContextMergeInput,
    PreviewContextMergeToolOutput,
    PublishContextEditInput,
    PublishContextEditToolOutput,
    SubmitContextEditForReviewInput,
    SubmitContextEditForReviewToolOutput,
    SubmitFeedbackInput,
    SubmitFeedbackToolOutput,
    UpdateContextEditInput,
    UpdateContextEditToolOutput,
)
from backend.schemas.mcp.maintenance_tools import (
    WikiMaintenanceInput,
    WikiMaintenanceOutput,
)
from backend.schemas.mcp.operation_tools import (
    GetOperationStatusInput,
    GetOperationStatusOutput,
)
from backend.schemas.mcp.read_tools import (
    FetchInput,
    FetchOutput,
    GetIndexInput,
    GetIndexOutput,
    GrepWikiInput,
    GrepWikiOutput,
    GuideInput,
    GuideOutput,
    ListAgentCategoriesInput,
    ListAgentCategoriesOutput,
    ListChangesInput,
    ListChangesOutput,
    ListContextDraftsInput,
    ListContextDraftsOutput,
    ListMyWorkspacesInput,
    ListMyWorkspacesOutput,
    ReadAgentPageInput,
    ReadAgentPageOutput,
    ReadConceptInput,
    ReadConceptOutput,
    ReadContextOutlineInput,
    ReadContextOutlineOutput,
    ReadInput,
    ReadOutput,
    ReadStructureMapInput,
    ReadStructureMapOutput,
    SearchInput,
    SearchOutput,
    WhatChangedInput,
    WhatChangedOutput,
)
from backend.schemas.mcp.review_tools import (
    ListRequestedChangesInput,
    ListRequestedChangesOutput,
)
from backend.schemas.mcp.upload_tools import UploadImageInput, UploadImageToolOutput


@dataclass(frozen=True)
class ToolMeta:
    title: str
    description: str
    read_only: bool
    destructive: bool = False
    idempotent: bool = False
    open_world: bool = False
    localized_titles: dict[str, str] | None = None


TOOL_META = {
    "preview_context_merge": (
        "Preview context merge",
        "Show what publish_context_edit would publish for this draft: the draft "
        "merged with the current published version. Saves nothing. result "
        "carries the result_hash to publish; a conflict also returns keep_both "
        "and latest, and result follows on_conflict once you choose. Pass "
        "body_md/page_metadata to preview an edited result.",
        True,
    ),
    "bring_in_context_version": (
        "Bring in context version",
        "Merge a commit of this page into the named draft (default: the current "
        "published version) and return the draft's new editing_revision. A draft "
        "that already contains it is not saved. A conflict saves nothing until "
        "on_conflict is chosen. Requires conversation_token: echo one from an "
        "earlier envelope or pass the literal new.",
        False,
        False,
        True,
    ),
    "publish_context_edit": (
        "Publish context edit",
        "Publish the result previewed by preview_context_merge, with the same "
        "on_conflict and result edits. Workspace review policy decides whether "
        "it publishes or needs a person (human_approval_required). The returned "
        "version_id can be a new merge commit. Requires conversation_token: "
        "echo one from an earlier envelope or pass the literal new.",
        False,
        False,
        True,
    ),
    "get_operation_status": (
        "Get operation status",
        "Recover your keyed write within 24 hours without resending its body. "
        "Follow next_action: committed returns the saved result; in_progress "
        "asks you to wait; failed explains recovery; not_seen gives bounded "
        "resend guidance. Results describe the original write, not current state.",
        True,
        False,
        True,
    ),
    "list_my_workspaces": (
        "List my workspaces",
        "List workspaces visible to the authenticated actor, including the "
        "actor role in each workspace. For a PAT or OAuth credential this is "
        "the credential's allowed set, not every workspace the underlying "
        "user is a member of -- to reach a different workspace, expand this "
        "credential's allowed set in Settings -> MCP access.",
        True,
    ),
    "guide": (
        "Get workspace guide",
        "Call this first for a workspace. detail_level='brief' (default) "
        "returns what you need to read, edit and explore: the workspace, your "
        "permissions, how to find pages and the read/write workflow. 'normal' "
        "adds taxonomies, prompts/resources, AgentPage policy, hot concepts "
        "and your full tool list; 'detailed' adds the full workspace summary "
        "and field explanations. Every level lists all three.",
        True,
    ),
    "search": (
        "Search workspace knowledge",
        "Resolve direct paths, ids, exact names, and fuzzy page names before "
        "ranked retrieval across Context, Library, and Agent pages. The all "
        "scope excludes feedback and changes; use the explicit feedback or "
        "change scope for authorized audit searches. All search hits use the "
        "same ranked_results list. Context hits retain matching versions, "
        "grouped by page with published, review-requested draft, then other "
        "draft versions. Inspect version_status and draft; read version_id "
        "for the exact hit. Drafts are not citation evidence. "
        "Set filters.include_agents=false to exclude "
        "Agent pages before pagination. Keyword mode is always "
        "available; semantic and hybrid "
        "modes are available when an embedding provider is configured. MRTR "
        "preview clients may optionally return a relevance ranking for large "
        "fallback result sets.",
        True,
    ),
    "fetch": (
        "Fetch search result",
        "Fetch the complete actor-visible content for one search result using "
        "its workspace_id, id, and type pair. Returns OpenAI-compatible id, "
        "title, text, url, and metadata fields. Use search first; a hidden or "
        "missing target returns not_found.",
        True,
        False,
        True,
        False,
    ),
    "grep_wiki": (
        "Grep wiki Markdown",
        "Deterministically scan actor-visible Context Markdown and return "
        "matching lines with nearby context. Use this when exact text "
        "navigation is more useful than ranked search.",
        True,
    ),
    "what_changed": (
        "What changed",
        "Published-change delta since a timestamp: created/updated/"
        "archived/unpublished entries with stable links, reasons and version ids. "
        "created also marks a page published again after it was withdrawn "
        "or archived; the reason says why. Re-sync with this instead of "
        "re-crawling; use read_concept with as_of for point-in-time content.",
        True,
    ),
    "read": (
        "Read workspace entity",
        "Read a specific context, source, artifact, feedback, or change entity "
        "by target_type and target_id.",
        True,
    ),
    "list_context_drafts": (
        "List Context drafts",
        "List saved open drafts for a Context page, newest save first. "
        "Editors can read other authors' drafts; viewer results are empty. "
        "Pass next_cursor to read the remainder. Reading never opens an edit.",
        True,
    ),
    "list_changes": (
        "List change history",
        "List version and change history for an agent page, context page, "
        "source, feedback item, workspace, or change set.",
        True,
    ),
    "list_agent_categories": (
        "List agent categories",
        "List AgentPage categories and the workspace default injection policy "
        "for each category, including persona, system_prompt, and rule_policy "
        "guidance categories.",
        True,
    ),
    "read_agent_page": (
        "Read agent page",
        "Read an AgentPage by id or slug using the active, draft, or numeric "
        "version selector. Use this for explicit persona, system_prompt, "
        "rule_policy, skill, memory, or reference guidance reads when allowed.",
        True,
    ),
    "create_context_page": (
        "Create context page",
        "Create a Context page and its first shared draft. "
        "Title/body/tags/aliases are optional; an omitted title receives a "
        "placeholder. This always creates a new page. Omit "
        "placement_parent_node_id to place it in Inbox. Save with "
        "update_context_edit or patch_context_edit, then request review when "
        "ready. The response names the draft and its editing_revision. "
        f"{CONTEXT_LINKING_INSTRUCTION} "
        "Requires conversation_token: echo one from an earlier envelope or pass the "
        "literal new. ",
        False,
    ),
    "begin_context_edit": (
        "Begin context edit",
        "Open a shared Context draft idempotently. With from naming a draft, "
        "join that exact draft. With a commit, start a new draft parented on "
        "that commit. A list of commits starts a merged draft; on_conflict "
        "selects keep_both or latest when needed. Omit from to create a new "
        "draft from the published version. It never implicitly resumes another "
        "open draft or a previous chat submission. The response includes the "
        "body, version_id and editing_revision. Requires conversation_token: "
        "echo one from an earlier envelope or pass the literal new. ",
        False,
    ),
    "update_context_edit": (
        "Update context edit",
        "Replace a shared draft body in place. Send page_id, version_id and "
        "expected_editing_revision from the last read/write. A changed revision "
        "returns a conflict: read that exact version before retrying. "
        "Successful saves retain version_id and return an edit receipt. Prefer "
        "patch_context_edit for local changes. Request review once the task is "
        "ready; the draft remains editable afterward. "
        f"{CONTEXT_LINKING_INSTRUCTION} "
        "Requires conversation_token: echo one from an earlier envelope or pass the "
        "literal new. ",
        False,
    ),
    "submit_context_edit_for_review": (
        "Submit context edit for review",
        "Open or reuse the review request for a shared draft. Requires "
        "version_id and expected_editing_revision. Submit once when the task is "
        "ready. The draft stays editable, including while review is pending or "
        "changes are requested. The response names the draft and review; it "
        "does not repeat the body. A missing graph-visible Context link "
        "produces context_page_has_no_graph_visible_links; submission still succeeds. "
        f"{CONTEXT_LINKING_INSTRUCTION} "
        "Requires conversation_token: echo one from an earlier "
        "envelope or pass the literal new. ",
        False,
    ),
    "discard_context_edit": (
        "Discard context edit",
        "End the named shared draft. The same version_id becomes a closed Git "
        "commit retained in History; its review request is withdrawn. Published "
        "content remains available. Start another draft from that commit with "
        "begin_context_edit(from=version_id). This is terminal for the draft "
        "and never deletes the retained commit. Requires conversation_token: "
        "echo one from an earlier envelope or pass the literal new. ",
        False,
        True,
    ),
    "patch_context_edit": (
        "Patch context edit",
        "Apply typed block/section edit operations to the named shared "
        "draft in place "
        "(replace_block/insert_after/insert_row/delete_block/replace_text/"
        "insert_text/replace_section/insert_section/delete_section/"
        "replace_document/append_document). All operations "
        "in one call resolve against the draft's current body and apply "
        "atomically as "
        "one revision: if any operation is refused, or two target "
        "overlapping text, nothing changes and error_details names the "
        "operation_index. replace_text finds old_text exactly, then ignoring "
        "invisible differences (whitespace, Unicode form, curly quotes); "
        "scoped with at={by:'block', block, expected} or a section it also "
        "forgives a small copy slip when exactly one place is that close. "
        "It never guesses: text_match_not_found/_ambiguous/_low_confidence "
        "list the nearest lines and block ids to retry with. Requires the "
        "editing_revision you last saw as a CAS input; if the edit changed "
        "since, nothing is applied and the conflict's error_details name its "
        "current editing_revision -- re-read it before retrying. "
        "data.edit_receipt lists each operation's line range, section and "
        "match_kind, and the id and hash of every block whose id or hash "
        "changed (replace_block keeps the block's id; a block other "
        "operations changed keeps it while its text stays close to the old "
        "text), so a follow-up block-addressed patch needs no "
        "read_structure_map call. The response does not repeat the body: "
        "data.edit_receipt.changes quotes each region the patch replaced as "
        "stored, including a forgiven copy slip or a server format fix, so "
        "do not re-read to check the result. Read with the same version_id "
        "only when text or a target hash needed for your next task was omitted. "
        "Keeps the same draft version; use "
        "submit_context_edit_for_review for that. "
        f"{CONTEXT_LINKING_INSTRUCTION} "
        "Requires conversation_token: echo one from an earlier envelope or "
        "pass the literal 'new'.",
        False,
    ),
    "list_my_context_edits": (
        "List my context edits",
        "List shared drafts you created or contributed to across the workspace. "
        "Returns page/version IDs, title, parents, editing_revision, "
        "content_hash and last-saved time. Bodies are omitted; read an exact "
        "version to continue editing. Other contributors can edit the same "
        "draft. ",
        True,
    ),
    "upload_image": (
        "Upload image",
        "Upload a PNG, JPEG, GIF, or WebP image before referencing it in a "
        "Context draft. Store data.markdown or data.logical_ref; content_url is "
        "preview transport only and must never be persisted in Markdown. "
        "Requires an existing page_id and the named shared draft "
        "version_id -- there is no page-less upload. "
        "Never fall back to a data: URI when upload fails. "
        "Requires conversation_token: "
        "echo one from an earlier envelope or pass the literal 'new'.",
        False,
        False,
        True,
        False,
    ),
    "cite": (
        "Register references",
        "Add or remove selected Context, Library or Agent document IDs in your "
        "shared Context draft's References. Use search and read/fetch to choose "
        "documents first. Requires page_id, version_id, expected_editing_revision "
        "and idempotency_key; target version IDs are unnecessary. This mutates "
        "draft metadata only. Published Graph citations change at final publish. "
        "Removing metadata preserves any remaining inline citations. "
        "Requires conversation_token: echo one or pass 'new'.",
        False,
        False,
        True,
        False,
    ),
    "submit_feedback": (
        "Submit feedback",
        "Create a FeedbackItem for a context, source, answer, or agent target "
        "and record audit evidence."
        "Requires conversation_token: echo one from an earlier envelope or "
        "pass the literal 'new'.",
        False,
    ),
    "write_agent_page_draft": (
        "Write agent page draft",
        "Create or update an AgentPage draft. This never activates the page. "
        "Visible exact system index refs normalize to canonical browser links; "
        "normalization_report lists applied destinations and unavailable refs "
        "whose original text is retained. "
        "Requires conversation_token: echo one from an earlier envelope or "
        "pass the literal 'new'.",
        False,
    ),
    "build_agent_input_bundle": (
        "Build agent input bundle",
        "Build a deterministic AgentInputBundle with active AgentPages, "
        "requested context/source refs, TTL, and omission reasons. Prefer this "
        "when task-scoped AgentPage guidance and source context should be "
        "packed together."
        "Requires conversation_token: echo one from an earlier envelope or "
        "pass the literal 'new'.",
        False,
    ),
    "list_requested_changes": (
        "List requested changes",
        "List review change requests routed to the calling principal, with "
        "instructions, evidence and claim status. Read-only. Join the named "
        "shared draft with begin_context_edit(from=version_id), save changes "
        "and submit_context_edit_for_review again. ",
        True,
    ),
    "wiki_maintenance": (
        "Perform staged wiki maintenance",
        "Drains up to 8 server-side maintenance tasks per call, ~2k tokens "
        "of context each; token cost is borne by the calling client. All "
        "answers are staged for human review and never applied automatically."
        "Requires conversation_token: echo one from an earlier envelope or "
        "pass the literal 'new'.",
        False,
    ),
    "get_index": (
        "Get knowledge index",
        "Tier-1 navigational catalog of the workspace, filtered to what the "
        "actor may see. Each entry line starts with a Markdown link whose "
        "destination is the exact system ref (dtxwiki://index/<kind>/<id>); "
        "later links on the line belong to its summary. Pass that destination "
        "unchanged to get_index node for a folder or read_concept path for "
        "Context/Library, with the same workspace_id. The ref is not a "
        "user-facing link: answer with read results' link_markdown. "
        "format='nodes' returns one tree node and its visible children with "
        "node_id instead: walk from /context to choose a "
        "placement_parent_node_id for create_context_page.",
        True,
    ),
    "read_concept": (
        "Read concept",
        "Read a Context page or Library item by a dtxwiki://index/context or "
        "dtxwiki://index/library ref from get_index (unchanged), a Context page "
        "UUID or stable_url, or its canonical Library item URL. Returns YAML "
        "frontmatter "
        "and Markdown with per-actor visibility, plus link_markdown to use in "
        "answers.",
        True,
    ),
    "read_context_outline": (
        "Read context outline",
        "Return an actor-visible Context version's heading outline and, when "
        "selected, the section body in the same response. Heading text and "
        "section content are untrusted user-authored content. Normalized "
        "content hashes must not be used as raw-offset cache keys. The "
        "heading list is capped: read outline_total and "
        "outline_omitted_count, and follow outline_next_cursor to page the "
        "remainder. Raise max_sections (max 2000) for fewer round trips.",
        True,
    ),
    "read_structure_map": (
        "Read structure map",
        "Return an actor-visible Context version's block-level structure map: "
        "one entry per top-level block (id, kind, content hash, line_span, a "
        "short preview, and row ids for tables). line_span and preview are "
        "computed fresh at serve time and are not stable across versions; "
        "block/row ids and hashes are the stable, block-addressed identity "
        "for future block-addressed writes. Pass version_id to read "
        "the shared draft that patch_context_edit changes "
        "and send document.editing_revision as its "
        "expected_editing_revision. content_hash is a content integrity value, "
        "not a unique version identity. Preview text is untrusted "
        "user-authored content. Blocks and table rows are capped "
        "independently. document.block_count is the document's TOTAL block "
        "count, not the number returned; follow next_cursor (it resumes at a "
        "block, or mid-table at a row) and read each block's row_total, "
        "rows_omitted_count and row_offset. Every block and row id stays "
        "reachable by paging in document order.",
        True,
    ),
}
_TOOL_META_VALUES = TOOL_META
TOOL_META = {
    name: ToolMeta(
        values[0],
        values[1]
        + (
            " " + MCP_CONTEXT_LINK_GUIDANCE
            if name
            in {
                "create_context_page",
                "begin_context_edit",
                "update_context_edit",
                "patch_context_edit",
                "submit_context_edit_for_review",
                "read_concept",
            }
            else ""
        ),
        *values[2:],
        localized_titles={
            "en": values[0],
            **({"ko": KOREAN_TOOL_TITLES[name]} if name in KOREAN_TOOL_TITLES else {}),
        },
    )
    for name, values in _TOOL_META_VALUES.items()
}


def list_dtxwiki_tools(tool_names: Iterable[str] | None = None) -> list[Tool]:
    tools = [
        tool(
            "list_my_workspaces",
            ListMyWorkspacesInput.model_json_schema(),
            ListMyWorkspacesOutput.model_json_schema(),
        ),
        tool("guide", GuideInput.model_json_schema(), GuideOutput.model_json_schema()),
        tool(
            "search", SearchInput.model_json_schema(), SearchOutput.model_json_schema()
        ),
        tool("fetch", FetchInput.model_json_schema(), FetchOutput.model_json_schema()),
        tool(
            "grep_wiki",
            GrepWikiInput.model_json_schema(),
            GrepWikiOutput.model_json_schema(),
        ),
        tool(
            "what_changed",
            WhatChangedInput.model_json_schema(),
            WhatChangedOutput.model_json_schema(),
        ),
        tool("read", ReadInput.model_json_schema(), ReadOutput.model_json_schema()),
        tool(
            "get_index",
            GetIndexInput.model_json_schema(),
            GetIndexOutput.model_json_schema(),
        ),
        tool(
            "read_concept",
            ReadConceptInput.model_json_schema(),
            ReadConceptOutput.model_json_schema(),
        ),
        tool(
            "list_context_drafts",
            ListContextDraftsInput.model_json_schema(),
            ListContextDraftsOutput.model_json_schema(),
        ),
        tool(
            "list_changes",
            ListChangesInput.model_json_schema(),
            ListChangesOutput.model_json_schema(),
        ),
        tool(
            "list_agent_categories",
            ListAgentCategoriesInput.model_json_schema(),
            ListAgentCategoriesOutput.model_json_schema(),
        ),
        tool(
            "read_agent_page",
            ReadAgentPageInput.model_json_schema(),
            ReadAgentPageOutput.model_json_schema(),
        ),
        tool(
            "get_operation_status",
            GetOperationStatusInput.model_json_schema(),
            GetOperationStatusOutput.model_json_schema(),
        ),
        tool(
            "preview_context_merge",
            PreviewContextMergeInput.model_json_schema(),
            PreviewContextMergeToolOutput.model_json_schema(),
        ),
        tool(
            "bring_in_context_version",
            BringInContextVersionInput.model_json_schema(),
            BringInContextVersionToolOutput.model_json_schema(),
        ),
        tool(
            "publish_context_edit",
            PublishContextEditInput.model_json_schema(),
            PublishContextEditToolOutput.model_json_schema(),
        ),
        tool(
            "create_context_page",
            CreateContextPageInput.model_json_schema(),
            CreateContextPageToolOutput.model_json_schema(),
        ),
        tool(
            "begin_context_edit",
            BeginContextEditInput.model_json_schema(),
            BeginContextEditToolOutput.model_json_schema(),
        ),
        tool(
            "update_context_edit",
            UpdateContextEditInput.model_json_schema(),
            UpdateContextEditToolOutput.model_json_schema(),
        ),
        tool(
            "submit_context_edit_for_review",
            SubmitContextEditForReviewInput.model_json_schema(),
            SubmitContextEditForReviewToolOutput.model_json_schema(),
        ),
        tool(
            "discard_context_edit",
            DiscardContextEditInput.model_json_schema(),
            DiscardContextEditToolOutput.model_json_schema(),
        ),
        tool(
            "patch_context_edit",
            PatchContextEditInput.model_json_schema(),
            PatchContextEditToolOutput.model_json_schema(),
        ),
        tool(
            "list_my_context_edits",
            ListMyContextEditsInput.model_json_schema(),
            ListMyContextEditsToolOutput.model_json_schema(),
        ),
        tool(
            "upload_image",
            UploadImageInput.model_json_schema(),
            UploadImageToolOutput.model_json_schema(),
        ),
        tool("cite", CiteInput.model_json_schema(), CiteToolOutput.model_json_schema()),
        tool(
            "submit_feedback",
            SubmitFeedbackInput.model_json_schema(),
            SubmitFeedbackToolOutput.model_json_schema(),
        ),
        tool(
            "write_agent_page_draft",
            WriteAgentPageDraftInput.model_json_schema(),
            WriteAgentPageDraftToolOutput.model_json_schema(),
        ),
        tool(
            "build_agent_input_bundle",
            BuildAgentInputBundleInput.model_json_schema(),
            BuildAgentInputBundleToolOutput.model_json_schema(),
        ),
        tool(
            "list_requested_changes",
            ListRequestedChangesInput.model_json_schema(),
            ListRequestedChangesOutput.model_json_schema(),
        ),
        tool(
            "wiki_maintenance",
            WikiMaintenanceInput.model_json_schema(),
            WikiMaintenanceOutput.model_json_schema(),
        ),
        tool(
            "read_context_outline",
            ReadContextOutlineInput.model_json_schema(),
            ReadContextOutlineOutput.model_json_schema(),
        ),
        tool(
            "read_structure_map",
            ReadStructureMapInput.model_json_schema(),
            ReadStructureMapOutput.model_json_schema(),
        ),
    ]
    if tool_names is None:
        return tools
    allowed = set(tool_names)
    return [tool for tool in tools if tool.name in allowed]


def tool(name: str, input_schema: dict, output_schema: dict) -> Tool:
    # ``output_schema`` is accepted (and still computed by every call site
    # below, since the same XxxOutput models are also the return type of the
    # handlers) but deliberately NOT wired onto the wire. Measured across the
    # 27 tools, outputSchema is 75% of the advertised tools/list payload and
    # the reference SDK confirms it is never read by a model -- Anthropic's
    # own tool-use API accepts only name/description/input_schema, so this
    # bytes are dead weight for every client built on it. Dropping it here
    # also disables the SDK's own post-call structuredContent validation
    # (mcp/server/lowlevel/server.py, mcp/client/session.py); that gate is
    # replaced by the runner's own fail-closed check (see
    # backend/mcp/protocol/output_validation.py), which validates BOTH the
    # handler payload and the slimmed wire payload against these same
    # Pydantic-derived schemas -- the ones exported to
    # shared/schemas/mcp/v1/*.json and kept in sync by
    # tests/static/test_schema_drift.py, not read from disk at runtime. A
    # violation on any validated path is replaced by an internal_error result
    # rather than served, so this parameter stays unused on the wire and
    # remains the source of truth for that internal check.
    #
    # "Any validated path" is deliberate, not hedging: the runner's two
    # `except` handlers and the SDK-level fallback build their payload from
    # the fixed _tool_error_payload shape and are exempted from the check on
    # purpose, because validating the replacement is what would recurse. That
    # shape is proved conformant for all 25 tools by
    # test_output_validation.py::test_tool_error_payload_satisfies_every_tool_
    # contract_in_both_phases, which is the standing evidence for the
    # exemption.
    del output_schema
    meta = TOOL_META[name]
    return Tool(
        name=name,
        title=meta.title,
        description=meta.description,
        # Advertised in the portable subset hosts actually convert; the full
        # Pydantic model still validates every call (schema_projection.py).
        inputSchema=host_compatible_schema(input_schema),
        annotations=ToolAnnotations(
            title=meta.title,
            readOnlyHint=meta.read_only,
            destructiveHint=meta.destructive,
            idempotentHint=meta.idempotent,
            openWorldHint=meta.open_world,
        ),
        _meta={
            "required_role": TOOL_REQUIRED_ROLES[name],
            **(
                {"com.dtxwiki/localizedTitles": meta.localized_titles}
                if meta.localized_titles
                else {}
            ),
        },
    )


def _tool_contract_material() -> dict:
    """Everything a client's copy of this server's tool contract depends on.

    Three parts, because the contract is not all on the wire:

    * ``tools`` -- the `tools/list` payload itself, sorted by name so that
      merely reordering the catalog source is not reported as a change. This
      is what catches an added/removed/renamed tool, a changed input schema,
      a changed description or annotation, and the removal of ``outputSchema``
      from the wire (decision D2).
    * ``outputs`` -- the per-tool output schemas, which since D2 are NOT
      advertised anywhere a client can see them, yet still describe every
      result it has to parse. Without this part the digest would be blind to
      exactly the changes that motivated this marker: a read projection
      growing a cursor, or ``document.block_count`` changing meaning.
    * ``wire_envelope_fields`` -- the model-facing envelope allowlist. Folding
      14 audit fields into ``envelope.meta`` moved every result's shape while
      leaving both of the above untouched, so the allowlist is contract too.

    Built from the same live objects the runner validates against
    (``build_mcp_schemas``), never from ``shared/schemas/mcp/v1/*.json``, so
    the digest cannot go stale against an unregenerated checked-in file.
    """

    return {
        "envelope_schema_version": MCP_SCHEMA_VERSION,
        "tools": sorted(
            (
                tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                for tool in list_dtxwiki_tools()
            ),
            key=lambda dumped: dumped["name"],
        ),
        "outputs": {
            name: schema["output"] for name, schema in build_mcp_schemas().items()
        },
        "wire_envelope_fields": sorted(WIRE_ENVELOPE_FIELDS),
    }


@lru_cache(maxsize=1)
def tool_contract_digest() -> str:
    """Content address of the live tool contract.

    This is the half of the published contract version that cannot silently
    stop tracking: it is computed FROM the catalog rather than maintained
    alongside it, so a contract change moves it whether or not anyone
    remembered to bump ``MCP_TOOL_CONTRACT_REVISION``. The curated revision is
    what a human reads; this is what makes the marker true.

    Truncated to 12 hex characters. This is a change detector, not a security
    boundary -- a client compares it for equality against the value it last
    saw, and 48 bits is far past the point where an accidental collision
    between two catalogs of a couple of dozen tools is worth reasoning about.

    Cached for the process: ``create_mcp_server`` is called once per POST on
    the ``json_stateless`` path, and rebuilding 25 Pydantic JSON Schemas per
    request would be a real cost for a value that cannot change without a
    redeploy. Tests that mutate the catalog must call ``cache_clear()``.
    """

    blob = json.dumps(
        _tool_contract_material(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def tool_contract_version() -> str:
    """The dtxWiki tool contract version, e.g. ``llmwiki.mcp.v1.tools.1.ab12``.

    Nested under the envelope family (``MCP_SCHEMA_VERSION``) on purpose --
    see the axis argument in ``backend/schemas/common.py``. The trailing
    segment is ``tool_contract_digest()``.
    """

    return (
        f"{MCP_SCHEMA_VERSION}.tools.{MCP_TOOL_CONTRACT_REVISION}"
        f".{tool_contract_digest()}"
    )


def mcp_server_version(app_version: str) -> str:
    """What both MCP surfaces publish as ``serverInfo.version``.

    ``app_version`` alone was unusable as a change signal: across Epic-1 the
    wire contract changed four separate times -- ``outputSchema`` left
    ``tools/list``, three legacy page tools were deleted, 14 envelope fields
    moved into ``envelope.meta``, and two read projections grew cursors --
    and ``serverInfo.version`` read ``0.1.0`` before and after every one of
    them. A client had no way to notice, and there was no dual-write and no
    deprecation window to shelter under.

    ``serverInfo`` rather than the instructions string, for two reasons: it is
    structured data a client already stores, logs and displays, whereas
    ``instructions`` is prose written for the MODEL and would have to be
    regexed by client code that has no promise the wording is stable (this
    server rewrites it whenever the tool list moves); and a marker in
    ``instructions`` costs tokens in every conversation to tell the model
    something only its host can act on. Neither field costs an extra round
    trip -- the same response carries both -- and both survive the current
    spec revision retiring ``initialize`` in favour of ``server/discover``,
    which is why this is a marker to READ rather than a gate that refuses
    traffic at a method that is going away.

    Attached as semver build metadata so the string stays a valid version for
    anything that parses it, and so the app version remains readable in front
    of the ``+``. Note the consequence, which the CHANGELOG states for
    clients: build metadata is ignored by semver PRECEDENCE, so a client
    detecting contract change must compare the whole string for equality (or
    read the ``tools.<N>`` segment), not ask whether one version is newer.
    """

    return f"{app_version}+{tool_contract_version()}"

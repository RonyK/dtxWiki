"""Export the checked-in MCP tool contracts under ``shared/schemas/mcp/v1``.

``main()`` does not only write: it also DELETES the top-level ``*.json`` of any
tool that ``build_mcp_schemas()`` no longer registers, so that a removed tool
reaches ``make verify-generated-schemas`` as a visible deletion instead of a
stale contract nobody rewrote. A hand-written file in that directory whose stem
is not a registered tool will therefore not survive a regeneration.
"""

import json
from pathlib import Path

from backend.schemas.mcp.agent_tools import (
    BuildAgentInputBundleInput,
    BuildAgentInputBundleToolOutput,
    WriteAgentPageDraftInput,
    WriteAgentPageDraftToolOutput,
)
from backend.schemas.mcp.context_tools import (
    BeginContextEditInput,
    BeginContextEditToolOutput,
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
    SubmitRevisionForReviewInput,
    SubmitRevisionForReviewOutput,
)
from backend.schemas.mcp.upload_tools import UploadImageInput, UploadImageToolOutput

# Anchored to the directory that holds the `backend` package, not to the
# process cwd: main() deletes stale contracts, and a cwd-relative target would
# make the delete set depend on where the exporter happened to be launched
# from. That anchor is the repo root in a source checkout; in an installed
# wheel (backend.scripts is packaged) it is site-packages, which has no
# contract directory to maintain -- hence the is_dir() guard in main(), which
# keeps both the writes and the deletes off such an environment instead of
# materialising a stray shared/schemas tree there.
SCHEMA_DIR = Path(__file__).resolve().parents[2] / "shared" / "schemas" / "mcp" / "v1"


def build_mcp_schemas() -> dict[str, dict]:
    return {
        "list_my_workspaces": {
            "input": ListMyWorkspacesInput.model_json_schema(),
            "output": ListMyWorkspacesOutput.model_json_schema(),
        },
        "guide": {
            "input": GuideInput.model_json_schema(),
            "output": GuideOutput.model_json_schema(),
        },
        "search": {
            "input": SearchInput.model_json_schema(),
            "output": SearchOutput.model_json_schema(),
        },
        "fetch": {
            "input": FetchInput.model_json_schema(),
            "output": FetchOutput.model_json_schema(),
        },
        "grep_wiki": {
            "input": GrepWikiInput.model_json_schema(),
            "output": GrepWikiOutput.model_json_schema(),
        },
        "what_changed": {
            "input": WhatChangedInput.model_json_schema(),
            "output": WhatChangedOutput.model_json_schema(),
        },
        "read": {
            "input": ReadInput.model_json_schema(),
            "output": ReadOutput.model_json_schema(),
        },
        "get_index": {
            "input": GetIndexInput.model_json_schema(),
            "output": GetIndexOutput.model_json_schema(),
        },
        "read_concept": {
            "input": ReadConceptInput.model_json_schema(),
            "output": ReadConceptOutput.model_json_schema(),
        },
        "list_changes": {
            "input": ListChangesInput.model_json_schema(),
            "output": ListChangesOutput.model_json_schema(),
        },
        "list_agent_categories": {
            "input": ListAgentCategoriesInput.model_json_schema(),
            "output": ListAgentCategoriesOutput.model_json_schema(),
        },
        "read_agent_page": {
            "input": ReadAgentPageInput.model_json_schema(),
            "output": ReadAgentPageOutput.model_json_schema(),
        },
        "read_context_outline": {
            "input": ReadContextOutlineInput.model_json_schema(),
            "output": ReadContextOutlineOutput.model_json_schema(),
        },
        "read_structure_map": {
            "input": ReadStructureMapInput.model_json_schema(),
            "output": ReadStructureMapOutput.model_json_schema(),
        },
        "upload_image": {
            "input": UploadImageInput.model_json_schema(),
            "output": UploadImageToolOutput.model_json_schema(),
        },
        "cite": {
            "input": CiteInput.model_json_schema(),
            "output": CiteToolOutput.model_json_schema(),
        },
        "submit_feedback": {
            "input": SubmitFeedbackInput.model_json_schema(),
            "output": SubmitFeedbackToolOutput.model_json_schema(),
        },
        "write_agent_page_draft": {
            "input": WriteAgentPageDraftInput.model_json_schema(),
            "output": WriteAgentPageDraftToolOutput.model_json_schema(),
        },
        "build_agent_input_bundle": {
            "input": BuildAgentInputBundleInput.model_json_schema(),
            "output": BuildAgentInputBundleToolOutput.model_json_schema(),
        },
        "list_requested_changes": {
            "input": ListRequestedChangesInput.model_json_schema(),
            "output": ListRequestedChangesOutput.model_json_schema(),
        },
        "submit_revision_for_review": {
            "input": SubmitRevisionForReviewInput.model_json_schema(),
            "output": SubmitRevisionForReviewOutput.model_json_schema(),
        },
        "wiki_maintenance": {
            "input": WikiMaintenanceInput.model_json_schema(),
            "output": WikiMaintenanceOutput.model_json_schema(),
        },
        "create_context_page": {
            "input": CreateContextPageInput.model_json_schema(),
            "output": CreateContextPageToolOutput.model_json_schema(),
        },
        "begin_context_edit": {
            "input": BeginContextEditInput.model_json_schema(),
            "output": BeginContextEditToolOutput.model_json_schema(),
        },
        "update_context_edit": {
            "input": UpdateContextEditInput.model_json_schema(),
            "output": UpdateContextEditToolOutput.model_json_schema(),
        },
        "submit_context_edit_for_review": {
            "input": SubmitContextEditForReviewInput.model_json_schema(),
            "output": SubmitContextEditForReviewToolOutput.model_json_schema(),
        },
        "discard_context_edit": {
            "input": DiscardContextEditInput.model_json_schema(),
            "output": DiscardContextEditToolOutput.model_json_schema(),
        },
        "patch_context_edit": {
            "input": PatchContextEditInput.model_json_schema(),
            "output": PatchContextEditToolOutput.model_json_schema(),
        },
        "list_my_context_edits": {
            "input": ListMyContextEditsInput.model_json_schema(),
            "output": ListMyContextEditsToolOutput.model_json_schema(),
        },
    }


def main(schema_dir: Path | None = None) -> None:
    # SCHEMA_DIR is read here, at call time, and deliberately NOT written as a
    # `schema_dir: Path = SCHEMA_DIR` default: such a default binds at import
    # time, which would make a test's monkeypatch of the module global silently
    # ineffective and point the unlink below at the checked-in contracts.
    target = SCHEMA_DIR if schema_dir is None else schema_dir
    if not target.is_dir():
        # Never create the tree: outside a source checkout (an installed wheel
        # anchors at site-packages) there is no contract directory to maintain,
        # and a script that unlinks *.json has no business inventing one.
        raise SystemExit(
            f"MCP contract directory not found: {target}. This exporter maintains "
            "a source checkout's shared/schemas/mcp/v1 and deletes the contracts "
            "of tools that are no longer registered; run it from a checkout, via "
            "`make export-schemas`."
        )
    schemas = build_mcp_schemas()
    for tool_name, schema in schemas.items():
        (target / f"{tool_name}.json").write_text(
            json.dumps(schema, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    # A removed tool leaves its JSON behind untouched, so the drift check in
    # `make verify-generated-schemas` sees no change and certifies a directory
    # that still advertises a tool the server no longer implements. Pruning here
    # turns the stale file into a deletion git can see. Runs after the writes so
    # an interrupted export cannot leave the directory emptier than it started
    # (pinned by test_mcp_schema_export_prunes_only_after_the_writes_succeed).
    for artifact in sorted(target.glob("*.json")):
        if artifact.stem not in schemas:
            artifact.unlink()


if __name__ == "__main__":
    main()

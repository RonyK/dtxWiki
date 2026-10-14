"""Canonical semantic dictionary shared by Graph query projections.

Storage kinds are facts; labels, visual layer, direction, and action meaning
must not be reverse-engineered independently by each renderer.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GraphSemantic:
    kind: str
    label: str
    inverse_label: str
    layer: str
    action_kind: str | None
    provenance: str = "derived"
    directed: bool = True
    default_visible: bool = True


EDGE_SEMANTICS: dict[str, GraphSemantic] = {
    "wikilink": GraphSemantic(
        "wikilink",
        "links to",
        "linked from",
        "knowledge",
        None,
        provenance="curated",
    ),
    "tree": GraphSemantic(
        "tree",
        "contains",
        "belongs to",
        "knowledge",
        None,
        provenance="curated",
    ),
    "citation_page": GraphSemantic(
        "citation_page",
        "cites",
        "cited by",
        "evidence",
        "cited",
        provenance="curated",
    ),
    "citation_source": GraphSemantic(
        "citation_source",
        "cites source",
        "cited by",
        "evidence",
        "cited",
        provenance="curated",
    ),
    "ask_citation": GraphSemantic(
        "ask_citation",
        "included as evidence",
        "included by",
        "evidence",
        "included",
        provenance="usage",
    ),
    "promoted_from": GraphSemantic(
        "promoted_from", "was promoted from", "promoted to", "action", "promoted"
    ),
    "page_provenance": GraphSemantic(
        "page_provenance", "was created from", "produced", "activity", "created"
    ),
    "library_sync_version": GraphSemantic(
        "library_sync_version",
        "was retrieved in",
        "retrieved",
        "activity",
        "retrieved",
    ),
    "version_parent": GraphSemantic(
        "version_parent", "supersedes", "superseded by", "activity", "superseded"
    ),
    # A diverged Context branch folded cleanly into a linear version: the
    # git-merge "second parent", kept distinct from version_parent so a query
    # can tell lineage from reconciliation.
    "version_merge": GraphSemantic(
        "version_merge", "was merged into", "merged from", "activity", "merged"
    ),
    "co_access": GraphSemantic(
        "co_access",
        "often read together",
        "often read together",
        "activity",
        None,
        provenance="usage",
        directed=False,
    ),
}


ACTION_SEMANTICS: dict[str, GraphSemantic] = {
    "searched": GraphSemantic(
        "searched", "searched", "was searched by", "action", "searched"
    ),
    "returned": GraphSemantic(
        "returned", "returned candidate", "was returned to", "action", "returned"
    ),
    "retrieved": GraphSemantic(
        "retrieved", "retrieved", "was retrieved by", "action", "retrieved"
    ),
    "included": GraphSemantic(
        "included", "included in context", "was included by", "action", "included"
    ),
    "cited": GraphSemantic("cited", "cited", "was cited by", "evidence", "cited"),
    "created": GraphSemantic(
        "created", "created", "was created by", "action", "created"
    ),
    "edited": GraphSemantic("edited", "edited", "was edited by", "action", "edited"),
    "reviewed": GraphSemantic(
        "reviewed", "reviewed", "was reviewed by", "activity", "reviewed"
    ),
    "approved": GraphSemantic(
        "approved", "approved", "was approved by", "action", "approved"
    ),
    "rejected": GraphSemantic(
        "rejected", "rejected", "was rejected by", "action", "rejected"
    ),
    "request_changes": GraphSemantic(
        "request_changes",
        "requested changes to",
        "received change request from",
        "action",
        "request_changes",
    ),
    "published": GraphSemantic(
        "published", "published", "was published by", "action", "published"
    ),
    "restored": GraphSemantic(
        "restored", "restored", "was restored by", "action", "restored"
    ),
    "superseded": GraphSemantic(
        "superseded", "superseded", "was superseded by", "activity", "superseded"
    ),
    "merged": GraphSemantic(
        "merged", "merged into", "was merged from", "activity", "merged"
    ),
    "promoted": GraphSemantic(
        "promoted", "promoted", "was promoted by", "action", "promoted"
    ),
    "tool_produced": GraphSemantic(
        "tool_produced",
        "tool produced",
        "was produced by tool",
        "action",
        "tool_produced",
    ),
    "referenced": GraphSemantic(
        "referenced", "referenced", "was referenced by", "evidence", "referenced"
    ),
}


NODE_FAMILY_BY_KIND: dict[str, str] = {
    "context_page": "document",
    "context_page_version": "document",
    "library_item": "document",
    "library_item_version": "document",
    "library_artifact": "document",
    "mcp_chat_session": "chat",
    "ask_session": "ask",
    "ask_turn": "ask",
    "review_item": "review",
    "review_decision": "review",
    "agent_page": "agent",
    "actor": "agent",
    "audit_event": "event",
    "mcp_tool_call": "event",
    "change_set": "event",
    "change_operation": "event",
    "graph_observation": "event",
    "workspace_tree_node": "structural",
    "library_sync_run": "event",
}


def edge_semantic(edge_kind: str) -> GraphSemantic:
    """Fail closed to an explicit, non-action knowledge relation."""

    return EDGE_SEMANTICS.get(
        edge_kind,
        GraphSemantic(
            edge_kind,
            edge_kind.replace("_", " "),
            edge_kind.replace("_", " "),
            "knowledge",
            None,
            default_visible=False,
        ),
    )


def action_semantic(action_kind: str) -> GraphSemantic:
    return ACTION_SEMANTICS.get(
        action_kind,
        GraphSemantic(
            action_kind,
            action_kind.replace("_", " "),
            action_kind.replace("_", " "),
            "action",
            action_kind,
            default_visible=False,
        ),
    )

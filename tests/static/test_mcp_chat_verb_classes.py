"""Regression gates for MCP chat artifact relationship semantics."""

from __future__ import annotations

import ast
from pathlib import Path
from uuid import uuid4

import pytest

from backend.models.mcp_chat import (
    AUTHORING_VERBS,
    CONSUMPTION_VERBS,
    EXPOSURE_VERBS,
    McpChatArtifactVerb,
)
from backend.services.indexing.edge_registry import CO_ACCESS_SOURCE_VERBS
from backend.services.mcp_trace_capture import derive_artifact_links

REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECTION_SOURCES = {
    "backend/services/indexing/edge_registry.py": 1,
    "backend/services/trail_analysis.py": 1,
}
# Projection verb filters may name only these allowlists. Both are pinned to
# consumption below, so no filter can admit an exposure or authoring verb.
PROJECTION_VERB_ALLOWLISTS = frozenset({"CONSUMPTION_VERBS", "CO_ACCESS_SOURCE_VERBS"})


def test_artifact_verbs_are_partitioned_into_exactly_one_semantic_class():
    classes = (CONSUMPTION_VERBS, AUTHORING_VERBS, EXPOSURE_VERBS)

    assert _partition_violations(classes, set(McpChatArtifactVerb)) == []
    assert McpChatArtifactVerb.SEARCH_MATCH not in CONSUMPTION_VERBS


def test_co_access_reads_only_focused_reads():
    """Co-reads are a subset of consumption without per-write citation fan-out.

    Trace capture writes one CITED row per citation candidate of a write, all
    at the same instant, so admitting CITED would pair every candidate.
    """

    assert CO_ACCESS_SOURCE_VERBS <= CONSUMPTION_VERBS
    assert McpChatArtifactVerb.CITED not in CO_ACCESS_SOURCE_VERBS
    assert McpChatArtifactVerb.REFERENCED in CO_ACCESS_SOURCE_VERBS


@pytest.mark.counterexample(
    "search_match classified as both exposure and consumption must fail the gate"
)
def test_partition_gate_rejects_search_exposure_as_consumption():
    mutant = (
        CONSUMPTION_VERBS | EXPOSURE_VERBS,
        AUTHORING_VERBS,
        EXPOSURE_VERBS,
    )

    assert _partition_violations(mutant, set(McpChatArtifactVerb))


def test_search_tools_derive_exposure_links_only():
    context_id, library_id, agent_id = uuid4(), uuid4(), uuid4()
    search_links = derive_artifact_links(
        session=None,  # Direct surface ids require no database normalization.
        tool_name="search",
        arguments={"query": "semantic"},
        payload={
            "data": {
                "ranked_results": [
                    {"id": str(context_id), "type": "context_page"},
                    {"id": str(library_id), "type": "library_item"},
                    {"id": str(agent_id), "type": "agent_page"},
                ]
            }
        },
    )
    grep_links = derive_artifact_links(
        session=None,
        tool_name="grep_wiki",
        arguments={"pattern": "semantic"},
        payload={"matches": [{"page_id": str(context_id)}]},
    )

    assert search_links
    assert grep_links
    assert {link.verb for link in [*search_links, *grep_links]} <= EXPOSURE_VERBS


def test_projection_filters_use_the_consumption_allowlist_only():
    for relative_path, expected_count in PROJECTION_SOURCES.items():
        tree = ast.parse((REPO_ROOT / relative_path).read_text(encoding="utf-8"))
        arguments = [
            call.args[0]
            for call in ast.walk(tree)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "in_"
            and _attribute_path(call.func.value).endswith(
                # Co-reads (co_access and hot paths) project from the
                # per-call edge, which has every read's own instant; the hot
                # concept aggregate still reads the rollup. Every filter must
                # name a consumption-pinned allowlist.
                ("McpChatArtifactLink.verb", "McpChatToolCallArtifact.verb")
            )
            and call.args
        ]

        assert len(arguments) == expected_count, relative_path
        assert all(
            isinstance(argument, ast.Name)
            and argument.id in PROJECTION_VERB_ALLOWLISTS
            for argument in arguments
        ), relative_path


def _attribute_path(node: ast.expr) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _partition_violations(classes, all_verbs) -> list[str]:
    violations: list[str] = []
    if set().union(*classes) != all_verbs:
        violations.append("semantic classes do not cover exactly every verb")
    for index, left in enumerate(classes):
        for right in classes[index + 1 :]:
            if not left.isdisjoint(right):
                violations.append("semantic classes overlap")
    return violations

"""The cross-language contract and the single authored-link decision table."""

import json
from pathlib import Path
from uuid import UUID

from backend.services.context_links import classify, format_stable_url, link_markdown


def test_context_link_contract_and_source_decisions():
    # Budget exception: each fixture row's `reason` names a different URL
    # boundary (scheme, authority, scope, encoding, identity, or fragment).
    # Existing service tests cannot detect Python/TypeScript parser drift.
    # Formatting, original-AST eligibility and destination-only source spans
    # are separate failure paths and require this cheapest pure-unit layer.
    fixture = json.loads(
        (
            Path(__file__).parents[3] / "shared/fixtures/context_link_locators.json"
        ).read_text()
    )
    workspace_id = UUID(fixture["workspace_id"])
    for case in fixture["cases"]:
        assert case["reason"]
        locator = classify(case["input"], fixture["self_origins"])
        assert locator.kind == case["kind"], case["name"]
        assert (str(locator.workspace_id) if locator.workspace_id else None) == case[
            "workspace_id"
        ]
        assert (str(locator.page_id) if locator.page_id else None) == case["page_id"]
        assert locator.fragment == case["fragment"]
        if case["stable_url"]:
            assert (
                format_stable_url(
                    locator.workspace_id or workspace_id,
                    locator.page_id,
                    locator.fragment,
                )
                == case["stable_url"]
            )
    for case in fixture["format_cases"]:
        assert case["reason"]
        assert (
            format_stable_url(case["workspace_id"], case["page_id"], case["fragment"])
            == case["stable_url"]
        )
        assert (
            link_markdown(
                case["workspace_id"], case["page_id"], case["title"], case["fragment"]
            )
            == case["link_markdown"]
        )

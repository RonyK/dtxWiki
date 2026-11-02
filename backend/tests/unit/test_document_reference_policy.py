"""Settings and publication consume the same declared bibliography policy."""

from types import SimpleNamespace

import pytest

from backend.services.citation_policy import (
    DOCUMENT_CITATION_KINDS,
    CitationPolicyError,
    normalized_citation_policy,
)
from backend.services.settings_service import (
    SettingsPolicyUpdateRejected,
    SettingsService,
    _coerce_policy_value,
)


def test_document_kinds_normalize_without_changing_legacy_evidence_policy():
    # Budget exception: persisted defaults, settings coercion and malformed
    # policy values must agree before any writer runs. Command and migration
    # lifecycles do not exercise this fail-closed boundary; no DB case is needed.
    legacy = {"strictness": "strict", "accepted_citation_types": ["raw_source"]}
    effective = normalized_citation_policy(legacy)
    assert effective == {
        **legacy,
        "accepted_document_kinds": list(DOCUMENT_CITATION_KINDS),
    }
    assert legacy == {"strictness": "strict", "accepted_citation_types": ["raw_source"]}
    assert (
        SettingsService()._policy_group_projection(
            SimpleNamespace(citation_policy=legacy), "citation"
        )
        == effective
    )
    policy = {**legacy, "accepted_document_kinds": []}
    assert normalized_citation_policy(policy)["accepted_document_kinds"] == []
    assert _coerce_policy_value("accepted_document_kinds", []) == []
    patched = _coerce_policy_value("accepted_document_kinds", "agent_page,context_page")
    assert patched == ["context_page", "agent_page"]
    assert (
        normalized_citation_policy({**legacy, "accepted_document_kinds": patched})[
            "accepted_document_kinds"
        ]
        == patched
    )
    with pytest.raises(SettingsPolicyUpdateRejected):
        _coerce_policy_value("accepted_document_kinds", ["raw_source"])
    with pytest.raises(CitationPolicyError):
        normalized_citation_policy({"accepted_document_kinds": False})

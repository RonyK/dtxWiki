from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Any

from backend.models.workspace import Workspace
from backend.spi.types import AI_PROVENANCES

REVIEW_RISK_POLICY_VERSION = "2026-05-31.balanced.v1"
RISK_LEVELS = ("low", "medium", "high")
RISK_ORDER = {level: index for index, level in enumerate(RISK_LEVELS)}
AUTO_APPROVE_DELAY_MIN_DAYS = 0  # F-9: delay 0 = no aging wait (instant on sweep)
AUTO_APPROVE_DELAY_MAX_DAYS = 30
RECONCILIATION_REMINDER_DEFAULT_HOURS = 24
RECONCILIATION_ATTENTION_DEFAULT_DAYS = 14
RECONCILIATION_REMINDER_MAX_HOURS = 24 * 30
RECONCILIATION_ATTENTION_MAX_DAYS = 365
REVIEW_RISK_PRESETS = ("conservative", "balanced", "fast_moving")
REVIEW_RISK_SIGNALS = {
    "citation_warning",
    "risk_notes_present",
    "manual_citation",
    "decision_page_type",
    "low_placement_confidence",
    "sensitive_agent_category",
    # Auto-approval gate signals (lint-gate plan D-T3). Caller-requested review
    # raises severity one level; objective lint errors force high severity.
    "caller_needs_review",
    "lint_error",
}

# Gate signals whose severity behavior is fixed independently of configurable
# workspace risk rules. Both remain review vetoes in the auto-approval gate.
GATE_RAISE_SIGNALS = ("caller_needs_review",)
GATE_FORCE_SIGNALS = ("lint_error",)


def _balanced_defaults() -> dict[str, str]:
    # Product decision for Sprint 6: keep routine context drafts/partial updates
    # low risk and full rewrites/publish medium risk in the balanced preset.
    # Context write paths still pass signals.review_required=True, so this
    # choice affects displayed risk and auto-approve eligibility, not whether a
    # ReviewItem is created.
    return {
        "context_draft_create": "low",
        "context_partial_update": "low",
        "context_full_rewrite": "medium",
        "context_publish": "medium",
        "context_ingress_auto": "low",
        "context_ingress_review_required": "medium",
        "direct_page_write_audit": "low",
        "stale_alternative": "medium",
        "inline_comment": "low",
        "agent_sensitive_draft": "high",
        "agent_non_sensitive_draft": "low",
        "agent_sensitive_activation": "high",
        "agent_non_sensitive_activation": "low",
        "tree_mutation": "low",
        # Informational FYI raised when a cited Library source is hard-deleted;
        # the citing page keeps a render-time tombstone, so this is low risk.
        "source_deleted_notice": "low",
    }


def _preset_policy(preset: str) -> dict[str, Any]:
    defaults = _balanced_defaults()
    if preset == "conservative":
        defaults = {
            **defaults,
            "context_draft_create": "medium",
            "context_partial_update": "medium",
            "context_full_rewrite": "high",
            "context_ingress_auto": "medium",
            "inline_comment": "medium",
            "agent_non_sensitive_draft": "medium",
        }
        review_from = "low"
    elif preset == "fast_moving":
        defaults = {
            **defaults,
            "context_publish": "low",
            "context_ingress_review_required": "low",
            "stale_alternative": "low",
        }
        review_from = "medium"
    else:
        preset = "balanced"
        review_from = "medium"
    # Codex #16: a fresh workspace (balanced default) must NOT auto-approve until
    # an admin opts in. Only the explicit fast_moving preset enables low/medium
    # auto-approval; balanced/conservative leave all per-risk rows disabled.
    auto_low_medium = preset == "fast_moving"
    return {
        "preset": preset,
        "policy_version": REVIEW_RISK_POLICY_VERSION,
        "defaults": defaults,
        "risk_rules": {
            "raise_one_level": [
                "manual_citation",
                "decision_page_type",
                "low_placement_confidence",
            ],
            "force_high": [
                "citation_warning",
                "risk_notes_present",
                "sensitive_agent_category",
            ],
        },
        "review_required": {
            "from_risk": review_from,
            "always_for_sensitive_agent": True,
            "always_for_decision_page": True,
            # The one switch that decides whether an AI-authored write needs a
            # person. True keeps a new workspace safe: anything arriving over
            # MCP, OAuth or a PAT waits for a human. Turning it off is how a
            # workspace says "I trust what my agents write" -- those writes then
            # publish the way a person's do, still carrying a ChangeSet, an
            # approved ReviewItem with this policy snapshot, and an audit event.
            # Nothing else in the resolution keys on provenance, so this is the
            # only place that decision lives.
            "always_for_ai_authored": True,
        },
        "automation": {
            "auto_approve_enabled": False,
            "auto_approve_below": "medium",
            "auto_approve_after_hours": 168,
            "auto_approve_by_risk": {
                "low": {"allowed": auto_low_medium, "delay_days": 7},
                "medium": {"allowed": auto_low_medium, "delay_days": 14},
                "high": {"allowed": False, "delay_days": 28},
            },
        },
        "reconciliation": {
            "reminder_after_hours": RECONCILIATION_REMINDER_DEFAULT_HOURS,
            "attention_after_days": RECONCILIATION_ATTENTION_DEFAULT_DAYS,
        },
    }


def default_review_policy() -> dict[str, Any]:
    return _preset_policy("balanced")


def apply_review_preset(policy: dict[str, Any], preset: str) -> dict[str, Any]:
    if preset not in REVIEW_RISK_PRESETS:
        raise ValueError("preset must be conservative, balanced, or fast_moving")
    next_policy = _preset_policy(preset)
    next_policy["automation"] = {
        **next_policy["automation"],
        **normalized_review_policy(policy)["automation"],
    }
    return next_policy


def normalized_review_policy(policy: dict[str, Any] | None) -> dict[str, Any]:
    base = default_review_policy()
    if not isinstance(policy, dict):
        return base
    nested_keys = {
        "defaults",
        "risk_rules",
        "review_required",
        "automation",
        "reconciliation",
    }
    merged = {
        **base,
        **{key: value for key, value in policy.items() if key not in nested_keys},
        "defaults": {
            **base["defaults"],
            **_dict_value(policy.get("defaults")),
        },
        "risk_rules": {
            "raise_one_level": _signal_list(
                policy.get("risk_rules", {}).get("raise_one_level")
                if isinstance(policy.get("risk_rules"), dict)
                else None,
                base["risk_rules"]["raise_one_level"],
            ),
            "force_high": _signal_list(
                policy.get("risk_rules", {}).get("force_high")
                if isinstance(policy.get("risk_rules"), dict)
                else None,
                base["risk_rules"]["force_high"],
            ),
        },
        "review_required": {
            **base["review_required"],
            **_dict_value(policy.get("review_required")),
        },
        "automation": {
            **base["automation"],
            **_dict_value(policy.get("automation")),
        },
        "reconciliation": {
            **base["reconciliation"],
            **_dict_value(policy.get("reconciliation")),
        },
    }
    merged["preset"] = _preset_value(merged.get("preset"))
    merged["policy_version"] = str(
        merged.get("policy_version") or REVIEW_RISK_POLICY_VERSION
    )
    merged["defaults"] = {
        key: _risk_value(value, base["defaults"].get(key, "medium"))
        for key, value in merged["defaults"].items()
    }
    merged["review_required"]["from_risk"] = _risk_value(
        merged["review_required"].get("from_risk"),
        base["review_required"]["from_risk"],
    )
    source_automation = _dict_value(policy.get("automation"))
    automation_source = (
        merged["automation"]
        if "auto_approve_by_risk" in source_automation
        else source_automation
    )
    by_risk = _auto_approve_by_risk_value(automation_source, base["automation"])
    merged["automation"]["auto_approve_by_risk"] = by_risk
    _sync_legacy_auto_approve_fields(merged["automation"])
    merged["reconciliation"] = {
        "reminder_after_hours": _int_between(
            merged["reconciliation"].get("reminder_after_hours"),
            default=RECONCILIATION_REMINDER_DEFAULT_HOURS,
            minimum=1,
            maximum=RECONCILIATION_REMINDER_MAX_HOURS,
        ),
        "attention_after_days": _int_between(
            merged["reconciliation"].get("attention_after_days"),
            default=RECONCILIATION_ATTENTION_DEFAULT_DAYS,
            minimum=1,
            maximum=RECONCILIATION_ATTENTION_MAX_DAYS,
        ),
    }
    return merged


def validate_review_policy(policy: dict[str, Any]) -> dict[str, Any]:
    _validate_raw_auto_approve_by_risk(policy)
    normalized = normalized_review_policy(policy)
    _validate_known_defaults(normalized)
    _validate_automation(normalized)
    return normalized


@dataclass(frozen=True)
class ReviewRiskResolution:
    risk_level: str
    review_required: bool
    reason_codes: list[str]
    policy_version: str
    summary: str
    signals: dict[str, Any]
    policy_snapshot: dict[str, Any]


class ReviewRiskResolver:
    def resolve(
        self,
        *,
        workspace: Workspace,
        event: str,
        signals: dict[str, Any] | None = None,
        actor_provenance: str = "internal",
        # D-3 ratchet: the severity a caller-declared event would have carried
        # (e.g. what `full_rewrite` costs under this policy), applied as a
        # FLOOR under a measured/reclassified `event`. This keeps the resolved
        # LABEL truthful (it names what actually happened) while the SEVERITY
        # never drops below what the original declaration implied -- and the
        # `floor:` reason code below makes it auditable when the ratchet
        # fires. Applied before signal handling so raise_one_level/force_high
        # still compose on top of it instead of being overwritten by it.
        floor_level: str | None = None,
    ) -> ReviewRiskResolution:
        policy = normalized_review_policy(workspace.review_policy)
        signals = dict(signals or {})
        defaults = policy["defaults"]
        base_level = defaults.get(event, "medium")
        level = base_level
        reason_codes = [f"default:{event}:{base_level}"]
        if floor_level and RISK_ORDER.get(floor_level, 1) > RISK_ORDER.get(level, 1):
            level = floor_level
            reason_codes.append(f"floor:{floor_level}")
        active_signals = [
            key
            for key, value in signals.items()
            if key in REVIEW_RISK_SIGNALS and bool(value)
        ]
        for signal in active_signals:
            if (
                signal in policy["risk_rules"]["raise_one_level"]
                or signal in GATE_RAISE_SIGNALS
            ):
                level = promote_one_level(level)
                reason_codes.append(f"raise:{signal}")
        for signal in active_signals:
            if (
                signal in policy["risk_rules"]["force_high"]
                or signal in GATE_FORCE_SIGNALS
            ):
                level = "high"
                reason_codes.append(f"force_high:{signal}")
        review_from = policy["review_required"]["from_risk"]
        review_required = bool(signals.get("review_required")) or risk_at_or_above(
            level,
            review_from,
        )
        if signals.get("sensitive_agent_category") and policy["review_required"].get(
            "always_for_sensitive_agent",
            True,
        ):
            review_required = True
        if signals.get("decision_page_type") and policy["review_required"].get(
            "always_for_decision_page",
            True,
        ):
            review_required = True
        if signals.get("ai_authored") and policy["review_required"].get(
            "always_for_ai_authored",
            True,
        ):
            review_required = True
            reason_codes.append("author:ai_requires_human")
        if actor_provenance not in AI_PROVENANCES and actor_provenance != "internal":
            # A person writing in the browser is not sent to review merely by a
            # risk threshold. An explicit request for another person to review
            # the change is different and must remain effective. Objective lint
            # failures still hold the body back through publication invariants;
            # they do not create a review on the author's behalf.
            review_required = bool(signals.get("caller_needs_review"))
            reason_codes.append("author:human_direct_write")
        # `human_review_required` marks the reviews a person MUST decide, which
        # is narrower than the reviews that get created. Callers used to set it
        # from provenance, making it a second name for `ai_authored`; the expiry
        # sweep then vetoed every agent draft, so no policy could release one.
        # Setting it to `review_required` would be just as wrong in the other
        # direction -- every pending row would be vetoed and the sweep would
        # release nothing at all.
        #
        # So it records only the reasons that outrank the automation matrix: the
        # caller asked for a person, objective lint failed, or the workspace
        # says this category always needs one. "An agent wrote it at routine
        # risk" is not among them -- that is precisely what the matrix is for.
        signals["human_review_required"] = bool(
            signals.get("caller_needs_review")
            or signals.get("lint_error")
            or (
                signals.get("sensitive_agent_category")
                and policy["review_required"].get("always_for_sensitive_agent", True)
            )
            or (
                signals.get("decision_page_type")
                and policy["review_required"].get("always_for_decision_page", True)
            )
        )
        return ReviewRiskResolution(
            risk_level=level,
            review_required=review_required,
            reason_codes=reason_codes,
            policy_version=policy["policy_version"],
            summary=_resolution_summary(event, level, active_signals),
            signals={key: signals[key] for key in sorted(signals)},
            policy_snapshot={
                "preset": policy["preset"],
                "policy_version": policy["policy_version"],
                "default": base_level,
                "review_required_from_risk": review_from,
            },
        )


def review_risk_snapshot(
    resolution: ReviewRiskResolution | None,
) -> dict[str, object | None]:
    if resolution is None:
        return {}
    return {
        "risk_policy_version": resolution.policy_version,
        "risk_resolution_summary": resolution.summary,
        "risk_signals": {
            "reason_codes": resolution.reason_codes,
            **resolution.signals,
        },
        "risk_policy_snapshot": resolution.policy_snapshot,
    }


def ai_writes_require_human_review(workspace: Workspace) -> bool:
    """Whether this workspace still wants a person in front of an AI write.

    Write paths ask this before they decide to stage a draft: if the workspace
    has turned the requirement off, an agent's write publishes the way a
    person's does, and staging it would leave a draft nobody is coming for.
    """
    policy = normalized_review_policy(workspace.review_policy)
    return bool(policy["review_required"].get("always_for_ai_authored", True))


def promote_one_level(level: str) -> str:
    index = min(RISK_ORDER.get(level, 1) + 1, len(RISK_LEVELS) - 1)
    return RISK_LEVELS[index]


def risk_at_or_above(level: str, threshold: str) -> bool:
    return RISK_ORDER.get(level, 0) >= RISK_ORDER.get(threshold, 1)


def risk_at_or_below(level: str, threshold: str) -> bool:
    return RISK_ORDER.get(level, 0) <= RISK_ORDER.get(threshold, 1)


def auto_approve_risk_levels(policy: dict[str, Any]) -> list[str]:
    automation = normalized_review_policy(policy)["automation"]
    # F-9: high is included — it auto-approves only when an admin has opted in
    # (allowed via the UI danger acknowledgement); otherwise its allowed=False.
    return [
        level
        for level in RISK_LEVELS
        if automation["auto_approve_by_risk"][level]["allowed"]
    ]


def auto_approve_by_risk_entries(policy: dict[str, Any]) -> list[dict[str, int | str]]:
    automation = normalized_review_policy(policy)["automation"]
    return [
        {
            "risk_level": level,
            "delay_days": automation["auto_approve_by_risk"][level]["delay_days"],
        }
        for level in RISK_LEVELS
        if automation["auto_approve_by_risk"][level]["allowed"]
    ]


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _signal_list(value: Any, default: list[str]) -> list[str]:
    if not isinstance(value, list):
        return list(default)
    normalized = []
    for item in value:
        if (
            isinstance(item, str)
            and item in REVIEW_RISK_SIGNALS
            and item not in normalized
        ):
            normalized.append(item)
    return normalized


def _preset_value(value: Any) -> str:
    if isinstance(value, str) and value in REVIEW_RISK_PRESETS:
        return value
    return "balanced"


def _risk_value(value: Any, default: str) -> str:
    return value if isinstance(value, str) and value in RISK_LEVELS else default


def _auto_approve_below_value(value: Any) -> str:
    return value if value in {"low", "medium"} else "medium"


def _auto_approve_by_risk_value(
    automation: dict[str, Any],
    base_automation: dict[str, Any],
) -> dict[str, dict[str, int | bool]]:
    raw = automation.get("auto_approve_by_risk")
    if not isinstance(raw, dict):
        return _legacy_auto_approve_by_risk(automation)
    normalized: dict[str, dict[str, int | bool]] = {}
    base_by_risk = _dict_value(base_automation.get("auto_approve_by_risk"))
    for level in RISK_LEVELS:
        row = _dict_value(raw.get(level))
        base_row = _dict_value(base_by_risk.get(level))
        # RO-3: "allowed" is the canonical key; absorb the legacy "enabled" key
        # for one release so a partial deploy / unmigrated row keeps its value.
        allowed_value = row.get("allowed")
        if allowed_value is None:
            allowed_value = row.get(
                "enabled",
                base_row.get("allowed", base_row.get("enabled", False)),
            )
        allowed = bool(allowed_value)
        delay_days = _int_between(
            row.get("delay_days", base_row.get("delay_days")),
            default=7,
            minimum=AUTO_APPROVE_DELAY_MIN_DAYS,
            maximum=AUTO_APPROVE_DELAY_MAX_DAYS,
        )
        normalized[level] = {
            # F-9: high may be allowed once an admin acknowledges the risk in the
            # UI ("danger noted"); the workspace owns this informed choice.
            "allowed": allowed,
            "delay_days": delay_days,
        }
    return normalized


def _legacy_auto_approve_by_risk(
    automation: dict[str, Any],
) -> dict[str, dict[str, int | bool]]:
    enabled = bool(automation.get("auto_approve_enabled"))
    threshold = _auto_approve_below_value(automation.get("auto_approve_below"))
    legacy_hours = _int_between(
        automation.get("auto_approve_after_hours"),
        default=168,
        minimum=1,
        maximum=720,
    )
    # Legacy policies used one hour-based delay for every eligible level.
    # Convert conservatively by rounding up to whole days, so existing rows do
    # not become eligible earlier during the model migration.
    delay_days = _int_between(
        ceil(legacy_hours / 24),
        default=7,
        minimum=AUTO_APPROVE_DELAY_MIN_DAYS,
        maximum=AUTO_APPROVE_DELAY_MAX_DAYS,
    )
    return {
        level: {
            "allowed": enabled
            and level != "high"
            and risk_at_or_below(level, threshold),
            "delay_days": delay_days,
        }
        for level in RISK_LEVELS
    }


def _sync_legacy_auto_approve_fields(automation: dict[str, Any]) -> None:
    by_risk = automation["auto_approve_by_risk"]
    enabled_levels = [level for level in ("low", "medium") if by_risk[level]["allowed"]]
    automation["auto_approve_enabled"] = bool(enabled_levels)
    automation["auto_approve_below"] = "medium" if "medium" in enabled_levels else "low"
    delay_days = min(
        (by_risk[level]["delay_days"] for level in enabled_levels),
        default=by_risk["low"]["delay_days"],
    )
    automation["auto_approve_after_hours"] = int(delay_days) * 24


def _validate_raw_auto_approve_by_risk(policy: dict[str, Any]) -> None:
    automation = _dict_value(policy.get("automation"))
    by_risk = automation.get("auto_approve_by_risk")
    if not isinstance(by_risk, dict):
        return
    # F-9: high.allowed is now permitted — an admin opts in via the UI's
    # "danger noted" acknowledgement. (Previously high was forced to false here.)


def _int_between(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return min(max(value, minimum), maximum)


def _validate_known_defaults(policy: dict[str, Any]) -> None:
    allowed_defaults = set(default_review_policy()["defaults"])
    unknown = set(policy["defaults"]) - allowed_defaults
    if unknown:
        raise ValueError(f"unknown review risk default: {sorted(unknown)[0]}")
    for key, value in policy["defaults"].items():
        if value not in RISK_LEVELS:
            raise ValueError(f"{key} must be low, medium, or high")


def _validate_automation(policy: dict[str, Any]) -> None:
    automation = policy["automation"]
    review_from = policy["review_required"]["from_risk"]
    for level, row in automation["auto_approve_by_risk"].items():
        if level not in RISK_LEVELS:
            raise ValueError("auto_approve_by_risk contains an unknown risk level")
        # F-9: high.allowed is gated by the UI "danger noted" acknowledgement,
        # not by review_required.from_risk — an admin may opt high in regardless
        # of the manual-review threshold. low/medium still cannot exceed it.
        if (
            level != "high"
            and row["allowed"]
            and not risk_at_or_below(level, review_from)
        ):
            raise ValueError(
                f"auto_approve_by_risk.{level}.allowed cannot exceed "
                "review_required.from_risk"
            )
        if not (
            AUTO_APPROVE_DELAY_MIN_DAYS
            <= row["delay_days"]
            <= AUTO_APPROVE_DELAY_MAX_DAYS
        ):
            raise ValueError(
                f"auto_approve_by_risk.{level}.delay_days must be between "
                f"{AUTO_APPROVE_DELAY_MIN_DAYS} and {AUTO_APPROVE_DELAY_MAX_DAYS}"
            )


def _resolution_summary(event: str, level: str, signals: list[str]) -> str:
    labels = [_humanize_token(event)]
    if signals:
        labels.extend(_humanize_token(signal) for signal in signals)
    return f"{', '.join(labels)} classified as {level} risk."


def _humanize_token(value: str) -> str:
    return value.replace("_", " ")

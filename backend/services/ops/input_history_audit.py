"""Read-only reports for preserved data outside the scalar migration CHECKs.

These findings never block migration, revoke grants, or rewrite history. This
reader reflects only available tables/columns, so it also works before upgrade.
Plaintext passwords and token hashes cannot reveal old credential lengths and
are deliberately not inspected.
"""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

import sqlalchemy as sa

from backend.domain.input_policy import normalize_short_text
from backend.domain.oauth_input import (
    validate_client_name,
    validate_identifier,
    validate_redirect_list,
    validate_scope_input,
    validate_supported_metadata,
)

_SCALARS = (
    ("context_page_version", "body_md", 1_000_000, False),
    ("agent_page_version", "body_markdown", 1_000_000, False),
    ("library_semantic_overlay", "body_markdown", 1_000_000, True),
    ("ask_turn", "user_message", 8_000, False),
    ("change_set", "reason", 2_000, False),
    ("review_decision", "decision_reason", 2_000, False),
    ("library_discovery_candidate", "decision_reason", 2_000, False),
    ("lint_finding", "suggested_action", 2_000, False),
    ("feedback_item", "body", 10_000, False),
    ("context_page", "placement_reason", 2_000, False),
    ("context_ingress_proposal", "placement_reason", 2_000, False),
    ("agent_page_version", "activation_reason", 2_000, False),
    ("library_item_version", "original_filename", 255, True),
)
_LABELS = (
    ("context_page", "tags", 32, 48),
    ("context_page", "aliases", 32, 80),
    ("agent_page", "tags", 32, 48),
    ("ask_session", "default_topic_tags", 32, 48),
    ("ask_turn", "topic_tags", 32, 48),
    ("api_tokens", "scopes", 16, None),
)
_JSON_HISTORY = (
    ("agent_page_version", "frontmatter"),
    ("context_page_version", "reconcile_manifest"),
    ("audit_event", "payload"),
    ("change_operation", "diff_ref"),
    ("workspace", "library_policy"),
)
_TEXT_FIELDS = {
    "body_md": 1_000_000,
    "body_markdown": 1_000_000,
    "merged_body_markdown": 1_000_000,
    "reason": 2_000,
    "decision_reason": 2_000,
    "note": 2_000,
    "risk_notes": 2_000,
    "patch_description": 2_000,
    "suggested_action": 2_000,
    "rationale": 1_000,
    "anchor_text": 2_000,
    "context_before": 500,
    "context_after": 500,
}
_LIST_FIELDS = {
    "tags": (32, 48),
    "default_topic_tags": (32, 48),
    "aliases": (32, 80),
    "context_refs": (128, None),
    "library_artifact_refs": (128, None),
    "requested_categories": (4, None),
    "workspace_grants": (256, None),
    "workspace_ids": (256, None),
    "operations": (20, None),
    "original_operations": (20, None),
}


def _fingerprint(value) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def _safe_id(value) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return "sha256:" + _fingerprint(value)


def _json_value(value):
    return json.loads(value) if isinstance(value, str) else value


def _list_invalid(value, maximum, item_maximum=None) -> bool:
    if not isinstance(value, list) or len(value) > maximum:
        return True
    if item_maximum is None:
        return False
    try:
        for item in value:
            normalize_short_text(item, field="label", max_length=item_maximum)
    except ValueError:
        return True
    return False


def _history_json_violations(value) -> set[str]:
    """Inspect known request fields, never invent a limit for output bundles.

    In particular context_version_refs/invalidation_keys are computed output,
    not submitted context_refs, and must not acquire an input-list ceiling.
    """
    result = set()
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, list):
            stack.extend(current)
            continue
        if not isinstance(current, dict):
            continue
        for key, item in current.items():
            if key in _TEXT_FIELDS and isinstance(item, str):
                if len(item) > _TEXT_FIELDS[key]:
                    result.add(key)
            elif key in _LIST_FIELDS and item is not None:
                if _list_invalid(item, *_LIST_FIELDS[key]):
                    result.add(key)
            if isinstance(item, (list, dict)):
                stack.append(item)
        context = current.get("context_refs")
        library = current.get("library_artifact_refs")
        if isinstance(context, list) and isinstance(library, list):
            if len(context) + len(library) > 256:
                result.add("bundle_references")
    return result


def _library_policy_violations(value) -> set[str]:
    if not isinstance(value, dict):
        return {"shape"}
    result = set()
    for field, maximum in (("format_families", 6), ("custom_formats", 64)):
        entries = value.get(field)
        if entries is None:
            continue
        if not isinstance(entries, list) or len(entries) > maximum:
            result.add(field)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                result.add(field)
                continue
            if "name" in entry:
                name = entry["name"]
                try:
                    normalized = normalize_short_text(
                        name, field="name", max_length=len(name)
                    )
                    if not 1 <= len(" ".join(normalized.split())) <= 80:
                        result.add("format_names")
                except (ValueError, TypeError):
                    result.add("format_names")
            extensions = entry.get("extensions")
            if field == "custom_formats" and extensions is not None:
                if not isinstance(extensions, list) or len(extensions) > 16:
                    result.add("extensions")
                elif any(
                    not isinstance(extension, str)
                    or not 1 <= len(extension) <= 33
                    or not extension.isascii()
                    for extension in extensions
                ):
                    result.add("extensions")
    return result


def audit_preserved_inputs(connection) -> list[dict]:
    """Return bounded, content-free findings without requiring today's ORM."""
    present = set(sa.inspect(connection).get_table_names())
    tables = {}
    reports = {}

    def table(name, *required):
        if name not in present:
            return None
        if name not in tables:
            tables[name] = sa.Table(
                name, sa.MetaData(), autoload_with=connection, resolve_fks=False
            )
        result = tables[name]
        return result if all(field in result.c for field in required) else None

    def record(
        rule, identity, *, classification="preserved_history", fingerprint=False
    ):
        if rule not in reports:
            reports[rule] = {
                "rule": rule,
                "classification": classification,
                "count": 0,
                "id_fingerprints" if fingerprint else "ids": [],
            }
        report = reports[rule]
        report["count"] += 1
        identities = report["id_fingerprints" if fingerprint else "ids"]
        if len(identities) < 5:
            identities.append(
                _fingerprint(identity) if fingerprint else _safe_id(identity)
            )

    for name, field, maximum, byte_length in _SCALARS:
        target = table(name, "id", field)
        if target is None:
            continue
        column = target.c[field]
        if byte_length:
            size = (
                sa.func.length(sa.cast(column, sa.LargeBinary))
                if connection.dialect.name == "sqlite"
                else sa.func.octet_length(column)
            )
        else:
            size = sa.func.length(column)
        # Only IDs cross the connection for large body/reason scans.
        for identity in connection.execute(
            sa.select(target.c.id)
            .where(
                sa.or_(size > maximum, sa.func.instr(column, sa.func.char(0)) > 0)
                if connection.dialect.name == "sqlite"
                else size > maximum
            )
            .order_by(target.c.id)
        ).scalars():
            record(f"preserved_{name}_{field}", identity)

    reviews = table("review_item", "id", "review_type", "decision_summary")
    if reviews is not None:
        maximum = sa.case(
            (reviews.c.review_type == "page_inline_comment", 5000), else_=2000
        )
        for identity in connection.execute(
            sa.select(reviews.c.id)
            .where(sa.func.length(reviews.c.decision_summary) > maximum)
            .order_by(reviews.c.id)
        ).scalars():
            record("preserved_review_item_decision_summary", identity)

    for name, field, maximum, item_maximum in _LABELS:
        target = table(name, "id", field)
        if target is None:
            continue
        for identity, raw in connection.execute(
            sa.select(target.c.id, sa.cast(target.c[field], sa.Text)).order_by(
                target.c.id
            )
        ).yield_per(100):
            if raw is None:
                continue
            try:
                invalid = _list_invalid(_json_value(raw), maximum, item_maximum)
            except (ValueError, TypeError, RecursionError):
                invalid = True
            if invalid:
                record(f"preserved_{name}_{field}", identity)

    for name, field in _JSON_HISTORY:
        target = table(name, "id", field)
        if target is None:
            continue
        for identity, raw in connection.execute(
            sa.select(target.c.id, sa.cast(target.c[field], sa.Text)).order_by(
                target.c.id
            )
        ).yield_per(100):
            if raw is None:
                continue
            try:
                value = _json_value(raw)
            except (ValueError, TypeError, RecursionError):
                # Most change-operation diff_ref values are non-JSON references.
                if name != "change_operation":
                    record(f"preserved_{name}_{field}_invalid_json", identity)
                continue
            if name == "agent_page_version" and isinstance(value, dict):
                # Arbitrary frontmatter keys remain arbitrary document data.
                # Only typed title/tag/alias metadata has these input limits.
                value = {
                    key: item
                    for key, item in value.items()
                    if key
                    in {
                        "tags",
                        "aliases",
                    }
                }
            violations = (
                _library_policy_violations(value)
                if name == "workspace"
                else _history_json_violations(value)
            )
            for key in sorted(violations):
                record(f"preserved_{name}_{field}_{key}", identity)

    for name, keys in (
        ("api_token_workspaces", ("token_id",)),
        ("oauth_consents", ("user_id", "client_id")),
    ):
        target = table(name, *keys)
        if target is None:
            continue
        columns = [target.c[key] for key in keys]
        query = sa.select(*columns).group_by(*columns).having(sa.func.count() > 256)
        if "removed_at" in target.c:
            query = query.where(target.c.removed_at.is_(None))
        for row in connection.execute(query.order_by(*columns)):
            identity = row[0] if len(row) == 1 else tuple(row)
            record(
                f"preserved_{name}_workspace_grants",
                identity,
                classification="preserved_grants",
                fingerprint=len(row) > 1,
            )

    clients = table("oauth_clients", "id")
    if clients is not None:
        selected = [clients.c.id]
        json_fields = {"redirect_uris", "grant_types", "response_types"}
        fields = (
            "client_name",
            "redirect_uris",
            "grant_types",
            "response_types",
            "application_type",
            "token_endpoint_auth_method",
            "scope",
        )
        available = [field for field in fields if field in clients.c]
        selected.extend(
            sa.cast(clients.c[field], sa.Text).label(field)
            if field in json_fields
            else clients.c[field]
            for field in available
        )
        for row in connection.execute(
            sa.select(*selected).order_by(clients.c.id)
        ).mappings():
            try:
                validate_identifier(row["id"])
                payload = dict(row)
                for field in json_fields & set(available):
                    payload[field] = _json_value(payload[field])
                if "client_name" in payload:
                    validate_client_name(payload["client_name"])
                if "redirect_uris" in payload:
                    validate_redirect_list(payload["redirect_uris"])
                if "scope" in payload:
                    validate_scope_input(payload["scope"])
                    if set(payload["scope"].split()) - {
                        "mcp:read",
                        "mcp:write",
                        "mcp:agent",
                        "offline_access",
                    }:
                        raise ValueError("unsupported historical scope")
                validate_supported_metadata(payload)
            except (ValueError, TypeError, RecursionError):
                record(
                    "oauth_client_protocol_budget",
                    row["id"],
                    classification="preserved_oauth_metadata",
                    fingerprint=True,
                )
    return [reports[key] for key in sorted(reports)]

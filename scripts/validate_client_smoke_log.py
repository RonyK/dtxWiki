#!/usr/bin/env python3
"""Validate manual client smoke evidence (PAT + dtxWiki-issued OAuth rows)."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_LOG = Path("internal/audit/client-smoke-log.md")
REQUIRED_ROWS = {
    ("Claude Code", "PAT bearer"),
    ("Claude Code", "dtxWiki-issued OAuth"),
    ("Codex CLI", "PAT bearer"),
    ("Codex CLI", "dtxWiki-issued OAuth"),
}
PLACEHOLDER_RE = re.compile(r"<(?!redacted>)[^>]+>")
TOKEN_LIKE_RE = re.compile(
    r"(Bearer\s+(?!<redacted>)[A-Za-z0-9._\-]{16,}|"
    r"\bdtxw_[A-Za-z0-9._\-]+|"
    r"\beyJ[A-Za-z0-9._\-]{20,})"
)
LOCAL_HARNESS_RE = re.compile(
    r"(in-process|generate_phase5_manual_smoke|local server|local harness)",
    re.IGNORECASE,
)
MAX_ELAPSED = timedelta(minutes=30)


@dataclass(frozen=True)
class ClientSmokeRow:
    client: str
    version: str
    auth_path: str
    pat_or_dcr_started_at: str
    registered_at: str
    first_list_tools_at: str
    elapsed_to_list_tools: str
    list_tools: str
    read_page: str
    write_agent_page_draft: str
    cross_workspace_negative: str
    restricted_library_negative: str
    notes: str


def validate_client_smoke_log(path: Path) -> list[str]:
    content = path.read_text(encoding="utf-8")
    errors: list[str] = []
    if PLACEHOLDER_RE.search(content):
        errors.append("placeholder remains")
    if TOKEN_LIKE_RE.search(content):
        errors.append("unredacted token-like material found")
    if LOCAL_HARNESS_RE.search(content):
        errors.append(
            "local/in-process harness evidence is not valid manual "
            "external-client evidence"
        )
    rows = _parse_required_rows(content)
    if not rows:
        return errors + ["required client/auth table not found or empty"]

    seen: set[tuple[str, str]] = set()
    for index, row in enumerate(rows, start=1):
        row_errors = _validate_row(row)
        errors.extend(
            f"row {index} ({row.client} / {row.auth_path}): {error}"
            for error in row_errors
        )
        matched_key = _required_key_for(row)
        if matched_key is not None:
            seen.add(matched_key)
    missing = sorted(REQUIRED_ROWS - seen)
    errors.extend(
        f"missing required row: {client} / {auth}" for client, auth in missing
    )
    return errors


def _parse_required_rows(content: str) -> list[ClientSmokeRow]:
    lines = content.splitlines()
    rows: list[ClientSmokeRow] = []
    in_section = False
    header_seen = False
    for line in lines:
        if line.strip() == "## Required Client/Auth Rows":
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if not in_section or not line.startswith("|"):
            continue
        cells = [_clean_cell(cell) for cell in line.strip().strip("|").split("|")]
        if not cells:
            continue
        if cells[0] == "client":
            header_seen = True
            continue
        if cells[0].startswith("---"):
            continue
        if not header_seen:
            continue
        if len(cells) != 13:
            rows.append(
                ClientSmokeRow(
                    client=cells[0] if cells else "<parse-error>",
                    version="",
                    auth_path="",
                    pat_or_dcr_started_at="",
                    registered_at="",
                    first_list_tools_at="",
                    elapsed_to_list_tools="",
                    list_tools="",
                    read_page="",
                    write_agent_page_draft="",
                    cross_workspace_negative="",
                    restricted_library_negative="",
                    notes=f"parse_error: expected 13 cells, got {len(cells)}",
                )
            )
            continue
        rows.append(ClientSmokeRow(*cells))
    return rows


def _clean_cell(cell: str) -> str:
    value = cell.strip()
    if value.startswith("`") and value.endswith("`"):
        return value[1:-1]
    return value


def _validate_row(row: ClientSmokeRow) -> list[str]:
    errors: list[str] = []
    joined = " | ".join(row.__dict__.values())
    if PLACEHOLDER_RE.search(joined):
        errors.append("placeholder remains")
    if "parse_error:" in row.notes:
        errors.append(row.notes)
        return errors

    started = _parse_timestamp(row.pat_or_dcr_started_at)
    registered = _parse_timestamp(row.registered_at)
    first_tools = _parse_timestamp(row.first_list_tools_at)
    if started is None:
        errors.append("pat_or_dcr_started_at is not an ISO timestamp")
    if registered is None:
        errors.append("registered_at is not an ISO timestamp")
    if first_tools is None:
        errors.append("first_list_tools_at is not an ISO timestamp")
    errors.extend(_kst_errors(started, registered, first_tools))
    if started is not None and registered is not None and registered < started:
        errors.append("registered_at is before pat_or_dcr_started_at")
    if (
        registered is not None
        and first_tools is not None
        and first_tools < registered
    ):
        errors.append("first_list_tools_at is before registered_at")
    if started is not None and first_tools is not None:
        actual_elapsed = first_tools - started
        if actual_elapsed < timedelta():
            errors.append("first_list_tools_at is before pat_or_dcr_started_at")
        if actual_elapsed > MAX_ELAPSED:
            errors.append("elapsed timestamp delta exceeds 30m")
    reported_elapsed = _parse_elapsed(row.elapsed_to_list_tools)
    if reported_elapsed is None:
        errors.append("elapsed_to_list_tools must be mm:ss or HH:MM:SS")
    elif reported_elapsed > MAX_ELAPSED:
        errors.append("elapsed_to_list_tools exceeds 30m")
    elif started is not None and first_tools is not None:
        actual_elapsed = first_tools - started
        if _whole_seconds(reported_elapsed) != _whole_seconds(actual_elapsed):
            errors.append("elapsed_to_list_tools does not match timestamp delta")

    if "OK" not in row.list_tools or "count=" not in row.list_tools:
        errors.append("list_tools must include OK and count=")
    if "OK" not in row.read_page or "slug=" not in row.read_page:
        errors.append("read_page must include OK and slug=")
    if "OK" not in row.write_agent_page_draft:
        errors.append("write_agent_page_draft must include OK")
    if (
        "403" not in row.cross_workspace_negative
        or "workspace" not in row.cross_workspace_negative
    ):
        errors.append("cross-workspace negative must include 403 and workspace")
    if (
        "403" not in row.restricted_library_negative
        or "restricted_library_denied" not in row.restricted_library_negative
    ):
        errors.append(
            "restricted-library negative must include 403 and restricted_library_denied"
        )
    if "external dev pc" not in row.notes.lower():
        errors.append("notes must identify external dev PC evidence")
    if _required_key_for(row) is None:
        errors.append("client/auth path is not one of the required rows")
    return errors


def _required_key_for(row: ClientSmokeRow) -> tuple[str, str] | None:
    for client, auth_prefix in REQUIRED_ROWS:
        if row.client == client and row.auth_path.startswith(auth_prefix):
            return (client, auth_prefix)
    return None


def _parse_timestamp(value: str) -> datetime | None:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _kst_errors(
    started: datetime | None,
    registered: datetime | None,
    first_tools: datetime | None,
) -> list[str]:
    errors: list[str] = []
    for field_name, parsed in (
        ("pat_or_dcr_started_at", started),
        ("registered_at", registered),
        ("first_list_tools_at", first_tools),
    ):
        if parsed is not None and parsed.utcoffset() != timedelta(hours=9):
            errors.append(f"{field_name} must use +09:00 KST offset")
    return errors


def _parse_elapsed(value: str) -> timedelta | None:
    parts = value.strip().split(":")
    if len(parts) not in {2, 3}:
        return None
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return None
    if any(number < 0 for number in numbers):
        return None
    if len(numbers) == 2:
        minutes, seconds = numbers
        hours = 0
    else:
        hours, minutes, seconds = numbers
    if minutes >= 60 or seconds >= 60:
        return None
    return timedelta(hours=hours, minutes=minutes, seconds=seconds)


def _whole_seconds(value: timedelta) -> int:
    return int(value.total_seconds())


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate manual client smoke log evidence.",
    )
    parser.add_argument("path", nargs="?", default=str(DEFAULT_LOG))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    errors = validate_client_smoke_log(Path(args.path))
    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print("client smoke log: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Markdown format lint rules (plan B-T1) — deterministic, syntactic, default-on.

These check the *shape* of a Context page's Markdown body (heading hierarchy,
code-fence balance, table/frontmatter well-formedness, mermaid block sanity,
image refs). They are the cheap, objective layer used both for the Lint screen
and as the auto-approval gate's "lint error" veto (D). Full Mermaid validation
is the frontend renderer's job (it already fails gracefully); the backend does a
best-effort structural check only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from backend.services.lint.base import (
    FixResult,
    LintContext,
    LintFindingDraft,
    LintModule,
    LintStage,
)
from backend.utils.markdown_fence import fence_opener, is_fence_closer
from backend.utils.page_frontmatter import frontmatter_body_offset

_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.*)$")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]*)\)")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_MERMAID_HEADERS = (
    "graph",
    "flowchart",
    "sequencediagram",
    "classdiagram",
    "statediagram",
    "erdiagram",
    "gantt",
    "pie",
    "journey",
    "gitgraph",
    "mindmap",
    "timeline",
    "quadrantchart",
    "requirementdiagram",
    "c4context",
)

_TARGETS = frozenset({"context_page"})


@dataclass
class _Parsed:
    # (level, line, text)
    headings: list[tuple[int, int, str]] = field(default_factory=list)
    unclosed_fence_line: int | None = None
    mermaid_blocks: list[tuple[int, list[str]]] = field(default_factory=list)
    table_blocks: list[tuple[int, list[str]]] = field(default_factory=list)
    images: list[tuple[int, str, str]] = field(default_factory=list)  # (line,alt,url)


def _parse(body: str) -> _Parsed:
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    lines = body.split("\n")
    out = _Parsed()

    # Strip a leading YAML frontmatter block so its content is never re-scanned
    # as body fences/tables/links; `offset` keeps reported line numbers absolute.
    offset = frontmatter_body_offset(lines)

    fence_marker: str | None = None
    fence_len = 0
    fence_open_line = 0
    fence_lang = ""
    fence_buf: list[str] = []
    pipe_run: list[str] = []
    pipe_run_start = 0

    def flush_pipe_run() -> None:
        if len(pipe_run) >= 2:
            out.table_blocks.append((pipe_run_start, pipe_run[:]))
        pipe_run.clear()

    for idx, line in enumerate(lines[offset:], start=offset):
        if fence_marker is not None:
            # M9: a closing fence is a line of ONLY the marker char, at least as
            # long as the opener (CommonMark) — so a 4-backtick fence is not
            # closed by a 3-backtick run inside it.
            is_close = is_fence_closer(
                line,
                marker=fence_marker,
                minimum=fence_len,
            )
            if is_close:
                if fence_lang == "mermaid":
                    out.mermaid_blocks.append((fence_open_line, fence_buf[:]))
                fence_marker = None
                fence_lang = ""
                fence_buf = []
            elif fence_lang == "mermaid":
                fence_buf.append(line)
            continue
        opener = fence_opener(line)
        if opener is not None:
            flush_pipe_run()
            fence_marker, fence_len = opener
            fence_open_line = idx
            fence_lang = line.lstrip(" ")[fence_len:].strip().lower()
            fence_buf = []
            continue
        match = _HEADING_RE.match(line)
        if match:
            flush_pipe_run()
            out.headings.append((len(match.group(1)), idx, match.group(2).strip()))
            continue
        for image in _IMAGE_RE.finditer(line):
            out.images.append((idx, image.group(1), image.group(2).strip()))
        if "|" in line and line.strip():
            if not pipe_run:
                pipe_run_start = idx
            pipe_run.append(line)
        else:
            flush_pipe_run()

    flush_pipe_run()
    if fence_marker is not None:
        out.unclosed_fence_line = fence_open_line
    return out


def _parsed_body(body: str, ctx: LintContext) -> _Parsed:
    key = (__name__, body)
    parsed = ctx.body_analysis.get(key)
    if not isinstance(parsed, _Parsed):
        parsed = _parse(body)
        ctx.body_analysis[key] = parsed
    return parsed


def _finding(
    rule_id: str,
    severity: str,
    target_id: UUID,
    message: str,
    *,
    anchor: str = "",
    message_args: dict[str, Any] | None = None,
) -> LintFindingDraft:
    # fingerprint = rule_id + a line-number-free anchor (M3): stable across
    # unrelated edits so acknowledged/dismissed findings are not resurrected.
    return LintFindingDraft(
        rule_id=rule_id,
        severity=severity,
        target_type="context_page",
        target_id=target_id,
        suggested_action=message,
        message_key=rule_id,
        message_args=message_args or {},
        fingerprint=f"{rule_id}|{anchor}",
    )


def _has_table_separator(block_lines: list[str]) -> bool:
    return any("-" in ln and _TABLE_SEP_RE.match(ln) for ln in block_lines)


_LINE_RE = re.compile(r"\bline (\d+)\b")


def body_relative_message(message: str, body: str) -> str:
    """Restate a finding's line number in the author's coordinates.

    Findings carry ABSOLUTE line numbers on purpose: the Lint screen shows the
    stored body, frontmatter included. The page editor does not -- its buffer
    starts after the frontmatter block, which it edits through a properties
    panel instead. Handing the raw number to the editor points the author at the
    wrong line, which is worse than not pointing at all.
    """
    normalized = body.replace("\r\n", "\n")
    normalized = normalized.replace("\r", "\n")
    offset = frontmatter_body_offset(normalized.split("\n"))
    if offset <= 0:
        return message

    def shift(match: re.Match[str]) -> str:
        return f"line {max(1, int(match.group(1)) - offset)}"

    return _LINE_RE.sub(shift, message)


class _HeadingHierarchyRule:
    rule_id = "md.heading_hierarchy"
    layer = "format"
    default_enabled = True
    applies_to = _TARGETS
    stages = frozenset({LintStage.SAVE, LintStage.READ, LintStage.RUN})

    def check_body(self, body: str, ctx: LintContext) -> list[LintFindingDraft]:
        if not body:
            return []
        parsed = _parsed_body(body, ctx)
        findings: list[LintFindingDraft] = []
        prev: int | None = None
        for level, line, text in parsed.headings:
            if prev is not None and level > prev + 1:
                findings.append(
                    _finding(
                        self.rule_id,
                        "warning",
                        getattr(ctx.page, "id", None),
                        f"Heading level jumps from H{prev} to H{level} at line "
                        f'{line + 1} ("{text[:40]}").',
                        anchor=f"{level}:{text}",
                    )
                )
            prev = level
        return findings


class _CodeFenceBalanceRule:
    rule_id = "md.code_fence_balance"
    layer = "format"
    default_enabled = True
    applies_to = _TARGETS
    stages = frozenset(
        {
            LintStage.SAVE,
            LintStage.READ,
            LintStage.RUN,
            LintStage.PUBLISH_FLOOR,
        }
    )

    def check_body(self, body: str, ctx: LintContext) -> list[LintFindingDraft]:
        if not body:
            return []
        parsed = _parsed_body(body, ctx)
        if parsed.unclosed_fence_line is not None:
            return [
                _finding(
                    self.rule_id,
                    "error",
                    getattr(ctx.page, "id", None),
                    f"Unclosed code fence opened at line "
                    f"{parsed.unclosed_fence_line + 1}.",
                )
            ]
        return []


class _TableWellformedRule:
    rule_id = "md.table_wellformed"
    layer = "format"
    default_enabled = True
    applies_to = _TARGETS
    stages = frozenset({LintStage.SAVE, LintStage.READ, LintStage.RUN})

    def check_body(self, body: str, ctx: LintContext) -> list[LintFindingDraft]:
        if not body:
            return []
        parsed = _parsed_body(body, ctx)
        findings: list[LintFindingDraft] = []
        for start, block in parsed.table_blocks:
            if not _has_table_separator(block):
                findings.append(
                    _finding(
                        self.rule_id,
                        "warning",
                        getattr(ctx.page, "id", None),
                        f"Table-like block at line {start + 1} is missing a header "
                        "separator row (|---|).",
                        anchor=block[0].strip()[:80],
                    )
                )
        return findings


class _ImageRefRule:
    rule_id = "md.image_ref"
    layer = "format"
    default_enabled = True
    applies_to = _TARGETS
    stages = frozenset({LintStage.SAVE, LintStage.READ, LintStage.RUN})

    def check_body(self, body: str, ctx: LintContext) -> list[LintFindingDraft]:
        if not body:
            return []
        parsed = _parsed_body(body, ctx)
        return [
            _finding(
                self.rule_id,
                "warning",
                getattr(ctx.page, "id", None),
                f"Image at line {line + 1} has an empty URL.",
                anchor=alt or f"#{i}",
            )
            for i, (line, alt, url) in enumerate(parsed.images)
            if not url
        ]


class _MermaidBlockRule:
    rule_id = "md.mermaid_block"
    layer = "format"
    default_enabled = True
    applies_to = _TARGETS
    stages = frozenset({LintStage.SAVE, LintStage.READ, LintStage.RUN})

    def check_body(self, body: str, ctx: LintContext) -> list[LintFindingDraft]:
        if not body:
            return []
        parsed = _parsed_body(body, ctx)
        findings: list[LintFindingDraft] = []
        for open_line, content in parsed.mermaid_blocks:
            nonempty = [c.strip() for c in content if c.strip()]
            if not nonempty:
                findings.append(
                    _finding(
                        self.rule_id,
                        "warning",
                        getattr(ctx.page, "id", None),
                        f"Mermaid block at line {open_line + 1} is empty.",
                        anchor="empty",
                    )
                )
                continue
            first = nonempty[0].lower()
            if not any(first.startswith(header) for header in _MERMAID_HEADERS):
                findings.append(
                    _finding(
                        self.rule_id,
                        "warning",
                        getattr(ctx.page, "id", None),
                        f"Mermaid block at line {open_line + 1} has no recognized "
                        "diagram type on its first line (full validation at render).",
                        anchor=f"no-type:{nonempty[0][:60]}",
                    )
                )
        return findings


def safe_format_body(body: str, ctx: LintContext | None = None) -> str:
    """The format module's deterministic, fail-closed fixer."""
    from backend.services.lint.autofix import safe_normalize_markdown

    return safe_normalize_markdown(body)


class _FormatFixer:
    def fix(self, body: str, ctx: LintContext) -> FixResult:
        normalized = safe_format_body(body, ctx)
        return FixResult(normalized, ("format",) if normalized != body else ())


MODULE = LintModule(
    "format",
    900,
    (
        _HeadingHierarchyRule(),
        _CodeFenceBalanceRule(),
        _TableWellformedRule(),
        _ImageRefRule(),
        _MermaidBlockRule(),
    ),
    fixer=_FormatFixer(),
    fix_on_save_setting="auto_fix",
)

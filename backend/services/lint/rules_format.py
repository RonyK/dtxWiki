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
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from backend.models.context import ContextPage, ContextPageVersion
from backend.services.lint.base import LintFindingDraft, LintModule
from backend.utils.markdown_fence import fence_opener, is_fence_closer

_FRONTMATTER_DELIMS = {"---", "..."}

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


def _shared_draft_version(
    session: Session, version_id: UUID | None
) -> ContextPageVersion | None:
    """Resolve ``latest_draft_version_id`` only when it names a SHARED row.

    P8.1: that pointer can durably name another author's still-private
    "editing" row (autosave keeps a row "editing" through any number of
    saves, and `_set_current_version` still advances `latest_draft_version_id`
    to it -- P8.2, which removes that page-global pointer, has not landed
    yet). Lint has no actor context during its background scan, so the only
    safe rule is: never treat "editing" content as a scannable "draft" body
    at all -- not "hide it later from some actors", since `list_findings`'s
    page-level visibility gate does not re-check per-finding provenance and
    a page with any visible head (e.g. published) would otherwise let this
    private content's exact text reach any editor-role actor in the
    workspace (LEAK-04 class -- see `_exclude_draft_only_findings`'s
    docstring for the sibling gate this one is not a substitute for).
    """
    if version_id is None:
        return None
    version = session.get(ContextPageVersion, version_id)
    if version is None or version.version_status == "editing":
        return None
    return version


def context_page_body(
    session: Session,
    page: ContextPage,
    *,
    prefer: str = "draft",
) -> str | None:
    """A single Markdown body for a page that may hold two.

    Kept for callers that want one body. Rules go through
    :func:`page_findings_across_bodies` instead, because a page can be
    malformed in the published head, in the staged draft, or in both, and
    inspecting only one of them makes the other invisible.
    """
    if prefer == "published":
        version_id = page.current_published_version_id
        if version_id is not None:
            version = session.get(ContextPageVersion, version_id)
            return version.body_md if version is not None else None
        draft = _shared_draft_version(session, page.latest_draft_version_id)
        return draft.body_md if draft is not None else None
    draft = _shared_draft_version(session, page.latest_draft_version_id)
    if draft is not None:
        return draft.body_md
    version_id = page.current_published_version_id
    if version_id is None:
        return None
    version = session.get(ContextPageVersion, version_id)
    return version.body_md if version is not None else None


def _page_bodies(session: Session, page: ContextPage) -> list[tuple[str, str]]:
    """(scope, body) for the published head and the staged draft, in that order.

    Published first so it is the representative when the same defect sits in
    both. A page holding only one of the two yields only that one.
    """
    bodies: list[tuple[str, str]] = []
    seen: set = set()
    published_id = page.current_published_version_id
    if published_id is not None:
        seen.add(published_id)
        version = session.get(ContextPageVersion, published_id)
        if version is not None and version.body_md:
            bodies.append(("published", version.body_md))
    draft_id = page.latest_draft_version_id
    if draft_id is not None and draft_id not in seen:
        draft = _shared_draft_version(session, draft_id)
        if draft is not None and draft.body_md:
            bodies.append(("draft", draft.body_md))
    return bodies


def page_findings_across_bodies(rule, *, session, workspace_id, page):
    """Run ``rule`` over both bodies and return one finding per defect.

    Readers see the published head, so a defect there has to stay reported even
    after the author stages a clean draft on top of it. A reviewer decides on
    the staged draft, so a defect there has to be visible while it can still be
    fixed. Inspecting both answers each question without asking the executor to
    merge anything: `run_lint` builds its reconciliation map once, before the
    rule loop, so a rule that yielded the same (target, fingerprint) twice would
    insert two rows. Collapsing here means it never sees a key twice, and the
    fingerprint is unchanged, so stored findings keep their identity and their
    acknowledged/dismissed decisions.
    """
    collapsed: dict[tuple, object] = {}
    for scope, body in _page_bodies(session, page):
        for finding in rule.check(
            session=session, workspace_id=workspace_id, target=page, body=body
        ):
            key = (finding.target_id or page.id, finding.fingerprint)
            existing = collapsed.get(key)
            if existing is None:
                args = dict(finding.message_args or {})
                args["body_scopes"] = [scope]
                collapsed[key] = replace(finding, message_args=args)
                continue
            scopes = list(existing.message_args.get("body_scopes", []))
            if scope not in scopes:
                scopes.append(scope)
                existing.message_args["body_scopes"] = scopes
    return list(collapsed.values())


@dataclass
class _Parsed:
    # (level, line, text)
    headings: list[tuple[int, int, str]] = field(default_factory=list)
    unclosed_fence_line: int | None = None
    mermaid_blocks: list[tuple[int, list[str]]] = field(default_factory=list)
    table_blocks: list[tuple[int, list[str]]] = field(default_factory=list)
    images: list[tuple[int, str, str]] = field(default_factory=list)  # (line,alt,url)


def _frontmatter_body_offset(lines: list[str]) -> int:
    """Index of the first body line after a leading YAML frontmatter block.

    Matches :func:`backend.utils.page_frontmatter.parse_page_frontmatter`
    exactly (incl. the ``len < 3`` minimum): a leading ``---`` is frontmatter
    ONLY when a later ``---``/``...`` closes it. An unclosed leading ``---`` is
    body (a thematic break) — there is no "unclosed frontmatter" error, because
    it can't be told apart from a valid rule + colon line like ``Note: ...``
    (LG-F4). Returns 0 when there is no closed frontmatter block to strip.
    """
    if len(lines) < 3 or lines[0].strip() != "---":
        return 0
    for idx in range(1, len(lines)):
        if lines[idx].strip() in _FRONTMATTER_DELIMS:
            return idx + 1
    return 0


def _parse(body: str) -> _Parsed:
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    lines = body.split("\n")
    out = _Parsed()

    # Strip a leading YAML frontmatter block so its content is never re-scanned
    # as body fences/tables/links; `offset` keeps reported line numbers absolute.
    offset = _frontmatter_body_offset(lines)

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
    offset = _frontmatter_body_offset(normalized.split("\n"))
    if offset <= 0:
        return message

    def shift(match: re.Match[str]) -> str:
        return f"line {max(1, int(match.group(1)) - offset)}"

    return _LINE_RE.sub(shift, message)


def body_error_findings(body: str) -> list[LintFindingDraft]:
    """The auto-approval gate's FIXED objective floor (DEC-2): an unclosed code
    fence — an unambiguous structural malformation with no false positives,
    enforced regardless of the workspace lint toggle.

    Unclosed *frontmatter* is intentionally NOT gated (and has no rule): a leading
    ``---`` without a close is indistinguishable from a valid thematic break +
    colon line (e.g. ``Note: ...``), so flagging it produced false vetoes (LG-F4).
    Cross-page checks (wikilink resolution) are likewise not gated — they have
    forward-reference false positives, so link.unresolved (rules_links) is an
    advisory *warning* (M6). Other format issues are warnings and never gate.
    """
    if not body:
        return []
    parsed = _parse(body)
    findings: list[LintFindingDraft] = []
    if parsed.unclosed_fence_line is not None:
        findings.append(
            _finding(
                "md.code_fence_balance",
                "error",
                None,
                f"Unclosed code fence opened at line {parsed.unclosed_fence_line + 1}.",
            )
        )
    return findings


class _HeadingHierarchyRule:
    rule_id = "md.heading_hierarchy"
    layer = "format"
    default_enabled = True
    applies_to = _TARGETS

    def check(
        self, *, session, workspace_id, target, body: str | None = None
    ) -> list[LintFindingDraft]:
        if body is None:
            return page_findings_across_bodies(
                self, session=session, workspace_id=workspace_id, page=target
            )
        if not body:
            return []
        parsed = _parse(body)
        findings: list[LintFindingDraft] = []
        prev: int | None = None
        for level, line, text in parsed.headings:
            if prev is not None and level > prev + 1:
                findings.append(
                    _finding(
                        self.rule_id,
                        "warning",
                        target.id,
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

    def check(
        self, *, session, workspace_id, target, body: str | None = None
    ) -> list[LintFindingDraft]:
        if body is None:
            return page_findings_across_bodies(
                self, session=session, workspace_id=workspace_id, page=target
            )
        if not body:
            return []
        parsed = _parse(body)
        if parsed.unclosed_fence_line is not None:
            return [
                _finding(
                    self.rule_id,
                    "error",
                    target.id,
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

    def check(
        self, *, session, workspace_id, target, body: str | None = None
    ) -> list[LintFindingDraft]:
        if body is None:
            return page_findings_across_bodies(
                self, session=session, workspace_id=workspace_id, page=target
            )
        if not body:
            return []
        parsed = _parse(body)
        findings: list[LintFindingDraft] = []
        for start, block in parsed.table_blocks:
            if not _has_table_separator(block):
                findings.append(
                    _finding(
                        self.rule_id,
                        "warning",
                        target.id,
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

    def check(
        self, *, session, workspace_id, target, body: str | None = None
    ) -> list[LintFindingDraft]:
        if body is None:
            return page_findings_across_bodies(
                self, session=session, workspace_id=workspace_id, page=target
            )
        if not body:
            return []
        parsed = _parse(body)
        return [
            _finding(
                self.rule_id,
                "warning",
                target.id,
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

    def check(
        self, *, session, workspace_id, target, body: str | None = None
    ) -> list[LintFindingDraft]:
        if body is None:
            return page_findings_across_bodies(
                self, session=session, workspace_id=workspace_id, page=target
            )
        if not body:
            return []
        parsed = _parse(body)
        findings: list[LintFindingDraft] = []
        for open_line, content in parsed.mermaid_blocks:
            nonempty = [c.strip() for c in content if c.strip()]
            if not nonempty:
                findings.append(
                    _finding(
                        self.rule_id,
                        "warning",
                        target.id,
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
                        target.id,
                        f"Mermaid block at line {open_line + 1} has no recognized "
                        "diagram type on its first line (full validation at render).",
                        anchor=f"no-type:{nonempty[0][:60]}",
                    )
                )
        return findings


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
)

import json
import re
from dataclasses import dataclass
from datetime import datetime

from backend.utils.markdown_fence import fence_opener, is_fence_closer

FRONTMATTER_DELIMITERS = {"---", "..."}
PAGE_METADATA_FIELDS = {"title", "slug", "type", "tags", "aliases"}
TAG_MAX_LENGTH = 48
ALIAS_MAX_LENGTH = 80
METADATA_LIST_MAX_ITEMS = 32


def normalize_page_tags(tags: list[str] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for tag in tags or []:
        value = str(tag).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def normalize_page_aliases(aliases: list[str] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for alias in aliases or []:
        value = " ".join(str(alias).strip().split())[:80]
        key = value.casefold()
        if not value or key in seen:
            continue
        seen.add(key)
        normalized.append(value)
        if len(normalized) >= 32:
            break
    return normalized


PAGE_TYPES = {
    "overview",
    "source",
    "entity",
    "concept",
    "synthesis",
    "comparison",
    "decision",
    "contradiction",
    "query",
    "guide",
}

_YAML_RESERVED = {"true", "false", "null", "yes", "no", "on", "off", "~"}
_YAML_UNSAFE_CHARS = set(
    map(chr, (9, 10, 33, 34, 35, 37, 38, 39, 42, 44, 58, 64, 91, 93, 96, 123, 124, 125))
)


@dataclass(frozen=True)
class PageFrontmatter:
    body: str
    has_frontmatter: bool
    page_type: str | None
    tags: list[str] | None
    aliases: list[str] | None
    title: str | None = None


def validate_frontmatter_input(markdown: str) -> None:
    """Validate authored metadata before the forgiving read parser deduplicates.

    Reads keep supporting historical metadata. Only write boundaries call this.
    """
    from backend.domain.input_policy import (
        InputPolicyError,
        validate_labels,
        validate_title,
    )

    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    closing = _frontmatter_closing_index(lines)
    if closing is None:
        return
    header = lines[1:closing]
    for index, line in enumerate(header):
        pair = _frontmatter_key_value(line)
        if pair is None:
            continue
        key, raw = pair
        if key == "title":
            # The reader treats an empty or null title as absent.
            if _parse_scalar(raw):
                validate_title(_parse_scalar(raw))
        elif key in {"tags", "aliases"}:
            values = []
            flow = raw.startswith("[") and raw.endswith("]")
            inner = raw[1:-1].strip() if flow else raw
            if inner and raw != "null":
                try:
                    decoded = json.loads(raw)
                except json.JSONDecodeError:
                    decoded = None
                if isinstance(decoded, list) and any(
                    isinstance(value, (dict, list)) for value in decoded
                ):
                    raise InputPolicyError(f"{key} entries must be scalar values")
                # Share the reader's lexical scalar extraction. str(True) or
                # str(1e3) would change the stored labels to True or 1000.0.
                values = _inline_metadata_values(raw)
                # YAML allows one trailing comma in a flow sequence. Any other
                # empty item is still rejected.
                if flow and inner.endswith(","):
                    values.pop()
            elif not raw:
                for following in header[index + 1 :]:
                    if not following.strip():
                        continue
                    if not following.lstrip().startswith("- "):
                        break
                    values.append(_parse_scalar(following.lstrip()[2:]))
            limit = 48 if key == "tags" else 80
            validate_labels(values, field=key, max_length=limit)


def validate_agent_markdown_input(markdown: str) -> None:
    """Agent titles are explicit metadata, independent of Markdown headings."""
    from backend.domain.input_policy import validate_body

    validate_body(markdown)
    validate_frontmatter_input(markdown)


def validate_page_markdown_input(markdown: str) -> None:
    """Check the complete stored Context Markdown and its derived title."""
    from backend.domain.input_policy import validate_title

    validate_agent_markdown_input(markdown)
    parsed = parse_page_frontmatter(markdown)
    title = title_from_markdown_heading(parsed.body)
    if title is not None:
        validate_title(title)


def title_from_markdown_heading(markdown: str) -> str | None:
    """Return the first prose H1, ignoring CommonMark fenced code blocks."""
    fence = None
    for line in markdown.splitlines():
        if fence is not None:
            if is_fence_closer(line, marker=fence[0], minimum=fence[1]):
                fence = None
            continue
        fence = fence_opener(line)
        if fence is not None:
            continue
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or None
    return None


def render_frontmatter(fields: dict[str, object]) -> str:
    """Render a small YAML frontmatter mapping without a YAML dependency."""
    lines = ["---"]
    for key, value in fields.items():
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, list):
            rendered = ", ".join(_yaml_scalar(str(item)) for item in value)
            lines.append(f"{key}: [{rendered}]")
        elif isinstance(value, datetime):
            lines.append(f"{key}: {value.isoformat()}")
        else:
            lines.append(f"{key}: {_yaml_scalar(str(value))}")
    lines.append("---")
    return "\n".join(lines)


def _yaml_scalar(text: str) -> str:
    needs_quote = (
        text == ""
        or text != text.strip()
        or text[0] in "-?:"
        or text.lower() in _YAML_RESERVED
        or _is_yaml_numeric(text)
        or any(ch in _YAML_UNSAFE_CHARS for ch in text)
    )
    if not needs_quote:
        return text
    return json.dumps(text)


def _is_yaml_numeric(text: str) -> bool:
    try:
        float(text.replace("_", ""))
    except ValueError:
        return False
    return True


def parse_page_frontmatter(markdown: str) -> PageFrontmatter:
    normalized = markdown.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    closing_index = _frontmatter_closing_index(lines)
    if closing_index is None:
        return PageFrontmatter(
            body=markdown,
            has_frontmatter=False,
            page_type=None,
            tags=None,
            aliases=None,
        )

    frontmatter_lines = lines[1:closing_index]
    page_type: str | None = None
    tags: list[str] | None = None
    aliases: list[str] | None = None
    title: str | None = None
    for index, line in enumerate(frontmatter_lines):
        match = _frontmatter_key_value(line)
        if match is None:
            continue
        key, raw_value = match
        if key == "title":
            value = _parse_scalar(raw_value)
            title = value or None
        elif key == "type":
            value = _parse_scalar(raw_value)
            page_type = value if value in PAGE_TYPES else None
        elif key == "tags":
            if raw_value.strip():
                tags = _parse_inline_tags(raw_value)
            else:
                tags = _parse_block_tags(frontmatter_lines[index + 1 :])
        elif key == "aliases":
            if raw_value.strip():
                aliases = _parse_inline_aliases(raw_value)
            else:
                aliases = _parse_block_aliases(frontmatter_lines[index + 1 :])

    body = "\n".join(lines[closing_index + 1 :])
    if body.startswith("\n"):
        body = body[1:]
    return PageFrontmatter(
        body=body,
        has_frontmatter=True,
        page_type=page_type,
        tags=tags,
        aliases=aliases,
        title=title,
    )


def upsert_page_frontmatter(
    markdown: str,
    *,
    title: str,
    slug: str,
    page_type: str,
    tags: list[str],
    aliases: list[str] | None = None,
) -> str:
    normalized = markdown.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    closing_index = _frontmatter_closing_index(lines)
    if closing_index is None:
        extra_lines: list[str] = []
        body = normalized.lstrip("\n")
    else:
        frontmatter_lines = lines[1:closing_index]
        managed_indexes = _managed_frontmatter_indexes(frontmatter_lines)
        extra_lines = [
            line
            for index, line in enumerate(frontmatter_lines)
            if index not in managed_indexes
        ]
        body = "\n".join(lines[closing_index + 1 :]).lstrip("\n")

    aliases_yaml = ", ".join(_quote_yaml_string(alias) for alias in (aliases or []))
    frontmatter = [
        "---",
        f"title: {_quote_yaml_string(title.strip() or 'Untitled')}",
        f"slug: {slug}",
        f"type: {page_type}",
        f"tags: [{', '.join(_quote_yaml_string(tag) for tag in tags)}]",
        f"aliases: [{aliases_yaml}]",
        *extra_lines,
        "---",
        "",
    ]
    return "\n".join(frontmatter) + body


def _frontmatter_closing_index(lines: list[str]) -> int | None:
    if len(lines) < 3 or lines[0].strip() != "---":
        return None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() in FRONTMATTER_DELIMITERS:
            return index
    return None


def _frontmatter_key_value(line: str) -> tuple[str, str] | None:
    if line.startswith((" ", "\t")) or ":" not in line:
        return None
    key, raw_value = line.split(":", maxsplit=1)
    key = key.strip()
    if not key.replace("_", "").replace("-", "").isalnum():
        return None
    return key, raw_value.strip()


def _managed_frontmatter_indexes(lines: list[str]) -> set[int]:
    indexes: set[int] = set()
    for index, line in enumerate(lines):
        match = _frontmatter_key_value(line)
        if match is None:
            continue
        key, _raw_value = match
        if key not in PAGE_METADATA_FIELDS:
            continue
        indexes.add(index)
        # All managed keys may carry an indented block payload (block-style
        # list, multi-line scalar, etc.). Sweep the indented continuation
        # so malformed values do not accumulate into extra_lines on round
        # trip. (Previously only `tags:` did this.)
        indexes.update(_indented_block_indexes(lines, index + 1))
    return indexes


def _indented_block_indexes(lines: list[str], start_index: int) -> set[int]:
    indexes: set[int] = set()
    for index in range(start_index, len(lines)):
        line = lines[index]
        if (
            not line.strip()
            or line.startswith((" ", "\t"))
            or line.lstrip().startswith("- ")
        ):
            indexes.add(index)
            continue
        break
    return indexes


def _parse_inline_tags(value: str) -> list[str]:
    trimmed = value.strip()
    if not trimmed or trimmed == "[]":
        return []
    return _dedupe_tags(_inline_metadata_values(trimmed))


def _parse_block_tags(lines: list[str]) -> list[str]:
    values: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        stripped = line.lstrip()
        if not stripped.startswith("- "):
            break
        values.append(_parse_scalar(stripped[2:]))
    return _dedupe_tags(values)


def _parse_inline_aliases(value: str) -> list[str]:
    trimmed = value.strip()
    if not trimmed or trimmed == "[]":
        return []
    return _dedupe_aliases(_inline_metadata_values(trimmed))


def _inline_metadata_values(value: str) -> list[str]:
    """Split flow-list commas without splitting quoted aliases or escapes."""
    raw = value.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    values: list[str] = []
    token: list[str] = []
    quote: str | None = None
    escaped = False
    for character in raw:
        if quote is not None:
            token.append(character)
            if escaped:
                escaped = False
            elif character == "\\" and quote == '"':
                escaped = True
            elif character == quote:
                quote = None
        elif character == ",":
            values.append(_parse_scalar("".join(token)))
            token = []
        else:
            if character in {"'", '"'} and not "".join(token).strip():
                quote = character
            token.append(character)
    values.append(_parse_scalar("".join(token)))
    return values


def _parse_block_aliases(lines: list[str]) -> list[str]:
    values: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        stripped = line.lstrip()
        if not stripped.startswith("- "):
            break
        values.append(_parse_scalar(stripped[2:]))
    return _dedupe_aliases(values)


def _parse_scalar(value: str) -> str:
    trimmed = value.strip()
    if not trimmed or trimmed == "null":
        return ""
    if trimmed.startswith('"') and trimmed.endswith('"'):
        # Quoted values are written by _quote_yaml_string as JSON strings.
        # Decode that same subset so Unicode and escaped quote/backslash
        # values round-trip instead of being exposed as literal escapes.
        try:
            parsed = json.loads(trimmed)
        except json.JSONDecodeError:
            # Keep hand-authored malformed frontmatter as forgiving as the
            # original quote-stripping parser.
            return trimmed[1:-1]
        if isinstance(parsed, str):
            return parsed
        return trimmed[1:-1]
    if trimmed.startswith("'") and trimmed.endswith("'"):
        return trimmed[1:-1]
    return trimmed


def _dedupe_tags(tags) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        value = str(tag).strip().lstrip("#")[:TAG_MAX_LENGTH]
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
        if len(normalized) >= METADATA_LIST_MAX_ITEMS:
            break
    return normalized


def _dedupe_aliases(aliases) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        value = " ".join(str(alias).strip().split())[:ALIAS_MAX_LENGTH]
        key = value.casefold()
        if not value or key in seen:
            continue
        seen.add(key)
        normalized.append(value)
        if len(normalized) >= METADATA_LIST_MAX_ITEMS:
            break
    return normalized


def _quote_yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def derive_nav_description(
    markdown: str | None, *, max_length: int = 200
) -> str | None:
    """First prose paragraph of the frontmatter-stripped body (nav summary).

    Frontmatter-aware (unlike a naive ``\n\n`` splitter): an editor-saved
    body whose closing ``---`` has no trailing blank line must still yield the
    FIRST paragraph, and a single-paragraph page must not come back empty.
    Line-oriented so ``# Heading\nProse`` (no blank line) still yields the
    prose, fenced code never bleeds into the description, and CRLF bodies
    split like LF ones. Returns None when no prose exists.
    """
    if not markdown:
        return None
    body = parse_page_frontmatter(markdown).body
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    prose: list[str] = []
    in_fence = False
    skipped_paragraph = False
    for line in body.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not stripped:
            if prose:
                break  # first prose paragraph complete
            skipped_paragraph = False
            continue
        # Lists and metadata blocks describe the document's administration,
        # rather than its subject. Skip their continuation lines as well.
        structural = stripped.startswith(("#", "---", "|", ">"))
        metadata = re.match(
            r"^(?:[-+*]\s+|\d+[.)]\s+|"
            r"(?:\*\*|__)?(?:status|state|date|created|updated|author|owner|"
            r"title|type|tags|aliases|version|document|source|"
            r"상태|문서\s*상태|날짜|작성일|갱신일|작성자|담당자|소유자|유형|버전|출처)"
            r"(?:\*\*|__)?\s*[:：])",
            stripped,
            re.IGNORECASE,
        )
        if structural or metadata:
            if prose:
                break  # a structural line ends the paragraph
            skipped_paragraph = bool(metadata)
            continue
        if skipped_paragraph:
            continue
        prose.append(stripped)
    if not prose:
        return None
    collapsed = " ".join(" ".join(prose).split())
    if len(collapsed) > max_length:
        return collapsed[: max_length - 1].rstrip() + "…"
    return collapsed


def frontmatter_body_offset(lines: list[str]) -> int:
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
        if lines[idx].strip() in FRONTMATTER_DELIMITERS:
            return idx + 1
    return 0

import json
from dataclasses import dataclass
from datetime import datetime

FRONTMATTER_DELIMITERS = {"---", "..."}
PAGE_METADATA_FIELDS = {"title", "slug", "type", "tags", "aliases"}
TAG_MAX_LENGTH = 48
ALIAS_MAX_LENGTH = 80
METADATA_LIST_MAX_ITEMS = 32
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
    from backend.domain.input_policy import validate_labels, validate_title

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
            validate_title(_parse_scalar(raw))
        elif key in {"tags", "aliases"}:
            values = []
            if raw and raw != "[]":
                try:
                    decoded = json.loads(raw)
                except json.JSONDecodeError:
                    decoded = None
                values = (
                    decoded
                    if isinstance(decoded, list)
                    else _inline_metadata_values(raw)
                )
            elif not raw:
                for following in header[index + 1 :]:
                    if not following.strip():
                        continue
                    if not following.lstrip().startswith("- "):
                        break
                    values.append(_parse_scalar(following.lstrip()[2:]))
            validate_labels(values, field=key, max_length=48 if key == "tags" else 80)


def validate_page_markdown_input(markdown: str) -> None:
    """Check the complete stored Markdown when creating or resubmitting it."""
    from backend.domain.input_policy import validate_body, validate_title

    validate_body(markdown)
    validate_frontmatter_input(markdown)
    parsed = parse_page_frontmatter(markdown)
    for line in parsed.body.splitlines():
        if line.strip().startswith("# "):
            validate_title(line.strip()[2:].strip())
            break


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
            continue
        if stripped.startswith(("#", "---", "|", ">")):
            if prose:
                break  # a structural line ends the paragraph
            continue
        prose.append(stripped)
    if not prose:
        return None
    collapsed = " ".join(" ".join(prose).split())
    if len(collapsed) > max_length:
        return collapsed[: max_length - 1].rstrip() + "…"
    return collapsed

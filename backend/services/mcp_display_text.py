"""Display-safe text primitives shared by MCP trace capture and Chats reads.

Trace capture (``mcp_trace_redaction``) and the Chats projection
(``mcp_chat_service``) both turn client-authored text into something a screen
may show. They must agree on what "safe" means, so the rules live here rather
than in either consumer.
"""

from __future__ import annotations

import re
import unicodedata

from backend.utils.graphemes import grapheme_clusters as grapheme_clusters

_URL_TAIL_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s?#]+)(?:[?#][^\s]*)")
_SENSITIVE_PARAMETER_RE = re.compile(
    r"(?i)(?:^|[\s?&#;])"
    r"(?:authorization[_-]?code|code|access[_-]?token|refresh[_-]?token|token|"
    r"redirect[_-]?(?:uri|url))\s*=\s*[^\s&#;]*"
)
_SENSITIVE_LABEL_RE = re.compile(
    r"(?i)\b(?:authorization[ _-]?code|access[ _-]?token|refresh[ _-]?token|"
    r"redirect[ _-]?(?:uri|url))\s*[:=]\s*\S+"
)
SECRET_MASK = "[redacted]"
# BEST-EFFORT SHAPE MASKING. These patterns recognise the credential shapes
# they name and nothing else: the evidence redactor's list
# (scripts/redact_evidence.sh), this codebase's own prefixes
# (backend/services/credential_hashing.py), PEM bodies, common provider token
# formats, URL userinfo, and values assigned to an identifier with a
# secret-naming segment. A credential in any other shape passes through, so
# this is never a substitute for keeping a field out of storage -- the field
# allowlist in mcp_trace_redaction is the actual privacy boundary. Provider
# shapes are anchored on their fixed prefix plus a minimum body length, so
# ordinary words ("skill-set", "task_id", "tokenizer") do not match.
_SECRET_NAME_SEGMENT = (
    r"(?:secret|token|api[_-]?key|access[_-]?key|password|passwd|"
    r"private[_-]?key|credentials?)"
)
_SECRET_TOKEN_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"
            r".*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
            re.DOTALL,
        ),
        SECRET_MASK,
    ),
    # scheme://user:password@host keeps the scheme and the host.
    (
        re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@"),
        r"\1" + SECRET_MASK + "@",
    ),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]{8,}"), SECRET_MASK),
    (re.compile(r"\b(?:dtxw|dtxr|wpg)_[A-Za-z0-9._\-]+"), SECRET_MASK),
    (re.compile(r"\beyJ[A-Za-z0-9._\-]{20,}"), SECRET_MASK),
    # OpenAI / Anthropic: sk-, sk-proj-, sk-ant-, sk-svcacct-, sk-admin-.
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"), SECRET_MASK),
    # GitHub classic/OAuth/user/server/refresh tokens and fine-grained PATs.
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), SECRET_MASK),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), SECRET_MASK),
    (re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}"), SECRET_MASK),
    # AWS access key ids.
    (re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[A-Z0-9]{16}\b"), SECRET_MASK),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}"), SECRET_MASK),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}"), SECRET_MASK),
    (
        re.compile(r"(?i)\bhttps?://hooks\.slack\.com/services/[A-Za-z0-9/_\-]+"),
        SECRET_MASK,
    ),
    (re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{10,}"), SECRET_MASK),
    (re.compile(r"\bhf_[A-Za-z0-9]{30,}"), SECRET_MASK),
    (re.compile(r"\bnpm_[A-Za-z0-9]{30,}"), SECRET_MASK),
    (
        re.compile(r"\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}"),
        SECRET_MASK,
    ),
    (
        re.compile(r"(?i)\b(?:authorization|cookie|set-cookie)\s*:\s*\S+"),
        SECRET_MASK,
    ),
    # The value assigned to an identifier with a secret-naming segment:
    # aws_secret_access_key =, GITHUB_TOKEN=, db.password:, x-api-key:. The
    # segment must be whole, so "tokenizer = BPE" is left alone.
    (
        re.compile(
            r"(?i)(\b(?:[A-Za-z0-9]+[._-])*"
            + _SECRET_NAME_SEGMENT
            + r"(?:[._-][A-Za-z0-9]+)*\s*[:=]\s*)\S+"
        ),
        r"\1" + SECRET_MASK,
    ),
)

_BIDI_CONTROL_CHARS = frozenset(
    {
        "\u061c",
        "\u200e",
        "\u200f",
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",
    }
)
_PRESERVED_FORMAT_CHARS = frozenset({"\u200c", "\u200d"})


def normalize_display_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value)
    output: list[str] = []
    pending_space = False
    for char in normalized:
        category = unicodedata.category(char)
        if char in _BIDI_CONTROL_CHARS:
            continue
        if char.isspace():
            pending_space = bool(output)
            continue
        if category in {"Cc", "Cs"} or (
            category == "Cf" and char not in _PRESERVED_FORMAT_CHARS
        ):
            continue
        if pending_space:
            output.append(" ")
            pending_space = False
        output.append(char)
    result = "".join(output).strip()
    return result or None


def sanitize_display_text(value: object) -> str | None:
    """Drop URL tails and OAuth-looking parameters from display candidates."""

    normalized = normalize_display_text(value)
    if normalized is None:
        return None
    without_url_tail = _URL_TAIL_RE.sub(r"\1", normalized)
    without_parameters = _SENSITIVE_PARAMETER_RE.sub(" ", without_url_tail)
    without_labels = _SENSITIVE_LABEL_RE.sub(" ", without_parameters)
    return normalize_display_text(without_labels)


def sanitize_display_lines(value: str) -> str:
    """``sanitize_display_text`` for multi-line text: keeps the line breaks.

    CRLF/CR become LF. The URL-tail and OAuth patterns run over the whole text
    first, so a value on the line after its label is still removed; a removal
    that swallowed line breaks gives them back rather than joining lines. Each
    line is then normalized on its own.
    """

    text = value.replace("\r\n", "\n").replace("\r", "\n")

    def removed(match: re.Match[str]) -> str:
        return "\n" * match.group(0).count("\n") or " "

    text = _URL_TAIL_RE.sub(r"\1", text)
    text = _SENSITIVE_PARAMETER_RE.sub(removed, text)
    text = _SENSITIVE_LABEL_RE.sub(removed, text)
    lines = [sanitize_display_text(line) or "" for line in text.split("\n")]
    return "\n".join(lines).strip("\n")


def mask_secret_tokens(value: str) -> str:
    """Replace credential-shaped substrings with ``SECRET_MASK``.

    Best-effort shape masking only; see ``_SECRET_TOKEN_PATTERNS``.
    """

    for pattern, replacement in _SECRET_TOKEN_PATTERNS:
        value = pattern.sub(replacement, value)
    return value


def truncate_graphemes(value: str, max_graphemes: int) -> str:
    clusters = grapheme_clusters(value)
    if len(clusters) <= max_graphemes:
        return value
    if max_graphemes <= 1:
        return "…"
    return "".join(clusters[: max_graphemes - 1]).rstrip() + "…"

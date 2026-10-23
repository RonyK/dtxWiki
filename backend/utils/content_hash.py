import unicodedata
from hashlib import sha256


def content_hash(markdown: str) -> str:
    canonical = canonicalize_markdown(markdown)
    return sha256(canonical.encode("utf-8")).hexdigest()


def same_authored_markdown(left: str, right: str) -> bool:
    """Retain hard breaks while preserving NFC, newline, and EOF equivalence.

    The public content hash strips every line's trailing spaces. Persistence
    and merge decisions also need to distinguish Markdown hard breaks, so
    they use this narrower equivalence without changing the hash contract.
    """

    def normalized(markdown: str) -> str:
        return (
            unicodedata.normalize("NFC", markdown)
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .rstrip("\n")
        )

    return normalized(left) == normalized(right)


def canonicalize_markdown(markdown: str) -> str:
    # Used to also strip a leading `---` block's `version_no`/
    # `last_modified_at` lines, on the theory that the server would someday
    # stamp those into visible frontmatter and that churn should not move
    # the hash. No write path has ever done that (grepped exhaustively
    # across MCP/API/ingress/library/legacy-page writers, and every commit
    # in this file's history), so the exclusion defended against a shape
    # the system never produces while opening a real lost-update hole: any
    # `---`-fenced block is caller-supplied, free-form content (a page
    # editor's body_markdown accepts one verbatim; imported Jekyll/Hugo/
    # Obsidian markdown conventionally opens with one and often carries a
    # version field), so two bodies differing ONLY in such a line hashed
    # identically -- silently defeating every CAS precondition built on
    # this hash (patch_draft's expected_content_hash, update_draft's
    # base_content_hash, replace_document's expected_content_hash).
    normalized = unicodedata.normalize("NFC", markdown)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(line.rstrip(" \t") for line in normalized.split("\n"))
    return _strip_trailing_blank_lines(normalized)


def _strip_trailing_blank_lines(markdown: str) -> str:
    lines = markdown.split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)

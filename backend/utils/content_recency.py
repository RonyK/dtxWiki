import unicodedata


def normalize_content_recency(markdown: str) -> str:
    """Normalize transport spelling without erasing visible Markdown changes.

    Spaces and tabs on individual lines are meaningful: two trailing spaces
    introduce a hard break, and code blocks preserve their whitespace.
    """
    normalized = unicodedata.normalize("NFC", markdown)
    return normalized.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")

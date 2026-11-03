"""Read-only location checks share the renderer's structural Markdown parser."""

from backend.utils.markdown_anchors import markdown_fragment_statuses


def test_heading_fragments_keep_duplicate_ids_and_ignore_fenced_headings():
    # Budget exception: heading resolution uses the renderer's structural AST;
    # citation-token parsing cannot prove duplicate IDs or fenced-heading
    # exclusion. Keep this independent mechanism in a pure batched check.
    # One batched location request distinguishes a real duplicated heading
    # from a code sample without changing the document-level reference.
    body = "# Title\n\n## **Section**\n\n## Section\n\n```md\n## Fenced\n```\n"
    assert markdown_fragment_statuses(body, {"section", "section-1", "fenced"}) == {
        "section": True,
        "section-1": True,
        "fenced": False,
    }
    # Footnote IDs and footnote-bearing heading slugs depend on the reader's
    # generated markup. This additional parser input covers that independent
    # token path while keeping provable headings available in the same batch.
    assert markdown_fragment_statuses(
        "# Title\n\n## Section[^1]\n\n[^1]: Footnote text.",
        {"title", "section", "user-content-fn-1", "user-content-fnref-1"},
    ) == {
        "title": True,
        "section": None,
        "user-content-fn-1": None,
        "user-content-fnref-1": None,
    }

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

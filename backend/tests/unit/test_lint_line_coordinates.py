
"""The line number an author is shown must be the line they are looking at.

Findings carry ABSOLUTE line numbers on purpose -- the Lint screen renders the
stored body, frontmatter included. The page editor does not: it edits
frontmatter through a properties panel and its markdown buffer starts after the
block. Handing the raw number straight to the editor points at the wrong line,
which is worse than not pointing at all.
"""

from backend.services.lint import publish_floor_findings
from backend.services.lint.rules_format import (
    body_relative_message,
)

FENCE = "```"


def test_a_frontmatter_block_shifts_the_reported_line():
    body = "\n".join(
        [
            "---",
            "title: t",
            "---",
            "",
            "# Title",
            "",
            "text",
            "",
            FENCE + "python",
            "x = 1",
            "",
        ]
    )
    finding = publish_floor_findings(body)[0]
    # Absolute: the fence is the 9th line of the stored body.
    assert "line 9" in finding.suggested_action
    # The editor's buffer starts after the three frontmatter lines.
    assert "line 6" in body_relative_message(finding.suggested_action, body)


def test_a_body_without_frontmatter_is_left_alone():
    body = "\n".join(["# Title", "", FENCE + "python", "x = 1", ""])
    finding = publish_floor_findings(body)[0]
    assert "line 3" in finding.suggested_action
    assert body_relative_message(finding.suggested_action, body) == (
        finding.suggested_action
    )


def test_an_unclosed_leading_rule_is_not_frontmatter():
    """A leading ``---`` with no closer is a thematic break, not frontmatter, so
    nothing is stripped and nothing shifts."""
    body = "\n".join(["---", "Note: something", "", FENCE + "python", "x = 1", ""])
    finding = publish_floor_findings(body)[0]
    assert body_relative_message(finding.suggested_action, body) == (
        finding.suggested_action
    )

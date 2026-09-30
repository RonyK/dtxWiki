"""Tests for backend.utils.page_frontmatter.

Parser parity with the frontend `pageFrontmatter.ts` is a high-risk design
property: both implementations are intentionally a strict YAML subset and
must agree on which keys round-trip through. These tests cover the cases
where the two parsers historically diverged or where malformed input could
accumulate noise in `extra_lines` on every save.
"""

from backend.utils.page_frontmatter import (
    parse_page_frontmatter,
    upsert_page_frontmatter,
)


class TestParse:
    def test_pages_without_frontmatter_pass_through_unchanged(self) -> None:
        body = "# Heading\n\nFirst paragraph.\n"

        result = parse_page_frontmatter(body)

        assert result.has_frontmatter is False
        assert result.body == body
        assert result.page_type is None
        assert result.tags is None

    def test_thematic_break_at_start_is_not_treated_as_frontmatter(self) -> None:
        # `---` followed by a heading, no closing `---`, must remain in body.
        body = "---\n# Real heading\n\nBody."

        result = parse_page_frontmatter(body)

        assert result.has_frontmatter is False
        assert result.body == body

    def test_inline_tags_parsed(self) -> None:
        body = (
            "---\n"
            "title: T\n"
            "slug: t\n"
            "type: decision\n"
            "tags: [alpha, beta]\n"
            'aliases: ["Launch Plan", "Release Brief"]\n'
            "---\n"
            "Body."
        )

        result = parse_page_frontmatter(body)

        assert result.has_frontmatter is True
        assert result.page_type == "decision"
        assert result.tags == ["alpha", "beta"]
        assert result.aliases == ["Launch Plan", "Release Brief"]
        assert result.body == "Body."

    def test_block_lists_parsed(self) -> None:
        body = (
            "---\n"
            "title: T\n"
            "slug: t\n"
            "type: overview\n"
            "tags:\n"
            "  - alpha\n"
            "  - beta\n"
            "aliases:\n"
            "  - Launch Plan\n"
            "  - release brief\n"
            "---\n"
            "Body."
        )

        result = parse_page_frontmatter(body)

        assert result.tags == ["alpha", "beta"]
        assert result.aliases == ["Launch Plan", "release brief"]
        assert result.body == "Body."

    def test_unknown_page_type_collapses_to_none(self) -> None:
        body = (
            "---\n"
            "title: T\n"
            "slug: t\n"
            "type: not-a-valid-type\n"
            "---\n"
        )

        result = parse_page_frontmatter(body)

        assert result.page_type is None


class TestUpsertManagedKeySweep:
    """Regression guard for the C2.5 fix.

    Before the fix, only `tags:` consumed indented continuation lines; if any
    other managed key (`title`, `slug`, `type`) was followed by an indented
    payload the orphan lines were re-emitted into `extra_lines` and accumulated
    on every save. After the fix, every managed key with an empty `raw_value`
    sweeps its indented continuation.
    """

    def test_malformed_block_payload_under_type_is_not_preserved_as_extra(
        self,
    ) -> None:
        body = (
            "---\n"
            "title: T\n"
            "slug: t\n"
            "type:\n"
            "  - bad\n"
            "  - leftover\n"
            "---\n"
            "Body."
        )

        result = upsert_page_frontmatter(
            body,
            title="T",
            slug="t",
            page_type="overview",
            tags=[],
        )

        # The orphan `- bad`/`- leftover` lines must NOT appear in the
        # rewritten frontmatter.
        assert "- bad" not in result
        assert "- leftover" not in result

    def test_block_scalar_payload_under_managed_key_is_not_preserved_as_extra(
        self,
    ) -> None:
        body = (
            "---\n"
            "title: |\n"
            "  stale title\n"
            "  payload\n"
            "slug: t\n"
            "type: overview\n"
            "---\n"
            "Body."
        )

        result = upsert_page_frontmatter(
            body,
            title="Fresh title",
            slug="t",
            page_type="overview",
            tags=[],
        )

        assert "stale title" not in result
        assert "payload" not in result
        assert 'title: "Fresh title"' in result

    def test_block_tags_payload_is_swept(self) -> None:
        body = (
            "---\n"
            "title: T\n"
            "slug: t\n"
            "type: overview\n"
            "tags:\n"
            "  - old\n"
            "  - stale\n"
            "---\n"
            "Body."
        )

        result = upsert_page_frontmatter(
            body,
            title="T",
            slug="t",
            page_type="overview",
            tags=["fresh"],
        )

        assert "- old" not in result
        assert "- stale" not in result
        assert 'tags: ["fresh"]' in result

    def test_block_aliases_payload_is_swept(self) -> None:
        body = (
            "---\n"
            "title: T\n"
            "slug: t\n"
            "type: overview\n"
            "aliases:\n"
            "  - Old name\n"
            "  - Stale name\n"
            "---\n"
            "Body."
        )

        result = upsert_page_frontmatter(
            body,
            title="T",
            slug="t",
            page_type="overview",
            tags=[],
            aliases=["Fresh name"],
        )

        assert "- Old name" not in result
        assert "- Stale name" not in result
        assert 'aliases: ["Fresh name"]' in result

    def test_unknown_keys_preserved_as_extra_lines(self) -> None:
        body = (
            "---\n"
            "title: T\n"
            "slug: t\n"
            "type: overview\n"
            "icon: book\n"
            "cover: null\n"
            "---\n"
            "Body."
        )

        result = upsert_page_frontmatter(
            body,
            title="T",
            slug="t",
            page_type="overview",
            tags=[],
        )

        # Unknown keys (icon, cover) must be preserved verbatim.
        assert "icon: book" in result
        assert "cover: null" in result

    def test_round_trip_managed_metadata(self) -> None:
        original = upsert_page_frontmatter(
            "Body.\n",
            title="Title One",
            slug="title-one",
            page_type="decision",
            tags=["alpha", "beta"],
            aliases=["Launch Plan"],
        )

        parsed = parse_page_frontmatter(original)

        assert parsed.has_frontmatter is True
        assert parsed.page_type == "decision"
        assert parsed.tags == ["alpha", "beta"]
        assert parsed.aliases == ["Launch Plan"]

        re_serialized = upsert_page_frontmatter(
            original,
            title="Title One",
            slug="title-one",
            page_type="decision",
            tags=["alpha", "beta"],
            aliases=["Launch Plan"],
        )

        # parse(serialize(parse(md))) === parse(md)
        re_parsed = parse_page_frontmatter(re_serialized)
        assert re_parsed.page_type == parsed.page_type
        assert re_parsed.tags == parsed.tags
        assert re_parsed.aliases == parsed.aliases
        assert re_parsed.body == parsed.body


class TestScalarRoundTrip:
    def test_upsert_round_trips_unicode_quotes_and_backslashes(self) -> None:
        title = '한국어 "제목"'
        tags = ["한글태그", "경로\\이름", '인용 "태그"']
        aliases = ['경로 "C:\\docs"']

        markdown = upsert_page_frontmatter(
            "본문.",
            title=title,
            slug="unicode-metadata",
            page_type="overview",
            tags=tags,
            aliases=aliases,
        )
        parsed = parse_page_frontmatter(markdown)

        assert 'tags: ["한글태그", "경로\\\\이름", "인용 \\"태그\\""]' in markdown
        assert "\\ud55c" not in markdown
        assert parsed.title == title
        assert parsed.tags == tags
        assert parsed.aliases == aliases

    def test_parses_legacy_json_escaped_unicode_scalars(self) -> None:
        markdown = "\n".join(
            [
                "---",
                r'tags: ["\ud55c\uae00\ud0dc\uadf8"]',
                r'aliases: ["\uacbd\ub85c\\docs"]',
                "---",
                "",
                "Body.",
            ]
        )

        parsed = parse_page_frontmatter(markdown)

        assert parsed.tags == ["한글태그"]
        assert parsed.aliases == ["경로\\docs"]

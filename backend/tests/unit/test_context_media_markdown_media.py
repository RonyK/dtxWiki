from __future__ import annotations

from uuid import UUID

import pytest

import backend.context_media.domain.markdown_media as markdown_media_module
from backend.context_media.domain.markdown_media import (
    LEGACY_UPLOAD_REFERENCE_SCAN_ERROR_CODES,
    LegacyMarkdownMediaError,
    browser_reachable_legacy_upload_identities,
    count_legacy_upload_reference_uses,
    locate_legacy_markdown_media,
    replace_legacy_upload_references,
    rewrite_legacy_markdown_media,
    scan_legacy_upload_references,
    source_mapped_local_legacy_upload_references,
)

WORKSPACE_ID = UUID("11111111-1111-1111-1111-111111111111")
DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64
LEGACY_URL = f"/api/v1/uploads/{WORKSPACE_ID}/{DIGEST}.png"
OTHER_LEGACY_URL = f"/api/v1/uploads/{WORKSPACE_ID}/{OTHER_DIGEST}.webp"
LOGICAL_ONE = "dtx-media://asset/22222222-2222-2222-2222-222222222222"
LOGICAL_TWO = "dtx-media://asset/33333333-3333-3333-3333-333333333333"
FOREIGN_WORKSPACE_ID = UUID("99999999-9999-9999-9999-999999999999")


def test_locates_inline_destination_and_preserves_angle_brackets_and_title() -> None:
    markdown = (
        f'\uc55e\ub9d0 ![nested [alt\\] text]](<{LEGACY_URL}> "a (title)") \ub4b7\ub9d0'
    )

    occurrences = locate_legacy_markdown_media(markdown)

    assert len(occurrences) == 1
    occurrence = occurrences[0]
    assert occurrence.ordinal == 0
    assert occurrence.kind == "inline_image"
    assert occurrence.destination == LEGACY_URL
    assert occurrence.destination_span.text(markdown) == LEGACY_URL
    assert occurrence.syntax_span.text(markdown) == (
        f'![nested [alt\\] text]](<{LEGACY_URL}> "a (title)")'
    )
    assert occurrence.usage_spans == (occurrence.syntax_span,)
    assert occurrence.identity.workspace_id == WORKSPACE_ID
    assert occurrence.identity.filename == f"{DIGEST}.png"
    assert occurrence.identity.sha256 == DIGEST
    assert occurrence.identity.extension == "png"
    assert (
        occurrence.destination_span.byte_start > occurrence.destination_span.char_start
    )

    rewritten = rewrite_legacy_markdown_media(markdown, {occurrence: LOGICAL_ONE})

    assert rewritten == (
        f'\uc55e\ub9d0 ![nested [alt\\] text]](<{LOGICAL_ONE}> "a (title)") '
        "\ub4b7\ub9d0"
    )


def test_rewrites_identical_inline_destinations_independently_by_source_span() -> None:
    markdown = f"![first]({LEGACY_URL}) and ![second]({LEGACY_URL})"
    occurrences = locate_legacy_markdown_media(markdown)

    assert [item.ordinal for item in occurrences] == [0, 1]
    assert occurrences[0].destination == occurrences[1].destination == LEGACY_URL
    assert occurrences[0].destination_span != occurrences[1].destination_span

    rewritten = rewrite_legacy_markdown_media(
        markdown,
        {occurrences[0]: LOGICAL_ONE, occurrences[1]: LOGICAL_TWO},
    )

    assert rewritten == f"![first]({LOGICAL_ONE}) and ![second]({LOGICAL_TWO})"


def test_reference_definition_records_all_full_collapsed_and_shortcut_uses() -> None:
    markdown = (
        "![one][Shared Label]\n"
        "![Shared   Label][]\n"
        "![shared label]\n\n"
        f"[SHARED label]: <{LEGACY_URL}> 'historical image'\n"
    )

    occurrences = locate_legacy_markdown_media(markdown)

    assert len(occurrences) == 1
    occurrence = occurrences[0]
    assert occurrence.kind == "reference_definition"
    assert occurrence.reference_label == "shared label"
    assert occurrence.destination_span.text(markdown) == LEGACY_URL
    assert len(occurrence.usage_spans) == 3
    assert [span.text(markdown) for span in occurrence.usage_spans] == [
        "![one][Shared Label]",
        "![Shared   Label][]",
        "![shared label]",
    ]

    rewritten = rewrite_legacy_markdown_media(markdown, {occurrence: LOGICAL_ONE})

    assert rewritten == markdown[: occurrence.destination_span.char_start] + (
        LOGICAL_ONE + markdown[occurrence.destination_span.char_end :]
    )


def test_separate_reference_definitions_with_same_url_remain_independent() -> None:
    markdown = f"![one][a] ![two][b]\n\n[a]: {LEGACY_URL}\n[b]: {LEGACY_URL}\n"
    occurrences = locate_legacy_markdown_media(markdown)

    assert len(occurrences) == 2
    assert [item.reference_label for item in occurrences] == ["a", "b"]

    rewritten = rewrite_legacy_markdown_media(
        markdown,
        {occurrences[0]: LOGICAL_ONE, occurrences[1]: LOGICAL_TWO},
    )

    assert f"[a]: {LOGICAL_ONE}" in rewritten
    assert f"[b]: {LOGICAL_TWO}" in rewritten


@pytest.mark.parametrize(
    "markdown",
    [
        f"> ![image][ref]\n>\n> [ref]: <{LEGACY_URL}>\n",
        f"- ![image][ref]\n\n  [ref]: {LEGACY_URL}\n",
    ],
)
def test_reference_definitions_inside_commonmark_containers_are_located(
    markdown: str,
) -> None:
    occurrence = locate_legacy_markdown_media(markdown)[0]

    assert occurrence.kind == "reference_definition"
    assert occurrence.destination_span.text(markdown) == LEGACY_URL
    assert len(occurrence.usage_spans) == 1


def test_list_indentation_is_not_confused_with_an_indented_code_block() -> None:
    markdown = f"- list item\n\n    ![active image]({LEGACY_URL})\n"

    occurrence = locate_legacy_markdown_media(markdown)[0]

    assert occurrence.kind == "inline_image"
    assert occurrence.destination == LEGACY_URL


def test_shared_image_and_non_image_reference_fails_closed() -> None:
    markdown = f"![image][shared] [download][shared]\n\n[shared]: {LEGACY_URL}\n"

    with pytest.raises(LegacyMarkdownMediaError) as raised:
        locate_legacy_markdown_media(markdown)

    assert raised.value.reason_code == "shared_reference_definition"


def test_ignores_code_examples_and_prose_exactly() -> None:
    markdown = (
        f"`![inline code]({LEGACY_URL})`\n\n"
        "````markdown\n"
        f"![fenced]({LEGACY_URL})\n"
        "```\n"
        "````\n\n"
        f"    ![indented]({LEGACY_URL})\n\n"
        "- list item\n\n"
        f"    ![active list image](/not-legacy/{DIGEST}.png)\n\n"
        f"Prose: {LEGACY_URL}\n"
    )

    assert locate_legacy_markdown_media(markdown) == ()
    assert rewrite_legacy_markdown_media(markdown, {}) == markdown


@pytest.mark.parametrize(
    ("markdown", "reason_code"),
    [
        (f"![broken]({LEGACY_URL}", "malformed_inline_image"),
        (f"![broken](<{LEGACY_URL})", "malformed_inline_image"),
        (
            f"![image][ref]\n\n[ref]: <{LEGACY_URL} trailing",
            "malformed_reference_definition",
        ),
        (
            f"![image][ref]\n\n[ref]: {LEGACY_URL} trailing garbage",
            "malformed_reference_definition",
        ),
    ],
)
def test_fails_closed_for_malformed_legacy_image_source(
    markdown: str,
    reason_code: str,
) -> None:
    with pytest.raises(LegacyMarkdownMediaError) as raised:
        locate_legacy_markdown_media(markdown)

    assert raised.value.reason_code == reason_code


@pytest.mark.parametrize(
    "destination",
    [
        f"/api/v1/uploads/{WORKSPACE_ID}/{DIGEST}.svg",
        f"/api/v1/uploads/not-a-workspace/{DIGEST}.png",
        OTHER_LEGACY_URL.replace(OTHER_DIGEST, OTHER_DIGEST.upper()),
    ],
)
def test_noncanonical_or_noneligible_routes_are_not_rewritten(destination: str) -> None:
    markdown = f"![image]({destination})"
    assert locate_legacy_markdown_media(markdown) == ()


def test_duplicate_reference_definition_uses_commonmark_first_definition() -> None:
    markdown = f"![image][ref]\n\n[ref]: {LEGACY_URL}\n[REF]: {OTHER_LEGACY_URL}\n"

    occurrences = locate_legacy_markdown_media(markdown)

    assert len(occurrences) == 1
    assert occurrences[0].destination == LEGACY_URL


@pytest.mark.parametrize(
    ("prefix", "image", "suffix"),
    [
        ("", f"![a]({LEGACY_URL})", ""),
        ("αβ\r\n", f"![a](<{LEGACY_URL}>)", "\r\n끝"),
        ("before ", f"![a]({LEGACY_URL} 'title')", " after"),
        ("🙂\n", f'![a\\]b]({LEGACY_URL} "title")', "\n尾"),
    ],
)
def test_property_like_rewrite_changes_only_the_reported_destination_span(
    prefix: str,
    image: str,
    suffix: str,
) -> None:
    markdown = prefix + image + suffix
    occurrence = locate_legacy_markdown_media(markdown)[0]

    rewritten = rewrite_legacy_markdown_media(markdown, {occurrence: LOGICAL_ONE})

    span = occurrence.destination_span
    assert rewritten == (
        markdown[: span.char_start] + LOGICAL_ONE + markdown[span.char_end :]
    )
    assert span.byte_start == len(markdown[: span.char_start].encode("utf-8"))
    assert span.byte_end == len(markdown[: span.char_end].encode("utf-8"))


def test_rewrite_requires_complete_current_occurrences_and_logical_destinations() -> (
    None
):
    markdown = f"![one]({LEGACY_URL}) ![two]({OTHER_LEGACY_URL})"
    occurrences = locate_legacy_markdown_media(markdown)

    with pytest.raises(LegacyMarkdownMediaError) as incomplete:
        rewrite_legacy_markdown_media(markdown, {occurrences[0]: LOGICAL_ONE})
    assert incomplete.value.reason_code == "stale_or_incomplete_replacements"

    with pytest.raises(LegacyMarkdownMediaError) as invalid:
        rewrite_legacy_markdown_media(
            markdown,
            {occurrences[0]: LOGICAL_ONE, occurrences[1]: "https://example.test/x"},
        )
    assert invalid.value.reason_code == "invalid_replacement_destination"

    stale_source = "prefix " + markdown
    with pytest.raises(LegacyMarkdownMediaError) as stale:
        rewrite_legacy_markdown_media(
            stale_source,
            {occurrences[0]: LOGICAL_ONE, occurrences[1]: LOGICAL_TWO},
        )
    assert stale.value.reason_code == "stale_or_incomplete_replacements"


def test_service_scan_exposes_raw_source_spans_and_exact_replace_api() -> None:
    markdown = f"앞 ![image]({LEGACY_URL}) 뒤"

    occurrences = scan_legacy_upload_references(markdown, WORKSPACE_ID)

    assert len(occurrences) == 1
    occurrence = occurrences[0]
    assert occurrence.kind == "inline_image"
    assert occurrence.destination == LEGACY_URL
    assert occurrence.syntax_span.text(markdown) == f"![image]({LEGACY_URL})"
    assert count_legacy_upload_reference_uses((occurrence,)) == 1
    assert (
        replace_legacy_upload_references(
            markdown,
            {occurrence: LOGICAL_ONE},
        )
        == f"앞 ![image]({LOGICAL_ONE}) 뒤"
    )


def test_service_scan_counts_reference_uses_not_only_definition_spans() -> None:
    markdown = f"![one][shared] ![two][shared]\n![shared][]\n\n[shared]: {LEGACY_URL}\n"

    occurrences = scan_legacy_upload_references(markdown, WORKSPACE_ID)

    assert len(occurrences) == 1
    assert count_legacy_upload_reference_uses(occurrences) == 3


@pytest.mark.parametrize(
    "collision",
    [
        "![authored](https://reconcile.invalid/legacy-occurrence-0-0)",
        "[authored](https://reconcile.invalid/legacy-occurrence-0-0)",
        "[decoded](https://reconcile.invalid/legacy&#45;occurrence-0-0)",
    ],
)
def test_service_scan_sentinels_do_not_collide_with_authored_urls(
    collision: str,
) -> None:
    markdown = f"![legacy]({LEGACY_URL})\n{collision}"

    occurrences = scan_legacy_upload_references(markdown, WORKSPACE_ID)

    assert len(occurrences) == 1
    assert occurrences[0].destination == LEGACY_URL


def test_service_scan_returns_empty_tuple_for_plain_markdown() -> None:
    assert scan_legacy_upload_references("Plain UTF-8 body: 앞 뒤") == ()


def test_service_scan_distinguishes_foreign_workspace_from_ambiguity() -> None:
    with pytest.raises(LegacyMarkdownMediaError) as foreign:
        scan_legacy_upload_references(
            f"![image]({LEGACY_URL})",
            FOREIGN_WORKSPACE_ID,
        )
    assert foreign.value.reason_code == "legacy_workspace_mismatch"

    shared = f"![image][same] [link][same]\n\n[same]: {LEGACY_URL}\n"
    with pytest.raises(LegacyMarkdownMediaError) as ambiguous:
        scan_legacy_upload_references(shared, WORKSPACE_ID)
    assert ambiguous.value.reason_code == "legacy_markdown_ambiguous"


def test_service_scan_rejects_raw_html_but_not_raw_html_in_code() -> None:
    raw_html = f'<IMG alt="legacy" SRC="{LEGACY_URL}">'
    with pytest.raises(LegacyMarkdownMediaError) as unsupported:
        scan_legacy_upload_references(raw_html, WORKSPACE_ID)
    assert unsupported.value.reason_code == "unsupported_legacy_media_syntax"

    code = f"`{raw_html}`\n\n```html\n{raw_html}\n```\n"
    assert scan_legacy_upload_references(code, WORKSPACE_ID) == ()


@pytest.mark.parametrize(
    "raw_html",
    [
        (
            '<img src="'
            f'{LEGACY_URL.replace("/api/v1/uploads/", "/api%2Fv1%2Fuploads%2F")}'
            '">'
        ),
        (
            '<video poster="'
            f'{LEGACY_URL.replace("/api/v1/uploads/", "/discard/../api/v1/uploads/")}'
            '"></video>'
        ),
        (
            '<video src="'
            f'{LEGACY_URL.replace("/api/v1/", "/api/\nv1/")}'
            '"></video>'
        ),
        f'<video src="{LEGACY_URL.replace("/", "\\")}"></video>',
        (
            '<source srcset="data:image/png;base64,AAAA 1x, '
            f'{LEGACY_URL.replace("/", "\\")} 2x">'
        ),
        (
            '<img src="'
            f'{LEGACY_URL.replace("/api/", "/discard/%2e%2e/api/")}'
            '?download=1#preview">'
        ),
        f'<a href="{LEGACY_URL}">download</a>',
        f'<a href="https:///wiki.example{LEGACY_URL}">download</a>',
        f'<img alt="diagram" longdesc="{LEGACY_URL}" src="/safe.png">',
        f'<math><mtext href="{LEGACY_URL}">download</mtext></math>',
    ],
    ids=(
        "percent-encoded-separators",
        "literal-dot-segments",
        "browser-stripped-newline",
        "backslash-video-source",
        "backslash-srcset-candidate",
        "encoded-dot-segment-with-query-fragment",
        "raw-anchor",
        "raw-anchor-absolute-runtime-origin",
        "raw-image-longdesc",
        "raw-mathml-href",
    ),
)
def test_service_scan_rejects_browser_reachable_raw_html_aliases(
    raw_html: str,
) -> None:
    with pytest.raises(LegacyMarkdownMediaError) as raised:
        scan_legacy_upload_references(raw_html, WORKSPACE_ID)

    assert raised.value.reason_code == "unsupported_legacy_media_syntax"


@pytest.mark.parametrize(
    "raw_html",
    [
        f'<a href="https://wiki.example{LEGACY_URL}\v">download</a>',
        f'<img src="https://wiki.example{LEGACY_URL}\x01">',
        f'<video poster="{LEGACY_URL}\x0e"></video>',
        f'<source srcset="{LEGACY_URL}\x1f 2x">',
    ],
)
def test_raw_html_urls_strip_browser_c0_controls_at_the_edges(
    raw_html: str,
) -> None:
    with pytest.raises(LegacyMarkdownMediaError) as raised:
        scan_legacy_upload_references(raw_html, WORKSPACE_ID)

    assert raised.value.reason_code == "unsupported_legacy_media_syntax"
    assert browser_reachable_legacy_upload_identities(raw_html)
    assert len(
        source_mapped_local_legacy_upload_references(
            raw_html,
            trusted_http_origins=frozenset({"https://wiki.example"}),
        )
    ) == 1


@pytest.mark.parametrize("padding", ["\ufeff", "\u00a0"])
@pytest.mark.parametrize("side", ["prefix", "suffix"])
def test_raw_image_source_uses_ecmascript_trim_policy(
    padding: str,
    side: str,
) -> None:
    source = (
        f"{padding}{LEGACY_URL}"
        if side == "prefix"
        else f"{LEGACY_URL}{padding}"
    )
    raw_html = f'<img src="{source}">'

    with pytest.raises(LegacyMarkdownMediaError) as raised:
        scan_legacy_upload_references(raw_html, WORKSPACE_ID)

    assert raised.value.reason_code == "unsupported_legacy_media_syntax"
    assert browser_reachable_legacy_upload_identities(raw_html)


@pytest.mark.parametrize(
    "raw_html",
    [
        f'<img src="/safe.png?next={LEGACY_URL}">',
        f'<img src="/safe.png#{LEGACY_URL}">',
        f'<img src="///{LEGACY_URL.lstrip("/")}">',
        (
            '<img src="/discard%2F..%2Fapi%2Fv1%2Fuploads%2F'
            f'{WORKSPACE_ID}%2F{DIGEST}.png">'
        ),
        f'<source srcset="/safe.png {LEGACY_URL}, /second-safe.png 2x">',
        f'<source srcset="{LEGACY_URL}\u00a0 1x">',
        f'<video srcset="{LEGACY_URL}"></video>',
        f'<img poster="{LEGACY_URL}">',
        f'<audio src="{LEGACY_URL}"></audio>',
        f'<track src="{LEGACY_URL}">',
        f'<source src="{LEGACY_URL}">',
        f'<img srcset="{LEGACY_URL}">',
        f'<img src="//wiki.example{LEGACY_URL}">',
        f'<img src="https:{LEGACY_URL}">',
        f'<img src="https:api/v1/uploads/{WORKSPACE_ID}/{DIGEST}.png">',
        f'<img src="{f"https://wiki.example{LEGACY_URL}".replace("/", "\\")}">',
        f'<img src="http://wiki.example{LEGACY_URL}">',
        f'<video src="http://wiki.example{LEGACY_URL}"></video>',
    ],
    ids=(
        "query-only",
        "fragment-only",
        "authority-consumes-api-segment",
        "encoded-separators-do-not-create-browser-dot-segments",
        "descriptor-text",
        "non-ascii-srcset-url-whitespace",
        "invalid-video-srcset",
        "invalid-image-poster",
        "sanitized-audio-tag",
        "sanitized-track-tag",
        "sanitized-source-src",
        "sanitized-image-srcset",
        "constrained-image-scheme-relative",
        "constrained-image-same-scheme-single-slash",
        "constrained-image-same-scheme-relative",
        "constrained-image-backslash-authority",
        "sanitized-image-http-src",
        "sanitized-video-http-src",
    ),
)
def test_service_scan_avoids_raw_html_route_false_positives(raw_html: str) -> None:
    assert scan_legacy_upload_references(raw_html, WORKSPACE_ID) == ()


@pytest.mark.parametrize(
    "raw_html",
    [
        f'<img src="https://wiki.example{LEGACY_URL}">',
        f'<img src="https:///wiki.example{LEGACY_URL}">',
        f'<img src="https:////wiki.example{LEGACY_URL}">',
        f'<video poster="//wiki.example{LEGACY_URL}"></video>',
        f'<a href="https:api/v1/uploads/{WORKSPACE_ID}/{DIGEST}.png">root</a>',
        (
            '<a href="https:../../../api/v1/uploads/'
            f'{WORKSPACE_ID}/{DIGEST}.png">root</a>'
        ),
        (
            '<source srcset="'
            f'{f"https://wiki.example{LEGACY_URL}".replace("/", "\\")} 2x">'
        ),
    ],
    ids=(
        "absolute-runtime-origin",
        "absolute-extra-slash-runtime-origin",
        "absolute-many-slash-runtime-origin",
        "scheme-relative-runtime-origin",
        "same-scheme-relative-runtime-origin",
        "same-scheme-dot-relative-runtime-origin",
        "absolute-backslash-runtime-origin",
    ),
)
def test_service_scan_rejects_authority_urls_that_may_be_same_origin(
    raw_html: str,
) -> None:
    with pytest.raises(LegacyMarkdownMediaError) as raised:
        scan_legacy_upload_references(raw_html, WORKSPACE_ID)

    assert raised.value.reason_code == "unsupported_legacy_media_syntax"


def test_external_srcset_candidate_cannot_hide_local_legacy_alias() -> None:
    encoded_local = LEGACY_URL.replace(
        "/api/v1/uploads/",
        "/api%2Fv1%2Fuploads%2F",
    )
    raw_html = (
        '<source srcset="'
        f"https://cdn.example.test{LEGACY_URL} 1x, {encoded_local} 2x"
        '">'
    )

    with pytest.raises(LegacyMarkdownMediaError) as raised:
        scan_legacy_upload_references(raw_html, WORKSPACE_ID)

    assert raised.value.reason_code == "unsupported_legacy_media_syntax"


def test_descriptor_free_srcset_candidate_does_not_hide_following_alias() -> None:
    encoded_local = LEGACY_URL.replace(
        "/api/v1/uploads/",
        "/api%2Fv1%2Fuploads%2F",
    )
    raw_html = f'<source srcset="/safe.png, {encoded_local} 2x">'

    with pytest.raises(LegacyMarkdownMediaError) as raised:
        scan_legacy_upload_references(raw_html, WORKSPACE_ID)

    assert raised.value.reason_code == "unsupported_legacy_media_syntax"


@pytest.mark.parametrize(
    "markdown",
    [
        f"![query]({LEGACY_URL}?download=1)",
        f"![fragment]({LEGACY_URL}#fragment)",
        f"[download]({LEGACY_URL})",
        f"[download][legacy]\n\n[legacy]: {LEGACY_URL}\n",
    ],
)
def test_service_scan_rejects_rendered_nonrewritable_legacy_destinations(
    markdown: str,
) -> None:
    with pytest.raises(LegacyMarkdownMediaError) as raised:
        scan_legacy_upload_references(markdown, WORKSPACE_ID)

    assert raised.value.reason_code == "unsupported_legacy_media_syntax"


@pytest.mark.parametrize(
    "markdown",
    [
        f"![absolute](https://wiki.example{LEGACY_URL})",
        f"![absolute extra slash](https:///wiki.example{LEGACY_URL})",
        f"![absolute many slash](https:////wiki.example{LEGACY_URL})",
        f"[absolute](https://wiki.example{LEGACY_URL})",
        f"[scheme-relative](//wiki.example{LEGACY_URL})",
        (
            "![encoded absolute](https://wiki.example"
            f'{LEGACY_URL.replace("/api/v1/uploads/", "/api%2Fv1%2Fuploads%2F")})'
        ),
    ],
)
def test_service_scan_rejects_authority_urls_that_may_be_same_origin_markdown(
    markdown: str,
) -> None:
    with pytest.raises(LegacyMarkdownMediaError) as raised:
        scan_legacy_upload_references(markdown, WORKSPACE_ID)

    assert raised.value.reason_code == "unsupported_legacy_media_syntax"


@pytest.mark.parametrize(
    "markdown",
    [
        f"![scheme-relative](//wiki.example{LEGACY_URL})",
        f"![scheme-relative extra slash](///wiki.example{LEGACY_URL})",
        f"![same-scheme single slash](https:{LEGACY_URL})",
        (
            "![same-scheme relative]"
            f"(https:api/v1/uploads/{WORKSPACE_ID}/{DIGEST}.png)"
        ),
        (
            "![same-scheme dot relative]"
            f"(https:../../../api/v1/uploads/{WORKSPACE_ID}/{DIGEST}.png)"
        ),
        f"![http](http://wiki.example{LEGACY_URL})",
    ],
)
def test_service_scan_matches_frontend_image_source_constraints(
    markdown: str,
) -> None:
    assert scan_legacy_upload_references(markdown, WORKSPACE_ID) == ()


@pytest.mark.parametrize(
    "destination",
    [
        LEGACY_URL.replace("/api/v1/uploads/", "/api%2Fv1%2Fuploads%2F"),
        LEGACY_URL.replace("/api/v1/uploads/", "/api/v1/./uploads/"),
        LEGACY_URL.replace("/api/v1/uploads/", "/api/v1/x/../uploads/"),
        LEGACY_URL.replace("/api/", "/discard/%2e%2e/api/"),
    ],
)
def test_service_scan_rejects_legacy_routes_after_url_normalization(
    destination: str,
) -> None:
    with pytest.raises(LegacyMarkdownMediaError) as raised:
        scan_legacy_upload_references(f"![image]({destination})", WORKSPACE_ID)

    assert raised.value.reason_code == "unsupported_legacy_media_syntax"


def test_service_scan_does_not_normalize_dot_segments_created_by_asgi_decode(
) -> None:
    destination = (
        "/discard%2F..%2Fapi%2Fv1%2Fuploads%2F"
        f"{WORKSPACE_ID}%2F{DIGEST}.png"
    )

    assert (
        scan_legacy_upload_references(f"![image]({destination})", WORKSPACE_ID)
        == ()
    )


@pytest.mark.parametrize(
    "markdown",
    [
        f"Prose https://wiki.example{LEGACY_URL}",
        f"*Emphasized https://wiki.example{LEGACY_URL}*",
        f"Prose http://wiki.example{LEGACY_URL}.",
        f"Prose www.wiki.example{LEGACY_URL}",
        (
            "Prose https://wiki.example"
            f'{LEGACY_URL.replace("/api/v1/uploads/", "/api%2Fv1%2Fuploads%2F")}'
        ),
    ],
)
def test_service_scan_rejects_gfm_autolink_literals_to_legacy_route(
    markdown: str,
) -> None:
    with pytest.raises(LegacyMarkdownMediaError) as raised:
        scan_legacy_upload_references(markdown, WORKSPACE_ID)

    assert raised.value.reason_code == "unsupported_legacy_media_syntax"


@pytest.mark.parametrize("prefix", ["0", "9", "_"])
def test_gfm_autolink_can_start_after_digit_or_underscore(prefix: str) -> None:
    markdown = f"{prefix}https://wiki.example{LEGACY_URL} tail"

    with pytest.raises(LegacyMarkdownMediaError) as raised:
        scan_legacy_upload_references(markdown, WORKSPACE_ID)

    assert raised.value.reason_code == "unsupported_legacy_media_syntax"
    assert browser_reachable_legacy_upload_identities(markdown)


@pytest.mark.parametrize("prefix", ["0", "9", "a", "Z", "é", "中", "９"])
def test_gfm_www_autolink_cannot_start_after_alphanumeric(prefix: str) -> None:
    """GFM does not open a `www.` autolink after a letter or digit."""

    markdown = f"{prefix}www.wiki.example{LEGACY_URL} tail"

    assert scan_legacy_upload_references(markdown, WORKSPACE_ID) == ()
    assert browser_reachable_legacy_upload_identities(markdown) == frozenset()


def test_gfm_www_autolink_can_start_after_underscore() -> None:
    markdown = f"_www.wiki.example{LEGACY_URL} tail"

    with pytest.raises(LegacyMarkdownMediaError) as raised:
        scan_legacy_upload_references(markdown, WORKSPACE_ID)

    assert raised.value.reason_code == "unsupported_legacy_media_syntax"
    assert browser_reachable_legacy_upload_identities(markdown)


def test_gfm_autolink_stops_before_bom_whitespace() -> None:
    markdown = f"https://wiki.example{LEGACY_URL}\ufefftail"

    with pytest.raises(LegacyMarkdownMediaError) as raised:
        scan_legacy_upload_references(markdown, WORKSPACE_ID)

    assert raised.value.reason_code == "unsupported_legacy_media_syntax"
    assert browser_reachable_legacy_upload_identities(markdown)


@pytest.mark.parametrize(
    "markdown",
    [
        # A mid-literal quote is ordinary GFM URL-body text (dtxWiki review,
        # 260827): remark-gfm renders one autolink spanning the quote and the
        # legacy path, so the scanner must not stop at the quote either.
        f'https://wiki.example"{LEGACY_URL} tail',
        f"https://wiki.example'{LEGACY_URL} tail",
        # A quote wrapping the whole literal already worked (GFM trims a
        # *trailing* quote); keep it pinned alongside the mid-literal case.
        f'"https://wiki.example{LEGACY_URL}" tail',
    ],
)
def test_gfm_autolink_literal_is_not_truncated_by_a_quote(markdown: str) -> None:
    with pytest.raises(LegacyMarkdownMediaError) as raised:
        scan_legacy_upload_references(markdown, WORKSPACE_ID)

    assert raised.value.reason_code == "unsupported_legacy_media_syntax"
    assert browser_reachable_legacy_upload_identities(markdown)


@pytest.mark.parametrize(
    "suffix",
    [
        "](",
        "][",
        ")](",
        ")][",
        "]((",
        "]()",
        "]([",
        "](]",
        "]({",
        "](}",
        "][(",
        "][)",
        "][[",
        "][]",
        "][{",
        "][}",
        "]](",
        "]][",
    ],
)
def test_gfm_resource_tail_stops_before_closing_bracket(suffix: str) -> None:
    markdown = f")https://wiki.example{LEGACY_URL}{suffix}'"

    with pytest.raises(LegacyMarkdownMediaError) as raised:
        scan_legacy_upload_references(markdown, WORKSPACE_ID)

    assert raised.value.reason_code == "unsupported_legacy_media_syntax"
    assert browser_reachable_legacy_upload_identities(markdown)


@pytest.mark.parametrize("spacing", [" ", "\t", "  "])
def test_gfm_literal_inside_invalid_angle_autolink_stays_visible(
    spacing: str,
) -> None:
    markdown = f"<https://wiki.example{LEGACY_URL}{spacing}>"

    with pytest.raises(LegacyMarkdownMediaError) as raised:
        scan_legacy_upload_references(markdown, WORKSPACE_ID)

    assert raised.value.reason_code == "unsupported_legacy_media_syntax"
    assert browser_reachable_legacy_upload_identities(markdown)


@pytest.mark.parametrize(
    "entity",
    [
        "&period;",
        "&comma;",
        "&rpar;",
        "&grave;",
        "&rcub;",
        "&quot;",
        "&apos;",
        "&sol;",
        "&bsol;",
        "&num;",
        "&amp;",
        "&plus;",
        "&equals;",
    ],
)
def test_service_scan_applies_gfm_trailing_named_entity_rules(
    entity: str,
) -> None:
    markdown = f"https://wiki.example{LEGACY_URL}{entity}"

    with pytest.raises(LegacyMarkdownMediaError) as raised:
        scan_legacy_upload_references(markdown, WORKSPACE_ID)

    assert raised.value.reason_code == "unsupported_legacy_media_syntax"


@pytest.mark.parametrize("suffix", ["`", "}"])
def test_service_scan_preserves_gfm_literal_suffixes_kept_in_href(
    suffix: str,
) -> None:
    markdown = f"https://wiki.example{LEGACY_URL}{suffix}"

    assert scan_legacy_upload_references(markdown, WORKSPACE_ID) == ()


@pytest.mark.parametrize(
    "markdown",
    [
        f"Prose https://wiki.example{LEGACY_URL.replace('.png', r'\.png')}",
        f"Prose https://wiki.example{LEGACY_URL.replace('.png', '&#46;png')}",
        f"Prose https://wiki.example{LEGACY_URL.replace('.png', '&period;png')}",
        (
            "Prose https://wiki.example"
            f"{LEGACY_URL.replace('/api/v1/', '/api&#47;v1/')}"
        ),
    ],
)
def test_service_scan_preserves_raw_gfm_url_spelling(markdown: str) -> None:
    assert scan_legacy_upload_references(markdown, WORKSPACE_ID) == ()
    assert browser_reachable_legacy_upload_identities(markdown) == frozenset()


@pytest.mark.parametrize(
    "markdown",
    [
        f"![encoded](https://wiki.example{LEGACY_URL.replace('.png', '%5C.png')})",
        f'<img src="https://wiki.example{LEGACY_URL.replace(".png", "%5C.png")}">',
        (
            "![encoded entity](https://wiki.example"
            f"{LEGACY_URL.replace('.png', '%26%2346%3Bpng')})"
        ),
    ],
)
def test_service_scan_does_not_decode_url_path_twice(markdown: str) -> None:
    assert scan_legacy_upload_references(markdown, WORKSPACE_ID) == ()
    assert browser_reachable_legacy_upload_identities(markdown) == frozenset()


def test_browser_reachable_detector_covers_rendered_legacy_surfaces() -> None:
    absolute = f"https://wiki.example{LEGACY_URL}"
    markdown = (
        f"![image]({LEGACY_URL})\n"
        f"[download]({LEGACY_URL})\n"
        f'<img src="{LEGACY_URL}">\n'
        f"Prose {absolute}\n"
    )

    identities = browser_reachable_legacy_upload_identities(markdown)

    assert {(item.workspace_id, item.filename) for item in identities} == {
        (WORKSPACE_ID, f"{DIGEST}.png")
    }


def test_browser_reachable_detector_excludes_non_destination_mentions() -> None:
    absolute = f"https://wiki.example{LEGACY_URL}"
    markdown = (
        f"`{absolute}`\n\n"
        f"```text\n{absolute}\n```\n\n"
        f"![{absolute}](/safe.png)\n"
        f'[safe](/safe "{absolute}")\n'
        f"![safe](/safe.png?next={LEGACY_URL})\n"
        f'<span title="{absolute}">safe</span>\n'
    )

    assert browser_reachable_legacy_upload_identities(markdown) == frozenset()


@pytest.mark.parametrize(
    "markdown_template",
    [
        "<span x={url} y=z>",
        "<a data-x={url} >body</a>",
    ],
    ids=("arbitrary-unquoted-attribute", "non-href-link-attribute"),
)
def test_gfm_literal_in_unquoted_html_attribute_is_browser_visible(
    markdown_template: str,
) -> None:
    absolute = f"https://wiki.example{LEGACY_URL}"
    markdown = markdown_template.format(url=absolute)

    with pytest.raises(LegacyMarkdownMediaError) as raised:
        scan_legacy_upload_references(markdown, WORKSPACE_ID)

    assert raised.value.reason_code == "unsupported_legacy_media_syntax"
    identities = browser_reachable_legacy_upload_identities(markdown)
    assert {(item.workspace_id, item.filename) for item in identities} == {
        (WORKSPACE_ID, f"{DIGEST}.png")
    }
    references = source_mapped_local_legacy_upload_references(
        markdown,
        trusted_http_origins=frozenset({"https://wiki.example"}),
    )
    assert len(references) == 1
    assert references[0].destination_span.text(markdown) == LEGACY_URL


def test_gfm_detection_keeps_raw_occurrences_position_bound() -> None:
    absolute = f"https://wiki.example{LEGACY_URL}"
    encoded_period = absolute.replace(".png", "&#46;png")
    markdown = f'[safe](/safe "{absolute}")\n\n{encoded_period}'

    assert scan_legacy_upload_references(markdown, WORKSPACE_ID) == ()
    assert browser_reachable_legacy_upload_identities(markdown) == frozenset()


def test_gfm_visibility_probe_cannot_collide_with_authored_text() -> None:
    absolute = f"https://wiki.example{LEGACY_URL}"
    markdown = (
        f'[safe](/safe "{absolute}")\n\n'
        "https://gfm-source-probe.invalid/visible-1"
    )

    assert scan_legacy_upload_references(markdown, WORKSPACE_ID) == ()
    assert browser_reachable_legacy_upload_identities(markdown) == frozenset()


def test_gfm_visibility_probe_cannot_collide_after_entity_decoding() -> None:
    absolute = f"https://wiki.example{LEGACY_URL}"
    markdown = (
        f'[safe](/safe "{absolute}")\n\n'
        "https://gfm-source-probe.invalid/visible&#45;1"
    )

    assert scan_legacy_upload_references(markdown, WORKSPACE_ID) == ()
    assert browser_reachable_legacy_upload_identities(markdown) == frozenset()


def test_dollar_heavy_source_context_uses_bounded_parser_calls(monkeypatch) -> None:
    parse_calls = 0
    original_parse = markdown_media_module.MarkdownIt.parse

    def counted_parse(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(markdown_media_module.MarkdownIt, "parse", counted_parse)
    markdown = " ".join(f"${value}$" for value in range(400))

    masked = markdown_media_module._math_mask(markdown)

    assert masked.count(1) == sum(len(f"${value}$") for value in range(400))
    assert parse_calls <= 4


def test_nonlegacy_gfm_literals_do_not_invoke_visibility_parser(monkeypatch) -> None:
    parse_calls = 0
    original_parse = markdown_media_module.MarkdownIt.parse

    def counted_parse(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(markdown_media_module.MarkdownIt, "parse", counted_parse)
    markdown = "\n".join(
        f"https://safe.example/assets/{value}.png" for value in range(400)
    )

    identities = markdown_media_module._gfm_legacy_autolink_identities(markdown)

    assert identities == frozenset()
    assert parse_calls == 0


def test_many_legacy_gfm_literals_share_one_visibility_batch(monkeypatch) -> None:
    parse_calls = 0
    original_parse = markdown_media_module.MarkdownIt.parse

    def counted_parse(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(markdown_media_module.MarkdownIt, "parse", counted_parse)
    visible = f"https://wiki.example{LEGACY_URL}"
    hidden = f"https://wiki.example{OTHER_LEGACY_URL}"
    markdown = "\n".join([visible] * 200 + [f"`{hidden}`"] * 200)

    identities = markdown_media_module._gfm_legacy_autolink_identities(markdown)

    assert {(identity.sha256, identity.extension) for identity in identities} == {
        (DIGEST, "png")
    }
    assert parse_calls <= 5


def test_many_unquoted_attribute_gfm_literals_share_one_visibility_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parse_calls = 0
    original_parse = markdown_media_module.MarkdownIt.parse

    def counted_parse(*args, **kwargs):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(markdown_media_module.MarkdownIt, "parse", counted_parse)
    absolute = f"https://wiki.example{LEGACY_URL}"
    markdown = "\n\n".join(
        [f"<span x={absolute} data-index={index}>" for index in range(200)]
        + [f"<a data-x={absolute} data-index={index}>body</a>" for index in range(200)]
    )

    identities = markdown_media_module._gfm_legacy_autolink_identities(markdown)

    assert {(identity.sha256, identity.extension) for identity in identities} == {
        (DIGEST, "png")
    }
    assert parse_calls <= 5


@pytest.mark.parametrize(
    "markdown",
    [
        f"$$\nhttps://wiki.example{LEGACY_URL}\n$$",
        f"$https://wiki.example{LEGACY_URL}$",
        f"$$\n\\href{{https://wiki.example{LEGACY_URL}}}{{diagram}}\n$$",
    ],
)
def test_gfm_detection_ignores_remark_math_source(markdown: str) -> None:
    assert scan_legacy_upload_references(markdown, WORKSPACE_ID) == ()
    assert browser_reachable_legacy_upload_identities(markdown) == frozenset()


@pytest.mark.parametrize(
    "markdown",
    [
        f"$![image]({LEGACY_URL})$",
        f"$[download]({LEGACY_URL})$",
        f"$<https://wiki.example{LEGACY_URL}>$",
        f"$$\n![image]({LEGACY_URL})\n$$",
    ],
)
def test_remark_math_destinations_are_not_inventory_or_rewrite_references(
    markdown: str,
) -> None:
    assert scan_legacy_upload_references(markdown, WORKSPACE_ID) == ()
    assert browser_reachable_legacy_upload_identities(markdown) == frozenset()
    assert source_mapped_local_legacy_upload_references(markdown) == ()


@pytest.mark.parametrize(
    "markdown",
    [
        f"$https://wiki.example{LEGACY_URL} $$",
        f"$https://wiki.example{LEGACY_URL}\n$$",
    ],
)
def test_longer_dollar_run_does_not_hide_visible_gfm_legacy_link(
    markdown: str,
) -> None:
    with pytest.raises(LegacyMarkdownMediaError) as raised:
        scan_legacy_upload_references(markdown, WORKSPACE_ID)

    assert raised.value.reason_code == "unsupported_legacy_media_syntax"
    assert browser_reachable_legacy_upload_identities(markdown)


@pytest.mark.parametrize(
    "markdown",
    [
        f"$$\n![image]({LEGACY_URL})\n$",
        f"$$\n[download]({LEGACY_URL})\n$$$",
        f"$$$\n<img src=\"{LEGACY_URL}\">\n$$",
        f"$$meta\nhttps://wiki.example{LEGACY_URL}\n$$$",
        f"> $$\n> ![image]({LEGACY_URL})\n> $$$",
        f"- $$\n  [download]({LEGACY_URL})\n  $$",
    ],
)
def test_remark_flow_math_fence_variants_hide_legacy_syntax(markdown: str) -> None:
    assert scan_legacy_upload_references(markdown, WORKSPACE_ID) == ()
    assert browser_reachable_legacy_upload_identities(markdown) == frozenset()
    assert source_mapped_local_legacy_upload_references(markdown) == ()


@pytest.mark.parametrize(
    "markdown",
    [
        (
            f"- $$\n- https://wiki.example{LEGACY_URL}\n- $$"
        ),
        (
            f"1. $$\n2. https://wiki.example{LEGACY_URL}\n3. $$"
        ),
        (
            f"> - $$\n> - https://wiki.example{LEGACY_URL}\n> - $$"
        ),
        (
            f"prefix $\n\nhttps://wiki.example{LEGACY_URL}\n\nsuffix $"
        ),
        (
            f"<div>\n$\n</div>\n\nhttps://wiki.example{LEGACY_URL}\n\n$"
        ),
    ],
)
def test_math_delimiters_cannot_cross_visible_markdown_blocks(markdown: str) -> None:
    with pytest.raises(LegacyMarkdownMediaError) as raised:
        scan_legacy_upload_references(markdown, WORKSPACE_ID)

    assert raised.value.reason_code == "unsupported_legacy_media_syntax"
    assert browser_reachable_legacy_upload_identities(markdown)


@pytest.mark.parametrize(
    "markdown",
    [
        (
            "<div>\n$$\n"
            f'<a href="https://wiki.example{LEGACY_URL}">download</a>\n'
            "$$\n</div>"
        ),
        (
            "<details>\n$$\n"
            f'<img src="{LEGACY_URL}">\n'
            "$$\n</details>"
        ),
    ],
)
def test_dollars_inside_raw_html_block_do_not_hide_live_destinations(
    markdown: str,
) -> None:
    with pytest.raises(LegacyMarkdownMediaError) as raised:
        scan_legacy_upload_references(markdown, WORKSPACE_ID)

    assert raised.value.reason_code == "unsupported_legacy_media_syntax"
    assert browser_reachable_legacy_upload_identities(markdown)


@pytest.mark.parametrize(
    "markdown",
    [
        (
            '<a title="$" href="'
            f'https://wiki.example{LEGACY_URL}">x</a> tail $'
        ),
        f'[safe](/safe "$") https://wiki.example{LEGACY_URL} $',
        f"[safe](/safe?x=$) https://wiki.example{LEGACY_URL} $",
        f'![safe](/safe.png "$") https://wiki.example{LEGACY_URL} $',
        f"<https://safe.invalid/$> https://wiki.example{LEGACY_URL} $",
        f"https://safe.invalid/$ https://wiki.example{LEGACY_URL} $",
    ],
)
def test_non_text_dollars_cannot_hide_visible_legacy_link(markdown: str) -> None:
    with pytest.raises(LegacyMarkdownMediaError) as raised:
        scan_legacy_upload_references(markdown, WORKSPACE_ID)

    assert raised.value.reason_code == "unsupported_legacy_media_syntax"
    assert browser_reachable_legacy_upload_identities(markdown)


@pytest.mark.parametrize(
    "markdown",
    [
        (
            "| first $ | x |\n"
            "| --- | --- |\n"
            f"| https://wiki.example{LEGACY_URL} | $ |"
        ),
        (
            "| first $ | second | third |\n"
            "| --- | --- | --- |\n"
            f"| x | https://wiki.example{LEGACY_URL} | $ |"
        ),
        (
            "| code `a\\|b` $ | second |\n"
            "| --- | --- |\n"
            f"| https://wiki.example{LEGACY_URL} | $ |"
        ),
    ],
)
def test_math_delimiters_cannot_cross_gfm_table_cells(markdown: str) -> None:
    with pytest.raises(LegacyMarkdownMediaError) as raised:
        scan_legacy_upload_references(markdown, WORKSPACE_ID)

    assert raised.value.reason_code == "unsupported_legacy_media_syntax"
    assert browser_reachable_legacy_upload_identities(markdown)


@pytest.mark.parametrize(
    "markdown",
    [
        f"prefix `\n\nhttps://wiki.example{LEGACY_URL}\n\nsuffix `",
        f'[safe](/safe "`") https://wiki.example{LEGACY_URL} `',
        (
            "<div>\n`\n"
            f'<a href="https://wiki.example{LEGACY_URL}">x</a>\n'
            "`\n</div>"
        ),
        (
            "| first ` | x |\n"
            "| --- | --- |\n"
            f"| https://wiki.example{LEGACY_URL} | ` |"
        ),
    ],
)
def test_code_delimiters_cannot_cross_live_markdown_contexts(markdown: str) -> None:
    with pytest.raises(LegacyMarkdownMediaError) as raised:
        scan_legacy_upload_references(markdown, WORKSPACE_ID)

    assert raised.value.reason_code == "unsupported_legacy_media_syntax"
    assert browser_reachable_legacy_upload_identities(markdown)


@pytest.mark.parametrize(
    "markdown",
    [
        f"$$\n- $$$\n![image]({LEGACY_URL})\n$$$",
        f"$$\n1. $$$\n[download]({LEGACY_URL})\n$$$",
        f"- $$\n  - $$$\n  ![image]({LEGACY_URL})\n  $$$",
        f"> $$\n> > $$$\n> [download]({LEGACY_URL})\n> $$$",
    ],
)
def test_flow_math_content_container_markers_are_not_closing_fences(
    markdown: str,
) -> None:
    assert scan_legacy_upload_references(markdown, WORKSPACE_ID) == ()
    assert browser_reachable_legacy_upload_identities(markdown) == frozenset()
    assert source_mapped_local_legacy_upload_references(markdown) == ()


@pytest.mark.parametrize(
    "markdown",
    [
        f"- $$\n\tinside\n\t$$\n  https://wiki.example{LEGACY_URL}",
        f"-\t$$\n\tinside\n\t$$\nhttps://wiki.example{LEGACY_URL}",
        (
            "123. $$\n     inside\n     $$\n"
            f"     https://wiki.example{LEGACY_URL}"
        ),
        (
            "123456789. $$\n           inside\n           $$\n"
            f"           https://wiki.example{LEGACY_URL}"
        ),
        (
            "-    $$\n     inside\n     $$\n"
            f"     https://wiki.example{LEGACY_URL}"
        ),
        (
            "  - $$\n    inside\n    $$\n"
            f"    https://wiki.example{LEGACY_URL}"
        ),
        (
            "> - $$\n> \tinside\n> \t$$\n"
            f">   https://wiki.example{LEGACY_URL}"
        ),
        (
            "- > $$\n  > inside\n  > $$\n"
            f"  > https://wiki.example{LEGACY_URL}"
        ),
    ],
)
def test_flow_math_uses_commonmark_container_indentation(markdown: str) -> None:
    with pytest.raises(LegacyMarkdownMediaError) as raised:
        scan_legacy_upload_references(markdown, WORKSPACE_ID)

    assert raised.value.reason_code == "unsupported_legacy_media_syntax"
    assert browser_reachable_legacy_upload_identities(markdown)


def test_escaped_dollar_before_inline_math_close_remains_math_content() -> None:
    markdown = f"$![image]({LEGACY_URL})\\$"

    assert scan_legacy_upload_references(markdown, WORKSPACE_ID) == ()
    assert browser_reachable_legacy_upload_identities(markdown) == frozenset()


def test_local_source_mapping_returns_only_rendered_root_relative_destinations(
) -> None:
    absolute = f"https://external.example{LEGACY_URL}"
    markdown = (
        f"![image]({LEGACY_URL})\n"
        f"[download]({LEGACY_URL})\n"
        f'<img src="{LEGACY_URL}">\n'
        f"`{LEGACY_URL}`\n"
        f"![{LEGACY_URL}](/safe.png)\n"
        f'[safe](/safe "{LEGACY_URL}")\n'
        f"![safe](/safe.png?next={LEGACY_URL})\n"
        f"[external]({absolute})\n"
    )

    references = source_mapped_local_legacy_upload_references(markdown)

    assert len(references) == 3
    assert all(
        reference.destination_span.text(markdown) == LEGACY_URL
        for reference in references
    )


def test_local_source_mapping_batches_one_megabyte_without_candidate_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered_group = (
        f"![image]({LEGACY_URL})\n"
        f"https://wiki.example{LEGACY_URL}\n"
        f"`{LEGACY_URL}`\n"
        f"[safe](/safe?next={LEGACY_URL})\n"
    )
    rendered_group_count = 1_024
    markdown = f"{'가' * 200_000}\n\n{rendered_group * rendered_group_count}"
    assert len(markdown.encode("utf-8")) >= 1_000_000

    browser_scan_calls = 0
    real_browser_scan = (
        markdown_media_module.browser_reachable_legacy_upload_identities
    )

    def counted_browser_scan(*args: object, **kwargs: object):
        nonlocal browser_scan_calls
        browser_scan_calls += 1
        return real_browser_scan(*args, **kwargs)

    monkeypatch.setattr(
        markdown_media_module,
        "browser_reachable_legacy_upload_identities",
        counted_browser_scan,
    )

    references = (
        markdown_media_module.source_mapped_local_legacy_upload_references(
            markdown,
            trusted_http_origins=frozenset({"https://wiki.example"}),
        )
    )

    assert browser_scan_calls == 2
    assert len(references) == rendered_group_count * 2
    assert all(
        reference.destination_span.text(markdown) == LEGACY_URL
        for reference in references
    )
    assert len({item.destination_span.char_start for item in references}) == len(
        references
    )
    last_span = references[-1].destination_span
    assert last_span.byte_start == len(
        markdown[: last_span.char_start].encode("utf-8")
    )


def test_authorization_mode_does_not_trust_unknown_absolute_authorities() -> None:
    markdown = f"![external](https://external.example{LEGACY_URL})"

    assert browser_reachable_legacy_upload_identities(markdown)
    assert (
        browser_reachable_legacy_upload_identities(
            markdown,
            allow_unknown_http_origins=False,
        )
        == frozenset()
    )
    assert source_mapped_local_legacy_upload_references(markdown) == ()


@pytest.mark.parametrize(
    "markdown",
    [
        f"[canonical](https://wiki.example{LEGACY_URL})",
        f"[canonical](//wiki.example{LEGACY_URL})",
        f'<a href="https:///wiki.example{LEGACY_URL}">canonical</a>',
    ],
)
def test_authorization_mode_accepts_only_configured_absolute_origin(
    markdown: str,
) -> None:
    trusted = frozenset({"https://wiki.example"})

    identities = browser_reachable_legacy_upload_identities(
        markdown,
        allow_unknown_http_origins=False,
        trusted_http_origins=trusted,
        relative_url_base_path="/w/default/pages/page",
    )
    references = source_mapped_local_legacy_upload_references(
        markdown,
        trusted_http_origins=trusted,
        relative_url_base_path="/w/default/pages/page",
    )

    assert {(item.workspace_id, item.filename) for item in identities} == {
        (WORKSPACE_ID, f"{DIGEST}.png")
    }
    assert len(references) == 1
    assert references[0].destination_span.text(markdown) == LEGACY_URL


@pytest.mark.parametrize(
    "markdown",
    [
        f"[canonical](https://proxy.example.test/dtxwiki{LEGACY_URL})",
        f"[canonical](/dtxwiki{LEGACY_URL})",
        f'<img src="https://proxy.example.test/dtxwiki{LEGACY_URL}">',
    ],
)
def test_configured_public_base_path_is_stripped_before_asgi_matching(
    markdown: str,
) -> None:
    trusted = frozenset({"https://proxy.example.test/dtxwiki"})

    identities = browser_reachable_legacy_upload_identities(
        markdown,
        allow_unknown_http_origins=False,
        trusted_http_origins=trusted,
    )
    references = source_mapped_local_legacy_upload_references(
        markdown,
        trusted_http_origins=trusted,
    )

    assert {(item.workspace_id, item.filename) for item in identities} == {
        (WORKSPACE_ID, f"{DIGEST}.png")
    }
    assert len(references) == 1
    assert references[0].destination_span.text(markdown) == LEGACY_URL


def test_inventory_fails_closed_for_unknown_reverse_proxy_path_prefix() -> None:
    markdown = f"[legacy](https://alias.example/unknown-prefix{LEGACY_URL})"

    with pytest.raises(LegacyMarkdownMediaError) as raised:
        scan_legacy_upload_references(markdown, WORKSPACE_ID)

    assert raised.value.reason_code == "unsupported_legacy_media_syntax"


def test_configured_public_base_path_does_not_trust_external_authority() -> None:
    markdown = f"[external](https://external.example/dtxwiki{LEGACY_URL})"
    trusted = frozenset({"https://proxy.example.test/dtxwiki"})

    assert (
        browser_reachable_legacy_upload_identities(
            markdown,
            allow_unknown_http_origins=False,
            trusted_http_origins=trusted,
        )
        == frozenset()
    )
    assert (
        source_mapped_local_legacy_upload_references(
            markdown,
            trusted_http_origins=trusted,
        )
        == ()
    )


@pytest.mark.parametrize(
    "authority",
    [
        "\u00df.example",
        "%C3%9F.example",
    ],
    ids=("unicode-idn", "percent-encoded-idn"),
)
def test_configured_origin_does_not_casefold_distinct_idn_authority(
    authority: str,
) -> None:
    markdown = f'<img src="https://{authority}/dtxwiki{LEGACY_URL}">'
    trusted = frozenset({"https://ss.example/dtxwiki"})

    assert (
        browser_reachable_legacy_upload_identities(
            markdown,
            allow_unknown_http_origins=False,
            trusted_http_origins=trusted,
        )
        == frozenset()
    )
    assert (
        source_mapped_local_legacy_upload_references(
            markdown,
            trusted_http_origins=trusted,
        )
        == ()
    )


@pytest.mark.parametrize(
    ("configured_authority", "source_authority"),
    [
        ("\u00df.example", "xn--zca.example"),
        ("xn--zca.example", "%C3%9F.example"),
        ("xn--ab-0ea.example", "a\u00b7b.example"),
    ],
    ids=("unicode-config", "percent-encoded-source", "relaxed-uts46"),
)
def test_configured_origin_accepts_browser_equivalent_idn_authority(
    configured_authority: str,
    source_authority: str,
) -> None:
    markdown = f'<img src="https://{source_authority}/dtxwiki{LEGACY_URL}">'
    trusted = frozenset({f"https://{configured_authority}/dtxwiki"})

    identities = browser_reachable_legacy_upload_identities(
        markdown,
        allow_unknown_http_origins=False,
        trusted_http_origins=trusted,
    )
    references = source_mapped_local_legacy_upload_references(
        markdown,
        trusted_http_origins=trusted,
    )

    assert {(item.workspace_id, item.filename) for item in identities} == {
        (WORKSPACE_ID, f"{DIGEST}.png")
    }
    assert len(references) == 1
    assert references[0].destination_span.text(markdown) == LEGACY_URL


@pytest.mark.parametrize(
    ("configured_authority", "source_authority"),
    [
        ("127.0.0.1", "127.1"),
        ("ab--cd.example", "ab--cd.example"),
        ("a..example", "a..example"),
        ("xn--abc-.example", "xn--abc-.example"),
    ],
    ids=(
        "ipv4-number",
        "check-hyphens-false",
        "empty-label",
        "chromium-ascii-a-label",
    ),
)
def test_configured_origin_accepts_browser_equivalent_ascii_authority(
    configured_authority: str,
    source_authority: str,
) -> None:
    markdown = f'<img src="https://{source_authority}/dtxwiki{LEGACY_URL}">'
    trusted = frozenset({f"https://{configured_authority}/dtxwiki"})

    identities = browser_reachable_legacy_upload_identities(
        markdown,
        allow_unknown_http_origins=False,
        trusted_http_origins=trusted,
    )
    references = source_mapped_local_legacy_upload_references(
        markdown,
        trusted_http_origins=trusted,
    )

    assert {(item.workspace_id, item.filename) for item in identities} == {
        (WORKSPACE_ID, f"{DIGEST}.png")
    }
    assert len(references) == 1
    assert references[0].destination_span.text(markdown) == LEGACY_URL


@pytest.mark.parametrize(
    ("authority", "canonical"),
    [
        ("0177.1", "127.0.0.1"),
        ("0x7f.1", "127.0.0.1"),
        ("2130706433", "127.0.0.1"),
        ("127.0.0.1.", "127.0.0.1"),
        ("0x", "0.0.0.0"),
        ("1.0x", "1.0.0.0"),
        ("-\u00e9.example", "xn----bga.example"),
        ("foo_bar.example", "foo_bar.example"),
        ("\u2615..example", "xn--53h..example"),
        (f"{'a' * 64}.example", f"{'a' * 64}.example"),
        ("XN--ABC-.example", "xn--abc-.example"),
    ],
)
def test_browser_hostname_uses_whatwg_canonical_form(
    authority: str,
    canonical: str,
) -> None:
    assert markdown_media_module._browser_ascii_hostname(authority) == canonical


@pytest.mark.parametrize(
    "authority",
    [
        "1a.\u05d0\u05d1",
        "\u2615.\u05d0\u05d1",
        "a-.\u05d0\u05d1",
        "xn--8i7caa.\u2615",
        "a\x01b.example",
        "a\x7fb.example",
        "09",
        "example.1",
        "4294967296",
        "9" * 4_301,
    ],
    ids=(
        "bidi-leading-digit",
        "bidi-leading-symbol",
        "bidi-trailing-hyphen",
        "mapped-a-label",
        "c0-control",
        "delete-control",
        "invalid-octal",
        "numeric-final-domain-label",
        "ipv4-overflow",
        "oversized-ipv4-number",
    ),
)
def test_browser_hostname_rejects_whatwg_invalid_authority(
    authority: str,
) -> None:
    assert markdown_media_module._browser_ascii_hostname(authority) is None


@pytest.mark.parametrize(
    "authority",
    [
        "external.example",
        "wiki.example.external.test",
        "wiki.example:444",
        "user@wiki.example",
    ],
)
def test_configured_origin_does_not_trust_nearby_external_authority(
    authority: str,
) -> None:
    markdown = f"[external](https://{authority}{LEGACY_URL})"

    assert (
        browser_reachable_legacy_upload_identities(
            markdown,
            allow_unknown_http_origins=False,
            trusted_http_origins=frozenset({"https://wiki.example"}),
        )
        == frozenset()
    )
    assert (
        source_mapped_local_legacy_upload_references(
            markdown,
            trusted_http_origins=frozenset({"https://wiki.example"}),
        )
        == ()
    )


@pytest.mark.parametrize(
    "markdown",
    [
        f"[relative](../../../api/v1/uploads/{WORKSPACE_ID}/{DIGEST}.png)",
        f'<img src="../../../api/v1/uploads/{WORKSPACE_ID}/{DIGEST}.png">',
    ],
)
def test_public_route_relative_alias_is_browser_reachable_and_source_mapped(
    markdown: str,
) -> None:
    base_path = "/w/default/pages/page"

    identities = browser_reachable_legacy_upload_identities(
        markdown,
        allow_unknown_http_origins=False,
        relative_url_base_path=base_path,
    )
    references = source_mapped_local_legacy_upload_references(
        markdown,
        relative_url_base_path=base_path,
    )

    assert {(item.workspace_id, item.filename) for item in identities} == {
        (WORKSPACE_ID, f"{DIGEST}.png")
    }
    assert len(references) == 1
    assert references[0].destination_span.text(markdown) == LEGACY_URL


def test_relative_alias_that_misses_public_route_root_is_not_source_mapped() -> None:
    markdown = f"[relative](../api/v1/uploads/{WORKSPACE_ID}/{DIGEST}.png)"

    assert (
        source_mapped_local_legacy_upload_references(
            markdown,
            relative_url_base_path="/w/default/pages/page",
        )
        == ()
    )


@pytest.mark.parametrize(
    "markdown",
    [
        f"[relative](api/v1/uploads/{WORKSPACE_ID}/{DIGEST}.png)",
        f'<img src="api/v1/uploads/{WORKSPACE_ID}/{DIGEST}.png">',
    ],
)
def test_omitted_relative_base_path_does_not_guess_a_route(markdown: str) -> None:
    """A caller that supplies no page route gets fail-closed, not a fake one.

    ``/context`` used to be the silent default and does not correspond to any
    route this application actually serves -- it happened to make a
    single-segment relative reference resolve by accident (replacing its own
    last path segment lands back at root). The real Context page route is
    ``/w/{workspace_slug}/pages/{slug}``: resolving the SAME relative
    reference against that shape does not reach ``/api/v1/uploads/...`` (see
    ``test_relative_alias_that_misses_public_route_root_is_not_source_mapped``),
    so a caller that skips the parameter must get "not reachable", never a
    route it never asked for.
    """

    assert (
        browser_reachable_legacy_upload_identities(
            markdown,
            allow_unknown_http_origins=False,
        )
        == frozenset()
    )
    assert source_mapped_local_legacy_upload_references(markdown) == ()


def test_local_source_probe_identity_cannot_collide_with_existing_alias() -> None:
    probe_workspace = UUID(int=1)
    probe_digest = f"{1:064x}"
    encoded_probe = (
        "/api%2Fv1%2Fuploads%2F"
        f"{probe_workspace}%2F{probe_digest}.png"
    )
    markdown = f"`{LEGACY_URL}`\n\n[existing alias]({encoded_probe})"

    references = source_mapped_local_legacy_upload_references(markdown)

    assert references == ()


def test_batched_source_probes_stay_unique_after_identity_collision() -> None:
    probe_workspace = UUID(int=1)
    probe_digest = f"{1:064x}"
    encoded_probe = (
        "/api%2Fv1%2Fuploads%2F"
        f"{probe_workspace}%2F{probe_digest}.png"
    )
    markdown = (
        f"[existing alias]({encoded_probe})\n\n"
        f"`{LEGACY_URL}`\n\n"
        f"![visible]({LEGACY_URL})"
    )

    references = source_mapped_local_legacy_upload_references(markdown)

    assert len(references) == 1
    assert references[0].destination_span.char_start == markdown.rindex(LEGACY_URL)


def test_bom_prefixed_known_frontmatter_is_hidden_like_the_page_renderer() -> None:
    markdown = (
        "\ufeff---\n"
        f'title: "![hidden]({LEGACY_URL})"\n'
        "---\n\n"
        "Visible body"
    )

    assert scan_legacy_upload_references(markdown, WORKSPACE_ID) == ()
    assert browser_reachable_legacy_upload_identities(markdown) == frozenset()
    assert source_mapped_local_legacy_upload_references(markdown) == ()


def test_bom_frontmatter_rebase_preserves_original_source_offsets() -> None:
    markdown = (
        "\ufeff---\n"
        "title: Visible legacy\n"
        "---\n\n"
        f"![visible]({LEGACY_URL})"
    )

    occurrences = scan_legacy_upload_references(markdown, WORKSPACE_ID)

    assert len(occurrences) == 1
    assert occurrences[0].destination_span.text(markdown) == LEGACY_URL
    assert occurrences[0].destination_span.char_start == markdown.index(LEGACY_URL)


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\v", "\f"])
def test_non_crlf_separators_do_not_create_hidden_frontmatter(
    separator: str,
) -> None:
    absolute = f"https://wiki.example{LEGACY_URL}"
    markdown = separator.join(("---", f"title: {absolute}", "---", "visible"))

    with pytest.raises(LegacyMarkdownMediaError) as raised:
        scan_legacy_upload_references(markdown, WORKSPACE_ID)

    assert raised.value.reason_code == "unsupported_legacy_media_syntax"
    assert browser_reachable_legacy_upload_identities(markdown)


@pytest.mark.parametrize("padding", ["\u0085", "\x1c", "\x1d", "\x1e", "\x1f"])
@pytest.mark.parametrize("delimiter", ["opening", "closing"])
def test_python_only_whitespace_does_not_hide_frontmatter_like_javascript(
    padding: str,
    delimiter: str,
) -> None:
    absolute = f"https://wiki.example{LEGACY_URL}"
    opening = f"{padding}---{padding}" if delimiter == "opening" else "---"
    closing = f"{padding}---{padding}" if delimiter == "closing" else "---"
    markdown = f"{opening}\ntitle: {absolute}\n{closing}\nvisible"

    with pytest.raises(LegacyMarkdownMediaError) as raised:
        scan_legacy_upload_references(markdown, WORKSPACE_ID)

    assert raised.value.reason_code == "unsupported_legacy_media_syntax"
    assert browser_reachable_legacy_upload_identities(markdown)


def test_python_only_whitespace_does_not_create_known_frontmatter_key() -> None:
    absolute = f"https://wiki.example{LEGACY_URL}"
    markdown = f"---\ntitle\u0085:\u0085{absolute}\n---\nvisible"

    with pytest.raises(LegacyMarkdownMediaError) as raised:
        scan_legacy_upload_references(markdown, WORKSPACE_ID)

    assert raised.value.reason_code == "unsupported_legacy_media_syntax"
    assert browser_reachable_legacy_upload_identities(markdown)


@pytest.mark.parametrize(
    "padding",
    [
        "\v",
        "\f",
        "\u00a0",
        "\u1680",
        "\u2007",
        "\u2028",
        "\u2029",
        "\u202f",
        "\u205f",
        "\u3000",
        "\ufeff",
    ],
)
def test_ecmascript_whitespace_keeps_frontmatter_hidden(padding: str) -> None:
    markdown = (
        f"{padding}---{padding}\n"
        f"title{padding}:{padding}https://wiki.example{LEGACY_URL}\n"
        f"{padding}---{padding}\nvisible"
    )

    assert scan_legacy_upload_references(markdown, WORKSPACE_ID) == ()
    assert browser_reachable_legacy_upload_identities(markdown) == frozenset()
    assert source_mapped_local_legacy_upload_references(markdown) == ()


def test_lone_cr_line_endings_preserve_visible_gfm_and_source_offsets() -> None:
    absolute = f"https://wiki.example{LEGACY_URL}"
    markdown = f"prefix\r{absolute}\rtail\r![image]({LEGACY_URL})"

    identities = browser_reachable_legacy_upload_identities(markdown)
    references = source_mapped_local_legacy_upload_references(markdown)

    assert {(item.workspace_id, item.filename) for item in identities} == {
        (WORKSPACE_ID, f"{DIGEST}.png")
    }
    assert len(references) == 1
    assert references[0].destination_span.char_start == markdown.rindex(LEGACY_URL)
    assert references[0].destination_span.text(markdown) == LEGACY_URL


@pytest.mark.parametrize(
    "markdown",
    [
        f"`{LEGACY_URL}`\rvisible",
        f"${LEGACY_URL}$\rvisible",
        f"$$\r{LEGACY_URL}\r$$\rvisible",
        f"```text\r{LEGACY_URL}\r```\rvisible",
    ],
)
def test_lone_cr_line_endings_keep_code_and_math_hidden(markdown: str) -> None:
    assert scan_legacy_upload_references(markdown, WORKSPACE_ID) == ()
    assert browser_reachable_legacy_upload_identities(markdown) == frozenset()
    assert source_mapped_local_legacy_upload_references(markdown) == ()


def test_service_scan_ignores_gfm_autolinks_in_code_and_safe_url_components(
) -> None:
    absolute = f"https://wiki.example{LEGACY_URL}"
    markdown = (
        f"`{absolute}`\n\n"
        f"```text\n{absolute}\n```\n\n"
        f"Safe https://wiki.example/safe.png?next={LEGACY_URL}\n"
        f'[safe](/safe "{absolute}")\n'
        f"[{absolute}](https://wiki.example/safe)\n"
        f"![{absolute}](/safe.png)\n"
        f'<span title="{absolute}">safe</span>\n'
    )

    assert scan_legacy_upload_references(markdown, WORKSPACE_ID) == ()


def test_service_scan_allows_prose_only_legacy_url() -> None:
    assert (
        scan_legacy_upload_references(f"Prose only: {LEGACY_URL}", WORKSPACE_ID)
        == ()
    )


def test_service_scan_does_not_treat_alt_or_link_text_as_an_image_destination() -> None:
    markdown = (
        f"![alt contains {LEGACY_URL}](/ordinary.png)\n"
        f"[link text contains {LEGACY_URL}](/ordinary)\n"
    )

    assert scan_legacy_upload_references(markdown, WORKSPACE_ID) == ()


def test_service_scan_error_codes_are_pinned() -> None:
    assert LEGACY_UPLOAD_REFERENCE_SCAN_ERROR_CODES == {
        "legacy_markdown_ambiguous",
        "legacy_workspace_mismatch",
        "unsupported_legacy_media_syntax",
    }


@pytest.mark.parametrize(
    "frontmatter_line",
    [
        f"title: https://wiki.example{LEGACY_URL}",
        f'note: "![image]({LEGACY_URL})"',
        f'note: \'<img src="{LEGACY_URL}">\'',
    ],
)
def test_page_frontmatter_is_not_scanned_as_rendered_content(
    frontmatter_line: str,
) -> None:
    markdown = f"---\ntitle: Clean\n{frontmatter_line}\n---\n\n# Body"

    assert scan_legacy_upload_references(markdown, WORKSPACE_ID) == ()
    assert browser_reachable_legacy_upload_identities(markdown) == frozenset()
    assert source_mapped_local_legacy_upload_references(markdown) == ()


def test_frontmatter_stripping_rebases_rewrite_spans_to_original_source() -> None:
    markdown = (
        "---\r\n"
        "title: Diagram\r\n"
        "custom: preserve\r\n"
        "---\r\n\r\n"
        f"앞 ![image]({LEGACY_URL}) 뒤"
    )

    occurrences = scan_legacy_upload_references(markdown, WORKSPACE_ID)

    assert len(occurrences) == 1
    occurrence = occurrences[0]
    assert occurrence.destination_span.text(markdown) == LEGACY_URL
    rewritten = replace_legacy_upload_references(
        markdown,
        {occurrence: LOGICAL_ONE},
    )
    assert rewritten.startswith(
        "---\r\ntitle: Diagram\r\ncustom: preserve\r\n---\r\n\r\n"
    )
    assert f"앞 ![image]({LOGICAL_ONE}) 뒤" in rewritten

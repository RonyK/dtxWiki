from __future__ import annotations

import gc
from concurrent.futures import ThreadPoolExecutor
from time import perf_counter

import pytest

from backend.context_media.domain.rendered_markdown import (
    RENDERED_MARKDOWN,
    RenderedMarkdownAnalysis,
)
from backend.utils.markdown_links import (
    _decoded_reader_url_spans,
    _raw_reader_url_spans,
    rendered_markdown_links,
    rendered_wikilinks,
)


def test_rendered_markdown_links_follow_explicit_renderer_syntax():
    target = "11111111-1111-1111-1111-111111111111"
    relative = f"/context?target={target}"
    absolute = f"https://wiki.example/context?target={target}"
    body = "\n\n".join(
        (
            f"[plain]({relative})",
            f'[title]({relative} "Target title")',
            f"[angle](<{relative}>)",
            f"[reference][target]\n\n[target]: {relative}",
            f"<{absolute}>",
            absolute,
            f"www.wiki.example/context?target={target}",
            f"user-{target}@example.com",
            f'<a href="{relative}">raw HTML</a>',
            f"$[math link]({relative})$",
            f"![image]({relative})",
            f"`[inline code]({relative})`",
            f"    [indented code]({relative})",
            f"```md\n[fenced code]({relative})\n```",
            f"<!-- [comment]({relative}) -->",
            rf"\[escaped]({relative})",
        )
    )

    links = rendered_markdown_links(body)

    assert [link.destination for link in links] == [
        relative,
        relative,
        relative,
        relative,
        absolute,
    ]
    assert all(0 <= link.source_start < link.source_end <= len(body) for link in links)


@pytest.mark.parametrize(
    "body",
    (
        "https://wiki.example/context?target=1",
        "www.wiki.example/context?target=1",
        "user@example.com",
        "https&#58;//wiki.example/context?target=1",
        r"https\://wiki.example/context?target=1",
    ),
)
def test_rendered_markdown_links_ignore_bare_gfm_literals(body):
    assert rendered_markdown_links(body) == ()


def test_rendered_markdown_link_span_is_the_label_in_a_long_paragraph():
    target = "11111111-1111-1111-1111-111111111111"
    destination = f"/context?target={target}"
    body = f"{'prefix ' * 1500}[target](<{destination}> \"Target\")"

    links = rendered_markdown_links(body)

    assert len(links) == 1
    assert links[0].destination == destination
    assert body[links[0].source_start : links[0].source_end] == "[target]"


@pytest.mark.parametrize(
    ("prefix", "syntax"),
    (
        (r"\\", "[actual]({destination})"),
        (r"\\\\", "[actual]({destination})"),
        (r"\!", "[actual]({destination})"),
        (r"\\", "<{destination}>"),
    ),
)
def test_rendered_markdown_links_preserve_even_escape_parity(prefix, syntax):
    destination = "https://wiki.example/context?target=" + (
        "11111111-1111-1111-1111-111111111111"
    )

    links = rendered_markdown_links(prefix + syntax.format(destination=destination))

    assert [link.destination for link in links] == [destination]


def test_rendered_markdown_links_leave_raw_html_state_to_the_renderer():
    target = "11111111-1111-1111-1111-111111111111"
    destination = f"/context?target={target}"
    body = (
        'p <template><script>var x="</template>";</script>'
        f"[inside]({destination})</template> "
        f"[outside]({destination})"
    )

    links = rendered_markdown_links(body)

    assert [body[link.source_start : link.source_end] for link in links] == [
        "[inside]",
        "[outside]",
    ]


def test_inline_cdata_does_not_create_an_explicit_link_relationship():
    body = "x <![CDATA[x](https://e.test)]]>"

    assert rendered_markdown_links(body) == ()


def test_unclosed_inline_cdata_opener_remains_explicit_link_text():
    body = "x <![CDATA[x](https://e.test)"

    assert [link.destination for link in rendered_markdown_links(body)] == [
        "https://e.test"
    ]


@pytest.mark.parametrize(
    ("body", "target"),
    (
        (r"\[\[target]]", "target"),
        (r"[\[target]]", "target"),
        (r"[[target]\]", "target"),
        ("&#91;&#91;target]]", "target"),
        ("&lbrack;&lbrack;target&rbrack;&rbrack;", "target"),
        ("[[target#]]", "target#"),
        ("[[target#|alias]]", "target#"),
        ("[[tar_get]]", "tar_get"),
        ("[[target|a_alias_b]]", "target"),
    ),
)
def test_rendered_wikilinks_follow_decoded_text_nodes(body, target):
    links = rendered_wikilinks(body)

    assert len(links) == 1
    assert links[0].target == target
    assert body[links[0].source_start : links[0].source_end] == body


@pytest.mark.parametrize(
    "body",
    (
        "[[https://example.com]]",
        "[[target|https://example.com_]]",
        "[[target|http://.]]",
        "[[target|www.example.com]]",
        "[[target|before www.example.com after]]",
        "[[user@example.com]]",
        "[[target|/user@example.com]]",
        "[[target|https&#58;//example.com]]",
        r"[[target|https\://example.com]]",
        "[[target|www.\u200bwiki_]]",
        "https://e.test/x/[[target]]",
        "www.e.test/x/[[target]]",
        "https&#58;//e.test/x/[[target]]",
        "https&colon;//e.test/x/[[target]]",
        "www&#46;e.test/[[target]]",
        r"www\.e.test/[[target]]",
        "&#119;ww.e.test/[[target]]",
        "https://e.test/x&#93;[[target]]",
        "https://e.test/x&#32;[[target]]",
        "https://e.test/x]&#91;&#91;target]]",
        r"https://e.test/x]\[\[target]]",
    ),
)
def test_rendered_wikilinks_exclude_url_or_email_like_relationships(body):
    assert rendered_wikilinks(body) == ()


@pytest.mark.parametrize(
    "body",
    (
        "[[target|*alias*]]",
        "[[tar*get*]]",
        "[[target|~~alias~~]]",
        "[[_target_]]",
        "![[target]](https://example.com/image.png)",
        "[label [[target]]](https://example.com)",
        "`[[target]]`",
        "    [[target]]",
        ">     [[target]]",
        "-     [[target]]",
        "1.     [[target]]",
        "~~~ [[target]]",
        "```md\n[[target]]\n```",
        "<!-- [[target]] -->",
        "<![CDATA[[[target]]]]>",
        "x <![CDATA[[[target]]]]>",
        "$[[target]]$",
    ),
)
def test_rendered_wikilinks_require_one_visible_non_link_text_token(body):
    assert rendered_wikilinks(body) == ()


@pytest.mark.parametrize(
    "body",
    (
        "==Read [[Design]] first==",
        "if a == b then [[Design]] and c == d",
        "a==b [[Design]] c==d",
        "&#61;&#61;Read [[Design]] first&#61;&#61;",
        r"\=\=Read \[\[Design\]\] first\=\=",
        r"==before \*format\* [[Design]] after==",
        "===Read [[Design]] first==",
        "- ==Read [[Design]] first==",
        "| note |\n| --- |\n| ==Read [[Design]] first== |",
    ),
)
def test_rendered_wikilinks_are_consumed_by_reader_highlight_tokens(body):
    assert rendered_wikilinks(body) == ()


@pytest.mark.parametrize(
    ("body", "targets"),
    (
        ("[[Before]] ==hide [[Design]]== [[After]]", ["Before", "After"]),
        ("==before== [[Design]] ==after==", ["Design"]),
        ("==note==[[Design]]==tail==", ["Design"]),
        ("[[A==B==C]]", ["A==B==C"]),
        ("[[Design|alias ==literal==]]", ["Design"]),
        ("==before **[[Design]]** after==", ["Design"]),
        ("==before *format* [[Design]] after==", ["Design"]),
        ("==before _format_ [[Design]] after==", ["Design"]),
        ("==before ~~format~~ [[Design]] after==", ["Design"]),
        ("==before `code` [[Design]] after==", ["Design"]),
        ("==before [ordinary](/context?target=x) [[Design]] after==", ["Design"]),
        ("==before\n[[Design]]\nafter==", ["Design"]),
        (r"\&#61;\&#61;Read [[Design]] first\&#61;\&#61;", ["Design"]),
        ("[[https://example.com|==literal]] [[Design]] tail==", ["Design"]),
    ),
)
def test_reader_highlight_visibility_preserves_other_wikilinks(body, targets):
    assert [link.target for link in rendered_wikilinks(body)] == targets


@pytest.mark.parametrize("force_marker", (False, True))
@pytest.mark.parametrize(
    "body",
    (
        "==before note[^1] [[Design]] after==\n\n[^1]: note",
        "==before [ordinary] [[Design]] after==\n\n[ordinary]: /context?target=x",
        "==before ![ordinary] [[Design]] after==\n\n[ordinary]: https://example.com",
        "==before https://example.com [[Design]] after==",
        "==before www.example.com [[Design]] after==",
        "==before user@example.com [[Design]] after==",
        "==before https://localhost [[Design]] after==",
        "==before www.localhost [[Design]] after==",
        "==before https://例子.中国 [[Design]] after==",
        "==before https://😀.foo [[Design]] after==",
        "==before http://😀.foo [[Design]] after==",
        "==before https://a.😀 [[Design]] after==",
        "==before www.😀.foo [[Design]] after==",
        "==before https&#58;//example.com [[Design]] after==",
        r"==before https:\/\/example.com [[Design]] after==",
        "==before www&#46;example.com [[Design]] after==",
        "==before user&#64;example.com [[Design]] after==",
        "==before αuser@example.com [[Design]] after==",
        "==before &#945;user@example.com [[Design]] after==",
        "==before [[Design]] https://example.com after==",
        "==before [[Design]] www.example.com after==",
        "==before [[Design]] user@example.com after==",
        "==before $x * y$ [[Design]] after==",
        "==before $x `y`$ [[Design]] after==",
        "==before `https://localhost` [[Design]] after==",
        "==before $x$ [[Design]] after==",
        "==before [[Design]] $x$ after==",
        "==$x$[[Design]]==",
        "==before $$x$$ [[Design]] after==",
        "==before [[Design]] $$x$$ after==",
        "==$$x$$[[Design]]==",
    ),
)
def test_reader_non_text_boundaries_prevent_cross_node_highlights(
    body, force_marker, monkeypatch
):
    """These expectations come from the production remark/GFM/math reader AST."""

    if force_marker:
        monkeypatch.setattr(
            "backend.utils.markdown_links._plain_text_wikilink_fast_path",
            lambda *_args, **_kwargs: False,
        )

    links = rendered_wikilinks(body)

    assert [link.target for link in links] == ["Design"]
    assert body[links[0].source_start:links[0].source_end] == "[[Design]]"


@pytest.mark.parametrize("force_marker", (False, True))
@pytest.mark.parametrize(
    "body",
    (
        "==before [ordinary] [[Design]] after==",
        "==before ![ordinary] [[Design]] after==",
        "==before note[^1] [[Design]] after==",
        "==before xhttps://example.com [[Design]] after==",
        "==before https://bad_domain.com [[Design]] after==",
        "==before https://bad.domain_com [[Design]] after==",
        "==before https&#58;//localhost [[Design]] after==",
        "==before https&#58;//😀.foo [[Design]] after==",
        "==before https://☀.foo [[Design]] after==",
        "==before user@example.123 [[Design]] after==",
        "==before αuser&#64;example.com [[Design]] after==",
        "==before &#36;x&#36; [[Design]] after==",
        r"==before \$x\$ [[Design]] after==",
    ),
)
def test_reader_literal_non_boundaries_keep_the_highlight_consuming_wikilinks(
    body, force_marker, monkeypatch
):
    if force_marker:
        monkeypatch.setattr(
            "backend.utils.markdown_links._plain_text_wikilink_fast_path",
            lambda *_args, **_kwargs: False,
        )

    assert rendered_wikilinks(body) == ()


@pytest.mark.parametrize("force_marker", (False, True))
@pytest.mark.parametrize(
    ("body", "targets"),
    (
        ("==_x_[[Design]]==", ["Design"]),
        ("==__x__[[Design]]==", ["Design"]),
        ("==___x___[[Design]]==", ["Design"]),
        ("==*[x]*[[Design]]==", ["Design"]),
        ("==**[x]**[[Design]]==", ["Design"]),
        ("==~[x]~[[Design]]==", ["Design"]),
        ("==~~[x]~~[[Design]]==", ["Design"]),
        ("==_x_![[Design]]==", ["Design"]),
        ("==*[x]*![[Design]]==", ["Design"]),
        ("==_x_[[[Design]]==", ["[Design"]),
        ("==*[x]*[[[Design]]==", ["[Design"]),
        ("==`x`[[Design]]==", ["Design"]),
        ("==_x_[[Design]]word==", ["Design"]),
        ("==*[x]*[[Design]]word==", ["Design"]),
        ("==_word_[[Design]]punct.==", ["Design"]),
        ("==word_x_[[Design]]==", []),
        ("==word_x_![[Design]]==", []),
        ("==*[x]*word[[Design]]==", []),
        ("==word*[x]*[[Design]]==", []),
        ("==word~[x]~[[Design]]==", []),
    ),
)
def test_wikilink_source_markers_preserve_adjacent_inline_flanking(
    body, targets, force_marker, monkeypatch
):
    if force_marker:
        monkeypatch.setattr(
            "backend.utils.markdown_links._plain_text_wikilink_fast_path",
            lambda *_args, **_kwargs: False,
        )

    assert [link.target for link in rendered_wikilinks(body)] == targets


@pytest.mark.parametrize("force_marker", (False, True))
@pytest.mark.parametrize(
    ("body", "targets"),
    (
        (r"==before \$$x$ [[Design]] after==", ["Design"]),
        (r"==before \$$$x$$ [[Design]] after==", ["Design"]),
        (r"==before \$$x$$ [[Design]] after==", []),
        (r"==before \$x\$ [[Design]] after==", []),
        (r"- ==before \https://localhost [[Design]] after==", ["Design"]),
        (r"> ==before \https://localhost [[Design]] after==", ["Design"]),
        (r"==before \https://localhost [[Design]] after==", ["Design"]),
        (r"- ==before \xhttps://localhost [[Design]] after==", []),
        ("==https://a.b\u00a0x[[Design]]==", ["Design"]),
        ("==https://a.b\u2028x[[Design]]==", ["Design"]),
        ("==https://a.b x[[Design]]==", ["Design"]),
        ("==https&#58;//a.b\u00a0x[[Design]]==", []),
    ),
)
def test_reader_raw_token_boundaries_are_not_reinterpreted_after_decoding(
    body, targets, force_marker, monkeypatch
):
    if force_marker:
        monkeypatch.setattr(
            "backend.utils.markdown_links._plain_text_wikilink_fast_path",
            lambda *_args, **_kwargs: False,
        )

    links = rendered_wikilinks(body)

    assert [link.target for link in links] == targets
    assert all(
        body[link.source_start:link.source_end] == "[[Design]]" for link in links
    )


@pytest.mark.parametrize("force_marker", (False, True))
@pytest.mark.parametrize(
    ("body", "targets"),
    (
        ("==[[Design]]\ufeffwww.a.b==", ["Design"]),
        ("==[[Design]]\ufeffhttps&#58;//a.b==", ["Design"]),
        ("==[[Design]]\ufeffwww&#46;a.b==", ["Design"]),
        ("==[[Design]]\ufeffuser&#64;a.b==", ["Design"]),
        ("==[[Design]]\ufeffhttps://a.b==", ["Design"]),
        ("==[[Design]]\ufeffuser@a.b==", ["Design"]),
        ("==[[Design]]&#xfeff;www.a.b==", ["Design"]),
        ("==[[Design]]\ufeffxwww.a.b==", []),
        ("==[[Design]]\ufeffxhttps&#58;//a.b==", []),
        ("==[[Design]]\ufeff/user&#64;a.b==", []),
        ("==[[Design]]\ufeffhttps&#58;//localhost==", []),
    ),
)
def test_reader_bom_before_literal_links_preserves_custom_token_boundaries(
    body, targets, force_marker, monkeypatch
):
    if force_marker:
        monkeypatch.setattr(
            "backend.utils.markdown_links._plain_text_wikilink_fast_path",
            lambda *_args, **_kwargs: False,
        )

    assert [link.target for link in rendered_wikilinks(body)] == targets


@pytest.mark.parametrize("force_marker", (False, True))
@pytest.mark.parametrize(
    ("body", "targets"),
    (
        ("https://a.b][[Design]]==", ["Design"]),
        ("www.a.b][[Design]]==", ["Design"]),
        ("https://a.b[[https://a.b|==literal]][[Design]]", ["Design"]),
        ("https://a.b]]][[Design]]==", ["Design"]),
        ("==https://a.b](x)[[Design]]==", ["Design"]),
        ("https://a.b]x[[Design]]==", []),
        ("==https&#58;//a.b][[Design]]==", []),
    ),
)
def test_reader_raw_url_resource_trails_leave_following_wikilinks_visible(
    body, targets, force_marker, monkeypatch
):
    if force_marker:
        monkeypatch.setattr(
            "backend.utils.markdown_links._plain_text_wikilink_fast_path",
            lambda *_args, **_kwargs: False,
        )

    assert [link.target for link in rendered_wikilinks(body)] == targets


@pytest.mark.parametrize("force_marker", (False, True))
@pytest.mark.parametrize(
    ("body", "targets"),
    (
        ("==[[Design]]xhttps://a.b[[https://a.b|==literal]]==", ["Design"]),
        ("==[[Design]]xwww.a.b[[https://a.b|==literal]]==", ["Design"]),
        (
            "user@a.b[[https://a.b|==literal]]&nbsp;https://a.b][[Design]]==[user@a.b*",
            ["Design"],
        ),
        (
            r"x$$[[https://a.b|==literal]]\h[[Design]]https://a.b)][[Design]]==",
            ["Design"],
        ),
        (
            "[[https://a.b|==literal]]www.a.b**&#61;\u00a0[[[[Design]][[Design]]**",
            ["[[Design", "Design"],
        ),
        (
            "https://a.b\n\\h===\u00a0[[https://a.b|==literal]]https://a.b][[Design]]\\=",
            ["Design"],
        ),
        (
            "_$$[[https://a.b|==literal]]www.a.b~~\\h\u00a0[[[Design]]",
            ["[Design"],
        ),
        ("==[[Design]]xhttps://a.b==", []),
        ("==[[Design]]xwww.a.b==", []),
        ("==[[Design]]www.bad_domain.a==", []),
        ("==[[Design]]www.a.bad_domain==", []),
        ("==[[Design]]https://☀.foo==", []),
    ),
)
def test_rejected_reader_url_openers_do_not_skip_later_valid_openers(
    body, targets, force_marker, monkeypatch
):
    """Production GFM retries after a rejected opener, not its greedy tail."""

    if force_marker:
        monkeypatch.setattr(
            "backend.utils.markdown_links._plain_text_wikilink_fast_path",
            lambda *_args, **_kwargs: False,
        )

    assert [link.target for link in rendered_wikilinks(body)] == targets


@pytest.mark.parametrize("raw", (False, True))
@pytest.mark.parametrize(
    "body",
    (
        "https://a.b/xhttps://c.d",
        "www.a.b/xwww.c.d",
        "https://a.b/.https://c.d",
        "https://a.b/xhttps://c.d)",
    ),
)
def test_reader_url_openers_inside_accepted_tails_are_not_retried(body, raw):
    spans = (_raw_reader_url_spans if raw else _decoded_reader_url_spans)(body)

    assert spans == ((0, len(body.rstrip(")"))),)


@pytest.mark.parametrize("count", (4_000, 32_000))
def test_rejected_reader_url_tails_keep_the_plain_index_path_bounded(
    count, monkeypatch
):
    body = "==[[Design]]" + "xhttps://a.b/" * count + "[[https://a.b|==literal]]=="

    def unexpected_parse(*_args, **_kwargs):
        raise AssertionError("rejected URL tails must not force CommonMark")

    monkeypatch.setattr(RENDERED_MARKDOWN, "parse", unexpected_parse)

    assert [link.target for link in rendered_wikilinks(body)] == ["Design"]


@pytest.mark.parametrize("force_marker", (False, True))
@pytest.mark.parametrize("whitespace", ("\u2028", "\u2029", "\ufeff"))
@pytest.mark.parametrize(
    ("pattern", "targets"),
    (
        ("==_x_WS[[Design]]==", ["Design"]),
        ("==[[Design]]WS_x_==", ["Design"]),
        ("==__x__WS[[Design]]==", ["Design"]),
        ("==~x~WS[[Design]]==", ["Design"]),
        ("- ==_x_WS[[Design]]==", ["Design"]),
        ("> ==[[Design]]WS_x_==", ["Design"]),
        ("==_x_wordWS[[Design]]==", []),
        ("==[[Design]]WSword_x_==", []),
    ),
)
def test_reader_private_emphasis_flanking_uses_frontend_unicode_whitespace(
    pattern, targets, whitespace, force_marker, monkeypatch
):
    body = pattern.replace("WS", whitespace)
    if force_marker:
        monkeypatch.setattr(
            "backend.utils.markdown_links._plain_text_wikilink_fast_path",
            lambda *_args, **_kwargs: False,
        )

    assert [link.target for link in rendered_wikilinks(body)] == targets


def test_reader_private_profile_does_not_mutate_shared_parser_under_concurrent_reads():
    body = "==_x_\u2028[[Design]]=="

    def canonical_shape():
        return tuple(
            (child.type, child.content)
            for token in RENDERED_MARKDOWN.parse(body)
            for child in token.children or ()
        )

    original_shape = canonical_shape()
    original_rules = tuple(RENDERED_MARKDOWN.inline.ruler.getRules(""))

    def read(ordinal):
        if ordinal % 2:
            assert canonical_shape() == original_shape
        else:
            assert [link.target for link in rendered_wikilinks(body)] == ["Design"]

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(read, range(40)))

    assert tuple(RENDERED_MARKDOWN.inline.ruler.getRules("")) == original_rules
    assert canonical_shape() == original_shape


def test_highlight_visibility_keeps_exact_decoded_wikilink_source_spans():
    visible = r"\[\[Visible\]\]"
    body = f"&#61;&#61;hide [[Hidden]]&#61;&#61; {visible}"

    links = rendered_wikilinks(body)

    assert [link.target for link in links] == ["Visible"]
    assert body[links[0].source_start:links[0].source_end] == visible


def test_raw_html_tags_do_not_create_a_hidden_container_state_for_wikilinks():
    body = "<template>[[inside]]</template> [[outside]]"

    assert [link.target for link in rendered_wikilinks(body)] == [
        "inside",
        "outside",
    ]


def test_unclosed_inline_cdata_opener_remains_wikilink_text():
    body = "x <![CDATA[[[target]]"

    assert [link.target for link in rendered_wikilinks(body)] == ["[target"]


@pytest.mark.parametrize(
    "body",
    (
        "[[\n[[target]]",
        "[[broken\n[[target]]",
        "&#91;[\n[[target]]",
    ),
)
def test_unclosed_wikilink_does_not_hide_the_next_line(body):
    assert [link.target for link in rendered_wikilinks(body)] == ["target"]


def test_email_path_prefix_does_not_hide_a_following_wikilink():
    body = (
        "user@example.com/[[target]] "
        "https://e.test/x][[after-boundary]] "
        "https://e.test/x][&#91;after-mixed-boundary]]"
    )

    assert [link.target for link in rendered_wikilinks(body)] == [
        "target",
        "after-boundary",
        "after-mixed-boundary",
    ]


def test_rendered_markdown_links_follow_referenced_footnote_body():
    target = "11111111-1111-1111-1111-111111111111"
    destination = f"/context?target={target}"
    body = f"note[^1]\n\n[^1]: [target]({destination})"

    links = rendered_markdown_links(body)

    assert len(links) == 1
    assert links[0].destination == destination
    assert body[links[0].source_start : links[0].source_end] == "[target]"


def test_unreferenced_footnote_relationships_are_absent():
    destination = "/context?target=11111111-1111-1111-1111-111111111111"
    body = f"note\n\n[^1]: [target]({destination}) and [[alpha]]"

    assert rendered_markdown_links(body) == ()
    assert rendered_wikilinks(body) == ()


def test_known_footnote_label_case_divergence_is_stable():
    destination = "/context?target=11111111-1111-1111-1111-111111111111"
    body = f"note[^Note]\n\n[^note]: [target]({destination})"

    # remark-gfm resolves labels case-insensitively; mdit-py-plugins 0.6.1
    # leaves this definition unreferenced.
    assert rendered_markdown_links(body) == ()


def test_known_footnote_escaped_bracket_divergence_is_stable():
    destination = "/context?target=11111111-1111-1111-1111-111111111111"
    body = r"note[^a\]b]" + "\n\n" + rf"[^a\]b]: [target]({destination})"

    # remark-gfm resolves the escaped label; the Python plugin exposes this
    # inert pseudo destination instead. Keep the divergence explicit.
    assert [link.destination for link in rendered_markdown_links(body)] == [
        f"%5Btarget%5D({destination})"
    ]


def test_known_duplicate_footnote_definition_divergence_is_stable():
    first = "/context?target=11111111-1111-1111-1111-111111111111"
    second = "/context?target=22222222-2222-2222-2222-222222222222"
    body = (
        "note[^x]\n\n"
        f"[^x]: [first]({first})\n\n"
        f"[^x]: [second]({second})"
    )

    # remark-gfm uses the first definition; mdit-py-plugins 0.6.1 uses the last.
    assert [link.destination for link in rendered_markdown_links(body)] == [second]


def test_shared_analysis_rejects_a_different_body():
    analysis = RenderedMarkdownAnalysis("[[one]]")

    with pytest.raises(ValueError, match="does not match"):
        rendered_wikilinks("[[two]]", analysis=analysis)
    with pytest.raises(ValueError, match="does not match"):
        rendered_markdown_links("[two](/context)", analysis=analysis)


def test_one_megabyte_plain_wikilink_paragraph_avoids_commonmark_backtracking(
    monkeypatch,
):
    token = "[[scale_target]] "
    count = 1_000_000 // len(token)
    body = (token * count).rstrip()
    assert len(body) <= 1_000_000

    def unexpected_parse(*_args, **_kwargs):
        raise AssertionError("plain wikilink scale path must not invoke CommonMark")

    monkeypatch.setattr(RENDERED_MARKDOWN, "parse", unexpected_parse)

    wikilinks = rendered_wikilinks(body)
    assert len(wikilinks) == count
    assert wikilinks[-1].target == "scale_target"
    assert rendered_markdown_links(body) == ()


def test_plain_index_page_paragraph_avoids_commonmark_backtracking(monkeypatch):
    """A reader-plain block is text whether or not the body has one line.

    CommonMark inline parsing backtracks over unmatched brackets, which is
    quadratic in one paragraph: 32k custom tokens measured 27 s while this
    path answers the same page in 0.5 s.
    """

    body = "# Index\n\nintro paragraph\n\n" + " ".join(
        f"[[page-{ordinal}]]" for ordinal in range(4_000)
    )

    def unexpected_parse(*_args, **_kwargs):
        raise AssertionError("plain index page must not invoke CommonMark")

    monkeypatch.setattr(RENDERED_MARKDOWN, "parse", unexpected_parse)

    wikilinks = rendered_wikilinks(body)

    assert [link.target for link in wikilinks] == [
        f"page-{ordinal}" for ordinal in range(4_000)
    ]


@pytest.mark.parametrize("count", (4_000, 32_000))
@pytest.mark.parametrize("literal_boundary", (False, True))
def test_highlighted_plain_index_stays_on_the_linear_scale_path(
    count, literal_boundary, monkeypatch
):
    hidden = count // 2
    body = "# Index\n\n" + " ".join(
        f"==Read [[page_{ordinal}]] first=="
        if ordinal == hidden
        else f"[[page_{ordinal}]]"
        for ordinal in range(count)
    )
    if literal_boundary:
        body += " ==before https://localhost [[Visible]] after=="

    def unexpected_parse(*_args, **_kwargs):
        raise AssertionError("a plain highlighted index must not invoke CommonMark")

    monkeypatch.setattr(RENDERED_MARKDOWN, "parse", unexpected_parse)

    links = rendered_wikilinks(body)

    assert [link.target for link in links] == [
        f"page_{ordinal}" for ordinal in range(count) if ordinal != hidden
    ] + (["Visible"] if literal_boundary else [])
    assert all(
        body[link.source_start : link.source_end] == f"[[{link.target}]]"
        for link in links
    )


@pytest.mark.parametrize("count", (4_000, 32_000))
def test_highlight_visibility_bounds_unclosed_bracket_runs(count, monkeypatch):
    body = "[" * count + "\n==Read [[hidden]] first=="

    def unexpected_parse(*_args, **_kwargs):
        raise AssertionError("plain bracket runs must not invoke CommonMark")

    monkeypatch.setattr(RENDERED_MARKDOWN, "parse", unexpected_parse)

    assert rendered_wikilinks(body) == ()


@pytest.mark.parametrize(
    "body",
    (
        "# Index\n\n[[alpha]] [[beta]]",
        "para one [[alpha]]\n\npara two [[beta]]",
        "- list [[alpha]]\n- list [[beta]]",
        "> quoted [[alpha]]\n\nplain [[beta]]",
        "| head |\n| --- |\n| [[alpha]] |",
        "[[alpha]] and `[[beta]]` code",
        "[[alpha]]\n\n```\n[[beta]]\n```",
        "text [[alpha]] then [link](/context?target=x) then [[beta]]",
        "[see [[alpha]]](/context?target=x)\n\ntail [[beta]]",
        "$[[alpha]]$ math\n\n[[beta]] after",
        "<div>\n[[alpha]]\n</div>\n\n[[beta]] after",
        "[[alpha]]: /context?target=x\n\n[[beta]] after",
        "www.example.com[[alpha]]\n\nplain [[beta]]",
        "https://example.com/[[alpha]] tail\n\nplain [[beta]]",
        "[[a\\]lpha]]\n\n[[beta]]",
        "[[al&#112;ha]] entity\n\n[[beta]]",
        "![[alpha]] image bang\n\n[[beta]]",
        "[[alpha|Alias]] aliased\n\n[[beta]]",
        "*[[alpha]]* emphasis\n\n[[beta]]",
        "[[alpha [^1]]] footnote-ish\n\n[^1]: note\n\n[[beta]]",
        "[[alpha]] ==hide [[hidden]]== [[beta]]",
        "&#61;&#61;hide [[hidden]]&#61;&#61; [[beta]]",
        r"\=\=hide [[hidden]]\=\= [[beta]]",
        "==before *format* [[alpha]] after== [[beta]]",
        "==before _format_ [[alpha]] after== [[beta]]",
        "==before `code` [[alpha]] after== [[beta]]",
        "==before [link](/context?target=x) [[alpha]] after== [[beta]]",
        "==before\n[[alpha]]\nafter== [[beta]]",
        "[[alpha==literal==]] ==hide [[hidden]]== [[beta]]",
        "- ==hide [[hidden]]== [[beta]]",
        "| head |\n| --- |\n| ==hide [[hidden]]== [[beta]] |",
    ),
)
def test_plain_block_fast_path_agrees_with_the_marker_round_trip(body, monkeypatch):
    """The fast path is an optimization, never a different reading."""

    fast = rendered_wikilinks(body)

    monkeypatch.setattr(
        "backend.utils.markdown_links._plain_text_wikilink_fast_path",
        lambda *_args, **_kwargs: False,
    )
    slow = rendered_wikilinks(body)

    assert fast == slow


def test_url_literal_prefix_check_scales_linearly():
    """Custom tokens packed without whitespace stay near-linear.

    Each candidate used to re-scan its whole preceding token, so a body
    without whitespace was quadratic: 16k tokens measured 176 s.
    """

    def elapsed_for(count: int) -> float:
        # Best of three with the collector paused: a single timing picks up
        # gen-2 collections whose cost follows the whole test process's heap,
        # which alone pushed a linear 4x step past 8x under a full suite.
        body = "x".join(f"[[page-{ordinal}]]" for ordinal in range(count))
        timings = []
        gc.collect()
        gc.disable()
        try:
            for _ in range(3):
                start = perf_counter()
                wikilinks = rendered_wikilinks(body)
                timings.append(perf_counter() - start)
                assert len(wikilinks) == count
        finally:
            gc.enable()
        return min(timings)

    small = elapsed_for(2_000)
    large = elapsed_for(8_000)

    # Linear work grows with the 4x input; quadratic work grows about 16x.
    assert large < max(small, 0.01) * 8


def test_url_literal_prefix_check_still_rejects_bare_url_tokens():
    body = "https://example.com/[[alpha]]x[[beta]]"

    assert [link.target for link in rendered_wikilinks(body)] == []

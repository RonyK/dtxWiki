from __future__ import annotations

import base64
import unicodedata
from collections.abc import Iterator, Mapping
from decimal import Decimal
from typing import Any

import pytest

from backend.services.conversion_result_validator import (
    SANITIZER_VERSION,
    validate_conversion_result,
)
from backend.spi.conversion import (
    ConversionError,
    ConversionLimits,
    ConversionResult,
    ConversionWarning,
    ProducedAsset,
    SourceLocation,
)

_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _limits(**overrides: Any) -> ConversionLimits:
    values: dict[str, Any] = {
        "max_input_bytes": 1024,
        "max_output_bytes": 4096,
        "max_asset_bytes": 4096,
        "max_assets": 8,
        "max_nodes": 128,
        "max_depth": 8,
        "max_cell_bytes": 256,
    }
    values.update(overrides)
    return ConversionLimits(**values)


def _unsafe_result(**overrides: Any) -> ConversionResult:
    values: dict[str, Any] = {
        "markdown": "safe\n",
        "assets": (),
        "metadata": {},
        "locations": (),
        "warnings": (),
        "completeness": "complete",
        "quality_score": None,
    }
    values.update(overrides)
    result = object.__new__(ConversionResult)
    for key, value in values.items():
        object.__setattr__(result, key, value)
    return result


def _asset(
    *,
    role: str = "preview",
    media_type: str = "text/plain",
    content: bytes = b"safe",
    filename: str | None = "preview.txt",
    ordinal: int = 0,
) -> ProducedAsset:
    return ProducedAsset(
        role=role,  # type: ignore[arg-type]
        media_type=media_type,
        content=content,
        filename=filename,
        ordinal=ordinal,
    )


def _unsafe_asset(**overrides: Any) -> ProducedAsset:
    values: dict[str, Any] = {
        "role": "preview",
        "media_type": "text/plain",
        "content": b"safe",
        "filename": "preview.txt",
        "ordinal": 0,
    }
    values.update(overrides)
    asset = object.__new__(ProducedAsset)
    for key, value in values.items():
        object.__setattr__(asset, key, value)
    return asset


def _unsafe_location(**overrides: Any) -> SourceLocation:
    values: dict[str, Any] = {
        "kind": "page",
        "ordinal": 0,
        "label": None,
        "data": {},
    }
    values.update(overrides)
    location = object.__new__(SourceLocation)
    for key, value in values.items():
        object.__setattr__(location, key, value)
    return location


def test_sanitizer_escapes_raw_html_comments_multiline_tags_and_event_attrs() -> None:
    result = ConversionResult(
        markdown=(
            "<!-- hidden -->\n"
            '<img\n src="x"\n onerror="alert(1)">\n'
            "<script>alert(2)</script>\n"
            '<br><BR /><br onmouseover="alert(3)">\n'
        )
    )

    validated = validate_conversion_result(result, limits=_limits())

    assert SANITIZER_VERSION == "dtxwiki-markdown-v3"
    assert "<!--" not in validated.markdown
    assert "<img" not in validated.markdown
    assert "<script" not in validated.markdown
    assert "&lt;!-- hidden --&gt;" in validated.markdown
    assert "&lt;img\n src=" in validated.markdown
    assert "&lt;br onmouseover=" in validated.markdown
    assert validated.markdown.count("<br>") == 2
    assert [warning.code for warning in validated.warnings] == [
        "unsafe_markup_sanitized"
    ]


def test_sanitizer_preserves_dynamic_fenced_code_body_exactly() -> None:
    internal_fence = "`" * 3
    shorter_fence = "`" * 4
    outer_fence = "`" * (len(shorter_fence) + 1)
    longer_closer = outer_fence + "`"
    body = (
        f"literal {internal_fence} marker\n"
        f"{shorter_fence}\n"
        "<script>inside()</script>\n"
        "[inside](javascript:alert(1))\n"
        "literal &lt; stays encoded\n"
    )
    markdown = f"{outer_fence}html\n{body}{longer_closer}\n"

    validated = validate_conversion_result(
        ConversionResult(markdown=markdown), limits=_limits()
    )

    assert validated.markdown == markdown
    assert validated.warnings == ()


def test_sanitizer_preserves_body_but_sanitizes_info_and_post_fence_markup() -> None:
    outer_fence = "`" * 4
    opening = f"{outer_fence}html <unsafe-info> [info](javascript:alert(0))\n"
    body = "<script>inside()</script>\n[inside](javascript:alert(1))\n~~~\n```\n"
    outside = "<script>outside()</script>\n[outside](javascript:alert(2))\n"
    markdown = f"{opening}{body}{outer_fence}\n{outside}"

    validated = validate_conversion_result(
        ConversionResult(markdown=markdown), limits=_limits()
    )

    assert validated.markdown is not None
    sanitized_opening, remainder = validated.markdown.split("\n", 1)
    body_end = remainder.index(f"{outer_fence}\n")
    preserved_body = remainder[:body_end]
    sanitized_outside = remainder[body_end + len(outer_fence) + 1 :]
    assert "<unsafe-info>" not in sanitized_opening
    assert "&lt;unsafe-info&gt;" in sanitized_opening
    assert "javascript:" not in sanitized_opening
    assert "blocked:" in sanitized_opening
    assert preserved_body == body
    assert "<script>outside" not in sanitized_outside
    assert "&lt;script&gt;outside" in sanitized_outside
    assert "javascript:" not in sanitized_outside
    assert "blocked:" in sanitized_outside
    assert [warning.code for warning in validated.warnings] == [
        "unsafe_markup_sanitized"
    ]


def test_sanitizer_preserves_unclosed_indented_tilde_fence_body_to_eof() -> None:
    markdown = (
        "   ~~~text\r\n<script>inside()</script>\r\n[inside](javascript:alert(1))"
    )

    validated = validate_conversion_result(
        ConversionResult(markdown=markdown), limits=_limits()
    )

    assert validated.markdown == markdown
    assert validated.warnings == ()


def test_sanitizer_resumes_sanitizing_after_longer_indented_tilde_closer() -> None:
    body = "<script>inside()</script>\n[inside](javascript:alert(1))\n"
    markdown = f"~~~text\n{body}   ~~~~\n<script>outside()</script>\n"

    validated = validate_conversion_result(
        ConversionResult(markdown=markdown), limits=_limits()
    )

    assert validated.markdown is not None
    assert body in validated.markdown
    assert "<script>outside" not in validated.markdown
    assert "&lt;script&gt;outside" in validated.markdown


def test_sanitizer_replaces_nul_everywhere_but_keeps_code_angles() -> None:
    markdown = (
        "```text\n"
        "inside\x00<script>kept</script>\n"
        "```\n"
        "outside\x00<script>sanitized</script>\n"
    )

    validated = validate_conversion_result(
        ConversionResult(markdown=markdown), limits=_limits()
    )

    assert validated.markdown is not None
    assert "\x00" not in validated.markdown
    assert "inside\ufffd<script>kept</script>" in validated.markdown
    assert "outside\ufffd&lt;script&gt;sanitized&lt;/script&gt;" in validated.markdown


@pytest.mark.parametrize("opening", ["``", "    ```", "```lang`invalid"])
def test_sanitizer_does_not_treat_invalid_fence_openers_as_code(opening) -> None:
    markdown = f"{opening}\n<script>outside code</script>\n"

    validated = validate_conversion_result(
        ConversionResult(markdown=markdown), limits=_limits()
    )

    assert validated.markdown is not None
    assert "<script>" not in validated.markdown
    assert "&lt;script&gt;outside code&lt;/script&gt;" in validated.markdown


def test_sanitizer_neutralizes_obfuscated_markdown_uri_schemes() -> None:
    result = ConversionResult(
        markdown=(
            "[one](javascript:alert(1))\n"
            "![two](java&#x73;cript:alert(2))\n"
            "[three](%256aavascript:alert(3))\n"
            "[four](java\n\tscript:alert(4))\n"
            "[five](<data:text/html,bad>)\n"
            "[six][unsafe]\n"
            "[unsafe]: vbscript:msgbox(1)\n"
            "[safe](https://example.test/a?b=c)\n"
        )
    )

    validated = validate_conversion_result(result, limits=_limits())

    assert validated.markdown is not None
    assert validated.markdown.count("blocked:") == 6
    assert "https://example.test/a?b=c" in validated.markdown
    assert "javascript:" not in validated.markdown.casefold()
    assert "vbscript:" not in validated.markdown.casefold()
    assert "data:text" not in validated.markdown.casefold()


def test_sanitizer_rechecks_expanded_output_size() -> None:
    with pytest.raises(
        ConversionError, match="output exceeds the configured limit"
    ) as caught:
        validate_conversion_result(
            ConversionResult(markdown="<" * 10),
            limits=_limits(max_output_bytes=10, max_cell_bytes=10),
        )

    assert caught.value.code == "converter_output_limit_exceeded"
    assert caught.value.retryable is False


def test_valid_assets_are_copied_normalized_and_content_checked() -> None:
    decomposed_name = unicodedata.normalize("NFD", "미리보기.png")
    result = ConversionResult(
        markdown="preview\n",
        assets=(
            _asset(
                media_type="image/png",
                content=_PNG_1X1,
                filename=decomposed_name,
            ),
            _asset(
                role="metadata",
                media_type="application/json",
                content=b'{"width":1,"nested":[true,null]}',
                filename="metadata.json",
                ordinal=0,
            ),
        ),
    )

    validated = validate_conversion_result(result, limits=_limits())

    assert validated.assets[0].filename == "미리보기.png"
    assert validated.assets[0].content is _PNG_1X1
    assert validated.assets[1].media_type == "application/json"


def test_asset_count_and_total_size_are_bounded() -> None:
    with pytest.raises(ConversionError) as count_error:
        validate_conversion_result(
            ConversionResult(markdown=None, assets=(_asset(), _asset(ordinal=1))),
            limits=_limits(max_assets=1),
        )
    assert count_error.value.code == "converter_asset_count_exceeded"

    with pytest.raises(ConversionError) as size_error:
        validate_conversion_result(
            ConversionResult(
                markdown=None,
                assets=(
                    _asset(content=b"1234"),
                    _asset(role="asset", content=b"5678", ordinal=0),
                ),
            ),
            limits=_limits(max_asset_bytes=7),
        )
    assert size_error.value.code == "converter_asset_bytes_exceeded"


@pytest.mark.parametrize(
    "asset",
    [
        _unsafe_asset(media_type="text/html"),
        _unsafe_asset(media_type="Image/PNG", content=_PNG_1X1),
        _unsafe_asset(media_type="image/png", content=b"not-a-png"),
        _unsafe_asset(media_type="application/json", content=b'{"x":NaN}'),
        _unsafe_asset(media_type="application/json", content=b'{"x":1,"x":2}'),
        _unsafe_asset(media_type="text/plain", content=b"bad\x00text"),
        _unsafe_asset(filename="../preview.txt"),
        _unsafe_asset(filename="C:\\preview.txt"),
        _unsafe_asset(filename="CON.txt"),
        _unsafe_asset(filename="preview.txt."),
        _unsafe_asset(role="shell"),
        _unsafe_asset(ordinal=-1),
    ],
)
def test_adversarial_asset_payloads_are_rejected(asset: ProducedAsset) -> None:
    with pytest.raises(ConversionError):
        validate_conversion_result(
            ConversionResult(markdown=None, assets=(asset,)),
            limits=_limits(),
        )


def test_mutable_asset_content_and_duplicate_ordinals_are_rejected() -> None:
    mutable = _unsafe_result(
        assets=(
            _unsafe_asset(
                content=bytearray(b"mutable"),  # type: ignore[arg-type]
            ),
        )
    )
    with pytest.raises(ConversionError) as mutable_error:
        validate_conversion_result(mutable, limits=_limits())
    assert mutable_error.value.code == "converter_result_invalid"

    with pytest.raises(ConversionError) as duplicate_error:
        validate_conversion_result(
            ConversionResult(
                markdown=None,
                assets=(_asset(), _asset(filename="other.txt")),
            ),
            limits=_limits(),
        )
    assert duplicate_error.value.code == "converter_asset_ordinal_duplicate"


def test_locations_are_bounded_unique_and_do_not_expose_host_paths() -> None:
    valid = ConversionResult(
        markdown="page\n",
        locations=(
            SourceLocation(
                kind="page",
                ordinal=0,
                label="Page 1",
                data={"bbox": [0, 1, 2, 3]},
            ),
        ),
    )
    validated = validate_conversion_result(valid, limits=_limits())
    assert validated.locations[0].data["bbox"] == (0, 1, 2, 3)
    with pytest.raises(TypeError):
        validated.locations[0].data["new"] = 1  # type: ignore[index]

    with pytest.raises(ConversionError) as duplicate_error:
        validate_conversion_result(
            ConversionResult(
                markdown=None,
                locations=(
                    SourceLocation(kind="page", ordinal=0),
                    SourceLocation(kind="page", ordinal=0),
                ),
            ),
            limits=_limits(),
        )
    assert duplicate_error.value.code == "converter_location_ordinal_duplicate"

    with pytest.raises(ConversionError) as path_error:
        validate_conversion_result(
            ConversionResult(
                markdown=None,
                locations=(
                    SourceLocation(
                        kind="page",
                        ordinal=0,
                        data={"source_path": "C:\\secret\\source.pdf"},
                    ),
                ),
            ),
            limits=_limits(),
        )
    assert path_error.value.code == "converter_location_path_denied"
    assert "secret" not in str(path_error.value)


def test_location_count_invalid_literals_and_labels_are_rejected() -> None:
    with pytest.raises(ConversionError) as count_error:
        validate_conversion_result(
            ConversionResult(
                markdown=None,
                locations=(
                    SourceLocation(kind="page", ordinal=0),
                    SourceLocation(kind="page", ordinal=1),
                ),
            ),
            limits=_limits(max_nodes=1),
        )
    assert count_error.value.code == "converter_location_limit_exceeded"

    for location in (
        _unsafe_location(kind="PAGE"),
        _unsafe_location(ordinal=True),
        SourceLocation(kind="page", ordinal=0, label="x" * 257),
    ):
        with pytest.raises(ConversionError):
            validate_conversion_result(
                ConversionResult(markdown=None, locations=(location,)),
                limits=_limits(),
            )


def test_nested_json_is_bounded_and_returned_recursively_immutable() -> None:
    result = ConversionResult(
        markdown=None,
        metadata={"nested": {"items": [1, {"ok": True}]}},
    )

    validated = validate_conversion_result(result, limits=_limits())

    assert validated.metadata["nested"]["items"] == (1, {"ok": True})
    with pytest.raises(TypeError):
        validated.metadata["new"] = "value"  # type: ignore[index]
    with pytest.raises(TypeError):
        validated.metadata["nested"]["new"] = "value"  # type: ignore[index]

    too_deep: object = "leaf"
    for _index in range(10):
        too_deep = {"child": too_deep}
    with pytest.raises(ConversionError) as depth_error:
        validate_conversion_result(
            ConversionResult(markdown=None, metadata={"root": too_deep}),
            limits=_limits(max_depth=4),
        )
    assert depth_error.value.code == "converter_json_limit_exceeded"


def test_cycles_non_json_values_and_sensitive_metadata_paths_are_rejected() -> None:
    cycle: dict[str, object] = {}
    cycle["self"] = cycle
    for result in (
        _unsafe_result(metadata=cycle),
        _unsafe_result(metadata={"bad": {1, 2}}),
        ConversionResult(
            markdown=None,
            metadata={"storage_ref": "file:///private/source"},
        ),
    ):
        with pytest.raises(ConversionError):
            validate_conversion_result(result, limits=_limits())


def test_result_runtime_literals_quality_and_warning_bounds_are_strict() -> None:
    invalid_results = (
        _unsafe_result(assets=[]),
        _unsafe_result(locations=[]),
        _unsafe_result(warnings=[]),
        _unsafe_result(completeness="partial"),
        _unsafe_result(quality_score=0.5),
        _unsafe_result(quality_score=Decimal("NaN")),
        _unsafe_result(quality_score=Decimal("1.01")),
    )
    for result in invalid_results:
        with pytest.raises(ConversionError) as caught:
            validate_conversion_result(result, limits=_limits())
        assert str(caught.value) == "The converter returned an invalid result."

    warnings = tuple(
        ConversionWarning(code=f"warning-{index}", message="bounded")
        for index in range(3)
    )
    with pytest.raises(ConversionError) as warning_error:
        validate_conversion_result(
            ConversionResult(markdown=None, warnings=warnings),
            limits=_limits(max_nodes=2),
        )
    assert warning_error.value.code == "converter_warning_limit_exceeded"


class _ExplodingMapping(Mapping[str, object]):
    def __getitem__(self, _key: str) -> object:
        raise RuntimeError("private provider detail")

    def __iter__(self) -> Iterator[str]:
        raise RuntimeError("private provider detail")

    def __len__(self) -> int:
        raise RuntimeError("private provider detail")


def test_unexpected_provider_exception_is_mapped_to_safe_error() -> None:
    with pytest.raises(ConversionError) as caught:
        validate_conversion_result(
            _unsafe_result(metadata=_ExplodingMapping()),
            limits=_limits(),
        )

    assert caught.value.code == "converter_result_invalid"
    assert str(caught.value) == "The converter returned an invalid result."
    assert "private" not in str(caught.value)

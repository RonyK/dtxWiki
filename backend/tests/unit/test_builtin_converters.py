import io
import json
from dataclasses import dataclass

import pytest

from backend.converters.builtin_structured import (
    STRUCTURED_CONVERTER_MAX_INPUT_BYTES,
    BuiltinStructuredConverter,
)
from backend.converters.builtin_text import BuiltinTextConverter, decode_library_text
from backend.services.conversion_result_validator import validate_conversion_result
from backend.spi.conversion import (
    ConversionError,
    ConversionLimits,
    ConversionRequest,
    ConversionResult,
    ProbeRequest,
    ProducedAsset,
    SourceLocation,
)


@dataclass
class MemoryBlob:
    content: bytes

    @property
    def byte_size(self):
        return len(self.content)

    def open_binary(self):
        return io.BytesIO(self.content)

    def copy_to(self, destination):
        destination.write_bytes(self.content)
        return len(self.content)


def request(
    content: bytes,
    format_id: str,
    *,
    limits: ConversionLimits | None = None,
    options=None,
) -> ConversionRequest:
    return ConversionRequest(
        source=MemoryBlob(content),
        source_sha256="a" * 64,
        byte_size=len(content),
        format_id=format_id,
        media_type="text/plain",
        original_filename=f"source.{format_id}",
        limits=limits or ConversionLimits(max_input_bytes=1024 * 1024),
        options=options or {},
    )


def test_markdown_converter_normalizes_newlines():
    result = BuiltinTextConverter().convert(request(b"# Title\r\nBody", "markdown"))

    assert result.markdown == "# Title\nBody\n"
    assert result.completeness == "complete"


def test_text_converter_advertises_only_closed_inert_source_formats():
    converter = BuiltinTextConverter()
    descriptor = converter.descriptor()

    assert descriptor.formats == {
        "markdown",
        "text",
        "html",
        "javascript",
    }
    assert {
        "text/html",
        "application/xhtml+xml",
        "text/javascript",
        "application/javascript",
    } <= descriptor.media_types
    for format_id in descriptor.formats:
        probe = converter.probe(
            ProbeRequest(
                format_id=format_id,
                media_type="text/plain",
                original_filename="source.txt",
                byte_size=0,
            )
        )
        assert probe.supported is True
    assert (
        converter.probe(
            ProbeRequest(
                format_id="mermaid",
                media_type="text/plain",
                original_filename="source.txt",
                byte_size=0,
            )
        ).supported
        is False
    )


def test_text_converter_rejects_unknown_format_before_rendering():
    with pytest.raises(ConversionError) as exc_info:
        BuiltinTextConverter().convert(request(b"graph TD; A-->B", "mermaid"))

    assert exc_info.value.code == "unsupported"
    assert exc_info.value.retryable is False
    assert exc_info.value.fallback_allowed is False


@pytest.mark.parametrize(
    ("format_id", "language"),
    [("html", "html"), ("javascript", "javascript")],
)
def test_active_source_stays_inside_a_fixed_non_mermaid_fence(format_id, language):
    content = (
        b'<script>window.__source_marker = "not executed"</script>\n'
        b"[source-link](javascript:alert(1))\n"
        b"```mermaid\ngraph TD; A-->B\n```\n"
    )

    converted = BuiltinTextConverter().convert(request(content, format_id))

    outer_fence = "`" * 4
    assert converted.markdown.startswith(f"{outer_fence}{language}\n")
    assert converted.markdown.endswith(f"\n{outer_fence}\n")
    assert converted.markdown.count(outer_fence) == 2
    assert "```mermaid" in converted.markdown
    assert "<script>" in converted.markdown
    assert "javascript:" in converted.markdown

    validated = validate_conversion_result(
        converted,
        limits=ConversionLimits(max_input_bytes=1024 * 1024),
    )
    assert validated.markdown == converted.markdown
    assert validated.warnings == ()


def test_csv_converter_renders_bounded_markdown_table():
    result = BuiltinStructuredConverter().convert(
        request(b"name,value\nalpha,1\nbeta,2\n", "csv")
    )

    assert "| name | value |" in result.markdown
    assert "| beta | 2 |" in result.markdown
    assert result.metadata["rows_rendered"] == 3


@pytest.mark.parametrize("format_id", ["csv", "tsv", "json", "yaml"])
def test_structured_converter_immutable_10_mb_cap_cannot_be_raised_by_policy(
    format_id,
):
    converter = BuiltinStructuredConverter()
    assert converter.descriptor().max_input_bytes == 10_000_000
    assert (
        converter.descriptor().max_input_bytes == STRUCTURED_CONVERTER_MAX_INPUT_BYTES
    )
    content = b"x" * (STRUCTURED_CONVERTER_MAX_INPUT_BYTES + 1)
    raised_policy_limit = ConversionLimits(max_input_bytes=50_000_000)

    with pytest.raises(ConversionError) as exc_info:
        converter.convert(request(content, format_id, limits=raised_policy_limit))

    assert exc_info.value.code == "input_limit_exceeded"
    assert exc_info.value.fallback_allowed is False


def test_json_converter_rejects_invalid_input():
    with pytest.raises(Exception, match="JSON document is invalid"):
        BuiltinStructuredConverter().convert(request(b"{", "json"))


def test_json_converter_emits_deterministic_sorted_markdown():
    result = BuiltinStructuredConverter().convert(
        request(json.dumps({"z": 1, "a": 2}).encode(), "json")
    )

    assert result.markdown.index('"a"') < result.markdown.index('"z"')


def test_validator_neutralizes_raw_html_and_dangerous_scheme():
    converted = BuiltinTextConverter().convert(
        request(b"<script>x</script> [bad](javascript:alert(1))", "markdown")
    )

    validated = validate_conversion_result(
        converted, limits=ConversionLimits(max_input_bytes=1024)
    )

    assert "<script>" not in validated.markdown
    assert "javascript:" not in validated.markdown
    assert any(
        warning.code == "unsafe_markup_sanitized" for warning in validated.warnings
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_input_bytes", 0),
        ("max_cell_bytes", -1),
        ("max_archive_entries", 0),
        ("max_image_pixels", -1),
        ("max_yaml_aliases", True),
        ("max_yaml_events", 0),
        ("max_archive_expansion_ratio", float("inf")),
        ("timeout_seconds", float("inf")),
        ("timeout_seconds", float("nan")),
    ],
)
def test_conversion_limits_reject_non_positive_or_non_finite_values(field, value):
    arguments = {"max_input_bytes": 1024, field: value}

    with pytest.raises(ValueError, match=field):
        ConversionLimits(**arguments)


def test_spi_json_snapshots_are_deeply_immutable_and_detached():
    mutable_options = {"nested": {"values": [1, 2]}}
    conversion_request = request(b"hello", "text", options=mutable_options)
    mutable_options["nested"]["values"].append(3)

    assert conversion_request.options["nested"]["values"] == (1, 2)
    assert json.loads(json.dumps(dict(conversion_request.options))) == {
        "nested": {"values": [1, 2]}
    }
    with pytest.raises(TypeError, match="immutable"):
        conversion_request.options["nested"]["new"] = True

    location = SourceLocation(
        kind="row", ordinal=0, data={"coordinates": [1, {"column": 2}]}
    )
    result = ConversionResult(
        markdown="ok",
        metadata={"location": {"coordinates": [1, 2]}},
        locations=(location,),
    )

    with pytest.raises(TypeError, match="immutable"):
        result.metadata["location"]["changed"] = True
    with pytest.raises(TypeError, match="immutable"):
        result.locations[0].data["coordinates"][1]["column"] = 3
    assert json.loads(json.dumps(dict(result.metadata))) == {
        "location": {"coordinates": [1, 2]}
    }


def test_spi_rejects_invalid_source_and_result_envelopes():
    with pytest.raises(ValueError, match="source identity"):
        ConversionRequest(
            source=MemoryBlob(b"hello"),
            source_sha256="not-a-hash",
            byte_size=5,
            format_id="text",
            media_type="text/plain",
            original_filename="source.txt",
            limits=ConversionLimits(max_input_bytes=10),
        )

    with pytest.raises(ValueError, match="result envelope"):
        ConversionResult(markdown="ok", completeness="invented")

    with pytest.raises(ValueError, match="filename"):
        ProducedAsset(
            role="preview",
            media_type="image/png",
            content=b"png",
            filename="../preview.png",
        )


def test_bomless_utf16_like_binary_is_rejected_but_bom_marked_utf16_is_supported():
    with pytest.raises(ConversionError) as exc_info:
        decode_library_text(b"h\x00i\x00")

    assert exc_info.value.code == "unsupported_encoding"
    decoded, encoding = decode_library_text("hi".encode("utf-16"))
    assert decoded == "hi"
    assert encoding == "utf-16"


@pytest.mark.parametrize(
    "content",
    [
        b"\x01\x02\x03",
        b"\xef\xbb\xbf\x01\x02\x03",
        "\x01\x02\x03".encode("utf-16"),
    ],
)
def test_all_supported_text_encodings_reject_binary_like_controls(content):
    with pytest.raises(ConversionError) as exc_info:
        decode_library_text(content)

    assert exc_info.value.code == "unsupported_encoding"
    assert exc_info.value.fallback_allowed is False


@pytest.mark.parametrize(
    ("format_id", "content", "opening", "closing"),
    [
        ("text", b"a" * 1_000, "```text\n", "\n```\n"),
        ("json", json.dumps({"value": "a" * 1_000}).encode(), "```json\n", "\n```\n"),
        ("yaml", b"value: " + b"a" * 1_000, "```yaml\n", "\n```\n"),
    ],
)
def test_generated_fences_remain_balanced_when_output_is_truncated(
    format_id, content, opening, closing
):
    limits = ConversionLimits(max_input_bytes=2_000, max_output_bytes=48)
    converter = (
        BuiltinTextConverter() if format_id == "text" else BuiltinStructuredConverter()
    )

    result = converter.convert(request(content, format_id, limits=limits))

    assert result.completeness == "truncated"
    assert result.markdown.startswith(opening)
    assert closing in result.markdown
    assert result.markdown.count("```") == 2
    assert len(result.markdown.encode("utf-8")) <= limits.max_output_bytes


def test_tiny_output_budget_never_emits_an_unclosed_generated_fence():
    limits = ConversionLimits(max_input_bytes=100, max_output_bytes=5)

    result = BuiltinTextConverter().convert(
        request(b"plain text", "text", limits=limits)
    )

    assert "```" not in result.markdown
    assert len(result.markdown.encode("utf-8")) <= limits.max_output_bytes


def test_csv_oversized_cell_raises_safe_conversion_error():
    limits = ConversionLimits(max_input_bytes=100_000, max_cell_bytes=64)

    with pytest.raises(ConversionError) as exc_info:
        BuiltinStructuredConverter().convert(
            request(b"name,value\nlarge," + b"x" * 65, "csv", limits=limits)
        )

    assert exc_info.value.code == "cell_limit_exceeded"
    assert exc_info.value.fallback_allowed is False


def test_csv_parser_limit_is_wrapped_as_safe_conversion_error():
    content = b"name,value\nlarge," + b"x" * 200_000
    limits = ConversionLimits(
        max_input_bytes=len(content) + 1,
        max_cell_bytes=len(content) + 1,
    )

    with pytest.raises(ConversionError) as exc_info:
        BuiltinStructuredConverter().convert(request(content, "csv", limits=limits))

    assert exc_info.value.code == "invalid_delimited"
    assert exc_info.value.safe_message == (
        "The delimited document is invalid or exceeds parser limits."
    )


def test_csv_partial_rows_and_columns_have_distinct_provenance():
    limits = ConversionLimits(
        max_input_bytes=1_024,
        max_rows=2,
        max_columns=2,
    )
    content = b"first,second,omitted\na,b,c\nnot,rendered,row\n"

    result = BuiltinStructuredConverter().convert(
        request(content, "csv", limits=limits)
    )

    warning_codes = {warning.code for warning in result.warnings}
    assert warning_codes == {"row_limit_reached", "column_limit_reached"}
    assert result.metadata["rows_rendered"] == 2
    assert result.metadata["source_rows_examined"] == 3
    assert result.metadata["row_limit_reached"] is True
    assert result.metadata["column_limit_reached"] is True
    assert result.locations[0].data == {
        "row": 1,
        "source_columns": 3,
        "partial": True,
    }


def test_yaml_invalid_syntax_is_rejected_before_rendering():
    with pytest.raises(ConversionError) as exc_info:
        BuiltinStructuredConverter().convert(request(b"value: [unterminated", "yaml"))

    assert exc_info.value.code == "invalid_yaml"


@pytest.mark.parametrize(
    ("format_id", "content"),
    [
        ("json", b'{"value": "12345"}'),
        ("yaml", b"value: 12345\n"),
    ],
)
def test_structured_scalar_byte_limit_is_enforced(format_id, content):
    limits = ConversionLimits(max_input_bytes=1_024, max_string_bytes=4)

    with pytest.raises(ConversionError) as exc_info:
        BuiltinStructuredConverter().convert(request(content, format_id, limits=limits))

    assert exc_info.value.code == "string_limit_exceeded"


def test_yaml_event_and_alias_limits_are_enforced_without_construction():
    event_limits = ConversionLimits(max_input_bytes=1_024, max_yaml_events=3)
    with pytest.raises(ConversionError) as event_error:
        BuiltinStructuredConverter().convert(
            request(b"value: 1\n", "yaml", limits=event_limits)
        )
    assert event_error.value.code == "yaml_event_limit_exceeded"

    alias_limits = ConversionLimits(max_input_bytes=1_024, max_yaml_aliases=1)
    alias_document = b"base: &base {value: 1}\na: *base\nb: *base\n"
    with pytest.raises(ConversionError) as alias_error:
        BuiltinStructuredConverter().convert(
            request(alias_document, "yaml", limits=alias_limits)
        )
    assert alias_error.value.code == "yaml_alias_limit_exceeded"

from dataclasses import FrozenInstanceError

import pytest

from backend.media.application import (
    ByteRangeNotSatisfiable,
    HttpByteRange,
    if_none_match_matches,
    if_range_allows_range,
    parse_http_byte_range,
)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("bytes=0-3", (0, 3)),
        ("bytes=5-", (5, 9)),
        ("bytes=-3", (7, 9)),
        ("bytes=-20", (0, 9)),
    ],
)
def test_shared_media_range_parser_normalizes_one_bounded_range(
    header: str,
    expected: tuple[int, int],
) -> None:
    result = parse_http_byte_range(
        header,
        complete_length=10,
        max_range_bytes=10,
    )

    assert result is not None
    assert (result.start, result.end) == expected
    assert result.length == expected[1] - expected[0] + 1
    assert result.content_range == f"bytes {expected[0]}-{expected[1]}/10"


@pytest.mark.parametrize(
    "header",
    [
        "items=0-1",
        "bytes=",
        "bytes=-0",
        "bytes=3-2",
        "bytes=10-10",
        "bytes=0-1,3-4",
        "bytes=0-5",
    ],
)
def test_shared_media_range_parser_rejects_invalid_or_unbounded_ranges(
    header: str,
) -> None:
    with pytest.raises(ByteRangeNotSatisfiable) as error:
        parse_http_byte_range(
            header,
            complete_length=10,
            max_range_bytes=5,
        )

    assert error.value.complete_length == 10


def test_shared_media_range_parser_can_window_open_ended_media_ranges() -> None:
    first = parse_http_byte_range(
        "bytes=0-",
        complete_length=100,
        max_range_bytes=16,
        clamp_open_ended=True,
    )
    later = parse_http_byte_range(
        "bytes=91-",
        complete_length=100,
        max_range_bytes=16,
        clamp_open_ended=True,
    )

    assert first is not None
    assert (first.start, first.end, first.length) == (0, 15, 16)
    assert first.content_range == "bytes 0-15/100"
    assert later is not None
    assert (later.start, later.end, later.length) == (91, 99, 9)


def test_shared_media_range_value_is_immutable() -> None:
    value = HttpByteRange(start=0, end=0, complete_length=1)

    with pytest.raises(FrozenInstanceError):
        value.start = 1  # type: ignore[misc]


def test_shared_media_etag_preconditions_use_weak_none_match_and_strong_if_range() -> (
    None
):
    etag = '"opaque-v1"'

    assert if_none_match_matches('W/"other", W/"opaque-v1"', etag)
    assert if_none_match_matches("*", etag)
    assert not if_none_match_matches('"other"', etag)
    assert if_range_allows_range(None, etag)
    assert if_range_allows_range(etag, etag)
    assert not if_range_allows_range('W/"opaque-v1"', etag)
    assert not if_range_allows_range('"other"', etag)

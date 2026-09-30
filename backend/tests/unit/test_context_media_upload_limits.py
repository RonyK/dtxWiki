import json
from dataclasses import replace
from typing import Annotated
from uuid import uuid4

import anyio
import pytest
from fastapi import APIRouter, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import Response
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from backend.api import context_media_upload_limits as upload_limits
from backend.api.context_media_upload_limits import (
    ContextMediaUploadRoute,
    _read_bounded_upload_body,
)
from backend.context_media.domain import CONTEXT_MEDIA_UPLOAD_POLICY


def _scope(*, headers=()) -> dict[str, object]:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": f"/api/v1/workspaces/{uuid4()}/context/pages/{uuid4()}/media",
        "raw_path": b"/api/v1/workspaces/test/context/pages/test/media",
        "query_string": b"",
        "root_path": "",
        "headers": list(headers),
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "state": {},
    }


def _receive(messages, calls: list[int] | None = None):
    queue = list(messages)

    async def receive():
        if calls is not None:
            calls.append(1)
        if not queue:
            raise AssertionError("bounded reader requested another body chunk")
        return queue.pop(0)

    return receive


def _multipart(boundary: bytes, parts: list[tuple[list[bytes], bytes]]) -> bytes:
    body = bytearray()
    for headers, content in parts:
        body.extend(b"--" + boundary + b"\r\n")
        for header in headers:
            body.extend(header + b"\r\n")
        body.extend(b"\r\n" + content + b"\r\n")
    body.extend(b"--" + boundary + b"--\r\n")
    return bytes(body)


def _small_policy(**updates):
    values = {
        "max_upload_bytes": 64,
        "max_request_body_bytes": 512,
        "max_multipart_parts": 2,
        "max_headers_per_part": 4,
        "max_header_line_bytes": 128,
        "spool_memory_bytes": 32,
    }
    values.update(updates)
    return replace(CONTEXT_MEDIA_UPLOAD_POLICY, **values)


def test_upload_limit_module_does_not_reexport_domain_policy() -> None:
    assert upload_limits.__all__ == ["ContextMediaUploadRoute"]
    assert not hasattr(upload_limits, "CONTEXT_MEDIA_UPLOAD_POLICY")
    assert not hasattr(upload_limits, "ContextMediaUploadPolicy")


def test_oversized_content_length_is_rejected_without_reading_body() -> None:
    policy = _small_policy(max_request_body_bytes=8)
    calls: list[int] = []
    scope = _scope(headers=[(b"content-length", b"9")])

    body, response = anyio.run(
        lambda: _read_bounded_upload_body(
            scope,
            _receive([], calls),
            policy=policy,
        )
    )

    assert body is None
    assert response is not None and response.status_code == 413
    assert json.loads(response.body)["error_code"] == "media_too_large"
    assert calls == []


def test_pathological_content_length_is_rejected_without_integer_conversion() -> None:
    scope = _scope(headers=[(b"content-length", b"9" * 10_000)])
    calls: list[int] = []

    body, response = anyio.run(
        lambda: _read_bounded_upload_body(
            scope,
            _receive([], calls),
            policy=_small_policy(),
        )
    )

    assert body is None
    assert response is not None and response.status_code == 400
    assert json.loads(response.body)["error_code"] == "validation_failed"
    assert calls == []


def test_duplicate_content_type_is_rejected_before_body_or_any_multipart_parser(
    monkeypatch,
) -> None:
    app = FastAPI()
    router = APIRouter(route_class=ContextMediaUploadRoute)
    endpoint_called = False
    raw_parser_called = False
    fastapi_parser_called = False

    @router.post("/workspaces/{workspace_id}/context/pages/{page_id}/media")
    async def upload(
        workspace_id: str,
        page_id: str,
        file: Annotated[UploadFile, File()],
        marker_uuid: Annotated[str, Form()],
    ) -> Response:
        del workspace_id, page_id, file, marker_uuid
        nonlocal endpoint_called
        endpoint_called = True
        return Response(status_code=204)

    app.include_router(router, prefix="/api/v1")

    def unexpected_raw_parse(*args, **kwargs):
        del args, kwargs
        nonlocal raw_parser_called
        raw_parser_called = True
        raise AssertionError(
            "raw multipart validation started for ambiguous Content-Type"
        )

    async def unexpected_fastapi_parse(*args, **kwargs):
        del args, kwargs
        nonlocal fastapi_parser_called
        fastapi_parser_called = True
        raise AssertionError(
            "FastAPI multipart parser started for ambiguous Content-Type"
        )

    monkeypatch.setattr(
        upload_limits,
        "_validate_multipart_envelope",
        unexpected_raw_parse,
    )
    monkeypatch.setattr(
        "starlette.formparsers.MultiPartParser.parse",
        unexpected_fastapi_parse,
    )
    scope = _scope(
        headers=[
            (b"content-type", b"multipart/form-data; boundary=first"),
            (b"content-type", b"multipart/form-data; boundary=second"),
        ]
    )
    calls: list[int] = []
    sent: list[dict[str, object]] = []

    async def exercise() -> None:
        async def send(message):
            sent.append(message)

        await app(scope, _receive([], calls), send)

    anyio.run(exercise)

    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 400
    assert endpoint_called is False
    assert raw_parser_called is False
    assert fastapi_parser_called is False
    assert calls == []


def test_chunked_overflow_stops_before_raw_or_fastapi_multipart_parsing(
    monkeypatch,
) -> None:
    policy = _small_policy(max_request_body_bytes=8)
    calls: list[int] = []

    def unexpected_raw_parse(*args, **kwargs):
        del args, kwargs
        raise AssertionError("raw multipart validation started before size admission")

    monkeypatch.setattr(
        upload_limits,
        "_validate_multipart_envelope",
        unexpected_raw_parse,
    )
    scope = _scope(
        headers=[(b"content-type", b"multipart/form-data; boundary=bounded")]
    )
    messages = [
        {"type": "http.request", "body": b"12345678", "more_body": True},
        {"type": "http.request", "body": b"9", "more_body": False},
    ]

    body, response = anyio.run(
        lambda: _read_bounded_upload_body(
            scope,
            _receive(messages, calls),
            policy=policy,
        )
    )

    assert body is None
    assert response is not None and response.status_code == 413
    assert calls == [1, 1]


def test_upload_route_rejects_chunked_overflow_before_fastapi_form_parser(
    monkeypatch,
) -> None:
    app = FastAPI()
    router = APIRouter(route_class=ContextMediaUploadRoute)
    endpoint_called = False
    fastapi_parser_called = False

    @router.post("/workspaces/{workspace_id}/context/pages/{page_id}/media")
    async def upload(
        workspace_id: str,
        page_id: str,
        file: Annotated[UploadFile, File()],
        marker_uuid: Annotated[str, Form()],
    ) -> Response:
        del workspace_id, page_id, file, marker_uuid
        nonlocal endpoint_called
        endpoint_called = True
        return Response(status_code=204)

    app.include_router(router, prefix="/api/v1")
    monkeypatch.setattr(
        upload_limits,
        "_CONTEXT_MEDIA_UPLOAD_POLICY",
        _small_policy(max_request_body_bytes=8),
    )

    async def unexpected_fastapi_parse(*args, **kwargs):
        del args, kwargs
        nonlocal fastapi_parser_called
        fastapi_parser_called = True
        raise AssertionError("FastAPI multipart parser started before admission")

    monkeypatch.setattr(
        "starlette.formparsers.MultiPartParser.parse",
        unexpected_fastapi_parse,
    )
    scope = _scope(
        headers=[(b"content-type", b"multipart/form-data; boundary=bounded")]
    )
    messages = [
        {"type": "http.request", "body": b"12345678", "more_body": True},
        {"type": "http.request", "body": b"9", "more_body": False},
    ]
    sent: list[dict[str, object]] = []

    async def exercise() -> None:
        async def send(message):
            sent.append(message)

        await app(scope, _receive(messages), send)

    anyio.run(exercise)

    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413
    assert endpoint_called is False
    assert fastapi_parser_called is False


def test_valid_bounded_multipart_is_replayed_byte_for_byte() -> None:
    boundary = b"context-boundary"
    marker = str(uuid4()).encode()
    raw = _multipart(
        boundary,
        [
            ([b'Content-Disposition: form-data; name="marker_uuid"'], marker),
            (
                [
                    b'Content-Disposition: form-data; name="file"; filename="a.png"',
                    b"Content-Type: image/png",
                ],
                b"small-png",
            ),
        ],
    )
    scope = _scope(
        headers=[
            (b"content-type", b"multipart/form-data; boundary=" + boundary),
            (b"content-length", str(len(raw)).encode()),
        ]
    )
    messages = [
        {"type": "http.request", "body": raw[:37], "more_body": True},
        {"type": "http.request", "body": raw[37:], "more_body": False},
    ]

    body, response = anyio.run(
        lambda: _read_bounded_upload_body(
            scope,
            _receive(messages),
            policy=_small_policy(),
        )
    )

    assert response is None
    assert body is not None

    async def replay() -> bytes:
        chunks: list[bytes] = []
        while True:
            message = await body.receive()
            chunks.append(message["body"])
            if not message["more_body"]:
                break
        await body.close()
        return b"".join(chunks)

    assert anyio.run(replay) == raw


def test_multipart_part_count_is_bounded_before_fastapi_parsing() -> None:
    boundary = b"part-limit"
    parts = [
        ([f'Content-Disposition: form-data; name="field{index}"'.encode()], b"x")
        for index in range(3)
    ]
    raw = _multipart(boundary, parts)
    scope = _scope(
        headers=[(b"content-type", b"multipart/form-data; boundary=" + boundary)]
    )

    body, response = anyio.run(
        lambda: _read_bounded_upload_body(
            scope,
            _receive([{"type": "http.request", "body": raw, "more_body": False}]),
            policy=_small_policy(),
        )
    )

    assert body is None
    assert response is not None and response.status_code == 413
    assert json.loads(response.body)["error_code"] == "request_too_large"


def test_multipart_part_size_is_bounded_before_fastapi_parsing() -> None:
    boundary = b"part-size-limit"
    raw = _multipart(
        boundary,
        [([b'Content-Disposition: form-data; name="file"'], b"12345")],
    )
    scope = _scope(
        headers=[(b"content-type", b"multipart/form-data; boundary=" + boundary)]
    )

    body, response = anyio.run(
        lambda: _read_bounded_upload_body(
            scope,
            _receive([{"type": "http.request", "body": raw, "more_body": False}]),
            policy=_small_policy(max_upload_bytes=4),
        )
    )

    assert body is None
    assert response is not None and response.status_code == 413
    assert json.loads(response.body)["error_code"] == "request_too_large"


def test_multipart_header_line_is_bounded_before_fastapi_parsing() -> None:
    boundary = b"header-limit"
    raw = _multipart(
        boundary,
        [
            (
                [
                    b'Content-Disposition: form-data; name="file"; filename="'
                    + (b"a" * 140)
                    + b'"'
                ],
                b"x",
            )
        ],
    )
    scope = _scope(
        headers=[(b"content-type", b"multipart/form-data; boundary=" + boundary)]
    )

    body, response = anyio.run(
        lambda: _read_bounded_upload_body(
            scope,
            _receive([{"type": "http.request", "body": raw, "more_body": False}]),
            policy=_small_policy(),
        )
    )

    assert body is None
    assert response is not None and response.status_code == 413
    assert json.loads(response.body)["error_code"] == "request_too_large"


def test_multipart_headers_per_part_are_bounded_before_fastapi_parsing() -> None:
    boundary = b"header-count-limit"
    raw = _multipart(
        boundary,
        [
            (
                [
                    b'Content-Disposition: form-data; name="file"',
                    b"X-One: 1",
                    b"X-Two: 2",
                    b"X-Three: 3",
                    b"X-Four: 4",
                ],
                b"x",
            )
        ],
    )
    scope = _scope(
        headers=[(b"content-type", b"multipart/form-data; boundary=" + boundary)]
    )

    body, response = anyio.run(
        lambda: _read_bounded_upload_body(
            scope,
            _receive([{"type": "http.request", "body": raw, "more_body": False}]),
            policy=_small_policy(),
        )
    )

    assert body is None
    assert response is not None and response.status_code == 413
    assert json.loads(response.body)["error_code"] == "request_too_large"


def test_rolled_spools_close_after_stream_structure_and_disconnect_rejections(
    monkeypatch,
) -> None:
    real_spooled_temporary_file = upload_limits.SpooledTemporaryFile
    spools = []

    def tracked_spool(*args, **kwargs):
        spool = real_spooled_temporary_file(*args, **kwargs)
        spools.append(spool)
        return spool

    monkeypatch.setattr(upload_limits, "SpooledTemporaryFile", tracked_spool)
    overflow_policy = _small_policy(
        max_request_body_bytes=8,
        spool_memory_bytes=1,
    )
    body, response = anyio.run(
        lambda: _read_bounded_upload_body(
            _scope(),
            _receive(
                [
                    {"type": "http.request", "body": b"12345678", "more_body": True},
                    {"type": "http.request", "body": b"9", "more_body": False},
                ]
            ),
            policy=overflow_policy,
        )
    )
    assert body is None
    assert response is not None and response.status_code == 413
    assert spools[-1]._rolled is True
    assert spools[-1].closed is True

    boundary = b"rolled-structure-limit"
    raw = _multipart(
        boundary,
        [
            ([f'Content-Disposition: form-data; name="field{index}"'.encode()], b"x")
            for index in range(3)
        ],
    )
    body, response = anyio.run(
        lambda: _read_bounded_upload_body(
            _scope(
                headers=[
                    (b"content-type", b"multipart/form-data; boundary=" + boundary)
                ]
            ),
            _receive([{"type": "http.request", "body": raw, "more_body": False}]),
            policy=_small_policy(spool_memory_bytes=1),
        )
    )
    assert body is None
    assert response is not None and response.status_code == 413
    assert spools[-1]._rolled is True
    assert spools[-1].closed is True

    with pytest.raises(ClientDisconnect):
        anyio.run(
            lambda: _read_bounded_upload_body(
                _scope(),
                _receive(
                    [
                        {"type": "http.request", "body": b"12", "more_body": True},
                        {"type": "http.disconnect"},
                    ]
                ),
                policy=_small_policy(spool_memory_bytes=1),
            )
        )
    assert spools[-1]._rolled is True
    assert spools[-1].closed is True


def test_unrelated_route_keeps_original_receive_path() -> None:
    app = FastAPI()
    router = APIRouter(route_class=ContextMediaUploadRoute)

    @router.post("/unrelated")
    async def unrelated(request: Request) -> Response:
        del request
        return Response(status_code=204)

    app.include_router(router)
    client = TestClient(app)

    response = client.post(
        "/unrelated",
        content=b"x",
        headers={
            "Content-Length": str(
                CONTEXT_MEDIA_UPLOAD_POLICY.max_request_body_bytes + 1
            )
        },
    )

    assert response.status_code == 204

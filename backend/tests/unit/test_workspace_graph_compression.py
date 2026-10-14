from __future__ import annotations

import asyncio
import gzip
import json
import random
import threading

import pytest
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from backend.api.middleware import workspace_graph_compression
from backend.api.middleware.workspace_graph_compression import (
    WorkspaceGraphCompressionMiddleware,
)

_PATH = "/api/v1/workspaces/workspace-one/graph/overview/query"
_BODY = json.dumps(
    {
        "documents": [{"id": "original-one", "title": "원본 문서"}] * 40,
        "relations": [{"source": "original-one", "target": "original-two"}] * 40,
        "cursor": "opaque-cursor",
        "omission": {"reason": "privacy"},
    },
    ensure_ascii=False,
).encode("utf-8")


def _scope(*, headers: list[tuple[bytes, bytes]] | None = None, **overrides) -> Scope:
    return {
        "type": "http",
        "method": "POST",
        "path": _PATH,
        "headers": [(b"accept-encoding", b"gzip")] if headers is None else headers,
        **overrides,
    }


def _messages(
    body: bytes = _BODY,
    *,
    status: int = 200,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> list[Message]:
    return [
        {
            "type": "http.response.start",
            "status": status,
            "headers": (
                [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ]
                if headers is None
                else headers
            ),
        },
        {"type": "http.response.body", "body": body, "more_body": False},
    ]


def _app(messages: list[Message]) -> ASGIApp:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        for message in messages:
            await send(message)

    return app


async def _run(app: ASGIApp, scope: Scope) -> list[Message]:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


def _response(
    messages: list[Message] | None = None,
    *,
    scope: Scope | None = None,
    minimum_size: int = 1000,
) -> list[Message]:
    return asyncio.run(
        _run(
            WorkspaceGraphCompressionMiddleware(
                _app(_messages() if messages is None else messages),
                minimum_size=minimum_size,
            ),
            _scope() if scope is None else scope,
        )
    )


def _header_values(message: Message, name: bytes) -> list[bytes]:
    return [value for key, value in message["headers"] if key.lower() == name]


def test_gzip_and_identity_preserve_every_serialized_byte():
    original = _messages()
    compressed = _response(original)
    identity = _response(original, scope=_scope(headers=[]))

    assert gzip.decompress(compressed[1]["body"]) == identity[1]["body"] == _BODY
    assert compressed[1]["body"] == gzip.compress(_BODY, compresslevel=6, mtime=0)
    assert len(compressed[1]["body"]) < len(_BODY)
    assert _header_values(compressed[0], b"content-encoding") == [b"gzip"]
    assert _header_values(compressed[0], b"content-length") == [
        str(len(compressed[1]["body"])).encode("ascii")
    ]
    assert _header_values(identity[0], b"content-length") == [
        str(len(_BODY)).encode("ascii")
    ]
    assert _header_values(identity[0], b"content-encoding") == []
    assert _header_values(identity[0], b"vary") == [b"Accept-Encoding"]
    assert _header_values(compressed[0], b"vary") == [b"Accept-Encoding"]
    assert original == _messages()


@pytest.mark.parametrize(
    "headers",
    [
        [(b"accept-encoding", b"gzip")],
        [(b"Accept-Encoding", b"GZIP; Q = 0.001")],
        [(b"accept-encoding", b"br, gzip;q=0.5")],
        [(b"accept-encoding", b"gzip;q=1.000")],
        [(b"accept-encoding", b"*")],
        [(b"accept-encoding", b"*;q=0.5")],
        [(b"accept-encoding", b"*;q=0, gzip;q=0.2")],
        [(b"accept-encoding", b"gzip;q=0.8, gzip;q=0.2")],
        [(b"accept-encoding", b"br"), (b"accept-encoding", b"gzip;q=0.5")],
    ],
)
def test_negotiated_positive_quality_compresses(headers):
    result = _response(scope=_scope(headers=headers))

    assert _header_values(result[0], b"content-encoding") == [b"gzip"]
    assert gzip.decompress(result[1]["body"]) == _BODY


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"accept-encoding", b"")],
        [(b"accept-encoding", b"br, deflate")],
        [(b"accept-encoding", b"gzip;q=0")],
        [(b"accept-encoding", b"gzip;q=0.000")],
        [(b"accept-encoding", b"gzip;q=0, *;q=1")],
        [(b"accept-encoding", b"*;q=0")],
        [(b"accept-encoding", b"gzip;q=bogus, *")],
        [(b"accept-encoding", b"gzip;q=-1")],
        [(b"accept-encoding", b"gzip;q=1.001")],
        [(b"accept-encoding", b"gzip;q=2")],
        [(b"accept-encoding", b"gzip;q=nan")],
        [(b"accept-encoding", b"gzip;q=inf")],
        [(b"accept-encoding", b"gzip;q=.5")],
        [(b"accept-encoding", b"gzip;q=0.0001")],
        [(b"accept-encoding", b"gzip;q=")],
        [(b"accept-encoding", b'gzip;q="1"')],
        [(b"accept-encoding", b"gzip;q=1;q=0")],
        [(b"accept-encoding", b"gzip;unsupported=1")],
        [(b"accept-encoding", b"gzip;q=1, gzip;q=0")],
        [(b"accept-encoding", b"gzip;q=0, gzip;q=1")],
        [(b"accept-encoding", b"gzip"), (b"Accept-Encoding", b"gzip;q=0")],
        [(b"accept-encoding", b"gzip;q=0"), (b"accept-encoding", b"gzip")],
        [(b"accept-encoding", b"gzip"), (b"accept-encoding", b"gzip;q=bad")],
        [(b"accept-encoding", b"*"), (b"accept-encoding", b"*;q=0")],
    ],
)
def test_refused_or_invalid_quality_preserves_identity_and_varies(headers):
    result = _response(scope=_scope(headers=headers))

    assert result[1]["body"] == _BODY
    assert _header_values(result[0], b"content-encoding") == []
    assert _header_values(result[0], b"content-length") == [
        str(len(_BODY)).encode("ascii")
    ]
    assert _header_values(result[0], b"vary") == [b"Accept-Encoding"]


@pytest.mark.parametrize("accept_encoding", [[], [(b"accept-encoding", b"gzip")]])
def test_vary_preserves_tokens_duplicate_headers_and_header_order(accept_encoding):
    headers = [
        (b"Set-Cookie", b"first=value; HttpOnly"),
        (b"Content-Type", b"application/json; charset=utf-8"),
        (b"Vary", b"Origin, Accept-Language"),
        (b"Content-Length", str(len(_BODY)).encode("ascii")),
        (b"vary", b"Cookie"),
        (b"Set-Cookie", b"second=value; Secure"),
    ]
    result = _response(
        _messages(headers=headers), scope=_scope(headers=accept_encoding)
    )

    assert _header_values(result[0], b"vary") == [
        b"Origin, Accept-Language, Accept-Encoding",
        b"Cookie",
    ]
    assert [name for name, _ in result[0]["headers"]][:6] == [
        name for name, _ in headers
    ]
    assert _header_values(result[0], b"set-cookie") == [
        b"first=value; HttpOnly",
        b"second=value; Secure",
    ]
    assert headers[2] == (b"Vary", b"Origin, Accept-Language")


@pytest.mark.parametrize("vary", [b"*", b"Origin, aCcEpT-EnCoDiNg"])
def test_existing_vary_is_not_duplicated(vary):
    messages = _messages()
    messages[0]["headers"].extend([(b"vary", b"Cookie"), (b"Vary", vary)])

    result = _response(messages)

    assert _header_values(result[0], b"vary") == [b"Cookie", vary]


def test_compressed_response_without_original_length_gets_wire_length():
    result = _response(_messages(headers=[(b"content-type", b"application/json")]))

    assert _header_values(result[0], b"content-length") == [
        str(len(result[1]["body"])).encode("ascii")
    ]
    assert gzip.decompress(result[1]["body"]) == _BODY


@pytest.mark.parametrize("size", [0, 2, 999])
def test_below_minimum_size_is_completely_unchanged(size):
    original = _messages(b" " * size)

    assert _response(original) == original


def test_minimum_size_boundary_can_compress():
    body = b'{"data":"' + b"a" * 989 + b'"}'
    assert len(body) == 1000

    result = _response(_messages(body))

    assert _header_values(result[0], b"content-encoding") == [b"gzip"]
    assert gzip.decompress(result[1]["body"]) == body


def test_custom_minimum_size_is_respected():
    original = _messages()

    assert _response(original, minimum_size=len(_BODY) + 1) == original


def test_no_size_saving_keeps_identity_and_original_length():
    # A small valid JSON body above a deliberately lowered threshold expands
    # under gzip, proving that minimum size alone does not force compression.
    original = _messages(b'{"id":1}')
    result = _response(original, minimum_size=1)

    assert result[1] == original[1]
    assert _header_values(result[0], b"content-encoding") == []
    assert _header_values(result[0], b"content-length") == [b"8"]
    assert _header_values(result[0], b"vary") == [b"Accept-Encoding"]


def test_large_incompressible_body_keeps_identity():
    body = random.Random(84).randbytes(2048)
    assert len(gzip.compress(body, compresslevel=6, mtime=0)) >= len(body)

    result = _response(_messages(body))

    assert result[1]["body"] == body
    assert _header_values(result[0], b"content-encoding") == []
    assert _header_values(result[0], b"content-length") == [b"2048"]
    assert _header_values(result[0], b"vary") == [b"Accept-Encoding"]


@pytest.mark.parametrize("status", [201, 204, 206, 301, 400, 401, 403, 404, 500])
def test_only_status_200_is_eligible(status):
    original = _messages(status=status)

    assert _response(original) == original


@pytest.mark.parametrize(
    "overrides",
    [
        {"method": "GET"},
        {"method": "HEAD"},
        {"method": "PUT"},
        {"path": "/api/v1/workspaces/workspace-one/graph/query"},
        {"path": "/api/v1/workspaces/workspace-one/graph/overview/query/"},
        {"path": "/api/v1/workspaces//graph/overview/query"},
        {"path": "/api/v1/workspaces/one/two/graph/overview/query"},
        {"path": "/prefix" + _PATH},
        {"path": "/api/v1/auth/me"},
        {"path": "/api/v1/workspaces/workspace-one/graph/overview/export"},
        {"type": "websocket"},
        {"type": "lifespan"},
    ],
)
def test_other_methods_paths_and_non_http_scopes_are_unchanged(overrides):
    original = _messages()

    assert _response(original, scope=_scope(**overrides)) == original


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"content-type", b"text/event-stream")],
        [(b"content-type", b"application/octet-stream")],
        [(b"content-type", b"application/problem+json")],
        [(b"content-type", b"text/plain")],
        [(b"content-type", b"application/json"), (b"content-type", b"text/plain")],
    ],
)
def test_non_json_or_ambiguous_content_type_is_unchanged(headers):
    original = _messages(headers=headers)

    assert _response(original) == original


@pytest.mark.parametrize(
    "extra_header",
    [
        (b"Content-Encoding", b"gzip"),
        (b"content-encoding", b"br"),
        (b"content-encoding", b"identity"),
        (b"content-encoding", b""),
        (b"Content-Range", b"bytes 0-999/2000"),
        (b"ETag", b'"serialized-bytes"'),
        (b"etag", b'W/"weak-validator"'),
        (b"Content-MD5", b"original-body-digest"),
        (b"Digest", b"sha-256=original-body-digest"),
        (b"Content-Digest", b"sha-256=:original-body-digest:"),
        (b"Repr-Digest", b"sha-256=:original-body-digest:"),
        (b"Content-Disposition", b'attachment; filename="graph.json"'),
        (b"cache-control", b"private, no-transform"),
        (b"Cache-Control", b"NO-TRANSFORM, max-age=0"),
    ],
)
def test_already_encoded_byte_sensitive_download_and_no_transform_skip(extra_header):
    original = _messages()
    original[0]["headers"].append(extra_header)

    assert _response(original) == original


@pytest.mark.parametrize(
    "header",
    [
        (b"Range", b"bytes=0-999"),
        (b"If-Range", b'"original-validator"'),
        (b"Cache-Control", b"no-transform"),
    ],
)
def test_byte_sensitive_or_no_transform_request_is_unchanged(header):
    original = _messages()

    assert (
        _response(
            original, scope=_scope(headers=[(b"accept-encoding", b"gzip"), header])
        )
        == original
    )


def test_private_no_store_cache_policy_does_not_prevent_compression():
    messages = _messages()
    messages[0]["headers"].append((b"cache-control", b"private, no-store"))

    result = _response(messages)

    assert gzip.decompress(result[1]["body"]) == _BODY
    assert _header_values(result[0], b"cache-control") == [b"private, no-store"]


def test_duplicate_cache_control_no_transform_cannot_be_lost():
    original = _messages()
    original[0]["headers"].extend(
        [(b"cache-control", b"private"), (b"Cache-Control", b"no-transform")]
    )

    assert _response(original) == original


def test_stream_is_flushed_unchanged_before_producer_continues():
    original = _messages()
    original[1]["more_body"] = True
    original.append({"type": "http.response.body", "body": b"tail"})
    sent: list[Message] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send(original[0])
        assert sent == []
        await send(original[1])
        assert sent == original[:2]
        await send(original[2])

    async def scenario() -> None:
        async def receive() -> Message:
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            sent.append(message)

        await WorkspaceGraphCompressionMiddleware(app)(_scope(), receive, send)

    asyncio.run(scenario())
    assert sent == original


def test_response_pathsend_extension_and_trailers_are_unchanged():
    pathsend = [_messages()[0], {"type": "http.response.pathsend", "path": "graph"}]
    assert _response(pathsend) == pathsend
    trailers = _messages()
    trailers[0]["trailers"] = True
    trailers.append({"type": "http.response.trailers", "headers": []})
    assert _response(trailers) == trailers


def test_compression_runs_in_threadpool_with_response_local_arguments(monkeypatch):
    event_loop_thread = threading.get_ident()
    original_compress = gzip.compress
    calls = []

    def compress(body, *, compresslevel, mtime):
        calls.append((threading.get_ident(), body, compresslevel, mtime))
        return original_compress(body, compresslevel=compresslevel, mtime=mtime)

    monkeypatch.setattr(workspace_graph_compression.gzip, "compress", compress)

    result = _response()

    assert gzip.decompress(result[1]["body"]) == _BODY
    assert len(calls) == 1
    assert calls[0][0] != event_loop_thread
    assert calls[0][1:] == (_BODY, 6, 0)


def test_identity_and_small_responses_do_not_spend_threadpool_cpu(monkeypatch):
    def unexpected_compression(*args, **kwargs):
        pytest.fail("identity and small responses must not be compressed")

    monkeypatch.setattr(
        workspace_graph_compression.gzip, "compress", unexpected_compression
    )

    assert _response(scope=_scope(headers=[]))[1]["body"] == _BODY
    assert _response(_messages(b"{}")) == _messages(b"{}")


def test_concurrent_actor_responses_remain_independently_encoded():
    bodies = [
        json.dumps({"actor": actor, "documents": [f"private-{actor}"] * 1000}).encode()
        for actor in range(12)
    ]

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        messages = _messages(scope["state"]["body"])
        await send(messages[0])
        await asyncio.sleep(0)
        await send(messages[1])

    async def scenario() -> list[list[Message]]:
        shared_middleware = WorkspaceGraphCompressionMiddleware(app)
        return await asyncio.gather(
            *(_run(shared_middleware, _scope(state={"body": body})) for body in bodies)
        )

    responses = asyncio.run(scenario())

    for original, result in zip(bodies, responses, strict=True):
        assert gzip.decompress(result[1]["body"]) == original
        assert result[1]["body"] == gzip.compress(original, compresslevel=6, mtime=0)
        assert _header_values(result[0], b"content-length") == [
            str(len(result[1]["body"])).encode("ascii")
        ]
    assert len({response[1]["body"] for response in responses}) == len(bodies)

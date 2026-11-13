"""Reuse immutable Markdown syntax results for one synchronous read request."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import cast

_Memo = dict[tuple[object, str], object]
_request_memo: ContextVar[_Memo | None] = ContextVar(
    "request_markdown_memo", default=None
)


@contextmanager
def request_markdown_memo_scope() -> Iterator[_Memo]:
    """Nested reads share their caller's memo; the owning read always resets it."""
    memo = _request_memo.get()
    if memo is not None:
        yield memo
        return
    memo = {}
    token = _request_memo.set(memo)
    try:
        yield memo
    finally:
        _request_memo.reset(token)


def with_request_markdown_memo[**P, T](function: Callable[P, T]) -> Callable[P, T]:
    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        with request_markdown_memo_scope():
            return function(*args, **kwargs)

    return wrapped


def memoize_markdown_analysis[T](function: Callable[[str], T]) -> Callable[[str], T]:
    """Memoize a fixed-profile pure analysis by operation and exact source text."""
    @wraps(function)
    def wrapped(body: str) -> T:
        memo = _request_memo.get()
        if memo is None:
            return function(body)
        key = (function, body)
        if key not in memo:
            memo[key] = function(body)
        return cast(T, memo[key])

    return wrapped

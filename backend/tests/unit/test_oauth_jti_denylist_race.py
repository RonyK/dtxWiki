"""MCP-04: the process-local JTI denylist cache must not let a verify that
races a concurrent revoke's commit tail re-memoize a stale "not revoked"
answer for the rest of the 60-second time bucket.

``is_access_jti_revoked`` reads through its own session and only then
consults/stores the cache. Without the epoch guard added for MCP-04, a
revoke that clears the cache while a concurrent read is in flight (between
that read's DB fetch and its cache store) is invisible to the read: it
stores the pre-commit answer anyway, right after the clear ran, and that
stale entry then survives for up to 60 seconds.

This test controls that interleaving deterministically with a fake
session/factory instead of two overlapping real DB transactions (SQLite
:memory: + StaticPool serializes real concurrent sessions onto one
connection, which would make a genuine DB-level race flaky rather than
deterministic).
"""

import threading

from backend.services.oauth_service import (
    _JTI_DENYLIST_CACHE,
    _jti_denylist_cache_clear,
    is_access_jti_revoked,
)


class _FakeRow:
    def __init__(self, revoked_at):
        self.revoked_at = revoked_at


class _RacyFactory:
    """A fake ``session_factory``. Its one ``.get()`` call snapshots the
    row (simulating transaction-read semantics: later mutations to the
    "table" must not retroactively change what this read already saw) and
    then pauses *between the DB read and returning to the caller* so a
    concurrent revoke + post-commit cache clear can be interleaved right
    there, matching the real ``is_access_jti_revoked`` window this closes.
    """

    def __init__(self, row: _FakeRow):
        self._row = row
        self.read_done = threading.Event()
        self.may_return = threading.Event()

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get(self, model, pk):
        # Snapshot now: a real DB read would not see a commit that lands
        # after this SELECT executed, even though it lands before the
        # caller acts on the result.
        snapshot = _FakeRow(revoked_at=self._row.revoked_at)
        self.read_done.set()
        assert self.may_return.wait(timeout=5), "test deadlock: may_return never set"
        return snapshot


def test_jti_denylist_epoch_guard_rejects_read_that_raced_a_commit():
    jti = "race-jti"
    row = _FakeRow(revoked_at=None)  # pre-commit: not yet revoked
    factory = _RacyFactory(row)

    result: dict[str, bool] = {}

    def reader() -> None:
        result["revoked"] = is_access_jti_revoked(
            jti=jti,
            session_factory=factory,
            time_bucket=0,
        )

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    assert factory.read_done.wait(timeout=5), "reader never reached the DB read"

    # Simulate the revoking transaction committing while the reader above is
    # paused between its DB read (which observed revoked_at IS NULL) and its
    # cache store -- e.g. MCP-01's disable path or /oauth/revoke completing
    # on a different session/thread.
    row.revoked_at = "2026-01-01T00:00:00Z"  # any non-None sentinel
    _jti_denylist_cache_clear()  # the MCP-04 post-commit clear

    factory.may_return.set()
    reader_thread.join(timeout=5)

    cache_key = (jti, 0, id(factory))
    assert result.get("revoked") is False, (
        "the in-flight read legitimately observed pre-commit state"
    )
    assert cache_key not in _JTI_DENYLIST_CACHE, (
        "epoch guard must refuse to memoize the stale pre-commit answer "
        "once a revoke committed while the read was in flight -- without "
        "it this jti would read as NOT revoked for up to 60s"
    )

    # A fresh read with no race in flight legitimately observes -- and may
    # now cache -- the committed revocation.
    assert (
        is_access_jti_revoked(jti=jti, session_factory=factory, time_bucket=0)
        is True
    )
    assert _JTI_DENYLIST_CACHE[cache_key] is True


def test_jti_denylist_cache_clear_advances_epoch():
    """Smoke test for the epoch primitive itself: every clear opens a new
    generation, including back-to-back calls (eviction-vs-invalidation
    only differs in whether ``_jti_denylist_cache_clear`` -- vs a bare
    ``dict.clear()`` -- was used)."""

    from backend.services import oauth_service as oauth_service_module

    before = oauth_service_module._JTI_DENYLIST_EPOCH
    _jti_denylist_cache_clear()
    after = oauth_service_module._JTI_DENYLIST_EPOCH
    assert after != before

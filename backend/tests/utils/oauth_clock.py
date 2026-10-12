"""The OAuth service's "now" as a logical clock, for tests that need exact time.

``OAuthService`` reads the wall clock through ``oauth_service.utc_now``, and the
host clock is not monotonic: an NTP or VM-resume step sets it back, and waking
from a host sleep sets it forward. A test that backdates a row against
``utc_now()``, or measures a window around a request, is therefore right only
while no step lands inside it. On this clock time passes by assignment and the
service's decisions follow it exactly.

A test module registers the fixtures by importing them. They are requested as
``oauth_clock`` and ``oauth_row_clock``; the functions carry a ``_fixture``
suffix only so that a test parameter of the same name does not shadow the
import::

    from backend.tests.utils.oauth_clock import (  # noqa: F401
        OAuthClock,
        oauth_clock_fixture,
        oauth_row_clock_fixture,
    )
"""

from __future__ import annotations

from datetime import datetime

import pytest

from backend.models.base import utc_now


class OAuthClock:
    """The OAuth service's "now", moved only when a test moves it.

    Model defaults such as ``created_at`` and the session service keep their
    own clocks, so tests on this clock assert deadlines and expiries, not those
    timestamps, unless they also take ``oauth_row_clock``.
    """

    def __init__(self) -> None:
        # An anchor only: nothing reads the host clock through it again.
        self.value = utc_now()

    def now(self) -> datetime:
        return self.value


@pytest.fixture(name="oauth_clock")
def oauth_clock_fixture(monkeypatch) -> OAuthClock:
    from backend.services import oauth_service

    clock = OAuthClock()
    monkeypatch.setattr(oauth_service, "utc_now", clock.now)
    return clock


@pytest.fixture(name="oauth_row_clock")
def oauth_row_clock_fixture(oauth_clock, monkeypatch) -> OAuthClock:
    """``oauth_clock`` that stamps the rows the test writes as well.

    Models take ``created_at`` from ``models.base.utc_now``, which reads the
    host clock on its own, so a test that compares a stored ``created_at`` with
    a service deadline would compare two clocks. Here every row is stamped with
    the clock's value. Rows written between two moves therefore share a
    timestamp, and a test on this clock must not depend on ordering by it.
    """

    from backend.models import base

    class _InstanceOfDatetime(type):
        # ``UTCDateTime`` re-attaches UTC only to values that pass its
        # ``isinstance(value, datetime)`` check, run against ``models.base``'s
        # own ``datetime``. A bare subclass in its place fails that for every
        # ordinary datetime, and SQLite hands the values back naive.
        def __instancecheck__(cls, obj: object) -> bool:
            return isinstance(obj, datetime)

    class _ClockDatetime(datetime, metaclass=_InstanceOfDatetime):
        @classmethod
        def now(cls, tz):
            return oauth_clock.now().astimezone(tz)

    monkeypatch.setattr(base, "datetime", _ClockDatetime)
    return oauth_clock

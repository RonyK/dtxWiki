"""app.state lazy-memoization atomicity (sync deps run in a threadpool)."""

import threading
import time
from types import SimpleNamespace

from starlette.datastructures import State

from backend.app import state as app_state
from backend.app.settings import Settings
from backend.app.state import (
    get_page_service_from_app,
    get_session_factory_from_app,
)


def _request():
    app = SimpleNamespace(state=State())
    app.state.settings = Settings(auto_bootstrap=False)
    return SimpleNamespace(app=app)


def test_session_factory_built_once_under_concurrent_cold_start(monkeypatch):
    calls = []

    def slow_factory(settings):
        calls.append(settings)
        time.sleep(0.05)
        return object()

    monkeypatch.setattr(
        app_state, "make_session_factory_from_settings", slow_factory
    )
    request = _request()
    results = []
    start = threading.Barrier(8, timeout=30)

    def worker():
        start.wait()
        results.append(get_session_factory_from_app(request))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(not thread.is_alive() for thread in threads)

    assert len(calls) == 1
    assert len(results) == 8
    assert all(result is results[0] for result in results)
    assert request.app.state.session_factory is results[0]


def test_page_service_built_once_under_concurrent_cold_start(monkeypatch):
    calls = []

    def slow_factory(settings):
        calls.append(settings)
        time.sleep(0.05)
        return object()

    monkeypatch.setattr(
        app_state, "make_session_factory_from_settings", slow_factory
    )
    request = _request()
    results = []
    start = threading.Barrier(8, timeout=30)

    def worker():
        start.wait()
        results.append(get_page_service_from_app(request))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(not thread.is_alive() for thread in threads)

    # get_page_service_from_app builds its factory via
    # get_session_factory_from_app while holding the lock (reentrancy), and
    # both memoizations stay single-shot.
    assert len(calls) == 1
    assert len(results) == 8
    assert all(result is results[0] for result in results)
    assert request.app.state.page_service is results[0]
    assert request.app.state.session_factory is results[0].session_factory


def test_injected_page_service_supplies_session_factory():
    request = _request()
    factory = object()
    request.app.state.page_service = SimpleNamespace(session_factory=factory)

    assert get_session_factory_from_app(request) is factory
    assert request.app.state.session_factory is factory

from __future__ import annotations

import threading

from fastapi import Request
from sqlalchemy.orm import Session, sessionmaker

from backend.app.container import AppContainer, assemble
from backend.app.db import make_session_factory_from_settings
from backend.services.page_service import PageService

# FastAPI runs sync dependencies in a threadpool, so two cold-start requests
# can race the check-then-set memoization below and each build an engine
# (last writer wins, the loser's pool is orphaned). One process-wide reentrant
# lock keeps construction single-shot; RLock because get_page_service_from_app
# builds its factory via get_session_factory_from_app while holding it.
_state_lock = threading.RLock()


def get_session_factory_from_app(request: Request) -> sessionmaker[Session]:
    """Return the app-wide SQLAlchemy session factory.

    Test apps may inject either ``app.state.session_factory`` directly or a
    prebuilt ``app.state.page_service``. Production apps lazily create one
    shared factory from settings and store it on ``app.state`` so REST auth,
    scope enforcement, and page APIs do not each create independent factories.
    """

    state = request.app.state
    if hasattr(state, "session_factory"):
        return state.session_factory
    if hasattr(state, "container"):
        session_factory = state.container.db.control_session_factory()
        state.session_factory = session_factory
        return session_factory
    with _state_lock:
        if hasattr(state, "session_factory"):
            return state.session_factory
        if hasattr(state, "container"):
            session_factory = state.container.db.control_session_factory()
            state.session_factory = session_factory
            return session_factory
        if hasattr(state, "page_service"):
            session_factory = state.page_service.session_factory
        else:
            session_factory = make_session_factory_from_settings(state.settings)
        state.session_factory = session_factory
        return session_factory


def get_page_service_from_app(request: Request) -> PageService:
    state = request.app.state
    if hasattr(state, "page_service"):
        return state.page_service
    with _state_lock:
        if not hasattr(state, "page_service"):
            if hasattr(state, "container"):
                state.page_service = state.container.get_page_service()
            else:
                session_factory = get_session_factory_from_app(request)
                container = get_container_from_app(request)
                services = container.get_services()
                state.page_service = PageService(
                    session_factory,
                    job_queue=services.job_queue,
                    body_preparer=services.body_preparer,
                    lint_registry=services.lint_registry,
                )
                container.page_service = state.page_service
        return state.page_service


def get_search_service_from_app(request: Request):
    """Return the app-wide SearchService built from *this app's* settings.

    Built lazily from ``app.state.settings`` (not a module-global ``Settings()``)
    so an app created via ``create_app(settings=...)`` — e.g. a test or CLI app
    configured with an embedding provider — actually advertises and serves
    semantic/hybrid retrieval instead of silently falling back to keyword-only.
    """

    from backend.services.search_service import build_search_service

    state = request.app.state
    if hasattr(state, "search_service"):
        return state.search_service
    if hasattr(state, "container"):
        state.search_service = state.container.get_search_service()
        return state.search_service
    with _state_lock:
        if not hasattr(state, "search_service"):
            if hasattr(state, "container"):
                state.search_service = state.container.get_search_service()
            else:
                state.search_service = build_search_service(state.settings)
        return state.search_service


def get_ask_service_from_app(request: Request):
    """Return the app-wide AskService wrapping the app-wide SearchService."""

    from backend.services.ask_service import AskService

    state = request.app.state
    if hasattr(state, "ask_service"):
        return state.ask_service
    if hasattr(state, "container"):
        state.ask_service = state.container.get_ask_service()
        return state.ask_service
    with _state_lock:
        if not hasattr(state, "ask_service"):
            if hasattr(state, "container"):
                state.ask_service = state.container.get_ask_service()
            else:
                state.ask_service = AskService(
                    search_service=get_search_service_from_app(request)
                )
        return state.ask_service


def get_container_from_app(request: Request) -> AppContainer:
    state = request.app.state
    if hasattr(state, "container"):
        return state.container
    with _state_lock:
        if not hasattr(state, "container"):
            state.container = assemble(
                state.settings,
                session_factory=(
                    state.session_factory if hasattr(state, "session_factory") else None
                ),
                page_service=(
                    state.page_service if hasattr(state, "page_service") else None
                ),
                search_service=(
                    state.search_service if hasattr(state, "search_service") else None
                ),
                ask_service=(
                    state.ask_service if hasattr(state, "ask_service") else None
                ),
            )
        return state.container

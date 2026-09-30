"""A workspace can pick the retrieval mode Ask starts from.

`auto` is the historical hybrid > semantic > keyword ladder, so nothing changes
until someone chooses otherwise. The interesting case is a stored choice this
deployment cannot serve: `SearchService` rejects an unknown mode outright,
which is right for a caller who asked for it and wrong for a setting an
operator changed months ago -- Ask would 500 for everyone until someone found
the row.
"""

from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.models.workspace import Workspace
from backend.schemas.ask import AskSessionCreate, AskTurnCreate
from backend.services.ask_service import (
    UNSUPPORTED_RETRIEVAL_MODE_FALLBACK_WARNING,
    AskService,
)
from backend.services.search_service import SearchService

pytestmark = pytest.mark.integration


class _HybridSearch:
    def supported_modes(self):
        return {"keyword", "semantic", "hybrid"}


def _owner(workspace_id):
    from backend.policy.auth import ActorContext

    return ActorContext(
        actor_kind="human",
        actor_id="owner",
        token_kind="session",
        scopes=("workspace-admin",),
        workspace_id=workspace_id,
        workspace_role="owner",
        role="owner",
    )


def _set_mode(session_factory, mode: str):
    with session_factory.begin() as session:
        workspace = session.scalar(select(Workspace).where(Workspace.slug == "default"))
        workspace.ask_retrieval_mode = mode
        return workspace.id


def test_auto_keeps_the_ladder_it_always_used():
    service = AskService(search_service=_HybridSearch())

    assert service._best_retrieval_mode("auto") == ("hybrid", True)
    assert service._best_retrieval_mode(None) == ("hybrid", True)


def test_a_supported_choice_is_honoured():
    service = AskService(search_service=_HybridSearch())

    assert service._best_retrieval_mode("keyword") == ("keyword", True)
    assert service._best_retrieval_mode("semantic") == ("semantic", True)


def test_an_unservable_choice_falls_back_instead_of_raising():
    service = AskService(search_service=SearchService())

    mode, honored = service._best_retrieval_mode("semantic")

    assert mode == "keyword"
    assert honored is False


def test_the_fallback_reaches_the_turn_as_a_warning(session_factory, page_service):
    """Silently downgrading would leave the workspace believing semantic search
    is running when it is not."""

    workspace_id = _set_mode(session_factory, "semantic")
    page_service.write_page(
        workspace_slug="default",
        slug="retrieval-policy-evidence",
        title="Retrieval Policy Evidence",
        body_md="# Retrieval Policy Evidence\n\nKeyword still finds this.",
        actor_kind="human",
        actor_id=str(uuid4()),
        actor_provenance="browser_session",
    )
    service = AskService(search_service=SearchService())

    with session_factory.begin() as session:
        ask_session = service.create_session(
            session=session,
            workspace_id=workspace_id,
            request=AskSessionCreate(),
            actor_kind="human",
            actor_id=str(uuid4()),
        )
        _detail, warnings = service.create_turn(
            session=session,
            workspace_id=workspace_id,
            ask_session_id=ask_session.id,
            request=AskTurnCreate(user_message="what does the retrieval policy say"),
            actor_kind="human",
            actor_id=str(uuid4()),
        )

    assert UNSUPPORTED_RETRIEVAL_MODE_FALLBACK_WARNING in warnings


def test_a_servable_choice_produces_no_warning(session_factory, page_service):
    workspace_id = _set_mode(session_factory, "keyword")
    page_service.write_page(
        workspace_slug="default",
        slug="retrieval-policy-honoured",
        title="Retrieval Policy Honoured",
        body_md="# Retrieval Policy Honoured\n\nKeyword is what was asked for.",
        actor_kind="human",
        actor_id=str(uuid4()),
        actor_provenance="browser_session",
    )
    service = AskService(search_service=SearchService())

    with session_factory.begin() as session:
        ask_session = service.create_session(
            session=session,
            workspace_id=workspace_id,
            request=AskSessionCreate(),
            actor_kind="human",
            actor_id=str(uuid4()),
        )
        _detail, warnings = service.create_turn(
            session=session,
            workspace_id=workspace_id,
            ask_session_id=ask_session.id,
            request=AskTurnCreate(user_message="what does the retrieval policy say"),
            actor_kind="human",
            actor_id=str(uuid4()),
        )

    assert UNSUPPORTED_RETRIEVAL_MODE_FALLBACK_WARNING not in warnings


def test_the_setting_round_trips_through_the_policies_api(session_factory):
    from backend.services.settings_service import (
        SettingsPolicyUpdateRejected,
        SettingsService,
    )

    service = SettingsService()
    with session_factory.begin() as session:
        workspace_id = session.scalar(
            select(Workspace.id).where(Workspace.slug == "default")
        )
        service.update_policy(
            control_session=session,
            workspace_session=session,
            workspace_id=workspace_id,
            actor=_owner(workspace_id),
            group="workspace",
            key="ask_retrieval_mode",
            value="hybrid",
        )
    with session_factory() as session:
        policies = service.get_policies(
            control_session=session,
            workspace_session=session,
            workspace_id=workspace_id,
        )
    assert policies.workspace.policy["ask_retrieval_mode"] == "hybrid"

    with session_factory.begin() as session:
        with pytest.raises(SettingsPolicyUpdateRejected):
            service.update_policy(
                control_session=session,
                workspace_session=session,
                workspace_id=workspace_id,
                actor=_owner(workspace_id),
                group="workspace",
                key="ask_retrieval_mode",
                value="graph",
            )

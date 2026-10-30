"""Pins for public overlay assembly surfaces that are not SPI Protocols."""

from __future__ import annotations

import inspect
import subprocess
import sys
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

import backend.policy.auth as compat_auth
import backend.schemas.read as compat_read
from backend.app.container import assemble
from backend.app.main import create_app
from backend.services.graph_service import GraphService
from backend.services.review_service import ReviewService
from backend.spi import DEFAULT_REST_ROLE, ActorContext, RankedResult, SearchFilters
from backend.worker.registry import JobHandler, register
from backend.worker.runtime import OneShotWorker


def test_overlay_assembly_surface_signatures_are_pinned():
    """Public overlay surface: changes require an explicit changelog entry."""

    # assemble() exposes explicit lint modules alongside the existing provider
    # injection points. The additive overlay contract is recorded in CHANGELOG.
    assert str(inspect.signature(assemble)) == (
        "(settings: 'Settings', *, db: 'DatabaseProvider | None' = None, "
        "session_factory: 'sessionmaker[Session] | None' = None, page_service: "
        "'object | None' = None, search_service: 'object | None' = None, "
        "ask_service: 'object | None' = None, directory: "
        "'WorkspaceDirectory | None' = None, services: "
        "'ServiceRegistry | None' = None, lint_modules: 'tuple[LintModule, ...]' = (), "
        "blob_store_provider: "
        "'BlobStoreProvider | None' = None) -> 'AppContainer'"
    )
    assert str(inspect.signature(create_app)) == (
        "(settings: backend.app.settings.Settings | None = None, "
        "page_service=None, *, container: backend.app.container.AppContainer | "
        "None = None, extra_mount=None, extra_lifespan=None, "
        "bootstrap_on_startup: bool = False, static_root: pathlib.Path | None "
        "= None, include_llms_txt: bool = True) -> "
        "fastapi.applications.FastAPI"
    )
    # context_media_orphan_sweep_service uses the same lazily resolved injection
    # point as media_install_reconciler -- a deliberate, reviewed
    # public-surface change, not a silent drift.
    assert str(inspect.signature(OneShotWorker)) == (
        "(job_queue: 'JobQueueService | None' = None, library_service: "
        "'LibraryService | None' = None, extraction_service: "
        "'ExtractionService | None' = None, bundle_expiration_service: "
        "'BundleExpirationService | None' = None, feedback_resolver: "
        "'FeedbackResolverService | None' = None, email_delivery_service: "
        "'EmailDeliveryService | None' = None, media_install_reconciler: "
        "'_MediaInstallReconciler | None' = None, "
        "context_media_orphan_sweep_service: "
        "'ContextMediaOrphanSweepService | None' = None, "
        "indexer: 'object | None' = None, "
        "settings: 'Settings | None' = None, database_provider: "
        "'DatabaseProvider | None' = None, services: "
        "'ServiceRegistry | None' = None, blob_store_provider: "
        "'BlobStoreProvider | None' = None) -> 'None'"
    )
    assert str(inspect.signature(register)) == (
        "(job_type: 'str', handler: 'JobHandler') -> 'None'"
    )
    assert str(inspect.signature(JobHandler)) == (
        "(business: 'Callable[..., dict[str, Any]]', completes_in_service: "
        "'bool', on_failure: 'Callable[..., None] | None' = None) -> None"
    )


def test_graph_service_public_surface_signatures_are_pinned():
    """GraphService becomes a formal SPI only at the S3 hook-first change.

    Graph View widened the surface: ``neighbors`` accepts a reusable
    ``hydration_context`` and returns ``NeighborEdges`` -- a ``list`` subclass
    carrying scan-bound metadata, so every existing ``list[NeighborEdge]``
    caller keeps working -- and ``hydrate_entities``, ``hydration_context``
    and ``visible_projection_edges`` are the actor-visible building blocks the
    Graph query service composes. F08 added ``ranked_projection_edges`` (the
    Local Graph's per-kind ranked collector) and ``relation_rank_denominators``
    (the actor-visible counts a relation's rank divides by). Their read-scope
    arguments remain required keywords; the depth-two collector adds the
    backward-compatible optional ``min_partition_cap=1`` floor.
    """

    expected = {
        "ppr": (
            "(self, session: 'Session', workspace_id: 'UUID', *, seeds: "
            "'list[tuple[str, UUID, float]]', actor: 'ActorContext | None', "
            "top_k: 'int' = 20) -> 'list[NeighborEdge]'"
        ),
        "context_page_neighbors_panel": (
            "(self, session: 'Session', workspace_id: 'UUID', *, page_id: "
            "'UUID', actor: 'ActorContext | None', include_mermaid: 'bool' = "
            "False) -> 'GraphNeighborsPayload'"
        ),
        "render_context_page_mermaid": (
            "(seed_title: 'str', payload: 'GraphNeighborsPayload') -> 'str'"
        ),
        "context_page_graph_context": (
            "(self, session: 'Session', workspace_id: 'UUID', *, page_id: "
            "'UUID', actor: 'ActorContext | None', limit: 'int' = 5) -> "
            "'list[GraphNeighborEntry]'"
        ),
        "context_pages_graph_context": (
            "(self, session: 'Session', workspace_id: 'UUID', *, page_ids: "
            "'list[UUID]', actor: 'ActorContext | None', limit: 'int' = 5) -> "
            "'dict[UUID, list[GraphNeighborEntry]]'"
        ),
        "neighbors": (
            "(self, session: 'Session', workspace_id: 'UUID', *, kind: 'str', "
            "entity_id: 'UUID', actor: 'ActorContext | None', edge_kinds: "
            "'tuple[str, ...] | None' = None, direction: 'str' = 'both', "
            "limit: 'int' = 50, hydration_context: 'GraphHydrationContext | "
            "None' = None) -> 'NeighborEdges'"
        ),
        "hydrate_entities": (
            "(self, session: 'Session', workspace_id: 'UUID', *, entities: "
            "'list[tuple[str, UUID]]', actor: 'ActorContext | None', "
            "hydration_context: 'GraphHydrationContext | None' = None) -> "
            "'dict[tuple[str, UUID], _Hydrated]'"
        ),
        "hydration_context": (
            "(session: 'Session', workspace_id: 'UUID', actor: 'ActorContext | "
            "None') -> 'GraphHydrationContext'"
        ),
        "visible_projection_edges": (
            "(self, session: 'Session', workspace_id: 'UUID', *, kind: 'str', "
            "entity_id: 'UUID', actor: 'ActorContext | None', edge_kinds: "
            "'tuple[str, ...] | None' = None, exclude_edge_kinds: "
            "'tuple[str, ...]' = (), neighbor_kinds: 'tuple[str, ...] | None' = "
            "None, occurred_from: 'datetime | None' = None, occurred_to: "
            "'datetime | None' = None, direction: 'str' = 'both', limit: 'int' = "
            "50, hydration_context: 'GraphHydrationContext | None' = None) -> "
            "'VisibleProjectionEdges'"
        ),
        "siblings": (
            "(self, session: 'Session', workspace_id: 'UUID', *, page_id: "
            "'UUID', actor: 'ActorContext | None', limit: 'int' = 10) -> "
            "'list[NeighborEdge]'"
        ),
        "ranked_projection_edges": (
            "(self, session: 'Session', workspace_id: 'UUID', *, seeds: "
            "'tuple[tuple[str, UUID], ...]', actor: 'ActorContext | None', "
            "edge_kinds: 'tuple[str, ...]', neighbor_kinds: 'tuple[str, ...]', "
            "occurred_from: 'datetime | None', occurred_to: 'datetime | None', "
            "direction: 'str', edge_budget: 'int', now: 'datetime', "
            "hydration_context: 'GraphHydrationContext', min_partition_cap: "
            "'int' = 1) -> "
            "'VisibleProjectionEdges'"
        ),
        "relation_rank_denominators": (
            "(self, session: 'Session', workspace_id: 'UUID', *, actor: "
            "'ActorContext | None', sources: 'tuple[tuple[str, UUID], ...]') -> "
            "'dict[tuple[str, UUID], RankDenominators]'"
        ),
    }

    assert {name for name in vars(GraphService) if not name.startswith("_")} == set(
        expected
    )
    assert {
        name: str(inspect.signature(vars(GraphService)[name])) for name in expected
    } == expected


@pytest.mark.parametrize(
    ("seed_count", "floor", "expected_cap"),
    [(1, None, 1024), (250, None, 4), (250, 8, 8), (1100, None, 1)],
)
def test_ranked_projection_partition_floor_preserves_default_allocation(
    monkeypatch, seed_count, floor, expected_cap
):
    service = GraphService()
    workspace_id = uuid4()
    caps = []

    def collect(_session, _workspace_id, **kwargs):
        assert _workspace_id == workspace_id
        assert kwargs["actor"] is None
        caps.append(kwargs["cap"])
        return []

    monkeypatch.setattr(service, "_ranked_partition_rows", collect)
    with Session() as session:
        optional_floor = {} if floor is None else {"min_partition_cap": floor}
        result = service.ranked_projection_edges(
            session,
            workspace_id,
            seeds=tuple(("context_page", uuid4()) for _ in range(seed_count)),
            actor=None,
            edge_kinds=("wikilink",),
            neighbor_kinds=("context_page",),
            occurred_from=None,
            occurred_to=None,
            direction="both",
            edge_budget=750,
            now=datetime.now(UTC),
            hydration_context=service.hydration_context(session, workspace_id, None),
            **optional_floor,
        )

    assert caps == [expected_cap, expected_cap]
    assert result == []
    assert result.scan_capped is False


def test_review_service_role_defaults_are_fail_closed():
    role_parameters = {
        "list_reviews": "actor_role",
        "count_reviews": "actor_role",
        "get_review": "actor_role",
        "claim_review": "actor_role",
        "list_requested_changes_for_principal": "principal_role",
        "submit_revision_for_review": "principal_role",
        "can_decide_review": "actor_role",
        "list_recent_decisions": "actor_role",
        "apply_decision": "actor_role",
        "_detail": "actor_role",
    }
    for method_name, parameter_name in role_parameters.items():
        parameter = inspect.signature(getattr(ReviewService, method_name)).parameters[
            parameter_name
        ]
        assert parameter.default is None


def test_spi_payload_types_are_canonical_and_import_lightweight():
    assert compat_auth.ActorContext is ActorContext
    assert compat_auth.DEFAULT_REST_ROLE == DEFAULT_REST_ROLE
    assert compat_read.RankedResult is RankedResult
    assert compat_read.SearchFilters is SearchFilters

    probe = (
        "import sys; import backend.spi; "
        "assert 'fastapi' not in sys.modules; "
        "assert not any(n.startswith('backend.services') for n in sys.modules)"
    )
    subprocess.run([sys.executable, "-c", probe], check=True)
    reverse_probe = (
        "import backend.schemas.read as r; import backend.policy.auth as a; "
        "import backend.spi as s; "
        "assert r.RankedResult is s.RankedResult; "
        "assert r.SearchFilters is s.SearchFilters; "
        "assert a.ActorContext is s.ActorContext"
    )
    subprocess.run([sys.executable, "-c", reverse_probe], check=True)


# Field shapes an overlay compiles against. `test_spi_payload_types_are_
# canonical_and_import_lightweight` above pins only class IDENTITY, so a field
# could change type and every gate stayed green -- `RankedResult.heading_path`
# went from `str` to `list[str]` and `section_id` was added with no signal at
# all. On an extension seam a field's shape IS the contract, so pin it here and
# make changing one a deliberate edit to this literal rather than a silent one.
SPI_PAYLOAD_FIELD_SHAPES = {
    "RankedResult": {
        "id": "<class 'uuid.UUID'>",
        "type": (
            "typing.Literal['context_page', 'context_claim', 'library_item', "
            "'library_artifact', 'feedback', 'change_set', 'agent_page', 'source']"
        ),
        "title": "<class 'str'>",
        "snippet": "<class 'str'>",
        "score": "<class 'float'>",
        "citation_available": "<class 'bool'>",
        "stale": "<class 'bool'>",
        "archived": "<class 'bool'>",
        "summary": "str | None",
        "location": "list[str] | None",
        "concept_path": "str | None",
        "concept_type": "str | None",
        "version_id": "uuid.UUID | None",
        "heading_path": "list[str] | None",
        "section_id": "str | None",
        "source_ref": "dict[str, str] | None",
        "match_reason": (
            "typing.Optional[typing.Literal['exact_id', 'exact_slug', "
            "'exact_title', 'exact_alias', 'trigram', 'keyword', 'semantic', "
            "'hybrid', 'associative']]"
        ),
        "matched_name": "str | None",
        "entity_ref": "dict[str, str] | None",
    },
    "SearchFilters": {
        "include_agents": "<class 'bool'>",
        "context_type": "list[str] | None",
        "connector_type": "list[str] | None",
        "format_hint": "list[str] | None",
        "tag": "list[str] | None",
        "status": "list[str] | None",
        "trust_level": "list[str] | None",
        "date_from": "datetime.datetime | None",
        "date_to": "datetime.datetime | None",
    },
}


def test_spi_payload_field_shapes_are_pinned():
    models = {"RankedResult": RankedResult, "SearchFilters": SearchFilters}
    actual = {
        name: {
            field: str(info.annotation) for field, info in model.model_fields.items()
        }
        for name, model in models.items()
    }
    assert actual == SPI_PAYLOAD_FIELD_SHAPES, (
        "An SPI payload field changed shape. Overlays compile against these "
        "types, so update this pin deliberately and treat it as a contract "
        "change, not a refactor."
    )

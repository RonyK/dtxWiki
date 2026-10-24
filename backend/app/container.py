"""Application composition root for core services."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from backend.app.db import make_session_factory_from_settings
from backend.app.settings import Settings
from backend.spi.context import ToolCallContext

if TYPE_CHECKING:
    from backend.services.lint.base import LintModule

logger = logging.getLogger(__name__)

__all__ = [
    "PER_WORKSPACE_DATABASE_TOPOLOGY",
    "SHIPPED_STORAGE_TOPOLOGY",
    "SINGLE_DATABASE_TOPOLOGY",
    "AppContainer",
    "BlobStoreProvider",
    "DatabaseProvider",
    "ServiceRegistry",
    "StorageTopologyUnsupported",
    "ToolCallContext",
    "WorkspaceDirectory",
    "assemble",
    "check_storage_topology",
    "declared_storage_topology",
    "physical_split_compatibility",
    "storage_topology_report",
]


# --- Storage topology capability ----------------------------------------
#
# This build's storage topology is a DECLARED value, not something a caller
# infers by comparing the objects `control_session_factory()` and
# `session_for()` hand back. Those two happen to be the same sessionmaker
# today (see DatabaseProvider below), but "the two factories are identical"
# is an emergent property of the default provider: an overlay is free to
# return distinct objects that still address one physical database (two
# pools, a read replica, a proxy). Reading support off that comparison would
# call such an overlay "split" and, worse, would call a genuinely split
# overlay "single database" the moment it memoized one factory. So the
# provider says which topology it implements, and this module decides
# whether this build can serve it.
#
# The limit of a declaration, said plainly so nobody over-reads the guard: a
# provider that physically splits while declaring `single_database` is NOT
# caught here, and cannot be -- detecting it is exactly the inference this
# design refuses. The test suite already contains such a provider
# (`SplitProvider` in backend/tests/integration/test_cloud_composition.py
# hands back two distinct file-backed engines and inherits this default), and
# it deliberately still starts. What the declaration buys is that enabling a
# split ON PURPOSE cannot be done quietly: the honest overlay is refused, and
# the dishonest one has to state a falsehood in its own source.
SINGLE_DATABASE_TOPOLOGY = "single_database"
PER_WORKSPACE_DATABASE_TOPOLOGY = "per_workspace_database"
# The only topology this release serves. Per-workspace PHYSICAL databases are
# unsupported; see README.md ("현재 제한 사항") and DEPLOYMENT.md
# ("데이터베이스").
SHIPPED_STORAGE_TOPOLOGY = SINGLE_DATABASE_TOPOLOGY
KNOWN_STORAGE_TOPOLOGIES = frozenset(
    {
        SINGLE_DATABASE_TOPOLOGY,
        PER_WORKSPACE_DATABASE_TOPOLOGY,
    }
)
UNDECLARED_STORAGE_TOPOLOGY = "undeclared"


class StorageTopologyUnsupported(RuntimeError):
    """A provider declared a storage topology this build cannot serve."""


def declared_storage_topology(provider: object) -> str:
    """Return the topology ``provider`` declares, or ``"undeclared"``.

    A provider that does not carry the attribute at all is reported as
    ``undeclared`` rather than being assumed single-database. Undeclared is
    not refused -- there is nothing to refuse, no claim was made -- but it is
    reported distinctly so `/health/storage-topology` cannot show a foreign
    provider as if it had affirmed the shipped topology.
    """

    declared = getattr(provider, "storage_topology", None)
    if not isinstance(declared, str) or not declared:
        return UNDECLARED_STORAGE_TOPOLOGY
    return declared


def physical_split_compatibility() -> dict[str, object]:
    """The physical-split compatibility gate, as data.

    The edge census is NOT kept here. It is computed from ``Base.metadata``
    by ``backend/tenancy_manifest.py``, whose ``physical_split_blockers``
    docstring asks startup/readiness code to call it rather than maintain a
    second inventory -- so wiring a real per-workspace overlay goes red on
    the measured edge set and not on a list somebody has to remember to
    update. Imported lazily: importing the container must not drag the whole
    model graph in.

    ``compatible`` is True only when NO workspace/mixed-plane foreign key
    points at a control-plane table. It is False in this release: a shard
    built from the shard-resident tables alone cannot satisfy those edges,
    and SQLite fails such an INSERT outright (``OperationalError: no such
    table: main.<parent>``) rather than failing a referential check -- an
    absent parent table is not a nullability question.
    """

    from backend.tenancy_manifest import (
        physical_split_blockers,
        workspace_to_control_fk_edges,
    )

    edges = workspace_to_control_fk_edges()
    return {
        "compatible": not edges,
        "workspace_to_control_fk_edges": len(edges),
        "blocking_parent_tables": sorted({edge.parent for edge in edges}),
        "blockers": physical_split_blockers(),
    }


def check_storage_topology(provider: object) -> None:
    """Refuse startup for a topology this build cannot serve.

    Called from ``create_app`` before any router is mounted, so the process
    never accepts traffic on a topology whose writes cannot land.

    - ``single_database`` and ``undeclared`` pass.
    - ``per_workspace_database`` passes ONLY when the compatibility gate is
      green. It is red today, so declaring it refuses. The refusal is keyed
      on the live edge census rather than a constant, so it stops refusing by
      itself once the edges are actually gone -- nobody has to remember to
      delete this guard.
    - Any other value refuses: an unrecognized topology is a
      misconfiguration, and defaulting it to "probably fine" is how a split
      gets enabled by a typo.
    """

    topology = declared_storage_topology(provider)
    if topology in {SINGLE_DATABASE_TOPOLOGY, UNDECLARED_STORAGE_TOPOLOGY}:
        return
    if topology not in KNOWN_STORAGE_TOPOLOGIES:
        raise StorageTopologyUnsupported(
            f"unknown storage topology {topology!r}; this build serves "
            f"{SHIPPED_STORAGE_TOPOLOGY!r}"
        )
    gate = physical_split_compatibility()
    if gate["compatible"]:
        return
    detail = "\n".join(str(line) for line in gate["blockers"])
    raise StorageTopologyUnsupported(
        f"storage topology {topology!r} is unsupported by this build; it "
        f"serves {SHIPPED_STORAGE_TOPOLOGY!r}. Every count below was measured "
        f"from the running schema:\n{detail}"
    )


def storage_topology_report(provider: object) -> dict[str, object]:
    """Operator-readable topology block for ``/health/storage-topology``."""

    gate = physical_split_compatibility()
    return {
        "storage_topology": declared_storage_topology(provider),
        "shipped_storage_topology": SHIPPED_STORAGE_TOPOLOGY,
        "per_workspace_physical_databases": (
            "supported" if gate["compatible"] else "unsupported"
        ),
        "physical_split_compatible": gate["compatible"],
        "workspace_to_control_fk_edges": gate["workspace_to_control_fk_edges"],
    }


class DatabaseProvider:
    def __init__(
        self,
        settings: Settings | None,
        *,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        # settings may be None only when a session factory is injected
        # (request-scoped contexts synthesized around an existing factory).
        self.settings = settings
        self._session_factory = session_factory

    @cached_property
    def session_factory(self) -> sessionmaker[Session]:
        if self._session_factory is not None:
            factory = self._session_factory
        elif self.settings is None:
            raise ValueError(
                "DatabaseProvider needs settings or an injected session factory"
            )
        else:
            factory = make_session_factory_from_settings(self.settings)
        # Phase 6b (job hints, plan §3 D-2): stamp every Session this factory
        # creates with a back-reference to this provider, in session.info.
        # JobQueueService.enqueue reads it off the session it was already
        # given (session.info.get("database_provider")) to register a hint
        # write -- this is what lets the hint mechanism reach every one of
        # enqueue's many call sites (worker, library, extraction, bundle
        # expiration, ...) without threading a DatabaseProvider through each
        # one's constructor. Harmless where session_for(workspace_id) and
        # control_session_factory() return this same object (today, no real
        # sharding overlay): the stamp only matters once JobQueueService.
        # enqueue actually looks for it. An overlay that builds distinct
        # per-workspace sessionmakers should call .configure(info={...}) the
        # same way on each one it constructs to keep hints working.
        #
        # Guarded because an INJECTED factory is not required to be a real
        # sessionmaker -- this constructor's whole point is that an overlay
        # (or a test) can hand in any callable that yields sessions, and
        # `sessionmaker.configure` is not part of that contract. Calling it
        # unconditionally broke exactly that seam, which is the one Phase 7
        # depends on. A factory without `configure` simply carries no
        # back-reference, so its sessions produce no hints: latency, never
        # correctness, which is the same guarantee a dropped hint already
        # has (backend/services/job_hints.py).
        configure = getattr(factory, "configure", None)
        if callable(configure):
            configure(info={"database_provider": self})
        return factory

    # Declared, not inferred (see the storage-topology block above). The
    # default provider addresses ONE physical database: both accessors below
    # return the same sessionmaker. An overlay that gives each workspace its
    # own physical database must override this to
    # PER_WORKSPACE_DATABASE_TOPOLOGY, and create_app then refuses to start
    # while the physical-split compatibility gate is red.
    storage_topology: str = SINGLE_DATABASE_TOPOLOGY

    def control_session_factory(self) -> sessionmaker[Session]:
        return self.session_factory

    def session_for(self, workspace_id) -> sessionmaker[Session]:
        return self.session_factory


@runtime_checkable
class PurgeableBlobStore(Protocol):
    """The purge capability ``store_for`` must return for `?purge=true` to work.

    260912 review, F-1: before this Protocol existed, the workspace-delete
    route called ``store.purge_workspace(workspace_id)`` duck-typed -- a
    backend store that does not implement it raised ``AttributeError``, which
    a broad ``except Exception`` around that call swallowed into a log line.
    The workspace row and its content were already gone by then, so the
    failure mode was a permanent, silent byte leak, not a rejected request.

    This repo has no mypy/pyright gate, so declaring the Protocol alone
    catches nothing at runtime -- ``isinstance(store, PurgeableBlobStore)`` at
    the call site (``read.py``'s ``delete_workspace``, before anything is
    deleted) is what actually enforces it, rejecting with 503
    ``purge_unavailable`` instead of leaking bytes.
    """

    def purge_workspace(self, workspace_id: UUID) -> list[str]: ...


class BlobStoreProvider:
    """D-5 — the blob-storage analogue of ``DatabaseProvider``.

    Path layout is already workspace-partitioned (``b/{workspace_token}/...``);
    what's missing is a seam to hand a workspace a different *physical* store
    (disk, bucket, host). Default implementation ignores ``workspace_id`` and
    always returns the single settings-derived store — no behavior change. An
    overlay swaps this for one that routes by workspace.
    """

    def __init__(
        self,
        settings: Settings | None,
        *,
        store: object | None = None,
    ) -> None:
        # settings may be None only when a store is injected (mirrors
        # DatabaseProvider's session_factory injection contract).
        self.settings = settings
        self._store = store

    @cached_property
    def default_store(self):
        if self._store is not None:
            return self._store
        from backend.services.library_blob_storage import FilesystemLibraryBlobStore

        root = (
            Path(self.settings.library_blob_root)
            if self.settings is not None and self.settings.library_blob_root
            else None
        )
        return FilesystemLibraryBlobStore(root=root)

    def store_for(self, workspace_id: UUID | None) -> PurgeableBlobStore:
        return self.default_store


class WorkspaceDirectory:
    """R5′ lite — canonical workspace id lookups for routing decisions.

    Wraps the existing rbac resolvers (no new logic); an overlay replaces
    this to serve slug/page → workspace lookups from its control plane.
    """

    def workspace_id_by_slug(self, session, workspace_slug: str):
        from backend.policy.rbac import resolve_workspace_id_by_slug

        return resolve_workspace_id_by_slug(session, workspace_slug)

    def workspace_id_by_context_page(self, session, page_id):
        from backend.policy.rbac import resolve_workspace_id_by_context_page

        return resolve_workspace_id_by_context_page(session, page_id)

    def slug_by_workspace_id(self, session, workspace_id):
        from backend.policy.rbac import resolve_workspace_slug_by_id

        return resolve_workspace_slug_by_id(session, workspace_id)


class ServiceRegistry:
    """Container-owned domain services (R1′ — replaces module singletons).

    Services receive sessions or a routed provider per call. Container-owned
    state, such as the permalink request budget, persists across requests.
    The registry is the single rebinding point an overlay swaps instead of
    monkeypatching module globals. Imports stay lazy so importing the
    container never drags the service graph in.
    """

    settings: Settings | None = None
    lint_modules: tuple[LintModule, ...] = ()
    # Context media is workspace-owned data, so this holds a routing callable
    # (Callable[[UUID], sessionmaker[Session]]) -- get_services() stamps
    # self.db.session_for -- not one session_factory pinned to a plane.
    context_media_session_for: object | None = None
    _page_locations_lock = Lock()

    @cached_property
    def workspace_directory(self) -> WorkspaceDirectory:
        return WorkspaceDirectory()

    @property
    def page_locations(self):
        # This service owns request budgets, so concurrent first requests
        # must share one instance. Keep the existing dictionary override seam.
        with self._page_locations_lock:
            if "page_locations" not in self.__dict__:
                from backend.services.page_location import PageLocationService

                self.__dict__["page_locations"] = PageLocationService()
            return self.__dict__["page_locations"]

    @cached_property
    def tree_read(self):
        from backend.services.tree_read import TreeReadService

        return TreeReadService()

    @cached_property
    def tree(self):
        from backend.services.tree_service import TreeService

        return TreeService(tree_read_service=self.tree_read)

    @cached_property
    def workspace_read(self):
        from backend.services.workspace_read import WorkspaceReadService

        return WorkspaceReadService()

    @cached_property
    def workspace_write(self):
        from backend.services.workspace_write import WorkspaceWriteService

        return WorkspaceWriteService()

    @cached_property
    def domain_read(self):
        from backend.services.domain_read import DomainReadService

        return DomainReadService(
            self.tree_read,
            library_blob_repository_factory=self.library_blob_repository_factory,
            lint_registry=self.lint_registry,
        )

    @cached_property
    def library(self):
        from backend.services.library_service import LibraryService

        return LibraryService(
            logical_quota_factory=self.library_logical_quota_factory,
        )

    @cached_property
    def library_format_registry(self):
        from backend.services.library_formats import FormatRegistry

        return FormatRegistry()

    @cached_property
    def library_family_preset_catalog(self):
        from backend.catalog.library_family_presets import (
            LibraryFamilyPresetCatalogRepository,
        )

        repository = LibraryFamilyPresetCatalogRepository(
            format_registry=self.library_format_registry,
        )
        repository.snapshot()
        return repository

    @cached_property
    def library_family_policy(self):
        from backend.services.library_policy import LibraryFamilyPolicyService

        return LibraryFamilyPolicyService(
            format_registry=self.library_format_registry,
            preset_catalog=self.library_family_preset_catalog,
        )

    @cached_property
    def blob_store_provider(self) -> BlobStoreProvider:
        return BlobStoreProvider(self.settings)

    @cached_property
    def media_blob_gc(self):
        """Physical GC authority routed through the workspace DB and store.

        The service owns several short, separately committed transactions, so
        it receives the routing callable itself rather than a caller-owned
        session.  Storage remains a per-workspace adapter selected from the
        same provider graph used by uploads, conversion, Preview, and reads.
        """

        from backend.media.adapters.sqlalchemy import SqlAlchemyBlobGcService
        from backend.services.media_blob_gc_holds import media_blob_gc_hold_is_active

        if not callable(self.context_media_session_for):
            raise RuntimeError("Media blob GC requires a workspace session router")
        return SqlAlchemyBlobGcService(
            session_for=self.context_media_session_for,
            store_for=self.blob_store_provider.store_for,
            hold_is_active=media_blob_gc_hold_is_active,
        )

    @cached_property
    def library_blob_repository_factory(self):
        from backend.library_media.adapters.sqlalchemy import (
            SqlAlchemyLibraryBlobRepository,
        )
        from backend.media.adapters.sqlalchemy.quota import SqlAlchemyPhysicalQuota
        from backend.media.adapters.sqlalchemy.repository import (
            SqlAlchemyMediaRepository,
        )

        def build(transaction):
            return SqlAlchemyLibraryBlobRepository(
                transaction,
                media_repository=SqlAlchemyMediaRepository(transaction),
                physical_quota=SqlAlchemyPhysicalQuota(transaction),
            )

        return build

    @cached_property
    def library_logical_quota_factory(self):
        from backend.library_media.adapters.sqlalchemy import (
            SqlAlchemyLibraryLogicalQuota,
        )

        def build(transaction):
            return SqlAlchemyLibraryLogicalQuota(transaction)

        return build

    @cached_property
    def available_blob_repository_factory(self):
        from backend.media.adapters.sqlalchemy.available import (
            SqlAlchemyAvailableBlobRepository,
        )
        from backend.media.adapters.sqlalchemy.quota import SqlAlchemyPhysicalQuota
        from backend.media.adapters.sqlalchemy.repository import (
            SqlAlchemyMediaRepository,
        )

        def build(transaction):
            return SqlAlchemyAvailableBlobRepository(
                transaction,
                media_repository=SqlAlchemyMediaRepository(transaction),
                physical_quota=SqlAlchemyPhysicalQuota(transaction),
            )

        return build

    @cached_property
    def context_media_repository_factory(self):
        from backend.context_media.adapters.sqlalchemy import (
            SqlAlchemyContextMediaRepository,
        )

        def build(transaction):
            return SqlAlchemyContextMediaRepository(transaction)

        return build

    @cached_property
    def context_logical_quota_factory(self):
        from backend.context_media.adapters.sqlalchemy import (
            SqlAlchemyContextLogicalQuota,
        )

        def build(transaction):
            return SqlAlchemyContextLogicalQuota(transaction)

        return build

    @cached_property
    def context_media(self):
        from backend.context_media.adapters.sqlalchemy import (
            SqlAlchemyContextMediaUnitOfWorkFactory,
        )
        from backend.context_media.application import ContextMediaService

        if self.context_media_session_for is None:
            raise RuntimeError("Context media requires a session factory")
        return ContextMediaService(
            SqlAlchemyContextMediaUnitOfWorkFactory(self.context_media_session_for)
        )

    @cached_property
    def context_media_upload(self):
        from backend.services.context_media_upload import ContextMediaUploadService

        return ContextMediaUploadService(
            media_installer_factory=self.durable_media_installer_factory,
            logical_quota_factory=self.context_logical_quota_factory,
            context_media_repository_factory=self.context_media_repository_factory,
        )

    @cached_property
    def context_image_ingest(self):
        from backend.services.context_image_ingest import ContextImageIngestService

        return ContextImageIngestService(self.context_media_upload)

    def durable_media_installer_factory(
        self,
        session_factory: sessionmaker[Session],
        workspace_id: UUID,
    ):
        """Return an installer whose journal owns short durable transactions.

        Callers must pass the exact Workspace factory selected for the request
        or claimed job. Falling back to the registry's control-plane factory
        would split physical Media rows from their domain associations under a
        routing-capable DatabaseProvider.
        """

        if session_factory is None:
            raise ValueError("Shared Media requires an explicit session factory")
        from backend.media.adapters.sqlalchemy import SqlAlchemyMediaInstallJournal

        return self._new_validated_media_installer(
            SqlAlchemyMediaInstallJournal(session_factory),
            workspace_id=workspace_id,
        )

    def durable_library_conversion_media_installer_factory(
        self,
        session_factory: sessionmaker[Session],
        workspace_id: UUID,
    ):
        """Build the durable installer profile for one conversion attempt.

        Conversion may materialize every admitted asset plus one Markdown
        artifact. Its internal attempt-scoped actor/session must therefore be
        allowed to stage that bounded set without weakening ordinary API and
        Context upload throttles.
        """

        from backend.media.adapters.sqlalchemy import SqlAlchemyMediaInstallJournal
        from backend.services.conversion_orchestrator import (
            MAX_DURABLE_CONVERSION_OUTPUTS,
        )
        from backend.spi.conversion import ConversionLimits

        maximum_outputs = ConversionLimits(max_input_bytes=1).max_assets + 1
        if MAX_DURABLE_CONVERSION_OUTPUTS < maximum_outputs:
            raise RuntimeError(
                "Library conversion staging profile is below the output limit"
            )
        settings = self._require_settings_for_library()
        maximum_staged_output_bytes = max(
            settings.library_conversion_max_output_bytes,
            settings.library_conversion_max_asset_bytes,
        )
        return self._new_validated_media_installer(
            SqlAlchemyMediaInstallJournal(
                session_factory,
                actor_max_bytes=maximum_staged_output_bytes,
                session_max_bytes=maximum_staged_output_bytes,
                actor_rate_per_minute=MAX_DURABLE_CONVERSION_OUTPUTS,
                session_rate_per_minute=MAX_DURABLE_CONVERSION_OUTPUTS,
            ),
            workspace_id=workspace_id,
        )

    def durable_library_preview_media_installer_factory(
        self,
        session_factory: sessionmaker[Session],
        workspace_id: UUID,
    ):
        """Build a bounded durable Shared Media profile for Preview variants."""

        from backend.media.adapters.sqlalchemy import SqlAlchemyMediaInstallJournal
        from backend.spi.preview import PreviewLimits

        settings = self._require_settings_for_library()
        limits = PreviewLimits(
            max_input_bytes=settings.library_upload_hard_cap_bytes,
            max_variant_bytes=min(
                settings.library_conversion_max_asset_bytes,
                settings.library_conversion_max_output_bytes,
            ),
            max_total_output_bytes=settings.library_conversion_max_output_bytes,
            timeout_seconds=settings.library_conversion_timeout_seconds,
        )
        return self._new_validated_media_installer(
            SqlAlchemyMediaInstallJournal(
                session_factory,
                actor_max_bytes=limits.max_total_output_bytes,
                session_max_bytes=limits.max_total_output_bytes,
                actor_rate_per_minute=limits.max_variants,
                session_rate_per_minute=limits.max_variants,
            ),
            workspace_id=workspace_id,
        )

    def validated_media_installer_for_session(
        self, session: Session, workspace_id: UUID
    ):
        """Legacy caller-bound installer used only inside one owned UoW."""

        from backend.media.adapters.sqlalchemy import SqlAlchemyMediaInstallJournal

        return self._new_validated_media_installer(
            SqlAlchemyMediaInstallJournal.for_session(session),
            workspace_id=workspace_id,
        )

    def _new_validated_media_installer(self, journal, *, workspace_id: UUID):
        from backend.media.application import InstallValidatedBlob

        # BlobStoreProvider.store_for(workspace_id) existed but nothing
        # called it with a real workspace_id -- every caller of the four
        # factory methods above already knows which workspace it is acting
        # for (the request, the claimed job, the conversion/preview attempt),
        # so that is what gets threaded through here instead of the
        # store_for(None) this method used to hardcode.
        store = self.blob_store_provider.store_for(workspace_id)
        return InstallValidatedBlob(
            stager=store,
            publisher=store,
            deleter=store,
            layout=store,
            journal=journal,
        )

    @cached_property
    def media_install_reconciler(self):
        """Compose the Shared Media crash reconciler at the application edge.

        Built once per registry and swept repeatedly across every workspace
        over its lifetime (worker/runtime.py's periodic sweep job carries
        job.workspace_id per call), so the store it uses has to be resolved
        per sweep too -- see SqlAlchemyMediaInstallReconciler.sweep_expired.
        """

        from backend.media.adapters.sqlalchemy.reconcile import (
            SqlAlchemyMediaInstallReconciler,
        )

        return SqlAlchemyMediaInstallReconciler(
            store_for=self.blob_store_provider.store_for,
        )

    @cached_property
    def library_converter_registry(self):
        from backend.services.converter_registry import build_converter_registry

        settings = self._require_settings_for_library()
        return build_converter_registry(
            external_manifests_json=settings.library_converter_manifests_json,
            os_sandboxed=settings.library_converter_os_sandboxed,
            network_allowed=settings.library_converter_network_allowed,
        )

    def library_upload_admission_for_session_factory(
        self,
        session_factory: sessionmaker[Session],
        workspace_id: UUID,
    ):
        from backend.services.library_ingest import LibraryUploadAdmissionService

        return LibraryUploadAdmissionService(
            store_for=self.blob_store_provider.store_for,
            format_registry=self.library_format_registry,
            media_installer=self.durable_media_installer_factory(
                session_factory, workspace_id
            ),
        )

    def library_upload_admission_for_session(
        self, session: Session, workspace_id: UUID
    ):
        from backend.services.library_ingest import LibraryUploadAdmissionService

        return LibraryUploadAdmissionService(
            store_for=self.blob_store_provider.store_for,
            format_registry=self.library_format_registry,
            media_installer=self.validated_media_installer_for_session(
                session, workspace_id
            ),
        )

    @cached_property
    def library_capabilities(self):
        from backend.services.library_capabilities import LibraryCapabilityService

        settings = self._require_settings_for_library()
        return LibraryCapabilityService(
            format_registry=self.library_format_registry,
            converter_registry=self.library_converter_registry,
            family_policy=self.library_family_policy,
            preview_provider_registry=self.library_preview_provider_registry,
            ingest_enabled=settings.library_blob_ingest_enabled,
            conversion_enabled=settings.library_conversion_enabled,
            instance_hard_cap_bytes=settings.library_upload_hard_cap_bytes,
        )

    @cached_property
    def library_content(self):
        from backend.services.library_content_service import LibraryContentService

        return LibraryContentService(
            store_for=self.blob_store_provider.store_for,
            blob_repository_factory=self.library_blob_repository_factory,
        )

    @cached_property
    def library_semantic_overlay(self):
        from backend.services.library_semantic_overlay_service import (
            LibrarySemanticOverlayService,
        )

        return LibrarySemanticOverlayService(job_queue=self.job_queue)

    @cached_property
    def library_preview_provider_registry(self):
        from backend.services.preview_provider_registry import (
            PreviewProviderRegistry,
        )

        # Native decoder providers remain installed but disabled until an
        # operator-supplied OS resource/network sandbox profile is composed.
        # Structurally admitted PDF/JPEG/PNG originals use the typed source
        # Preview transport and do not require a generated Preview attempt.
        return PreviewProviderRegistry()

    @cached_property
    def library_preview_execution_boundary(self):
        from backend.services.preview_execution import (
            BundledSubprocessPreviewExecutionBoundary,
        )

        # Every decoder registered above must also have an explicit fixed host
        # route here. Unknown future builtins fail closed instead of silently
        # falling back to an in-process timeout.
        return BundledSubprocessPreviewExecutionBoundary()

    @cached_property
    def library_preview_orchestrator(self):
        from backend.services.preview_orchestrator import (
            LibraryPreviewOrchestrator,
        )
        from backend.spi.preview import PreviewLimits

        settings = self._require_settings_for_library()
        default_limits = PreviewLimits(
            max_input_bytes=settings.library_upload_hard_cap_bytes,
            max_variant_bytes=min(
                settings.library_conversion_max_asset_bytes,
                settings.library_conversion_max_output_bytes,
            ),
            max_total_output_bytes=settings.library_conversion_max_output_bytes,
            timeout_seconds=settings.library_conversion_timeout_seconds,
        )
        return LibraryPreviewOrchestrator(
            provider_registry=self.library_preview_provider_registry,
            store_for=self.blob_store_provider.store_for,
            blob_repository_factory=self.library_blob_repository_factory,
            media_installer_factory=self.validated_media_installer_for_session,
            durable_media_installer_factory=(
                self.durable_library_preview_media_installer_factory
            ),
            logical_quota_factory=self.library_logical_quota_factory,
            job_queue=self.job_queue,
            execution_boundary=self.library_preview_execution_boundary,
            default_limits=default_limits,
        )

    @cached_property
    def library_preview_manifest(self):
        from backend.services.preview_manifest_service import (
            LibraryPreviewManifestService,
        )

        return LibraryPreviewManifestService(
            store_for=self.blob_store_provider.store_for,
            blob_repository_factory=self.library_blob_repository_factory,
        )

    @cached_property
    def library_conversion(self):
        from backend.services.conversion_orchestrator import (
            LibraryConversionOrchestrator,
        )

        settings = self._require_settings_for_library()
        chains = _parse_converter_chains(
            settings.library_converter_chains_json,
            registry=self.library_converter_registry,
        )
        return LibraryConversionOrchestrator(
            converter_registry=self.library_converter_registry,
            store_for=self.blob_store_provider.store_for,
            blob_repository_factory=self.library_blob_repository_factory,
            media_installer_factory=self.validated_media_installer_for_session,
            durable_media_installer_factory=(
                self.durable_library_conversion_media_installer_factory
            ),
            logical_quota_factory=self.library_logical_quota_factory,
            job_queue=self.job_queue,
            preferred_chains=chains,
            allow_network=settings.library_converter_network_allowed,
            conversion_enabled=settings.library_conversion_enabled,
            preview_orchestrator=self.library_preview_orchestrator,
        )

    @cached_property
    def library_binary_registration(self):
        from backend.services.library_binary_registration import (
            LibraryBinaryRegistrationService,
        )

        return LibraryBinaryRegistrationService(
            conversion_orchestrator=self.library_conversion,
            preview_orchestrator=self.library_preview_orchestrator,
            blob_repository_factory=self.library_blob_repository_factory,
            logical_quota_factory=self.library_logical_quota_factory,
        )

    def _require_settings_for_library(self) -> Settings:
        if self.settings is None:
            raise RuntimeError(
                "Library binary services require container-resolved settings"
            )
        return self.settings

    @cached_property
    def activity(self):
        from backend.services.activity_service import ActivityService

        return ActivityService(review_service=self.review)

    @cached_property
    def settings_svc(self):
        from backend.services.settings_service import SettingsService

        return SettingsService(
            lint_registry=self.lint_registry,
            library_family_policy=self.library_family_policy,
            orphan_retention_hours=(
                self.settings.context_media_orphan_retention_hours
                if self.settings is not None
                else 168
            ),
        )

    @cached_property
    def context_ingress(self):
        from backend.services.context_ingress_service import ContextIngressService

        return ContextIngressService(
            body_preparer=self.body_preparer,
            tree_read_service=self.tree_read,
            context_media_repository_factory=self.context_media_repository_factory,
        )

    @cached_property
    def agent_page(self):
        from backend.services.agent_page_service import AgentPageService

        return AgentPageService()

    @cached_property
    def bundle(self):
        from backend.services.bundle_service import AgentInputBundleService

        return AgentInputBundleService()

    @cached_property
    def context_write(self):
        from backend.services.context_write_service import ContextWriteService

        return ContextWriteService(
            body_preparer=self.body_preparer,
            context_media_repository_factory=self.context_media_repository_factory,
            media_installer_factory=self.validated_media_installer_for_session,
        )

    @cached_property
    def citation(self):
        from backend.services.citation_service import CitationService

        return CitationService()

    @cached_property
    def feedback(self):
        from backend.services.feedback_service import FeedbackService

        return FeedbackService()

    @cached_property
    def review(self):
        from backend.services.review_service import ReviewService

        return ReviewService(
            context_media_repository_factory=self.context_media_repository_factory,
            body_preparer=self.body_preparer,
        )

    @cached_property
    def body_preparer(self):
        from backend.services.body_preparer import BodyPreparer

        return BodyPreparer(registry=self.lint_registry)

    @cached_property
    def lint_registry(self):
        from backend.services.lint.modules import BUILTIN_LINT_MODULES
        from backend.services.lint.registry import LintRegistry

        return LintRegistry((*BUILTIN_LINT_MODULES, *self.lint_modules))

    @cached_property
    def lint(self):
        from backend.services.lint_service import LintService

        return LintService(job_queue=self.job_queue, lint_registry=self.lint_registry)

    @cached_property
    def job_queue(self):
        from backend.services.job_queue import JobQueueService

        return JobQueueService()

    @cached_property
    def job_read(self):
        from backend.services.job_read_service import JobReadService

        return JobReadService()

    @cached_property
    def mcp_chat(self):
        from backend.services.mcp_chat_service import McpChatService

        return McpChatService()

    @cached_property
    def graph_service(self):
        from backend.services.graph_service import GraphService

        return GraphService()

    @cached_property
    def keyword_only_search(self):
        # Settings-free keyword-only SearchService: the last-resort fallback
        # for contexts constructed without an injected search service (tests,
        # bare dispatch). Production paths always inject the container-built
        # service, so no fresh Settings() is ever read here.
        from backend.services.search_service import SearchService

        return SearchService()


def _parse_converter_chains(raw_json: str, *, registry) -> dict[str, tuple[str, ...]]:
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError("Library converter chains must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Library converter chains must be a JSON object")
    chains: dict[str, tuple[str, ...]] = {}
    for format_id, raw_chain in payload.items():
        if not isinstance(format_id, str) or not format_id:
            raise ValueError("Library converter chain format id is invalid")
        if not isinstance(raw_chain, list) or not all(
            isinstance(converter_id, str) and converter_id for converter_id in raw_chain
        ):
            raise ValueError("Library converter chain must be a converter id list")
        chain = tuple(raw_chain)
        if len(chain) != len(set(chain)):
            raise ValueError("Library converter chain contains duplicate providers")
        for converter_id in chain:
            descriptor = registry.descriptor(converter_id)
            if format_id not in descriptor.formats:
                raise ValueError(
                    "Library converter chain provider does not support its format"
                )
        chains[format_id] = chain
    return chains


@dataclass
class AppContainer:
    settings: Settings
    db: DatabaseProvider
    page_service: object | None = None
    search_service: object | None = None
    ask_service: object | None = None
    services: ServiceRegistry = field(default_factory=ServiceRegistry)

    def get_page_service(self):
        if self.page_service is None:
            from backend.services.page_service import PageService

            self.page_service = PageService(
                self.db.control_session_factory(),
                job_queue=self.services.job_queue,
                body_preparer=self.services.body_preparer,
                lint_registry=self.services.lint_registry,
                context_media_repository_factory=(
                    self.services.context_media_repository_factory
                ),
            )
        return self.page_service

    def get_search_service(self):
        if self.search_service is None:
            from backend.services.search_service import build_search_service

            self.search_service = build_search_service(
                self.settings,
                graph_service=self.services.graph_service,
            )
        return self.search_service

    def get_ask_service(self):
        if self.ask_service is None:
            from backend.llm.registry import resolve_llm_provider
            from backend.services.ask_service import AskService

            # A bad DTXWIKI_LLM_PROVIDER setting must not 500 the Ask surface:
            # the provider is built during dependency resolution, so a raise here
            # escapes every handler's try/except and takes Ask down entirely.
            # Degrade to the deterministic evidence pack and carry the reason so
            # the user is told why the answer style changed.
            provider, reason = resolve_llm_provider(self.settings)
            if reason:
                # Say it once server-side. The UI tells the user to ask an admin
                # to check the LLM settings, and before the degrade this reason
                # was in the 500's traceback -- without this line the admin they
                # ask finds nothing in the log at all. Matches what
                # build_search_service already does for embedding/rerank.
                logger.warning("Ask answer model unavailable: %s", reason)
            self.ask_service = AskService(
                search_service=self.get_search_service(),
                provider=provider,
                graph_service=self.services.graph_service,
                provider_unavailable_reason=reason,
            )
        return self.ask_service

    def get_services(self) -> ServiceRegistry:
        # Tool handlers receive the registry through ToolCallContext. Stamp the
        # exact assembled Settings instance so injected providers never fall
        # through to process-global environment settings.
        self.services.settings = self.settings
        # D-7: bind the routing callable itself, not one resolved factory --
        # ContextMediaService calls this once per method with the caller's
        # workspace_id (see SqlAlchemyContextMediaUnitOfWorkFactory).
        self.services.context_media_session_for = self.db.session_for
        return self.services


def assemble(
    settings: Settings,
    *,
    db: DatabaseProvider | None = None,
    session_factory: sessionmaker[Session] | None = None,
    page_service: object | None = None,
    search_service: object | None = None,
    ask_service: object | None = None,
    directory: WorkspaceDirectory | None = None,
    services: ServiceRegistry | None = None,
    lint_modules: tuple[LintModule, ...] = (),
    blob_store_provider: BlobStoreProvider | None = None,
) -> AppContainer:
    if db is not None and session_factory is not None:
        raise ValueError("db and session_factory cannot both be provided")
    if lint_modules and services is not None and "lint_registry" in services.__dict__:
        raise ValueError(
            "lint modules must be injected before the lint registry is constructed"
        )
    resolved_db = (
        db
        if db is not None
        else DatabaseProvider(settings, session_factory=session_factory)
    )
    resolved_services = services if services is not None else ServiceRegistry()
    if lint_modules:
        resolved_services.lint_modules = (
            *resolved_services.lint_modules,
            *lint_modules,
        )
    if directory is not None:
        # WorkspaceDirectory is a cached_property on the default registry.
        # Seeding the cache gives overlays one explicit rebinding point while
        # preserving lazy construction when no override is supplied.
        resolved_services.__dict__["workspace_directory"] = directory
    if blob_store_provider is not None:
        # Same rebinding trick as workspace_directory above, for the D-5
        # blob-store seam.
        resolved_services.__dict__["blob_store_provider"] = blob_store_provider
    return AppContainer(
        settings=settings,
        db=resolved_db,
        page_service=page_service,
        search_service=search_service,
        ask_service=ask_service,
        services=resolved_services,
    )

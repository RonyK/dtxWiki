from uuid import uuid4

import pytest

from backend.app.container import (
    DatabaseProvider,
    ServiceRegistry,
    ToolCallContext,
    assemble,
)
from backend.app.settings import Settings
from backend.media.adapters.sqlalchemy.install import (
    DEFAULT_STAGING_ACTOR_MAX_BYTES,
    DEFAULT_STAGING_SESSION_MAX_BYTES,
)


def _settings() -> Settings:
    return Settings(database_url="sqlite+pysqlite:///:memory:")


def test_registry_shares_tree_read_wiring():
    registry = ServiceRegistry()

    assert registry.tree is registry.tree  # memoized
    assert registry.tree.tree_read_service is registry.tree_read
    assert registry.domain_read.tree_read_service is registry.tree_read
    assert registry.context_ingress.tree_read_service is registry.tree_read


def test_registry_memoizes_canonical_context_image_ingest_wiring():
    registry = ServiceRegistry()

    assert registry.context_image_ingest is registry.context_image_ingest
    assert (
        registry.context_image_ingest._context_media_upload
        is registry.context_media_upload
    )


def test_container_memoizes_service_registry():
    container = assemble(_settings())

    assert container.get_services() is container.get_services()


def test_container_stamps_settings_on_injected_service_registry():
    settings = _settings()
    registry = ServiceRegistry()
    container = assemble(settings, services=registry)

    assert container.get_services() is registry
    assert registry.settings is settings
    assert container.get_services().settings is settings


def test_container_threads_registry_graph_service_into_search():
    registry = ServiceRegistry()
    graph_service = object()
    registry.__dict__["graph_service"] = graph_service
    container = assemble(_settings(), services=registry)

    assert container.get_search_service()._graph_service is graph_service


def test_tool_call_context_binds_workspace_sessions():
    """R5′: a resolved tool call must acquire sessions through
    DatabaseProvider.session_for(workspace_id) so a routing-capable provider
    can shard; control contexts keep using the control factory."""

    calls: list[object] = []

    class RecordingProvider(DatabaseProvider):
        def session_for(self, workspace_id):
            calls.append(workspace_id)
            return super().session_for(workspace_id)

    provider = RecordingProvider(_settings())
    workspace_id = uuid4()
    bound = ToolCallContext(
        actor=object(),
        db=provider,
        page_service=object(),
        workspace_id=workspace_id,
    )
    control = ToolCallContext(actor=object(), db=provider, page_service=object())

    assert bound.session_factory is provider.session_factory
    assert calls == [workspace_id]
    assert control.session_factory is provider.control_session_factory()
    assert calls == [workspace_id]


def test_database_provider_requires_settings_or_factory():
    provider = DatabaseProvider(None)

    with pytest.raises(ValueError, match="settings or an injected session factory"):
        provider.session_factory  # noqa: B018 — cached_property access raises

    injected = DatabaseProvider(None, session_factory=object())
    assert injected.session_factory is not None


def test_durable_media_installer_requires_explicit_session_factory():
    registry = ServiceRegistry()

    with pytest.raises(ValueError, match="explicit session factory"):
        registry.durable_media_installer_factory(None, uuid4())


def test_conversion_installer_profile_rejects_spi_output_limit_drift(monkeypatch):
    class ExpandedConversionLimits:
        def __init__(self, *, max_input_bytes):
            del max_input_bytes
            self.max_assets = 65

    monkeypatch.setattr(
        "backend.spi.conversion.ConversionLimits",
        ExpandedConversionLimits,
    )
    registry = ServiceRegistry()

    with pytest.raises(RuntimeError, match="profile is below the output limit"):
        registry.durable_library_conversion_media_installer_factory(
            object(), uuid4()
        )


def test_conversion_installer_byte_profile_covers_configured_asset_boundary(
    tmp_path,
):
    configured_max_bytes = 1024 * 1024 * 1024
    registry = assemble(
        Settings(
            database_url="sqlite+pysqlite:///:memory:",
            library_blob_root=str(tmp_path / "conversion-profile"),
            library_conversion_max_asset_bytes=configured_max_bytes,
        )
    ).get_services()

    conversion = registry.durable_library_conversion_media_installer_factory(
        object(), uuid4()
    )
    ordinary = registry.durable_media_installer_factory(object(), uuid4())

    assert conversion._journal._actor_max_bytes == configured_max_bytes
    assert conversion._journal._session_max_bytes == configured_max_bytes
    assert ordinary._journal._actor_max_bytes == DEFAULT_STAGING_ACTOR_MAX_BYTES
    assert ordinary._journal._session_max_bytes == DEFAULT_STAGING_SESSION_MAX_BYTES

"""Graph service break-glass settings contract."""

from backend.app.settings import Settings


def test_graph_service_switch_defaults_on_and_reads_its_documented_env(monkeypatch):
    monkeypatch.delenv("DTXWIKI_GRAPH_SERVICE_AVAILABLE", raising=False)
    assert Settings().graph_service_available is True

    monkeypatch.setenv("DTXWIKI_GRAPH_SERVICE_AVAILABLE", "false")
    assert Settings().graph_service_available is False


def test_retired_workspace_map_env_no_longer_switches_the_product(monkeypatch):
    """The owner's Settings card is the only product switch now."""

    monkeypatch.setenv("DTXWIKI_WORKSPACE_GRAPH_ENABLED", "true")

    assert not hasattr(Settings(), "workspace_graph_enabled")

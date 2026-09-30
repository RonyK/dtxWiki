from __future__ import annotations

import hashlib
from dataclasses import replace
from io import BytesIO

import pytest

from backend.services.preview_orchestrator import _BufferedPreviewSink
from backend.services.preview_provider_registry import (
    PreviewProviderRegistry,
    PreviewProviderRegistryError,
)
from backend.spi.preview import (
    PREVIEW_PROVIDER_SPI_VERSION,
    PreviewLimits,
    PreviewProviderDescriptor,
    PreviewProviderError,
)


class _Plugin:
    def __init__(
        self,
        descriptor: PreviewProviderDescriptor,
        *,
        healthy: bool = True,
    ) -> None:
        self._descriptor = descriptor
        self._healthy = healthy

    def descriptor(self) -> PreviewProviderDescriptor:
        return self._descriptor

    def probe(self, _request):  # pragma: no cover - protocol stub
        raise AssertionError("not called")

    def generate(self, _request, _sink):  # pragma: no cover - protocol stub
        raise AssertionError("not called")

    def health(self) -> bool:
        return self._healthy


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _descriptor(
    provider_id: str,
    *,
    priority: int = 100,
    execution_mode: str = "builtin",
    network_required: bool = False,
) -> PreviewProviderDescriptor:
    external = execution_mode != "builtin"
    return PreviewProviderDescriptor(
        provider_id=provider_id,
        provider_version="1.2.3",
        spi_version=PREVIEW_PROVIDER_SPI_VERSION,
        formats=frozenset({"pdf"}),
        media_types=frozenset({"application/pdf"}),
        input_kinds=frozenset({"source"}),
        output_renderer_contracts=frozenset({"core.image.v1"}),
        output_media_types=frozenset({"image/png"}),
        priority=priority,
        execution_mode=execution_mode,  # type: ignore[arg-type]
        network_required=network_required,
        deterministic=True,
        license_expression="Apache-2.0",
        source_url="https://example.test/preview-provider",
        distribution_digest=_digest(f"{provider_id}-distribution"),
        sbom_sha256=_digest(f"{provider_id}-sbom") if external else None,
        signature_identity="sigstore:preview@example.test" if external else None,
        sandbox_profile="preview-offline-v1" if external else None,
        max_input_bytes=10_000_000,
    )


def test_registry_snapshots_a_deterministic_ready_chain() -> None:
    preferred = _Plugin(_descriptor("builtin.preferred", priority=1))
    higher_priority = _Plugin(_descriptor("builtin.high", priority=200))
    unhealthy = _Plugin(_descriptor("builtin.unhealthy", priority=500), healthy=False)
    registry = PreviewProviderRegistry((higher_priority, unhealthy, preferred))

    snapshots = registry.provider_chain_snapshot(
        format_id="pdf",
        media_type="application/pdf",
        input_kind="source",
        preferred_ids=("builtin.preferred",),
    )

    assert [value["provider_id"] for value in snapshots] == [
        "builtin.preferred",
        "builtin.high",
    ]
    assert snapshots[0]["renderer_contracts"] == ["core.image.v1"]
    assert registry.plugin_for_snapshot(snapshots[0]) is preferred
    with pytest.raises(PreviewProviderRegistryError):
        registry.plugin_for_snapshot({**snapshots[0], "provider_version": "changed"})


def test_registry_rejects_builtin_without_verifiable_distribution_provenance() -> None:
    descriptor = replace(
        _descriptor("builtin.missing-provenance"),
        distribution_digest=None,
    )

    with pytest.raises(PreviewProviderRegistryError, match="distribution digest"):
        PreviewProviderRegistry((_Plugin(descriptor),))


def test_external_provider_fails_closed_without_a_killable_host() -> None:
    plugin = _Plugin(_descriptor("vendor.pdf", execution_mode="subprocess"))

    with pytest.raises(PreviewProviderRegistryError, match="host is unavailable"):
        PreviewProviderRegistry((plugin,), os_sandboxed=True)


def test_networked_sidecar_also_fails_closed_without_a_host() -> None:
    plugin = _Plugin(
        _descriptor(
            "vendor.remote",
            execution_mode="sidecar",
            network_required=True,
        )
    )

    with pytest.raises(PreviewProviderRegistryError, match="host is unavailable"):
        PreviewProviderRegistry(
            (plugin,),
            os_sandboxed=True,
            network_allowed=True,
        )


@pytest.mark.parametrize(
    "overrides",
    (
        {"delivery": "range"},
        {"width": 11, "height": 10},
        {"duration_ms": 1},
        {"page_count": 1},
    ),
)
def test_preview_sink_rejects_renderer_and_resource_contract_mismatches(
    overrides: dict,
) -> None:
    sink = _BufferedPreviewSink(
        limits=PreviewLimits(
            max_input_bytes=1,
            max_pixels=100,
            max_pages=1,
            max_duration_seconds=1,
        ),
        descriptor=_descriptor("builtin.bounded"),
    )
    content = b"x"
    arguments = {
        "source": BytesIO(content),
        "expected_sha256": hashlib.sha256(content).hexdigest(),
        "expected_byte_size": len(content),
        "kind": "image",
        "media_type": "image/png",
        "renderer_contract": "core.image.v1",
        "delivery": "inline",
        "rank": 0,
        "width": 1,
        "height": 1,
    }
    arguments.update(overrides)

    with pytest.raises(PreviewProviderError, match="approved contract"):
        sink.write_variant(**arguments)
    sink.close()


def test_preview_sink_rejects_duplicate_kind_and_ordinal() -> None:
    sink = _BufferedPreviewSink(
        limits=PreviewLimits(max_input_bytes=1),
        descriptor=_descriptor("builtin.unique"),
    )
    content = b"x"
    arguments = {
        "expected_sha256": hashlib.sha256(content).hexdigest(),
        "expected_byte_size": len(content),
        "kind": "image",
        "media_type": "image/png",
        "renderer_contract": "core.image.v1",
        "delivery": "inline",
        "rank": 0,
        "width": 1,
        "height": 1,
    }
    sink.write_variant(source=BytesIO(content), **arguments)

    with pytest.raises(PreviewProviderError, match="approved contract"):
        sink.write_variant(source=BytesIO(content), **arguments)
    sink.close()

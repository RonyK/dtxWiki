from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from typing import Any

import pytest

from backend.models.workspace import default_library_policy
from backend.services.converter_registry import (
    ConverterRegistry,
    ConverterRegistryError,
    build_converter_registry,
)
from backend.services.library_capabilities import LibraryCapabilityService
from backend.services.library_formats import FormatRegistry
from backend.spi.conversion import (
    CONVERSION_OUTPUT_SCHEMA_VERSION,
    CONVERTER_SPI_VERSION,
    ConverterDescriptor,
)


class _Plugin:
    def __init__(self, descriptor: ConverterDescriptor) -> None:
        self._descriptor = descriptor
        self.descriptor_calls = 0

    def descriptor(self) -> ConverterDescriptor:
        self.descriptor_calls += 1
        return self._descriptor

    def probe(self, _request):  # pragma: no cover - registry protocol stub
        raise AssertionError("not called")

    def convert(self, _request):  # pragma: no cover - registry protocol stub
        raise AssertionError("not called")


class _HealthPlugin(_Plugin):
    def __init__(
        self,
        descriptor: ConverterDescriptor,
        health: tuple[bool, str | None],
    ) -> None:
        super().__init__(descriptor)
        self._health = health
        self.healthcheck_calls = 0

    def healthcheck(self) -> tuple[bool, str | None]:
        self.healthcheck_calls += 1
        return self._health


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _descriptor(
    converter_id: str,
    *,
    priority: int = 100,
    network_required: bool = False,
    execution_mode: str = "builtin",
    deterministic: bool = True,
) -> ConverterDescriptor:
    return ConverterDescriptor(
        converter_id=converter_id,
        converter_version="1.2.3",
        spi_version=CONVERTER_SPI_VERSION,
        formats=frozenset({"test-format"}),
        media_types=frozenset({"application/x-test"}),
        priority=priority,
        execution_mode=execution_mode,  # type: ignore[arg-type]
        network_required=network_required,
        deterministic=deterministic,
        license_expression="Apache-2.0",
        source_url="https://example.test/converter",
        capabilities=frozenset({"markdown"}),
        distribution_digest=(
            _digest(converter_id) if execution_mode != "builtin" else None
        ),
    )


def _manifest(**overrides: Any) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "enabled": True,
        "converter_id": "vendor.pdf",
        "converter_version": "4.5.0",
        "spi_version": CONVERTER_SPI_VERSION,
        "output_schema_version": CONVERSION_OUTPUT_SCHEMA_VERSION,
        "formats": ["pdf"],
        "media_types": ["application/pdf"],
        "priority": 50,
        "execution_mode": "subprocess",
        "network_required": False,
        "network_approved": False,
        "deterministic": True,
        "license_expression": "MIT",
        "license_approved": True,
        "source_url": "https://example.test/vendor-pdf",
        "capabilities": ["markdown", "locations"],
        "distribution_digest": _digest("vendor-pdf-artifact"),
        "sbom_sha256": _digest("vendor-pdf-sbom"),
        "sbom_approved": True,
        "signature_approved": True,
        "signature_identity": "sigstore:vendor@example.test",
        "sandbox_profile": "oci-network-none-v1",
        "sandbox_approved": True,
        "command": [sys.executable, "-m", "vendor_pdf_host"],
    }
    manifest.update(overrides)
    return manifest


def test_builtin_registry_uses_real_sha_for_bundled_subprocess() -> None:
    registry = build_converter_registry()

    health = {item.converter_id: item for item in registry.health()}
    digest = health["dtxwiki.ooxml"].distribution_digest
    assert digest is not None
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")
    assert digest != "0" * 64
    assert registry.available_formats() >= {
        "csv",
        "docx",
        "json",
        "markdown",
        "html",
        "javascript",
        "pptx",
        "text",
        "xlsx",
        "yaml",
    }
    assert registry.ready_formats() == registry.available_formats()


def test_default_capabilities_name_exact_upload_and_conversion_boundaries() -> None:
    service = LibraryCapabilityService(
        format_registry=FormatRegistry(),
        converter_registry=build_converter_registry(),
        ingest_enabled=True,
        conversion_enabled=True,
        instance_hard_cap_bytes=50_000_000,
    )

    formats = {
        item["format_id"]: item
        for item in service.project(default_library_policy())["formats"]
    }
    uploadable = {format_id for format_id, item in formats.items() if item["upload"]}
    available = {
        format_id
        for format_id, item in formats.items()
        if item["conversion"] == "available"
    }
    stored_unconverted = {
        format_id
        for format_id, item in formats.items()
        if item["conversion"] == "stored_unconverted"
    }

    assert uploadable == {
        "markdown",
        "text",
        "html",
        "javascript",
        "csv",
        "tsv",
        "json",
        "yaml",
        "pdf",
        "docx",
        "xlsx",
        "pptx",
        "jpeg",
        "png",
        "hdf5",
        "parquet",
        "mp3",
        "ogg",
        "wav",
        "mp4",
        "webm",
    }
    assert available == {
        "markdown",
        "text",
        "html",
        "javascript",
        "csv",
        "tsv",
        "json",
        "yaml",
        "docx",
        "xlsx",
        "pptx",
    }
    assert stored_unconverted == {
        "pdf",
        "jpeg",
        "png",
        "hdf5",
        "parquet",
        "mp3",
        "ogg",
        "wav",
        "mp4",
        "webm",
    }


def test_capability_catalog_separates_policy_intent_from_runtime_readiness() -> None:
    service = LibraryCapabilityService(
        format_registry=FormatRegistry(),
        converter_registry=build_converter_registry(),
        ingest_enabled=True,
        conversion_enabled=True,
        instance_hard_cap_bytes=50_000_000,
    )

    payload = service.project(default_library_policy())
    formats = {item["format_id"]: item for item in payload["formats"]}

    assert payload["catalog_revision"] != payload["policy_revision"]
    assert "current_profile" not in payload
    assert "profiles" not in payload
    assert payload["policy_schema_version"] == 5
    assert payload["families"] == []
    assert [item["preset_id"] for item in payload["family_presets"]] == [
        "text",
        "document",
        "image",
        "audio",
        "video",
        "data",
    ]
    assert formats["mp4"]["policy_enabled"] is True
    assert formats["mp4"]["admission_state"] == "ready"
    assert formats["mp4"]["upload"] is True
    assert formats["mp4"]["conversion_state"] == "stored_unconverted"
    assert formats["mp4"]["preview_state"] == "ready"
    assert formats["mp4"]["render_state"] == "available"
    for format_id in ("pdf", "jpeg", "png"):
        assert formats[format_id]["preview_state"] == "ready"
        detector_id = (
            "pdf" if format_id == "pdf" else formats[format_id]["category"]
        )
        assert formats[format_id]["provider_ids"] == [
            f"core.detector.{detector_id}"
        ]
    for format_id in ("mp3", "ogg", "wav", "mp4", "webm"):
        assert formats[format_id]["preview_state"] == "ready"
        assert formats[format_id]["provider_ids"] == [f"core.detector.{format_id}"]
    assert formats["doc"]["policy_enabled"] is True
    assert formats["doc"]["admission_state"] == "validator_unavailable"
    assert formats["doc"]["upload"] is False
    assert formats["doc"]["reason"] == "validator_unavailable"


def test_descriptor_is_snapshotted_once_and_selection_is_deterministic() -> None:
    lower = _Plugin(_descriptor("vendor.lower", priority=10))
    higher = _Plugin(_descriptor("vendor.higher", priority=20))
    registry = ConverterRegistry((lower, higher))

    lower._descriptor = replace(lower._descriptor, priority=999)

    assert [item.converter_id for item in registry.select_chain("test-format")] == [
        "vendor.higher",
        "vendor.lower",
    ]
    assert lower.descriptor_calls == 1
    assert higher.descriptor_calls == 1
    assert registry.descriptor("vendor.lower").priority == 10
    assert [
        item.converter_id
        for item in registry.select_chain(
            "test-format", preferred_ids=("vendor.lower",)
        )
    ] == ["vendor.lower", "vendor.higher"]


def test_network_provider_is_filtered_without_boolean_coercion() -> None:
    local = _Plugin(_descriptor("vendor.local", priority=10))
    remote = _Plugin(
        _descriptor(
            "vendor.remote",
            priority=20,
            network_required=True,
            execution_mode="subprocess",
        )
    )
    registry = ConverterRegistry((local, remote))

    assert [item.converter_id for item in registry.select_chain("test-format")] == [
        "vendor.local"
    ]
    assert [
        item.converter_id
        for item in registry.select_chain("test-format", allow_network=True)
    ] == ["vendor.remote", "vendor.local"]
    with pytest.raises(ConverterRegistryError, match="boolean"):
        registry.select_chain("test-format", allow_network=1)  # type: ignore[arg-type]
    with pytest.raises(ConverterRegistryError, match="boolean"):
        registry.select_chain("test-format", ready_only=1)  # type: ignore[arg-type]


def test_ready_only_chain_skips_unhealthy_higher_priority_provider() -> None:
    unhealthy = _HealthPlugin(
        _descriptor(
            "vendor.unhealthy",
            priority=200,
            execution_mode="subprocess",
        ),
        (False, "converter_health_unavailable"),
    )
    healthy = _HealthPlugin(
        _descriptor(
            "vendor.healthy",
            priority=100,
            execution_mode="subprocess",
        ),
        (True, None),
    )
    registry = ConverterRegistry((unhealthy, healthy))

    assert [item.converter_id for item in registry.select_chain("test-format")] == [
        "vendor.unhealthy",
        "vendor.healthy",
    ]
    assert [
        item.converter_id
        for item in registry.select_chain("test-format", ready_only=True)
    ] == ["vendor.healthy"]
    assert [
        item["converter_id"]
        for item in registry.provider_chain_snapshot(
            "test-format",
            ready_only=True,
        )
    ] == ["vendor.healthy"]
    assert unhealthy.healthcheck_calls == 1
    assert healthy.healthcheck_calls == 1


def test_nondeterministic_provider_is_visible_but_never_ready_or_selected() -> None:
    nondeterministic = _HealthPlugin(
        _descriptor(
            "vendor.nondeterministic",
            priority=200,
            execution_mode="subprocess",
            deterministic=False,
        ),
        (True, None),
    )
    deterministic = _HealthPlugin(
        _descriptor(
            "vendor.deterministic",
            priority=100,
            execution_mode="subprocess",
        ),
        (True, None),
    )
    registry = ConverterRegistry((nondeterministic, deterministic))

    health = {item.converter_id: item for item in registry.health()}
    assert health["vendor.nondeterministic"].enabled is True
    assert health["vendor.nondeterministic"].ready is False
    assert health["vendor.nondeterministic"].reason_code == (
        "converter_nondeterministic_resume_unsupported"
    )
    assert nondeterministic.healthcheck_calls == 0
    assert health["vendor.deterministic"].ready is True
    assert health["vendor.deterministic"].reason_code is None
    assert deterministic.healthcheck_calls == 1
    assert registry.available_formats() == {"test-format"}
    assert registry.ready_formats() == {"test-format"}
    assert [
        item.converter_id for item in registry.select_chain("test-format")
    ] == ["vendor.deterministic"]
    assert [
        item["converter_id"]
        for item in registry.provider_chain_snapshot(
            "test-format",
            ready_only=True,
        )
    ] == ["vendor.deterministic"]


def test_capabilities_store_nondeterministic_only_format_unconverted() -> None:
    nondeterministic_pdf = replace(
        _descriptor("vendor.nondeterministic-pdf", deterministic=False),
        formats=frozenset({"pdf"}),
        media_types=frozenset({"application/pdf"}),
    )
    deterministic_pdf = replace(
        _descriptor("vendor.deterministic-pdf"),
        formats=frozenset({"pdf"}),
        media_types=frozenset({"application/pdf"}),
    )

    def pdf_capability(descriptor: ConverterDescriptor) -> dict[str, Any]:
        service = LibraryCapabilityService(
            format_registry=FormatRegistry(),
            converter_registry=ConverterRegistry((_Plugin(descriptor),)),
            ingest_enabled=True,
            conversion_enabled=True,
            instance_hard_cap_bytes=50_000_000,
        )
        return next(
            item
            for item in service.project(default_library_policy())["formats"]
            if item["format_id"] == "pdf"
        )

    blocked = pdf_capability(nondeterministic_pdf)
    available = pdf_capability(deterministic_pdf)

    assert blocked["upload"] is True
    assert blocked["conversion"] == "stored_unconverted"
    assert available["upload"] is True
    assert available["conversion"] == "available"


def test_priority_collision_rejects_registration_without_partial_mutation() -> None:
    first = _Plugin(_descriptor("vendor.first"))
    registry = ConverterRegistry((first,))

    with pytest.raises(ConverterRegistryError, match="priority collision"):
        registry.register(_Plugin(_descriptor("vendor.second")))

    assert [item.converter_id for item in registry.health()] == ["vendor.first"]


def test_duplicate_id_and_preference_are_rejected() -> None:
    registry = ConverterRegistry((_Plugin(_descriptor("vendor.same")),))

    with pytest.raises(ConverterRegistryError, match="duplicate converter id"):
        registry.register(_Plugin(_descriptor("vendor.same", priority=200)))
    with pytest.raises(ConverterRegistryError, match="contain duplicates"):
        registry.select_chain(
            "test-format", preferred_ids=("vendor.same", "vendor.same")
        )


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: replace(value, spi_version="2.0"),
        lambda value: replace(value, converter_version="latest"),
        lambda value: replace(value, execution_mode="thread"),
        lambda value: replace(value, priority=True),
        lambda value: replace(value, network_required=1),
        lambda value: replace(value, deterministic="true"),
        lambda value: replace(value, formats=["test-format"]),
        lambda value: replace(value, formats=frozenset({"TEST"})),
        lambda value: replace(value, media_types=frozenset({"not-a-mime"})),
        lambda value: replace(value, capabilities=frozenset({"shell"})),
        lambda value: replace(value, license_expression="NOASSERTION"),
        lambda value: replace(value, source_url="http://example.test/source"),
    ],
)
def test_descriptor_runtime_literals_are_strict(mutator) -> None:
    descriptor = mutator(_descriptor("vendor.strict"))

    with pytest.raises(ConverterRegistryError):
        ConverterRegistry((_Plugin(descriptor),))


@pytest.mark.parametrize(
    "digest",
    [
        "bundled:dtxwiki",
        "0" * 64,
        "f" * 64,
        hashlib.sha256(b"").hexdigest(),
        _digest("placeholder"),
        "A" * 64,
    ],
)
def test_non_sha_and_placeholder_distribution_digests_are_rejected(
    digest: str,
) -> None:
    descriptor = replace(
        _descriptor("vendor.external", execution_mode="subprocess"),
        distribution_digest=digest,
    )

    with pytest.raises(ConverterRegistryError, match="non-placeholder SHA-256"):
        ConverterRegistry((_Plugin(descriptor),))


def test_provider_chain_snapshot_pins_protocol_schema_and_digest() -> None:
    registry = ConverterRegistry(
        (_Plugin(_descriptor("vendor.snapshot", execution_mode="subprocess")),)
    )

    assert registry.provider_chain_snapshot("test-format") == [
        {
            "converter_id": "vendor.snapshot",
            "converter_version": "1.2.3",
            "spi_version": CONVERTER_SPI_VERSION,
            "priority": 100,
            "execution_mode": "subprocess",
            "network_required": False,
            "deterministic": True,
            "license_expression": "Apache-2.0",
            "distribution_digest": _digest("vendor.snapshot"),
            "sbom_sha256": None,
            "signature_identity": None,
            "sandbox_profile": None,
            "max_input_bytes": None,
            "output_schema_version": CONVERSION_OUTPUT_SCHEMA_VERSION,
        }
    ]


def test_ready_input_limit_is_fail_closed_across_provider_chain() -> None:
    high_priority_unbounded = _Plugin(
        _descriptor("vendor.unbounded", priority=300)
    )
    medium = _Plugin(
        replace(
            _descriptor("vendor.medium", priority=200),
            max_input_bytes=20_000_000,
        )
    )
    strict = _Plugin(
        replace(
            _descriptor("vendor.strict-limit", priority=100),
            max_input_bytes=10_000_000,
        )
    )
    registry = ConverterRegistry((high_priority_unbounded, medium, strict))

    assert registry.ready_input_limit("test-format") == 10_000_000
    assert registry.ready_input_limit("unknown") is None


@pytest.mark.parametrize("value", [0, -1, True])
def test_descriptor_rejects_invalid_immutable_input_limit(value) -> None:
    descriptor = replace(
        _descriptor("vendor.invalid-limit"),
        max_input_bytes=value,
    )

    with pytest.raises(ConverterRegistryError, match="max_input_bytes"):
        ConverterRegistry((_Plugin(descriptor),))


def test_valid_external_manifest_requires_all_explicit_gates() -> None:
    registry = build_converter_registry(
        external_manifests_json=json.dumps([_manifest()]),
        os_sandboxed=True,
    )

    selected = registry.select_chain("pdf")
    assert [item.converter_id for item in selected] == ["vendor.pdf"]
    assert selected[0].distribution_digest == _digest("vendor-pdf-artifact")
    assert selected[0].sbom_sha256 == _digest("vendor-pdf-sbom")
    assert selected[0].signature_identity == "sigstore:vendor@example.test"
    assert selected[0].sandbox_profile == "oci-network-none-v1"
    health = {item.converter_id: item for item in registry.health()}
    assert health["vendor.pdf"].ready is False
    assert health["vendor.pdf"].reason_code == "converter_health_unavailable"
    assert "pdf" not in registry.ready_formats()


def test_network_manifest_requires_instance_and_manifest_approval() -> None:
    raw = json.dumps([_manifest(network_required=True, network_approved=True)])
    with pytest.raises(ConverterRegistryError, match="instance policy"):
        build_converter_registry(
            external_manifests_json=raw,
            os_sandboxed=True,
            network_allowed=False,
        )

    registry = build_converter_registry(
        external_manifests_json=raw,
        os_sandboxed=True,
        network_allowed=True,
    )
    assert registry.select_chain("pdf") == ()
    assert registry.select_chain("pdf", allow_network=True)[0].converter_id == (
        "vendor.pdf"
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"enabled": "false"}, "enabled must be a boolean"),
        ({"deterministic": 1}, "deterministic must be a boolean"),
        ({"priority": "50"}, "priority must be an integer"),
        ({"spi_version": "9.0"}, "SPI version"),
        ({"output_schema_version": "9.0"}, "output schema"),
        ({"execution_mode": "sidecar"}, "only subprocess"),
        ({"sandbox_approved": False}, "sandbox is not approved"),
        ({"sandbox_profile": "python-timeout"}, "sandbox profile is invalid"),
        ({"license_approved": False}, "license is not approved"),
        ({"license_expression": "NOASSERTION"}, "license expression"),
        ({"sbom_approved": False}, "SBOM is not approved"),
        ({"sbom_sha256": "0" * 64}, "sbom_sha256"),
        ({"signature_approved": False}, "signature is not approved"),
        ({"signature_identity": ""}, "signature_identity"),
        ({"distribution_digest": "bundled:dtxwiki"}, "distribution_digest"),
        ({"formats": ["pdf", "pdf"]}, "contains duplicates"),
        ({"command": ["relative-host"]}, "absolute token list"),
        ({"source_url": "https://user:secret@example.test"}, "source URL"),
        ({"source_url": "https://example.test/source?token=secret"}, "source URL"),
    ],
)
def test_external_manifest_rejects_ambiguous_or_unapproved_values(
    overrides: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ConverterRegistryError, match=message):
        build_converter_registry(
            external_manifests_json=json.dumps([_manifest(**overrides)]),
            os_sandboxed=True,
        )


def test_external_manifest_requires_os_sandbox_and_rejects_unknown_fields() -> None:
    raw = json.dumps([_manifest()])
    with pytest.raises(ConverterRegistryError, match="OS sandbox"):
        build_converter_registry(external_manifests_json=raw)

    manifest = _manifest()
    manifest["command_template"] = "secret {filename}"
    with pytest.raises(ConverterRegistryError, match="unknown fields") as caught:
        build_converter_registry(
            external_manifests_json=json.dumps([manifest]),
            os_sandboxed=True,
        )
    assert "secret" not in str(caught.value)


def test_disabled_manifest_needs_no_execution_approval() -> None:
    registry = build_converter_registry(
        external_manifests_json=json.dumps([{"enabled": False}])
    )

    assert "vendor.pdf" not in {item.converter_id for item in registry.health()}


def test_manifest_rejects_nonfinite_json_and_non_boolean_policy_arguments() -> None:
    with pytest.raises(ConverterRegistryError, match="JSON is invalid"):
        build_converter_registry(
            external_manifests_json="[NaN]",
            os_sandboxed=True,
        )
    with pytest.raises(ConverterRegistryError, match="flags must be booleans"):
        build_converter_registry(os_sandboxed=1)  # type: ignore[arg-type]

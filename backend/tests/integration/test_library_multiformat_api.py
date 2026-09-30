from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.app.container import DatabaseProvider
from backend.app.main import create_app
from backend.app.settings import Settings
from backend.models.job import Job
from backend.models.library import (
    LibraryArtifact,
    LibraryAttempt,
    LibraryItem,
    LibraryItemVersion,
    LibraryLogicalCharge,
    LibraryLogicalQuotaUsage,
    LibraryProjectionOutbox,
    LibraryRun,
)
from backend.models.media import (
    BlobIncarnationRow,
    MediaInstallRequest,
    MediaOutboxRow,
    MediaPhysicalCharge,
    MediaStagingLease,
    StoredBlobInspectionRow,
    StoredBlobRow,
)
from backend.models.tree import WorkspaceTreeNode
from backend.models.workspace import Workspace
from backend.services.conversion_orchestrator import LIBRARY_CONVERSION_JOB_TYPE
from backend.services.library_formats import _hdf5_lookup3_checksum
from backend.services.library_ingest import LibraryUploadAdmissionService
from backend.tests.conftest import authenticate_test_client_in_workspace
from backend.tests.fixtures.library_binary_samples import (
    VALID_HDF5_V0_WITH_DATASET,
    VALID_HDF5_V0_WITH_GROUP,
    VALID_HDF5_V0_WITH_GROUP_OFFSETS4_LENGTHS8,
    VALID_HDF5_V0_WITH_GROUP_OFFSETS8_LENGTHS4,
    VALID_HDF5_V0_WITH_ORDERED_GROUPS,
    VALID_HDF5_V0_WITH_UNWRITTEN_DATASET,
    VALID_HDF5_V0_WITH_UNWRITTEN_FILL_DATASET,
    VALID_HDF5_V3,
    VALID_HDF5_V3_WITH_DATASET,
    VALID_HDF5_V3_WITH_DATASET_AND_ROOT_ATTRIBUTE,
    VALID_HDF5_V3_WITH_FLOAT_DATASET,
    VALID_HDF5_V3_WITH_GROUP,
    VALID_HDF5_V3_WITH_TWO_DATASETS,
    VALID_HDF5_V3_WITH_UNWRITTEN_DATASET,
    VALID_HDF5_V3_WITH_UNWRITTEN_FILL_DATASET,
    VALID_HDF5_V3_WITH_V2_CONTINUATION,
    VALID_PARQUET_ONE_ROW,
    VALID_PARQUET_ZERO_ROWS_DICTIONARY_ONLY_ROW_GROUP,
    VALID_PARQUET_ZERO_ROWS_EMPTY_ROW_GROUP,
)
from backend.worker.runtime import OneShotWorker


@dataclass(frozen=True, slots=True)
class LibraryApiFixture:
    client: TestClient
    workspace_id: UUID
    session_factory: object
    blob_root: Path


def _unknown_hdf5_child_message_attack() -> bytes:
    payload = bytearray(VALID_HDF5_V3_WITH_GROUP)
    payload[186] = 0xFE
    payload[189] = 0x80
    payload[306:310] = _hdf5_lookup3_checksum(payload[179:306]).to_bytes(4, "little")
    return bytes(payload)


def _known_hdf5_child_payload_confusion() -> bytes:
    payload = bytearray(VALID_HDF5_V3_WITH_GROUP)
    payload[186] = 0x01
    payload[306:310] = _hdf5_lookup3_checksum(payload[179:306]).to_bytes(4, "little")
    return bytes(payload)


def _hdf5_ochk_attribute_type_confusion(message_type: int) -> bytes:
    payload = bytearray(VALID_HDF5_V3_WITH_V2_CONTINUATION)
    payload[314] = message_type
    payload[373:377] = _hdf5_lookup3_checksum(payload[310:373]).to_bytes(4, "little")
    return bytes(payload)


def _legacy_hdf5_tree_sibling_attack() -> bytes:
    payload = bytearray(VALID_HDF5_V0_WITH_GROUP)
    payload[144:152] = (96).to_bytes(8, "little")
    return bytes(payload)


def _legacy_hdf5_tree_key_attack(*, first_key: int = 0, final_key: int = 8) -> bytes:
    payload = bytearray(VALID_HDF5_V0_WITH_GROUP)
    assert payload[136:140] == b"TREE"
    payload[160:168] = first_key.to_bytes(8, "little")
    payload[176:184] = final_key.to_bytes(8, "little")
    return bytes(payload)


def _legacy_hdf5_snod_order_attack() -> bytes:
    payload = bytearray(VALID_HDF5_V0_WITH_ORDERED_GROUPS)
    assert payload[1512:1520] == (8).to_bytes(8, "little")
    assert payload[1552:1560] == (16).to_bytes(8, "little")
    payload[1512:1520] = (16).to_bytes(8, "little")
    payload[1552:1560] = (8).to_bytes(8, "little")
    return bytes(payload)


def _hdf5_v2_continuation_checksum_attack() -> bytes:
    payload = bytearray(VALID_HDF5_V3_WITH_V2_CONTINUATION)
    assert payload[310:314] == b"OCHK"
    payload[320] ^= 1
    return bytes(payload)


def _hdf5_raw_metadata_overlap_attack(
    address: int, *, payload: bytes = VALID_HDF5_V3_WITH_DATASET
) -> bytes:
    mutated = bytearray(payload)
    assert mutated[237:239] == b"\x03\x01"
    mutated[239:247] = address.to_bytes(8, "little")
    mutated[443:447] = _hdf5_lookup3_checksum(mutated[179:443]).to_bytes(4, "little")
    return bytes(mutated)


def _hdf5_raw_raw_overlap_attack() -> bytes:
    payload = bytearray(VALID_HDF5_V3_WITH_TWO_DATASETS)
    assert payload[507:515] == (2072).to_bytes(8, "little")
    payload[507:515] = (2048).to_bytes(8, "little")
    payload[711:715] = _hdf5_lookup3_checksum(payload[447:711]).to_bytes(4, "little")
    return bytes(payload)


def _hdf5_float_bit_overlap_attack() -> bytes:
    payload = bytearray(VALID_HDF5_V3_WITH_FLOAT_DATASET)
    assert payload[227] == 52
    payload[227] = 0
    payload[443:447] = _hdf5_lookup3_checksum(payload[179:443]).to_bytes(4, "little")
    return bytes(payload)


def _hdf5_global_heap_size_attack() -> bytes:
    payload = bytearray(VALID_HDF5_V3_WITH_V2_CONTINUATION)
    assert payload[2048:2052] == b"GCOL"
    payload[2056:2064] = (40).to_bytes(8, "little")
    return bytes(payload)


def _hdf5_unwritten_layout_size_attack() -> bytes:
    payload = bytearray(VALID_HDF5_V3_WITH_UNWRITTEN_DATASET)
    assert payload[239:247] == b"\xff" * 8
    assert payload[247:255] == (80).to_bytes(8, "little")
    payload[247:255] = (79).to_bytes(8, "little")
    payload[443:447] = _hdf5_lookup3_checksum(payload[179:443]).to_bytes(4, "little")
    return bytes(payload)


def _malformed_empty_parquet_attack() -> bytes:
    payload = bytearray(VALID_PARQUET_ZERO_ROWS_EMPTY_ROW_GROUP)
    assert payload[57:59] == b"\x16\x00"
    payload[58] = 0x02
    return bytes(payload)


def _malformed_empty_dictionary_parquet_attack() -> bytes:
    payload = bytearray(VALID_PARQUET_ZERO_ROWS_DICTIONARY_ONLY_ROW_GROUP)
    assert payload[11:13] == b"\x15\x00"
    payload[12] = 0x02
    return bytes(payload)


@pytest.fixture
def library_api(page_service, tmp_path: Path) -> Iterator[LibraryApiFixture]:
    blob_root = tmp_path / "library-blobs"
    settings = Settings(
        database_url="sqlite+pysqlite:///:memory:",
        session_secret="test-session-secret",
        library_blob_root=str(blob_root),
        library_upload_hard_cap_bytes=10_000_000,
    )
    with TestClient(create_app(settings=settings, page_service=page_service)) as client:
        workspace_id = UUID(
            authenticate_test_client_in_workspace(
                client,
                page_service.session_factory,
                email="library-api-owner@example.com",
                workspace_slug="default",
            )
        )
        yield LibraryApiFixture(
            client=client,
            workspace_id=workspace_id,
            session_factory=page_service.session_factory,
            blob_root=blob_root,
        )


def _library_url(fixture: LibraryApiFixture, suffix: str = "") -> str:
    return f"/api/v1/workspaces/{fixture.workspace_id}/library{suffix}"


def _library_items_url(fixture: LibraryApiFixture) -> str:
    """P4.2: POST library/items -- always creates a new item, distinct from
    the item-scoped GET/DELETE routes _library_url still models."""

    return f"/api/v1/workspaces/{fixture.workspace_id}/library/items"


def _replace_library_policy(
    fixture: LibraryApiFixture,
    **updates,
) -> None:
    with fixture.session_factory.begin() as session:
        workspace = session.get(Workspace, fixture.workspace_id)
        assert workspace is not None
        workspace.library_policy = {**workspace.library_policy, **updates}


def _valid_pdf(*, minimum_bytes: int = 0) -> bytes:
    prefix = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
    trailer_template = (
        b"xref\n0 1\n0000000000 65535 f \n"
        b"trailer\n<< /Root 1 0 R >>\nstartxref\n{offset}\n%%EOF"
    )
    # Padding is a legal PDF comment and is placed before xref so the validator
    # can still seek directly to the declared cross-reference offset.
    provisional = trailer_template.replace(b"{offset}", str(len(prefix)).encode())
    padding_size = max(0, minimum_bytes - len(prefix) - len(provisional) + 16)
    padding = b"%" + (b"x" * max(0, padding_size - 2)) + b"\n" if padding_size else b""
    xref_offset = len(prefix) + len(padding)
    trailer = trailer_template.replace(b"{offset}", str(xref_offset).encode())
    payload = prefix + padding + trailer
    assert len(payload) >= minimum_bytes
    return payload


def _signature_only_ole_document() -> bytes:
    return b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + (b"\x00" * 504)


def _incomplete_webp() -> bytes:
    payload = b"WEBP" + b"VP8 " + (b"\x00" * 4)
    return b"RIFF" + len(payload).to_bytes(4, "little") + payload


def _incomplete_hwpx() -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("Contents/content.hpf", "<hpf />")
    return output.getvalue()


def _mp4_box(box_type: bytes, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + box_type + payload


def _valid_mp4() -> bytes:
    ftyp = _mp4_box(b"ftyp", b"isom" + b"\0" * 4 + b"isom")
    handler = _mp4_box(b"hdlr", b"\0" * 8 + b"vide" + b"\0" * 4)
    sample = (16).to_bytes(4, "big") + b"avc1" + b"\0" * 8
    sample_table = _mp4_box(
        b"stsd", b"\0" * 4 + (1).to_bytes(4, "big") + sample
    )
    track = _mp4_box(
        b"trak",
        _mp4_box(
            b"mdia",
            handler + _mp4_box(b"minf", _mp4_box(b"stbl", sample_table)),
        ),
    )
    return ftyp + _mp4_box(b"moov", track) + _mp4_box(b"mdat", b"\0")


def _upload(
    fixture: LibraryApiFixture,
    *,
    filename: str,
    content: bytes,
    media_type: str,
    operation_id: UUID | None = None,
):
    headers = (
        {"X-DTX-Library-Upload-Operation-Id": str(operation_id)}
        if operation_id is not None
        else None
    )
    return fixture.client.post(
        _library_items_url(fixture),
        files={"file": (filename, content, media_type)},
        headers=headers,
    )


def _add_version(
    fixture: LibraryApiFixture,
    *,
    item_id: UUID,
    filename: str,
    content: bytes,
    media_type: str,
):
    """P4.2: POST library/items/{item_id}/versions -- the explicit
    add-a-version-to-this-exact-item command. Never a filename lookup."""

    return fixture.client.post(
        _library_url(fixture, f"/items/{item_id}/versions"),
        files={"file": (filename, content, media_type)},
    )


def _all_mapping_keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_all_mapping_keys(child) for child in value.values()))
    if isinstance(value, list):
        return set().union(*(_all_mapping_keys(child) for child in value))
    return set()


def test_capabilities_intersect_policy_with_installed_converters(
    library_api: LibraryApiFixture,
) -> None:
    _replace_library_policy(
        library_api,
        allow_store_without_converter=False,
        max_upload_bytes=9_000_000,
        format_overrides={"md": 4_000_000},
    )

    response = library_api.client.get(_library_url(library_api, "/capabilities"))

    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    formats = {entry["format_id"]: entry for entry in payload["formats"]}
    assert payload["max_files_per_batch"] == 20
    assert len(payload["policy_revision"]) == 64
    assert formats["markdown"]["upload"] is True
    assert formats["markdown"]["conversion"] == "available"
    assert formats["markdown"]["max_bytes"] == 4_000_000
    # PDF and PNG are policy-enabled, but no approved converter is installed.
    assert formats["pdf"]["upload"] is False
    assert formats["pdf"]["conversion"] == "denied"
    assert formats["png"]["upload"] is False
    assert formats["png"]["conversion"] == "denied"
    # v5 has no allowlist: every built-in format remains policy-enabled and
    # detector/converter readiness determines whether it can be uploaded.
    assert formats["json"]["upload"] is True
    assert formats["json"]["conversion"] == "available"


def test_builtin_alias_override_projects_and_governs_upload_admission(
    library_api: LibraryApiFixture,
) -> None:
    _replace_library_policy(
        library_api,
        builtin_format_overrides={"pdf": {"extensions": [".paper"]}},
        allow_store_without_converter=True,
    )

    capabilities = library_api.client.get(_library_url(library_api, "/capabilities"))

    assert capabilities.status_code == 200, capabilities.text
    payload = capabilities.json()["data"]
    pdf = next(item for item in payload["formats"] if item["format_id"] == "pdf")
    assert payload["policy_schema_version"] == 5
    assert pdf["extensions"] == [".paper"]
    assert pdf["default_extensions"] == [".pdf"]
    assert pdf["extensions_overridden"] is True

    renamed = _upload(
        library_api,
        filename="renamed.paper",
        content=_valid_pdf(),
        media_type="application/pdf",
    )
    assert renamed.status_code == 201, renamed.text
    assert renamed.json()["data"]["current_version"]["format_id"] == "pdf"

    removed_default = _upload(
        library_api,
        filename="removed.pdf",
        content=_valid_pdf(),
        media_type="application/pdf",
    )
    assert removed_default.status_code == 422, removed_default.text
    assert removed_default.json()["error_code"] == "unsupported_library_upload"


def test_structurally_validated_tsv_is_available_without_an_allowlist(
    library_api: LibraryApiFixture,
) -> None:
    _replace_library_policy(
        library_api,
        allow_store_without_converter=True,
    )

    capabilities = library_api.client.get(_library_url(library_api, "/capabilities"))

    assert capabilities.status_code == 200, capabilities.text
    formats = {
        entry["format_id"]: entry for entry in capabilities.json()["data"]["formats"]
    }
    assert formats["tsv"]["upload"] is True
    assert formats["tsv"]["conversion"] == "available"

    uploaded = _upload(
        library_api,
        filename="rows.tsv",
        content=b"name\tvalue\nalpha\t1\n",
        media_type="text/tab-separated-values",
    )

    assert uploaded.status_code == 201, uploaded.text
    version = uploaded.json()["data"]["current_version"]
    assert version["format_id"] == "tsv"
    assert version["processing_status"] == "queued"


def test_inert_source_capabilities_upload_and_conversion_use_fixed_fences(
    library_api: LibraryApiFixture,
) -> None:
    capabilities = library_api.client.get(_library_url(library_api, "/capabilities"))
    assert capabilities.status_code == 200, capabilities.text
    formats = {
        entry["format_id"]: entry for entry in capabilities.json()["data"]["formats"]
    }
    assert formats["html"]["extensions"] == [".html", ".htm", ".xhtml"]
    assert formats["javascript"]["extensions"] == [".js", ".mjs", ".cjs"]
    for format_id in ("html", "javascript"):
        assert formats[format_id]["upload"] is True
        assert formats[format_id]["conversion"] == "available"
        assert formats[format_id]["preview_kind"] == "markdown"

    source_cases = (
        (
            "inert-source.html",
            "text/html",
            "html",
            "html_source_marker",
            b"<script>window.html_source_marker = true</script>\n"
            b"```mermaid\ngraph TD; HTML-->RUN\n```\n",
        ),
        (
            "inert-source.mjs",
            "application/javascript",
            "javascript",
            "javascript_source_marker",
            b'const javascript_source_marker = "<script>not executed</script>";\n'
            b"```mermaid\ngraph TD; JS-->RUN\n```\n",
        ),
    )
    uploaded_items: list[tuple[str, str, str]] = []
    for filename, media_type, format_id, marker, content in source_cases:
        uploaded = _upload(
            library_api,
            filename=filename,
            content=content,
            media_type=media_type,
        )
        assert uploaded.status_code == 201, uploaded.text
        data = uploaded.json()["data"]
        assert data["trust_level"] == "unknown"
        assert data["current_version"]["format_id"] == format_id
        assert data["current_version"]["processing_status"] == "queued"
        original = library_api.client.get(data["current_version"]["original_url"])
        assert original.status_code == 200, original.text
        assert original.content == content
        assert original.headers["content-disposition"].startswith("attachment")
        assert original.headers["x-content-type-options"] == "nosniff"
        uploaded_items.append((data["id"], format_id, marker))

    settings = library_api.client.app.state.container.settings
    provider = DatabaseProvider(
        settings,
        session_factory=library_api.session_factory,
    )
    worker = OneShotWorker(settings=settings, database_provider=provider)
    for _item_id, _format_id, _marker in uploaded_items:
        dispatched = worker.dispatch_once_with_session_factory(
            library_api.session_factory,
            workspace_id=library_api.workspace_id,
            job_types={LIBRARY_CONVERSION_JOB_TYPE},
        )
        assert dispatched is not None and dispatched.status == "completed"

    expected_languages = {"html": "html", "javascript": "javascript"}
    outer_fence = "`" * 4
    for item_id, format_id, marker in uploaded_items:
        fetched = library_api.client.get(_library_url(library_api, f"/items/{item_id}"))
        assert fetched.status_code == 200, fetched.text
        detail = fetched.json()["data"]
        assert detail["current_version"]["processing_status"] == "ready"
        markdown = next(
            artifact
            for artifact in detail["artifacts"]
            if artifact["role"] == "markdown"
        )
        rendered_source = markdown["index_text"]
        assert rendered_source.startswith(
            f"{outer_fence}{expected_languages[format_id]}\n"
        )
        assert rendered_source.endswith(f"\n{outer_fence}\n")
        assert rendered_source.count(outer_fence) == 2
        assert "```mermaid" in rendered_source
        assert marker in rendered_source
        assert "<script>" in rendered_source


def test_active_source_revision_downgrades_stale_trust_but_preserves_restriction(
    library_api: LibraryApiFixture,
) -> None:
    first = _upload(
        library_api,
        filename="reused.html",
        content=b"<p>first</p>",
        media_type="text/html",
    )
    assert first.status_code == 201, first.text
    item_id = UUID(first.json()["data"]["id"])
    assert first.json()["data"]["trust_level"] == "unknown"

    with library_api.session_factory.begin() as session:
        item = session.get(LibraryItem, item_id)
        assert item is not None
        item.trust_level = "trusted"

    second = _add_version(
        library_api,
        item_id=item_id,
        filename="reused.html",
        content=b"<p>second</p>",
        media_type="text/html",
    )
    assert second.status_code == 201, second.text
    assert second.json()["data"]["id"] == str(item_id)
    assert second.json()["data"]["trust_level"] == "unknown"

    with library_api.session_factory.begin() as session:
        item = session.get(LibraryItem, item_id)
        assert item is not None
        item.trust_level = "restricted"

    third = _add_version(
        library_api,
        item_id=item_id,
        filename="reused.html",
        content=b"<p>third</p>",
        media_type="text/html",
    )
    assert third.status_code == 201, third.text
    assert third.json()["data"]["trust_level"] == "restricted"


def test_custom_format_is_store_only_and_existing_version_survives_catalog_change(
    library_api: LibraryApiFixture,
) -> None:
    _replace_library_policy(
        library_api,
        custom_formats=[
            {
                "format_id": "custom-drawing-v1",
                "display_name": "Drawing",
                "extensions": [".foo", ".bar"],
            }
        ],
        format_families=[],
        format_overrides={},
        allow_store_without_converter=True,
    )

    capability_response = library_api.client.get(
        _library_url(library_api, "/capabilities")
    )
    assert capability_response.status_code == 200, capability_response.text
    custom_capability = next(
        item
        for item in capability_response.json()["data"]["formats"]
        if item["format_id"] == "custom-drawing-v1"
    )
    assert custom_capability["extensions"] == [".foo", ".bar"]
    assert custom_capability["conversion_state"] == "stored_unconverted"
    assert custom_capability["preview_kind"] == "metadata"

    first_source = b"opaque passive drawing revision one"
    first = _upload(
        library_api,
        filename="drawing.foo",
        content=first_source,
        media_type="application/x-example-drawing",
    )
    assert first.status_code == 201, first.text
    first_version = first.json()["data"]["current_version"]
    first_item_id = first.json()["data"]["id"]
    assert first_version["format_id"] == "custom-drawing-v1"
    assert first_version["processing_status"] == "stored_unconverted"
    original_url = first_version["original_url"]

    _replace_library_policy(
        library_api,
        custom_formats=[],
        format_families=[],
        format_overrides={},
    )

    downloaded = library_api.client.get(original_url)
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == first_source
    existing_item = library_api.client.get(
        _library_url(library_api, f"/items/{first_item_id}")
    )
    assert existing_item.status_code == 200, existing_item.text
    assert (
        existing_item.json()["data"]["current_version"]["format_id"]
        == "custom-drawing-v1"
    )

    # A later catalog entry may reuse the alias, but it applies only to new
    # admissions. Persisted versions keep the exact format id captured at
    # upload time and are never reinterpreted through the current catalog.
    _replace_library_policy(
        library_api,
        custom_formats=[
            {
                "format_id": "custom-drawing-v2",
                "display_name": "Drawing v2",
                "extensions": [".foo"],
            }
        ],
    )
    second = _upload(
        library_api,
        filename="drawing-v2.foo",
        content=b"opaque passive drawing revision two",
        media_type="application/octet-stream",
    )
    assert second.status_code == 201, second.text
    assert (
        second.json()["data"]["current_version"]["format_id"]
        == "custom-drawing-v2"
    )
    with library_api.session_factory() as session:
        persisted_first = session.get(
            LibraryItemVersion,
            UUID(first_version["id"]),
        )
        assert persisted_first is not None
        assert persisted_first.format_id == "custom-drawing-v1"


def test_binary_capabilities_distinguish_validated_media_from_weak_containers(
    library_api: LibraryApiFixture,
) -> None:
    denied_formats = [
        "doc",
        "ppt",
        "xls",
        "gif",
        "hdf4",
        "hwp",
        "hwpx",
        "hwpml",
        "tiff",
        "webp",
    ]
    validated_store_only_formats = ["mp3", "ogg", "wav", "mp4", "webm"]
    _replace_library_policy(
        library_api,
        allow_store_without_converter=True,
    )

    capabilities = library_api.client.get(_library_url(library_api, "/capabilities"))

    assert capabilities.status_code == 200, capabilities.text
    formats = {
        entry["format_id"]: entry for entry in capabilities.json()["data"]["formats"]
    }
    for format_id in denied_formats:
        assert formats[format_id]["upload"] is False
        assert formats[format_id]["conversion"] == "denied"
        assert formats[format_id]["admission_state"] == "validator_unavailable"
    for format_id in validated_store_only_formats:
        assert formats[format_id]["upload"] is True
        assert formats[format_id]["conversion"] == "stored_unconverted"
        assert formats[format_id]["admission_state"] == "ready"
        assert formats[format_id]["storage_state"] == "available"
        assert formats[format_id]["preview_state"] == "ready"

    ole = _signature_only_ole_document()
    cases = (
        ("legacy.doc", ole, "application/msword"),
        ("legacy.ppt", ole, "application/vnd.ms-powerpoint"),
        ("legacy.xls", ole, "application/vnd.ms-excel"),
        ("korean.hwp", ole, "application/x-hwp"),
        ("pixel.gif", b"GIF89a\x01\x00\x01\x00\x00\x00\x00garbage;", "image/gif"),
        (
            "legacy.hdf4",
            b"\x0e\x03\x13\x01\x00\x00\x00\x00",
            "application/x-hdf",
        ),
        ("pixel.webp", _incomplete_webp(), "image/webp"),
        ("pixel.tiff", b"II*\x00\x08\x00\x00\x00\x00\x00", "image/tiff"),
        ("korean.hwpx", _incomplete_hwpx(), "application/vnd.hancom.hwpx+zip"),
        ("korean.hwpml", b"<?xml version='1.0'?><not-hwpml />", "application/xml"),
    )
    with library_api.session_factory() as session:
        item_count_before = session.scalar(
            select(func.count()).select_from(LibraryItem)
        )
        blob_count_before = session.scalar(
            select(func.count()).select_from(StoredBlobRow)
        )

    for filename, content, media_type in cases:
        uploaded = _upload(
            library_api,
            filename=filename,
            content=content,
            media_type=media_type,
        )

        assert uploaded.status_code == 422, (filename, uploaded.text)
        assert uploaded.json()["error_code"] == "library_format_disabled"

    with library_api.session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(LibraryItem))
            == item_count_before
        )
        assert (
            session.scalar(select(func.count()).select_from(StoredBlobRow))
            == blob_count_before
        )


def test_m4v_upload_accepts_common_declared_mime_alias(
    library_api: LibraryApiFixture,
) -> None:
    _replace_library_policy(
        library_api,
        allow_store_without_converter=True,
    )

    uploaded = _upload(
        library_api,
        filename="browser-export.m4v",
        content=_valid_mp4(),
        media_type="video/x-m4v",
    )

    assert uploaded.status_code == 201, uploaded.text
    version = uploaded.json()["data"]["current_version"]
    assert version["format_id"] == "mp4"
    assert version["declared_media_type"] == "video/x-m4v"
    assert version["detected_media_type"] == "video/mp4"
    assert version["processing_status"] == "stored_unconverted"
    assert version["detection"]["container"]["validator"] == "iso-bmff-video-v1"


@pytest.mark.parametrize(
    ("filename", "content", "media_type", "format_id"),
    [
        ("valid.h5", VALID_HDF5_V3, "application/x-hdf5", "hdf5"),
        (
            "earliest-ordered-groups.h5",
            VALID_HDF5_V0_WITH_ORDERED_GROUPS,
            "application/x-hdf5",
            "hdf5",
        ),
        (
            "earliest-group-offsets8-lengths4.h5",
            VALID_HDF5_V0_WITH_GROUP_OFFSETS8_LENGTHS4,
            "application/x-hdf5",
            "hdf5",
        ),
        (
            "earliest-group-offsets4-lengths8.h5",
            VALID_HDF5_V0_WITH_GROUP_OFFSETS4_LENGTHS8,
            "application/x-hdf5",
            "hdf5",
        ),
        (
            "earliest-dataset.h5",
            VALID_HDF5_V0_WITH_DATASET,
            "application/x-hdf5",
            "hdf5",
        ),
        (
            "earliest-unwritten-dataset.h5",
            VALID_HDF5_V0_WITH_UNWRITTEN_DATASET,
            "application/x-hdf5",
            "hdf5",
        ),
        (
            "earliest-unwritten-fill-dataset.h5",
            VALID_HDF5_V0_WITH_UNWRITTEN_FILL_DATASET,
            "application/x-hdf5",
            "hdf5",
        ),
        (
            "latest-dataset.h5",
            VALID_HDF5_V3_WITH_DATASET,
            "application/x-hdf5",
            "hdf5",
        ),
        (
            "latest-unwritten-dataset.h5",
            VALID_HDF5_V3_WITH_UNWRITTEN_DATASET,
            "application/x-hdf5",
            "hdf5",
        ),
        (
            "latest-unwritten-fill-dataset.h5",
            VALID_HDF5_V3_WITH_UNWRITTEN_FILL_DATASET,
            "application/x-hdf5",
            "hdf5",
        ),
        (
            "latest-dataset-root-attribute.h5",
            VALID_HDF5_V3_WITH_DATASET_AND_ROOT_ATTRIBUTE,
            "application/x-hdf5",
            "hdf5",
        ),
        (
            "latest-two-datasets.h5",
            VALID_HDF5_V3_WITH_TWO_DATASETS,
            "application/x-hdf5",
            "hdf5",
        ),
        (
            "latest-float-dataset.h5",
            VALID_HDF5_V3_WITH_FLOAT_DATASET,
            "application/x-hdf5",
            "hdf5",
        ),
        (
            "v2-continuation.h5",
            VALID_HDF5_V3_WITH_V2_CONTINUATION,
            "application/x-hdf5",
            "hdf5",
        ),
        (
            "valid.parquet",
            VALID_PARQUET_ONE_ROW,
            "application/vnd.apache.parquet",
            "parquet",
        ),
        (
            "empty.parquet",
            VALID_PARQUET_ZERO_ROWS_EMPTY_ROW_GROUP,
            "application/vnd.apache.parquet",
            "parquet",
        ),
        (
            "dictionary-empty.parquet",
            VALID_PARQUET_ZERO_ROWS_DICTIONARY_ONLY_ROW_GROUP,
            "application/vnd.apache.parquet",
            "parquet",
        ),
    ],
)
def test_structurally_valid_binary_data_uploads_are_preserved_without_converter(
    library_api: LibraryApiFixture,
    filename,
    content,
    media_type,
    format_id,
) -> None:
    uploaded = _upload(
        library_api,
        filename=filename,
        content=content,
        media_type=media_type,
    )

    assert uploaded.status_code == 201, uploaded.text
    version = uploaded.json()["data"]["current_version"]
    assert version["format_id"] == format_id
    assert version["processing_status"] == "stored_unconverted"


@pytest.mark.parametrize(
    ("filename", "content", "media_type"),
    [
        (
            "nine-byte.h5",
            b"\x89HDF\r\n\x1a\n\x03",
            "application/x-hdf5",
        ),
        (
            "thirteen-byte.parquet",
            b"PAR1\x00\x01\x00\x00\x00PAR1",
            "application/vnd.apache.parquet",
        ),
        (
            "zero-root.h5",
            VALID_HDF5_V3[:48] + (b"\x00" * (len(VALID_HDF5_V3) - 48)),
            "application/x-hdf5",
        ),
        (
            "zero-pages.parquet",
            VALID_PARQUET_ONE_ROW[:4] + (b"\x00" * 50) + VALID_PARQUET_ONE_ROW[54:],
            "application/vnd.apache.parquet",
        ),
        (
            "zero-linked-child.h5",
            VALID_HDF5_V3_WITH_GROUP[:179]
            + (b"\x00" * (len(VALID_HDF5_V3_WITH_GROUP) - 179)),
            "application/x-hdf5",
        ),
        (
            "unknown-page-encoding.parquet",
            VALID_PARQUET_ONE_ROW[:14] + b"\x7e" + VALID_PARQUET_ONE_ROW[15:],
            "application/vnd.apache.parquet",
        ),
        (
            "unknown-child-message.h5",
            _unknown_hdf5_child_message_attack(),
            "application/x-hdf5",
        ),
        (
            "known-child-payload-confusion.h5",
            _known_hdf5_child_payload_confusion(),
            "application/x-hdf5",
        ),
        *(
            (
                f"ochk-payload-confusion-{message_type}.h5",
                _hdf5_ochk_attribute_type_confusion(message_type),
                "application/x-hdf5",
            )
            for message_type in (0x01, 0x03, 0x08)
        ),
        (
            "forged-tree-sibling.h5",
            _legacy_hdf5_tree_sibling_attack(),
            "application/x-hdf5",
        ),
        (
            "forged-tree-first-key.h5",
            _legacy_hdf5_tree_key_attack(first_key=7),
            "application/x-hdf5",
        ),
        (
            "forged-tree-final-key.h5",
            _legacy_hdf5_tree_key_attack(final_key=9),
            "application/x-hdf5",
        ),
        (
            "forged-snod-order.h5",
            _legacy_hdf5_snod_order_attack(),
            "application/x-hdf5",
        ),
        (
            "bad-v2-continuation-checksum.h5",
            _hdf5_v2_continuation_checksum_attack(),
            "application/x-hdf5",
        ),
        (
            "raw-over-root-ohdr.h5",
            _hdf5_raw_metadata_overlap_attack(48),
            "application/x-hdf5",
        ),
        (
            "raw-over-continuation.h5",
            _hdf5_raw_metadata_overlap_attack(
                447, payload=VALID_HDF5_V3_WITH_DATASET_AND_ROOT_ATTRIBUTE
            ),
            "application/x-hdf5",
        ),
        (
            "raw-over-raw.h5",
            _hdf5_raw_raw_overlap_attack(),
            "application/x-hdf5",
        ),
        (
            "float-bit-overlap.h5",
            _hdf5_float_bit_overlap_attack(),
            "application/x-hdf5",
        ),
        (
            "undersized-global-heap.h5",
            _hdf5_global_heap_size_attack(),
            "application/x-hdf5",
        ),
        (
            "unwritten-layout-size-mismatch.h5",
            _hdf5_unwritten_layout_size_attack(),
            "application/x-hdf5",
        ),
        (
            "malformed-empty.parquet",
            _malformed_empty_parquet_attack(),
            "application/vnd.apache.parquet",
        ),
        (
            "malformed-empty-dictionary.parquet",
            _malformed_empty_dictionary_parquet_attack(),
            "application/vnd.apache.parquet",
        ),
    ],
)
def test_invalid_binary_container_attacks_create_no_library_item_or_blob(
    library_api: LibraryApiFixture,
    filename,
    content,
    media_type,
) -> None:
    with library_api.session_factory() as session:
        item_count_before = session.scalar(
            select(func.count()).select_from(LibraryItem)
        )
        blob_count_before = session.scalar(
            select(func.count()).select_from(StoredBlobRow)
        )

    uploaded = _upload(
        library_api,
        filename=filename,
        content=content,
        media_type=media_type,
    )

    assert uploaded.status_code == 422, uploaded.text
    assert uploaded.json()["error_code"] == "library_container_invalid"
    with library_api.session_factory() as session:
        assert (
            session.scalar(select(func.count()).select_from(LibraryItem))
            == item_count_before
        )
        assert (
            session.scalar(select(func.count()).select_from(StoredBlobRow))
            == blob_count_before
        )


def test_upload_rejects_extension_and_declared_media_type_conflicts(
    library_api: LibraryApiFixture,
) -> None:
    pdf = _valid_pdf()

    spoofed_extension = _upload(
        library_api,
        filename="pretends-to-be-markdown.md",
        content=pdf,
        media_type="text/markdown",
    )

    assert spoofed_extension.status_code == 422, spoofed_extension.text
    assert spoofed_extension.json()["error_code"] == "library_format_mismatch"

    declared_as_text = _upload(
        library_api,
        filename="actual-report.pdf",
        content=pdf,
        media_type="text/plain",
    )

    assert declared_as_text.status_code == 422, declared_as_text.text
    assert declared_as_text.json()["error_code"] == "library_format_mismatch"


def test_invalid_new_item_parent_is_rejected_before_blob_publication(
    library_api: LibraryApiFixture,
) -> None:
    with library_api.session_factory() as session:
        wrong_section_parent_id = session.scalar(
            select(WorkspaceTreeNode.id).where(
                WorkspaceTreeNode.workspace_id == library_api.workspace_id,
                WorkspaceTreeNode.section == "context",
                WorkspaceTreeNode.parent_id.is_(None),
            )
        )
    assert wrong_section_parent_id is not None

    cases = (
        (wrong_section_parent_id, 403, "rejected_by_policy"),
        (uuid4(), 404, "not_found"),
    )
    for parent_node_id, status_code, error_code in cases:
        response = library_api.client.post(
            _library_items_url(library_api),
            data={"parent_node_id": str(parent_node_id)},
            files={"file": ("orphan.md", b"# Must not publish", "text/markdown")},
        )
        assert response.status_code == status_code, response.text
        assert response.json()["error_code"] == error_code

    with library_api.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(StoredBlobRow)) == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(LibraryItem)
                .where(LibraryItem.title == "orphan.md")
            )
            == 0
        )
    assert not library_api.blob_root.exists() or not any(
        path.is_file() for path in library_api.blob_root.rglob("*")
    )


@pytest.mark.parametrize(
    ("filename", "media_type", "content", "policy_updates"),
    [
        (
            "workspace-limit.md",
            "text/markdown",
            b"#" * 1_001,
            {"max_upload_bytes": 1_000},
        ),
        (
            "format-limit.pdf",
            "application/pdf",
            _valid_pdf(minimum_bytes=1_001),
            {"format_overrides": {"pdf": 1_000}},
        ),
        (
            "family-limit.pdf",
            "application/pdf",
            _valid_pdf(minimum_bytes=1_001),
            {
                "format_families": [
                    {
                        "family_id": "documents",
                        "name": "Documents",
                        "extensions": [".pdf"],
                        "max_upload_bytes": 1_000,
                    }
                ]
            },
        ),
    ],
    ids=("workspace", "detected-format", "detected-family"),
)
def test_upload_enforces_workspace_exact_format_and_family_caps(
    library_api: LibraryApiFixture,
    filename: str,
    media_type: str,
    content: bytes,
    policy_updates: dict,
) -> None:
    _replace_library_policy(
        library_api,
        **{"max_upload_bytes": 10_000, **policy_updates},
    )

    response = _upload(
        library_api,
        filename=filename,
        content=content,
        media_type=media_type,
    )

    assert response.status_code == 413, response.text
    body = response.json()
    assert body["error_code"] == "library_upload_too_large"
    assert body["limit_bytes"] == 1_000
    assert body["settings_link"] == "/settings?section=library"


def test_original_download_supports_full_range_etag_and_invalid_range(
    library_api: LibraryApiFixture,
) -> None:
    source = b"0123456789abcdefghijklmnopqrstuvwxyz"
    uploaded = _upload(
        library_api,
        filename="range-source.txt",
        content=source,
        media_type="application/octet-stream",
    )
    assert uploaded.status_code == 201, uploaded.text
    original_url = uploaded.json()["data"]["current_version"]["original_url"]

    full = library_api.client.get(original_url)
    assert full.status_code == 200, full.text
    assert full.content == source
    assert full.headers["content-type"].startswith("text/plain")
    assert full.headers["content-length"] == str(len(source))
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["cache-control"] == "private, no-store"
    assert full.headers["x-content-type-options"] == "nosniff"
    assert "range-source.txt" in full.headers["content-disposition"]
    etag = full.headers["etag"]

    partial = library_api.client.get(original_url, headers={"Range": "bytes=2-5"})
    assert partial.status_code == 206, partial.text
    assert partial.content == source[2:6]
    assert partial.headers["content-range"] == f"bytes 2-5/{len(source)}"
    assert partial.headers["content-length"] == "4"
    partial_head = library_api.client.head(
        original_url,
        headers={"Range": "bytes=-3"},
    )
    assert partial_head.status_code == 206
    assert partial_head.content == b""
    assert partial_head.headers["content-range"] == (
        f"bytes {len(source) - 3}-{len(source) - 1}/{len(source)}"
    )
    matching_if_range = library_api.client.get(
        original_url,
        headers={"Range": "bytes=0-1", "If-Range": etag},
    )
    assert matching_if_range.status_code == 206
    assert matching_if_range.content == source[:2]
    stale_if_range = library_api.client.get(
        original_url,
        headers={"Range": "bytes=0-1", "If-Range": '"stale"'},
    )
    assert stale_if_range.status_code == 200
    assert stale_if_range.content == source

    unchanged = library_api.client.get(
        original_url,
        headers={"If-None-Match": etag},
    )
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    assert unchanged.headers["etag"] == etag

    invalid = library_api.client.get(
        original_url,
        headers={"Range": "bytes=0-1,3-4"},
    )
    assert invalid.status_code == 416, invalid.text
    assert invalid.json()["error_code"] == "library_range_not_satisfiable"
    assert invalid.headers["content-range"] == f"bytes */{len(source)}"
    assert invalid.headers["accept-ranges"] == "bytes"


def test_original_download_requires_auth_and_serves_each_versions_own_content(
    library_api: LibraryApiFixture,
) -> None:
    first = _upload(
        library_api,
        filename="first.txt",
        content=b"first source",
        media_type="text/plain",
    )
    second = _upload(
        library_api,
        filename="second.txt",
        content=b"second source",
        media_type="text/plain",
    )
    assert first.status_code == second.status_code == 201
    first_data = first.json()["data"]
    second_data = second.json()["data"]
    # P9.2: versions/{version_id}/content is flat -- no item in the URL --
    # so the second item's own version is simply its own legitimate target.
    second_version_id = second_data["current_version"]["id"]
    second_own = library_api.client.get(
        _library_url(library_api, f"/versions/{second_version_id}/content")
    )
    assert second_own.status_code == 200, second_own.text
    assert second_own.content == b"second source"

    viewer = TestClient(library_api.client.app)
    authenticate_test_client_in_workspace(
        viewer,
        library_api.session_factory,
        email="library-api-viewer@example.com",
        workspace_slug="default",
        role="viewer",
    )
    original_url = first_data["current_version"]["original_url"]
    assert viewer.get(original_url).content == b"first source"
    viewer.close()

    anonymous = TestClient(library_api.client.app)
    denied = anonymous.get(original_url)
    anonymous.close()
    assert denied.status_code == 401


@pytest.mark.parametrize(
    ("filename", "content", "media_type", "marker"),
    (
        (
            "worker-runtime.json",
            b'{"marker":"durable-json"}',
            "application/json",
            "durable-json",
        ),
        (
            "worker-runtime.csv",
            b"kind,value\nmarker,durable-csv\n",
            "text/csv",
            "durable-csv",
        ),
    ),
)
def test_two_phase_worker_builds_durable_installer_from_claimed_factory(
    library_api: LibraryApiFixture,
    filename: str,
    content: bytes,
    media_type: str,
    marker: str,
) -> None:
    uploaded = _upload(
        library_api,
        filename=filename,
        content=content,
        media_type=media_type,
    )
    assert uploaded.status_code == 201, uploaded.text
    item_id = uploaded.json()["data"]["id"]
    settings = library_api.client.app.state.container.settings
    provider = DatabaseProvider(
        settings,
        session_factory=library_api.session_factory,
    )
    worker = OneShotWorker(settings=settings, database_provider=provider)

    dispatched = worker.dispatch_once_with_session_factory(
        library_api.session_factory,
        workspace_id=library_api.workspace_id,
        job_types={LIBRARY_CONVERSION_JOB_TYPE},
    )

    assert dispatched is not None and dispatched.status == "completed"
    fetched = library_api.client.get(_library_url(library_api, f"/items/{item_id}"))
    assert fetched.status_code == 200, fetched.text
    detail = fetched.json()["data"]
    assert detail["current_version"]["processing_status"] == "ready"
    assert any(
        marker in (artifact["index_text"] or "") for artifact in detail["artifacts"]
    )


def test_selected_artifact_content_supports_full_range_head_and_etag(
    library_api: LibraryApiFixture,
) -> None:
    uploaded = _upload(
        library_api,
        filename="artifact-content.md",
        content=b"# Artifact content\n\nConverted body\n",
        media_type="text/markdown",
    )
    assert uploaded.status_code == 201, uploaded.text
    item_id = uploaded.json()["data"]["id"]

    conversion = library_api.client.app.state.container.services.library_conversion
    worker = OneShotWorker(job_queue=conversion.job_queue)
    worker.library_conversion_service = conversion
    with library_api.session_factory.begin() as session:
        dispatched = worker.dispatch_once(
            session,
            workspace_id=library_api.workspace_id,
            job_types={LIBRARY_CONVERSION_JOB_TYPE},
        )
        assert dispatched is not None
        assert dispatched.status == "completed"

    fetched = library_api.client.get(_library_url(library_api, f"/items/{item_id}"))
    assert fetched.status_code == 200, fetched.text
    artifacts = fetched.json()["data"]["artifacts"]
    markdown = next(
        artifact for artifact in artifacts if artifact["role"] == "markdown"
    )
    content_url = markdown["content_url"]
    assert content_url.startswith(
        f"/api/v1/workspaces/{library_api.workspace_id}/library/artifacts/"
    )

    full = library_api.client.get(content_url)
    assert full.status_code == 200, full.text
    assert b"Artifact content" in full.content
    assert full.headers["content-type"].startswith("text/markdown")
    assert full.headers["content-disposition"].startswith("inline")
    assert full.headers["cache-control"] == "private, no-store"
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["x-content-type-options"] == "nosniff"
    etag = full.headers["etag"]

    partial = library_api.client.get(
        content_url,
        headers={"Range": "bytes=2-7", "If-Range": etag},
    )
    assert partial.status_code == 206, partial.text
    assert partial.content == full.content[2:8]
    assert partial.headers["content-range"] == f"bytes 2-7/{len(full.content)}"

    head = library_api.client.head(content_url, headers={"Range": "bytes=-4"})
    assert head.status_code == 206
    assert head.content == b""
    assert head.headers["content-length"] == "4"

    unchanged = library_api.client.get(
        content_url,
        headers={"If-None-Match": etag},
    )
    assert unchanged.status_code == 304
    assert unchanged.content == b""


def test_reuploaded_bytes_are_a_new_item_but_dedupe_at_the_blob_layer(
    library_api: LibraryApiFixture,
) -> None:
    """P4.2: no filename-derived upsert -- a second `POST library/items` call
    with the same bytes (even the same filename) is a genuinely new, separate
    item/version, never silently merged into the first. The immutable blob
    underneath is still content-addressed and shared: only the domain
    registration is un-deduplicated, not the storage."""

    content = b"# Same source\n\nSame bytes\n"
    first = _upload(
        library_api,
        filename="same.md",
        content=content,
        media_type="text/markdown",
    )
    second = _upload(
        library_api,
        filename="same.md",
        content=content,
        media_type="application/octet-stream",
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    first_data = first.json()["data"]
    second_data = second.json()["data"]
    assert second_data["id"] != first_data["id"]
    assert (
        second_data["current_version"]["id"] != first_data["current_version"]["id"]
    )
    assert (
        second_data["current_version"]["content_hash"]
        == first_data["current_version"]["content_hash"]
    )
    assert first_data["current_version"]["declared_media_type"] == "text/markdown"

    with library_api.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(LibraryItem)) == 2
        assert session.scalar(select(func.count()).select_from(LibraryItemVersion)) == 2
        # Content-addressed: two registrations of the same bytes still share
        # one physical blob.
        assert session.scalar(select(func.count()).select_from(StoredBlobRow)) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(LibraryRun)
                .where(LibraryRun.stage == "conversion")
            )
            == 2
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.job_type == "library_conversion")
            )
            == 2
        )

    fetched = library_api.client.get(
        _library_url(library_api, f"/items/{first_data['id']}")
    )
    assert fetched.status_code == 200, fetched.text
    for response in (first, second, fetched):
        payload = response.json()
        assert _all_mapping_keys(payload).isdisjoint(
            {
                "storage_key",
                "storage_ref",
                "raw_storage_ref",
                "source_blob_workspace_id",
                "source_blob_id",
                "source_blob_epoch",
            }
        )
        serialized = json.dumps(payload, sort_keys=True)
        assert str(library_api.blob_root) not in serialized
        assert "library-blobs" not in serialized


def test_source_logical_quota_rejects_new_digest_without_partial_install(
    library_api: LibraryApiFixture,
) -> None:
    content = b"# Reused quota source\n"
    operation_id = uuid4()
    first = _upload(
        library_api,
        filename="quota-first.md",
        content=content,
        media_type="text/markdown",
        operation_id=operation_id,
    )
    assert first.status_code == 201, first.text

    with library_api.session_factory.begin() as session:
        usage = session.get(
            LibraryLogicalQuotaUsage,
            {"workspace_id": library_api.workspace_id, "quota_kind": "source"},
        )
        assert usage is not None and usage.charged_bytes > 0
        usage.limit_bytes = usage.charged_bytes
        charged_before = usage.charged_bytes

    # P4.2: an idempotent replay of the SAME operation id is the only way a
    # second call with identical bytes stays free (no filename/content
    # matching any more) -- this confirms the replay itself charges nothing
    # more, before the real assertion below (a genuinely new digest is
    # rejected once quota is exhausted).
    replay = _upload(
        library_api,
        filename="quota-first.md",
        content=content,
        media_type="text/markdown",
        operation_id=operation_id,
    )
    assert replay.status_code == 200, replay.text

    with library_api.session_factory() as session:
        counts_before = {
            model.__tablename__: session.scalar(select(func.count()).select_from(model))
            for model in (
                StoredBlobRow,
                BlobIncarnationRow,
                StoredBlobInspectionRow,
                MediaPhysicalCharge,
                MediaStagingLease,
                MediaInstallRequest,
                MediaOutboxRow,
                LibraryLogicalCharge,
                LibraryItem,
                LibraryItemVersion,
            )
        }
    files_before = {
        path.relative_to(library_api.blob_root).as_posix()
        for path in library_api.blob_root.rglob("*")
        if path.is_file()
    }

    rejected = _upload(
        library_api,
        filename="quota-second.md",
        content=b"# A distinct quota source\n",
        media_type="text/markdown",
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["error_code"] == "media_quota_exceeded"

    with library_api.session_factory() as session:
        usage = session.get(
            LibraryLogicalQuotaUsage,
            {"workspace_id": library_api.workspace_id, "quota_kind": "source"},
        )
        assert usage is not None and usage.charged_bytes == charged_before
        counts_after = {
            model.__tablename__: session.scalar(select(func.count()).select_from(model))
            for model in (
                StoredBlobRow,
                BlobIncarnationRow,
                StoredBlobInspectionRow,
                MediaPhysicalCharge,
                MediaStagingLease,
                MediaInstallRequest,
                MediaOutboxRow,
                LibraryLogicalCharge,
                LibraryItem,
                LibraryItemVersion,
            )
        }
    assert counts_after == counts_before
    assert {
        path.relative_to(library_api.blob_root).as_posix()
        for path in library_api.blob_root.rglob("*")
        if path.is_file()
    } == files_before


def test_upload_install_and_registration_share_one_outer_session(
    library_api: LibraryApiFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    services = library_api.client.app.state.container.get_services()
    registration_service = services.library_binary_registration
    original_admission_factory = services.library_upload_admission_for_session
    original_register = registration_service.register
    observed_sessions = {}

    def admission_for_session(session, workspace_id):
        observed_sessions["admission"] = session
        assert session.in_transaction()
        return original_admission_factory(session, workspace_id)

    def register(**kwargs):
        session = kwargs["session"]
        observed_sessions["registration"] = session
        assert session.in_transaction()
        return original_register(**kwargs)

    monkeypatch.setattr(
        services,
        "library_upload_admission_for_session",
        admission_for_session,
    )
    monkeypatch.setattr(registration_service, "register", register)

    uploaded = _upload(
        library_api,
        filename="one-outer-session.md",
        content=b"# One outer session\n",
        media_type="text/markdown",
    )

    assert uploaded.status_code == 201, uploaded.text
    assert observed_sessions["admission"] is observed_sessions["registration"]


def test_policy_change_during_upload_rejects_without_install_residue(
    library_api: LibraryApiFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    residue_models = (
        StoredBlobRow,
        BlobIncarnationRow,
        StoredBlobInspectionRow,
        MediaPhysicalCharge,
        MediaStagingLease,
        MediaInstallRequest,
        MediaOutboxRow,
        LibraryLogicalQuotaUsage,
        LibraryLogicalCharge,
        LibraryItem,
        LibraryItemVersion,
        LibraryRun,
        LibraryAttempt,
        LibraryArtifact,
        LibraryProjectionOutbox,
        Job,
        WorkspaceTreeNode,
    )
    with library_api.session_factory() as session:
        counts_before = {
            model.__tablename__: session.scalar(select(func.count()).select_from(model))
            for model in residue_models
        }
    files_before = {
        path.relative_to(library_api.blob_root).as_posix()
        for path in library_api.blob_root.rglob("*")
        if path.is_file()
    }

    original_admit = LibraryUploadAdmissionService.admit
    policy_changed = False

    async def admit_after_policy_change(self, *args, **kwargs):
        nonlocal policy_changed
        assert not policy_changed
        with library_api.session_factory.begin() as session:
            workspace = session.get(Workspace, library_api.workspace_id)
            assert workspace is not None
            current_limit = workspace.library_policy["max_upload_bytes"]
            assert isinstance(current_limit, int)
            workspace.library_policy = {
                **workspace.library_policy,
                "max_upload_bytes": current_limit - 1_000,
            }
        policy_changed = True
        return await original_admit(self, *args, **kwargs)

    monkeypatch.setattr(
        LibraryUploadAdmissionService,
        "admit",
        admit_after_policy_change,
    )

    rejected = _upload(
        library_api,
        filename="policy-race.md",
        content=b"# Policy race\n",
        media_type="text/markdown",
    )

    assert policy_changed
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["error_code"] == "library_upload_policy_changed"
    with library_api.session_factory() as session:
        counts_after = {
            model.__tablename__: session.scalar(select(func.count()).select_from(model))
            for model in residue_models
        }
    assert counts_after == counts_before
    assert {
        path.relative_to(library_api.blob_root).as_posix()
        for path in library_api.blob_root.rglob("*")
        if path.is_file()
    } == files_before


def test_delete_ready_item_removes_conversion_lineage_outbox_and_jobs(
    library_api: LibraryApiFixture,
) -> None:
    uploaded = _upload(
        library_api,
        filename="delete-ready.md",
        content=b"# Delete after conversion\n\nBody\n",
        media_type="text/markdown",
    )
    assert uploaded.status_code == 201, uploaded.text
    item_id = UUID(uploaded.json()["data"]["id"])

    container = library_api.client.app.state.container
    conversion = container.services.library_conversion
    worker = OneShotWorker(job_queue=conversion.job_queue)
    worker.library_conversion_service = conversion
    with library_api.session_factory.begin() as session:
        dispatched = worker.dispatch_once(
            session,
            workspace_id=library_api.workspace_id,
            job_types={LIBRARY_CONVERSION_JOB_TYPE},
        )
        assert dispatched is not None
        assert dispatched.status == "completed"

    with library_api.session_factory() as session:
        item = session.get(LibraryItem, item_id)
        assert item is not None
        version = session.scalar(
            select(LibraryItemVersion).where(
                LibraryItemVersion.library_item_id == item_id
            )
        )
        assert version is not None
        run = session.scalar(
            select(LibraryRun).where(
                LibraryRun.library_item_version_id == version.id,
                LibraryRun.stage == "conversion",
            )
        )
        attempt = session.scalar(
            select(LibraryAttempt).where(
                LibraryAttempt.library_item_version_id == version.id,
                LibraryAttempt.stage == "conversion",
            )
        )
        assert run is not None
        assert attempt is not None
        artifact_ids = set(
            session.scalars(
                select(LibraryArtifact.id).where(
                    LibraryArtifact.library_item_version_id == version.id
                )
            ).all()
        )
        outbox_ids = set(
            session.scalars(
                select(LibraryProjectionOutbox.id).where(
                    LibraryProjectionOutbox.library_item_version_id == version.id
                )
            ).all()
        )
        lineage_jobs = [
            job
            for job in session.scalars(
                select(Job).where(Job.workspace_id == library_api.workspace_id)
            ).all()
            if (
                job.target_ref.get("library_item_version_id") == str(version.id)
                or (
                    job.target_ref.get("kind") == "library_item"
                    and job.target_ref.get("id") == str(item_id)
                )
            )
        ]
        version_id = version.id
        run_id = run.id
        attempt_id = attempt.id
        job_ids = {job.id for job in lineage_jobs}
        source_usage = session.get(
            LibraryLogicalQuotaUsage,
            {"workspace_id": library_api.workspace_id, "quota_kind": "source"},
        )
        derived_usage = session.get(
            LibraryLogicalQuotaUsage,
            {"workspace_id": library_api.workspace_id, "quota_kind": "derived"},
        )
        assert item.current_ready_version_id == version_id
        assert version.selected_conversion_attempt_id == attempt_id
        assert artifact_ids
        assert outbox_ids
        assert len(job_ids) == 2
        assert source_usage is not None and source_usage.charged_bytes > 0
        assert derived_usage is not None and derived_usage.charged_bytes > 0

    deleted = library_api.client.delete(_library_url(library_api, f"/items/{item_id}"))
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["data"]["deleted"] is True

    with library_api.session_factory() as session:
        assert session.get(LibraryItem, item_id) is None
        assert session.get(LibraryItemVersion, version_id) is None
        assert session.get(LibraryRun, run_id) is None
        assert session.get(LibraryAttempt, attempt_id) is None
        assert all(
            session.get(LibraryArtifact, value) is None for value in artifact_ids
        )
        assert all(
            session.get(LibraryProjectionOutbox, value) is None for value in outbox_ids
        )
        assert all(session.get(Job, value) is None for value in job_ids)
        source_usage = session.get(
            LibraryLogicalQuotaUsage,
            {"workspace_id": library_api.workspace_id, "quota_kind": "source"},
        )
        derived_usage = session.get(
            LibraryLogicalQuotaUsage,
            {"workspace_id": library_api.workspace_id, "quota_kind": "derived"},
        )
        charges = session.scalars(
            select(LibraryLogicalCharge).where(
                LibraryLogicalCharge.workspace_id == library_api.workspace_id
            )
        ).all()
        assert source_usage is not None and source_usage.charged_bytes == 0
        assert derived_usage is not None and derived_usage.charged_bytes == 0
        assert charges and {charge.state for charge in charges} == {"released"}

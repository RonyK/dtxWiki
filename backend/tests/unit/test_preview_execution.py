from __future__ import annotations

import hashlib
import io
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image

from backend.preview_providers.pillow_image import PillowImagePreviewProvider
from backend.services.preview_execution import (
    BundledSubprocessPreviewExecutionBoundary,
)
from backend.spi.preview import (
    PreviewLimits,
    PreviewProviderError,
    PreviewRequest,
    PreviewVariantReceipt,
)


@dataclass(frozen=True, slots=True)
class _MemoryBlob:
    content: bytes

    @property
    def byte_size(self) -> int:
        return len(self.content)

    def open_binary(self):
        return io.BytesIO(self.content)

    def copy_to(self, destination: Path) -> int:
        destination.write_bytes(self.content)
        return len(self.content)


class _CapturingSink:
    def __init__(self) -> None:
        self.content = b""

    def write_variant(self, *, source, **values):
        self.content = source.read()
        return PreviewVariantReceipt(
            receipt_id=uuid4(),
            kind=values["kind"],
            media_type=values["media_type"],
            renderer_contract=values["renderer_contract"],
            delivery=values["delivery"],
            byte_size=values["expected_byte_size"],
            sha256=values["expected_sha256"],
            rank=values["rank"],
            ordinal=values["ordinal"],
            filename=values["filename"],
            width=values["width"],
            height=values["height"],
            duration_ms=values["duration_ms"],
            page_count=values["page_count"],
            metadata=values["metadata"],
        )


def _png() -> bytes:
    image = Image.new("RGB", (24, 12), (20, 80, 160))
    output = io.BytesIO()
    try:
        image.save(output, format="PNG")
        return output.getvalue()
    finally:
        image.close()


def _request(content: bytes, *, timeout_seconds: float = 5.0) -> PreviewRequest:
    return PreviewRequest(
        workspace_id=uuid4(),
        library_item_id=uuid4(),
        version_id=uuid4(),
        input_kind="source",
        input_conversion_attempt_id=None,
        input_artifact_id=None,
        source=_MemoryBlob(content),
        input_sha256=hashlib.sha256(content).hexdigest(),
        byte_size=len(content),
        format_id="png",
        media_type="image/png",
        original_filename="source.png",
        limits=PreviewLimits(
            max_input_bytes=max(1, len(content)),
            timeout_seconds=timeout_seconds,
        ),
        trace_id="preview-subprocess-test",
    )


def test_bundled_pillow_preview_runs_in_fixed_child_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = PillowImagePreviewProvider()
    sink = _CapturingSink()
    monkeypatch.setattr(
        provider,
        "generate",
        lambda *_args, **_kwargs: pytest.fail("parent provider executed"),
    )

    result = BundledSubprocessPreviewExecutionBoundary().execute(
        plugin=provider,
        descriptor=provider.descriptor(),
        request=_request(_png()),
        sink=sink,
    )

    assert result.completeness == "complete"
    assert len(result.variants) == 1
    assert result.variants[0].media_type == "image/png"
    assert hashlib.sha256(sink.content).hexdigest() == result.variants[0].sha256
    with Image.open(io.BytesIO(sink.content)) as generated:
        assert generated.format == "PNG"
        assert generated.size == (24, 12)


def test_hanging_preview_host_is_timed_out_and_its_child_tree_is_killed(
    tmp_path: Path,
) -> None:
    heartbeat = tmp_path / "heartbeat.txt"
    child = tmp_path / "heartbeat_child.py"
    child.write_text(
        "from pathlib import Path\n"
        "import sys,time\n"
        "path=Path(sys.argv[1])\n"
        "for counter in range(1000):\n"
        "    path.write_text(str(counter),encoding='utf-8')\n"
        "    time.sleep(0.02)\n",
        encoding="utf-8",
    )
    parent = tmp_path / "hanging_host.py"
    parent.write_text(
        "from pathlib import Path\n"
        "import subprocess,sys,time\n"
        "subprocess.Popen([sys.executable,sys.argv[1],sys.argv[2]])\n"
        "deadline=time.monotonic()+2\n"
        "while not Path(sys.argv[2]).exists() and time.monotonic()<deadline:\n"
        "    time.sleep(0.01)\n"
        "time.sleep(20)\n",
        encoding="utf-8",
    )
    provider = PillowImagePreviewProvider()
    descriptor = provider.descriptor()
    executable = str(Path(sys.executable).absolute())
    boundary = BundledSubprocessPreviewExecutionBoundary(
        {
            descriptor.provider_id: (
                executable,
                str(parent),
                str(child),
                str(heartbeat),
            )
        }
    )

    started = time.monotonic()
    with pytest.raises(PreviewProviderError) as exc_info:
        boundary.execute(
            plugin=provider,
            descriptor=descriptor,
            request=_request(b"x", timeout_seconds=1.0),
            sink=_CapturingSink(),
        )
    elapsed = time.monotonic() - started

    assert exc_info.value.code == "preview_timeout"
    assert exc_info.value.retryable is True
    assert elapsed < 8
    assert heartbeat.exists(), "the descendant process never started"
    stopped_value = heartbeat.read_text(encoding="utf-8")
    time.sleep(0.3)
    assert heartbeat.read_text(encoding="utf-8") == stopped_value


def test_exited_host_leader_cannot_leave_transport_waiting_on_descendant(
    tmp_path: Path,
) -> None:
    heartbeat = tmp_path / "orphan-heartbeat.txt"
    child = tmp_path / "orphan_child.py"
    child.write_text(
        "from pathlib import Path\n"
        "import sys,time\n"
        "path=Path(sys.argv[1])\n"
        "for counter in range(75):\n"
        "    path.write_text(str(counter),encoding='utf-8')\n"
        "    time.sleep(0.02)\n",
        encoding="utf-8",
    )
    parent = tmp_path / "exiting_host.py"
    parent.write_text(
        "from pathlib import Path\n"
        "import subprocess,sys,time\n"
        "subprocess.Popen([sys.executable,sys.argv[1],sys.argv[2]])\n"
        "deadline=time.monotonic()+2\n"
        "while not Path(sys.argv[2]).exists() and time.monotonic()<deadline:\n"
        "    time.sleep(0.01)\n",
        encoding="utf-8",
    )
    provider = PillowImagePreviewProvider()
    descriptor = provider.descriptor()
    boundary = BundledSubprocessPreviewExecutionBoundary(
        {
            descriptor.provider_id: (
                str(Path(sys.executable).absolute()),
                str(parent),
                str(child),
                str(heartbeat),
            )
        }
    )

    started = time.monotonic()
    with pytest.raises(PreviewProviderError) as exc_info:
        boundary.execute(
            plugin=provider,
            descriptor=descriptor,
            request=_request(b"x", timeout_seconds=1.0),
            sink=_CapturingSink(),
        )

    assert exc_info.value.code in {"preview_timeout", "preview_output_invalid"}
    assert time.monotonic() - started < 8
    assert heartbeat.exists(), "the descendant process never started"

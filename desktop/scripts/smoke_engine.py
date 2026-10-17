"""Smoke-test the frozen desktop engine without touching the user's wiki."""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]


def _default_engine() -> Path:
    filename = "dtxwiki-engine.exe" if sys.platform == "win32" else "dtxwiki-engine"
    return ROOT / "desktop" / "build" / "engine" / filename


def _available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_until_ready(origin: str, process: subprocess.Popen, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    last_error = "no response"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"engine exited before readiness with code {process.returncode}"
            )
        try:
            with urllib.request.urlopen(
                f"{origin}/health/ready", timeout=2
            ) as response:
                payload = json.load(response)
                if (
                    response.status == 200
                    and payload.get("status") == "ready"
                    and payload.get("components", {}).get("worker") == "ok"
                ):
                    return
                last_error = f"HTTP {response.status}: {payload}"
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            last_error = str(error)
        time.sleep(0.25)
    raise TimeoutError(f"engine readiness timed out: {last_error}")


def _check_file_processing(
    origin: str, process: subprocess.Popen, timeout: int
) -> None:
    """A real queued conversion must finish without a separately launched worker."""

    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )

    def request(
        path: str, *, data: bytes | None = None, content_type: str = "application/json"
    ):
        with opener.open(
            urllib.request.Request(
                origin + path,
                data=data,
                headers={"Content-Type": content_type},
            ),
            timeout=5,
        ) as response:
            return json.load(response)["data"]

    request(
        "/api/v1/auth/signup",
        data=json.dumps(
            {"email": "desktop-smoke@example.com", "password": "desktop-smoke-password"}
        ).encode(),
    )
    workspaces = request("/api/v1/workspaces")
    workspace_id = (
        workspaces.get("current_workspace_id")
        or workspaces.get("default_workspace_id")
        or workspaces["personal_workspace_id"]
    )
    endpoint = f"/api/v1/workspaces/{workspace_id}/library/items"
    marker = "DesktopManagedWorkerSmoke"
    boundary = "desktop-smoke-upload-boundary"
    docx = io.BytesIO()
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.'
            'openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body><w:p><w:r><w:t>{marker}</w:t></w:r></w:p></w:body></w:document>",
        )
    fixtures = (
        (
            "worker-smoke.json",
            "application/json",
            json.dumps({"marker": marker}).encode(),
        ),
        (
            "worker-smoke.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            docx.getvalue(),
        ),
    )
    for filename, media_type, content in fixtures:
        payload = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {media_type}\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        uploaded = request(
            endpoint,
            data=payload,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        deadline = time.monotonic() + timeout
        last_detail = uploaded
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("desktop engine exited during file conversion")
            last_detail = request(f"{endpoint}/{uploaded['id']}")
            version = last_detail.get("current_version") or {}
            if (
                version.get("id")
                and last_detail.get("current_ready_version_id") == version["id"]
                and version.get("selected_conversion_attempt_id")
            ):
                if any(
                    artifact.get("library_item_version_id") == version["id"]
                    and marker in (artifact.get("index_text") or "")
                    for artifact in last_detail.get("artifacts", [])
                ):
                    break
            if version.get("processing_status") == "failed":
                raise RuntimeError(f"desktop file conversion failed: {last_detail}")
            time.sleep(0.1)
        else:
            raise TimeoutError(f"desktop file conversion did not finish: {last_detail}")


def _check_frontend(origin: str) -> None:
    """Readiness alone does not prove the packaged SPA and its assets exist."""

    class Assets(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.scripts: list[str] = []
            self.styles: list[str] = []

        def handle_starttag(self, tag: str, attrs) -> None:
            values = dict(attrs)
            if tag == "script" and values.get("src"):
                self.scripts.append(values["src"])
            if tag == "link" and values.get("rel") == "stylesheet":
                self.styles.append(values["href"])

    with urllib.request.urlopen(origin + "/", timeout=5) as response:
        if response.status != 200 or "text/html" not in response.headers.get(
            "Content-Type", ""
        ):
            raise RuntimeError("packaged SPA index was not served as HTML")
        document = response.read().decode("utf-8")
    assets = Assets()
    assets.feed(document)
    if not assets.scripts or not assets.styles:
        raise RuntimeError("packaged SPA index is missing JavaScript or CSS assets")
    for asset in (*assets.scripts, *assets.styles):
        url = urljoin(origin + "/", asset)
        if urlsplit(url).netloc != urlsplit(origin).netloc:
            raise RuntimeError("packaged SPA references an external runtime asset")
        with urllib.request.urlopen(url, timeout=5) as response:
            content_type = response.headers.get("Content-Type", "")
            if response.status != 200 or not response.read():
                raise RuntimeError(f"packaged SPA asset is absent: {asset}")
            expected = "javascript" if asset in assets.scripts else "text/css"
            if expected not in content_type:
                raise RuntimeError(f"packaged SPA asset has wrong type: {asset}")


def _check_preview_host(engine: Path, timeout: int) -> None:
    """Validate the frozen host without enabling optional production providers."""

    # The descriptor digest must come from the checkout being built, including
    # when another worktree owns this interpreter's editable installation.
    sys.path.insert(0, str(ROOT))
    from PIL import Image

    from backend.preview_providers.pillow_image import PillowImagePreviewProvider
    from backend.spi.preview import PREVIEW_PROVIDER_SPI_VERSION, PreviewLimits

    source = io.BytesIO()
    with Image.new("RGB", (4, 3), (20, 80, 160)) as image:
        image.save(source, format="PNG")
    content = source.getvalue()
    descriptor = PillowImagePreviewProvider().descriptor()
    payload = {
        "protocol": PREVIEW_PROVIDER_SPI_VERSION,
        "provider_id": descriptor.provider_id,
        "provider_version": descriptor.provider_version,
        "distribution_digest": descriptor.distribution_digest,
        "source_path": "source.bin",
        "workspace_id": str(uuid4()),
        "library_item_id": str(uuid4()),
        "version_id": str(uuid4()),
        "input_kind": "source",
        "input_conversion_attempt_id": None,
        "input_artifact_id": None,
        "input_sha256": hashlib.sha256(content).hexdigest(),
        "byte_size": len(content),
        "format_id": "png",
        "media_type": "image/png",
        "original_filename": "source.png",
        "limits": asdict(PreviewLimits(max_input_bytes=len(content))),
        "recipe": {},
        "trace_id": "desktop-frozen-preview-smoke",
    }
    with tempfile.TemporaryDirectory(prefix="dtxwiki-frozen-preview-") as scratch:
        Path(scratch, "source.bin").write_bytes(content)
        result = subprocess.run(
            [str(engine), "preview-host", "--provider", descriptor.provider_id],
            input=json.dumps(payload).encode(),
            capture_output=True,
            check=True,
            cwd=scratch,
            timeout=timeout,
        )
        response = json.loads(result.stdout)
        if "error" in response:
            raise RuntimeError(f"frozen Preview host failed: {response['error']}")
        variant = response["variants"][0]
        generated = Path(scratch, variant["path"]).read_bytes()
        if hashlib.sha256(generated).hexdigest() != variant["sha256"]:
            raise RuntimeError("frozen Preview host output digest mismatch")
        with Image.open(io.BytesIO(generated)) as image:
            if image.size != (4, 3) or image.format != "PNG":
                raise RuntimeError("frozen Preview host output dimensions changed")


def _check_notices(engine: Path) -> None:
    """The installer redistributes the frozen runtime; its notices must ship."""

    notices = engine.parent / "licenses" / "PYTHON_THIRD_PARTY_NOTICES.txt"
    if not notices.is_file():
        raise FileNotFoundError(notices)
    text = notices.read_text(encoding="utf-8")
    listed = [line for line in text.splitlines() if line.startswith("- ")]
    if not listed or not listed[0].startswith("- Python ") or "- fastapi==" not in text:
        raise RuntimeError(f"{notices} does not list the frozen Python runtime")


def smoke_engine(engine: Path, timeout: int = 60) -> None:
    engine = engine.resolve()
    if not engine.is_file():
        raise FileNotFoundError(engine)
    _check_notices(engine)
    _check_preview_host(engine, timeout)
    port = _available_port()
    with tempfile.TemporaryDirectory(prefix="dtxwiki-desktop-smoke-") as data_dir:
        environment = os.environ.copy()
        environment["DTXWIKI_DATA_DIR"] = data_dir
        environment["HOME"] = data_dir
        environment["USERPROFILE"] = data_dir
        environment["APPDATA"] = str(Path(data_dir) / "AppData" / "Roaming")
        bridge = subprocess.run(
            [str(engine), "mcp-detect"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=timeout,
            check=True,
        )
        inventory = json.loads(bridge.stdout)
        client_ids = {client["id"] for client in inventory.get("clients", [])}
        expected_client_ids = {
            "claude-code",
            "codex",
            "gemini-cli",
            "antigravity",
        }
        if (
            not inventory.get("ok")
            or client_ids != expected_client_ids
            or any(
                "local" in client or "remote" in client
                for client in inventory.get("clients", [])
            )
        ):
            raise RuntimeError("frozen HTTP MCP bridge inventory is invalid")
        process = subprocess.Popen(
            [str(engine), "--host", "127.0.0.1", "--port", str(port)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
        )
        try:
            origin = f"http://127.0.0.1:{port}"
            _wait_until_ready(origin, process, timeout)
            _check_frontend(origin)
            _check_file_processing(origin, process, timeout)
            assert process.stdin is not None
            process.stdin.write("shutdown\n")
            process.stdin.flush()
            # Match Electron's budget: HTTP drain 10s + worker stop 10s.
            process.wait(timeout=25)
            if process.returncode != 0:
                raise RuntimeError(f"engine shutdown returned {process.returncode}")
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            assert process.stdout is not None
            output = process.stdout.read()
            if output:
                print(output, file=sys.stderr)
            raise
        finally:
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()
    print(f"desktop engine smoke passed on 127.0.0.1:{port}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, default=_default_engine())
    parser.add_argument("--timeout", type=int, default=60)
    arguments = parser.parse_args()
    smoke_engine(arguments.engine, timeout=arguments.timeout)


if __name__ == "__main__":
    main()

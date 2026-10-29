from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "dist"


def main() -> None:
    wheel = latest_wheel()
    with tempfile.TemporaryDirectory(prefix="dtxwiki-packaged-smoke-") as tmp_name:
        tmp = Path(tmp_name)
        venv = tmp / "venv"
        port = free_port()
        data_dir = tmp / "data"
        db_path = tmp / "dtxwiki.sqlite"
        log_path = tmp / "server.log"

        run([sys.executable, "-m", "venv", str(venv)])
        python = venv_python(venv)
        run(
            [
                str(python),
                "-m",
                "pip",
                "--disable-pip-version-check",
                "install",
                str(wheel),
            ]
        )
        run([str(venv_bin(venv, "dtxwiki")), "migrate-data", "--help"])

        env = {
            **os.environ,
            "DTXWIKI_DATA_DIR": str(data_dir),
        }
        server = start_server(
            command=[
                str(venv_bin(venv, "dtxwiki")),
                "--no-open",
                "--port",
                str(port),
                "--db",
                f"sqlite+pysqlite:///{db_path}",
                "--log-level",
                "warning",
            ],
            env=env,
            log_path=log_path,
        )
        try:
            wait_for_health(port)
            run_packaged_browser_check(port)
        except Exception:
            print_server_log(log_path)
            raise
        finally:
            stop_server(server)

    print(f"packaged browser smoke passed for {wheel.name}")


def latest_wheel() -> Path:
    wheels = sorted(DIST.glob("dtxwiki-*.whl"), key=lambda path: path.stat().st_mtime)
    if not wheels:
        raise FileNotFoundError(
            "No wheel found under dist/. Run make build-wheel first."
        )
    return wheels[-1]


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def venv_bin(venv: Path, name: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    return venv / ("Scripts" if os.name == "nt" else "bin") / f"{name}{suffix}"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, check=True, text=True, **kwargs)


def start_server(
    command: list[str],
    env: dict[str, str],
    log_path: Path,
) -> subprocess.Popen[str]:
    log_file = log_path.open("w", encoding="utf-8")
    try:
        return subprocess.Popen(
            command,
            # The worker must import the installed wheel, not this checkout.
            cwd=log_path.parent,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except Exception:
        log_file.close()
        raise


def wait_for_health(port: int) -> None:
    deadline = time.monotonic() + 30
    url = f"http://127.0.0.1:{port}/health/live"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.25)
    raise TimeoutError(f"Timed out waiting for {url}")


def run_packaged_browser_check(port: int) -> None:
    script = """
import { chromium, expect } from '@playwright/test';

const port = process.env.DTXWIKI_SMOKE_PORT;
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage();
const apiRequests = [];
const failedRequests = [];
const pageErrors = [];
const baseUrl = `http://127.0.0.1:${port}`;
page.on('request', (request) => {
  const url = request.url();
  if (url.includes('/api/v1/')) {
    apiRequests.push(url);
  }
});
page.on('requestfailed', (request) => {
  const failure = request.failure()?.errorText ?? '';
  failedRequests.push(`${request.method()} ${request.url()} ${failure}`);
});
page.on('pageerror', (error) => pageErrors.push(error.message));
const signup = await page.request.post(`${baseUrl}/api/v1/auth/signup`, {
  data: { email: 'smoke@example.com', password: 'correct-password' },
});
if (!signup.ok()) {
  throw new Error(`signup failed: ${signup.status()} ${await signup.text()}`);
}
const health = await page.request.get(`${baseUrl}/health/ready`);
expect((await health.json()).components.worker).toBe('ok');
const workspaces = await page.request.get(`${baseUrl}/api/v1/workspaces`);
expect(workspaces.ok()).toBeTruthy();
const workspaceId = (await workspaces.json()).data.default_workspace_id;
expect(workspaceId).toBeTruthy();
const libraryUrl = `${baseUrl}/api/v1/workspaces/${workspaceId}/library/items`;
const marker = 'The packaged worker converted this uploaded document.';
const upload = await page.request.post(libraryUrl, {
  multipart: {
    file: {
      name: 'packaged-worker.md', mimeType: 'text/markdown',
      buffer: Buffer.from(`# Packaged worker proof\\n\\n${marker}\\n`),
    },
  },
});
expect(upload.status(), await upload.text()).toBe(201);
const uploaded = (await upload.json()).data;
let converted;
await expect.poll(async () => {
  const response = await page.request.get(`${libraryUrl}/${uploaded.id}`);
  expect(response.ok()).toBeTruthy();
  converted = (await response.json()).data;
  return converted.current_version.processing_status;
}, { timeout: 30000, message: 'The default launch must process Library uploads' })
  .toBe('ready');
expect(converted.current_ready_version.id).toBe(uploaded.current_version.id);
expect(converted.current_version.selected_conversion_attempt_id).toBeTruthy();
expect(converted.artifacts.some((artifact) =>
  artifact.stage === 'conversion' && artifact.role === 'markdown'
  && artifact.index_text.includes(marker),
)).toBeTruthy();
await page.goto(`${baseUrl}/workspaces/${workspaceId}/dashboard`, {
  waitUntil: 'networkidle',
});
await page
  .getByRole('heading', { name: 'Workspace Operations', exact: true })
  .waitFor({ timeout: 10000 });
await page.getByText('Workspace now', { exact: true }).waitFor({ timeout: 10000 });
if (pageErrors.length > 0) {
  throw new Error(`page errors: ${pageErrors.join('; ')}`);
}
if (failedRequests.length > 0) {
  throw new Error(`failed requests: ${failedRequests.join('; ')}`);
}
if (apiRequests.length === 0) {
  throw new Error('no packaged API requests observed');
}
const expectedPrefix = `http://127.0.0.1:${port}/api/v1/`;
const wrongOrigin = apiRequests.filter(
  (url) => url.startsWith('http://127.0.0.1:21080') && !url.startsWith(expectedPrefix),
);
if (wrongOrigin.length > 0) {
  throw new Error(`unexpected dev-origin requests: ${wrongOrigin.join(', ')}`);
}
const sameOrigin = apiRequests.filter((url) => url.startsWith(expectedPrefix));
if (sameOrigin.length === 0) {
  throw new Error(`no same-origin packaged API requests: ${apiRequests.join(', ')}`);
}
console.log(JSON.stringify({ apiRequests: sameOrigin }, null, 2));
await browser.close();
"""
    run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        env={**os.environ, "DTXWIKI_SMOKE_PORT": str(port)},
    )


def stop_server(server: subprocess.Popen[str]) -> None:
    if server.poll() is not None:
        return
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait(timeout=10)


def print_server_log(log_path: Path) -> None:
    if log_path.is_file():
        print("----- packaged server log -----", file=sys.stderr)
        print(log_path.read_text(encoding="utf-8"), file=sys.stderr)


if __name__ == "__main__":
    main()

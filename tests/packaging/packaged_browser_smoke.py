from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from backend.tests.utils.environment import child_env_without_dtxwiki_config

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
        env = isolated_server_env(data_dir)
        verify_installed_backend(python, cwd=tmp, env=env)
        run(
            [str(venv_bin(venv, "dtxwiki")), "migrate-data", "--help"],
            cwd=tmp,
            env=env,
        )
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


def isolated_server_env(data_dir: Path) -> dict[str, str]:
    # A wheel check must neither import checkout modules through PYTHONPATH nor
    # inherit a developer's operational database/configuration or user data.
    env = child_env_without_dtxwiki_config()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["DTXWIKI_DATA_DIR"] = str(data_dir)
    # Exercise the shipped security policy, including Blob/module Workers.
    env["DTXWIKI_FORCE_SECURITY_HEADERS"] = "true"
    return env


def verify_installed_backend(python: Path, *, cwd: Path, env: dict[str, str]) -> None:
    run(
        [
            str(python),
            "-I",
            "-c",
            "import sys; from pathlib import Path; import backend; "
            "import backend.cli.main; "
            "prefix = Path(sys.prefix).resolve(); "
            "assert Path(backend.__file__).resolve().is_relative_to(prefix); "
            "assert Path(backend.cli.main.__file__).resolve().is_relative_to(prefix)",
        ],
        cwd=cwd,
        env=env,
    )


def start_server(
    command: list[str],
    env: dict[str, str],
    log_path: Path,
) -> subprocess.Popen[str]:
    log_file = log_path.open("w", encoding="utf-8")
    try:
        return subprocess.Popen(
            command,
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
    script = r"""
import { chromium, expect } from '@playwright/test';
import { randomUUID } from 'node:crypto';

const port = process.env.DTXWIKI_SMOKE_PORT;
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage();
const apiRequests = [];
const failedRequests = [];
const pageErrors = [];
const consoleErrors = [];
const collisionResponses = [];
const workerUrls = [];
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
page.on('console', (message) => {
  if (message.type() === 'error') consoleErrors.push(message.text());
});
page.on('worker', (worker) => workerUrls.push(worker.url()));
page.context().on('response', (response) => {
  const path = new URL(response.url()).pathname;
  if (/\/forceAtlas2Collision\.worker-[^/]+\.js$/.test(path)) {
    collisionResponses.push({ url: response.url(), status: response.status(),
      contentType: response.headers()['content-type'] ?? '' });
  }
});
async function requestData(method, path, data) {
  const response = await page.request.fetch(`${baseUrl}${path}`, { method, data });
  if (!response.ok()) throw new Error(`${method} ${path} failed: ${response.status()}`);
  return (await response.json()).data;
}
try {
const signup = await page.request.post(`${baseUrl}/api/v1/auth/signup`, {
  data: { email: 'smoke@example.com', password: 'correct-password' },
});
if (!signup.ok()) {
  throw new Error(`signup failed: ${signup.status()} ${await signup.text()}`);
}
await page.goto(`${baseUrl}/dashboard`, { waitUntil: 'networkidle' });
await page
  .getByRole('heading', { name: 'Workspace Operations', exact: true })
  .waitFor({ timeout: 10000 });
await page.getByText('Workspace now', { exact: true }).waitFor({ timeout: 10000 });
// A small REST-authored chain checks installed assets/Workers/CSP, not 2K
// capacity, convergence, graph readability or target-device performance.
const workspace = await requestData('POST', '/api/v1/workspaces', {
  title: 'Packaged Graph Worker smoke',
});
const workspacePath = `/api/v1/workspaces/${workspace.id}`;
await requestData('PATCH', `${workspacePath}/settings/policies`, {
  group: 'graph', key: 'workspace_map_enabled', value: true,
});
const documents = [];
for (let index = 0; index < 8; index++) {
  const previous = documents[index - 1];
  const created = await requestData('POST', `${workspacePath}/context/pages`, {
    title: `Packaged smoke document ${index}`,
    body_md: previous ? `Connected to [[${previous.slug}]].` : 'Packaged Worker root.',
  });
  const submitted = await requestData('POST',
    `${workspacePath}/context/pages/${created.id}/submit-for-review`, {
      version_id: created.version_id,
      expected_editing_revision: created.editing_revision,
      idempotency_key: randomUUID(),
    });
  await requestData('POST',
    `${workspacePath}/reviews/${submitted.review_item_id}/decision`,
    { decision_type: 'approve' });
  const versionPath = `${workspacePath}/context/pages/${created.id}/versions`;
  await requestData('POST',
    `${versionPath}/${submitted.version_id}/publish`, {
      reason: 'Packaged browser Worker smoke fixture',
      expected_content_hash: submitted.content_hash,
    });
  documents.push(created);
}
// Use the real application's workspace selection hint, resolved against its
// authenticated workspace list. No response, Worker or coordinate is mocked.
await page.evaluate(({ id, slug }) => {
  localStorage.setItem('dtxwiki.selectedWorkspaceId', id);
  localStorage.setItem('dtxwiki.selectedWorkspaceSlug', slug);
}, workspace);
const overviewPromise = page.waitForResponse(response =>
  new URL(response.url()).pathname === `${workspacePath}/graph/overview/query`
  && response.request().method() === 'POST');
const navigation = await page.goto(`${baseUrl}/graph`);
if (!navigation?.ok()) throw new Error('packaged Graph View document failed to load');
const csp = navigation.headers()['content-security-policy'] ?? '';
if (!csp.split(';').some(directive => /^\s*worker-src\s/.test(directive)
  && directive.includes("'self'") && directive.includes('blob:'))) {
  throw new Error('packaged Graph View has no shipped Blob/module Worker CSP');
}
const overviewResponse = await overviewPromise;
if (!overviewResponse.ok()) {
  throw new Error(`graph overview failed: ${overviewResponse.status()}`);
}
expect(overviewResponse.request().postDataJSON().budget).toEqual({
  documents: 2000, relations: 6000,
});
const scene = (await overviewResponse.json()).data;
expect(scene.representation).toBe('exact');
expect(scene.completeness).toBe('complete');
expect(scene.truncated).toBe(false);
expect(scene.nodes).toHaveLength(documents.length);
expect(scene.edges).toHaveLength(documents.length - 1);
const ids = new Map(scene.nodes.map(node => [node.entity_id, node.id]));
expect(new Set(ids.keys())).toEqual(new Set(documents.map(document => document.id)));
expect(new Set(scene.edges.map(edge => `${edge.source}->${edge.target}`))).toEqual(
  new Set(documents.slice(1).map((document, index) =>
    `${ids.get(document.id)}->${ids.get(documents[index].id)}`)));
expect(scene.edges.every(edge => edge.relation_kind === 'wikilink'
  && edge.direction === 'directed' && !edge.aggregate
  && edge.relation_count === 1)).toBe(true);
const canvas = page.getByTestId('graph-canvas');
await expect(canvas.locator('..')).toHaveAttribute(
  'data-renderer-status', 'ready', { timeout: 30000 });
await expect(canvas).toHaveAttribute('data-map-physics', 'force-atlas2-worker');
await expect(canvas.locator('canvas')).toHaveCount(1);
await expect.poll(async () =>
  Number(await canvas.getAttribute('data-map-layout-ticks')),
{ timeout: 30000 }).toBeGreaterThan(0);
const readPublishedLayout = () => canvas.evaluate(element => ({
  phase: element.dataset.mapLayoutPhase,
  iterations: Number(element.dataset.mapLayoutIterations),
  structureIterations: Number(element.dataset.mapLayoutStructureIterations),
  contactZoom: Number(element.dataset.mapContactReferenceZoom),
}));
await expect.poll(async () => {
  const state = await readPublishedLayout();
  return state.phase === 'contact' && state.iterations > state.structureIterations
    && state.contactZoom > 0 && Number.isFinite(state.contactZoom);
}, { timeout: 45000 }).toBe(true);
await expect(page.getByTestId('workspace-map-layout-status')).toHaveCount(0);
expect(Number(await canvas.getAttribute('data-map-node-count'))).toBe(documents.length);
expect(Number(await canvas.getAttribute('data-map-edge-count')))
  .toBe(documents.length - 1);
expect(workerUrls.some(url => url.startsWith('blob:'))).toBe(true);
expect(collisionResponses.length).toBeGreaterThan(0);
expect(collisionResponses.every(response => response.status === 200
  && new URL(response.url).origin === baseUrl
  && /javascript/.test(response.contentType))).toBe(true);
if (pageErrors.length > 0) {
  throw new Error(`page errors: ${pageErrors.join('; ')}`);
}
if (failedRequests.length > 0) {
  throw new Error(`failed requests: ${failedRequests.join('; ')}`);
}
if (consoleErrors.length > 0) {
  throw new Error(`console errors: ${consoleErrors.join('; ')}`);
}
if (apiRequests.length === 0) {
  throw new Error('no packaged API requests observed');
}
const expectedPrefix = `http://127.0.0.1:${port}/api/v1/`;
const wrongOrigin = apiRequests.filter(
  (url) => !url.startsWith(expectedPrefix),
);
if (wrongOrigin.length > 0) {
  throw new Error(`unexpected dev-origin requests: ${wrongOrigin.join(', ')}`);
}
const sameOrigin = apiRequests.filter((url) => url.startsWith(expectedPrefix));
if (sameOrigin.length === 0) {
  throw new Error(`no same-origin packaged API requests: ${apiRequests.join(', ')}`);
}
console.log(JSON.stringify({ apiRequests: sameOrigin, collisionResponses,
  graph: { nodes: scene.nodes.length, originalRelations: scene.edges.length,
    physics: await canvas.getAttribute('data-map-physics'),
    ...await readPublishedLayout() },
  scope: 'Small installed-wheel Worker/CSP runtime smoke; '
    + 'not 2K capacity or device acceptance' }, null, 2));
} finally {
  await browser.close();
}
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

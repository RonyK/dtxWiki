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
    # Exercise the shipped security policy, including same-origin module Workers.
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
const layoutResponses = [];
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
  if (/\/d3Layout\.worker-[^/]+\.js$/.test(path)) {
    layoutResponses.push({ url: response.url(), status: response.status(),
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
// Labs are saved per workspace. Enable Agent for the independent inventory
// below and Knowledge Map for the canvas through the shipped Settings form.
await page.goto(`${baseUrl}/workspaces/${workspace.id}/settings?section=labs`);
for (const { name, group } of [
  { name: /^Agent$/, group: 'labs' },
  { name: /^(Workspace Knowledge Map|워크스페이스 지식 지도)$/, group: 'graph' },
]) {
  const checkbox = page.getByRole('checkbox', { name });
  await checkbox.check();
  const card = page.locator('.policy-card').filter({ has: checkbox });
  const save = card.getByRole('button', { name: /^(Save|저장)$/ });
  await expect(save).toBeEnabled();
  const [saved] = await Promise.all([
    page.waitForResponse(response =>
      new URL(response.url()).pathname === `${workspacePath}/settings/policies/batch`
      && response.request().method() === 'PATCH'
      && response.request().postDataJSON()?.group === group),
    save.click(),
  ]);
  expect(saved.ok()).toBeTruthy();
  await expect(save).toBeDisabled();
}
// New workspaces include a real active Agent page. Read its identity through
// the independent inventory instead of assuming the graph starts empty.
const seededAgents = await requestData('GET',
  `${workspacePath}/agent/pages?lifecycle_status=active`);
expect(seededAgents.map(agent => agent.slug)).toEqual(['default-system-prompt']);
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
// Navigate with the authored workspace identity; the unscoped /graph alias
// can resolve the user's default workspace before selection hints are read.
const overviewPromise = page.waitForResponse(response =>
  new URL(response.url()).pathname === `${workspacePath}/graph/overview/query`
  && response.request().method() === 'POST');
const navigation = await page.goto(`${baseUrl}/workspaces/${workspace.id}/graph`);
if (!navigation?.ok()) throw new Error('packaged Graph View document failed to load');
const csp = navigation.headers()['content-security-policy'] ?? '';
if (!csp.split(';').some(directive => /^\s*worker-src\s/.test(directive)
  && directive.trim() === "worker-src 'self'")) {
  throw new Error('packaged Graph View has no shipped same-origin module Worker CSP');
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
const documentNodes = scene.nodes.filter(node => node.grain === 'document');
const folderNodes = scene.nodes.filter(node => node.grain === 'folder');
const contextNodes = documentNodes.filter(node => node.entity_kind === 'context');
const agentNodes = documentNodes.filter(node => node.entity_kind === 'agent');
const authoredEdges = scene.edges.filter(edge => edge.relation_kind !== 'tree');
const containmentEdges = scene.edges.filter(edge => edge.relation_kind === 'tree');
expect(contextNodes).toHaveLength(documents.length);
expect(new Set(agentNodes.map(node => node.entity_id))).toEqual(
  new Set(seededAgents.map(agent => agent.id)));
expect(documentNodes).toHaveLength(documents.length + seededAgents.length);
// The provisioned Context/Inbox and Agent/Agent Guide ancestors are rendered
// as ordinary folder nodes. Their containment is outside authored budgets.
expect(folderNodes.map(node => node.path).sort()).toEqual([
  'Agent', 'Agent / Agent Guide', 'Context', 'Context / Inbox',
]);
expect(folderNodes.every(node => node.id === `folder:${node.folder_id}`)).toBe(true);
expect(scene.nodes).toHaveLength(documents.length + seededAgents.length + 4);
expect(authoredEdges).toHaveLength(documents.length - 1);
expect(scene.counts).toMatchObject({
  rendered_documents: documents.length + seededAgents.length,
  rendered_clusters: 0,
  rendered_relations: documents.length - 1,
});
const ids = new Map(contextNodes.map(node => [node.entity_id, node.id]));
expect(new Set(ids.keys())).toEqual(new Set(documents.map(document => document.id)));
expect(new Set(authoredEdges.map(edge => `${edge.source}->${edge.target}`))).toEqual(
  new Set(documents.slice(1).map((document, index) =>
    `${ids.get(document.id)}->${ids.get(documents[index].id)}`)));
expect(authoredEdges.every(edge => edge.relation_kind === 'wikilink'
  && edge.direction === 'directed' && !edge.aggregate
  && edge.relation_count === 1)).toBe(true);
const folders = new Map(folderNodes.map(node => [node.path, node.id]));
const expectedContainment = [
  ...contextNodes.map(node => [folders.get('Context / Inbox'), node.id]),
  ...agentNodes.map(node => [folders.get('Agent / Agent Guide'), node.id]),
  [folders.get('Context'), folders.get('Context / Inbox')],
  [folders.get('Agent'), folders.get('Agent / Agent Guide')],
];
expect(containmentEdges).toHaveLength(documents.length + seededAgents.length + 2);
expect(new Set(containmentEdges.map(edge => `${edge.source}->${edge.target}`))).toEqual(
  new Set(expectedContainment.map(([source, target]) => `${source}->${target}`)));
expect(containmentEdges.every(edge =>
  edge.id === `contains:${edge.source}|${edge.target}`
  && edge.direction === 'directed' && !edge.aggregate && edge.primary
  && edge.relation_count === 1)).toBe(true);
expect(scene.edges).toHaveLength((documents.length - 1) + expectedContainment.length);
const canvas = page.getByTestId('graph-canvas');
await expect(canvas.locator('..')).toHaveAttribute(
  'data-renderer-status', 'ready', { timeout: 30000 });
await expect(canvas).toHaveAttribute('data-map-physics', 'd3-worker');
await expect(canvas.locator('canvas')).toHaveCount(1);
await expect.poll(async () =>
  Number(await canvas.getAttribute('data-map-layout-ticks')),
{ timeout: 30000 }).toBeGreaterThan(0);
const readPublishedLayout = () => canvas.evaluate(element => ({
  phase: element.dataset.mapLayoutPhase,
  ticks: Number(element.dataset.mapLayoutTicks),
}));
await expect.poll(async () => {
  const state = await readPublishedLayout();
  return ['live', 'cooled'].includes(state.phase) && state.ticks > 0;
}, { timeout: 45000 }).toBe(true);
await expect(page.getByTestId('workspace-map-layout-status')).toHaveCount(0);
// Containment keeps the seeded Agent connected. Canvas diagnostics report
// admitted documents/authored relations, excluding structural folders/lines.
expect(Number(await canvas.getAttribute('data-map-node-count')))
  .toBe(documents.length + seededAgents.length);
expect(Number(await canvas.getAttribute('data-map-edge-count')))
  .toBe(documents.length - 1);
expect(workerUrls.some(url =>
  /\/d3Layout\.worker-[^/]+\.js$/.test(new URL(url).pathname))).toBe(true);
expect(layoutResponses.length).toBeGreaterThan(0);
expect(layoutResponses.every(response => response.status === 200
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
console.log(JSON.stringify({ apiRequests: sameOrigin, layoutResponses,
  graph: { nodes: scene.nodes.length, documents: documentNodes.length,
    folders: folderNodes.length, originalRelations: authoredEdges.length,
    containmentRelations: containmentEdges.length,
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

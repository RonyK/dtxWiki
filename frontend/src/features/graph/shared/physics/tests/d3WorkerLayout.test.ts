import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { Worker as NodeWorker } from "node:worker_threads";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import { createD3WorkerLayout, createInitialD3Positions } from "../d3WorkerLayout";
import type { D3WorkerResponse } from "../d3WorkerProtocol";
import type { LocalLayoutInput } from "../layoutTypes";

const input: LocalLayoutInput = {
  nodes: Array.from({ length: 40 }, (_, index) => ({ key: `node:${String(index).padStart(2, "0")}`, isSeed: index === 0, radiusPx: 6 })),
  edges: Array.from({ length: 39 }, (_, index) => ({ sourceKey: `node:${String(index).padStart(2, "0")}`,
    targetKey: `node:${String(index + 1).padStart(2, "0")}`, kind: "link" as const, count: 1 })),
};
let workerBody = "";
const live = new Set<ThreadWorker>();
beforeAll(() => {
  // The JSDOM realm has distinct typed-array constructors; build the actual
  // browser Worker in Node, then run that bundle on a real worker thread.
  const require = createRequire(import.meta.url);
  const source = resolve(dirname(fileURLToPath(import.meta.url)), "..", "d3Layout.worker.ts");
  const script = `const esbuild=require(${JSON.stringify(require.resolve("esbuild"))});
    process.stdout.write(esbuild.buildSync({entryPoints:[${JSON.stringify(source)}],bundle:true,
      write:false,format:"esm",platform:"browser"}).outputFiles[0].text);`;
  const bundled = execFileSync(process.execPath, ["-e", script], { encoding: "utf8" });
  workerBody = `import { parentPort } from 'node:worker_threads';
    globalThis.self={onmessage:null,postMessage:(message,transfer)=>parentPort.postMessage(message,transfer)};
    parentPort.on('message',data=>self.onmessage({data}));\n${bundled}`;
});
class ThreadWorker extends EventTarget {
  readonly node = new NodeWorker(new URL(`data:text/javascript;base64,${Buffer.from(workerBody).toString("base64")}`));
  readonly messages: D3WorkerResponse[] = [];
  latestSequence = 0;
  readonly terminate = vi.fn(() => { live.delete(this); void this.node.terminate(); });
  constructor() {
    super(); live.add(this);
    this.node.on("message", data => {
      if (data.positions) data.positions = new Float32Array(data.positions);
      this.messages.push(data); this.dispatchEvent(new MessageEvent("message", { data }));
    });
    this.node.on("error", error => this.dispatchEvent(new ErrorEvent("error", { message: error.message })));
  }
  postMessage(message: unknown, transfer: readonly Transferable[] = []) {
    if (message && typeof message === "object" && "sequence" in message) this.latestSequence = Number(message.sequence);
    this.node.postMessage(message, transfer as Array<ArrayBuffer>);
  }
}
afterEach(() => { for (const worker of live) worker.terminate(); });
const factory = (worker: ThreadWorker) => () => worker as unknown as Worker;

describe("shared D3 Worker and proxy integration", () => {
  it("keeps the real cold solve through queued pause/pin reconciliation and publishes reduced-motion rows with stable catalogs", async () => {
    const worker = new ThreadWorker(), indexed = vi.fn(), onError = vi.fn(), settledPin = vi.fn();
    const layout = createD3WorkerLayout(input, { workerFactory: factory(worker), autoStart: false,
      reducedMotion: true, onIndexedTick: indexed, onError, onSettledPin: settledPin,
      cachedPositions: { "node:02": { x: Infinity, y: 1 }, hidden: { x: 50, y: 60 } } });
    const provisional = createInitialD3Positions(input);
    layout.pause();
    layout.setPositions({ ...provisional, "node:03": { x: 250, y: 175 } }, ["node:03"]);
    layout.setReducedMotion(true);
    await vi.waitFor(() => expect(indexed).toHaveBeenCalled(), { timeout: 10_000 });
    expect(layout.getState().ready).toBe(true);
    expect(layout.getPositions()["node:03"]).toEqual({ x: 250, y: 175 });
    expect(layout.getPositions()["node:12"]).not.toEqual(provisional["node:12"]);
    expect(onError).not.toHaveBeenCalled();
    expect(layout.getPositions()).not.toHaveProperty("hidden");
    const firstCatalog = indexed.mock.calls[0][1];
    layout.resume();
    await vi.waitFor(() => expect(layout.getState().phase).toBe("cooled"));
    expect(indexed.mock.calls.every(([, keys]) => keys === firstCatalog)).toBe(true);
    layout.pinWhenSettled("node:05");
    expect(layout.pin("node:05", { x: NaN, y: 0 })).toBeNull();
    await vi.waitFor(() => expect(settledPin).toHaveBeenCalledOnce());
    layout.update({ ...input, nodes: input.nodes.map(node => ({ ...node, radiusPx: 12 })) }, undefined,
      { settle: true, settleTicks: 96 });
    await vi.waitFor(() => expect(worker.messages.filter(message => message.type === "catalog")).toHaveLength(2));
    expect(worker.messages.filter(message => message.type === "frame").every(frame => !Object.hasOwn(frame, "keys"))).toBe(true);
    expect(layout.getIndexedPositions()).toHaveLength(80);
    layout.destroy(); expect(worker.terminate).toHaveBeenCalledOnce();
  });

  it("restores solved checkpoints without motion, preserves cooled permission resumes, and fails closed on an older live Worker error", async () => {
    const worker = new ThreadWorker(), onError = vi.fn();
    const cached = Object.fromEntries(input.nodes.map((node, index) => [node.key, { x: index * 30, y: index % 2 * 120 }]));
    cached["node:00"] = { x: 0, y: 0 };
    const layout = createD3WorkerLayout(input, { workerFactory: factory(worker), cachedPositions: cached,
      initialPositionsAreSolved: true, autoStart: false, onError });
    await vi.waitFor(() => expect(layout.getState().ready).toBe(true), { timeout: 10_000 });
    const restored = layout.getIndexedPositions(); expect(layout.getPositions()).toEqual(cached);
    layout.resume(); await vi.waitFor(() => expect(layout.getState().phase).toBe("cooled"));
    layout.pause(); layout.resume(); await new Promise(resolve => setTimeout(resolve, 100));
    expect(layout.getIndexedPositions()).toEqual(restored); expect(layout.isRunning()).toBe(false);
    layout.update(input, undefined, { settle: true, settleAtCurrentAlpha: true, settleTicks: 96 });
    await vi.waitFor(() => expect(worker.messages.some(message => message.type === "frame"
      && message.sequence === worker.latestSequence)).toBe(true));
    layout.resume(); await new Promise(resolve => setTimeout(resolve, 100));
    expect(layout.getIndexedPositions()).toEqual(restored); expect(layout.isRunning()).toBe(false);
    layout.resume(true); await vi.waitFor(() => expect(layout.getIndexedPositions()).not.toEqual(restored));
    // A malformed command faults the actual host; a later queued permission
    // command must not hide that older error and leave the proxy waiting.
    worker.node.postMessage({ type: "setPositions", sequence: worker.latestSequence + 1, topology: 2,
      positions: { keys: ["node:00"], positions: new Float32Array([Number.NaN, 0]) } });
    layout.pause(); layout.resume();
    await vi.waitFor(() => expect(onError).toHaveBeenCalledOnce());
    expect(worker.terminate).toHaveBeenCalledOnce(); expect(layout.isRunning()).toBe(false);
  });
});

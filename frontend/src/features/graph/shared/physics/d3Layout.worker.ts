import { createD3Layout, type LocalLayout } from "./d3Layout";
import type { D3WorkerRequest, D3WorkerResponse } from "./d3WorkerProtocol";
import { unpackD3Positions } from "./d3WorkerProtocol";

type WorkerScope = {
  onmessage: ((event: MessageEvent<D3WorkerRequest>) => void) | null;
  postMessage(message: D3WorkerResponse, transfer?: Transferable[]): void;
};
const scope = self as unknown as WorkerScope;
let layout: LocalLayout | null = null;
let sequence = 0;
let topology = 0;
let outstanding = false;
let pending = false;
let applying = false;
let failed = false;

function fail(error: unknown): void {
  if (failed) return;
  failed = true;
  layout?.destroy(); layout = null;
  scope.postMessage({ type: "error", sequence, message: error instanceof Error ? error.message : String(error) });
}
function publish(rows?: Float32Array): void {
  pending = true;
  if (applying || outstanding || !layout || failed) return;
  try {
    const positions = rows ?? layout.getIndexedPositions();
    if (!positions.every(Number.isFinite)) throw new Error("D3 Worker returned non-finite coordinates");
    outstanding = true; pending = false;
    scope.postMessage({ type: "frame", sequence, topology, positions, state: layout.getState() }, [positions.buffer]);
  } catch (error) { fail(error); }
}
function catalog(): void {
  if (layout) scope.postMessage({ type: "catalog", topology, keys: [...layout.getKeys()] });
}

scope.onmessage = event => {
  if (failed) return;
  const request = event.data;
  if (request.type === "ack") {
    outstanding = false;
    if (pending) publish();
    return;
  }
  if (!Number.isSafeInteger(request.sequence) || request.sequence <= sequence) return;
  sequence = request.sequence;
  applying = true;
  try {
    if (request.type === "init") {
      if (layout) throw new Error("D3 Worker is already initialized");
      topology = request.topology;
      layout = createD3Layout(request.input, {
        ...request.options, preserveCoolingOnResume: true,
        cachedPositions: request.options.cachedPositions ? unpackD3Positions(request.options.cachedPositions) : undefined,
        fixedPositions: request.options.fixedPositions ? unpackD3Positions(request.options.fixedPositions) : undefined,
        onIndexedTick: rows => publish(rows),
        onState: () => publish(),
        onSettledPin(key, position) {
          scope.postMessage({ type: "settledPin", sequence, topology, key, position });
        },
      });
      catalog();
    } else {
      if (!layout) throw new Error("D3 Worker is not initialized");
      switch (request.type) {
        case "update":
          topology = request.topology;
          layout.update(request.input, request.fixedPositions ? unpackD3Positions(request.fixedPositions) : undefined,
            { settle: request.settle, settleTicks: request.settleTicks, settleAtCurrentAlpha: request.settleAtCurrentAlpha });
          catalog(); break;
        case "setPositions": layout.setPositions(unpackD3Positions(request.positions), request.pins, request.options); break;
        case "pin": layout.pin(request.key, request.position, request.reheat); break;
        case "beginDrag": layout.beginDrag(request.key, request.position); break;
        case "moveDrag": layout.moveDrag(request.key, request.position); break;
        case "pinWhenSettled": layout.pinWhenSettled(request.key); break;
        case "unpin": layout.unpin(request.key, request.reheat); break;
        case "endDrag": layout.endDrag(request.key); break;
        case "clearPins": layout.clearPins(request.reheat); break;
        case "pause":
          layout.pause();
          if (request.positions) layout.setPositions(unpackD3Positions(request.positions), undefined, { reheat: false });
          break;
        case "resume": layout.resume(request.reheat); break;
        case "setReducedMotion": layout.setReducedMotion(request.enabled); break;
        case "destroy": layout.destroy(); layout = null; break;
      }
    }
    applying = false;
    if (layout) publish();
  } catch (error) { applying = false; fail(error); }
};

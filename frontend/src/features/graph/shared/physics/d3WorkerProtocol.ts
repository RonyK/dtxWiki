import type { LocalLayoutInput, LocalLayoutPositions } from "./layoutTypes";
import type { D3LayoutState, D3LayoutUpdateOptions } from "./d3Layout";
import type { GraphPoint } from "../rendererTypes";

export type D3WorkerOptions = {
  cachedPositions?: LocalLayoutPositions | null;
  fixedPositions?: LocalLayoutPositions | null;
  reducedMotion?: boolean;
  autoStart?: boolean;
  initialPositionsAreSolved?: boolean;
  initialTicks?: number;
};
export type D3ReconcileOptions = { pinsOnly?: boolean; reheat?: boolean };
export type D3WorkerCommand =
  | { type: "init"; input: LocalLayoutInput; options: D3WorkerOptions }
  | ({ type: "update"; input: LocalLayoutInput; fixedPositions?: LocalLayoutPositions } & D3LayoutUpdateOptions)
  | { type: "setPositions"; positions: LocalLayoutPositions; pins?: readonly string[]; options?: D3ReconcileOptions }
  | { type: "pin" | "beginDrag"; key: string; position?: GraphPoint; reheat?: boolean }
  | { type: "moveDrag"; key: string; position: GraphPoint }
  | { type: "pinWhenSettled" | "unpin" | "endDrag"; key: string; reheat?: boolean }
  | { type: "clearPins" | "destroy"; reheat?: boolean }
  | { type: "resume"; reheat?: boolean }
  | { type: "pause"; positions?: LocalLayoutPositions }
  | { type: "setReducedMotion"; enabled: boolean };
type WireCommand = Exclude<D3WorkerCommand, { type: "init" | "update" | "setPositions" | "pause" }>
  | { type: "init"; input: LocalLayoutInput; options: Omit<D3WorkerOptions, "cachedPositions" | "fixedPositions"> & {
    cachedPositions?: D3PositionRows | null; fixedPositions?: D3PositionRows | null } }
  | ({ type: "update"; input: LocalLayoutInput; fixedPositions?: D3PositionRows } & D3LayoutUpdateOptions)
  | { type: "setPositions"; positions: D3PositionRows; pins?: readonly string[]; options?: D3ReconcileOptions }
  | { type: "pause"; positions?: D3PositionRows };
export type D3WorkerRequest = ({ sequence: number; topology: number } & WireCommand) | { type: "ack" };
export type D3WorkerResponse =
  | { type: "catalog"; topology: number; keys: string[] }
  | { type: "frame"; sequence: number; topology: number; positions: Float32Array; state: D3LayoutState }
  | { type: "settledPin"; sequence: number; topology: number; key: string; position: GraphPoint }
  | { type: "error"; sequence: number; message: string };

export type D3PositionRows = { keys: readonly string[]; positions: Float32Array };
export function packD3Positions(points: LocalLayoutPositions): D3PositionRows {
  const keys = Object.keys(points);
  const positions = new Float32Array(keys.length * 2);
  keys.forEach((key, index) => { positions[index * 2] = points[key].x; positions[index * 2 + 1] = points[key].y; });
  return { keys, positions };
}
export function unpackD3Positions(rows: D3PositionRows): LocalLayoutPositions {
  if (!(rows.positions instanceof Float32Array) || rows.positions.length !== rows.keys.length * 2
    || !rows.positions.every(Number.isFinite) || new Set(rows.keys).size !== rows.keys.length) {
    throw new Error("D3 Worker requires finite indexed coordinate rows");
  }
  return Object.fromEntries(rows.keys.map((key, index) => [key,
    { x: rows.positions[index * 2], y: rows.positions[index * 2 + 1] }]));
}

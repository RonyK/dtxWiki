// Compatibility for Context until its renderer moves to the shared Worker.
export { createD3Layout as createLocalLayout, advanceArrival } from "../shared/physics/d3Layout";
export type { LocalLayout, LocalLayoutOptions, ArrivalWatch } from "../shared/physics/d3Layout";

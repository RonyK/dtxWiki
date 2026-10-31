import type { CosmosGpuSimulation, CosmosGpuSimulationFrame } from "./cosmosGpuTypes";
import { createWorkspaceForceAtlas2Driver, type WorkspaceForceAtlas2Driver, type WorkspaceForceAtlas2DriverInput } from "./forceAtlas2Driver";
import { createWorkspaceForceAtlas2Contact, type WorkspaceForceAtlas2Contact } from "./forceAtlas2Contact";
import { createWorkspaceForceAtlas2GpuTransfer, type WorkspaceForceAtlas2GpuTransfer } from "./forceAtlas2GpuTransfer";
import { withWorkspaceForceAtlas2GlState } from "./forceAtlas2GlState";

export type WorkspaceForceAtlas2BindingInput = WorkspaceForceAtlas2DriverInput & {
  origin: readonly [number, number];
};

/**
 * One scene's numerical state, geometric contact constraint and renderer seam.
 * The SDK retains scheduling, camera, picking, drag and pause ownership. This
 * binding owns no SDK resource and does no per-frame coordinate readback.
 *
 * The contact pass makes this AUGMENTED size-aware FA2, not byte-identical
 * upstream FA2. Bare numerical parity is tested separately from constraints.
 * Actual position uploads invalidate history even for same-sized replacement;
 * color/search/selection changes and camera movement do not.
 */
export function createWorkspaceForceAtlas2Binding(
  getInput: () => WorkspaceForceAtlas2BindingInput,
  onError?: (error: unknown) => void,
): CosmosGpuSimulation {
  let driver: WorkspaceForceAtlas2Driver | null = null;
  let contact: WorkspaceForceAtlas2Contact | null = null;
  let transfer: WorkspaceForceAtlas2GpuTransfer | null = null;
  let context: WebGL2RenderingContext | null = null;
  let pins: number[] = [], destroyed = false;

  function dispose() {
    // Every ordinary call restores SDK bindings; our resources are never left
    // bound in the enclosing renderer. Destruction is safe after context loss.
    transfer?.destroy(); contact?.destroy(); driver?.destroy();
    transfer = null; contact = null; driver = null; context = null; pins = [];
  }
  function ensure(frame: CosmosGpuSimulationFrame) {
    if (destroyed) throw new Error("ForceAtlas2 scene binding was destroyed");
    if (context && context !== frame.gl) dispose();
    if (driver) {
      if (driver.nodeCount !== frame.nodeCount) throw new Error("ForceAtlas2 scene changed without invalidation");
      return;
    }
    const input = getInput();
    if (input.radii.length !== frame.nodeCount) throw new Error("ForceAtlas2 visible scene/native rows disagree");
    context = frame.gl;
    try {
      driver = createWorkspaceForceAtlas2Driver(frame.gl, input);
      contact = createWorkspaceForceAtlas2Contact(frame.gl, { radii: input.radii });
      transfer = createWorkspaceForceAtlas2GpuTransfer(frame.gl, frame.nodeCount, input.origin);
      // GPU-native positions own the committed upload, including retained pins
      // and explicit edits. A stale CPU seed never overwrites that scene.
      transfer.importNative(frame.previousPositionTexture, frame.textureSize, driver.currentTexture, driver.textureSize);
      transfer.importNative(frame.previousPositionTexture, frame.textureSize, driver.previousTexture, driver.textureSize);
    } catch (error) { dispose(); throw error; }
  }
  function synchronizePins(frame: CosmosGpuSimulationFrame) {
    const next = [...new Set([...frame.pinnedIndices, ...(frame.draggingIndex === null ? [] : [frame.draggingIndex])])]
      .sort((a, b) => a - b);
    if (next.length === pins.length && next.every((value, index) => value === pins[index])) return;
    driver!.setPins(next); contact!.setPins(next);
    const prior = new Set(pins), current = new Set(next);
    for (const index of new Set([...pins, ...next])) {
      if (prior.has(index) === current.has(index)) continue;
      // Pin changes retire that node's old adaptive impulse, not its neighbors'.
      transfer!.importNative(frame.previousPositionTexture, frame.textureSize, driver!.currentTexture, driver!.textureSize, index);
      transfer!.importNative(frame.previousPositionTexture, frame.textureSize, driver!.previousTexture, driver!.textureSize, index);
    }
    pins = next;
  }
  return {
    step(frame) {
      withWorkspaceForceAtlas2GlState(frame.gl, () => {
        ensure(frame); synchronizePins(frame);
        driver!.step();
        const corrected = contact!.project(driver!.currentTexture, driver!.textureSize);
        transfer!.adopt(corrected, contact!.textureSize, driver!.currentTexture, driver!.textureSize);
        transfer!.publish(driver!.currentTexture, driver!.textureSize, frame.positionFramebuffer,
          frame.textureSize, frame.previousPositionTexture, frame.pinsTexture, frame.draggingIndex);
      });
    },
    afterDrag(frame, index) {
      // Native drag swapped its position FBO *after* drawing. Copy its exact
      // current row back to both model histories, even when physics is paused.
      withWorkspaceForceAtlas2GlState(frame.gl, () => {
        if (!driver) return; // Initial static scene has not started physics.
        if (context !== frame.gl) throw new Error("ForceAtlas2 drag belongs to another context");
        transfer!.importNative(frame.positionTexture, frame.textureSize, driver.currentTexture, driver.textureSize, index);
        transfer!.importNative(frame.positionTexture, frame.textureSize, driver.previousTexture, driver.textureSize, index);
      });
    },
    invalidate() { dispose(); },
    onError(error) {
      if (destroyed) return;
      destroyed = true; dispose();
      if (onError) onError(error);
      else throw error;
    },
    destroy() { if (destroyed) return; destroyed = true; dispose(); },
  };
}

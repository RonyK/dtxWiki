import { createWorkspaceForceAtlas2Driver } from "../../frontend/src/features/graph/workspace/forceAtlas2Driver";

export type ForceAtlas2Fixture = {
  nodeIds: string[];
  edgeIds: string[];
  positions: number[];
  radii: number[];
  sources: number[];
  targets: number[];
  weights: number[];
  pins?: number[];
};

declare global {
  interface Window {
    __forceAtlas2Core: {
      run(input: ForceAtlas2Fixture, checkpoints: number[], exercisePause?: boolean): {
        samples: Array<{ iteration: number; positions: number[]; force: number[]; previousForce: number[]; masses: number[] }>;
        errors: number[];
        pausedUnchanged: boolean;
        destroyedRejectsStep: boolean;
      };
      invalid(input: ForceAtlas2Fixture): string | null;
    };
  }
}

function context() {
  const canvas = document.createElement("canvas");
  const gl = canvas.getContext("webgl2", { antialias: false });
  if (!gl) throw new Error("WebGL2 required for the full-model numerical gate");
  if (!gl.getExtension("EXT_color_buffer_float")) throw new Error("Float32 GPU targets required");
  return gl;
}

function driver(gl: WebGL2RenderingContext, input: ForceAtlas2Fixture) {
  return createWorkspaceForceAtlas2Driver(gl, { positions: Float32Array.from(input.positions),
    radii: Float32Array.from(input.radii), sources: Uint32Array.from(input.sources),
    targets: Uint32Array.from(input.targets), weights: Float64Array.from(input.weights), pins: input.pins });
}

window.__forceAtlas2Core = {
  run(input, checkpoints, exercisePause = false) {
    const gl = context(), simulation = driver(gl, input), errors: number[] = [];
    let pausedUnchanged = true, destroyedRejectsStep = false;
    try {
      const samples = [simulation.snapshot()];
      for (const target of checkpoints) {
        const count = target - samples.at(-1)!.iteration;
        simulation.step(count);
        samples.push(simulation.snapshot()); errors.push(gl.getError());
        if (exercisePause) {
          simulation.pause();
          simulation.step(2);
          pausedUnchanged &&= JSON.stringify(simulation.snapshot()) === JSON.stringify(samples.at(-1));
          simulation.resume();
        }
      }
      simulation.destroy(); simulation.destroy();
      try { simulation.step(); } catch { destroyedRejectsStep = true; }
      errors.push(gl.getError());
      return { samples: samples.map(sample => ({ iteration: sample.iteration,
        positions: Array.from(sample.positions), force: Array.from(sample.force),
        previousForce: Array.from(sample.previousForce), masses: Array.from(sample.masses) })),
        errors, pausedUnchanged, destroyedRejectsStep };
    } finally { simulation.destroy(); gl.getExtension("WEBGL_lose_context")?.loseContext(); }
  },
  invalid(input) {
    const gl = context();
    try { const simulation = driver(gl, input); simulation.destroy(); return null; }
    catch (error) { return error instanceof Error ? error.message : String(error); }
    finally { gl.getExtension("WEBGL_lose_context")?.loseContext(); }
  },
};

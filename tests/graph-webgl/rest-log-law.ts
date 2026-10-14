import { RestLogGraph as Graph } from "../../frontend/src/features/graph/workspace/cosmosRestLog";

const CASES = [
  { name: "compression", d: 5, L: 10, S: 1, pin: false },
  { name: "rest", d: 10, L: 10, S: 1, pin: false },
  { name: "extension20", d: 20, L: 10, S: 1, pin: false },
  { name: "extension40", d: 40, L: 10, S: 1, pin: false },
  { name: "sqrt-strength", d: 40, L: 10, S: 4, pin: false },
  { name: "scale-covariance", d: 80, L: 20, S: 1, pin: false },
  { name: "pinned-source", d: 40, L: 10, S: 1, pin: true },
] as const;
const CONFIG_FIELDS = ["spaceSize", "rescalePositions", "enableSimulation", "simulationGravity", "simulationCenter",
  "simulationRepulsion", "simulationLinkSpring", "simulationLinkDistance", "simulationLinkDistRandomVariationRange",
  "simulationCluster", "simulationCollision", "simulationFriction", "simulationRepulsionFromMouse",
  "enableRightClickRepulsion", "transitionDuration"] as const;
const TOLERANCE = .001; // Float32 coordinates around2000, not a force-law tolerance sweep.

async function collect(inputCase: typeof CASES[number]) {
  const host = document.createElement("div");
  host.style.cssText = "width:600px;height:400px;position:relative";
  document.body.append(host);
  const alphas: number[] = [];
  const native = new Graph(host, {
    enableSimulation: false, spaceSize: 4096, rescalePositions: false,
    fitViewOnInit: false, transitionDuration: 0, enableZoom: false, enableDrag: false,
    simulationGravity: 0, simulationCenter: 0, simulationRepulsion: 0,
    simulationCluster: 0, simulationCollision: 0, simulationRepulsionFromMouse: 0,
    enableRightClickRepulsion: false, simulationFriction: 1,
    simulationLinkSpring: 1, simulationLinkDistance: inputCase.L,
    simulationLinkDistRandomVariationRange: [1, 1],
    onSimulationTick(alpha) { alphas.push(alpha); },
  });
  try {
    await native.ready;
    const input = Float32Array.from([2000, 2000, 2000 + inputCase.d, 2000]);
    const links = Float32Array.from([0, 1]), strength = Float32Array.from([inputCase.S]);
    const pins = inputCase.pin ? [0] : [];
    native.setPointPositions(input); native.setPointSizes(Float32Array.from([3, 3]));
    native.setLinks(links); native.setLinkStrength(strength); native.setPinnedPoints(pins);
    native.render(undefined, 0);
    const ticksBefore = alphas.length;
    // Public API only. stop resets alpha; render(1,0) restores it without
    // rearming live motion. No await or RAF can introduce an automatic tick.
    native.setConfigPartial({ enableSimulation: true });
    native.stop(); native.render(1, 0);
    const before = Array.from(native.getPointPositions());
    const runningBefore = native.isSimulationRunning;
    const gl = host.querySelector("canvas")?.getContext("webgl2");
    if (!gl) throw new Error(`${inputCase.name}: WebGL2 unavailable`);
    const glBefore = gl.getError();
    native.step();
    const after = Array.from(native.getPointPositions());
    const runningAfter = native.isSimulationRunning;
    const glAfter = gl.getError();
    const config = Object.fromEntries(CONFIG_FIELDS.map(key => [key, native.config[key]]));
    native.stop();
    return { ...inputCase, input: Array.from(input), before, after, ticksBefore,
      ticks: alphas.length, alphas: [...alphas], runningBefore, runningAfter,
      gl: [glBefore, glAfter], maxTextureSize: gl.getParameter(gl.MAX_TEXTURE_SIZE), config,
      pointIds: ["source", "target"], edgeIds: ["authored-source-target"],
      endpointIds: [["source", "target"]], links: Array.from(links), strength: Array.from(strength), pins };
  } finally { native.destroy(); host.remove(); }
}

// Independent desired law; never imported from or injected into the shader.
function displacement(d: number, L: number, S: number): number {
  if (d > L) return .5 * Math.sqrt(S) * L * Math.log1p((d - L) / L);
  const clamped = Math.max(d, .99 * L);
  return .5 * Math.sqrt(S) * d * (clamped - L) / clamped;
}

export async function runRestLogLawProbe() {
  const cases: Awaited<ReturnType<typeof collect>>[] = [];
  for (const inputCase of CASES) cases.push(await collect(inputCase));
  // Preserve every raw readback before assertions can report a bad extension.
  const failures: string[] = [];
  const checks = cases.map(sample => {
    const source = sample.pin ? 0 : displacement(sample.d, sample.L, sample.S);
    // SDK incoming/source pass integrates first; outgoing/target pass sees
    // that updated source. A pinned source is unchanged, not mass-reweighted.
    const target = displacement(sample.d - source, sample.L, sample.S);
    const expected = [2000 + source, 2000, 2000 + sample.d - target, 2000];
    if (sample.ticksBefore !== 0 || sample.ticks !== 1) failures.push(`${sample.name}: not exactly one manual tick`);
    if (sample.runningBefore || sample.runningAfter) failures.push(`${sample.name}: automatic simulation armed`);
    if (sample.alphas.length !== 1 || !Number.isFinite(sample.alphas[0]) || sample.alphas[0] <= 0) failures.push(`${sample.name}: inactive alpha`);
    if (sample.gl.some(value => value !== 0)) failures.push(`${sample.name}: GL errors ${sample.gl.join(",")}`);
    if (sample.before.some((value, index) => value !== sample.input[index]) || sample.before.length !== 4) failures.push(`${sample.name}: initial input changed`);
    if (sample.after.length !== 4 || sample.after.some(value => !Number.isFinite(value))) failures.push(`${sample.name}: invalid GPU positions`);
    const errors = expected.map((value, index) => Math.abs(sample.after[index] - value));
    if (errors.some(value => !Number.isFinite(value) || value > TOLERANCE)) failures.push(`${sample.name}: law errors ${errors.join(",")}`);
    if (sample.pin && (sample.after[0] !== 2000 || sample.after[1] !== 2000)) failures.push(`${sample.name}: source pin moved`);
    return { name: sample.name, sourceDisplacement: source, targetDisplacement: target, expected, errors };
  });
  return { scope: "SDK-only seven-case actualGPU one-step relation-law oracle; not live2K UX/performance acceptance",
    initialAlpha: 1, tolerance: TOLERANCE, passed: failures.length === 0, failures, cases, checks };
}

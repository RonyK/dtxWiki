/** Borrowed native Cosmos resources. The simulation must not delete them. */
export interface CosmosGpuSimulationFrame {
  readonly gl: WebGL2RenderingContext;
  /** Native RGBA32F rows contain [rendererX, rendererY, nodeIndex, 0]. */
  readonly positionTexture: WebGLTexture;
  readonly previousPositionTexture: WebGLTexture;
  readonly positionFramebuffer: WebGLFramebuffer;
  /** Native RGBA32F pin flags occupy the red channel. */
  readonly pinsTexture: WebGLTexture;
  readonly textureSize: Readonly<{ width: number; height: number }>;
  readonly nodeCount: number;
  readonly pinnedIndices: readonly number[];
  readonly draggingIndex: number | null;
}

/** Owns only its simulation resources, never the shared context or renderer. */
export interface CosmosGpuSimulation {
  /** After the native swap, previousPositionTexture holds the freshest input. */
  step(frame: CosmosGpuSimulationFrame): void;
  /** Import the native drag result without CPU position readback. */
  afterDrag(frame: CosmosGpuSimulationFrame, index: number): void;
  /** Actual position upload invalidated integration history/geometry identity. */
  invalidate(): void;
  /** Report one failed attachment after rollback/pause; absent means rethrow. */
  onError?(error: unknown): void;
  destroy(): void;
}

export interface CosmosGpuSimulationHost {
  /** Replacing/detaching destroys the previous simulation exactly once. */
  attachGpuSimulation(simulation: CosmosGpuSimulation | null): void;
  /** Null until native resources exist, and after destruction/context loss. */
  getGpuSimulationFrame(): CosmosGpuSimulationFrame | null;
}

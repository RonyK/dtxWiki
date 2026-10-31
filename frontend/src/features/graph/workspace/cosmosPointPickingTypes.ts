/** Borrowed, same-frame native drawing inputs; a picker must never delete them. */
export interface CosmosPointPickFrame {
  readonly gl: WebGL2RenderingContext;
  readonly positionTexture: WebGLTexture;
  readonly pointStatusTexture: WebGLTexture;
  readonly exitTexture: WebGLTexture;
  readonly textureSize: Readonly<{ width: number; height: number }>;
  readonly nodeCount: number;
  readonly transformationMatrix: readonly number[];
  readonly screenSize: readonly [number, number];
  /** CSS pixels, bottom-left origin, exactly as in native screenMousePosition. */
  readonly pointer: readonly [number, number];
  readonly spaceSize: number;
  readonly pointSizeScale: number;
  readonly scalePointsOnZoom: boolean;
  readonly maxPointSize: number;
  readonly pixelRatio: number;
  readonly pointOpacity: number;
  readonly pointGreyoutOpacity: number | null;
}

export interface CosmosPointPickResult {
  readonly index: number;
  readonly position: [number, number];
}

/** Optional point-query owner. The renderer retains callbacks, scheduling and drag. */
export interface CosmosPointPicker {
  readonly hasPending: boolean;
  pickSync(frame: CosmosPointPickFrame | null): CosmosPointPickResult | null;
  request(frame: CosmosPointPickFrame | null): boolean;
  /**
   * Undefined means pending/obsolete; null is a completed query with no point. The frame carries the pointer
   * now: a point found for an earlier pointer is served only while it still targets this one, and at a bounded
   * rate (an answer skipped for that is dropped unread, so the caller asks again).
   */
  take(frame: CosmosPointPickFrame | null): CosmosPointPickResult | null | undefined;
  discard(): void;
  destroy(): void;
}

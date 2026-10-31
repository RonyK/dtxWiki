type NativeCamera = { x: number; y: number; k: number };

function finiteCamera(value: unknown): value is NativeCamera {
  if (typeof value !== "object" || value === null) return false;
  const camera = value as Partial<NativeCamera>;
  return typeof camera.x === "number" && Number.isFinite(camera.x)
    && typeof camera.y === "number" && Number.isFinite(camera.y)
    && typeof camera.k === "number" && Number.isFinite(camera.k) && camera.k > 0;
}

/** Same zoom5 / CSS pan2 precision as the accepted 2K palette-remount gate.
 * A new renderer roundtrips the canonical camera center through Float32;
 * this tolerance is NOT used for same-renderer Pause/Resume assertions. */
export function sameWorkspaceRemountCamera(before: unknown, after: unknown): boolean {
  return finiteCamera(before) && finiteCamera(after)
    && Math.abs(before.k - after.k) < .5 * 10 ** -5
    && Math.abs(before.x - after.x) < .5 * 10 ** -2
    && Math.abs(before.y - after.y) < .5 * 10 ** -2;
}

type RailResizeMode = "widen" | "reset";

type RailResizeIconProps = {
  mode: RailResizeMode;
  size?: number;
  className?: string;
};

const DASH = "2.4 2";

/**
 * Width-control glyph for rail / side-panel resize buttons (방식 A): a panel
 * framed by a second rectangle drawn as a DASHED outline, plus a directional
 * arrow. The dashed rectangle marks the *other* width state:
 * - `widen` → the outer box (the area the rail will grow into) is dashed, the
 *   current narrow panel is solid, and the arrow points outward.
 * - `reset` → the outer box (the current wide extent) is solid, the original
 *   narrow panel is dashed, and the arrow points back inward.
 * The framed/dashed treatment distinguishes this width control from the solid
 * framed-box `PanelVisibilityIcon` used for show/hide, so the two button groups
 * never look alike. Matches the lucide render contract so it sits next to lucide
 * icons.
 */
export function RailResizeIcon({
  mode,
  size = 14,
  className,
}: RailResizeIconProps) {
  const widen = mode === "widen";
  return (
    <svg
      aria-hidden="true"
      className={className}
      fill="none"
      height={size}
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth={2}
      viewBox="0 0 24 24"
      width={size}
      xmlns="http://www.w3.org/2000/svg"
    >
      {/* outer box = target/extent: dashed when widening (area grown into) */}
      <rect
        x="2"
        y="5"
        width="20"
        height="14"
        rx="2"
        strokeDasharray={widen ? DASH : undefined}
      />
      {/* inner left panel = the rail: dashed when resetting (original narrow) */}
      <rect
        x="2"
        y="5"
        width="7"
        height="14"
        rx="2"
        strokeDasharray={widen ? undefined : DASH}
      />
      {widen ? <path d="m13 9 3 3-3 3" /> : <path d="m16 9-3 3 3 3" />}
    </svg>
  );
}

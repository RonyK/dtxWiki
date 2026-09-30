type PanelSide = "left" | "right" | "bottom";
type PanelVisibilityAction = "show" | "hide";

type PanelVisibilityIconProps = {
  side: PanelSide;
  action: PanelVisibilityAction;
  size?: number;
  className?: string;
};

const DIVIDER: Record<PanelSide, string> = {
  left: "M9 3v18",
  right: "M15 3v18",
  bottom: "M3 15h18",
};
const SHAFT: Record<PanelSide, string> = {
  left: "M11 12h6",
  right: "M7 12h6",
  bottom: "M12 7v6",
};
// Arrowhead points AWAY from the panel divider for "show" (open) and TOWARD it
// for "hide" (collapse). Keyed by side then action.
const ARROW: Record<PanelSide, Record<PanelVisibilityAction, string>> = {
  left: { show: "m14 9 3 3-3 3", hide: "m14 9-3 3 3 3" },
  right: { show: "m10 9-3 3 3 3", hide: "m10 9 3 3-3 3" },
  bottom: { show: "m9 10 3-3 3 3", hide: "m9 12 3 3 3-3" },
};

/**
 * Visibility-control glyph for rail, side-panel, and bottom-sheet controls: a
 * panel BOX (frame + a divider on `side`) plus a directional arrow. `show`
 * points the arrow away from the panel edge (open); `hide` points it toward the
 * edge (collapse). The framed box distinguishes show/hide from the bar-style
 * `RailResizeIcon` used for width. Matches the lucide render contract.
 */
export function PanelVisibilityIcon({
  side,
  action,
  size = 14,
  className,
}: PanelVisibilityIconProps) {
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
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <path d={DIVIDER[side]} />
      <path d={SHAFT[side]} />
      <path d={ARROW[side][action]} />
    </svg>
  );
}

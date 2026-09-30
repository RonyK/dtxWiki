import { AlertCircle, CheckCircle2, Clock3 } from "lucide-react";

import type { ActivityLane } from "../api/operations";

/**
 * The lane glyph for the Pending / Advisory / History trio. Dashboard and
 * Activity render the same three lanes, so they must not drift into different
 * glyphs or sizes: 16px matches every other `.metric-card-title` icon.
 */
export function iconForActivityLane(lane: ActivityLane) {
  if (lane === "pending") {
    return <AlertCircle size={16} aria-hidden="true" />;
  }
  if (lane === "advisory") {
    return <Clock3 size={16} aria-hidden="true" />;
  }
  return <CheckCircle2 size={16} aria-hidden="true" />;
}

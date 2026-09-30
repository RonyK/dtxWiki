import { useCallback, type ForwardedRef } from "react";

import { assignRef } from "./assignRef";

export function useExplicitInertRef<T extends HTMLElement>(
  forwardedRef: ForwardedRef<T>,
  inertActive: boolean,
) {
  return useCallback(
    (node: T | null) => {
      assignRef(forwardedRef, node);
      if (!node) {
        return;
      }
      if (inertActive) {
        node.setAttribute("inert", "");
        node.inert = true;
      } else {
        node.removeAttribute("inert");
        node.inert = false;
      }
    },
    [forwardedRef, inertActive],
  );
}

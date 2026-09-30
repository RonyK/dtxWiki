import type { ForwardedRef } from "react";

/**
 * Write a node into whichever kind of ref React handed us.
 *
 * Shared rather than copied: `useExplicitInertRef` and `useScrollLeftGuardRef`
 * both need it, and they are composed with one another (Shell.tsx passes one
 * into the other), so a divergence between two copies would surface as a ref
 * that silently stops being forwarded -- no type error, no test failure, just
 * an element that quietly stops being marked `inert`. Anything that changes
 * ref-assignment semantics (React 19's ref cleanup functions, say) has one
 * place to change.
 */
export function assignRef<T>(ref: ForwardedRef<T>, value: T | null) {
  if (typeof ref === "function") {
    ref(value);
    return;
  }
  if (ref) {
    ref.current = value;
  }
}

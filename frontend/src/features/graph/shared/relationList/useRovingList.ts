import { useEffect, useLayoutEffect, useRef, useState, type FocusEvent, type KeyboardEvent, type MouseEvent } from "react";

export type RovingSelectionOrigin = {
  control: HTMLButtonElement;
  modality: "keyboard" | "pointer";
};

type RovingListOptions = {
  /** The list is active and shown; otherwise it offers no Tab entry. */
  enabled: boolean;
  /** Row keys in display order. A row key is unique even when two rows select the same item. */
  ids: readonly string[];
  /** Callbacks receive the row's target (what it selects and highlights), then its key. */
  onBlur?: (target: string, id: string) => void;
  onFocus?: (target: string, id: string) => void;
  onSelect: (target: string, origin: RovingSelectionOrigin, id: string) => void;
  /** A new scope (workspace, selection) forgets the remembered row. */
  scopeKey: string;
};

/**
 * One Tab entry per list: arrows, Home and End move between rows, Enter or
 * Space selects. Moving never selects. A row that disappears while it owns
 * focus reports its blur so the caller can clear a transient highlight.
 */
export function useRovingList({ enabled, ids, onBlur, onFocus, onSelect, scopeKey }: RovingListOptions) {
  const buttonsRef = useRef(new Map<string, HTMLButtonElement>());
  // Callers may pass new closures on every render; the effects below must not
  // treat that as an unmount, or a re-render would clear the focused row's highlight.
  const callbacksRef = useRef({ onBlur, onFocus, onSelect });
  useLayoutEffect(() => {
    callbacksRef.current = { onBlur, onFocus, onSelect };
  });
  // The target is remembered with the focus: a row that disappears can still clear its highlight.
  const ownedFocusRef = useRef<{ button: HTMLButtonElement; id: string; target: string } | null>(null);
  const [navigation, setNavigation] = useState<{ id: string | null; scope: string }>({
    id: null, scope: scopeKey,
  });
  // Reconcile before paint, not after an effect: removed IDs never leave a
  // stale Tab stop and a metadata-only update never creates focus.
  const entryId = enabled
    ? navigation.scope === scopeKey && navigation.id && ids.includes(navigation.id)
      ? navigation.id
      : ids[0] ?? null
    : null;

  useLayoutEffect(() => {
    if (navigation.scope !== scopeKey || (navigation.id !== null
      && (!enabled || !ids.includes(navigation.id)))) {
      setNavigation({ id: null, scope: scopeKey });
    }
    const owned = ownedFocusRef.current;
    if (!owned || (enabled && navigation.scope === scopeKey && ids.includes(owned.id))) return;
    ownedFocusRef.current = null;
    const ownsDomFocus = document.activeElement === owned.button;
    if (ownsDomFocus || (!owned.button.isConnected && document.activeElement === document.body)) {
      // The caller also checks the original ID before clearing its highlight.
      callbacksRef.current.onBlur?.(owned.target, owned.id);
      if (ownsDomFocus) owned.button.blur();
    }
  }, [enabled, ids, navigation.id, navigation.scope, scopeKey]);

  useEffect(() => () => {
    const owned = ownedFocusRef.current;
    ownedFocusRef.current = null;
    if (owned && (document.activeElement === owned.button
      || (!owned.button.isConnected && document.activeElement === document.body))) {
      callbacksRef.current.onBlur?.(owned.target, owned.id);
    }
  }, []);

  function canInteract(button: HTMLButtonElement, id: string) {
    return enabled && ids.includes(id) && button.isConnected && !button.closest("[hidden], details:not([open])");
  }

  return function rowProps(id: string, index: number, target: string = id) {
    return {
      onBlur: (event: FocusEvent<HTMLButtonElement>) => {
        if (ownedFocusRef.current?.button !== event.currentTarget) return;
        ownedFocusRef.current = null;
        callbacksRef.current.onBlur?.(target, id);
      },
      onClick: (event: MouseEvent<HTMLButtonElement>) => {
        if (!canInteract(event.currentTarget, id)) return;
        callbacksRef.current.onSelect(target, {
          control: event.currentTarget,
          modality: event.detail === 0 && document.activeElement === event.currentTarget ? "keyboard" : "pointer",
        }, id);
      },
      onFocus: (event: FocusEvent<HTMLButtonElement>) => {
        if (!canInteract(event.currentTarget, id)) return;
        ownedFocusRef.current = { button: event.currentTarget, id, target };
        setNavigation({ id, scope: scopeKey });
        callbacksRef.current.onFocus?.(target, id);
      },
      onKeyDown: (event: KeyboardEvent<HTMLButtonElement>) => {
        if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey
          || !canInteract(event.currentTarget, id)) return;
        const nextIndex = event.key === "Home" ? 0
          : event.key === "End" ? ids.length - 1
          : event.key === "ArrowUp" ? Math.max(0, index - 1)
          : event.key === "ArrowDown" ? Math.min(ids.length - 1, index + 1)
          : null;
        if (nextIndex === null) return;
        event.preventDefault();
        const nextId = ids[nextIndex];
        const nextButton = buttonsRef.current.get(nextId);
        if (nextButton && canInteract(nextButton, nextId)) nextButton.focus();
      },
      ref: (button: HTMLButtonElement | null) => {
        if (button) buttonsRef.current.set(id, button);
        else buttonsRef.current.delete(id);
      },
      tabIndex: entryId === id ? 0 : -1,
    };
  };
}

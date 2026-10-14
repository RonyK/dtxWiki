import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";

export type LocalGraphSelectionOrigin = {
  control: HTMLButtonElement;
  modality: "keyboard" | "pointer";
};

type LocalGraphListProps<T extends { id: string }> = {
  active: boolean;
  className?: string;
  defaultOpen?: boolean;
  description?: string;
  empty: string;
  items: readonly T[];
  label: string;
  onBlur: (id: string) => void;
  onFocus: (id: string) => void;
  onSelect: (id: string, origin: LocalGraphSelectionOrigin) => void;
  renderItem: (item: T) => ReactNode;
  scopeKey: string;
  selectedId: string | null;
};

/** A bounded Local list: native semantics, one entry, no selection on movement. */
export function LocalGraphList<T extends { id: string }>({
  active,
  className,
  defaultOpen = false,
  description,
  empty,
  items,
  label,
  onBlur,
  onFocus,
  onSelect,
  renderItem,
  scopeKey,
  selectedId,
}: LocalGraphListProps<T>) {
  const descriptionId = useId();
  const disclosureRef = useRef<HTMLDetailsElement>(null);
  const buttonsRef = useRef(new Map<string, HTMLButtonElement>());
  const ownedFocusRef = useRef<{ button: HTMLButtonElement; id: string } | null>(null);
  const [open, setOpen] = useState(defaultOpen);
  const [navigation, setNavigation] = useState<{ id: string | null; scope: string }>({
    id: null, scope: scopeKey,
  });
  const ids = items.map((item) => item.id);
  const enabled = active && open;
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
      // The parent also checks the original ID before clearing its highlight.
      onBlur(owned.id);
      if (ownsDomFocus) owned.button.blur();
    }
  }, [enabled, ids, navigation.id, navigation.scope, onBlur, scopeKey]);

  useEffect(() => () => {
    const owned = ownedFocusRef.current;
    ownedFocusRef.current = null;
    if (owned && (document.activeElement === owned.button
      || (!owned.button.isConnected && document.activeElement === document.body))) {
      onBlur(owned.id);
    }
  }, [onBlur]);

  function canInteract(button: HTMLButtonElement, id: string) {
    return active && disclosureRef.current?.open && ids.includes(id)
      && button.isConnected && !button.closest("[hidden], details:not([open])");
  }

  return <details
    className={className}
    onToggle={(event) => {
      const nextOpen = event.currentTarget.open;
      setOpen(nextOpen);
      if (!nextOpen) setNavigation({ id: null, scope: scopeKey });
    }}
    open={open}
    ref={disclosureRef}
  >
    <summary>{label} ({items.length})</summary>
    {description && items.length > 0 ? <p id={descriptionId}>{description}</p> : null}
    <ul
      aria-describedby={description && items.length > 0 ? descriptionId : undefined}
      aria-label={label}
      className="graph-local-list"
    >
      {items.map((item, index) => <li key={item.id}>
        <button
          aria-pressed={selectedId === item.id}
          className="graph-list-select"
          data-local-graph-id={item.id}
          onBlur={(event) => {
            if (ownedFocusRef.current?.button !== event.currentTarget) return;
            ownedFocusRef.current = null;
            onBlur(item.id);
          }}
          onClick={(event) => {
            if (!canInteract(event.currentTarget, item.id)) return;
            onSelect(item.id, {
              control: event.currentTarget,
              modality: event.detail === 0 && document.activeElement === event.currentTarget
                ? "keyboard" : "pointer",
            });
          }}
          onFocus={(event) => {
            if (!canInteract(event.currentTarget, item.id)) return;
            ownedFocusRef.current = { button: event.currentTarget, id: item.id };
            setNavigation({ id: item.id, scope: scopeKey });
            onFocus(item.id);
          }}
          onKeyDown={(event) => {
            if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey
              || !canInteract(event.currentTarget, item.id)) return;
            const nextIndex = event.key === "Home" ? 0
              : event.key === "End" ? items.length - 1
              : event.key === "ArrowUp" ? Math.max(0, index - 1)
              : event.key === "ArrowDown" ? Math.min(items.length - 1, index + 1)
              : null;
            if (nextIndex === null) return;
            event.preventDefault();
            const nextId = ids[nextIndex];
            const nextButton = buttonsRef.current.get(nextId);
            if (nextButton && canInteract(nextButton, nextId)) nextButton.focus();
          }}
          ref={(button) => {
            if (button) buttonsRef.current.set(item.id, button);
            else buttonsRef.current.delete(item.id);
          }}
          tabIndex={entryId === item.id ? 0 : -1}
          type="button"
        >{renderItem(item)}</button>
      </li>)}
    </ul>
    {items.length === 0 ? <p>{empty}</p> : null}
  </details>;
}

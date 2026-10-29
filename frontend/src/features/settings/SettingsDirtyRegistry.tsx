import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  type ReactNode,
} from "react";

/**
 * Knows whether any Settings pane is holding unsaved edits.
 *
 * Switching section unmounts the pane and drops its draft without asking --
 * there is no guard anywhere in Settings. That was survivable while each
 * policy had its own destination, because leaving one was an obvious
 * navigation. Folding five of them into tabs makes discarding a draft look
 * like flipping a lightweight toggle, so the guard stops being optional.
 *
 * Panes register a GETTER, not a value: a keystroke updates a ref and
 * re-renders nothing. Unmounting unregisters, so a pane that goes away can
 * never leave a stale dirty flag behind -- the failure mode a lifted-state
 * design would have had.
 *
 * `useBlocker` would cover cross-screen navigation too, but it requires a data
 * router: it calls `useDataRouterContext`, which throws under the plain
 * `<MemoryRouter>` that all 89 Settings render sites use. Guarding section and
 * tab changes needs no router at all, and `beforeunload` covers the tab close.
 */
type DirtyRegistry = {
  register: (id: string, isDirty: () => boolean) => () => void;
  anyDirty: () => boolean;
};

const SettingsDirtyContext = createContext<DirtyRegistry | null>(null);

export function SettingsDirtyProvider({ children }: { children: ReactNode }) {
  const entries = useRef(new Map<string, () => boolean>());

  const registry = useMemo<DirtyRegistry>(
    () => ({
      register(id, isDirty) {
        entries.current.set(id, isDirty);
        return () => {
          entries.current.delete(id);
        };
      },
      anyDirty() {
        for (const isDirty of entries.current.values()) {
          if (isDirty()) {
            return true;
          }
        }
        return false;
      },
    }),
    [],
  );

  useEffect(() => {
    function onBeforeUnload(event: BeforeUnloadEvent) {
      if (!registry.anyDirty()) {
        return;
      }
      event.preventDefault();
      event.returnValue = "";
    }
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [registry]);

  return (
    <SettingsDirtyContext.Provider value={registry}>
      {children}
    </SettingsDirtyContext.Provider>
  );
}

/**
 * Declare this pane's unsaved state.
 *
 * `dirty` goes into a ref, so calling this on every keystroke costs nothing.
 */
export function useRegisterSettingsDirty(id: string, dirty: boolean) {
  const registry = useContext(SettingsDirtyContext);
  const dirtyRef = useRef(dirty);

  // Not `dirtyRef.current = dirty` inline: writing a ref during render is
  // exactly the concurrent-rendering footgun refs are meant to avoid -- a
  // render can be thrown away or replayed without committing, which would
  // leave this ref pointing at a value the screen never actually showed.
  // An effect only runs after a render actually commits.
  useEffect(() => {
    dirtyRef.current = dirty;
  }, [dirty]);

  useEffect(() => {
    if (!registry) {
      return undefined;
    }
    return registry.register(id, () => dirtyRef.current);
  }, [id, registry]);
}

/** A background read must not replace the baseline of an unsaved policy form. */
export function useSettingsDirtyCheck() {
  const registry = useContext(SettingsDirtyContext);
  return useCallback(() => registry?.anyDirty() ?? false, [registry]);
}

/**
 * Run `change` unless a pane is holding edits and the reader declines.
 *
 * `window.confirm` rather than a bespoke dialog: it is what the rest of the
 * app already uses for exactly this (PageEditor, AgentScreen, ContextScreen,
 * LibraryScreen), and a guard that only appears in Settings would be the
 * odd one out.
 */
export function useGuardedSettingsChange(message: string) {
  const registry = useContext(SettingsDirtyContext);
  return useCallback(
    (change: () => void) => {
      if (registry?.anyDirty() && !window.confirm(message)) {
        return;
      }
      change();
    },
    [message, registry],
  );
}

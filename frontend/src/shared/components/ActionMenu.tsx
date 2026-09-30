import { MoreHorizontal } from "lucide-react";
import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from "react";

export type ActionMenuItem = {
  key: string;
  label: string;
  icon?: ReactNode;
  onSelect: () => Promise<void> | void;
  disabled?: boolean;
  tone?: "default" | "danger";
};

type ActionMenuProps = {
  label: string;
  items: ActionMenuItem[];
  disabled?: boolean;
};

function enabledMenuItems(container: HTMLElement | null) {
  if (!container) return [];
  return Array.from(
    container.querySelectorAll<HTMLButtonElement>(
      '[role="menuitem"]:not([disabled])',
    ),
  );
}

/** Accessible, reusable menu-button for secondary row/card actions. */
export function ActionMenu({ label, items, disabled = false }: ActionMenuProps) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const menuId = useId();

  const close = useCallback((restoreFocus: boolean) => {
    setOpen(false);
    if (restoreFocus) triggerRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!open) return undefined;
    const [first] = enabledMenuItems(menuRef.current);
    first?.focus();

    function onPointerDown(event: PointerEvent) {
      const target = event.target as Node;
      if (
        !menuRef.current?.contains(target) &&
        !triggerRef.current?.contains(target)
      ) {
        close(false);
      }
    }
    function onDocumentKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        close(true);
      }
    }
    document.addEventListener("pointerdown", onPointerDown, true);
    document.addEventListener("keydown", onDocumentKeyDown, true);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown, true);
      document.removeEventListener("keydown", onDocumentKeyDown, true);
    };
  }, [close, open]);

  useEffect(() => {
    if (disabled && open) close(false);
  }, [close, disabled, open]);

  function onMenuKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    const menuItems = enabledMenuItems(menuRef.current);
    if (menuItems.length === 0) return;
    const current = menuItems.indexOf(
      document.activeElement as HTMLButtonElement,
    );
    if (event.key === "ArrowDown") {
      event.preventDefault();
      menuItems[(current + 1 + menuItems.length) % menuItems.length]?.focus();
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      menuItems[(current - 1 + menuItems.length) % menuItems.length]?.focus();
    } else if (event.key === "Home") {
      event.preventDefault();
      menuItems[0]?.focus();
    } else if (event.key === "End") {
      event.preventDefault();
      menuItems[menuItems.length - 1]?.focus();
    } else if (event.key === "Tab") {
      close(false);
    }
  }

  if (items.length === 0) return null;

  return (
    <div className="action-menu">
      <button
        aria-controls={open ? menuId : undefined}
        aria-expanded={open}
        aria-haspopup="menu"
        aria-label={label}
        className="icon-button action-menu__trigger"
        disabled={disabled}
        onClick={() => setOpen((value) => !value)}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            setOpen(true);
          }
        }}
        ref={triggerRef}
        title={label}
        type="button"
      >
        <MoreHorizontal size={18} aria-hidden="true" />
      </button>
      {open ? (
        <div
          aria-label={label}
          className="action-menu__panel"
          id={menuId}
          onKeyDown={onMenuKeyDown}
          ref={menuRef}
          role="menu"
          tabIndex={-1}
        >
          {items.map((item) => (
            <button
              className={`action-menu__item${item.tone === "danger" ? " is-danger" : ""}`}
              disabled={item.disabled}
              key={item.key}
              onClick={() => {
                close(true);
                void item.onSelect();
              }}
              role="menuitem"
              type="button"
            >
              {item.icon}
              <span>{item.label}</span>
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

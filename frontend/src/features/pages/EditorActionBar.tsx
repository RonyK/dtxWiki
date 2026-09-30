import {
  Fragment,
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import {
  Code2,
  Image,
  Link2,
  List,
  ListChecks,
  Paperclip,
  Quote,
  Redo2,
  Table2,
  Undo2,
} from "lucide-react";

import {
  EDITOR_COMMANDS,
  type EditorCommand,
  type EditorCommandId,
  type EditorCommandState,
  type EditorMode,
} from "./editorCommands";

const MORE_ITEM_ID = "__editor-more__";

const SCREEN_READER_ONLY_STYLE = {
  border: 0,
  clip: "rect(0 0 0 0)",
  clipPath: "inset(50%)",
  height: "1px",
  margin: "-1px",
  overflow: "hidden",
  padding: 0,
  position: "absolute",
  whiteSpace: "nowrap",
  width: "1px",
} as const;

export type EditorActionBarLabels = {
  more: string;
  moreShort: string;
  moreMenu: string;
  readingDisabledReason: string;
  toolbar: string;
};

const DEFAULT_LABELS: EditorActionBarLabels = {
  more: "More editor actions",
  moreShort: "More",
  moreMenu: "More editor actions",
  readingDisabledReason: "Editing commands are unavailable in Reading mode.",
  toolbar: "Editor actions",
};

function semanticCommandContent(
  command: EditorCommand,
  label: string,
  inMenu: boolean,
) {
  const iconProps = { "aria-hidden": true, size: 16 } as const;
  const icons: Partial<Record<EditorCommandId, ReactNode>> = {
    undo: <Undo2 {...iconProps} />,
    redo: <Redo2 {...iconProps} />,
    "inline-code": <Code2 {...iconProps} />,
    link: <Link2 {...iconProps} />,
    image: <Image {...iconProps} />,
    attach: <Paperclip {...iconProps} />,
    "bulleted-list": <List {...iconProps} />,
    "task-list": <ListChecks {...iconProps} />,
    quote: <Quote {...iconProps} />,
    table: <Table2 {...iconProps} />,
  };
  const glyphs: Partial<Record<EditorCommandId, ReactNode>> = {
    heading: <span className="editor-command-glyph is-heading">H</span>,
    bold: <span className="editor-command-glyph is-bold">B</span>,
    italic: <span className="editor-command-glyph is-italic">I</span>,
    strikethrough: <span className="editor-command-glyph is-strikethrough">S</span>,
  };
  const visual = glyphs[command.id] ?? icons[command.id];
  const showLabel = inMenu || command.id === "attach" || !visual;

  return (
    <span className="editor-command-content">
      {visual}
      {showLabel ? <span className="editor-command-label">{label}</span> : null}
    </span>
  );
}

export type EditorActionBarProps = {
  commandStates?: Partial<Record<EditorCommandId, EditorCommandState>>;
  commands?: readonly EditorCommand[];
  disabledReasons?: Partial<Record<EditorCommandId, string | null>>;
  isMobile?: boolean;
  labels?: Partial<EditorActionBarLabels>;
  mode: EditorMode;
  onCommand: (commandId: EditorCommandId, command: EditorCommand) => void;
  onPreserveSelection?: () => void;
  renderCommand?: (command: EditorCommand, label: string) => ReactNode;
  resolveLabel?: (labelKey: string, fallbackLabel: string) => string;
};

export function EditorActionBar({
  commandStates = {},
  commands = EDITOR_COMMANDS,
  disabledReasons = {},
  isMobile = false,
  labels: labelOverrides,
  mode,
  onCommand,
  onPreserveSelection,
  renderCommand,
  resolveLabel = (_labelKey, fallbackLabel) => fallbackLabel,
}: EditorActionBarProps) {
  const labels = { ...DEFAULT_LABELS, ...labelOverrides };
  const instanceId = useId().replace(/:/g, "");
  const rootRef = useRef<HTMLDivElement | null>(null);
  const moreButtonRef = useRef<HTMLButtonElement | null>(null);
  const moreMenuRef = useRef<HTMLDivElement | null>(null);
  const topLevelRefs = useRef(new Map<string, HTMLButtonElement>());
  const menuItemRefs = useRef(new Map<string, HTMLButtonElement>());
  const focusTimerRef = useRef<number | null>(null);
  const previousMobileRef = useRef(isMobile);
  const [moreOpen, setMoreOpen] = useState(false);
  const [rovingId, setRovingId] = useState<string>("");
  const [menuRovingId, setMenuRovingId] = useState<string>("");

  const actionCommands = commands
    .filter((command) => command.surfaces.includes("action-bar") && command.actionBar)
    .sort((left, right) =>
      (left.actionBar?.order ?? 0) - (right.actionBar?.order ?? 0),
    );
  const placement = isMobile ? "mobile" : "desktop";
  const primaryCommands = actionCommands.filter(
    (command) => command.actionBar?.[placement] === "primary",
  );
  const overflowCommands = actionCommands.filter(
    (command) => command.actionBar?.[placement] === "overflow",
  );
  const topLevelIds = [
    ...primaryCommands.map((command) => command.id),
    ...(overflowCommands.length ? [MORE_ITEM_ID] : []),
  ];
  const effectiveRovingId = topLevelIds.includes(rovingId)
    ? rovingId
    : (topLevelIds[0] ?? "");
  const effectiveMenuRovingId = overflowCommands.some(
    (command) => command.id === menuRovingId,
  )
    ? menuRovingId
    : (overflowCommands[0]?.id ?? "");

  const clearFocusTimer = useCallback(() => {
    if (focusTimerRef.current !== null) {
      window.clearTimeout(focusTimerRef.current);
      focusTimerRef.current = null;
    }
  }, []);

  const focusMoreButtonSoon = useCallback(() => {
    clearFocusTimer();
    focusTimerRef.current = window.setTimeout(() => {
      moreButtonRef.current?.focus();
      focusTimerRef.current = null;
    }, 0);
  }, [clearFocusTimer]);

  const focusFirstMenuItemSoon = useCallback((commandId: string) => {
    clearFocusTimer();
    focusTimerRef.current = window.setTimeout(() => {
      menuItemRefs.current.get(commandId)?.focus();
      focusTimerRef.current = null;
    }, 0);
  }, [clearFocusTimer]);

  const closeMore = useCallback((restoreFocus: boolean) => {
    setMoreOpen(false);
    if (restoreFocus) {
      focusMoreButtonSoon();
    }
  }, [focusMoreButtonSoon]);

  useEffect(() => clearFocusTimer, [clearFocusTimer]);

  useEffect(() => {
    if (!moreOpen) return undefined;

    const handleMouseDown = (event: MouseEvent) => {
      if (
        event.target instanceof Node &&
        rootRef.current?.contains(event.target)
      ) {
        return;
      }
      setMoreOpen(false);
      clearFocusTimer();
      focusTimerRef.current = window.setTimeout(() => {
        const active = document.activeElement;
        if (
          !active ||
          active === document.body ||
          (active instanceof Node && moreMenuRef.current?.contains(active))
        ) {
          moreButtonRef.current?.focus();
        }
        focusTimerRef.current = null;
      }, 0);
    };
    const handleResize = () => closeMore(true);
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      closeMore(true);
    };

    document.addEventListener("mousedown", handleMouseDown);
    window.addEventListener("resize", handleResize);
    window.addEventListener("keydown", handleEscape);
    return () => {
      document.removeEventListener("mousedown", handleMouseDown);
      window.removeEventListener("resize", handleResize);
      window.removeEventListener("keydown", handleEscape);
    };
  }, [clearFocusTimer, closeMore, moreOpen]);

  useEffect(() => {
    if (previousMobileRef.current !== isMobile && moreOpen) {
      closeMore(true);
    }
    previousMobileRef.current = isMobile;
  }, [closeMore, isMobile, moreOpen]);

  function commandLabel(command: EditorCommand) {
    return resolveLabel(command.labelKey, command.fallbackLabel);
  }

  function disabledReason(command: EditorCommand) {
    const explicit = disabledReasons[command.id];
    if (typeof explicit === "string") return explicit;
    if (!command.modes.includes(mode)) {
      return mode === "reading"
        ? labels.readingDisabledReason
        : `This command is unavailable in ${mode} mode.`;
    }
    return null;
  }

  function commandTitle(command: EditorCommand, label: string, reason: string | null) {
    const shortcutHint = command.shortcut ? ` (${command.shortcut.display})` : "";
    return reason ? `${label}${shortcutHint} — ${reason}` : `${label}${shortcutHint}`;
  }

  function preservePointerSelection(event: ReactPointerEvent<HTMLButtonElement>) {
    if (event.button === 0) event.preventDefault();
  }

  function preserveMouseSelection(event: ReactMouseEvent<HTMLButtonElement>) {
    if (event.button !== 0) return;
    event.preventDefault();
    onPreserveSelection?.();
  }

  function runCommand(command: EditorCommand) {
    if (disabledReason(command)) return;
    setMoreOpen(false);
    onCommand(command.id, command);
  }

  function moveRovingFocus(
    event: ReactKeyboardEvent<HTMLButtonElement>,
    ids: readonly string[],
    currentId: string,
    refs: Map<string, HTMLButtonElement>,
    update: (id: string) => void,
  ) {
    const horizontal =
      event.key === "ArrowLeft" || event.key === "ArrowRight";
    const vertical = event.key === "ArrowUp" || event.key === "ArrowDown";
    if (!horizontal && !vertical && event.key !== "Home" && event.key !== "End") {
      return;
    }
    event.preventDefault();
    const currentIndex = Math.max(0, ids.indexOf(currentId));
    let nextIndex = currentIndex;
    if (event.key === "Home") nextIndex = 0;
    if (event.key === "End") nextIndex = ids.length - 1;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") {
      nextIndex = (currentIndex + 1) % ids.length;
    }
    if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
      nextIndex = (currentIndex - 1 + ids.length) % ids.length;
    }
    const nextId = ids[nextIndex];
    if (!nextId) return;
    update(nextId);
    refs.get(nextId)?.focus();
  }

  function renderActionButton(command: EditorCommand, inMenu: boolean) {
    const label = commandLabel(command);
    const reason = disabledReason(command);
    const state = commandStates[command.id];
    const descriptionId = reason
      ? `editor-action-${instanceId}-${command.id}-disabled`
      : undefined;
    const content = renderCommand?.(command, label) ??
      semanticCommandContent(command, label, inMenu);
    const tabIndex = inMenu
      ? (command.id === effectiveMenuRovingId ? 0 : -1)
      : (command.id === effectiveRovingId ? 0 : -1);

    return (
      <button
        aria-describedby={descriptionId}
        aria-disabled={reason ? true : undefined}
        aria-label={label}
        aria-pressed={state ? (state === "mixed" ? "mixed" : state === "active") : undefined}
        className={`editor-action-button${state && state !== "inactive" ? " active" : ""}`}
        data-command-id={command.id}
        data-state={state ?? "inactive"}
        key={command.id}
        onClick={() => runCommand(command)}
        onKeyDown={(event) => {
          if (!inMenu) {
            moveRovingFocus(
              event,
              topLevelIds,
              command.id,
              topLevelRefs.current,
              setRovingId,
            );
            return;
          }
          if (event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            closeMore(true);
            return;
          }
          moveRovingFocus(
            event,
            overflowCommands.map((candidate) => candidate.id),
            command.id,
            menuItemRefs.current,
            setMenuRovingId,
          );
        }}
        onMouseDown={preserveMouseSelection}
        onPointerDown={preservePointerSelection}
        ref={(element) => {
          const refs = inMenu ? menuItemRefs.current : topLevelRefs.current;
          if (element) refs.set(command.id, element);
          else refs.delete(command.id);
        }}
        role={inMenu ? "menuitem" : undefined}
        tabIndex={tabIndex}
        title={commandTitle(command, label, reason)}
        type="button"
      >
        <span aria-hidden="true">{content}</span>
        {reason ? (
          <span id={descriptionId} style={SCREEN_READER_ONLY_STYLE}>
            {reason}
          </span>
        ) : null}
      </button>
    );
  }

  function openMoreMenu() {
    const firstId = overflowCommands[0]?.id;
    if (!firstId) return;
    setMenuRovingId(firstId);
    setMoreOpen(true);
    focusFirstMenuItemSoon(firstId);
  }

  return (
    <div
      className="editor-action-bar-shell"
      data-viewport={isMobile ? "mobile" : "desktop"}
      ref={rootRef}
    >
      <div
        aria-label={labels.toolbar}
        className="editor-action-bar"
        role="toolbar"
      >
        {primaryCommands.map((command, index) => {
          const previous = primaryCommands[index - 1];
          const separated = previous &&
            previous.actionBar?.group !== command.actionBar?.group;
          return (
            <Fragment key={command.id}>
              {separated ? (
                <span
                  aria-hidden="true"
                  className="editor-action-separator"
                  role="separator"
                />
              ) : null}
              {renderActionButton(command, false)}
            </Fragment>
          );
        })}
        {overflowCommands.length ? (
          <>
            {primaryCommands.length ? (
              <span
                aria-hidden="true"
                className="editor-action-separator"
                role="separator"
              />
            ) : null}
            <button
              aria-expanded={moreOpen}
              aria-haspopup="menu"
              aria-label={labels.more}
              className="editor-action-button editor-action-more"
              onClick={() => {
                if (moreOpen) closeMore(true);
                else openMoreMenu();
              }}
              onKeyDown={(event) => {
                if (
                  !moreOpen &&
                  (event.key === "ArrowDown" || event.key === "Enter" || event.key === " ")
                ) {
                  event.preventDefault();
                  openMoreMenu();
                  return;
                }
                moveRovingFocus(
                  event,
                  topLevelIds,
                  MORE_ITEM_ID,
                  topLevelRefs.current,
                  setRovingId,
                );
              }}
              onMouseDown={preserveMouseSelection}
              onPointerDown={preservePointerSelection}
              ref={(element) => {
                moreButtonRef.current = element;
                if (element) topLevelRefs.current.set(MORE_ITEM_ID, element);
                else topLevelRefs.current.delete(MORE_ITEM_ID);
              }}
              tabIndex={effectiveRovingId === MORE_ITEM_ID ? 0 : -1}
              title={labels.more}
              type="button"
            >
              <span aria-hidden="true">{labels.moreShort}</span>
            </button>
          </>
        ) : null}
      </div>
      {moreOpen ? (
        <div
          aria-label={labels.moreMenu}
          className="editor-action-more-menu"
          ref={moreMenuRef}
          role="menu"
        >
          {overflowCommands.map((command) => renderActionButton(command, true))}
        </div>
      ) : null}
    </div>
  );
}

import { defaultKeymap, history, historyKeymap, indentWithTab, redo, undo } from "@codemirror/commands";
import { markdown, markdownLanguage } from "@codemirror/lang-markdown";
import {
  bracketMatching,
  defaultHighlightStyle,
  foldGutter,
  indentOnInput,
  syntaxHighlighting,
} from "@codemirror/language";
import type { Extension } from "@codemirror/state";
import { EditorView, type KeyBinding, keymap } from "@codemirror/view";
import type { EditorView as EditorViewType } from "@codemirror/view";
import CodeMirror from "@uiw/react-codemirror";
import {
  type KeyboardEvent as ReactKeyboardEvent,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";

import { EditorActionBar } from "./EditorActionBar";
import {
  EDITOR_COMMANDS,
  editorCommandById,
  editorShortcutCodeMirrorKey,
  getMarkdownCommandState,
  isTextMutationCommand,
  planEditorCommand,
  type EditorCommand,
  type EditorCommandId,
  type EditorCommandState,
  type EditorMode,
} from "./editorCommands";
import { MarkdownView } from "./MarkdownView";

const MARKDOWN_KERNEL_COMMANDS = EDITOR_COMMANDS.filter(
  (command) =>
    command.id === "undo" ||
    command.id === "redo" ||
    isTextMutationCommand(command),
) as readonly EditorCommand[];

export type MarkdownEditorKernelLabels = {
  actionBarMore: string;
  actionBarMoreMenu: string;
  actionBarMoreShort: string;
  actionBarReadingDisabled: string;
  actionBarToolbar: string;
  bodyAria: string;
  bodyPlaceholder: string;
  controlsAria: string;
  modeAria: string;
  readingMode: string;
  readingModeTitle: string;
  sourceMode: string;
  sourceModeTitle: string;
  splitMode: string;
  splitModeTitle: string;
};

export type MarkdownEditorKernelProps = {
  className?: string;
  labels: MarkdownEditorKernelLabels;
  onChange: (body: string) => void;
  onSave: () => void;
  renderPolicy?: "page" | "untrusted-preview";
  resolveCommandLabel?: (labelKey: string, fallbackLabel: string) => string;
  value: string;
};

export type MarkdownSourceEditorProps = {
  /** Names the textbox CodeMirror owns. */
  ariaLabel: string;
  /** The surface's own features. */
  extensions?: readonly Extension[];
  /** The surface's own keys; they win over the standard keys. */
  keyBindings?: readonly KeyBinding[];
  onChange: (value: string) => void;
  onCreateEditor?: (view: EditorViewType) => void;
  placeholder: string;
  value: string;
};

const NO_EXTENSIONS: readonly Extension[] = [];
const NO_KEY_BINDINGS: readonly KeyBinding[] = [];
// The app's undo and redo shortcuts (its editor commands), on every surface.
const HISTORY_SHORTCUTS: readonly KeyBinding[] = (["undo", "redo"] as const).map((id) => ({
  key: editorShortcutCodeMirrorKey(editorCommandById(id)!.shortcut!),
  run: id === "undo" ? undo : redo,
  preventDefault: true,
}));

/**
 * The one CodeMirror host for Markdown source: Context pages, the merge
 * result and Library semantic Markdown. Every surface edits the same way --
 * one undo history with the app's undo/redo shortcuts, the standard keys
 * below its own, Markdown, line wrapping and a label on the textbox. A
 * surface adds only its own keys and features.
 */
export function MarkdownSourceEditor({
  ariaLabel,
  extensions = NO_EXTENSIONS,
  keyBindings = NO_KEY_BINDINGS,
  onChange,
  onCreateEditor,
  placeholder,
  value,
}: MarkdownSourceEditorProps) {
  const sourceExtensions = useMemo(() => [
    history(),
    keymap.of([...keyBindings, ...HISTORY_SHORTCUTS, ...defaultKeymap, ...historyKeymap]),
    EditorView.lineWrapping,
    // @uiw/react-codemirror does not reliably forward its aria-label prop to
    // CodeMirror's contenteditable textbox in a real browser: label the
    // element CodeMirror owns, the one that receives keyboard input.
    EditorView.contentAttributes.of({ "aria-label": ariaLabel }),
    markdown({ base: markdownLanguage }),
    ...extensions,
  ], [ariaLabel, extensions, keyBindings]);
  return (
    <CodeMirror
      basicSetup={false}
      extensions={sourceExtensions}
      height="100%"
      onChange={onChange}
      onCreateEditor={onCreateEditor}
      placeholder={placeholder}
      value={value}
    />
  );
}

/**
 * The reusable, content-only part of the Context Markdown editor.
 *
 * It deliberately owns no Page workflow: Review, publication, comments,
 * media upload, titles, and routing remain adapters' responsibilities. This
 * keeps Library semantic overlays on the same CodeMirror/command/sanitized
 * reading surface without coupling the two domains' persistence rules.
 */
export function MarkdownEditorKernel({
  className,
  labels,
  onChange,
  onSave,
  renderPolicy = "page",
  resolveCommandLabel,
  value,
}: MarkdownEditorKernelProps) {
  const rawId = useId().replace(/:/g, "");
  const idBase = `markdown-editor-${rawId}`;
  const editorViewRef = useRef<EditorViewType | null>(null);
  const [mode, setMode] = useState<EditorMode>("source");
  const [singlePane, setSinglePane] = useState(() => matchesMedia("(max-width: 640px)"));
  const [compactActions, setCompactActions] = useState(() => matchesMedia("(max-width: 900px)"));
  const [commandStates, setCommandStates] = useState<
    Partial<Record<EditorCommandId, EditorCommandState>>
  >({});

  useEffect(() => watchMedia("(max-width: 640px)", setSinglePane), []);
  useEffect(() => watchMedia("(max-width: 900px)", setCompactActions), []);
  useEffect(() => {
    if (singlePane && mode === "split") setMode("source");
  }, [mode, singlePane]);

  const syncCommandStates = useCallback((view: EditorViewType) => {
    const selection = view.state.selection.main;
    const body = view.state.doc.toString();
    const next: Partial<Record<EditorCommandId, EditorCommandState>> = {};
    for (const command of MARKDOWN_KERNEL_COMMANDS) {
      if (!isTextMutationCommand(command)) continue;
      next[command.id] = getMarkdownCommandState({
        body,
        commandId: command.mutation,
        selection: { anchor: selection.anchor, head: selection.head },
      });
    }
    setCommandStates(next);
  }, []);

  const executeCommandInView = useCallback((
    commandId: EditorCommandId,
    commandView: EditorViewType | null,
  ) => {
    const command = editorCommandById(commandId, MARKDOWN_KERNEL_COMMANDS);
    const view = commandView;
    if (!command || !view || !command.modes.includes(mode)) return false;
    if (command.id === "undo") return undo(view);
    if (command.id === "redo") return redo(view);
    if (!isTextMutationCommand(command)) return false;

    const selection = view.state.selection.main;
    const plan = planEditorCommand({
      body: view.state.doc.toString(),
      commandId: command.mutation,
      selection: { anchor: selection.anchor, head: selection.head },
    });
    view.dispatch({
      changes: plan.change,
      selection: plan.selection,
      scrollIntoView: true,
    });
    syncCommandStates(view);
    view.focus();
    return true;
  }, [mode, syncCommandStates]);

  const extensions = useMemo(
    () => [
      foldGutter(),
      indentOnInput(),
      syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
      bracketMatching(),
      EditorView.updateListener.of((update) => {
        if (update.selectionSet || update.docChanged) syncCommandStates(update.view);
      }),
    ],
    [syncCommandStates],
  );
  const keyBindings = useMemo(
    () => [
      indentWithTab,
      ...MARKDOWN_KERNEL_COMMANDS.filter((command) => command.shortcut).map(
        (command) => ({
          key: editorShortcutCodeMirrorKey(command.shortcut!),
          run(view: EditorViewType) {
            return executeCommandInView(command.id, view);
          },
          preventDefault: true,
        }),
      ),
    ],
    [executeCommandInView],
  );

  const activeMode = singlePane && mode === "split" ? "source" : mode;
  const showSource = activeMode !== "reading";
  const showReading = activeMode !== "source";
  const availableModes: readonly EditorMode[] = singlePane
    ? ["source", "reading"]
    : ["source", "reading", "split"];

  function handleModeKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (!["ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const currentIndex = Math.max(0, availableModes.indexOf(activeMode));
    const nextMode = event.key === "Home"
      ? availableModes[0]
      : event.key === "End"
        ? availableModes.at(-1)!
        : availableModes[
            (currentIndex + (event.key === "ArrowLeft" ? -1 : 1) + availableModes.length) %
              availableModes.length
          ];
    setMode(nextMode);
    requestAnimationFrame(() =>
      document.getElementById(`${idBase}-${nextMode}-tab`)?.focus(),
    );
  }

  return (
    <div
      className={`markdown-editor-kernel${className ? ` ${className}` : ""}`}
      onKeyDownCapture={(event) => {
        if (
          !event.nativeEvent.isComposing &&
          (event.ctrlKey || event.metaKey) &&
          !event.altKey &&
          event.key.toLowerCase() === "s"
        ) {
          event.preventDefault();
          onSave();
        }
      }}
    >
      <div className="editor-command-bar" aria-label={labels.controlsAria}>
        <div
          aria-label={labels.modeAria}
          className="segmented-control compact editor-mode-tabs"
          onKeyDown={handleModeKeyDown}
          role="tablist"
          tabIndex={-1}
        >
          <ModeTab
            active={activeMode === "source"}
            controls={`${idBase}-source-panel`}
            id={`${idBase}-source-tab`}
            label={labels.sourceMode}
            onClick={() => setMode("source")}
            title={labels.sourceModeTitle}
          />
          <ModeTab
            active={activeMode === "reading"}
            controls={`${idBase}-reading-panel`}
            id={`${idBase}-reading-tab`}
            label={labels.readingMode}
            onClick={() => setMode("reading")}
            title={labels.readingModeTitle}
          />
          {!singlePane ? (
            <ModeTab
              active={activeMode === "split"}
              controls={`${idBase}-source-panel ${idBase}-reading-panel`}
              id={`${idBase}-split-tab`}
              label={labels.splitMode}
              onClick={() => setMode("split")}
              title={labels.splitModeTitle}
            />
          ) : null}
        </div>
        <EditorActionBar
          commandStates={commandStates}
          commands={MARKDOWN_KERNEL_COMMANDS}
          isMobile={compactActions}
          labels={{
            more: labels.actionBarMore,
            moreMenu: labels.actionBarMoreMenu,
            moreShort: labels.actionBarMoreShort,
            readingDisabledReason: labels.actionBarReadingDisabled,
            toolbar: labels.actionBarToolbar,
          }}
          mode={activeMode}
          onCommand={(commandId) =>
            executeCommandInView(commandId, editorViewRef.current)
          }
          onPreserveSelection={() => editorViewRef.current?.focus()}
          resolveLabel={resolveCommandLabel}
        />
      </div>
      <div className="editor-grid" data-layout={activeMode}>
        <div
          aria-hidden={showSource ? undefined : "true"}
          aria-labelledby={`${idBase}-${activeMode === "split" ? "split" : "source"}-tab`}
          className="source-pane"
          hidden={!showSource}
          id={`${idBase}-source-panel`}
          inert={showSource ? undefined : true}
          role="tabpanel"
        >
          <MarkdownSourceEditor
            ariaLabel={labels.bodyAria}
            extensions={extensions}
            keyBindings={keyBindings}
            onChange={onChange}
            onCreateEditor={(view) => {
              editorViewRef.current = view;
              syncCommandStates(view);
            }}
            placeholder={labels.bodyPlaceholder}
            value={value}
          />
        </div>
        {showReading ? (
          <div
            aria-labelledby={`${idBase}-${activeMode === "split" ? "split" : "reading"}-tab`}
            className="preview-pane"
            id={`${idBase}-reading-panel`}
            role="tabpanel"
            tabIndex={0}
          >
            <MarkdownView
              body={value}
              renderPolicy={renderPolicy}
              sourceLineNumbers
              wikilinkMode="plain"
            />
          </div>
        ) : null}
      </div>
    </div>
  );
}

function ModeTab({
  active,
  controls,
  id,
  label,
  onClick,
  title,
}: {
  active: boolean;
  controls: string;
  id: string;
  label: string;
  onClick: () => void;
  title: string;
}) {
  return (
    <button
      aria-controls={controls}
      aria-selected={active}
      className={active ? "active" : ""}
      id={id}
      onClick={onClick}
      role="tab"
      tabIndex={active ? 0 : -1}
      title={title}
      type="button"
    >
      {label}
    </button>
  );
}

function matchesMedia(query: string) {
  return typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia(query).matches;
}

function watchMedia(query: string, update: (matches: boolean) => void) {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return undefined;
  }
  const media = window.matchMedia(query);
  const listener = (event: MediaQueryListEvent) => update(event.matches);
  update(media.matches);
  media.addEventListener("change", listener);
  return () => media.removeEventListener("change", listener);
}

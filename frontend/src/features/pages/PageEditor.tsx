import { autocompletion, closeBrackets, closeBracketsKeymap } from "@codemirror/autocomplete";
import { markdown, markdownLanguage } from "@codemirror/lang-markdown";
import {
  defaultKeymap,
  history,
  historyKeymap,
  indentWithTab,
  redo,
  undo,
} from "@codemirror/commands";
import {
  bracketMatching,
  defaultHighlightStyle,
  foldGutter,
  foldKeymap,
  indentOnInput,
  syntaxHighlighting,
} from "@codemirror/language";
import { highlightSelectionMatches, searchKeymap } from "@codemirror/search";
import { Prec } from "@codemirror/state";
import type { EditorView as EditorViewType } from "@codemirror/view";
import {
  autoUpdate,
  computePosition,
  flip,
  offset,
  shift,
  type VirtualElement,
} from "@floating-ui/dom";
import {
  drawSelection,
  EditorView,
  highlightActiveLine,
  highlightActiveLineGutter,
  keymap,
  lineNumbers,
} from "@codemirror/view";
import {
  AtSign,
  Bold,
  Brackets,
  CheckCircle2,
  Code,
  Images,
  Italic,
  Keyboard,
  Link2,
  Maximize2,
  MessageSquarePlus,
  Minimize2,
  Settings2,
  Strikethrough,
  Wand2,
  X,
} from "lucide-react";
import {
  type ChangeEvent as ReactChangeEvent,
  type CSSProperties,
  type FormEvent as ReactFormEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  type RefObject,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Link,
  useBlocker,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";

import { ApiError, apiErrorMessage } from "../../shared/api/client";
import { useAuth } from "../../shared/auth/AuthContext";
import {
  decideReview,
  formatMarkdown as normalizeMarkdownApi,
} from "../../shared/api/operations";
import {
  autosavePage,
  createPage,
  createPageComment,
  diffPageVersions,
  listPageTags,
  listPages,
  newIdempotencyKey,
  publishPageVersion,
  readPageBySlug,
  sendPageAutosave,
  updatePage,
} from "../../shared/api/pages";
import type { PagePayload, PageSummary, PageVersionDiff } from "../../shared/api/pages";
import { listLibraryItems } from "../../shared/api/library";
import type { LibraryItemSummary } from "../../shared/api/library";
import {
  type ContextMediaCapabilities,
  type ContextMediaAssetUrlResolver,
  contextMediaCapabilitiesAreCurrent,
  contextMediaUploadPolicy,
  contextMediaPageVersionContentUrl,
  getContextMediaCapabilities,
  PENDING_CONTEXT_MEDIA_CAPABILITIES,
} from "../../shared/api/contextMedia";
import { uploadImage as uploadEditorImage } from "../../shared/api/uploads";
import {
  listTree,
  listWorkspaces,
  resolveSelectedWorkspaceId,
} from "../../shared/api/workspace";
import type { TreeNodePayload } from "../../shared/api/workspace";
import { AdvancedSection } from "../../shared/components/AdvancedSection";
import { Breadcrumbs, type BreadcrumbItem } from "../../shared/components/Breadcrumbs";
import { ChipInput } from "../../shared/components/ChipInput";
import { DiffViewer } from "../../shared/components/DiffViewer";
import { Dialog } from "../../shared/components/Dialog";
import { EmojiPicker } from "../../shared/components/EmojiPicker";
import { FormField, joinIds } from "../../shared/components/FormField";
import { KeyboardShortcutsModal } from "../../shared/components/KeyboardShortcutsModal";
import { MobileDrawer } from "../../shared/components/MobileDrawer";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { TreePickerDropdown } from "../../shared/components/TreePickerDropdown";
import { TreeRail } from "../../shared/components/TreeRail";
import { targetBreadcrumbs } from "../../shared/components/treeBreadcrumbs";
import { TREE_TARGET_PARAM } from "../../shared/components/treeRouteState";
import { useMobileModalFocus } from "../../shared/components/useMobileModalFocus";
import { isAbortError } from "../../shared/lib/abortError";
import type { ContextPublishNotice } from "../../shared/lib/contextPublishNavigation";
import { formatVersionTimestamp } from "../../shared/lib/datetime";
import {
  formatUploadByteLimit,
  uploadAcceptValue,
  uploadFileRejection,
} from "../../shared/media/formatCapabilities";
import { normalizeSlugInput, slugify } from "../../shared/text/slug";
import { ContextMediaManagerDialog } from "./ContextMediaManagerDialog";
import { EditorActionBar } from "./EditorActionBar";
import {
  MARKDOWN_GEOMETRY_CHANGE_EVENT,
  MarkdownView,
} from "./MarkdownView";
import { MarkdownSourceEditor } from "./MarkdownEditorKernel";
import {
  buildAnchorTable,
  lineToOffset,
  offsetToLine,
  scrollTopForAnchor,
  type ScrollAnchor,
  type ScrollAnchorSpan,
  viewportAnchor,
} from "./editorScrollMapping";
import {
  BLOCK_MENU_GROUP_LABELS,
  BLOCK_MENU_GROUPS,
  blockInsertPrefix,
  blockInsertRangeForSelection,
  blockSnippet,
  filterBlockEntries,
} from "./editorBlocks";
import type { BlockMenuEntry } from "./editorBlocks";
import { createEditorCompletionSource } from "./editorCompletions";
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
import {
  appendClosingCodeFence,
  findUnclosedCodeFence,
} from "./codeFenceBalance";
import { PagePublicShareControl } from "./PagePublicShareControl";
import {
  cancelAllImageUploads,
  canRetryImageUpload,
  cancelImageUpload as cancelQueuedImageUpload,
  createUploadTargetSnapshot as buildUploadTargetSnapshot,
  createEditorPasteHandlers,
  invalidateAllImageUploads,
  insertImageFilesAtRange,
  removeImageUpload,
  type EditorPasteOptions,
  type ImageUploadLifecycleStatus,
  type ImageUploadPlaceholder,
  type UploadTargetSnapshot,
  retryImageUpload,
} from "./editorPaste";
import {
  type MediaUploadCoordinatorState,
  type MediaUploadRejectionCode,
  summarizeMediaUploadBatches,
} from "./mediaUploadCoordinator";
import { clearEditorUploadSlots } from "./editorUploadSlots";
import {
  DEFAULT_PAGE_METADATA,
  PAGE_TYPE_OPTIONS,
  type PageIcon,
  type PageMetadata,
  type PageType,
  normalizeAliasesInput,
  normalizeTagsInput,
  parsePageDocument,
  serializePageDocument,
} from "./pageFrontmatter";

type PageEditorProps = {
  mode: "create" | "edit";
  autosaveCheckpointMs?: number;
};

type SaveState =
  | "clean"
  | "dirty"
  | "saving"
  | "saving_with_newer_edits"
  | "error"
  | "conflict";
type PublicationState = "published" | "staged" | "never-published";
type StalePublishState = "idle" | "blocked" | "reloading" | "error";
type ExecuteEditorCommandOptions = {
  replaceRange?: { from: number; to: number } | null;
};
type SaveOptions = {
  bodyOverride?: string;
  publish?: boolean;
  publishDespiteFindings?: boolean;
  skipFenceCheck?: boolean;
  source?: "auto" | "manual";
};
type PageLoadState = "loading" | "ready" | "not_found" | "error";
type MediaUploadCapabilityState = "loading" | "ready" | "error";
type DraftSnapshot = {
  title: string;
  slug: string;
  body: string;
  destinationNodeId: string;
  icon: PageIcon;
  pageType: PageType;
  tags: string[];
  aliases: string[];
};
type InlineToolbarState = {
  visible: boolean;
  left: number;
  top: number;
};
type ActiveCommandState = Partial<Record<EditorCommandId, EditorCommandState>>;
type FloatingPosition = {
  left: number;
  top: number;
};
type EmojiPickerState = {
  insertPrefix: string;
  left: number;
  range: { from: number; to: number };
  top: number;
};
type LinkDialogState = {
  from: number;
  to: number;
  text: string;
  url: string;
  left: number;
  top: number;
};
type CommentPreviewState = {
  from: number;
  to: number;
  selectedText: string;
  note: string;
  left: number;
  top: number;
};
type AiInlineState = {
  from: number;
  to: number;
  left: number;
  top: number;
  selectedText: string;
};
type CloseAiInlineOptions = {
  restoreFocus?: boolean;
};
type FencePublishPrompt = {
  closingFence: string;
  message: string;
};

type ContextImageUploadEntry = ImageUploadPlaceholder & {
  status: ImageUploadLifecycleStatus;
  message?: string;
};

const PAGE_EDITOR_LOCAL_ERROR = {
  pageCreationNotStarted: "page_editor_page_creation_not_started",
  workspaceUnavailable: "page_editor_workspace_unavailable",
} as const;

const INLINE_TOOLBAR_MARGIN = 8;
const INLINE_TOOLBAR_ESTIMATED_WIDTH = 260;
const INLINE_TOOLBAR_ESTIMATED_HEIGHT = 44;
const LINK_POPOVER_ESTIMATED_WIDTH = 360;
// Includes the mobile 44px close/action targets. A stale 210px estimate placed
// the 249px form below the viewport and made its actions unreachable.
const LINK_POPOVER_ESTIMATED_HEIGHT = 360;
const COMMENT_POPOVER_ESTIMATED_WIDTH = 360;
const COMMENT_POPOVER_ESTIMATED_HEIGHT = 360;
const EDITOR_MODES: EditorMode[] = ["source", "reading", "split"];
const CONTEXT_RAIL_DRAWER_MAX_WIDTH = 1024;
const COMPACT_ACTION_BAR_MAX_WIDTH = 720;
const SINGLE_PANE_EDITOR_MAX_WIDTH = 640;
const TITLE_TEXTAREA_MIN_HEIGHT = 44;
const TITLE_TEXTAREA_MAX_HEIGHT = 132;
const TITLE_MAX_LENGTH = 200;
const TITLE_CONTROL_CHARS_PATTERN = /[\u0000-\u001F\u007F\u2028\u2029]+/g;
// Defense-in-depth for legacy or manually pasted upload markers. Interactive
// uploads keep their transient slots outside EditorState.doc.
const UPLOAD_PLACEHOLDER_PATTERN = /!\[[^\]]*\]\(\u200b/;

export const AUTOSAVE_CHECKPOINT_MS = 60_000;
const SAVED_JUST_NOW_MS = 60_000;

function sourceLineBoundaryOffset(
  view: EditorViewType,
  sourceElement: HTMLElement,
  boundaryLine: number,
) {
  const lineCount = view.state.doc.lines;
  if (boundaryLine <= 1) {
    return 0;
  }
  if (boundaryLine >= lineCount + 1) {
    return sourceElement.scrollHeight;
  }
  const line = view.state.doc.line(Math.floor(boundaryLine));
  const block = view.lineBlockAt(line.from);
  const scaleY = Number.isFinite(view.scaleY) && view.scaleY > 0 ? view.scaleY : 1;
  return Math.max(
    0,
    Math.min(
      sourceElement.scrollHeight,
      (view.documentPadding.top + block.top) / scaleY,
    ),
  );
}

function sourceOffsetToLine(
  view: EditorViewType,
  sourceElement: HTMLElement,
  contentOffset: number,
) {
  const lineCount = view.state.doc.lines;
  const safeOffset = Math.max(
    0,
    Math.min(sourceElement.scrollHeight, contentOffset),
  );
  if (safeOffset <= 0) {
    return 1;
  }
  if (safeOffset >= sourceElement.scrollHeight) {
    return lineCount + 1;
  }
  const scaleY = Number.isFinite(view.scaleY) && view.scaleY > 0 ? view.scaleY : 1;
  const block = view.lineBlockAtHeight(
    safeOffset * scaleY - view.documentPadding.top,
  );
  const startLine = view.state.doc.lineAt(block.from).number;
  const endBoundaryLine = Math.min(
    lineCount + 1,
    Math.max(startLine, view.state.doc.lineAt(block.to).number) + 1,
  );
  const startOffset = sourceLineBoundaryOffset(view, sourceElement, startLine);
  const endOffset = sourceLineBoundaryOffset(
    view,
    sourceElement,
    endBoundaryLine,
  );
  if (endOffset <= startOffset) {
    return startLine;
  }
  const fraction = Math.max(
    0,
    Math.min(1, (safeOffset - startOffset) / (endOffset - startOffset)),
  );
  return Math.min(
    lineCount + 1,
    startLine + fraction * (endBoundaryLine - startLine),
  );
}

function sourceLineToOffset(
  view: EditorViewType,
  sourceElement: HTMLElement,
  sourceLine: number,
) {
  const lineCount = view.state.doc.lines;
  const safeLine = Math.max(1, Math.min(lineCount + 1, sourceLine));
  if (safeLine >= lineCount + 1) {
    return sourceElement.scrollHeight;
  }
  const lineNumber = Math.floor(safeLine);
  const block = view.lineBlockAt(view.state.doc.line(lineNumber).from);
  const startLine = view.state.doc.lineAt(block.from).number;
  const endBoundaryLine = Math.min(
    lineCount + 1,
    Math.max(startLine, view.state.doc.lineAt(block.to).number) + 1,
  );
  const lineSpan = Math.max(1, endBoundaryLine - startLine);
  const fraction = Math.max(0, Math.min(1, (safeLine - startLine) / lineSpan));
  const startOffset = sourceLineBoundaryOffset(view, sourceElement, startLine);
  const endOffset = sourceLineBoundaryOffset(
    view,
    sourceElement,
    endBoundaryLine,
  );
  return startOffset + fraction * (endOffset - startOffset);
}

export function RoutedPageEditor(props: PageEditorProps) {
  const { slug = "" } = useParams();
  return <PageEditor key={`${props.mode}:${slug}`} {...props} />;
}

export function PageEditor({
  mode,
  autosaveCheckpointMs = AUTOSAVE_CHECKPOINT_MS,
}: PageEditorProps) {
  const { t } = useTranslation("pages");
  const { user: authUser } = useAuth();
  const actorId = authUser?.user_id ?? null;
  const translationRef = useRef(t);
  useEffect(() => {
    translationRef.current = t;
  }, [t]);
  const discardChangesMessage = t("editor.discardChanges", {
    defaultValue:
      "Leave this editor? Your changes are saved as a draft but are not yet visible to readers.",
  });
  const discardPendingUploadsMessage = t("editor.discardPendingUploads", {
    defaultValue:
      "Leave this editor? Unresolved uploads and unsaved edits cannot be recovered and will be lost.",
  });
  const { slug = "" } = useParams();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [pages, setPages] = useState<PageSummary[]>([]);
  const [page, setPage] = useState<PagePayload | null>(null);
  const [sourceSuggestions, setSourceSuggestions] = useState<LibraryItemSummary[]>([]);
  const [title, setTitle] = useState("");
  const [pageSlug, setPageSlug] = useState(mode === "create" ? "" : slug);
  const [body, setBody] = useState("");
  const [pageIcon, setPageIcon] = useState<PageIcon>(DEFAULT_PAGE_METADATA.icon);
  const [pageType, setPageType] = useState<PageType>(DEFAULT_PAGE_METADATA.type);
  const [tagInput, setTagInput] = useState("");
  const [aliasInput, setAliasInput] = useState("");
  // Aliases run with truncateItems={false}, so an over-long entry is rejected
  // rather than silently trimmed. Without this the rejection was invisible:
  // Enter simply did nothing. CustomFormatEditor and FormatAliasGroupList
  // already surface ChipInput's onItemTooLong this way.
  const [aliasItemError, setAliasItemError] = useState<string | null>(null);
  const [tagQuery, setTagQuery] = useState("");
  const [tagSuggestions, setTagSuggestions] = useState<string[]>([]);
  const [status, setStatus] = useState<string | null>(null);
  const [publicationState, setPublicationState] = useState<PublicationState>(
    mode === "create" ? "never-published" : "published",
  );
  const [fencePublishPrompt, setFencePublishPrompt] =
    useState<FencePublishPrompt | null>(null);
  const [saveError, setSaveError] = useState(false);
  const [editorRevision, setEditorRevision] = useState(0);
  const [acknowledgedRevision, setAcknowledgedRevision] = useState(0);
  const [inFlightRevision, setInFlightRevision] = useState<number | null>(null);
  const [titleValidationRequested, setTitleValidationRequested] = useState(false);
  const [saveRelativeNow, setSaveRelativeNow] = useState(() => Date.now());
  const [workspaceId, setWorkspaceId] = useState<string | null>(null);
  const [workspaceSlug, setWorkspaceSlug] = useState<string | null>(null);
  const [editorPreferenceKey, setEditorPreferenceKey] = useState<string | null>(
    null,
  );
  const [workspaceVisibility, setWorkspaceVisibility] = useState("private");
  const [workspaceRole, setWorkspaceRole] = useState<string | null>(null);
  const [contextMediaCapabilities, setContextMediaCapabilities] =
    useState<ContextMediaCapabilities>(PENDING_CONTEXT_MEDIA_CAPABILITIES);
  const [mediaUploadCapabilityState, setMediaUploadCapabilityState] =
    useState<MediaUploadCapabilityState>("loading");
  const [contextTree, setContextTree] = useState<TreeNodePayload[]>([]);
  const [destinationNodeId, setDestinationNodeId] = useState("");
  const [isEditorReady, setIsEditorReady] = useState(false);
  const [pageLoadState, setPageLoadState] = useState<PageLoadState>("loading");
  const [editorMode, setEditorMode] = useState<EditorMode>("source");
  const [splitPercent, setSplitPercent] = useState(50);
  const [linkedScrolling, setLinkedScrolling] = useState(true);
  const [singlePaneViewport, setSinglePaneViewport] = useState(
    isSinglePaneEditorViewport,
  );
  const [stackedSplitViewport, setStackedSplitViewport] = useState(
    isStackedSplitEditorViewport,
  );
  const [compactActionBarViewport, setCompactActionBarViewport] = useState(
    isCompactActionBarViewport,
  );
  const [contextRailDrawerViewport, setContextRailDrawerViewport] = useState(
    isContextRailDrawerViewport,
  );
  const [focusMode, setFocusMode] = useState(false);
  const [contextRailOpen, setContextRailOpen] = useState(false);
  const [mediaContentUrls, setMediaContentUrls] = useState<Record<string, string>>({});
  const [imageUploadEntries, setImageUploadEntries] =
    useState<ContextImageUploadEntry[]>([]);
  const [imageUploadBatches, setImageUploadBatches] =
    useState<MediaUploadCoordinatorState[]>([]);
  const imageUploadValidationMessagesRef = useRef(new WeakMap<File, string>());
  const [propertiesOpen, setPropertiesOpen] = useState(false);
  const [showAdvancedProperties, setShowAdvancedProperties] = useState(false);
  const [contextExpandedNodeIds, setContextExpandedNodeIds] = useState<string[]>([]);
  const [shortcutHelpOpen, setShortcutHelpOpen] = useState(false);
  const [mediaManagerOpen, setMediaManagerOpen] = useState(false);
  const [inlineToolbar, setInlineToolbar] = useState<InlineToolbarState>({
    visible: false,
    left: 0,
    top: 0,
  });
  const [activeCommandStates, setActiveCommandStates] =
    useState<ActiveCommandState>({});
  const [linkDialog, setLinkDialog] = useState<LinkDialogState | null>(null);
  const [commentPreview, setCommentPreview] = useState<CommentPreviewState | null>(null);
  const [commentSubmitting, setCommentSubmitting] = useState(false);
  const [aiInline, setAiInline] = useState<AiInlineState | null>(null);
  const [blockMenuTrigger, setBlockMenuTrigger] = useState<"manual" | "slash" | null>(null);
  const [blockMenuQuery, setBlockMenuQuery] = useState("");
  const [blockMenuPosition, setBlockMenuPosition] = useState<FloatingPosition | null>(null);
  const [propertiesPopoverPosition, setPropertiesPopoverPosition] =
    useState<FloatingPosition | null>(null);
  const [emojiPicker, setEmojiPicker] = useState<EmojiPickerState | null>(null);
  const [highlightedBlockIndex, setHighlightedBlockIndex] = useState(0);
  const [conflict, setConflict] = useState<{
    serverVersionId: string;
    serverContentHash: string;
  } | null>(null);
  const [conflictDiff, setConflictDiff] = useState<PageVersionDiff | null>(null);
  const [conflictDiffError, setConflictDiffError] = useState<string | null>(null);
  const [conflictDiffLoading, setConflictDiffLoading] = useState(false);
  const [conflictDiffOpen, setConflictDiffOpen] = useState(false);
  const [stalePublishState, setStalePublishState] =
    useState<StalePublishState>("idle");
  const [isSaving, setIsSaving] = useState(false);
  const savingRef = useRef(false);
  // A publish the author asked for while a save was in flight.
  const pendingPublishRef = useRef<SaveOptions | null>(null);
  // Multiple checkpoint/flush requests that arrive during one write collapse
  // into a single follow-up autosave after that write settles.
  const pendingAutosaveRef = useRef(false);
  const isComposingRef = useRef(false);
  const slugTouchedRef = useRef(mode === "edit");
  const titleRef = useRef<HTMLTextAreaElement | null>(null);
  const propertiesButtonRef = useRef<HTMLButtonElement | null>(null);
  const propertiesPopoverRef = useRef<HTMLDivElement | null>(null);
  const propertiesDestinationMenuRef = useRef<HTMLDivElement | null>(null);
  const setPropertiesDestinationMenu = useCallback((element: HTMLDivElement | null) => {
    propertiesDestinationMenuRef.current = element;
  }, []);
  const editorViewRef = useRef<EditorViewType | null>(null);
  const previewPaneRef = useRef<HTMLDivElement | null>(null);
  const editorGridRef = useRef<HTMLDivElement | null>(null);
  const splitSeparatorRef = useRef<HTMLDivElement | null>(null);
  const splitPointerIdRef = useRef<number | null>(null);
  const syncPreviewToCaretRef = useRef<(view: EditorViewType) => void>(() => {});
  const frontmatterExtraLinesRef = useRef<string[]>([]);
  const pagesRef = useRef<PageSummary[]>([]);
  const sourceSuggestionsRef = useRef<LibraryItemSummary[]>([]);
  const pageIdRef = useRef<string | null>(null);
  const baseVersionIdRef = useRef<string | null>(null);
  const pageCreationPromiseRef = useRef<Promise<PagePayload> | null>(null);
  const destinationNodeIdRef = useRef(destinationNodeId);
  const contextMediaCapabilitiesRef = useRef<ContextMediaCapabilities>(
    PENDING_CONTEXT_MEDIA_CAPABILITIES,
  );
  const mediaUploadCapabilityCurrentRef = useRef(false);
  const workspaceIdRef = useRef<string | null>(null);
  const actorIdRef = useRef<string | null>(actorId);
  const actorSessionGenerationRef = useRef(1);
  const editorGenerationRef = useRef(1);
  const uploadAuthorityKeyRef = useRef<string | null>(null);
  const saveRef = useRef<(options?: SaveOptions) => void>(() => {});
  const autosaveBackstopRef = useRef<
    (options?: { allowInFlight?: boolean }) => boolean
  >(() => false);
  // One dismissal fires beforeunload *and* pagehide. The backstop never
  // updates lastSavedFingerprintRef, so it cannot dedupe itself; remember the
  // bytes it already shipped instead of latching a boolean, so a cancelled
  // dismissal followed by more edits still flushes.
  const dismissalAutosaveFingerprintRef = useRef<string | null>(null);
  const sessionVersionIdRef = useRef<string | null>(null);
  const commandRef = useRef<(
    commandId: EditorCommandId,
    view?: EditorViewType | null,
    options?: ExecuteEditorCommandOptions,
  ) => boolean>(() => false);
  const openBlockMenuAtSelectionRef = useRef<
    (view?: EditorViewType | null) => void
  >(() => {});
  const syncInlineToolbarRef = useRef<(view: EditorViewType) => void>(() => {});
  const syncSlashBlockMenuRef = useRef<(view: EditorViewType) => void>(() => {});
  const blockMenuTriggerRef = useRef<"manual" | "slash" | null>(null);
  const blockMenuRangeRef = useRef<{ from: number; to: number } | null>(null);
  const blockMenuSearchRef = useRef<HTMLInputElement | null>(null);
  const blockMenuRef = useRef<HTMLDivElement | null>(null);
  const imageFileInputRef = useRef<HTMLInputElement | null>(null);
  const contextRailRef = useRef<HTMLDivElement | null>(null);
  const contextRailToggleRef = useRef<HTMLButtonElement | null>(null);
  const linkTextInputRef = useRef<HTMLInputElement | null>(null);
  const linkUrlInputRef = useRef<HTMLInputElement | null>(null);
  const linkPopoverRef = useRef<HTMLFormElement | null>(null);
  const commentNoteRef = useRef<HTMLTextAreaElement | null>(null);
  const pendingImageInsertRef = useRef<{
    insertPrefix: string;
    range: { from: number; to: number };
  } | null>(null);
  const navigationConfirmedRef = useRef(false);
  const isEditorMountedRef = useRef(true);
  const autoSaveTimerRef = useRef<number | null>(null);
  const saveRequestIdRef = useRef(0);
  const editorRevisionRef = useRef(0);
  const acknowledgedRevisionRef = useRef(0);
  const inFlightRevisionRef = useRef<number | null>(null);
  const reviewEditCompletedRef = useRef(false);
  const latestDraftRef = useRef<DraftSnapshot>(emptyDraft(mode, slug));
  const lastSavedRef = useRef<DraftSnapshot>(emptyDraft(mode, slug));
  const lastSavedFingerprintRef = useRef<string | null>(null);
  const propertiesPopoverId = useId();
  const propertiesPopoverTitleId = useId();

  const normalizedSlug = slugify(pageSlug);
  const normalizedTags = useMemo(() => normalizeTagsInput(tagInput), [tagInput]);
  const normalizedAliases = useMemo(
    () => normalizeAliasesInput(aliasInput),
    [aliasInput],
  );
  const aliasValidationMessage = aliasInput.split(",").some((alias) => alias.trim().length > 80) ||
    aliasInput.split(",").filter((alias) => alias.trim()).length > 32
    ? t("editor.aliasesError", { defaultValue: "Use up to 32 aliases, each 80 characters or fewer." })
    : null;
  const pageMetadata = useMemo<PageMetadata>(() => ({
    aliases: normalizedAliases,
    extraLines: frontmatterExtraLinesRef.current,
    icon: pageIcon,
    tags: normalizedTags,
    type: pageType,
  }), [normalizedAliases, normalizedTags, pageIcon, pageType]);
  const hasPendingImageUpload =
    imageUploadEntries.some((entry) => entry.status !== "succeeded") ||
    UPLOAD_PLACEHOLDER_PATTERN.test(body);
  const contextMediaInputAccept = uploadAcceptValue(
    contextMediaUploadPolicy(contextMediaCapabilities),
  );
  const fromReview = searchParams.get("from") === "review";
  const reviewItemId =
    mode === "edit"
      ? searchParams.get("reviewItemId") ?? searchParams.get("review_item_id")
      : null;
  const createParentNodeId = mode === "create" ? searchParams.get("parentNodeId") : null;
  const createInitialTitle = mode === "create" ? normalizeInitialTitle(searchParams.get("title")) : "";
  const isLinkDialogOpen = Boolean(linkDialog);
  const isCommentPreviewOpen = Boolean(commentPreview);
  const isCurrentUploadTarget = useCallback((snapshot: UploadTargetSnapshot) => {
    return (
      snapshot.actorSessionGeneration === actorSessionGenerationRef.current &&
      snapshot.editorGeneration === editorGenerationRef.current &&
      snapshot.workspaceId === workspaceIdRef.current &&
      snapshot.pageId === pageIdRef.current &&
      snapshot.editingVersionId === baseVersionIdRef.current
    );
  }, []);
  const closeContextRail = useCallback(() => setContextRailOpen(false), []);
  const imageUploadOptions = useMemo<EditorPasteOptions>(() => ({
    getUploadBatchCapabilities() {
      return contextMediaCapabilitiesRef.current;
    },
    isUploadCapabilityCurrent() {
      return mediaUploadCapabilityCurrentRef.current;
    },
    uploadCapabilitiesUnavailableMessage() {
      return translationRef.current("editor.imageUploadCapabilitiesUnavailable", {
        defaultValue: "Current media upload limits are unavailable. Try again after they load.",
      });
    },
    uploadInsertionInvalidatedMessage() {
      return translationRef.current("editor.imageUploadRetryMissing", {
        defaultValue: "The upload insertion slot was removed. Insert the image again.",
      });
    },
    async createUploadTargetSnapshot(file, operationId, signal) {
      const actorSessionGeneration = actorSessionGenerationRef.current;
      const editorGeneration = editorGenerationRef.current;
      const workspaceId = workspaceIdRef.current;
      const policyRevision = contextMediaCapabilitiesRef.current.policy_revision;
      if (!workspaceId) {
        throw new Error(PAGE_EDITOR_LOCAL_ERROR.workspaceUnavailable);
      }
      let pageId = pageIdRef.current;
      let editingVersionId = baseVersionIdRef.current;
      if (!pageId || !editingVersionId) {
        if (!pageCreationPromiseRef.current) {
          // No page exists yet for this brand-new draft. Page-first creation
          // (createPage with no slug) is the modern replacement for the
          // deleted media-session bridge: it gives us a real page id and
          // editing version id immediately, so the upload can target the
          // normal /pages/{id}/media route from its very first attempt.
          const createdPromise = createPage(workspaceId, {
            placement_parent_node_id: destinationNodeIdRef.current || null,
            body_md: "",
          }).then((created) => {
            if (
              actorSessionGenerationRef.current !== actorSessionGeneration ||
              editorGenerationRef.current !== editorGeneration ||
              workspaceIdRef.current !== workspaceId
            ) {
              throw new DOMException(
                "Upload target is no longer current.",
                "AbortError",
              );
            }
            pageIdRef.current = created.id;
            baseVersionIdRef.current = created.version_id;
            sessionVersionIdRef.current = created.version_id;
            // Pre-empt the authority-key effect below: without this, its own
            // page?.id/version_id-driven comparison sees this same creation
            // as an authority change and immediately invalidates the upload
            // that triggered it.
            uploadAuthorityKeyRef.current = JSON.stringify([
              actorIdRef.current,
              mode,
              slug,
              workspaceIdRef.current,
              created.id,
              created.version_id,
            ]);
            // A page created only because an image was pasted before any
            // title/slug existed would otherwise leave the slug field
            // permanently empty: it freezes (disabled) the instant `page` is
            // set, and canSave requires a non-empty slug. Seed it from the
            // server placeholder unless the author already typed one.
            setPageSlug((current) => (current.trim() ? current : created.slug));
            setPage(created);
            setPublicationState("never-published");
            return created;
          });
          const trackedPromise = createdPromise.finally(() => {
            if (pageCreationPromiseRef.current === trackedPromise) {
              pageCreationPromiseRef.current = null;
            }
          });
          pageCreationPromiseRef.current = trackedPromise;
        }
        const creationPromise = pageCreationPromiseRef.current;
        if (!creationPromise) {
          throw new Error(PAGE_EDITOR_LOCAL_ERROR.pageCreationNotStarted);
        }
        const created = await waitForPromiseOrAbort(creationPromise, signal);
        pageId = created.id;
        editingVersionId = created.version_id;
      }
      throwIfAborted(signal);
      if (!pageId || !editingVersionId) {
        throw new Error(PAGE_EDITOR_LOCAL_ERROR.pageCreationNotStarted);
      }
      const snapshot = buildUploadTargetSnapshot({
        actorSessionGeneration,
        editingVersionId,
        editorGeneration,
        file,
        operationId,
        pageId,
        policyRevision,
        workspaceId,
      });
      if (!isCurrentUploadTarget(snapshot)) {
        throw new DOMException("Upload target is no longer current.", "AbortError");
      }
      return snapshot;
    },
    isUploadTargetCurrent: isCurrentUploadTarget,
    async refreshUploadPolicy() {
      const workspaceId = workspaceIdRef.current;
      const editorGeneration = editorGenerationRef.current;
      if (!workspaceId) {
        throw new Error(PAGE_EDITOR_LOCAL_ERROR.workspaceUnavailable);
      }
      mediaUploadCapabilityCurrentRef.current = false;
      setMediaUploadCapabilityState("loading");
      try {
        const refreshed = await getContextMediaCapabilities(workspaceId);
        if (workspaceIdRef.current !== workspaceId ||
            editorGenerationRef.current !== editorGeneration) {
          throw new DOMException("Upload target is no longer current.", "AbortError");
        }
        if (!contextMediaCapabilitiesAreCurrent(refreshed)) {
          throw new Error("Context media capabilities were unavailable or invalid.");
        }
        contextMediaCapabilitiesRef.current = refreshed;
        setContextMediaCapabilities(refreshed);
        mediaUploadCapabilityCurrentRef.current = true;
        setMediaUploadCapabilityState("ready");
        return refreshed;
      } catch (error) {
        mediaUploadCapabilityCurrentRef.current = false;
        setMediaUploadCapabilityState("error");
        if (!isEditorMountedRef.current) throw error;
        setStatus(translationRef.current("editor.imageUploadPolicyRefreshFailed", {
          defaultValue: "Media upload policy changed, but its current limits could not be loaded.",
        }));
        throw error;
      }
    },
    onUploadError(file, _message, rejectionCode) {
      if (!isEditorMountedRef.current) return;
      const validationMessage = imageUploadValidationMessagesRef.current.get(file);
      imageUploadValidationMessagesRef.current.delete(file);
      const rejectionMessage = rejectionCode
        ? imageUploadRejectionMessage(rejectionCode, translationRef.current)
        : null;
      const safeMessage = validationMessage ?? rejectionMessage ??
        (mediaUploadCapabilityCurrentRef.current
          ? translationRef.current("editor.imageUploadRequestFailed", {
              defaultValue: "Request failed. Please try again.",
            })
          : translationRef.current("editor.imageUploadCapabilitiesUnavailable", {
              defaultValue:
                "Current media upload limits are unavailable. Try again after they load.",
            }));
      setStatus(translationRef.current("editor.imageUploadFailed", {
        defaultValue: "Image upload failed for {{name}}: {{error}}",
        error: safeMessage,
        name: file.name || "image",
      }));
    },
    onUploadBatchesChange(batches) {
      if (!isEditorMountedRef.current) return;
      setImageUploadBatches([...batches]);
    },
    onUploadStateChange(upload, nextStatus, message) {
      if (!isEditorMountedRef.current) return;
      setImageUploadEntries((current) => {
        const nextEntry: ContextImageUploadEntry = {
          ...upload,
          status: nextStatus,
          message,
        };
        return current.some((entry) => entry.marker === upload.marker)
          ? current.map((entry) =>
              entry.marker === upload.marker ? nextEntry : entry,
            )
          : [...current, nextEntry];
      });
    },
    validateImageFile(file) {
      const rejection = uploadFileRejection(
        file,
        contextMediaUploadPolicy(contextMediaCapabilitiesRef.current),
      );
      if (!rejection) {
        imageUploadValidationMessagesRef.current.delete(file);
        return null;
      }
      if (rejection.reason === "too_large") {
        const message = translationRef.current("editor.imageUploadTooLarge", {
          maxSize: formatUploadByteLimit(rejection.maxBytes),
          defaultValue: "This image exceeds the {{maxSize}} upload limit.",
        });
        imageUploadValidationMessagesRef.current.set(file, message);
        return message;
      }
      const message = translationRef.current("editor.imageUploadUnsupported", {
        defaultValue:
          "Choose an image format allowed by the current workspace upload policy.",
      });
      imageUploadValidationMessagesRef.current.set(file, message);
      return message;
    },
    async uploadImage(file, context) {
      throwIfAborted(context.signal);
      if (!isCurrentUploadTarget(context.targetSnapshot)) {
        throw new DOMException("Upload target is no longer current.", "AbortError");
      }
      const result = await uploadEditorImage({
        file,
        marker: context.marker,
        editingVersionId: context.editingVersionId,
        workspaceId: context.targetSnapshot.workspaceId,
        pageId: context.pageId,
        policyRevision: context.targetSnapshot.policyRevision,
        signal: context.signal,
      });
      throwIfAborted(context.signal);
      if (!isCurrentUploadTarget(context.targetSnapshot)) {
        throw new DOMException("Upload target is no longer current.", "AbortError");
      }
      const assetId = result.asset_id;
      const contentUrl = result.content_url;
      if (assetId && contentUrl) {
        setMediaContentUrls((current) => ({
          ...current,
          [assetId]: contentUrl,
        }));
      }
      return result;
    },
  }), [isCurrentUploadTarget, mode, slug]);
  const bodyAriaLabel = t("editor.bodyAria", { defaultValue: "Markdown body" });
  const resolveContextMediaAsset = useMemo<ContextMediaAssetUrlResolver>(
    () => (assetId: string) => {
      const uploadContentUrl = mediaContentUrls[assetId];
      if (uploadContentUrl) {
        return { status: "ready", url: uploadContentUrl };
      }
      if (page?.id && page.version_id) {
        return {
          status: "ready",
          url: contextMediaPageVersionContentUrl(
            page.workspace_id,
            page.id,
            page.version_id,
            assetId,
          ),
        };
      }
      return { status: "unavailable", reason: "asset_url_unavailable" };
    },
    [mediaContentUrls, page?.id, page?.version_id, page?.workspace_id],
  );
  const extensions = useMemo(
    () => [
      history(),
      lineNumbers(),
      highlightActiveLineGutter(),
      foldGutter(),
      drawSelection(),
      EditorView.lineWrapping,
      // @uiw/react-codemirror does not reliably forward its aria-label prop
      // to CodeMirror's actual contenteditable textbox in a real browser.
      // Label the element CodeMirror owns so assistive technology names the
      // same role="textbox" surface that receives keyboard input.
      EditorView.contentAttributes.of({ "aria-label": bodyAriaLabel }),
      indentOnInput(),
      syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
      bracketMatching(),
      closeBrackets(),
      highlightActiveLine(),
      highlightSelectionMatches(),
      markdown({
        base: markdownLanguage,
      }),
      autocompletion({
        override: [createEditorCompletionSource(pagesRef, sourceSuggestionsRef)],
      }),
      createEditorPasteHandlers(imageUploadOptions),
      // CodeMirror's parsed `Mod-/` key descriptor is not dispatched by
      // WebKit even though the native Ctrl+/ event reaches the editor. Handle
      // this one palette shortcut at the editor DOM boundary so every engine
      // follows the same single path and selection remains owned by the view.
      // Highest precedence is required because defaultKeymap also owns Mod-/
      // for toggleComment; letting that handler win would mutate Markdown.
      Prec.highest(
        EditorView.domEventHandlers({
          keydown(event, view) {
            if (
              !event.isComposing &&
              (event.metaKey || event.ctrlKey) &&
              !event.altKey &&
              event.key === "/"
            ) {
              event.preventDefault();
              openBlockMenuAtSelectionRef.current(view);
              return true;
            }
            return false;
          },
        }),
      ),
      EditorView.updateListener.of((update) => {
        if (update.docChanged) {
          syncSlashBlockMenuRef.current(update.view);
        }
        if (update.selectionSet || update.docChanged || update.viewportChanged) {
          syncInlineToolbarRef.current(update.view);
        }
        if (update.selectionSet || update.docChanged) {
          syncPreviewToCaretRef.current(update.view);
        }
      }),
      keymap.of([
        indentWithTab,
        {
          key: "Mod-s",
          run() {
            saveRef.current();
            return true;
          },
          preventDefault: true,
        },
        ...(EDITOR_COMMANDS as readonly EditorCommand[])
          .filter((command) => command.shortcut)
          .map((command) => ({
            key: editorShortcutCodeMirrorKey(command.shortcut!),
            run(view: EditorViewType) {
              return commandRef.current(command.id, view);
            },
            preventDefault: true,
          })),
        ...defaultKeymap,
        ...closeBracketsKeymap,
        ...searchKeymap,
        ...foldKeymap,
        ...historyKeymap,
      ]),
    ],
    [bodyAriaLabel, imageUploadOptions],
  );
  const contextActiveNodeId = useMemo(
    () => contextTree.find((node) => node.target_id === page?.id)?.id ?? null,
    [contextTree, page?.id],
  );
  const editorBreadcrumbItems = useMemo<BreadcrumbItem[]>(() => {
    const contextLabel = t("editor.rail.context", { defaultValue: "Context" });
    if (mode === "create") {
      const parentItems = createParentNodeId
        ? targetBreadcrumbs({
            basePath: "/context",
            rootLabel: contextLabel,
            nodes: contextTree,
            targetNodeId: createParentNodeId,
          })
        : [{ label: contextLabel, href: "/context" }];
      const currentLabel =
        title.trim() || createInitialTitle || t("editor.breadcrumbNewPage", { defaultValue: "New page" });
      if (sameBreadcrumbLabel(parentItems.at(-1)?.label, currentLabel)) {
        return parentItems;
      }
      return [...parentItems, { label: currentLabel }];
    }
    return targetBreadcrumbs({
      basePath: "/context",
      rootLabel: contextLabel,
      nodes: contextTree,
      targetId: page?.id ?? null,
      targetNodeId: contextActiveNodeId,
      fallbackLabel:
        title.trim() || slug || t("editor.breadcrumbPageFallback", { defaultValue: "Page" }),
    });
  }, [
    contextActiveNodeId,
    contextTree,
    createInitialTitle,
    createParentNodeId,
    mode,
    page?.id,
    slug,
    t,
    title,
  ]);
  const destinationOptions = useMemo(
    () =>
      contextTree.filter(
        (node) =>
          node.can_have_children &&
          (!contextActiveNodeId ||
            (node.id !== contextActiveNodeId &&
              !isTreeDescendant(node, contextActiveNodeId, contextTree))),
      ),
    [contextActiveNodeId, contextTree],
  );
  const hasPersistedPage = Boolean(page);
  const currentDraft: DraftSnapshot = {
    aliases: pageMetadata.aliases,
    body,
    destinationNodeId,
    icon: pageMetadata.icon,
    pageType: pageMetadata.type,
    slug: pageSlug,
    tags: pageMetadata.tags,
    title,
  };
  const hasUnsavedChanges = editorRevision > acknowledgedRevision;
  const saveState: SaveState = conflict
    ? "conflict"
    : saveError
      ? "error"
      : inFlightRevision !== null
        ? editorRevision > inFlightRevision
          ? "saving_with_newer_edits"
          : "saving"
        : hasUnsavedChanges
          ? "dirty"
          : "clean";
  const hasUnpublishedChanges =
    hasUnsavedChanges || (Boolean(page) && publicationState !== "published");
  const hasDismissalRisk = hasUnpublishedChanges || hasPendingImageUpload;
  const dismissalRiskMessage = hasPendingImageUpload
    ? discardPendingUploadsMessage
    : discardChangesMessage;
  const navigationBlocker = useBlocker(({ currentLocation, nextLocation }) =>
    hasDismissalRisk &&
    !navigationConfirmedRef.current &&
    (currentLocation.pathname !== nextLocation.pathname ||
      currentLocation.search !== nextLocation.search ||
      currentLocation.hash !== nextLocation.hash),
  );
  const canSave =
    isEditorReady &&
    title.trim().length > 0 &&
    normalizedSlug.length > 0 &&
    !aliasValidationMessage &&
    !hasPendingImageUpload &&
    (mode === "edit" || destinationOptions.length === 0 || Boolean(destinationNodeId));
  const missingTitle = title.trim().length === 0 || normalizedSlug.length === 0;
  const titleValidationMessage = !titleValidationRequested
    ? null
    : title.trim().length === 0
      ? t("editor.saveBlockedTitle", {
          defaultValue: "Add a title before saving.",
        })
      : title !== title.trim()
        ? t("editor.titleWhitespaceHint", {
            defaultValue: "Leading and trailing spaces are removed when saving.",
          })
        : null;
  const missingDestination =
    mode === "create" && destinationOptions.length > 0 && !destinationNodeId;
  const saveBlockedReason = !isEditorReady
    ? null
    : missingTitle
      ? t("editor.saveBlockedTitle", {
          defaultValue: "Add a title before saving.",
        })
      : aliasValidationMessage
        ? t("editor.saveBlockedAliases", {
            defaultValue: "Fix the aliases before saving.",
          })
        : hasPendingImageUpload
          ? t("editor.saveBlockedUpload", {
              defaultValue:
                "Retry or remove every unresolved image upload before saving.",
            })
          : missingDestination
            ? t("editor.saveBlockedDestination", {
                defaultValue: "Choose a destination before saving.",
              })
            : null;
  const canPublishFromEditor =
    canSave &&
    stalePublishState === "idle" &&
    (saveState === "dirty" ||
      (saveState === "clean" && publicationState !== "published"));
  const activeEditorMode =
    singlePaneViewport && editorMode === "split" ? "source" : editorMode;
  const showSourcePane = activeEditorMode !== "reading";

  useEffect(() => {
    if (aliasValidationMessage) setShowAdvancedProperties(true);
  }, [aliasValidationMessage]);
  const showPreviewPane = activeEditorMode !== "source";
  const editorLayout = activeEditorMode;
  const blockMenuOpen = blockMenuTrigger !== null;
  const filteredBlockEntries = useMemo(
    () => filterBlockEntries(blockMenuQuery),
    [blockMenuQuery],
  );
  const focusModeLabel = focusMode
    ? t("editor.exitFocusMode", { defaultValue: "Exit focus mode (Ctrl+Shift+O)" })
    : t("editor.enterFocusMode", { defaultValue: "Enter focus mode (Ctrl+Shift+O)" });
  const [formatting, setFormatting] = useState(false);
  const fenceFinding = useMemo(() => findUnclosedCodeFence(body), [body]);
  const shortcutGroups = useMemo(
    () => {
      const registered = (EDITOR_COMMANDS as readonly EditorCommand[]).filter(
        (command) => command.shortcut,
      );
      const commandItems = (ids: readonly EditorCommandId[]) => registered
        .filter((command) => ids.includes(command.id))
        .map((command) => ({
          keys: command.shortcut?.display.split("+") ?? [],
          action: t(command.labelKey, { defaultValue: command.fallbackLabel }),
        }));
      return [
        {
          label: t("editor.shortcut.groupNavigation"),
          items: [
            { action: t("editor.shortcutSaveDraft"), keys: ["Ctrl/Cmd", "S"] },
            { action: t("editor.shortcut.modeSwitch"), keys: ["Ctrl/Cmd", "E"] },
            { action: t("editor.shortcut.splitMode"), keys: ["Ctrl/Cmd", "\\"] },
            { action: t("editor.shortcut.focusMode"), keys: ["Ctrl/Cmd", "Shift", "O"] },
            { action: t("editor.shortcut.showPanel"), keys: ["?"] },
          ],
        },
        {
          label: t("editor.shortcut.groupInsert"),
          items: [
            { action: t("editor.shortcut.blockMenu"), keys: ["Ctrl/Cmd", "/"] },
            ...commandItems(["link", "wikilink", "citation", "comment"]),
          ],
        },
        {
          label: t("editor.shortcut.groupFormat"),
          items: commandItems([
            "undo",
            "redo",
            "heading",
            "bold",
            "italic",
            "strikethrough",
            "inline-code",
            "code-block",
            "ordered-list",
            "bulleted-list",
            "task-list",
          ]),
        },
      ];
    },
    [t],
  );

  useLayoutEffect(() => {
    if (showSourcePane) {
      editorViewRef.current?.requestMeasure();
    }
  }, [showSourcePane]);
  // I8: a ref mirroring the live body so handleFormatNow can detect keystrokes
  // typed during the /lint/format round-trip and avoid clobbering them.
  const bodyRef = useRef(body);
  useEffect(() => {
    bodyRef.current = body;
  }, [body]);

  function markDraftMutation() {
    const nextRevision = editorRevisionRef.current + 1;
    editorRevisionRef.current = nextRevision;
    setEditorRevision(nextRevision);
    return nextRevision;
  }

  function acknowledgeEditorRevision(revision: number) {
    const nextAcknowledgedRevision = Math.max(
      acknowledgedRevisionRef.current,
      revision,
    );
    acknowledgedRevisionRef.current = nextAcknowledgedRevision;
    setAcknowledgedRevision(nextAcknowledgedRevision);
  }

  function acknowledgeCurrentEditorRevision() {
    acknowledgeEditorRevision(editorRevisionRef.current);
  }

  function updateInFlightRevision(revision: number | null) {
    inFlightRevisionRef.current = revision;
    setInFlightRevision(revision);
  }

  function updateAuthoredBody(nextBody: string) {
    if (bodyRef.current === nextBody) {
      return;
    }
    bodyRef.current = nextBody;
    setBody(nextBody);
    markDraftMutation();
  }

  // F2d: on-demand "Format now" — normalize the body via the server formatter
  // and mark the editor dirty so the change is saved like any other edit.
  async function handleFormatNow() {
    if (!workspaceId || formatting) {
      return;
    }
    const source = bodyRef.current;
    if (!source.trim()) {
      return;
    }
    setFormatting(true);
    try {
      const formatted = await normalizeMarkdownApi(workspaceId, source);
      // I8: the user kept typing during the round-trip — the buffer moved, so
      // don't overwrite their keystrokes with a stale-source format.
      if (bodyRef.current !== source) {
        return;
      }
      if (formatted === source) {
        setStatus(
          t("editor.formatNoChanges", {
            defaultValue: "Already formatted — no changes.",
          }),
        );
        return;
      }
      updateAuthoredBody(formatted);
    } catch {
      setStatus(
        t("editor.formatFailed", {
          defaultValue: "Could not format the document.",
        }),
      );
    } finally {
      setFormatting(false);
    }
  }

  useEffect(() => {
    isEditorMountedRef.current = true;
    return () => {
      isEditorMountedRef.current = false;
      clearAutoSaveTimer(autoSaveTimerRef);
      editorGenerationRef.current += 1;
      invalidateAllImageUploads(imageUploadOptions);
    };
  }, [imageUploadOptions]);

  useLayoutEffect(() => {
    const nextAuthorityKey = JSON.stringify([
      actorId,
      mode,
      slug,
      workspaceId,
      page?.id ?? null,
      page?.version_id ?? null,
    ]);
    pageIdRef.current = page?.id ?? null;
    baseVersionIdRef.current = page?.version_id ?? null;
    workspaceIdRef.current = workspaceId;
    if (uploadAuthorityKeyRef.current === null) {
      actorIdRef.current = actorId;
      uploadAuthorityKeyRef.current = nextAuthorityKey;
      return;
    }
    if (uploadAuthorityKeyRef.current === nextAuthorityKey) {
      return;
    }
    if (actorIdRef.current !== actorId) {
      actorSessionGenerationRef.current += 1;
      actorIdRef.current = actorId;
    }
    editorGenerationRef.current += 1;
    uploadAuthorityKeyRef.current = nextAuthorityKey;
    pageCreationPromiseRef.current = null;
    invalidateAllImageUploads(imageUploadOptions);
    if (editorViewRef.current) {
      clearEditorUploadSlots(editorViewRef.current);
    }
    setImageUploadEntries([]);
    setMediaContentUrls({});
  }, [
    actorId,
    imageUploadOptions,
    mode,
    page?.id,
    page?.version_id,
    slug,
    workspaceId,
  ]);

  useEffect(() => {
    if (focusMode) {
      document.body.dataset.focusMode = "true";
      setPropertiesOpen(false);
    } else {
      delete document.body.dataset.focusMode;
    }
    return () => {
      delete document.body.dataset.focusMode;
    };
  }, [focusMode]);

  useLayoutEffect(() => {
    const titleElement = titleRef.current;
    if (!titleElement) {
      return;
    }
    titleElement.style.height = "auto";
    const nextHeight = Math.min(
      Math.max(titleElement.scrollHeight, TITLE_TEXTAREA_MIN_HEIGHT),
      TITLE_TEXTAREA_MAX_HEIGHT,
    );
    titleElement.style.height = `${nextHeight}px`;
    titleElement.style.overflowY =
      titleElement.scrollHeight > TITLE_TEXTAREA_MAX_HEIGHT ? "auto" : "hidden";
  }, [title]);

  useEffect(() => {
    if (!propertiesOpen) {
      return;
    }

    function handlePointerDown(event: PointerEvent) {
      const target = event.target;
      if (!(target instanceof Node)) {
        return;
      }
      if (
        propertiesPopoverRef.current?.contains(target) ||
        propertiesButtonRef.current?.contains(target) ||
        propertiesDestinationMenuRef.current?.contains(target)
      ) {
        return;
      }
      setPropertiesOpen(false);
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key !== "Escape") {
        return;
      }
      const target = event.target;
      if (target instanceof Node && propertiesDestinationMenuRef.current?.contains(target)) {
        return;
      }
      event.preventDefault();
      setPropertiesOpen(false);
      propertiesButtonRef.current?.focus();
    }

    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown, true);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown, true);
    };
  }, [propertiesOpen]);

  useLayoutEffect(() => {
    if (!propertiesOpen) {
      setPropertiesPopoverPosition(null);
      return;
    }
    const reference = propertiesButtonRef.current;
    const floating = propertiesPopoverRef.current;
    if (!reference || !floating) {
      return;
    }
    let disposed = false;
    const update = () => {
      void computePosition(reference, floating, {
        middleware: [
          offset(8),
          flip({
            fallbackPlacements: ["bottom-start", "right-start"],
            padding: 12,
          }),
          shift({ padding: 12 }),
        ],
        placement: "bottom-end",
        strategy: "fixed",
      }).then(({ x, y }) => {
        if (!disposed) {
          setPropertiesPopoverPosition(clampFloatingElementPosition(x, y, floating));
        }
      });
    };
    const cleanup = autoUpdate(reference, floating, update);
    update();
    return () => {
      disposed = true;
      cleanup();
    };
  }, [propertiesOpen]);

  useEffect(() => {
    pagesRef.current = pages;
  }, [pages]);

  useEffect(() => {
    sourceSuggestionsRef.current = sourceSuggestions;
  }, [sourceSuggestions]);

  useEffect(() => {
    pageIdRef.current = page?.id ?? null;
    baseVersionIdRef.current = page?.version_id ?? null;
  }, [page]);

  useEffect(() => {
    if (!page?.version_id) {
      return;
    }
    // A version transition (new page, publish, or an alternative fork) makes
    // any cached upload content URL from the previous version no longer
    // authoritative -- callers fall back to contextMediaPageVersionContentUrl
    // for the current version instead.
    setMediaContentUrls({});
  }, [page?.version_id]);

  useEffect(() => {
    workspaceIdRef.current = workspaceId;
  }, [workspaceId]);

  useEffect(() => {
    destinationNodeIdRef.current = destinationNodeId;
  }, [destinationNodeId]);

  useEffect(() => {
    contextMediaCapabilitiesRef.current = contextMediaCapabilities;
  }, [contextMediaCapabilities]);

  useEffect(() => {
    if (conflict) {
      return;
    }
    setConflictDiff(null);
    setConflictDiffError(null);
    setConflictDiffLoading(false);
    setConflictDiffOpen(false);
  }, [conflict]);

  useEffect(() => {
    latestDraftRef.current = {
      aliases: pageMetadata.aliases,
      body,
      destinationNodeId,
      icon: pageMetadata.icon,
      pageType: pageMetadata.type,
      slug: pageSlug,
      tags: pageMetadata.tags,
      title,
    };
  }, [body, destinationNodeId, pageMetadata, pageSlug, title]);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setIsEditorReady(false);
      setPageLoadState("loading");
      setStatus(null);
      setConflict(null);
      setStalePublishState("idle");
      setSaveError(false);
      updateInFlightRevision(null);
      acknowledgedRevisionRef.current = editorRevisionRef.current;
      setAcknowledgedRevision(editorRevisionRef.current);
      setTitleValidationRequested(false);
      setPage(null);
      pageIdRef.current = null;
      pageCreationPromiseRef.current = null;
      contextMediaCapabilitiesRef.current = PENDING_CONTEXT_MEDIA_CAPABILITIES;
      mediaUploadCapabilityCurrentRef.current = false;
      setContextMediaCapabilities(PENDING_CONTEXT_MEDIA_CAPABILITIES);
      setMediaUploadCapabilityState("loading");
      setImageUploadBatches([]);
      setMediaContentUrls({});
      setMediaManagerOpen(false);
      workspaceIdRef.current = null;
      setWorkspaceId(null);
      setWorkspaceSlug(null);
      setEditorPreferenceKey(null);
      setSourceSuggestions([]);
      setTagQuery("");
      setTagSuggestions([]);
      navigationConfirmedRef.current = false;
      slugTouchedRef.current = mode === "edit";
      setTitle("");
      setPageSlug(mode === "edit" ? slug : "");
      bodyRef.current = "";
      setBody("");
      resetPageMetadata([]);
      setDestinationNodeId("");
      setPublicationState(mode === "create" ? "never-published" : "published");
      setFencePublishPrompt(null);
      reviewEditCompletedRef.current = false;
      lastSavedRef.current = emptyDraft(mode, slug);
      const workspaceResponse = await listWorkspaces();
      const selectedWorkspaceId = resolveSelectedWorkspaceId(workspaceResponse.data);
      const selectedWorkspace =
        workspaceResponse.data.workspaces.find(
          (workspace) => workspace.id === selectedWorkspaceId,
        ) ?? null;
      const selectedWorkspaceSlug = selectedWorkspace?.slug ?? null;
      setWorkspaceVisibility(selectedWorkspace?.visibility ?? "private");
      setWorkspaceRole(selectedWorkspace?.role ?? null);
      const [listResult, treeResult, sourceResult, mediaCapabilitiesResult] = await Promise.all([
        selectedWorkspaceId
          ? listPages(selectedWorkspaceId)
          : Promise.resolve({ data: [] as PageSummary[], next_cursor: null }),
        selectedWorkspaceId
          ? listTree(selectedWorkspaceId, "context")
          : Promise.resolve([] as TreeNodePayload[]),
        selectedWorkspaceId
          ? loadSourceSuggestions(selectedWorkspaceId)
          : Promise.resolve({ sources: [] as LibraryItemSummary[], warning: null }),
        selectedWorkspaceId
          ? getContextMediaCapabilities(selectedWorkspaceId)
              .then((capabilities) => ({
                capabilities,
                current: contextMediaCapabilitiesAreCurrent(capabilities),
              }))
              .catch(() => ({
                capabilities: PENDING_CONTEXT_MEDIA_CAPABILITIES,
                current: false,
              }))
          : Promise.resolve({
              capabilities: PENDING_CONTEXT_MEDIA_CAPABILITIES,
              current: false,
            }),
      ]);
      if (cancelled) return;
      const contextNodes = Array.isArray(treeResult) ? treeResult : [];
      const sources = sourceResult.sources;
      const mediaCapabilities = mediaCapabilitiesResult.capabilities;
      if (mode === "edit" && selectedWorkspaceSlug) {
        const preferenceKey = `dtxwiki:editor:${selectedWorkspaceSlug}:${slug}`;
        const preference = readEditorPreference(preferenceKey);
        setEditorMode(preference.mode);
        setSplitPercent(preference.splitPercent);
        setEditorPreferenceKey(preferenceKey);
      }
      pagesRef.current = listResult.data;
      sourceSuggestionsRef.current = sources;
      workspaceIdRef.current = selectedWorkspaceId;
      contextMediaCapabilitiesRef.current = mediaCapabilities;
      mediaUploadCapabilityCurrentRef.current = mediaCapabilitiesResult.current;
      setWorkspaceSlug(selectedWorkspaceSlug);
      setWorkspaceId(selectedWorkspaceId);
      setContextMediaCapabilities(mediaCapabilities);
      setMediaUploadCapabilityState(
        mediaCapabilitiesResult.current ? "ready" : "error",
      );
      setPages(listResult.data);
      setSourceSuggestions(sources);
      if (sourceResult.warning) {
        setStatus(sourceResult.warning);
      }
      setContextTree(contextNodes);
      if (mode === "create") {
        sessionVersionIdRef.current = null;
        const defaultDestination = chooseCreateDestination(
          contextNodes,
          createParentNodeId,
        );
        // Expand only the resolved destination's ancestor chain — the path the
        // new page actually lands in — collapsing the rest. Keying off the
        // resolved destination (not the raw parentNodeId param) keeps the
        // selected path open even when the param is absent or invalid and we
        // fall back to a default destination (item 23 follow-up).
        setContextExpandedNodeIds(
          ancestorPathNodeIds(contextNodes, defaultDestination),
        );
        setTitle(createInitialTitle);
        setPageSlug(createInitialTitle ? slugify(createInitialTitle) : "");
        setDestinationNodeId(defaultDestination);
        if (createInitialTitle) {
          markDraftMutation();
        }
        lastSavedRef.current = {
          aliases: [],
          body: "",
          destinationNodeId: defaultDestination,
          icon: DEFAULT_PAGE_METADATA.icon,
          pageType: DEFAULT_PAGE_METADATA.type,
          slug: "",
          tags: [],
          title: "",
        };
      }
      if (mode === "edit") {
        const loaded = selectedWorkspaceId
          ? await readPageBySlug(selectedWorkspaceId, slug)
          : null;
        if (cancelled) return;
        if (!loaded) {
          setPageLoadState("not_found");
          setStatus(translationRef.current("editor.notFound"));
          setIsEditorReady(true);
          return;
        }
        const parsed = parsePageDocument(loaded.body_md, {
          aliases: loaded.aliases,
          tags: loaded.tags,
          type: loaded.page_type as PageType,
        });
        const metadata = metadataFromPagePayload(parsed.metadata, loaded);
        const loadedDestination =
          contextNodes.find((node) => node.target_id === loaded.id)?.parent_id ??
          chooseDefaultDestination(contextNodes);
        const restoredPublication = readPagePublicationState(loaded);
        if (cancelled) return;
        // Expand the edited page's actual path so the tree opens to it. The
        // create-only parentNodeId param is null in edit mode, which previously
        // left ancestorPathNodeIds empty and collapsed the selected path
        // (item 23 follow-up).
        setContextExpandedNodeIds(
          ancestorPathNodeIds(contextNodes, loadedDestination),
        );
        applyParsedMetadata(metadata);
        sessionVersionIdRef.current = restoredPublication.sessionVersionId;
        setPublicationState(restoredPublication.state);
        pageIdRef.current = loaded.id;
        setPage(loaded);
        setTitle(loaded.title);
        setPageSlug(loaded.slug);
        bodyRef.current = parsed.body;
        setBody(parsed.body);
        setDestinationNodeId(loadedDestination);
        lastSavedRef.current = {
          aliases: metadata.aliases,
          body: parsed.body,
          destinationNodeId: loadedDestination,
          icon: metadata.icon,
          pageType: metadata.type,
          slug: loaded.slug,
          tags: metadata.tags,
          title: loaded.title,
        };
        lastSavedFingerprintRef.current = draftSaveFingerprint(
          lastSavedRef.current,
          frontmatterExtraLinesRef.current,
        );
        acknowledgedRevisionRef.current = editorRevisionRef.current;
        setAcknowledgedRevision(editorRevisionRef.current);
      }
      setPageLoadState("ready");
      setIsEditorReady(true);
    }
    load().catch(() => {
      if (!cancelled) {
        setPageLoadState("error");
        // Never expose a cleared editor after a failed page read: it looks like
        // a valid empty page and lets a user overwrite content they never saw.
        // The dedicated error branch below is intentionally non-editable and
        // uses localized copy rather than a raw server message.
        setStatus(null);
        setIsEditorReady(true);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [createInitialTitle, createParentNodeId, mode, slug]);

  useEffect(() => {
    if (mode === "create") {
      setEditorMode("source");
    }
  }, [mode]);

  useEffect(() => {
    function updateResponsiveViewports() {
      setSinglePaneViewport(isSinglePaneEditorViewport());
      setStackedSplitViewport(isStackedSplitEditorViewport());
      setCompactActionBarViewport(isCompactActionBarViewport());
      setContextRailDrawerViewport(isContextRailDrawerViewport());
    }
    updateResponsiveViewports();
    window.addEventListener("resize", updateResponsiveViewports);
    return () => window.removeEventListener("resize", updateResponsiveViewports);
  }, []);

  useEffect(() => {
    if (!contextRailDrawerViewport || focusMode) {
      closeContextRail();
    }
  }, [closeContextRail, contextRailDrawerViewport, focusMode]);

  useMobileModalFocus({
    active: contextRailDrawerViewport && contextRailOpen && !focusMode,
    containerRef: contextRailRef,
    onClose: closeContextRail,
    restoreFocusRef: contextRailToggleRef,
  });

  useEffect(() => {
    if (mode === "create" || !editorPreferenceKey) {
      return;
    }
    writeEditorPreference(editorPreferenceKey, {
      mode: editorMode,
      splitPercent,
    });
  }, [editorMode, editorPreferenceKey, mode, splitPercent]);

  useEffect(() => {
    if (blockMenuOpen) {
      window.setTimeout(() => blockMenuSearchRef.current?.focus(), 0);
    }
  }, [blockMenuOpen]);

  useEffect(() => {
    const query = tagQuery.trim();
    if (!query || !workspaceId) {
      setTagSuggestions([]);
      return;
    }
    let cancelled = false;
    listPageTags(workspaceId, query, 12)
      .then((tags) => {
        if (!cancelled) {
          setTagSuggestions(tags);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setTagSuggestions([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [tagQuery, workspaceId]);

  useLayoutEffect(() => {
    if (!blockMenuOpen) {
      setBlockMenuPosition(null);
      return;
    }
    const view = editorViewRef.current;
    const floating = blockMenuRef.current;
    if (!view || !floating) {
      return;
    }
    const reference = blockMenuReferenceElement(view, blockMenuRangeRef.current);
    if (!reference) {
      return;
    }
    let disposed = false;
    const update = () => {
      void computePosition(reference, floating, {
        middleware: [
          offset(8),
          flip({ padding: INLINE_TOOLBAR_MARGIN }),
          shift({ padding: INLINE_TOOLBAR_MARGIN }),
        ],
        placement: "bottom-start",
        strategy: "fixed",
      }).then(({ x, y }) => {
        if (!disposed) {
          setBlockMenuPosition(clampFloatingElementPosition(x, y, floating));
        }
      });
    };
    const cleanup = autoUpdate(reference, floating, update);
    update();
    return () => {
      disposed = true;
      cleanup();
    };
  }, [blockMenuOpen, blockMenuQuery, filteredBlockEntries.length]);

  useEffect(() => {
    setHighlightedBlockIndex(0);
  }, [blockMenuQuery]);

  useEffect(() => {
    if (highlightedBlockIndex >= filteredBlockEntries.length) {
      setHighlightedBlockIndex(Math.max(0, filteredBlockEntries.length - 1));
    }
  }, [filteredBlockEntries.length, highlightedBlockIndex]);

  useEffect(() => {
    if (activeEditorMode !== "source" && blockMenuOpen) {
      closeBlockMenu();
    }
    if (activeEditorMode !== "source" && emojiPicker) {
      closeEmojiPicker();
    }
  }, [activeEditorMode, blockMenuOpen, emojiPicker]);

  useEffect(() => {
    if (activeEditorMode !== "source") {
      hideInlineToolbar();
      setActiveCommandStates({});
      closeAiInlineEdit();
    }
  }, [activeEditorMode]);

  useEffect(() => {
    hideInlineToolbar();
    setActiveCommandStates({});
    closeLinkDialog();
    closeCommentPreview();
    closeAiInlineEdit();
    closeEmojiPicker();
  }, [activeEditorMode, focusMode]);

  // The DOM side of linked scrolling only owns cache invalidation, scheduling,
  // and echo recognition. Line/offset table construction and interpolation are
  // pure functions in editorScrollMapping.ts.
  useEffect(() => {
    if (activeEditorMode !== "split" || !isEditorReady || !linkedScrolling) {
      syncPreviewToCaretRef.current = () => {};
      return;
    }
    const view = editorViewRef.current;
    const sourceEl = view?.scrollDOM ?? null;
    const previewEl = previewPaneRef.current;
    if (!view || !sourceEl || !previewEl) {
      return;
    }

    let cachedAnchors: ScrollAnchor[] | null = null;
    let scheduledFrame: number | null = null;
    let pendingSync: { pane: "source" | "preview" | "caret"; line?: number } | null = null;
    let expectedSourceScroll: { top: number; expiresAt: number } | null = null;
    let expectedPreviewScroll: { top: number; expiresAt: number } | null = null;
    let lastActivePane: "source" | "preview" = "source";
    let geometryAuthority: {
      pane: "source" | "preview";
      expiresAt: number;
    } | null = null;
    let active = true;

    function invalidateAnchors() {
      cachedAnchors = null;
      if (active && pendingSync === null) {
        geometryAuthority = {
          pane: lastActivePane,
          expiresAt: performance.now() + 250,
        };
        scheduleSync(lastActivePane);
      }
    }

    function previewAnchors() {
      if (cachedAnchors) {
        return cachedAnchors;
      }
      const previewRect = previewEl!.getBoundingClientRect();
      const spans: ScrollAnchorSpan[] = [];
      for (const element of previewEl!.querySelectorAll<HTMLElement>(
        '[data-scroll-sync-anchor="true"]',
      )) {
        const startLine = Number(element.dataset.sourceLine);
        const endLine = Number(element.dataset.sourceEndLine ?? startLine);
        if (!Number.isFinite(startLine) || !Number.isFinite(endLine)) {
          continue;
        }
        const rect = element.getBoundingClientRect();
        spans.push({
          startLine,
          endLine,
          startOffset:
            rect.top - previewRect.top - previewEl!.clientTop + previewEl!.scrollTop,
          endOffset:
            rect.bottom - previewRect.top - previewEl!.clientTop + previewEl!.scrollTop,
        });
      }
      cachedAnchors = buildAnchorTable(spans, {
        firstLine: 1,
        lastLine: view!.state.doc.lines + 1,
        topOffset: 0,
        bottomOffset: previewEl!.scrollHeight,
      });
      return cachedAnchors;
    }

    function programmaticScroll(
      pane: "source" | "preview",
      element: HTMLElement,
      target: number,
    ) {
      const previous = element.scrollTop;
      element.scrollTop = target;
      const landed = element.scrollTop;
      const expected = Math.abs(landed - previous) > 0.01
        ? { top: landed, expiresAt: performance.now() + 160 }
        : null;
      if (pane === "source") {
        expectedSourceScroll = expected;
      } else {
        expectedPreviewScroll = expected;
      }
    }

    function isExpectedEcho(pane: "source" | "preview", actual: number) {
      const expected = pane === "source"
        ? expectedSourceScroll
        : expectedPreviewScroll;
      if (!expected || performance.now() > expected.expiresAt) {
        if (pane === "source") {
          expectedSourceScroll = null;
        } else {
          expectedPreviewScroll = null;
        }
        return false;
      }
      if (Math.abs(actual - expected.top) <= 1) {
        return true;
      }
      if (pane === "source") {
        expectedSourceScroll = null;
      } else {
        expectedPreviewScroll = null;
      }
      return false;
    }

    function claimPane(pane: "source" | "preview") {
      lastActivePane = pane;
      geometryAuthority = null;
      if (pane === "source") {
        expectedSourceScroll = null;
      } else {
        expectedPreviewScroll = null;
      }
    }

    function runPendingSync() {
      const pending = pendingSync;
      pendingSync = null;
      if (!pending) {
        return;
      }
      const anchors = previewAnchors();
      if (pending.pane === "source") {
        const anchor = viewportAnchor(
          sourceEl!.scrollTop,
          sourceEl!.clientHeight,
          sourceEl!.scrollHeight,
        );
        const sourceLine = sourceOffsetToLine(
          view!,
          sourceEl!,
          anchor.contentOffset,
        );
        const previewOffset = lineToOffset(sourceLine, anchors);
        programmaticScroll(
          "preview",
          previewEl!,
          scrollTopForAnchor(
            previewOffset,
            previewEl!.clientHeight,
            previewEl!.scrollHeight,
          ),
        );
        return;
      }
      if (pending.pane === "preview") {
        const anchor = viewportAnchor(
          previewEl!.scrollTop,
          previewEl!.clientHeight,
          previewEl!.scrollHeight,
        );
        const sourceLine = offsetToLine(anchor.contentOffset, anchors);
        const sourceOffset = sourceLineToOffset(view!, sourceEl!, sourceLine);
        programmaticScroll(
          "source",
          sourceEl!,
          scrollTopForAnchor(
            sourceOffset,
            sourceEl!.clientHeight,
            sourceEl!.scrollHeight,
          ),
        );
        return;
      }
      const target = lineToOffset(pending.line ?? 1, anchors);
      const top = previewEl!.scrollTop;
      const bottom = top + previewEl!.clientHeight;
      if (target < top + 24 || target > bottom - 48) {
        programmaticScroll(
          "preview",
          previewEl!,
          Math.max(0, target - previewEl!.clientHeight * 0.25),
        );
      }
    }

    function scheduleSync(pane: "source" | "preview" | "caret", line?: number) {
      pendingSync = { pane, line };
      if (scheduledFrame !== null) {
        return;
      }
      scheduledFrame = requestAnimationFrame(() => {
        scheduledFrame = null;
        runPendingSync();
      });
    }

    function handleSourceScroll() {
      if (isExpectedEcho("source", sourceEl!.scrollTop)) {
        return;
      }
      if (
        geometryAuthority?.pane === "preview" &&
        performance.now() <= geometryAuthority.expiresAt
      ) {
        return;
      }
      claimPane("source");
      scheduleSync("source");
    }

    function handlePreviewScroll() {
      if (isExpectedEcho("preview", previewEl!.scrollTop)) {
        return;
      }
      if (
        geometryAuthority?.pane === "source" &&
        performance.now() <= geometryAuthority.expiresAt
      ) {
        return;
      }
      claimPane("preview");
      scheduleSync("preview");
    }

    function handleSourceIntent() {
      claimPane("source");
      scheduleSync("source");
    }

    function handlePreviewIntent() {
      claimPane("preview");
      scheduleSync("preview");
    }

    sourceEl.addEventListener("scroll", handleSourceScroll, { passive: true });
    previewEl.addEventListener("scroll", handlePreviewScroll, { passive: true });
    for (const eventName of ["wheel", "pointerdown", "touchstart", "focusin"]) {
      sourceEl.addEventListener(eventName, handleSourceIntent, { passive: true });
      previewEl.addEventListener(eventName, handlePreviewIntent, { passive: true });
    }
    sourceEl.addEventListener("keydown", handleSourceIntent, true);
    previewEl.addEventListener("keydown", handlePreviewIntent, true);

    syncPreviewToCaretRef.current = (caretView) => {
      claimPane("source");
      const caretLine = caretView.state.doc.lineAt(
        caretView.state.selection.main.head,
      ).number;
      scheduleSync("caret", caretLine);
    };

    const resizeObserver =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(invalidateAnchors);
    resizeObserver?.observe(sourceEl);
    resizeObserver?.observe(view.contentDOM);
    resizeObserver?.observe(previewEl);
    const previewContent = previewEl.querySelector<HTMLElement>(".markdown-body");
    if (previewContent) {
      resizeObserver?.observe(previewContent);
    }

    function handlePreviewLoad(event: Event) {
      if (event.target instanceof HTMLImageElement) {
        invalidateAnchors();
      }
    }
    previewEl.addEventListener("load", handlePreviewLoad, true);
    previewEl.addEventListener("error", handlePreviewLoad, true);
    previewEl.addEventListener(
      MARKDOWN_GEOMETRY_CHANGE_EVENT,
      invalidateAnchors,
    );

    const fonts = document.fonts;
    void fonts?.ready.then(() => {
      if (active) {
        invalidateAnchors();
      }
    });
    fonts?.addEventListener?.("loadingdone", invalidateAnchors);
    scheduleSync("source");

    return () => {
      active = false;
      sourceEl.removeEventListener("scroll", handleSourceScroll);
      previewEl.removeEventListener("scroll", handlePreviewScroll);
      for (const eventName of ["wheel", "pointerdown", "touchstart", "focusin"]) {
        sourceEl.removeEventListener(eventName, handleSourceIntent);
        previewEl.removeEventListener(eventName, handlePreviewIntent);
      }
      sourceEl.removeEventListener("keydown", handleSourceIntent, true);
      previewEl.removeEventListener("keydown", handlePreviewIntent, true);
      previewEl.removeEventListener("load", handlePreviewLoad, true);
      previewEl.removeEventListener("error", handlePreviewLoad, true);
      previewEl.removeEventListener(
        MARKDOWN_GEOMETRY_CHANGE_EVENT,
        invalidateAnchors,
      );
      resizeObserver?.disconnect();
      fonts?.removeEventListener?.("loadingdone", invalidateAnchors);
      syncPreviewToCaretRef.current = () => {};
      if (scheduledFrame !== null) {
        cancelAnimationFrame(scheduledFrame);
      }
    };
  }, [activeEditorMode, isEditorReady, linkedScrolling]);

  useEffect(() => {
    if (!linkDialog) {
      return;
    }
    const focusTimer = window.setTimeout(() => {
      if (linkDialog.text.trim()) {
        linkUrlInputRef.current?.focus();
        return;
      }
      linkTextInputRef.current?.focus();
    }, 0);
    function handleEscape(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeLinkDialog({ restoreFocus: true });
      }
    }
    document.addEventListener("keydown", handleEscape, true);
    return () => {
      window.clearTimeout(focusTimer);
      document.removeEventListener("keydown", handleEscape, true);
    };
    // Opening chooses the initial field once. Depending on the whole dialog
    // state would steal focus on every keystroke.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isLinkDialogOpen]);

  useEffect(() => {
    if (!isLinkDialogOpen) {
      return;
    }
    const popover = linkPopoverRef.current;
    if (!popover) {
      return;
    }
    let scheduledFrame: number | null = null;
    const fitToVisualViewport = () => {
      scheduledFrame = null;
      setLinkDialog((current) => {
        if (!current) {
          return current;
        }
        const next = clampFloatingElementPosition(
          current.left,
          current.top,
          popover,
        );
        if (next.left === current.left && next.top === current.top) {
          return current;
        }
        return { ...current, ...next };
      });
    };
    const scheduleFit = () => {
      if (scheduledFrame === null) {
        scheduledFrame = window.requestAnimationFrame(fitToVisualViewport);
      }
    };
    const visualViewport = window.visualViewport;
    const resizeObserver = typeof ResizeObserver === "undefined"
      ? null
      : new ResizeObserver(scheduleFit);
    resizeObserver?.observe(popover);
    window.addEventListener("resize", scheduleFit);
    visualViewport?.addEventListener("resize", scheduleFit);
    visualViewport?.addEventListener("scroll", scheduleFit);
    scheduleFit();
    return () => {
      resizeObserver?.disconnect();
      window.removeEventListener("resize", scheduleFit);
      visualViewport?.removeEventListener("resize", scheduleFit);
      visualViewport?.removeEventListener("scroll", scheduleFit);
      if (scheduledFrame !== null) {
        window.cancelAnimationFrame(scheduledFrame);
      }
    };
  }, [isLinkDialogOpen]);

  useEffect(() => {
    if (!isCommentPreviewOpen) {
      return;
    }
    const focusTimer = window.setTimeout(() => commentNoteRef.current?.focus(), 0);
    function handleEscape(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeCommentPreview({ restoreFocus: true });
      }
    }
    document.addEventListener("keydown", handleEscape, true);
    return () => {
      window.clearTimeout(focusTimer);
      document.removeEventListener("keydown", handleEscape, true);
    };
  }, [isCommentPreviewOpen]);

  useEffect(() => {
    if (!aiInline) {
      return;
    }
    function handleEscape(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeAiInlineEdit({ restoreFocus: true });
      }
    }
    document.addEventListener("keydown", handleEscape, true);
    return () => {
      document.removeEventListener("keydown", handleEscape, true);
    };
  }, [aiInline]);

  useEffect(() => {
    if (!inlineToolbar.visible && !isLinkDialogOpen && !isCommentPreviewOpen && !aiInline) {
      return;
    }
    function handleDocumentPointerDown(event: PointerEvent) {
      const target = event.target instanceof Element ? event.target : null;
      if (!target) {
        return;
      }
      if (
        target.closest(".source-pane") ||
        target.closest(".editor-inline-toolbar") ||
        target.closest(".editor-link-popover") ||
        target.closest(".editor-comment-popover") ||
        target.closest(".editor-ai-popover")
      ) {
        return;
      }
      hideInlineToolbar();
      closeLinkDialog();
      closeCommentPreview();
      closeAiInlineEdit();
    }
    function handleDocumentFocusIn(event: FocusEvent) {
      const target = event.target instanceof Element ? event.target : null;
      if (!target) {
        return;
      }
      if (
        target.closest(".source-pane") ||
        target.closest(".editor-inline-toolbar") ||
        target.closest(".editor-link-popover") ||
        target.closest(".editor-comment-popover") ||
        target.closest(".editor-ai-popover")
      ) {
        return;
      }
      hideInlineToolbar();
      closeLinkDialog();
      closeCommentPreview();
      closeAiInlineEdit();
    }
    document.addEventListener("pointerdown", handleDocumentPointerDown, true);
    document.addEventListener("focusin", handleDocumentFocusIn, true);
    return () => {
      document.removeEventListener("pointerdown", handleDocumentPointerDown, true);
      document.removeEventListener("focusin", handleDocumentFocusIn, true);
    };
  }, [aiInline, inlineToolbar.visible, isCommentPreviewOpen, isLinkDialogOpen]);

  // `page.updated_at` is the published/visible instant and does not move on
  // an in-place draft autosave (PUB-06); `draft_saved_at` is the checkpoint
  // instant for that case. Every consumer of "when was this last saved" in
  // the editor reads this derived value instead of `page.updated_at`
  // directly, so the "Saved just now" affordance and its refresh timer track
  // autosaves the same way the meta line and SyncPill do.
  const lastSavedAt = page?.draft_saved_at ?? page?.updated_at ?? null;
  // L-2 (260908 UI audit): shared with SyncPill's own "Saved just now" check
  // so the meta line and the pill agree on when the redundancy window ends.
  const savedJustNow = isRecentTimestamp(lastSavedAt, saveRelativeNow);

  useEffect(() => {
    const refreshDelay = getSavedRelativeTimeRefreshDelay(lastSavedAt, saveRelativeNow);
    if (refreshDelay === null) {
      return;
    }
    const timer = window.setTimeout(() => setSaveRelativeNow(Date.now()), refreshDelay);
    return () => window.clearTimeout(timer);
  }, [lastSavedAt, saveRelativeNow]);

  useEffect(() => {
    if (
      !isEditorReady ||
      mode !== "edit" ||
      !hasUnsavedChanges ||
      !canSave ||
      Boolean(conflict) ||
      saveError
    ) {
      clearAutoSaveTimer(autoSaveTimerRef);
      return;
    }
    // A dirty period owns one deadline. Revision changes re-run this effect so
    // edits made during an in-flight save can start their own checkpoint, but
    // an already-armed timer is never replaced, so typing cannot postpone it.
    if (autoSaveTimerRef.current === null) {
      scheduleAutosave(autoSaveTimerRef, autosaveCheckpointMs, () => {
        saveRef.current({ source: "auto" });
      });
    }
  }, [
    autosaveCheckpointMs,
    canSave,
    conflict,
    editorRevision,
    hasUnsavedChanges,
    isEditorReady,
    saveError,
    mode,
  ]);

  // Narrow the effect deps to the three fields we actually read. `useBlocker`
  // returns a fresh object identity on every render that touches relevant
  // state, so depending on the whole blocker re-fires the effect even when
  // the blocker state has not changed (FP-C3).
  useEffect(() => {
    if (navigationBlocker.state !== "blocked") {
      return;
    }
    if (window.confirm(dismissalRiskMessage)) {
      autosaveBackstopRef.current({ allowInFlight: true });
      saveRef.current({ source: "auto" });
      navigationConfirmedRef.current = true;
      navigationBlocker.proceed();
      return;
    }
    navigationBlocker.reset();
  // `useBlocker` returns a new wrapper object while the three members below
  // remain the behavioral contract used by this effect.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    dismissalRiskMessage,
    navigationBlocker.state,
    navigationBlocker.proceed,
    navigationBlocker.reset,
  ]);

  useEffect(() => {
    // beforeunload covers refresh, close-tab, address-bar navigation, and
    // full-page programmatic navigation. useBlocker only fires on React Router
    // navigation. DO NOT remove without an explicit replacement for these
    // paths. pagehide is registered here too so one dismissal shares one
    // latch -- a browser fires beforeunload then pagehide for the same
    // dismissal, and the backstop never updates lastSavedFingerprintRef, so
    // it cannot dedupe itself the way save() does.
    function flushDismissalAutosave() {
      const fingerprint = draftSaveFingerprint(
        latestDraftRef.current,
        frontmatterExtraLinesRef.current,
      );
      if (dismissalAutosaveFingerprintRef.current === fingerprint) {
        return;
      }
      if (autosaveBackstopRef.current({ allowInFlight: true })) {
        dismissalAutosaveFingerprintRef.current = fingerprint;
      }
    }
    function handleBeforeUnload(event: BeforeUnloadEvent) {
      if (!hasDismissalRisk) {
        return;
      }
      flushDismissalAutosave();
      event.preventDefault();
      event.returnValue = "";
    }
    window.addEventListener("beforeunload", handleBeforeUnload);
    window.addEventListener("pagehide", flushDismissalAutosave);
    return () => {
      window.removeEventListener("beforeunload", handleBeforeUnload);
      window.removeEventListener("pagehide", flushDismissalAutosave);
    };
  }, [
    hasDismissalRisk,
  ]);

  useEffect(() => {
    // Keep this with useBlocker: same-origin same-tab plain <a href> clicks can
    // bypass useNavigate/<Link>. DO NOT remove without an explicit replacement
    // for markdown-rendered or server-rendered anchor navigation.
    function handleDocumentLinkClick(event: MouseEvent) {
      if (
        !hasDismissalRisk ||
        navigationConfirmedRef.current ||
        event.defaultPrevented ||
        event.button !== 0 ||
        event.metaKey ||
        event.ctrlKey ||
        event.shiftKey ||
        event.altKey
      ) {
        return;
      }
      const target = event.target instanceof Element ? event.target : null;
      const anchor = target?.closest<HTMLAnchorElement>("a[href]");
      if (!anchor || anchor.hasAttribute("download")) {
        return;
      }
      const anchorTarget =
        anchor.getAttribute("target") ||
        document.querySelector("base[target]")?.getAttribute("target") ||
        "";
      if (anchorTarget && anchorTarget !== "_self") {
        return;
      }
      const href = anchor.getAttribute("href");
      if (!href || href.startsWith("#") || href.startsWith("mailto:") || href.startsWith("tel:")) {
        return;
      }
      const url = new URL(anchor.href, window.location.href);
      if (url.origin !== window.location.origin) {
        return;
      }
      if (window.confirm(dismissalRiskMessage)) {
        autosaveBackstopRef.current({ allowInFlight: true });
        saveRef.current({ source: "auto" });
        navigationConfirmedRef.current = true;
        return;
      }
      event.preventDefault();
      event.stopPropagation();
    }
    document.addEventListener("click", handleDocumentLinkClick, true);
    return () => document.removeEventListener("click", handleDocumentLinkClick, true);
  }, [dismissalRiskMessage, hasDismissalRisk]);

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (shortcutHelpOpen && event.key === "Escape") {
        event.preventDefault();
        setShortcutHelpOpen(false);
        return;
      }
      if (
        event.key === "?" &&
        !event.metaKey &&
        !event.ctrlKey &&
        !event.altKey &&
        !isEditableShortcutTarget(event.target) &&
        !hasTextSelection()
      ) {
        event.preventDefault();
        setShortcutHelpOpen(true);
        return;
      }
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        saveRef.current({ source: "manual" });
        return;
      }
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "e") {
        event.preventDefault();
        setEditorMode((current) => nextEditorMode(current, singlePaneViewport));
        return;
      }
      if (
        (event.metaKey || event.ctrlKey) &&
        !event.shiftKey &&
        event.key === "\\"
      ) {
        event.preventDefault();
        if (!singlePaneViewport) {
          setEditorMode((current) => (current === "split" ? "source" : "split"));
        }
        return;
      }
      if (
        (event.metaKey || event.ctrlKey) &&
        event.shiftKey &&
        event.key.toLowerCase() === "o"
      ) {
        event.preventDefault();
        setFocusMode((current) => !current);
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [shortcutHelpOpen, singlePaneViewport]);

  useEffect(() => {
    function flushCheckpoint() {
      saveRef.current({ source: "auto" });
    }
    function handleVisibilityChange() {
      if (document.visibilityState === "hidden") {
        flushCheckpoint();
      }
    }
    function handleCompositionStart() {
      isComposingRef.current = true;
    }
    function handleCompositionEnd() {
      isComposingRef.current = false;
      if (pendingPublishRef.current) {
        const queuedPublish = pendingPublishRef.current;
        pendingPublishRef.current = null;
        pendingAutosaveRef.current = false;
        saveRef.current(queuedPublish);
        return;
      }
      if (pendingAutosaveRef.current) {
        pendingAutosaveRef.current = false;
        flushCheckpoint();
      }
    }
    // pagehide is registered on the beforeunload effect instead (PUB-08):
    // one dismissal fires both events, and only that effect's latch can
    // dedupe them.
    document.addEventListener("visibilitychange", handleVisibilityChange);
    document.addEventListener("compositionstart", handleCompositionStart, true);
    document.addEventListener("compositionend", handleCompositionEnd, true);
    return () => {
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      document.removeEventListener("compositionstart", handleCompositionStart, true);
      document.removeEventListener("compositionend", handleCompositionEnd, true);
    };
  }, []);

  async function completeReviewEditIfNeeded(
    source: SaveOptions["source"],
    requestId: number,
  ): Promise<ContextPublishNotice | null> {
    if (
      source === "auto" ||
      !fromReview ||
      !workspaceId ||
      !reviewItemId ||
      reviewEditCompletedRef.current
    ) {
      return null;
    }
    try {
      await decideReview(
        workspaceId,
        reviewItemId,
        "approve",
        "Edited directly in Page editor.",
      );
      if (!isCurrentSaveRequest(requestId, saveRequestIdRef)) return null;
      if (!isEditorMountedRef.current) return null;
      reviewEditCompletedRef.current = true;
      setStatus(
        t("editor.publishedReviewCompleted", {
          defaultValue: "Published and completed the review request.",
        }),
      );
      return { kind: "review_completed" };
    } catch (exc) {
      if (!isCurrentSaveRequest(requestId, saveRequestIdRef)) return null;
      if (!isEditorMountedRef.current) return null;
      const error = apiErrorMessage(
        exc,
        t("editor.unknownError", { defaultValue: "unknown error" }),
      );
      setStatus(
        t("editor.publishedReviewFailed", {
          defaultValue: "Published, but review completion failed: {{error}}",
          error,
        }),
      );
      return { kind: "review_failed", error };
    }
  }

  function publishNotice(
    warnings: { code: string; message: string }[],
    reviewNotice: ContextPublishNotice | null,
    reviewSuperseded = false,
  ): ContextPublishNotice {
    if (reviewNotice) {
      return reviewNotice;
    }
    if (reviewSuperseded) {
      return { kind: "review_superseded" };
    }
    if (warnings.some((warning) => warning.code === "published_with_findings")) {
      return { kind: "published_with_findings" };
    }
    return { kind: "published" };
  }

  function navigateToPublishedContext(
    pageId: string,
    notice: ContextPublishNotice,
    requestId: number,
  ) {
    if (!isCurrentSaveRequest(requestId, saveRequestIdRef)) return;
    if (!isEditorMountedRef.current) return;
    if (editorRevisionRef.current > acknowledgedRevisionRef.current) {
      setStatus(
        t("editor.publishedNewerEdits", {
          defaultValue:
            "An earlier version was published. Your newer changes remain in the editor.",
        }),
      );
      return;
    }
    const params = new URLSearchParams({ [TREE_TARGET_PARAM]: pageId });
    navigationConfirmedRef.current = true;
    navigate(`/context?${params.toString()}`, {
      replace: true,
      state: { contextPublishNotice: notice },
    });
  }

  async function save(options: SaveOptions = {}) {
    const source = options.source ?? "manual";
    const submittedBody = options.bodyOverride ?? body;
    if (options.publish && stalePublishState !== "idle") {
      return;
    }
    if (isComposingRef.current) {
      clearAutoSaveTimer(autoSaveTimerRef);
      if (source === "auto") {
        pendingAutosaveRef.current = true;
      } else {
        pendingPublishRef.current = { ...options, source };
      }
      return;
    }
    if (source !== "auto") {
      setTitleValidationRequested(true);
    }
    if (source === "manual" && options.publish && !options.skipFenceCheck) {
      const finding = findUnclosedCodeFence(submittedBody);
      if (finding) {
        setFencePublishPrompt({
          closingFence: finding.closingFence,
          message: t("editor.unclosedFenceMessage", {
            defaultValue: "Unclosed code fence opened at line {{line}}.",
            line: finding.line,
          }),
        });
        return;
      }
    }
    if (!canSave || conflict) {
      if (!conflict && source !== "auto" && saveBlockedReason) {
        if (missingTitle) {
          titleRef.current?.focus();
        } else {
          setStatus(saveBlockedReason);
        }
      }
      return;
    }
    if (!workspaceId) {
      return;
    }
    clearAutoSaveTimer(autoSaveTimerRef);
    if (savingRef.current) {
      // An autosave tick can be in flight when the author asks to publish.
      // Dropping the request here loses the one thing only they can decide, so
      // the intent is remembered and re-run once the current save settles.
      if (source !== "auto") {
        pendingPublishRef.current = { ...options, source };
      } else {
        pendingAutosaveRef.current = true;
      }
      return;
    }
    const requestId = saveRequestIdRef.current + 1;
    saveRequestIdRef.current = requestId;
    const dispatchedRevision = editorRevisionRef.current;
    const dispatchedDraft = {
      ...currentDraft,
      body: submittedBody,
      slug: mode === "edit" ? pageSlug : normalizedSlug,
      title: title.trim(),
    };
    const dispatchedFingerprint = draftSaveFingerprint(
      dispatchedDraft,
      frontmatterExtraLinesRef.current,
    );
    const contentUnchanged =
      dispatchedFingerprint === lastSavedFingerprintRef.current;
    if (
      contentUnchanged &&
      (source === "auto" || !page || publicationState === "published")
    ) {
      acknowledgeEditorRevision(dispatchedRevision);
      setSaveError(false);
      return;
    }
    const controller = new AbortController();
    savingRef.current = true;
    setIsSaving(true);
    updateInFlightRevision(dispatchedRevision);
    setSaveError(false);
    setStatus(null);
    setConflict(null);
    let publishRequestStarted = false;
    try {
      if (mode === "create" && !page) {
        const submittedDraft = {
          ...currentDraft,
          body: submittedBody,
          slug: normalizedSlug,
          title: title.trim(),
        };
        const created = await createPage(workspaceId, {
          title: submittedDraft.title,
          body_md: serializeDraftBody(submittedDraft, frontmatterExtraLinesRef.current),
          page_type: submittedDraft.pageType,
          tags: submittedDraft.tags,
          aliases: submittedDraft.aliases,
          placement_parent_node_id: submittedDraft.destinationNodeId || null,
        }, {
          signal: controller.signal,
        });
        if (!isCurrentSaveRequest(requestId, saveRequestIdRef)) return;
        if (!isEditorMountedRef.current) return;
        pageIdRef.current = created.id;
        setPage(created);
        setPublicationState("never-published");
        sessionVersionIdRef.current = created.version_id;
        acknowledgeEditorRevision(dispatchedRevision);
        lastSavedRef.current = {
          ...submittedDraft,
          title: created.title,
          slug: created.slug,
        };
        lastSavedFingerprintRef.current = draftSaveFingerprint(
          lastSavedRef.current,
          frontmatterExtraLinesRef.current,
        );
        let savedPage = created;
        let savedDraft = submittedDraft;
        const followUpRevision = editorRevisionRef.current;
        const latestDraft = latestDraftRef.current;
        if (
          followUpRevision > dispatchedRevision &&
          latestDraft.slug === submittedDraft.slug &&
          latestDraft.destinationNodeId === submittedDraft.destinationNodeId
        ) {
          const followUpDraft = {
            ...latestDraft,
            title: latestDraft.title.trim() || created.title,
          };
          updateInFlightRevision(followUpRevision);
          savedPage = await autosavePage(workspaceId, created.id, {
            title: followUpDraft.title,
            body_md: serializeDraftBody(
              followUpDraft,
              frontmatterExtraLinesRef.current,
            ),
            page_type: followUpDraft.pageType,
            tags: followUpDraft.tags,
            aliases: followUpDraft.aliases,
            base_version_id: created.version_id,
            base_content_hash: created.content_hash,
            expected_editing_revision: created.editing_revision,
            session_version_id: created.version_id,
            destination_node_id: followUpDraft.destinationNodeId || null,
            idempotency_key: newIdempotencyKey(),
            on_stale: "conflict",
            retry_only: false,
          }, { signal: controller.signal });
          if (!isCurrentSaveRequest(requestId, saveRequestIdRef)) return;
          if (!isEditorMountedRef.current) return;
          savedDraft = followUpDraft;
          acknowledgeEditorRevision(followUpRevision);
        }
        sessionVersionIdRef.current = savedPage.version_id;
        pageIdRef.current = savedPage.id;
        setPage(savedPage);
        setSaveRelativeNow(Date.now());
        lastSavedRef.current = {
          ...savedDraft,
          title: savedPage.title,
          slug: savedPage.slug,
        };
        lastSavedFingerprintRef.current = draftSaveFingerprint(
          lastSavedRef.current,
          frontmatterExtraLinesRef.current,
        );
        setPublicationState("never-published");
        if (editorRevisionRef.current > acknowledgedRevisionRef.current) {
          return;
        }
        if (source === "auto") {
          return;
        }
        if (!options.publish) {
          navigationConfirmedRef.current = true;
          navigate(`/context/${savedPage.slug}/edit`, { replace: true });
          return;
        }
        publishRequestStarted = true;
        const publishedResult = await publishPageVersion(
          workspaceId,
          savedPage.id,
          savedPage.version_id,
          {
            reason: "Published from Page editor.",
            publish_despite_findings: options.publishDespiteFindings ?? false,
            expected_content_hash: savedPage.content_hash,
          },
          { signal: controller.signal },
        );
        if (!isCurrentSaveRequest(requestId, saveRequestIdRef)) return;
        if (!isEditorMountedRef.current) return;
        sessionVersionIdRef.current = null;
        pageIdRef.current = publishedResult.page.id;
        setPage(publishedResult.page);
        setPublicationState("published");
        setStalePublishState("idle");
        setPublishedWarningStatus(publishedResult.warnings);
        const reviewNotice = await completeReviewEditIfNeeded(source, requestId);
        navigateToPublishedContext(
          publishedResult.page.id,
          publishNotice(publishedResult.warnings, reviewNotice),
          requestId,
        );
        return;
      }

      if (!page) return;
      if (contentUnchanged) {
        acknowledgeEditorRevision(dispatchedRevision);
        if (!options.publish) {
          return;
        }
        if (
          source !== "auto" &&
          fromReview &&
          workspaceId &&
          reviewItemId &&
          !reviewEditCompletedRef.current
        ) {
          // Approving a context_update review publishes it directly
          // (ReviewService.apply_decision -> ContextWriteService.publish_draft),
          // and that approval's own pointer check requires
          // latest_draft_version_id to still name this version. Calling
          // publishPageVersion first would clear that pointer and fail the
          // approval, so approve once instead of publishing separately.
          try {
            await decideReview(
              workspaceId,
              reviewItemId,
              "approve",
              "Edited directly in Page editor.",
            );
            if (!isCurrentSaveRequest(requestId, saveRequestIdRef)) return;
            if (!isEditorMountedRef.current) return;
            reviewEditCompletedRef.current = true;
            const reloaded = await readPageBySlug(workspaceId, page.slug);
            if (!isCurrentSaveRequest(requestId, saveRequestIdRef)) return;
            if (!isEditorMountedRef.current) return;
            if (reloaded) {
              const restoredPublication = readPagePublicationState(reloaded);
              sessionVersionIdRef.current = restoredPublication.sessionVersionId;
              pageIdRef.current = reloaded.id;
              setPage(reloaded);
              setPublicationState(restoredPublication.state);
            }
            setStatus(
              t("editor.publishedReviewCompleted", {
                defaultValue: "Published and completed the review request.",
              }),
            );
            navigateToPublishedContext(
              page.id,
              { kind: "review_completed" },
              requestId,
            );
          } catch (exc) {
            if (!isCurrentSaveRequest(requestId, saveRequestIdRef)) return;
            if (!isEditorMountedRef.current) return;
            setStatus(
              t("editor.reviewApprovalFailed", {
                defaultValue: "Could not approve the review: {{error}}",
                error: apiErrorMessage(
                  exc,
                  t("editor.unknownError", { defaultValue: "unknown error" }),
                ),
              }),
            );
          }
          return;
        }
        publishRequestStarted = true;
        const publishedResult = await publishPageVersion(
          workspaceId,
          page.id,
          page.version_id,
          {
            reason: "Published from Page editor.",
            publish_despite_findings: options.publishDespiteFindings ?? false,
            expected_content_hash: page.content_hash,
          },
          { signal: controller.signal },
        );
        if (!isCurrentSaveRequest(requestId, saveRequestIdRef)) return;
        if (!isEditorMountedRef.current) return;
        sessionVersionIdRef.current = null;
        pageIdRef.current = publishedResult.page.id;
        setPage(publishedResult.page);
        setPublicationState("published");
        setStalePublishState("idle");
        setPublishedWarningStatus(publishedResult.warnings);
        const reviewNotice = await completeReviewEditIfNeeded(source, requestId);
        navigateToPublishedContext(
          publishedResult.page.id,
          publishNotice(publishedResult.warnings, reviewNotice),
          requestId,
        );
        return;
      }
      const submittedDraft = {
        ...currentDraft,
        body: submittedBody,
        slug: pageSlug,
        title: title.trim(),
      };
      const requestedSessionVersionId = sessionVersionIdRef.current;
      const payload = {
        title: submittedDraft.title,
        body_md: serializeDraftBody(submittedDraft, frontmatterExtraLinesRef.current),
        page_type: submittedDraft.pageType,
        tags: submittedDraft.tags,
        aliases: submittedDraft.aliases,
        base_version_id: page.version_id,
        base_content_hash: page.content_hash,
        expected_editing_revision: page.editing_revision,
        session_version_id: requestedSessionVersionId,
        destination_node_id: submittedDraft.destinationNodeId || null,
        idempotency_key: newIdempotencyKey(),
        on_stale: "conflict" as const,
        retry_only: false,
      };
      const updated = await autosavePage(workspaceId, page.id, payload, {
        signal: controller.signal,
      });
      if (!isCurrentSaveRequest(requestId, saveRequestIdRef)) return;
      if (!isEditorMountedRef.current) return;
      rememberSessionVersion(updated.version_id, requestedSessionVersionId);
      // Body content changed, so the autosave above forked the draft and
      // (page_service._supersede_content_replaced_reviews) already closed
      // any review targeting the replaced version as "superseded". A later
      // decideReview("approve") on that same review can then only fail as
      // "already resolved" (review_service.py:984), so detect the
      // supersession here and skip the approval call entirely below.
      const supersededReviewByEdit =
        fromReview &&
        Boolean(reviewItemId) &&
        !reviewEditCompletedRef.current &&
        (updated.superseded_review_count ?? 0) > 0;
      if (supersededReviewByEdit) {
        reviewEditCompletedRef.current = true;
      }
      acknowledgeEditorRevision(dispatchedRevision);
      pageIdRef.current = updated.id;
      setPage(updated);
      setSaveRelativeNow(Date.now());
      setPublicationState((current) =>
        current === "never-published" ? "never-published" : "staged",
      );
      // I7 note: the server may have normalized the body (Auto-fix on save). We
      // deliberately do NOT re-seed the editor from updated.body_md here —
      // doing so resets the cursor mid-autosave and, because the draft
      // serializer strips leading newlines, mutates the buffer even with
      // Auto-fix off (re-review findings 3/5/6). The editor keeps the authored
      // text (a consistent, non-dirty baseline) and reflects the normalized
      // body on the next load; the explicit "Format now" button is how an
      // author pulls normalization into the live buffer.
      lastSavedRef.current = {
        ...submittedDraft,
        title: updated.title,
        slug: updated.slug,
        destinationNodeId: submittedDraft.destinationNodeId,
      };
      lastSavedFingerprintRef.current = dispatchedFingerprint;
      if (editorRevisionRef.current > acknowledgedRevisionRef.current) {
        return;
      }
      if (source === "auto") {
        return;
      }
      if (!options.publish) {
        // A media attach can create the page before the author's first manual
        // save. In that page-first shape `page` is already populated even
        // though this component is still mounted at /context/new. Once the
        // explicit save has persisted the editor buffer, move to the durable
        // edit URL just as the ordinary create path above does.
        if (mode === "create") {
          navigationConfirmedRef.current = true;
          navigate(`/context/${updated.slug}/edit`, { replace: true });
        }
        return;
      }
      publishRequestStarted = true;
      const publishedResult = await publishPageVersion(
        workspaceId,
        updated.id,
        updated.version_id,
        {
          reason: "Published from Page editor.",
          publish_despite_findings: options.publishDespiteFindings ?? false,
          expected_content_hash: updated.content_hash,
        },
        { signal: controller.signal },
      );
      if (!isCurrentSaveRequest(requestId, saveRequestIdRef)) return;
      if (!isEditorMountedRef.current) return;
      sessionVersionIdRef.current = null;
      pageIdRef.current = publishedResult.page.id;
      setPage(publishedResult.page);
      setPublicationState("published");
      setStalePublishState("idle");
      let reviewNotice: ContextPublishNotice | null = null;
      if (supersededReviewByEdit) {
        setStatus(
          t("editor.reviewSupersededByEdit", {
            defaultValue:
              "Published. The review request was superseded by this edit.",
          }),
        );
      } else {
        setPublishedWarningStatus(publishedResult.warnings);
        reviewNotice = await completeReviewEditIfNeeded(source, requestId);
      }
      navigateToPublishedContext(
        publishedResult.page.id,
        publishNotice(publishedResult.warnings, reviewNotice, supersededReviewByEdit),
        requestId,
      );
    } catch (exc) {
      if (isAbortError(exc)) {
        return;
      }
      if (!isCurrentSaveRequest(requestId, saveRequestIdRef)) return;
      if (!isEditorMountedRef.current) return;
      if (
        exc instanceof ApiError &&
        exc.status === 403 &&
        exc.body.error_code === "rejected_by_policy" &&
        exc.body.warnings.length > 0
      ) {
        const predicted = findUnclosedCodeFence(submittedBody);
        setFencePublishPrompt({
          closingFence: predicted?.closingFence ?? "```",
          message: exc.body.warnings
            .map((warning) =>
              t(`apiWarnings.${warning.code}`, {
                ns: "shell",
                defaultValue: t("apiWarnings.lint_publish_blocked", {
                  ns: "shell",
                  defaultValue: "Not published — a formatting issue is blocking it.",
                }),
              }),
            )
            .join(" "),
        });
        setPublicationState((current) =>
          current === "never-published" ? "never-published" : "staged",
        );
        return;
      }
      if (
        exc instanceof ApiError &&
        exc.status === 409 &&
        exc.body.error_code === "stale_context_draft"
      ) {
        // The publish target is not the latest linear eligible draft (a
        // second session, or a review that forked a successor draft). This
        // is not a base-content conflict -- the body was staged fine -- so
        // it must not open the conflict banner: exc.body carries no
        // server_version_id/server_content_hash for it to compare against
        // (the "" || fallback below would open a diff-yourself dead end),
        // and freezing autosave behind Boolean(conflict) would strand a
        // publish-eligibility failure as if it were a content race.
        setPublicationState((current) =>
          current === "never-published" ? "never-published" : "staged",
        );
        setStalePublishState("blocked");
        return;
      }
      if (exc instanceof ApiError && exc.status === 409) {
        sessionVersionIdRef.current = null;
        setConflict({
          serverVersionId: exc.body.server_version_id || "",
          serverContentHash: exc.body.server_content_hash || "",
        });
        setConflictDiff(null);
        setConflictDiffError(null);
        setConflictDiffOpen(false);
        return;
      }
      setStatus(
        options.publish
          ? publishRequestStarted
            ? t("editor.publishFailed", {
                defaultValue: "Publish failed. Your saved draft is still available.",
              })
            : t("editor.publishSaveFailed", {
                defaultValue:
                  "Could not save your latest changes, so nothing was published. Try again.",
              })
          : apiErrorMessage(
              exc,
              t("editor.saveFailed", {
                defaultValue: "Could not save changes. Try again.",
              }),
            ),
      );
      setSaveError(true);
    } finally {
      if (isCurrentSaveRequest(requestId, saveRequestIdRef)) {
        savingRef.current = false;
        if (isEditorMountedRef.current) {
          updateInFlightRevision(null);
          setIsSaving(false);
        }
      }
    }
  }

  function setPublishedWarningStatus(
    warnings: { code: string; message: string }[],
  ) {
    const warning = warnings.find((item) => item.code === "published_with_findings");
    setStatus(
      warning
        ? t(`apiWarnings.${warning.code}`, {
            ns: "shell",
            defaultValue: t("apiWarnings.published_with_findings", {
              ns: "shell",
              defaultValue: "Published, but a formatting issue was found.",
            }),
          })
        : null,
    );
  }

  function closeFenceAndPublish() {
    if (!fencePublishPrompt) {
      return;
    }
    const view = editorViewRef.current;
    const authoredBody = view?.state.doc.toString() ?? body;
    const fixedBody = appendClosingCodeFence(
      authoredBody,
      fencePublishPrompt.closingFence,
    );
    // Reading mode unmounts the editor without clearing the ref, so a stale
    // view would swallow the dispatch and leave the buffer unfixed while the
    // published body carried the closing fence. Dispatch only into a live
    // editor -- for undo history -- and otherwise set the state directly.
    if (view?.dom?.isConnected) {
      view.dispatch({
        changes: {
          from: authoredBody.length,
          insert: fixedBody.slice(authoredBody.length),
          to: authoredBody.length,
        },
      });
      view.focus();
    } else {
      updateAuthoredBody(fixedBody);
    }
    setFencePublishPrompt(null);
    void save({
      bodyOverride: fixedBody,
      publish: true,
      skipFenceCheck: true,
      source: "manual",
    });
  }

  function publishDespiteFenceFinding() {
    setFencePublishPrompt(null);
    void save({
      publish: true,
      publishDespiteFindings: true,
      skipFenceCheck: true,
      source: "manual",
    });
  }

  // A publish the author asked for while a save was in flight runs once that
  // save settles -- from an effect rather than the save's own `finally`, so it
  // starts from the committed state (the base version the first save just
  // returned, and the text typed since) instead of the closure that queued it.
  useEffect(() => {
    if (isSaving) {
      return;
    }
    const queued = pendingPublishRef.current;
    if (queued && isEditorMountedRef.current) {
      pendingPublishRef.current = null;
      pendingAutosaveRef.current = false;
      saveRef.current(queued);
      return;
    }
    if (pendingAutosaveRef.current && isEditorMountedRef.current) {
      pendingAutosaveRef.current = false;
      saveRef.current({ source: "auto" });
    }
  }, [isSaving]);

  // Refs that get re-assigned every render must NOT mutate during render —
  // StrictMode / concurrent rendering can discard a render and leave the ref
  // pointing at a closure from a phantom render. Bind in useLayoutEffect so
  // the assignment happens synchronously after commit. Review §3.1 FP-C2.
  useLayoutEffect(() => {
    saveRef.current = (options = { source: "manual" }) => {
      void save(options);
    };
    autosaveBackstopRef.current = (options = {}) => {
      if (
        !hasUnsavedChanges ||
        !page ||
        !workspaceId ||
        !canSave ||
        conflict ||
        (!options.allowInFlight && inFlightRevisionRef.current !== null) ||
        saveError ||
        isComposingRef.current
      ) {
        return false;
      }
      const draft = latestDraftRef.current;
      if (
        draftSaveFingerprint(draft, frontmatterExtraLinesRef.current) ===
        lastSavedFingerprintRef.current
      ) {
        return false;
      }
      return sendPageAutosave(workspaceId, page.id, {
        title: draft.title.trim(),
        body_md: serializeDraftBody(draft, frontmatterExtraLinesRef.current),
        page_type: draft.pageType,
        tags: draft.tags,
        aliases: draft.aliases,
        base_version_id: page.version_id,
        base_content_hash: page.content_hash,
        expected_editing_revision: page.editing_revision,
        session_version_id: sessionVersionIdRef.current,
        destination_node_id: draft.destinationNodeId || null,
        idempotency_key: newIdempotencyKey(),
        on_stale: "conflict",
        retry_only: false,
      });
    };
  });

  function rememberSessionVersion(
    savedVersionId: string,
    requestedSessionVersionId: string | null,
  ) {
    if (requestedSessionVersionId && savedVersionId !== requestedSessionVersionId) {
      sessionVersionIdRef.current = null;
      return;
    }
    sessionVersionIdRef.current = savedVersionId;
  }

  async function viewConflictChanges() {
    if (!page || !conflict || conflictDiffLoading || !workspaceId) {
      return;
    }
    setConflictDiffOpen(true);
    setConflictDiffError(null);
    if (!conflict.serverVersionId) {
      setConflictDiff(null);
      setConflictDiffError(
        t("editor.conflictVersionMissing", {
          defaultValue: "The server did not return a version to compare.",
        }),
      );
      return;
    }
    if (conflict.serverVersionId === page.version_id) {
      // An in-place autosave overwrite (two tabs on the same draft) reports
      // the conflict against the caller's own version id, so a version-diff
      // request here would just diff the draft against itself.
      setConflictDiff(null);
      setConflictDiffError(
        t("editor.conflictInPlaceOverwritten", {
          defaultValue:
            "This draft was overwritten in place by another session; there is no version-to-version comparison.",
        }),
      );
      return;
    }
    setConflictDiffLoading(true);
    try {
      setConflictDiff(
        await diffPageVersions(
          workspaceId,
          page.id,
          page.version_id,
          conflict.serverVersionId,
        ),
      );
    } catch (exc) {
      setConflictDiff(null);
      setConflictDiffError(
        apiErrorMessage(
          exc,
          t("editor.loadServerChangesFailed", {
            defaultValue: "Failed to load server changes",
          }),
        ),
      );
    } finally {
      setConflictDiffLoading(false);
    }
  }

  async function reloadLatest(slugOverride?: string): Promise<boolean> {
    if (!workspaceId) return false;
    const latest = await readPageBySlug(workspaceId, slugOverride ?? normalizedSlug);
    if (!latest) return false;
    const restoredPublication = readPagePublicationState(latest);
    const parsed = parsePageDocument(latest.body_md, {
      aliases: latest.aliases,
      tags: latest.tags,
      type: latest.page_type as PageType,
    });
    const metadata = metadataFromPagePayload(parsed.metadata, latest);
    const latestDestination =
      contextTree.find((node) => node.target_id === latest.id)?.parent_id ??
      chooseDefaultDestination(contextTree);
    applyParsedMetadata(metadata);
    sessionVersionIdRef.current = restoredPublication.sessionVersionId;
    setPublicationState(restoredPublication.state);
    pageIdRef.current = latest.id;
    setPage(latest);
    setTitle(latest.title);
    setPageSlug(latest.slug);
    bodyRef.current = parsed.body;
    setBody(parsed.body);
    setDestinationNodeId(latestDestination);
    setConflict(null);
    setStalePublishState("idle");
    lastSavedRef.current = {
      aliases: metadata.aliases,
      body: parsed.body,
      destinationNodeId: latestDestination,
      icon: metadata.icon,
      pageType: metadata.type,
      slug: latest.slug,
      tags: metadata.tags,
      title: latest.title,
    };
    lastSavedFingerprintRef.current = draftSaveFingerprint(
      lastSavedRef.current,
      frontmatterExtraLinesRef.current,
    );
    acknowledgeCurrentEditorRevision();
    setSaveError(false);
    setTitleValidationRequested(false);
    return true;
  }

  async function reloadAfterStalePublish() {
    if (stalePublishState === "reloading") {
      return;
    }
    setStalePublishState("reloading");
    try {
      const reloaded = await reloadLatest();
      if (!reloaded) {
        setStalePublishState("error");
      }
    } catch {
      setStalePublishState("error");
    }
  }

  async function preserveAlternative() {
    if (!page || !conflict || savingRef.current || !workspaceId) return;
    const preservedRevision = editorRevisionRef.current;
    savingRef.current = true;
    setIsSaving(true);
    updateInFlightRevision(preservedRevision);
    setStatus(null);
    try {
      const submittedDraft = { ...currentDraft, slug: pageSlug, title: title.trim() };
      const preserved = await updatePage(workspaceId, page.id, {
        title: submittedDraft.title,
        body_md: serializeDraftBody(submittedDraft, frontmatterExtraLinesRef.current),
        page_type: submittedDraft.pageType,
        tags: submittedDraft.tags,
        aliases: submittedDraft.aliases,
        base_version_id: page.version_id,
        base_content_hash: page.content_hash,
        expected_editing_revision: page.editing_revision,
        destination_node_id: submittedDraft.destinationNodeId || null,
        on_stale: "preserve_as_alternative",
      });
      sessionVersionIdRef.current = null;
      // The preserved bytes now live on a branched version that is not the
      // page's editable head (_preserve_alternative does not move
      // page.current_version_id), so this session cannot keep autosaving onto
      // it. Rebase the editor onto the server head instead of adopting the
      // branch, and name the parked alternative in the status line.
      await reloadLatest(preserved.slug);
      setStatus(
        t("editor.alternativePreserved", {
          id: preserved.alternative_draft_id?.slice(0, 8) ?? "",
          defaultValue: "Preserved as alternative draft {{id}}",
        }),
      );
    } catch (exc) {
      setStatus(
        apiErrorMessage(
          exc,
          t("editor.preserveFailed", { defaultValue: "Preserve failed" }),
        ),
      );
    } finally {
      savingRef.current = false;
      updateInFlightRevision(null);
      setIsSaving(false);
    }
  }

  function updateTitle(nextTitle: string) {
    const normalizedTitle = normalizeTitleInput(nextTitle);
    if (normalizedTitle === title) {
      return;
    }
    setTitle(normalizedTitle);
    if (mode === "create" && !slugTouchedRef.current && !savingRef.current && !hasPersistedPage) {
      setPageSlug(slugify(normalizedTitle));
    }
    markDraftMutation();
  }

  function handleTitleKeyDown(event: ReactKeyboardEvent<HTMLTextAreaElement>) {
    if (event.nativeEvent.isComposing) {
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      editorViewRef.current?.focus();
    }
  }

  function resetPageMetadata(tags: string[]) {
    frontmatterExtraLinesRef.current = [];
    setPageIcon(DEFAULT_PAGE_METADATA.icon);
    setPageType(DEFAULT_PAGE_METADATA.type);
    setTagInput(tags.join(", "));
    setAliasInput("");
    setTagQuery("");
    setTagSuggestions([]);
  }

  function applyParsedMetadata(metadata: PageMetadata) {
    frontmatterExtraLinesRef.current = metadata.extraLines;
    setPageIcon(metadata.icon);
    setPageType(metadata.type);
    setTagInput(metadata.tags.join(", "));
    setAliasInput(metadata.aliases.join(", "));
  }

  function updateSlug(nextSlug: string) {
    slugTouchedRef.current = true;
    const normalizedSlugInput = normalizeSlugInput(nextSlug);
    if (normalizedSlugInput === pageSlug) {
      return;
    }
    setPageSlug(normalizedSlugInput);
    markDraftMutation();
  }

  function updateTags(nextTags: string[]) {
    setTagInput(nextTags.join(", "));
    markDraftMutation();
  }

  function updateAliases(nextAliases: string[]) {
    setAliasInput(nextAliases.join(", "));
    setAliasItemError(null);
    markDraftMutation();
  }

  function resizeSplitFromPointer(clientX: number, clientY: number) {
    const bounds = editorGridRef.current?.getBoundingClientRect();
    const separatorBounds = splitSeparatorRef.current?.getBoundingClientRect();
    if (!bounds || !separatorBounds) return;
    const separatorSize = stackedSplitViewport
      ? separatorBounds.height
      : separatorBounds.width;
    const totalSize = stackedSplitViewport ? bounds.height : bounds.width;
    const availableSize = Math.max(1, totalSize - separatorSize);
    const pointerOffset = stackedSplitViewport
      ? clientY - bounds.top - separatorSize / 2
      : clientX - bounds.left - separatorSize / 2;
    const percent = (pointerOffset / availableSize) * 100;
    setSplitPercent(clampSplitPercent(percent));
  }

  function handleSplitSeparatorKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    const decrease = stackedSplitViewport
      ? event.key === "ArrowUp"
      : event.key === "ArrowLeft";
    const increase = stackedSplitViewport
      ? event.key === "ArrowDown"
      : event.key === "ArrowRight";
    if (!decrease && !increase && event.key !== "Home" && event.key !== "End") return;
    event.preventDefault();
    const step = event.shiftKey ? 10 : 2;
    setSplitPercent((current) => {
      if (event.key === "Home") return 40;
      if (event.key === "End") return 60;
      return clampSplitPercent(current + (increase ? step : -step));
    });
  }

  function finishSplitPointer(event: ReactPointerEvent<HTMLDivElement>) {
    if (splitPointerIdRef.current !== event.pointerId) return;
    splitPointerIdRef.current = null;
    if (
      typeof event.currentTarget.hasPointerCapture === "function" &&
      event.currentTarget.hasPointerCapture(event.pointerId)
    ) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  }

  function updateDestination(nextDestinationNodeId: string) {
    if (nextDestinationNodeId === destinationNodeId) {
      return;
    }
    setDestinationNodeId(nextDestinationNodeId);
    markDraftMutation();
  }

  function closeEditor() {
    if ((hasUnsavedChanges || hasPendingImageUpload) &&
        !window.confirm(dismissalRiskMessage)) {
      return;
    }
    if (hasUnsavedChanges) {
      autosaveBackstopRef.current({ allowInFlight: true });
      saveRef.current({ source: "auto" });
    }
    navigationConfirmedRef.current = true;
    if (fromReview) {
      navigate(-1);
      return;
    }
    const contextTarget = contextActiveNodeId ?? page?.id ?? null;
    navigate(contextTarget ? `/context?${TREE_TARGET_PARAM}=${contextTarget}` : "/context");
  }

  function selectContextTreeNode(node: TreeNodePayload) {
    if (node.target_kind !== "context_page" || !node.slug) {
      return;
    }
    setContextRailOpen(false);
    navigate(`/context/${node.slug}/edit`);
  }

  async function refreshContextTree() {
    if (!workspaceId) {
      return;
    }
    try {
      const nextTree = await listTree(workspaceId, "context");
      const knownNodeIds = new Set(nextTree.map((node) => node.id));
      setContextTree(nextTree);
      setContextExpandedNodeIds((current) => {
        const preserved = current.filter((nodeId) => knownNodeIds.has(nodeId));
        return preserved;
      });
    } catch (exc) {
      setStatus(
        apiErrorMessage(
          exc,
          t("editor.refreshContextTreeFailed", {
            defaultValue: "Failed to refresh Context tree",
          }),
        ),
      );
    }
  }

  function handleModeTabKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (
      event.key !== "ArrowRight" &&
      event.key !== "ArrowLeft" &&
      event.key !== "Home" &&
      event.key !== "End"
    ) {
      return;
    }
    event.preventDefault();
    const availableModes = singlePaneViewport
      ? EDITOR_MODES.slice(0, 2)
      : EDITOR_MODES;
    const currentIndex = Math.max(0, availableModes.indexOf(activeEditorMode));
    const nextMode = event.key === "Home"
      ? availableModes[0]
      : event.key === "End"
        ? availableModes[availableModes.length - 1]
        : availableModes[
            (
              currentIndex +
              (event.key === "ArrowLeft" ? -1 : 1) +
              availableModes.length
            ) % availableModes.length
          ];
    setEditorMode(nextMode);
    event.currentTarget
      .querySelector<HTMLButtonElement>(`#page-editor-${nextMode}-tab`)
      ?.focus();
  }

  function executeEditorCommand(
    commandId: EditorCommandId,
    commandView: EditorViewType | null = editorViewRef.current,
    options: ExecuteEditorCommandOptions = {},
  ) {
    const command = editorCommandById(commandId);
    const view = commandView ?? editorViewRef.current;
    if (!command || !view || isComposingRef.current) return false;
    if (!command.modes.includes(activeEditorMode)) return false;

    if (isTextMutationCommand(command)) {
      const selection = view.state.selection.main;
      const plan = planEditorCommand({
        body: view.state.doc.toString(),
        commandId: command.mutation,
        replaceRange: options.replaceRange,
        selection: { anchor: selection.anchor, head: selection.head },
      });
      view.dispatch({
        changes: plan.change,
        selection: plan.selection,
        scrollIntoView: true,
      });
      closeBlockMenu();
      scheduleInlineToolbarSync(view);
      view.focus();
      return true;
    }

    if (command.kind === "modal-mutation") {
      if (command.modal === "link-dialog") {
        return openLinkDialog(view, options.replaceRange);
      }
      const range = options.replaceRange ?? blockInsertRangeForSelection(view);
      if (command.modal === "emoji-picker") {
        const position = floatingPositionForRange(view, range);
        setEmojiPicker({
          insertPrefix: options.replaceRange ? "" : blockInsertPrefix(view, range),
          range,
          left: position.left,
          top: position.top,
        });
        closeBlockMenu();
        view.focus();
        return true;
      }
      openBlockMenu({
        query: command.modal === "math-dialog" ? "math" : "table",
        trigger: "manual",
        range,
      });
      return true;
    }

    if (command.kind === "async-attachment") {
      if (mediaUploadCapabilityState !== "ready") {
        setStatus(t("editor.imageUploadCapabilitiesUnavailable", {
          defaultValue: "Current media upload limits are unavailable. Try again after they load.",
        }));
        return true;
      }
      const range = options.replaceRange ?? blockInsertRangeForSelection(view);
      pendingImageInsertRef.current = {
        insertPrefix: options.replaceRange ? "" : blockInsertPrefix(view, range),
        range,
      };
      closeBlockMenu();
      imageFileInputRef.current?.click();
      view.focus();
      return true;
    }

    if (command.effect === "undo") return undo(view);
    if (command.effect === "redo") return redo(view);
    return openCommentPreview(view);
  }

  function scheduleInlineToolbarSync(view: EditorViewType) {
    queueMicrotask(() => {
      if (editorViewRef.current === view) {
        syncInlineToolbar(view);
      }
    });
  }

  function openLinkDialog(
    view = editorViewRef.current,
    replacementRange?: { from: number; to: number } | null,
  ) {
    if (!view) {
      return false;
    }
    const selection = replacementRange ?? view.state.selection.main;
    const selectedText = replacementRange
      ? ""
      : view.state.doc.sliceString(selection.from, selection.to);
    const selectionBox = view.coordsAtPos(selection.from);
    const viewport = visualViewportBounds();
    const minLeft = viewport.left + INLINE_TOOLBAR_MARGIN;
    const minTop = viewport.top + INLINE_TOOLBAR_MARGIN;
    const maxLeft = Math.max(
      minLeft,
      viewport.right - LINK_POPOVER_ESTIMATED_WIDTH - INLINE_TOOLBAR_MARGIN,
    );
    const maxTop = Math.max(
      minTop,
      viewport.bottom - LINK_POPOVER_ESTIMATED_HEIGHT - INLINE_TOOLBAR_MARGIN,
    );
    setLinkDialog({
      from: selection.from,
      to: selection.to,
      text: selectedText,
      url: "",
      left: clamp(selectionBox?.left ?? minLeft, minLeft, maxLeft),
      top: clamp(
        (selectionBox?.bottom ?? minTop) + INLINE_TOOLBAR_MARGIN,
        minTop,
        maxTop,
      ),
    });
    hideInlineToolbar();
    closeBlockMenu();
    closeEmojiPicker();
    closeCommentPreview();
    closeAiInlineEdit();
    view.focus();
    return true;
  }

  function closeLinkDialog(options: CloseAiInlineOptions = {}) {
    setLinkDialog(null);
    if (options.restoreFocus) {
      window.setTimeout(() => editorViewRef.current?.focus(), 0);
    }
  }

  function updateLinkDialog(patch: Partial<Pick<LinkDialogState, "text" | "url">>) {
    setLinkDialog((current) => current ? { ...current, ...patch } : current);
  }

  function insertLink(event: ReactFormEvent) {
    event.preventDefault();
    const view = editorViewRef.current;
    if (!view || !linkDialog) {
      return;
    }
    const text = linkDialog.text.trim() || "link";
    const url = normalizeLinkDestination(linkDialog.url);
    if (!url) {
      return;
    }
    const linkMarkdown = `[${escapeLinkText(text)}](${url})`;
    view.dispatch({
      changes: { from: linkDialog.from, to: linkDialog.to, insert: linkMarkdown },
      selection: { anchor: linkDialog.from + linkMarkdown.length },
      scrollIntoView: true,
    });
    closeLinkDialog({ restoreFocus: true });
  }

  function openCommentPreview(view = editorViewRef.current) {
    if (!view) {
      return false;
    }
    const selection = view.state.selection.main;
    if (selection.empty) {
      setStatus(
        t("editor.commentSelectText", {
          defaultValue: "Select text before adding a comment.",
        }),
      );
      return false;
    }
    const selectedText = view.state.doc.sliceString(selection.from, selection.to);
    const selectionBox = view.coordsAtPos(selection.from);
    const viewportWidth = window.innerWidth || document.documentElement.clientWidth;
    const viewportHeight = window.innerHeight || document.documentElement.clientHeight;
    const maxLeft = Math.max(
      INLINE_TOOLBAR_MARGIN,
      viewportWidth - COMMENT_POPOVER_ESTIMATED_WIDTH - INLINE_TOOLBAR_MARGIN,
    );
    const maxTop = Math.max(
      INLINE_TOOLBAR_MARGIN,
      viewportHeight - COMMENT_POPOVER_ESTIMATED_HEIGHT - INLINE_TOOLBAR_MARGIN,
    );
    setCommentPreview({
      from: selection.from,
      to: selection.to,
      selectedText,
      note: "",
      left: clamp(selectionBox?.left ?? INLINE_TOOLBAR_MARGIN, INLINE_TOOLBAR_MARGIN, maxLeft),
      top: clamp(
        (selectionBox?.bottom ?? INLINE_TOOLBAR_MARGIN) + INLINE_TOOLBAR_MARGIN,
        INLINE_TOOLBAR_MARGIN,
        maxTop,
      ),
    });
    hideInlineToolbar();
    closeBlockMenu();
    closeEmojiPicker();
    closeLinkDialog();
    closeAiInlineEdit();
    view.focus();
    return true;
  }

  function closeCommentPreview(options: CloseAiInlineOptions = {}) {
    setCommentPreview(null);
    setCommentSubmitting(false);
    if (options.restoreFocus) {
      window.setTimeout(() => editorViewRef.current?.focus(), 0);
    }
  }

  function updateCommentNote(note: string) {
    setCommentPreview((current) => current ? { ...current, note } : current);
  }

  async function createInlineComment() {
    if (!commentPreview || commentSubmitting) {
      return;
    }
    if (!page || !workspaceId) {
      setStatus(
        t("editor.commentSavePageFirst", {
          defaultValue: "Save this page before adding comments.",
        }),
      );
      return;
    }
    if (hasUnsavedChanges) {
      setStatus(
        t("editor.commentSaveEditsFirst", {
          defaultValue: "Save current edits before adding a comment.",
        }),
      );
      return;
    }
    const note = commentPreview.note.trim();
    if (!note) {
      setStatus(
        t("editor.commentNoteRequired", {
          defaultValue: "Write a comment note before creating it.",
        }),
      );
      return;
    }
    setCommentSubmitting(true);
    setStatus(null);
    try {
      const doc = editorViewRef.current?.state.doc.toString() ?? body;
      const comment = await createPageComment(workspaceId, page.id, {
        note,
        anchor: {
          anchor_text: commentPreview.selectedText,
          context_after: doc.slice(commentPreview.to, commentPreview.to + 160),
          context_before: doc.slice(Math.max(0, commentPreview.from - 160), commentPreview.from),
          version_id: page.version_id,
          char_offset_hint: commentPreview.from,
        },
      });
      closeCommentPreview({ restoreFocus: true });
      setStatus(
        t("editor.commentCreated", {
          defaultValue: "Comment created for review {{id}}.",
          id: comment.review_item_id.slice(0, 8),
        }),
      );
    } catch (exc) {
      setStatus(
        apiErrorMessage(
          exc,
          t("editor.commentCreationFailed", {
            defaultValue: "Comment creation failed",
          }),
        ),
      );
      setCommentSubmitting(false);
    }
  }

  function closeAiInlineEdit(options: CloseAiInlineOptions = {}) {
    setAiInline(null);
    if (options.restoreFocus) {
      window.setTimeout(() => editorViewRef.current?.focus(), 0);
    }
  }

  function handleAiInlineKeyDown(event: ReactKeyboardEvent) {
    if (event.key === "Escape") {
      event.preventDefault();
      closeAiInlineEdit({ restoreFocus: true });
    }
  }

  function hideInlineToolbar() {
    setInlineToolbar((current) => current.visible ? { ...current, visible: false } : current);
  }

  function syncInlineToolbar(view: EditorViewType) {
    const selection = view.state.selection.main;
    setActiveCommandStates(commandStatesForSelection(view));
    if (selection.empty) {
      hideInlineToolbar();
      return;
    }
    const sourcePane = view.dom.closest<HTMLElement>(".source-pane");
    const selectionBox = view.coordsAtPos(selection.from);
    if (!sourcePane || !selectionBox) {
      hideInlineToolbar();
      return;
    }
    const viewportWidth = window.innerWidth || document.documentElement.clientWidth;
    const viewportHeight = window.innerHeight || document.documentElement.clientHeight;
    const maxLeft = Math.max(
      INLINE_TOOLBAR_MARGIN,
      viewportWidth - INLINE_TOOLBAR_ESTIMATED_WIDTH - INLINE_TOOLBAR_MARGIN,
    );
    const selectionTop = selectionBox.top;
    const topAboveSelection =
      selectionTop - INLINE_TOOLBAR_ESTIMATED_HEIGHT - INLINE_TOOLBAR_MARGIN;
    const topBelowSelection = selectionBox.bottom + INLINE_TOOLBAR_MARGIN;
    const maxTop = Math.max(
      INLINE_TOOLBAR_MARGIN,
      viewportHeight - INLINE_TOOLBAR_ESTIMATED_HEIGHT - INLINE_TOOLBAR_MARGIN,
    );
    setInlineToolbar({
      visible: true,
      left: clamp(selectionBox.left, INLINE_TOOLBAR_MARGIN, maxLeft),
      top: topAboveSelection >= INLINE_TOOLBAR_MARGIN
        ? topAboveSelection
        : clamp(topBelowSelection, INLINE_TOOLBAR_MARGIN, maxTop),
    });
  }

  function syncSlashBlockMenu(view: EditorViewType) {
    const { head } = view.state.selection.main;
    const line = view.state.doc.lineAt(head);
    const beforeCursor = view.state.doc.sliceString(line.from, head);
    const afterCursor = view.state.doc.sliceString(head, line.to);
    const slashMatch = /^\/([\w\s-]*)$/.exec(beforeCursor);
    if (!slashMatch || afterCursor.trim()) {
      if (blockMenuTriggerRef.current === "slash") {
        closeBlockMenu();
      }
      return;
    }
    openBlockMenu({
      query: slashMatch[1],
      trigger: "slash",
      range: { from: line.from, to: line.to },
    });
  }

  function openBlockMenuAtSelection(view = editorViewRef.current) {
    if (activeEditorMode !== "source") {
      setEditorMode("source");
    }
    const targetView = view ?? editorViewRef.current;
    if (!targetView) {
      return;
    }
    openBlockMenu({
      query: "",
      trigger: "manual",
      range: blockInsertRangeForSelection(targetView),
    });
  }

  function openBlockMenu({
    query,
    trigger,
    range,
  }: {
    query: string;
    trigger: "manual" | "slash";
    range: { from: number; to: number } | null;
  }) {
    blockMenuTriggerRef.current = trigger;
    blockMenuRangeRef.current = range;
    setBlockMenuTrigger(trigger);
    setBlockMenuQuery(query);
  }

  function closeBlockMenu() {
    blockMenuTriggerRef.current = null;
    blockMenuRangeRef.current = null;
    setBlockMenuTrigger(null);
    setBlockMenuQuery("");
    setBlockMenuPosition(null);
  }

  function closeEmojiPicker(options: CloseAiInlineOptions = {}) {
    setEmojiPicker(null);
    if (options.restoreFocus) {
      window.setTimeout(() => editorViewRef.current?.focus(), 0);
    }
  }

  function insertBlock(entry: BlockMenuEntry) {
    const view = editorViewRef.current;
    if (!view) {
      return;
    }
    const range = blockMenuRangeRef.current ?? blockInsertRangeForSelection(view);
    const insertPrefix = blockMenuRangeRef.current ? "" : blockInsertPrefix(view, range);
    if (
      blockMenuTriggerRef.current === "slash" &&
      entry.commandId &&
      entry.commandId !== "table" &&
      entry.commandId !== "math"
    ) {
      executeEditorCommand(entry.commandId, view, { replaceRange: range });
      return;
    }
    if (entry.id === "image" || entry.id === "attach") {
      if (mediaUploadCapabilityState !== "ready") {
        setStatus(t("editor.imageUploadCapabilitiesUnavailable", {
          defaultValue: "Current media upload limits are unavailable. Try again after they load.",
        }));
        closeBlockMenu();
        view.focus();
        return;
      }
      pendingImageInsertRef.current = { insertPrefix, range };
      closeBlockMenu();
      imageFileInputRef.current?.click();
      view.focus();
      return;
    }
    if (entry.id === "emoji") {
      const position = blockMenuPosition ?? floatingPositionForRange(view, range);
      setEmojiPicker({
        insertPrefix,
        range,
        left: position.left,
        top: position.top,
      });
      closeBlockMenu();
      view.focus();
      return;
    }
    const snippet = blockSnippet(entry.template);
    const insertText = `${insertPrefix}${snippet.text}`;
    const cursor = range.from + insertPrefix.length + snippet.cursorOffset;
    view.dispatch({
      changes: { from: range.from, to: range.to, insert: insertText },
      selection: { anchor: cursor, head: cursor },
      scrollIntoView: true,
    });
    closeBlockMenu();
    view.focus();
  }

  function insertEmoji(emoji: string) {
    const view = editorViewRef.current;
    if (!view || !emojiPicker) {
      return;
    }
    const insertText = `${emojiPicker.insertPrefix}${emoji}`;
    const cursor = emojiPicker.range.from + insertText.length;
    view.dispatch({
      changes: { from: emojiPicker.range.from, to: emojiPicker.range.to, insert: insertText },
      selection: { anchor: cursor, head: cursor },
      scrollIntoView: true,
    });
    closeEmojiPicker({ restoreFocus: true });
  }

  function handleImageFileInput(event: ReactChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.currentTarget.files ?? []);
    event.currentTarget.value = "";
    const view = editorViewRef.current;
    const pending = pendingImageInsertRef.current;
    pendingImageInsertRef.current = null;
    if (files.length === 0) {
      view?.focus();
      return;
    }
    if (!view || !pending) {
      setStatus(
        t("editor.imageUploadEditorUnavailable", {
          defaultValue: "Image upload failed because the editor is not ready.",
        }),
      );
      return;
    }
    if (!insertImageFilesAtRange(
      view,
      files,
      imageUploadOptions,
      pending.range,
      pending.insertPrefix,
    )) {
      setStatus(t("editor.imageUploadUnsupported", {
        defaultValue:
          "Choose an image format allowed by the current workspace upload policy.",
      }));
      view.focus();
    }
  }

  function cancelImageUpload(marker: string) {
    cancelQueuedImageUpload(imageUploadOptions, marker);
  }

  function cancelAllContextImageUploads() {
    cancelAllImageUploads(imageUploadOptions);
  }

  function retryContextImageUpload(marker: string) {
    const upload = imageUploadEntries.find((entry) => entry.marker === marker);
    const view = editorViewRef.current;
    if (
      !upload ||
      !view ||
      (upload.status !== "failed" && upload.status !== "cancelled" &&
        upload.status !== "outcome-unknown")
    ) {
      return;
    }
    setStatus(null);
    if (!canRetryImageUpload(imageUploadOptions, marker)) {
      setStatus(t("editor.imageUploadPolicyRetryBlocked", {
        defaultValue: "The original batch must pass the refreshed upload limits before this item can retry.",
      }));
      return;
    }
    if (!retryImageUpload(view, upload, imageUploadOptions)) {
      setStatus(t("editor.imageUploadRetryMissing", {
        defaultValue: "The upload insertion slot was removed. Insert the image again.",
      }));
    }
    if (mediaUploadCapabilityState !== "ready") {
      setStatus(t("editor.imageUploadCapabilitiesUnavailable", {
        defaultValue: "Current media upload limits are unavailable. Try again after they load.",
      }));
      view?.focus();
      return;
    }
  }

  function retryFailedContextImageUploads() {
    const view = editorViewRef.current;
    if (!view) return;
    setStatus(null);
    for (const upload of imageUploadEntries) {
      if (upload.status === "failed" && upload.insertionSlotAvailable !== false &&
          canRetryImageUpload(imageUploadOptions, upload.marker)) {
        retryImageUpload(view, upload, imageUploadOptions);
      }
    }
  }

  function removeContextImageUpload(marker: string) {
    const upload = imageUploadEntries.find((entry) => entry.marker === marker);
    const view = editorViewRef.current;
    if (!upload || !view) return;
    if (removeImageUpload(view, upload, imageUploadOptions)) {
      setImageUploadEntries((current) =>
        current.filter((entry) => entry.marker !== marker),
      );
      setStatus(null);
    }
  }

  function handleBlockMenuKeyDown(event: ReactKeyboardEvent) {
    if (event.key === "Escape") {
      event.preventDefault();
      closeBlockMenu();
      editorViewRef.current?.focus();
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (filteredBlockEntries.length === 0) {
        return;
      }
      setHighlightedBlockIndex((current) =>
        Math.min(current + 1, filteredBlockEntries.length - 1),
      );
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      setHighlightedBlockIndex((current) => Math.max(current - 1, 0));
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      const entry = filteredBlockEntries[highlightedBlockIndex];
      if (entry) {
        insertBlock(entry);
      }
    }
  }

  // See note on saveRef above (FP-C2): bind these refs after commit so
  // discarded renders cannot leak phantom-render closures.
  useLayoutEffect(() => {
    commandRef.current = executeEditorCommand;
    openBlockMenuAtSelectionRef.current = openBlockMenuAtSelection;
    syncInlineToolbarRef.current = syncInlineToolbar;
    syncSlashBlockMenuRef.current = syncSlashBlockMenu;
  });

  const contextRailLabel = t("editor.rail.context", { defaultValue: "Context" });
  const navigationRail = focusMode ? null : (
    <>
      <button
        aria-controls="page-editor-context-rail"
        aria-expanded={contextRailOpen}
        aria-label={t("editor.openExplorer", {
          defaultValue: "Open Context explorer",
        })}
        className="workspace-mobile-drawer-toggle icon-button text-button"
        onClick={() => setContextRailOpen(true)}
        ref={contextRailToggleRef}
        type="button"
      >
        <span>{contextRailLabel}</span>
      </button>
      {contextRailDrawerViewport && contextRailOpen ? (
        <button
          aria-label={t("editor.closeExplorer", {
            defaultValue: "Close Context explorer",
          })}
          className="workspace-mobile-drawer-scrim"
          onClick={closeContextRail}
          type="button"
        />
      ) : null}
      <MobileDrawer
        id="page-editor-context-rail"
        label={contextRailLabel}
        mobileLayout={contextRailDrawerViewport}
        mobileOpen={contextRailDrawerViewport ? contextRailOpen : true}
        ref={contextRailRef}
      >
        <div className="editor-navigation-drawer-content">
          {contextRailDrawerViewport ? (
            <header className="editor-navigation-drawer-header">
              <strong>{contextRailLabel}</strong>
              <button
                aria-label={t("editor.closeExplorer", {
                  defaultValue: "Close Context explorer",
                })}
                className="icon-button"
                onClick={closeContextRail}
                type="button"
              >
                <X aria-hidden="true" size={18} />
              </button>
            </header>
          ) : null}
          <TreeRail
            title={contextRailLabel}
            accent="context"
            nodes={contextTree}
            activeTargetId={page?.id ?? null}
            activeNodeId={contextActiveNodeId}
            expandedNodeIds={contextExpandedNodeIds}
            forceActivePathExpanded={false}
            emptyLabel={t("editor.rail.contextEmpty", { defaultValue: "No Context tree nodes" })}
            hideSystemRootNodes
            onExpandedNodeIdsChange={setContextExpandedNodeIds}
            onRefresh={refreshContextTree}
            onSelectTarget={(node) => {
              if (contextRailDrawerViewport) {
                closeContextRail();
              }
              selectContextTreeNode(node);
            }}
          />
        </div>
      </MobileDrawer>
    </>
  );

  if (pageLoadState === "loading") {
    return (
      <div
        className={`page-workspace ${focusMode ? "focus-mode" : ""}`}
        data-focus-mode={focusMode ? "on" : "off"}
      >
        {navigationRail}
        <section className="editor-panel">
          <header className="editor-header">
            <div className="editor-header-lead">
              <Breadcrumbs
                ariaLabel={t("editor.location", { defaultValue: "Page location" })}
                items={editorBreadcrumbItems}
              />
              <p className="meta-line">
                {t("editor.loadingPage", { defaultValue: "Loading page" })}
              </p>
            </div>
            <div className="reader-actions">
              <button
                aria-label={t("editor.close")}
                className="icon-button"
                onClick={closeEditor}
                type="button"
              >
                <X size={16} aria-hidden="true" />
              </button>
            </div>
          </header>
          <div className="empty-state empty-state-rich">
            <h2>{t("editor.loadingPage", { defaultValue: "Loading page" })}</h2>
          </div>
        </section>
      </div>
    );
  }

  if (mode === "edit" && pageLoadState === "not_found") {
    return (
      <div
        className={`page-workspace ${focusMode ? "focus-mode" : ""}`}
        data-focus-mode={focusMode ? "on" : "off"}
      >
        {navigationRail}
        <section className="editor-panel">
          <header className="editor-header">
            <div className="editor-header-lead">
              <Breadcrumbs
                ariaLabel={t("editor.location", { defaultValue: "Page location" })}
                items={editorBreadcrumbItems}
              />
            </div>
            <div className="reader-actions">
              <button
                aria-label={t("editor.close")}
                className="icon-button"
                onClick={closeEditor}
                type="button"
              >
                <X size={16} aria-hidden="true" />
              </button>
            </div>
          </header>
          {/* Same copy and same action order as the route-level 404
              (NotFoundScreen); only the heading level differs, because this one
              sits inside the editor chrome under the breadcrumb. */}
          <div className="empty-state empty-state-rich">
            <h2>
              {t("notFound.title", {
                ns: "shell",
                defaultValue: "We couldn't find that page",
              })}
            </h2>
            <p>
              {t("notFound.body", {
                ns: "shell",
                defaultValue:
                  "The link may be mistyped or out of date. Use the navigation, or jump back to a known place.",
              })}
            </p>
            <div className="empty-state-actions">
              <Link className="icon-button text-button primary" to="/dashboard">
                {t("notFound.backToDashboard", {
                  ns: "shell",
                  defaultValue: "Back to dashboard",
                })}
              </Link>
              <Link className="icon-button text-button" to="/context">
                {t("notFound.openContext", {
                  ns: "shell",
                  defaultValue: "Open Context",
                })}
              </Link>
            </div>
          </div>
        </section>
      </div>
    );
  }

  if (pageLoadState === "error") {
    return (
      <div
        className={`page-workspace ${focusMode ? "focus-mode" : ""}`}
        data-focus-mode={focusMode ? "on" : "off"}
      >
        {navigationRail}
        <section className="editor-panel">
          <header className="editor-header">
            <div className="editor-header-lead">
              <Breadcrumbs
                ariaLabel={t("editor.location", { defaultValue: "Page location" })}
                items={editorBreadcrumbItems}
              />
            </div>
            <div className="reader-actions">
              <button
                aria-label={t("editor.close")}
                className="icon-button"
                onClick={closeEditor}
                type="button"
              >
                <X size={16} aria-hidden="true" />
              </button>
            </div>
          </header>
          <div className="empty-state empty-state-rich" role="alert">
            <h2>{t("editor.loadFailed", { defaultValue: "Load failed" })}</h2>
            <p>
              {t("editor.loadFailedBody", {
                defaultValue:
                  "This page could not be loaded, so editing is disabled to protect its existing content.",
              })}
            </p>
            <div className="empty-state-actions">
              <Link className="icon-button text-button primary" to="/context">
                {t("editor.returnToContext", { defaultValue: "Return to Context" })}
              </Link>
            </div>
          </div>
        </section>
      </div>
    );
  }

  return (
    <div
      className={`page-workspace ${focusMode ? "focus-mode" : ""}`}
      data-focus-mode={focusMode ? "on" : "off"}
      data-save-state={saveState}
    >
      {navigationRail}
      <section className="editor-panel">
        <header className="editor-header">
          <div className="editor-header-lead">
            <Breadcrumbs
              ariaLabel={t("editor.location", { defaultValue: "Page location" })}
              items={editorBreadcrumbItems}
            />
            <p className="meta-line">
              {editorMeta(page, hasUnsavedChanges, t, savedJustNow)}
            </p>
          </div>
          <div className="reader-actions">
            {!focusMode ? (
              <SyncPill
                state={saveState}
                publicationState={publicationState}
                isReady={isEditorReady}
                hasPersistedPage={Boolean(page)}
                lastSavedAt={lastSavedAt}
                now={saveRelativeNow}
                onRetry={() => void save({ publish: false })}
                blockedReason={saveBlockedReason}
              />
            ) : null}
            {!focusMode ? (
              <EditorPublicationActions
                canPublish={canPublishFromEditor}
                canSave={!saveBlockedReason && saveState === "dirty"}
                saveDisabledReason={saveBlockedReason}
                saving={saveState === "saving" || saveState === "saving_with_newer_edits"}
                onPublish={() => void save({ publish: true })}
                onSave={() => void save({ publish: false })}
              />
            ) : null}
            {!focusMode && page ? (
              <details className="editor-share-control">
                <summary className="icon-button text-button">
                  {t("editor.share", { defaultValue: "Share" })}
                </summary>
                <PagePublicShareControl
                  canPublish={
                    workspaceRole === "owner" ||
                    (workspaceVisibility === "public" && workspaceRole === "editor")
                  }
                  canUnpublish={
                    workspaceRole === "owner" || workspaceRole === "editor"
                  }
                  isPublic={Boolean(page.is_public)}
                  onPublicChange={(isPublic) =>
                    setPage((current) => current ? { ...current, is_public: isPublic } : current)
                  }
                  pageId={page.id}
                  publishDisabledReason={
                    !page.current_published_version_id
                      ? t("reader.visibilityNeedsPublishedVersion")
                      : null
                  }
                  publishedVersionId={page.current_published_version_id ?? null}
                  publishedVersionNo={page.published_version_no ?? null}
                  workspaceId={workspaceId}
                  slug={page.slug}
                  workspaceSlug={workspaceSlug}
                  workspaceVisibility={workspaceVisibility}
                />
              </details>
            ) : null}
            <button
              aria-label={t("editor.close")}
              className="icon-button"
              onClick={closeEditor}
              type="button"
            >
              <X size={16} aria-hidden="true" />
            </button>
          </div>
        </header>
        {status ? <StatusBanner tone="warning">{status}</StatusBanner> : null}
        {stalePublishState !== "idle" ? (
          <StatusBanner
            tone="warning"
            action={
              <button
                className="inline-action"
                disabled={stalePublishState === "reloading"}
                onClick={() => void reloadAfterStalePublish()}
                type="button"
              >
                {stalePublishState === "reloading"
                  ? t("editor.publishStaleReloading", {
                      defaultValue: "Reloading current draft",
                    })
                  : t("editor.publishStaleReload", {
                      defaultValue: "Reload current draft",
                    })}
              </button>
            }
          >
            {stalePublishState === "error"
              ? t("editor.publishStaleReloadFailed", {
                  defaultValue:
                    "The current draft could not be reloaded. Publishing remains blocked; try again.",
                })
              : t("editor.publishStaleDraft", {
                  defaultValue:
                    "This draft is no longer the latest one for this page. Reload to publish the current draft.",
                })}
          </StatusBanner>
        ) : null}
        <ContextImageUploadStatusRegion
          batches={imageUploadBatches}
          entries={imageUploadEntries}
          onCancel={cancelImageUpload}
          onCancelAll={cancelAllContextImageUploads}
          onRemove={removeContextImageUpload}
          onRetry={retryContextImageUpload}
          onRetryFailed={retryFailedContextImageUploads}
        />
        {conflict ? (
          <StatusBanner
            tone="warning"
            action={
              <span className="banner-actions">
                <button
                  className="inline-action"
                  type="button"
                  onClick={viewConflictChanges}
                  disabled={conflictDiffLoading}
                >
                  {conflictDiffLoading
                    ? t("editor.loadingChanges", {
                        defaultValue: "Loading changes",
                      })
                    : t("editor.viewTheirChanges", {
                        defaultValue: "View their changes",
                      })}
                </button>
                <button
                  className="inline-action"
                  type="button"
                  onClick={preserveAlternative}
                  disabled={isSaving}
                >
                  {t("editor.preserveAlternative", {
                    defaultValue: "Preserve mine as alternative",
                  })}
                </button>
                <button
                  className="inline-action"
                  type="button"
                  onClick={() => void reloadLatest()}
                  disabled={isSaving}
                >
                  {t("editor.conflictBannerReloadLatest", {
                    defaultValue: "Reload latest",
                  })}
                </button>
              </span>
            }
          >
            {t("editor.conflictBannerMessage", {
              defaultValue:
                "Someone else updated this page. A newer version is available.",
            })}
          </StatusBanner>
        ) : null}
        {conflict && conflictDiffOpen ? (
          <ConflictDiffPanel
            diff={conflictDiff}
            error={conflictDiffError}
            loading={conflictDiffLoading}
            onClose={() => setConflictDiffOpen(false)}
          />
        ) : null}
        <div className="editor-page-header">
          <div className="editor-title-row">
            <textarea
              aria-describedby={titleValidationMessage ? "editor-title-validation" : undefined}
              aria-invalid={
                titleValidationMessage && title.trim().length === 0 ? "true" : undefined
              }
              aria-label={t("editor.titleAria", { defaultValue: "Title" })}
              className="editor-title-input"
              data-testid="page-title-editor"
              maxLength={TITLE_MAX_LENGTH}
              onBlur={() => setTitleValidationRequested(true)}
              onChange={(event) => updateTitle(event.currentTarget.value)}
              onKeyDown={handleTitleKeyDown}
              placeholder={t("editor.titlePlaceholder", { defaultValue: "Untitled" })}
              ref={titleRef}
              rows={1}
              value={title}
            />
            {!singlePaneViewport ? (
              <div className="editor-title-tags">
                <ChipInput
                  ariaLabel={t("editor.tagsAria", { defaultValue: "Tags" })}
                  onChange={updateTags}
                  onQueryChange={setTagQuery}
                  placeholder={t("editor.tagPlaceholder", { defaultValue: "+ Tag" })}
                  suggestions={tagSuggestions}
                  value={normalizedTags}
                />
              </div>
            ) : null}
            <button
              aria-label={t("editor.formatDocumentAria", { defaultValue: "Format document" })}
              className="editor-format-button icon-button"
              disabled={formatting || isSaving}
              onClick={handleFormatNow}
              title={t("editor.formatDocumentTitle", {
                defaultValue: "Format (normalize Markdown)",
              })}
              type="button"
            >
              <Wand2 size={16} aria-hidden="true" />
            </button>
            <button
              aria-controls={propertiesOpen ? propertiesPopoverId : undefined}
              aria-expanded={propertiesOpen}
              aria-label={t("editor.pageProperties", { defaultValue: "Page properties" })}
              className="editor-properties-button icon-button"
              onClick={() => setPropertiesOpen((current) => !current)}
              ref={propertiesButtonRef}
              title={t("editor.pageProperties", { defaultValue: "Page properties" })}
              type="button"
            >
              <Settings2 size={16} aria-hidden="true" />
            </button>
          </div>
          {titleValidationMessage ? (
            <p className="field-error editor-title-validation" id="editor-title-validation">
              {titleValidationMessage}
            </p>
          ) : null}
          {propertiesOpen ? (
            <div
              aria-labelledby={propertiesPopoverTitleId}
              className="editor-properties-popover"
              id={propertiesPopoverId}
              ref={propertiesPopoverRef}
              role="region"
              style={{
                left: propertiesPopoverPosition?.left ?? 0,
                top: propertiesPopoverPosition?.top ?? 0,
                visibility: propertiesPopoverPosition ? undefined : "hidden",
              }}
            >
              <header className="editor-properties-popover-header">
                <span id={propertiesPopoverTitleId}>
                  {t("editor.pageProperties", { defaultValue: "Page properties" })}
                </span>
                <button
                  aria-label={t("editor.closePropertiesAria", { defaultValue: "Close properties" })}
                  className="icon-button"
                  onClick={() => {
                    setPropertiesOpen(false);
                    propertiesButtonRef.current?.focus();
                  }}
                  type="button"
                >
                  <X size={14} aria-hidden="true" />
                </button>
              </header>
              <div className="editor-properties-popover-body">
              {singlePaneViewport ? (
                <FormField
                  htmlFor="editor-mobile-tags"
                  label={t("editor.tagsAria", { defaultValue: "Tags" })}
                >
                  <ChipInput
                    ariaLabel={t("editor.tagsAria", { defaultValue: "Tags" })}
                    id="editor-mobile-tags"
                    onChange={updateTags}
                    onQueryChange={setTagQuery}
                    placeholder={t("editor.tagPlaceholder", { defaultValue: "+ Tag" })}
                    suggestions={tagSuggestions}
                    value={normalizedTags}
                  />
                </FormField>
              ) : null}
              {mode === "create" || hasPersistedPage ? (
                <FormField label={t("editor.destinationLabel", { defaultValue: "Destination" })}>
                  <TreePickerDropdown
                    ariaLabel={t("editor.destinationLabel", { defaultValue: "Destination" })}
                    disabled={
                      destinationOptions.length === 0 ||
                      isSaving ||
                      (mode === "create" && hasPersistedPage)
                    }
                    emptyLabel={t("editor.destinationEmptyLabel", {
                      defaultValue: "Context / Inbox",
                    })}
                    nodes={destinationOptions}
                    onMenuElementChange={setPropertiesDestinationMenu}
                    selectedId={destinationNodeId}
                    onChange={updateDestination}
                  />
                </FormField>
              ) : null}
              <FormField
                hint={t(`editor.pageTypes.${pageType}.description`)}
                hintId="editor-page-type-hint"
                htmlFor="editor-page-type"
                label={t("editor.pageType", { defaultValue: "Page type" })}
              >
                {(ids) => (
                  <select
                    aria-describedby={ids.hintId}
                    disabled={isSaving}
                    id="editor-page-type"
                    onChange={(event) => setPageType(event.target.value as PageType)}
                    value={pageType}
                  >
                    {PAGE_TYPE_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>
                        {t(`editor.pageTypes.${option.value}.label`, {
                          defaultValue: option.label,
                        })}
                      </option>
                    ))}
                  </select>
                )}
              </FormField>
              <AdvancedSection
                label={`${t("editor.advancedSettingsLabel", { defaultValue: "Advanced settings" })}${aliasValidationMessage || aliasItemError ? ` — ${t("editor.hasErrors", { defaultValue: "Has errors" })}` : ""}`}
                open={showAdvancedProperties}
                onOpenChange={setShowAdvancedProperties}
              >
                <FormField
                  error={aliasValidationMessage ?? aliasItemError}
                  errorId="aliases-error"
                  hint={t("editor.aliasesHint", {
                    defaultValue: "Alternate page names used by search and wikilinks.",
                  })}
                  label={t("editor.aliasesLabel", { defaultValue: "Aliases" })}
                >
                  {(ids) => (
                    <ChipInput
                      ariaDescribedBy={joinIds(ids.hintId, ids.errorId)}
                      ariaInvalid={Boolean(aliasValidationMessage ?? aliasItemError)}
                      ariaLabel={t("editor.aliasesLabel", { defaultValue: "Aliases" })}
                      id="editor-aliases"
                      maxItemLength={80}
                      onChange={updateAliases}
                      onItemTooLong={(alias) => setAliasItemError(t(
                        "editor.aliasTooLong",
                        {
                          alias,
                          count: 80,
                          defaultValue: "\"{{alias}}\" is too long. Each alias can be at most {{count}} characters.",
                        },
                      ))}
                      onQueryChange={() => setAliasItemError(null)}
                      placeholder={t("editor.aliasesPlaceholder", {
                        defaultValue: "launch plan, release brief",
                      })}
                      stripLeadingHash={false}
                      truncateItems={false}
                      value={normalizedAliases}
                    />
                  )}
                </FormField>
                <FormField
                  className="editor-property-grid-wide"
                  hint={t("editor.slugHint", {
                    defaultValue: "URL identifier generated from the title.",
                  })}
                  htmlFor="editor-slug"
                  label={t("editor.slugLabel", { defaultValue: "Slug" })}
                >
                  <input
                    aria-label={t("editor.slugLabel", { defaultValue: "Slug" })}
                    id="editor-slug"
                    value={pageSlug}
                    disabled={mode === "edit" || isSaving || hasPersistedPage}
                    onChange={(event) => updateSlug(event.target.value)}
                    placeholder={t("editor.slugPlaceholder", {
                      defaultValue: "auto-derived-from-title",
                    })}
                  />
                </FormField>
              </AdvancedSection>
              </div>
            </div>
          ) : null}
        </div>
        <div
          className="editor-command-bar editor-command-bar-primary"
          aria-label={t("editor.editorControlsAria", { defaultValue: "Editor controls" })}
        >
          <input
            aria-hidden="true"
            className="visually-hidden"
            disabled={mediaUploadCapabilityState !== "ready"}
            multiple
            accept={contextMediaInputAccept}
            onChange={handleImageFileInput}
            ref={imageFileInputRef}
            tabIndex={-1}
            type="file"
          />
          <div
            className="segmented-control compact editor-mode-tabs"
            role="tablist"
            aria-label={t("editor.editorModeAria", { defaultValue: "Editor mode" })}
            onKeyDown={handleModeTabKeyDown}
            tabIndex={-1}
          >
            <button
              aria-controls="page-editor-source-panel"
              aria-selected={activeEditorMode === "source"}
              className={activeEditorMode === "source" ? "active" : ""}
              id="page-editor-source-tab"
              onClick={() => setEditorMode("source")}
              role="tab"
              tabIndex={activeEditorMode === "source" ? 0 : -1}
              title={t("editor.sourceModeTitle", { defaultValue: "Source mode (Ctrl+E)" })}
              type="button"
            >
              {t("editor.sourceModeLabel", { defaultValue: "Source" })}
            </button>
            <button
              aria-controls="page-editor-reading-panel"
              aria-selected={activeEditorMode === "reading"}
              className={activeEditorMode === "reading" ? "active" : ""}
              id="page-editor-reading-tab"
              onClick={() => setEditorMode("reading")}
              role="tab"
              tabIndex={activeEditorMode === "reading" ? 0 : -1}
              title={t("editor.readingModeTitle", { defaultValue: "Reading mode (Ctrl+E)" })}
              type="button"
            >
              {t("editor.readingModeLabel", { defaultValue: "Reading" })}
            </button>
            {!singlePaneViewport ? (
              <button
                aria-controls="page-editor-source-panel page-editor-reading-panel"
                aria-selected={activeEditorMode === "split"}
                className={activeEditorMode === "split" ? "active" : ""}
                id="page-editor-split-tab"
                onClick={() => setEditorMode("split")}
                role="tab"
                tabIndex={activeEditorMode === "split" ? 0 : -1}
                title={t("editor.splitModeTitle", { defaultValue: "Split mode (Ctrl+E)" })}
                type="button"
              >
                {t("editor.splitModeLabel", { defaultValue: "Split" })}
              </button>
            ) : null}
          </div>
          {activeEditorMode === "split" ? (
            <button
              aria-pressed={linkedScrolling}
              className={`compact-button editor-linked-scroll-toggle ${linkedScrolling ? "active" : ""}`}
              onClick={() => setLinkedScrolling((current) => !current)}
              type="button"
            >
              {t("editor.linkedScrolling", { defaultValue: "Linked scrolling" })}
            </button>
          ) : null}
          <EditorActionBar
            commandStates={activeCommandStates}
            isMobile={compactActionBarViewport}
            labels={{
              more: t("editor.actionBar.more", { defaultValue: "More editor actions" }),
              moreMenu: t("editor.actionBar.moreMenu", { defaultValue: "More editor actions" }),
              moreShort: t("editor.actionBar.moreShort", { defaultValue: "More" }),
              readingDisabledReason: t("editor.actionBar.readingDisabled", {
                defaultValue: "Editing commands are unavailable in Reading mode.",
              }),
              toolbar: t("editor.actionBar.toolbar", { defaultValue: "Editor actions" }),
            }}
            mode={activeEditorMode}
            onCommand={(commandId) => executeEditorCommand(commandId)}
            resolveLabel={(labelKey, fallbackLabel) =>
              t(labelKey, { defaultValue: fallbackLabel })
            }
          />
          <div className="editor-command-cluster editor-command-cluster-trailing">
            <div className="editor-command-actions">
            <button
              aria-label={focusModeLabel}
              aria-pressed={focusMode}
              className={`icon-button editor-focus-toggle ${focusMode ? "active" : ""}`}
              onClick={() => setFocusMode((current) => !current)}
              title={focusModeLabel}
              type="button"
            >
              {focusMode ? (
                <Minimize2 size={16} aria-hidden="true" />
              ) : (
                <Maximize2 size={16} aria-hidden="true" />
              )}
            </button>
            <button
              aria-label={t("editor.keyboardShortcutsAria", { defaultValue: "Keyboard shortcuts" })}
              className="icon-button"
              onClick={() => setShortcutHelpOpen(true)}
              title={t("editor.keyboardShortcutsTitle", { defaultValue: "Keyboard shortcuts (?)" })}
              type="button"
            >
              <Keyboard size={16} aria-hidden="true" />
            </button>
            {page ? (
              <button
                aria-haspopup="dialog"
                aria-label={t("editor.managePageMedia", { defaultValue: "Manage page media" })}
                className="icon-button"
                onClick={() => setMediaManagerOpen(true)}
                title={t("editor.managePageMedia", { defaultValue: "Manage page media" })}
                type="button"
              >
                <Images size={16} aria-hidden="true" />
              </button>
            ) : null}
            </div>
          </div>
        </div>
        {focusMode ? (
          <div className="editor-focus-meta">
            <SyncPill
              state={saveState}
              publicationState={publicationState}
              isReady={isEditorReady}
              hasPersistedPage={Boolean(page)}
              lastSavedAt={lastSavedAt}
              now={saveRelativeNow}
              onRetry={() => void save({ publish: false })}
              blockedReason={saveBlockedReason}
            />
            <EditorPublicationActions
              canPublish={canPublishFromEditor}
              canSave={!saveBlockedReason && saveState === "dirty"}
              saveDisabledReason={saveBlockedReason}
              saving={saveState === "saving" || saveState === "saving_with_newer_edits"}
              onPublish={() => void save({ publish: true })}
              onSave={() => void save({ publish: false })}
            />
            <span className="meta-line">
              {editorMeta(page, hasUnsavedChanges, t, savedJustNow)}
            </span>
            <button
              aria-label={t("editor.close")}
              className="icon-button"
              onClick={closeEditor}
              type="button"
            >
              <X size={16} aria-hidden="true" />
            </button>
          </div>
        ) : null}
        {blockMenuOpen ? (
          <BlockMenu
            entries={filteredBlockEntries}
            highlightedIndex={highlightedBlockIndex}
            onChoose={insertBlock}
            onHighlight={setHighlightedBlockIndex}
            onKeyDown={handleBlockMenuKeyDown}
            onQueryChange={setBlockMenuQuery}
            menuRef={blockMenuRef}
            position={blockMenuPosition}
            query={blockMenuQuery}
            searchRef={blockMenuSearchRef}
          />
        ) : null}
        {emojiPicker ? (
          <EmojiPicker
            onClose={() => closeEmojiPicker({ restoreFocus: true })}
            onSelect={insertEmoji}
            style={{ left: emojiPicker.left, top: emojiPicker.top }}
          />
        ) : null}
        {shortcutHelpOpen ? (
          <KeyboardShortcutsModal
            groups={shortcutGroups}
            onClose={() => setShortcutHelpOpen(false)}
          />
        ) : null}
        {mediaManagerOpen && page ? (
          <ContextMediaManagerDialog
            onClose={() => setMediaManagerOpen(false)}
            pageId={page.id}
            workspaceId={page.workspace_id}
          />
        ) : null}
        {fencePublishPrompt ? (
          <Dialog
            className="fence-publish-dialog"
            labelledBy="fence-publish-dialog-title"
            onClose={() => setFencePublishPrompt(null)}
          >
            <section className="dialog-form">
              <header className="dialog-header">
                <div>
                  <h2 id="fence-publish-dialog-title">
                    {t("editor.fencePublishTitle", {
                      defaultValue: "Check the code fence before publishing",
                    })}
                  </h2>
                  <p className="feature-subtitle">{fencePublishPrompt.message}</p>
                </div>
              </header>
              <p>
                {t("editor.fencePublishDescription", {
                  defaultValue:
                    "You can add the matching closing fence to your editor, publish the draft as written, or cancel.",
                })}
              </p>
              <div className="dialog-actions">
                <button
                  className="icon-button text-button"
                  onClick={() => setFencePublishPrompt(null)}
                  type="button"
                >
                  {t("editor.fencePublishCancel", { defaultValue: "Cancel" })}
                </button>
                <button
                  className="icon-button text-button"
                  onClick={publishDespiteFenceFinding}
                  type="button"
                >
                  {t("editor.fencePublishAnyway", {
                    defaultValue: "Publish anyway",
                  })}
                </button>
                <button
                  className="icon-button primary"
                  onClick={closeFenceAndPublish}
                  type="button"
                >
                  {t("editor.fenceCloseAndPublish", {
                    defaultValue: "Close the fence and publish",
                  })}
                </button>
              </div>
            </section>
          </Dialog>
        ) : null}
        <div
          className="editor-grid"
          data-layout={editorLayout}
          ref={editorGridRef}
          style={{
            "--editor-split-end": `${100 - splitPercent}fr`,
            "--editor-split-start": `${splitPercent}fr`,
          } as CSSProperties}
        >
          <div
            aria-hidden={showSourcePane ? undefined : "true"}
            aria-labelledby={
              activeEditorMode === "split" ? "page-editor-split-tab" : "page-editor-source-tab"
            }
            className="source-pane"
            hidden={!showSourcePane}
            id="page-editor-source-panel"
            inert={showSourcePane ? undefined : true}
            role="tabpanel"
          >
              {inlineToolbar.visible ? (
                <InlineToolbar
                  commandStates={activeCommandStates}
                  left={inlineToolbar.left}
                  onCommand={(commandId) => executeEditorCommand(commandId)}
                  top={inlineToolbar.top}
                />
              ) : null}
              {linkDialog ? (
                <LinkPopover
                  state={linkDialog}
                  surfaceRef={linkPopoverRef}
                  textRef={linkTextInputRef}
                  urlRef={linkUrlInputRef}
                  onCancel={() => closeLinkDialog({ restoreFocus: true })}
                  onChange={updateLinkDialog}
                  onSubmit={insertLink}
                />
              ) : null}
              {commentPreview ? (
                <CommentPreviewPopover
                  canCreate={Boolean(page) && !hasUnsavedChanges}
                  isSubmitting={commentSubmitting}
                  noteRef={commentNoteRef}
                  onCancel={() => closeCommentPreview({ restoreFocus: true })}
                  onCreate={createInlineComment}
                  onNoteChange={updateCommentNote}
                  state={commentPreview}
                />
              ) : null}
              {aiInline ? (
                <AiInlinePopover
                  state={aiInline}
                  onCancel={() => closeAiInlineEdit({ restoreFocus: true })}
                  onKeyDown={handleAiInlineKeyDown}
                />
              ) : null}
              {fenceFinding ? (
                <p className="editor-fence-hint" role="status">
                  {t("editor.unclosedFenceHint", {
                    defaultValue:
                      "Unclosed code fence at line {{line}}. You can keep editing; publishing will ask what to do.",
                    line: fenceFinding.line,
                  })}
                </p>
              ) : null}
              <MarkdownSourceEditor
                value={body}
                extensions={extensions}
                onChange={updateAuthoredBody}
                onCreateEditor={(view) => {
                  editorViewRef.current = view;
                }}
                placeholder={t("editor.bodyPlaceholder", {
                  defaultValue: "Start typing… (Press / for blocks)",
                })}
              />
          </div>
          {activeEditorMode === "split" ? (
            // This ARIA separator is deliberately keyboard-focusable and draggable.
            // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions, jsx-a11y/no-noninteractive-tabindex
            <div tabIndex={0}
              aria-controls="page-editor-source-panel page-editor-reading-panel"
              aria-label={t("editor.splitSeparator", { defaultValue: "Resize editor panes" })}
              aria-orientation={stackedSplitViewport ? "horizontal" : "vertical"}
              aria-valuemax={60}
              aria-valuemin={40}
              aria-valuenow={Math.round(splitPercent)}
              aria-valuetext={t("editor.splitRatio", {
                defaultValue: "Source pane {{percent}} percent",
                percent: Math.round(splitPercent),
              })}
              className="editor-split-separator"
              onKeyDown={handleSplitSeparatorKeyDown}
              onLostPointerCapture={(event) => {
                if (splitPointerIdRef.current === event.pointerId) {
                  splitPointerIdRef.current = null;
                }
              }}
              onPointerCancel={finishSplitPointer}
              onPointerDown={(event) => {
                splitPointerIdRef.current = event.pointerId;
                if (typeof event.currentTarget.setPointerCapture === "function") {
                  event.currentTarget.setPointerCapture(event.pointerId);
                }
                resizeSplitFromPointer(event.clientX, event.clientY);
              }}
              onPointerMove={(event) => {
                if (splitPointerIdRef.current === event.pointerId) {
                  resizeSplitFromPointer(event.clientX, event.clientY);
                }
              }}
              onPointerUp={finishSplitPointer}
              ref={splitSeparatorRef}
              role="separator"
            >
              <span aria-hidden="true" />
            </div>
          ) : null}
          {showPreviewPane ? (
            <div
              aria-labelledby={
                activeEditorMode === "split" ? "page-editor-split-tab" : "page-editor-reading-tab"
              }
              className="preview-pane"
              id="page-editor-reading-panel"
              ref={previewPaneRef}
              role="tabpanel"
              tabIndex={0}
            >
              <MarkdownView
                body={body}
                resolveContextMediaAsset={resolveContextMediaAsset}
                sourceLineNumbers
                stripLeadingHeading={title}
                wikilinkPages={pages}
              />
            </div>
          ) : null}
        </div>
      </section>
    </div>
  );
}

function ToolbarButton({
  active,
  children,
  label,
  onClick,
}: {
  active?: EditorCommandState;
  children: ReactNode;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      aria-label={label}
      aria-pressed={
        active === undefined
          ? undefined
          : active === "mixed"
            ? "mixed"
            : active === "active"
      }
      className={`editor-toolbar-button ${active && active !== "inactive" ? "active" : ""}`.trim()}
      onClick={onClick}
      title={label}
      type="button"
    >
      {children}
    </button>
  );
}

function ConflictDiffPanel({
  diff,
  error,
  loading,
  onClose,
}: {
  diff: PageVersionDiff | null;
  error: string | null;
  loading: boolean;
  onClose: () => void;
}) {
  const { t } = useTranslation("pages");
  return (
    <section
      className="editor-conflict-panel"
      aria-label={t("editor.conflictDiffPanelTitle", { defaultValue: "Server changes" })}
    >
      <div className="editor-conflict-panel-header">
        <div>
          <p className="eyebrow">
            {t("editor.conflictDiffPanelEyebrow", { defaultValue: "Conflict" })}
          </p>
          <h2>
            {t("editor.conflictDiffPanelTitle", { defaultValue: "Server changes" })}
          </h2>
        </div>
        <button
          aria-label={t("editor.closeServerChangesAria", { defaultValue: "Close server changes" })}
          className="icon-button"
          onClick={onClose}
          type="button"
        >
          <X size={16} aria-hidden="true" />
        </button>
      </div>
      {loading ? (
        <p className="page-history-empty">
          {t("editor.loadingServerChanges", { defaultValue: "Loading server changes…" })}
        </p>
      ) : null}
      {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
      {!loading && !error && diff ? <DiffViewer diff={diff} /> : null}
      {!loading && !error && !diff ? (
        <p className="page-history-empty">
          {t("editor.noServerChangesLoaded", { defaultValue: "No server changes loaded yet." })}
        </p>
      ) : null}
    </section>
  );
}

function metadataFromPagePayload(metadata: PageMetadata, page: PagePayload): PageMetadata {
  return {
    ...metadata,
    aliases: normalizeAliasesInput((page.aliases ?? []).join(", ")),
    tags: normalizeTagsInput((page.tags ?? []).join(", ")),
    type: supportedPageType(page.page_type) ?? metadata.type,
  };
}

function supportedPageType(value: string): PageType | null {
  return PAGE_TYPE_OPTIONS.some((option) => option.value === value) ? (value as PageType) : null;
}

function InlineToolbar({
  commandStates,
  left,
  onCommand,
  top,
}: {
  commandStates: ActiveCommandState;
  left: number;
  onCommand: (commandId: EditorCommandId) => void;
  top: number;
}) {
  const { t } = useTranslation("pages");
  const commands = (EDITOR_COMMANDS as readonly EditorCommand[]).filter(
    (command) => command.surfaces.includes("floating-toolbar"),
  );
  return (
    <div
      aria-label={t("editor.selectionToolbarAria", {
        defaultValue: "Selection formatting toolbar",
      })}
      className="editor-inline-toolbar"
      role="toolbar"
      style={{ left, top }}
    >
      {commands.map((command) => (
        <ToolbarButton
          active={commandStates[command.id]}
          key={command.id}
          label={t(command.labelKey, { defaultValue: command.fallbackLabel })}
          onClick={() => onCommand(command.id)}
        >
          <EditorCommandGlyph commandId={command.id} />
        </ToolbarButton>
      ))}
    </div>
  );
}

function EditorCommandGlyph({ commandId }: { commandId: EditorCommandId }) {
  if (commandId === "bold") return <Bold size={15} aria-hidden="true" />;
  if (commandId === "italic") return <Italic size={15} aria-hidden="true" />;
  if (commandId === "strikethrough") {
    return <Strikethrough size={15} aria-hidden="true" />;
  }
  if (commandId === "inline-code") return <Code size={15} aria-hidden="true" />;
  if (commandId === "link") return <Link2 size={15} aria-hidden="true" />;
  if (commandId === "wikilink") return <Brackets size={15} aria-hidden="true" />;
  if (commandId === "citation") return <AtSign size={15} aria-hidden="true" />;
  if (commandId === "comment") {
    return <MessageSquarePlus size={15} aria-hidden="true" />;
  }
  return <span aria-hidden="true">•</span>;
}

function LinkPopover({
  onCancel,
  onChange,
  onSubmit,
  state,
  surfaceRef,
  textRef,
  urlRef,
}: {
  onCancel: () => void;
  onChange: (patch: Partial<Pick<LinkDialogState, "text" | "url">>) => void;
  onSubmit: (event: ReactFormEvent) => void;
  state: LinkDialogState;
  surfaceRef: RefObject<HTMLFormElement | null>;
  textRef: RefObject<HTMLInputElement | null>;
  urlRef: RefObject<HTMLInputElement | null>;
}) {
  const { t } = useTranslation("pages");
  return (
    <form
      aria-label={t("editor.insertLinkAria", { defaultValue: "Insert link" })}
      className="editor-link-popover"
      onSubmit={onSubmit}
      ref={surfaceRef}
      role="dialog"
      style={{ left: state.left, top: state.top }}
    >
      <div className="editor-link-header">
        <div>
          <p className="eyebrow">{t("editor.linkEyebrow", { defaultValue: "Link" })}</p>
          <h3>{t("editor.insertLinkAria", { defaultValue: "Insert link" })}</h3>
        </div>
        <button
          aria-label={t("editor.cancelLinkAria", { defaultValue: "Cancel link insertion" })}
          onClick={onCancel}
          type="button"
        >
          <X size={15} aria-hidden="true" />
        </button>
      </div>
      <label className="editor-link-field">
        {t("editor.linkTextLabel", { defaultValue: "Text" })}
        <input
          onChange={(event) => onChange({ text: event.target.value })}
          placeholder={t("editor.linkTextPlaceholder", { defaultValue: "Link text" })}
          ref={textRef}
          type="text"
          value={state.text}
        />
      </label>
      <label className="editor-link-field">
        {t("editor.linkUrlLabel", { defaultValue: "URL" })}
        <input
          onChange={(event) => onChange({ url: event.target.value })}
          inputMode="url"
          placeholder="https://example.com"
          ref={urlRef}
          type="text"
          value={state.url}
        />
      </label>
      <div className="editor-link-actions">
        <button onClick={onCancel} type="button">
          {t("editor.fencePublishCancel", { defaultValue: "Cancel" })}
        </button>
        <button className="primary-button" disabled={!normalizeLinkDestination(state.url)} type="submit">
          {t("editor.linkInsertSubmit", { defaultValue: "Insert" })}
        </button>
      </div>
    </form>
  );
}

function CommentPreviewPopover({
  canCreate,
  isSubmitting,
  noteRef,
  onCancel,
  onCreate,
  onNoteChange,
  state,
}: {
  canCreate: boolean;
  isSubmitting: boolean;
  noteRef: RefObject<HTMLTextAreaElement | null>;
  onCancel: () => void;
  onCreate: () => void;
  onNoteChange: (note: string) => void;
  state: CommentPreviewState;
}) {
  const { t } = useTranslation("pages");
  const readyToCreate = canCreate && state.note.trim().length > 0 && !isSubmitting;
  return (
    <section
      aria-label={t("editor.commentPreviewAria", { defaultValue: "Comment preview" })}
      className="editor-comment-popover"
      role="dialog"
      style={{ left: state.left, top: state.top }}
    >
      <header className="editor-comment-header">
        <div>
          <p className="eyebrow">{t("editor.commentEyebrow", { defaultValue: "Comment" })}</p>
          <h3>{t("editor.commentPrepareTitle", { defaultValue: "Prepare inline comment" })}</h3>
        </div>
        <button
          aria-label={t("editor.closeCommentPreviewAria", { defaultValue: "Close comment preview" })}
          onClick={onCancel}
          type="button"
        >
          <X size={15} aria-hidden="true" />
        </button>
      </header>
      <div
        className="editor-comment-selection"
        aria-label={t("editor.selectedTextAria", { defaultValue: "Selected text" })}
      >
        {state.selectedText}
      </div>
      <label className="editor-comment-field">
        {t("editor.commentNoteLabel", { defaultValue: "Note" })}
        <textarea
          onChange={(event) => onNoteChange(event.target.value)}
          placeholder={t("editor.commentNotePlaceholder", {
            defaultValue: "Write a comment for this selection",
          })}
          ref={noteRef}
          rows={3}
          value={state.note}
        />
      </label>
      <div className="editor-comment-status" role="status">
        {canCreate
          ? t("editor.commentAnchoredHint", {
              defaultValue: "Anchored to the current saved page version.",
            })
          : t("editor.commentSaveFirstHint", {
              defaultValue: "Save the page before creating an anchored comment.",
            })}
      </div>
      <div className="editor-comment-actions">
        <button onClick={onCancel} type="button">
          {t("editor.fencePublishCancel", { defaultValue: "Cancel" })}
        </button>
        <button
          className="primary-button"
          disabled={!readyToCreate}
          onClick={onCreate}
          type="button"
        >
          {isSubmitting
            ? t("editor.commentCreating", { defaultValue: "Creating…" })
            : t("editor.commentCreate", { defaultValue: "Create" })}
        </button>
      </div>
    </section>
  );
}

function AiInlinePopover({
  onCancel,
  onKeyDown,
  state,
}: {
  onCancel: () => void;
  onKeyDown: (event: ReactKeyboardEvent) => void;
  state: AiInlineState;
}) {
  const { t } = useTranslation("pages");
  return (
    // The dialog owns Escape handling while its buttons remain the tab stops.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
    <section
      aria-label={t("editor.aiInlineUnavailableAria", {
        defaultValue: "AI inline edit unavailable",
      })}
      className="editor-ai-popover"
      onKeyDown={onKeyDown}
      role="dialog"
      style={{ left: state.left, top: state.top }}
      tabIndex={-1}
    >
      <header className="editor-ai-header">
        <div>
          <p className="eyebrow">{t("editor.aiInlineEyebrow", { defaultValue: "AI inline edit" })}</p>
          <h3>{t("editor.aiInlineComingSoon", { defaultValue: "Coming in Sprint 9" })}</h3>
        </div>
        <button
          aria-label={t("editor.closeAiInlineAria", { defaultValue: "Close AI inline edit" })}
          className="icon-button"
          onClick={onCancel}
          type="button"
        >
          <X size={16} aria-hidden="true" />
        </button>
      </header>
      <div
        className="editor-ai-selection"
        aria-label={t("editor.selectedTextAria", { defaultValue: "Selected text" })}
      >
        {state.selectedText}
      </div>
      <div className="editor-ai-status" role="status">
        {t("editor.aiInlineDisabledStatus", {
          defaultValue: "AI rewrite controls are disabled in this build.",
        })}
      </div>
    </section>
  );
}

function BlockMenu({
  entries,
  highlightedIndex,
  menuRef,
  onChoose,
  onHighlight,
  onKeyDown,
  onQueryChange,
  position,
  query,
  searchRef,
}: {
  entries: BlockMenuEntry[];
  highlightedIndex: number;
  menuRef: RefObject<HTMLDivElement | null>;
  onChoose: (entry: BlockMenuEntry) => void;
  onHighlight: (index: number) => void;
  onKeyDown: (event: ReactKeyboardEvent) => void;
  onQueryChange: (query: string) => void;
  position: FloatingPosition | null;
  query: string;
  searchRef: RefObject<HTMLInputElement | null>;
}) {
  const { t } = useTranslation("pages");
  const generatedListboxId = useId();
  const listboxId = `page-editor-block-menu-${generatedListboxId.replace(/:/g, "")}`;
  const highlightedEntry = entries[highlightedIndex];
  const activeOptionId = highlightedEntry
    ? `${listboxId}-option-${highlightedEntry.id}`
    : undefined;
  return (
    // The dialog delegates arrow/Escape handling to its search/listbox model.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
    <div
      aria-label={t("editor.insertBlockAria", { defaultValue: "Insert block" })}
      className="editor-block-menu"
      onKeyDown={onKeyDown}
      ref={menuRef}
      role="dialog"
      style={position ? { left: position.left, top: position.top } : undefined}
      tabIndex={-1}
    >
      <input
        aria-activedescendant={activeOptionId}
        aria-autocomplete="list"
        aria-controls={listboxId}
        aria-expanded="true"
        aria-label={t("editor.filterBlocksAria", { defaultValue: "Filter block commands" })}
        className="editor-block-menu-search"
        onChange={(event) => onQueryChange(event.target.value)}
        placeholder={t("editor.searchBlocksPlaceholder", { defaultValue: "Search blocks" })}
        ref={searchRef}
        role="combobox"
        value={query}
      />
      <div className="editor-block-menu-list" id={listboxId} role="listbox">
        {entries.length === 0 ? (
          <div className="editor-block-menu-empty">
            {t("editor.noMatchingBlocks", { defaultValue: "No matching blocks" })}
          </div>
        ) : (
          BLOCK_MENU_GROUPS.map((group) => {
            const groupEntries = entries.filter((entry) => entry.group === group);
            if (groupEntries.length === 0) {
              return null;
            }
            return (
              <div
                aria-label={t(`blocks.groups.${group}`, {
                  defaultValue: BLOCK_MENU_GROUP_LABELS[group],
                })}
                className="editor-block-menu-group"
                key={group}
                role="group"
              >
                <p aria-hidden="true" className="eyebrow">
                  {t(`blocks.groups.${group}`, { defaultValue: BLOCK_MENU_GROUP_LABELS[group] })}
                </p>
                {groupEntries.map((entry) => {
                  const index = entries.indexOf(entry);
                  const active = index === highlightedIndex;
                  return (
                    <button
                      aria-selected={active}
                      className={`editor-block-menu-item ${active ? "active" : ""}`}
                      data-command-id={entry.commandId}
                      id={`${listboxId}-option-${entry.id}`}
                      key={entry.id}
                      onClick={() => onChoose(entry)}
                      onMouseEnter={() => onHighlight(index)}
                      role="option"
                      type="button"
                    >
                      <span>{t(entry.labelKey, { defaultValue: entry.label })}</span>
                    </button>
                  );
                })}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

function ContextImageUploadStatusRegion({
  batches,
  entries,
  onCancel,
  onCancelAll,
  onRemove,
  onRetry,
  onRetryFailed,
}: {
  batches: MediaUploadCoordinatorState[];
  entries: ContextImageUploadEntry[];
  onCancel: (marker: string) => void;
  onCancelAll: () => void;
  onRemove: (marker: string) => void;
  onRetry: (marker: string) => void;
  onRetryFailed: () => void;
}) {
  const { t } = useTranslation("pages");
  if (entries.length === 0 && batches.length === 0) {
    return null;
  }
  const summary = summarizeMediaUploadBatches(batches);
  const policyBlockedMarkers = new Set(
    batches
      .filter((batch) =>
        batch.policyRefreshRequired || Boolean(batch.policyRejectionMessage))
      .flatMap((batch) => batch.stalePolicyItemIds),
  );
  const hasPending = entries.some(
    (entry) => entry.status === "queued" || entry.status === "uploading" ||
      entry.status === "cancelling",
  );
  const hasRetryable = entries.some(
    (entry) => entry.insertionSlotAvailable !== false &&
      (entry.status === "failed" || entry.status === "cancelled" ||
        entry.status === "outcome-unknown"),
  );
  const hasFailed = entries.some(
    (entry) => entry.status === "failed" &&
      entry.insertionSlotAvailable !== false &&
      !policyBlockedMarkers.has(entry.marker),
  );
  const rejectionCodes = new Map(
    batches.flatMap((batch) =>
      batch.items.flatMap((item) =>
        item.rejectionCode ? [[item.id, item.rejectionCode] as const] : [],
      ),
    ),
  );
  const totalBytesLabel = formatUploadProgressBytes(summary.totalBytes);
  const aggregateLabel = summary.aggregateProgress.kind === "indeterminate"
    ? t("editor.imageUploadAggregateIndeterminate", {
        bytes: totalBytesLabel,
        defaultValue: "{{succeeded}} of {{total}} files finished ({{bytes}} total). Transfer progress is indeterminate.",
        succeeded: summary.succeededCount,
        total: summary.totalCount,
      })
    : summary.aggregateProgress.kind === "bytes"
      ? t("editor.imageUploadAggregateBytes", {
          defaultValue: "{{loaded}} of {{bytes}} uploaded across {{total}} files.",
          loaded: formatUploadProgressBytes(summary.aggregateProgress.loaded),
          bytes: formatUploadProgressBytes(summary.aggregateProgress.total),
          total: summary.totalCount,
        })
      : t("editor.imageUploadAggregateQueued", {
          bytes: totalBytesLabel,
          defaultValue: "{{total}} files selected ({{bytes}} total); {{unresolved}} unresolved.",
          total: summary.totalCount,
          unresolved: summary.unresolvedCount,
        });
  const actionableSummary = summary.policyRefreshingBatchCount > 0
    ? t("editor.imageUploadActionPolicyRefreshing", {
        defaultValue: "Upload policy changed. Refreshing limits and checking the original batch.",
      })
    : summary.policyRefreshFailedBatchCount > 0
      ? t("editor.imageUploadActionPolicyRefreshFailed", {
          defaultValue: "Current upload limits could not be refreshed. Remove unresolved items or try inserting the files again later.",
        })
      : summary.policyRejectedBatchCount > 0
      ? t("editor.imageUploadActionPolicyRejected", {
          defaultValue: "The original batch no longer meets current limits. Remove unresolved items and choose the batch again.",
        })
      : summary.outcomeUnknownCount > 0
        ? t("editor.imageUploadActionOutcomeUnknown", {
            defaultValue: "Replay each outcome-unknown upload or remove it before saving.",
          })
        : summary.failedCount > 0
          ? t("editor.imageUploadActionFailed", {
              defaultValue: "Retry failed uploads or remove them before saving.",
            })
          : summary.rejectedCount > 0
            ? t("editor.imageUploadActionRejected", {
                defaultValue: "Some selected files were rejected. Choose files within the current limits.",
              })
            : null;
  return (
    <section
      aria-label={t("editor.imageUploadQueue", {
        defaultValue: "Image upload queue",
      })}
      aria-live="polite"
      aria-relevant="additions text"
      className="editor-upload-queue"
    >
      <p>{aggregateLabel}</p>
      {summary.aggregateProgress.kind === "indeterminate" ? (
        <progress
          aria-label={t("editor.imageUploadAggregateProgress", {
            defaultValue: "Aggregate upload progress",
          })}
        />
      ) : (
        <progress
          aria-label={t("editor.imageUploadAggregateProgress", {
            defaultValue: "Aggregate upload progress",
          })}
          max={Math.max(1, summary.aggregateProgress.kind === "bytes"
            ? summary.aggregateProgress.total
            : summary.totalBytes)}
          value={summary.aggregateProgress.kind === "bytes"
            ? summary.aggregateProgress.loaded
            : 0}
        />
      )}
      {actionableSummary ? <p>{actionableSummary}</p> : null}
      {hasPending || hasRetryable ? (
        <div className="banner-actions">
          {hasPending ? (
            <button
              className="text-button compact-button"
              type="button"
              onClick={onCancelAll}
            >
              {t("editor.imageUploadCancelAll", { defaultValue: "Cancel all" })}
            </button>
          ) : null}
          {hasFailed ? (
            <button
              className="text-button compact-button"
              type="button"
              onClick={onRetryFailed}
            >
              {t("editor.imageUploadRetryFailed", {
                defaultValue: "Retry failed",
              })}
            </button>
          ) : null}
        </div>
      ) : null}
      <ul>
        {entries.map((entry) => {
          const entryMessage = imageUploadEntryMessage(
            entry,
            rejectionCodes.get(entry.marker),
            t,
          );
          return (
            <li key={entry.marker}>
              <span className="editor-upload-name">{entry.name}</span>
              <span className="editor-upload-status" data-status={entry.status}>
                {t(`editor.imageUploadStatus.${entry.status}`, {
                  defaultValue: entry.status,
                })}
              </span>
              {entryMessage ? (
                <span className="editor-upload-message">{entryMessage}</span>
              ) : null}
              {entry.status === "queued" || entry.status === "uploading" ? (
                <button
                  aria-label={t("editor.imageUploadCancelAria", {
                    defaultValue: "Cancel upload for {{name}}",
                    name: entry.name,
                  })}
                  className="text-button compact-button"
                  type="button"
                  onClick={() => onCancel(entry.marker)}
                >
                  {t("editor.imageUploadCancel", { defaultValue: "Cancel" })}
                </button>
              ) : null}
              {(entry.status === "failed" || entry.status === "cancelled" ||
                entry.status === "outcome-unknown") &&
                entry.insertionSlotAvailable !== false &&
                !policyBlockedMarkers.has(entry.marker) ? (
                <button
                  aria-label={t("editor.imageUploadRetryAria", {
                    defaultValue: "Retry upload for {{name}}",
                    name: entry.name,
                  })}
                  className="text-button compact-button"
                  type="button"
                  onClick={() => onRetry(entry.marker)}
                >
                  {t("editor.imageUploadRetry", { defaultValue: "Retry" })}
                </button>
              ) : null}
              {entry.status === "failed" || entry.status === "cancelled" ||
                entry.status === "outcome-unknown" ? (
                <button
                  aria-label={t("editor.imageUploadRemoveAria", {
                    defaultValue: "Remove upload for {{name}}",
                    name: entry.name,
                  })}
                  className="text-button compact-button"
                  type="button"
                  onClick={() => onRemove(entry.marker)}
                >
                  {t("editor.imageUploadRemove", { defaultValue: "Remove" })}
                </button>
              ) : null}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function imageUploadEntryMessage(
  entry: ContextImageUploadEntry,
  rejectionCode: MediaUploadRejectionCode | undefined,
  t: TFunction<"pages">,
) {
  if (rejectionCode) {
    return imageUploadRejectionMessage(rejectionCode, t);
  }
  if (entry.insertionSlotAvailable === false) {
    return t("editor.imageUploadRetryMissing", {
      defaultValue: "The upload insertion slot was removed. Insert the image again.",
    });
  }
  return entry.status === "failed"
    ? t("editor.imageUploadRequestFailed", {
        defaultValue: "Request failed. Please try again.",
      })
    : null;
}

function imageUploadRejectionMessage(
  rejectionCode: MediaUploadRejectionCode,
  t: TFunction<"pages">,
) {
  switch (rejectionCode) {
    case "batch_bytes_exceeded":
      return t("editor.imageUploadRejection.batchBytesExceeded", {
        defaultValue: "The selected files exceed the current batch upload limit.",
      });
    case "batch_file_count_exceeded":
      return t("editor.imageUploadRejection.batchFileCountExceeded", {
        defaultValue: "Too many files were selected for one upload batch.",
      });
    case "empty_file":
      return t("editor.imageUploadRejection.emptyFile", {
        defaultValue: "Empty files cannot be uploaded.",
      });
    case "file_too_large":
      return t("editor.imageUploadRejection.fileTooLarge", {
        defaultValue: "This file exceeds the current upload limit.",
      });
    case "unsupported_media_type":
      return t("editor.imageUploadRejection.unsupportedMediaType", {
        defaultValue: "This file type is not supported.",
      });
    case "validation_failed":
      return t("editor.imageUploadRejection.validationFailed", {
        defaultValue: "This file did not pass upload validation.",
      });
  }
}

function formatUploadProgressBytes(bytes: number) {
  const safeBytes = Math.max(0, Math.trunc(bytes));
  if (safeBytes < 1024) return `${safeBytes} B`;
  const kibibytes = safeBytes / 1024;
  if (kibibytes < 1024) {
    return `${Number.isInteger(kibibytes) ? kibibytes : kibibytes.toFixed(1)} KiB`;
  }
  const mebibytes = kibibytes / 1024;
  return `${Number.isInteger(mebibytes) ? mebibytes : mebibytes.toFixed(1)} MiB`;
}

function SyncPill({
  state,
  publicationState,
  isReady,
  hasPersistedPage,
  lastSavedAt,
  now: _now,
  onRetry,
  blockedReason: _blockedReason = null,
}: {
  state: SaveState;
  publicationState: PublicationState;
  isReady: boolean;
  hasPersistedPage: boolean;
  lastSavedAt: string | null;
  now: number;
  onRetry: () => void;
  blockedReason?: string | null;
}) {
  const { t } = useTranslation("pages");
  const savedJustNow = isRecentTimestamp(lastSavedAt, _now);
  const statusProps = {
    "aria-atomic": true,
    "aria-live": "polite" as const,
    role: "status",
  };
  if (!isReady) {
    return (
      <span className="sync-pill" {...statusProps}>
        {t("editor.syncLoading")}
      </span>
    );
  }
  if (state === "error") {
    return (
      <span className="sync-pill sync-pill-error" {...statusProps}>
        <button className="sync-pill-action" onClick={onRetry} type="button">
          {t("editor.syncPillError", {
            defaultValue: "Couldn't save or publish · Retry",
          })}
        </button>
      </span>
    );
  }
  if (state === "conflict") {
    return (
      <span className="sync-pill sync-pill-conflict" {...statusProps}>
        {t("editor.syncPillConflict", { defaultValue: "Out of date · Resolve" })}
      </span>
    );
  }
  if (state === "saving") {
    return (
      <span className="sync-pill sync-pill-saving" {...statusProps}>
        {t("editor.syncSavingDraft", { defaultValue: "Saving…" })}
      </span>
    );
  }
  if (state === "saving_with_newer_edits") {
    return (
      <span className="sync-pill sync-pill-saving" {...statusProps}>
        {t("editor.syncSavingWithNewerEdits", {
          defaultValue: "Saving · newer changes pending",
        })}
      </span>
    );
  }
  if (state === "dirty") {
    return (
      <span className="sync-pill" {...statusProps}>
        {t("editor.syncPillEditing", { defaultValue: "Editing" })}
      </span>
    );
  }
  if (state === "clean" && hasPersistedPage) {
    return (
      <span className="sync-pill sync-pill-saved" {...statusProps}>
        <CheckCircle2 size={14} aria-hidden="true" />
        <span>
          {publicationState === "staged"
            ? t("editor.syncPillDraftSaved", {
                defaultValue: "Unpublished draft saved",
              })
            : publicationState === "never-published"
              ? t("editor.syncPillDraftNeverPublished", {
                  defaultValue: "Draft saved · not published yet",
                })
              : savedJustNow
                ? t("editor.syncPillSavedJustNow", { defaultValue: "Saved just now" })
                : t("editor.syncPillSaved", { defaultValue: "Saved" })}
        </span>
      </span>
    );
  }
  return (
    <span className="sync-pill" {...statusProps}>
      {t("editor.syncPillNeverPublished", { defaultValue: "Not published yet" })}
    </span>
  );
}

function EditorPublicationActions({
  canPublish,
  canSave,
  saving,
  saveDisabledReason,
  onPublish,
  onSave,
}: {
  canPublish: boolean;
  canSave: boolean;
  saving: boolean;
  saveDisabledReason?: string | null;
  onPublish: () => void;
  onSave: () => void;
}) {
  const { t } = useTranslation("pages");
  return (
    <div
      aria-label={t("editor.publicationActions", {
        defaultValue: "Draft and publication actions",
      })}
      className="editor-publication-actions"
      role="group"
    >
      <button
        aria-disabled={saveDisabledReason ? true : undefined}
        className={`icon-button text-button ${canSave ? "primary" : ""}`}
        disabled={(!canSave && !saveDisabledReason) || saving}
        onClick={onSave}
        title={saveDisabledReason ?? undefined}
        type="button"
      >
        {saving
          ? t("editor.syncSavingDraft", { defaultValue: "Saving…" })
          : t("editor.saveDraft", { defaultValue: "Save draft" })}
      </button>
      <button
        className={`icon-button text-button ${canPublish ? "primary" : ""}`}
        disabled={!canPublish || saving}
        onClick={onPublish}
        type="button"
      >
        {t("editor.publish", { defaultValue: "Publish" })}
      </button>
    </div>
  );
}

function clearAutoSaveTimer(timerRef: { current: number | null }) {
  if (timerRef.current === null) {
    return;
  }
  window.clearTimeout(timerRef.current);
  timerRef.current = null;
}

// A dirty period keeps its first deadline. Callers may ask repeatedly as the
// revision changes, but an armed checkpoint is never replaced.
function scheduleAutosave(
  timerRef: { current: number | null },
  delayMs: number,
  callback: () => void,
) {
  if (timerRef.current !== null) {
    return;
  }
  timerRef.current = window.setTimeout(() => {
    timerRef.current = null;
    callback();
  }, delayMs);
}

function isCurrentSaveRequest(requestId: number, requestRef: { current: number }) {
  return requestRef.current === requestId;
}

function waitForPromiseOrAbort<T>(promise: Promise<T>, signal: AbortSignal) {
  if (signal.aborted) {
    return Promise.reject(new DOMException("The operation was aborted.", "AbortError"));
  }
  return new Promise<T>((resolve, reject) => {
    const abort = () => {
      signal.removeEventListener("abort", abort);
      reject(new DOMException("The operation was aborted.", "AbortError"));
    };
    signal.addEventListener("abort", abort, { once: true });
    promise.then(
      (value) => {
        signal.removeEventListener("abort", abort);
        resolve(value);
      },
      (error: unknown) => {
        signal.removeEventListener("abort", abort);
        reject(error);
      },
    );
  });
}

function throwIfAborted(signal: AbortSignal) {
  if (signal.aborted) {
    throw new DOMException("The operation was aborted.", "AbortError");
  }
}

function isEditableShortcutTarget(target: EventTarget | null) {
  if (!(target instanceof Element)) {
    return false;
  }
  return Boolean(target.closest("input, textarea, select, [contenteditable='true'], .cm-content"));
}

function hasTextSelection() {
  return Boolean(window.getSelection()?.toString());
}

function blockMenuReferenceElement(
  view: EditorViewType,
  range: { from: number; to: number } | null,
): VirtualElement | null {
  const position = range?.from ?? view.state.selection.main.from;
  const initialBox = view.coordsAtPos(position);
  if (!initialBox) {
    return null;
  }
  return {
    contextElement: view.dom,
    getBoundingClientRect() {
      const box = view.coordsAtPos(position) ?? initialBox;
      return {
        bottom: box.bottom,
        height: Math.max(0, box.bottom - box.top),
        left: box.left,
        right: box.right,
        top: box.top,
        width: Math.max(0, box.right - box.left),
        x: box.left,
        y: box.top,
      };
    },
  };
}

function clamp(value: number, min: number, max: number) {
  return Math.min(Math.max(value, min), max);
}

function clampFloatingElementPosition(
  left: number,
  top: number,
  element: HTMLElement,
): FloatingPosition {
  const viewport = visualViewportBounds();
  const width = element.offsetWidth || element.getBoundingClientRect().width;
  const height = element.offsetHeight || element.getBoundingClientRect().height;
  const minLeft = viewport.left + INLINE_TOOLBAR_MARGIN;
  const minTop = viewport.top + INLINE_TOOLBAR_MARGIN;
  const maxLeft = Math.max(
    minLeft,
    viewport.right - width - INLINE_TOOLBAR_MARGIN,
  );
  const maxTop = Math.max(
    minTop,
    viewport.bottom - height - INLINE_TOOLBAR_MARGIN,
  );
  return {
    left: clamp(left, minLeft, maxLeft),
    top: clamp(top, minTop, maxTop),
  };
}

function floatingPositionForRange(
  view: EditorViewType,
  range: { from: number; to: number },
): FloatingPosition {
  const rect = view.coordsAtPos(range.from);
  const viewport = visualViewportBounds();
  const minLeft = viewport.left + INLINE_TOOLBAR_MARGIN;
  const minTop = viewport.top + INLINE_TOOLBAR_MARGIN;
  const maxLeft = Math.max(
    minLeft,
    viewport.right - LINK_POPOVER_ESTIMATED_WIDTH - INLINE_TOOLBAR_MARGIN,
  );
  const maxTop = Math.max(
    minTop,
    viewport.bottom - COMMENT_POPOVER_ESTIMATED_HEIGHT - INLINE_TOOLBAR_MARGIN,
  );
  return {
    left: clamp(rect?.left ?? minLeft, minLeft, maxLeft),
    top: clamp(
      (rect?.bottom ?? minTop) + INLINE_TOOLBAR_MARGIN,
      minTop,
      maxTop,
    ),
  };
}

function visualViewportBounds() {
  const visualViewport = window.visualViewport;
  const left = visualViewport?.offsetLeft ?? 0;
  const top = visualViewport?.offsetTop ?? 0;
  const width = visualViewport?.width
    || window.innerWidth
    || document.documentElement.clientWidth;
  const height = visualViewport?.height
    || window.innerHeight
    || document.documentElement.clientHeight;
  return {
    bottom: top + height,
    left,
    right: left + width,
    top,
  };
}

function commandStatesForSelection(view: EditorViewType): ActiveCommandState {
  const selection = view.state.selection.main;
  const body = view.state.doc.toString();
  return Object.fromEntries(
    (EDITOR_COMMANDS as readonly EditorCommand[])
      .filter(isTextMutationCommand)
      .map((command) => [
        command.id,
        getMarkdownCommandState({
          body,
          commandId: command.mutation,
          selection: { anchor: selection.anchor, head: selection.head },
        }),
      ]),
  ) as ActiveCommandState;
}

function emptyDraft(mode: "create" | "edit", slug: string): DraftSnapshot {
  return {
    aliases: [],
    body: "",
    destinationNodeId: "",
    icon: DEFAULT_PAGE_METADATA.icon,
    pageType: DEFAULT_PAGE_METADATA.type,
    slug: mode === "create" ? "" : slug,
    tags: [],
    title: "",
  };
}

function serializeDraftBody(draft: DraftSnapshot, extraLines: string[]) {
  return serializePageDocument({
    body: draft.body,
    metadata: {
      aliases: draft.aliases,
      extraLines,
      icon: draft.icon,
      tags: draft.tags,
      type: draft.pageType,
    },
    slug: draft.slug,
    title: draft.title,
  });
}

function draftSaveFingerprint(draft: DraftSnapshot, extraLines: string[]) {
  return JSON.stringify({
    aliases: draft.aliases,
    body_md: serializeDraftBody(draft, extraLines),
    destination_node_id: draft.destinationNodeId || null,
    page_type: draft.pageType,
    tags: draft.tags,
    title: draft.title.trim(),
  });
}

function isRecentTimestamp(timestamp: string | null, now: number) {
  if (!timestamp) {
    return false;
  }
  const parsed = Date.parse(timestamp);
  return Number.isFinite(parsed) && now - parsed >= 0 && now - parsed < SAVED_JUST_NOW_MS;
}

function readPagePublicationState(page: PagePayload): {
  sessionVersionId: string | null;
  state: PublicationState;
} {
  // The payload states this outright, so the editor asks the page rather than
  // paging through version history: a page with enough newer drafts pushes its
  // published version off the end of that list and looks never-published.
  // P8/P9: an unsubmitted, still-private version is "editing", not "draft" --
  // both are equally unpublished until a `current_published_version_id`
  // exists, so both take this branch (see CONTEXT_PAGE_VERSION_STATUSES).
  if (page.version_status === "draft" || page.version_status === "editing") {
    return {
      sessionVersionId: page.version_id,
      state: page.current_published_version_id ? "staged" : "never-published",
    };
  }
  if (page.version_status) {
    return { sessionVersionId: null, state: "published" };
  }
  if (page.publish_blocked_reasons?.length) {
    return { sessionVersionId: page.version_id, state: "staged" };
  }
  return { sessionVersionId: null, state: "published" };
}

async function loadSourceSuggestions(workspaceId: string) {
  try {
    const sources = await listLibraryItems(workspaceId, { limit: 100 });
    return {
      sources: Array.isArray(sources.data) ? sources.data : [],
      warning: null,
    };
  } catch {
    return {
      sources: [] as LibraryItemSummary[],
      warning: "Citation suggestions could not be loaded. You can still type @cite{source-id} manually.",
    };
  }
}

export function editorMeta(
  page: PagePayload | null,
  dirty: boolean,
  t: TFunction<"pages">,
  savedJustNow: boolean,
) {
  if (!page) {
    return dirty
      ? t("editor.metaDraftNotSaved", { defaultValue: "Draft not saved yet" })
      : t("editor.metaNewDraft", { defaultValue: "New draft" });
  }
  // L-2 (260908 UI audit): the sync pill already says "Saved just now" right
  // after a save -- repeating the same fact here as a timestamp said nothing
  // new. Once the pill drops back to a plain "Saved", the exact time becomes
  // the only place that information lives, so it reappears here.
  if (!dirty && savedJustNow) {
    return "";
  }
  const time = formatDateTime(page.draft_saved_at ?? page.updated_at);
  // M-11 (260908 UI audit): this used to append `· v ${version_id.slice(0, 8)}`,
  // a raw UUID fragment with no meaning to a reader -- the timestamp alone is
  // the actionable part of "last saved".
  return t("editor.metaLastSaved", {
    defaultValue: `Last saved ${time}`,
    time,
  });
}

function formatDateTime(value: string) {
  return formatVersionTimestamp(value);
}

function getSavedRelativeTimeRefreshDelay(value: string | null, now = Date.now()) {
  if (!value) {
    return null;
  }
  const savedAt = new Date(value).getTime();
  if (!Number.isFinite(savedAt)) {
    return null;
  }
  const elapsedSeconds = Math.max(0, Math.floor((now - savedAt) / 1000));
  if (elapsedSeconds < 60) {
    return 1000;
  }
  if (elapsedSeconds < 60 * 60) {
    return (60 - (elapsedSeconds % 60)) * 1000;
  }
  if (elapsedSeconds < 24 * 60 * 60) {
    return (60 * 60 - (elapsedSeconds % (60 * 60))) * 1000;
  }
  if (elapsedSeconds < 7 * 24 * 60 * 60) {
    return (24 * 60 * 60 - (elapsedSeconds % (24 * 60 * 60))) * 1000;
  }
  return null;
}

function escapeLinkText(value: string) {
  return value.replace(/\\/g, "\\\\").replace(/\]/g, "\\]");
}

// Mirror editorPaste's safeMarkdownDestination for the link dialog: reject
// control characters and non-http(s)/mailto schemes (relative paths pass), and
// percent-encode the characters that end a CommonMark destination early.
const LINK_ALLOWED_SCHEMES = new Set(["http", "https", "mailto"]);

function normalizeLinkDestination(value: string) {
  const trimmed = value.trim().replace(/[\r\n]/g, "");
  if (!trimmed || /[\u0000-\u001F\u007F]/.test(trimmed)) {
    return "";
  }
  const scheme = /^([A-Za-z][A-Za-z0-9+.-]*):/.exec(trimmed)?.[1].toLowerCase();
  if (scheme && !LINK_ALLOWED_SCHEMES.has(scheme)) {
    return "";
  }
  return trimmed
    .replace(/ /g, "%20")
    .replace(/\(/g, "%28")
    .replace(/\)/g, "%29")
    .replace(/</g, "%3C")
    .replace(/>/g, "%3E");
}

function readEditorPreference(key: string): { mode: EditorMode; splitPercent: number } {
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) {
      return { mode: "source", splitPercent: 50 };
    }
    const parsed = JSON.parse(raw) as Partial<{
      mode: EditorMode;
      split: boolean;
      splitPercent: number;
    }>;
    if (parsed.mode === "split" || (parsed.mode === "source" && parsed.split)) {
      return { mode: "split", splitPercent: clampSplitPercent(parsed.splitPercent ?? 50) };
    }
    return {
      mode: parsed.mode === "reading" ? "reading" : "source",
      splitPercent: clampSplitPercent(parsed.splitPercent ?? 50),
    };
  } catch {
    return { mode: "source", splitPercent: 50 };
  }
}

function writeEditorPreference(
  key: string,
  preference: { mode: EditorMode; splitPercent: number },
) {
  try {
    window.localStorage.setItem(key, JSON.stringify(preference));
  } catch {
    // Ignore storage failures; editor mode remains available for the current session.
  }
}

function clampSplitPercent(value: number) {
  return Math.min(60, Math.max(40, Number.isFinite(value) ? value : 50));
}

function nextEditorMode(
  current: EditorMode,
  singlePaneViewport = false,
): EditorMode {
  const modes = singlePaneViewport ? EDITOR_MODES.slice(0, 2) : EDITOR_MODES;
  const currentIndex = Math.max(0, modes.indexOf(current));
  return modes[(currentIndex + 1) % modes.length];
}

function isSinglePaneEditorViewport() {
  if (typeof window === "undefined") {
    return false;
  }
  return (
    (window.innerWidth || document.documentElement.clientWidth) <=
    SINGLE_PANE_EDITOR_MAX_WIDTH
  );
}

function isStackedSplitEditorViewport() {
  if (typeof window === "undefined") {
    return false;
  }
  const width = window.innerWidth || document.documentElement.clientWidth;
  return width > SINGLE_PANE_EDITOR_MAX_WIDTH && width <= 1023;
}

function isCompactActionBarViewport() {
  if (typeof window === "undefined") {
    return false;
  }
  return (
    (window.innerWidth || document.documentElement.clientWidth) <=
    COMPACT_ACTION_BAR_MAX_WIDTH
  );
}

function isContextRailDrawerViewport() {
  if (typeof window === "undefined") {
    return false;
  }
  return (
    (window.innerWidth || document.documentElement.clientWidth) <=
    CONTEXT_RAIL_DRAWER_MAX_WIDTH
  );
}

function sameBreadcrumbLabel(left: string | undefined, right: string) {
  return left?.trim().toLowerCase() === right.trim().toLowerCase();
}

function normalizeInitialTitle(value: string | null) {
  return value?.trim().slice(0, 80) ?? "";
}

// Every title input path (typing, paste, drop, programmatic) funnels through
// updateTitle, so collapsing newline/control runs here covers multi-line
// paste that bypasses the Enter-key interceptor.
function normalizeTitleInput(value: string) {
  return value.replace(TITLE_CONTROL_CHARS_PATTERN, " ").slice(0, TITLE_MAX_LENGTH);
}

// Walk parent_id links from the destination node up to the root so the editor
// expands only that path (and collapses everything else).
function ancestorPathNodeIds(
  nodes: TreeNodePayload[],
  parentNodeId: string | null,
): string[] {
  if (!parentNodeId) {
    return [];
  }
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const path: string[] = [];
  const seen = new Set<string>();
  let current = byId.get(parentNodeId);
  while (current && !seen.has(current.id)) {
    seen.add(current.id);
    path.push(current.id);
    current = current.parent_id ? byId.get(current.parent_id) : undefined;
  }
  return path;
}

function chooseCreateDestination(
  nodes: TreeNodePayload[],
  requestedNodeId: string | null,
) {
  if (
    requestedNodeId &&
    nodes.some((node) => node.id === requestedNodeId && node.can_have_children)
  ) {
    return requestedNodeId;
  }
  // A page a person deliberately authored belongs at the Context root. Inbox is
  // the staging area for writes that arrive WITHOUT a directory — MCP tool
  // writes and Ask captures, which route through the backend services and never
  // reach this editor. Sending hand-authored pages there too made the New item
  // dialog's "Context" row mean Inbox for pages and the section root for
  // folders. Edit-mode fallbacks below still use chooseDefaultDestination.
  return (
    nodes.find((node) => node.slug === "context" && node.can_have_children)?.id ??
    chooseDefaultDestination(nodes)
  );
}

function chooseDefaultDestination(nodes: TreeNodePayload[]) {
  return (
    nodes.find((node) => node.slug === "inbox" && node.can_have_children)?.id ??
    nodes.find((node) => node.slug === "context" && node.can_have_children)?.id ??
    nodes.find((node) => node.can_have_children)?.id ??
    ""
  );
}

function isTreeDescendant(
  candidate: TreeNodePayload,
  ancestorNodeId: string,
  nodes: TreeNodePayload[],
) {
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const seen = new Set<string>();
  let parentId = candidate.parent_id;
  while (parentId && !seen.has(parentId)) {
    if (parentId === ancestorNodeId) {
      return true;
    }
    seen.add(parentId);
    parentId = nodeById.get(parentId)?.parent_id ?? null;
  }
  return false;
}

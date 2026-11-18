import { markdown, markdownLanguage } from "@codemirror/lang-markdown";
import { RangeSetBuilder, StateField, type Extension } from "@codemirror/state";
import { Decoration, type DecorationSet, EditorView } from "@codemirror/view";
import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../../shared/api/client";
import { ContextMergeChoiceRequired, ContextMergePreviewChanged } from "../../shared/api/pages";
import { listTree, readTreeNode, type TreeNodePayload } from "../../shared/api/workspace";
import { Dialog } from "../../shared/components/Dialog";
import { FormField } from "../../shared/components/FormField";
import { SegmentedControl } from "../../shared/components/SegmentedControl";
import { TreePickerDropdown } from "../../shared/components/TreePickerDropdown";
import { formatRelativeTimestamp } from "../../shared/lib/datetime";
import {
  conflictedMetadataFields,
  mergedLineRanges,
  mergeSourceText,
  metadataValue,
  type ContextMergeChoice,
  type ContextMergeMetadataEdit,
  type ContextMergeMetadataField,
  type ContextMergePreview,
  type ContextMergeResolution,
  type ContextMergeSide,
} from "./contextMerge";
import { MarkdownSourceEditor } from "./MarkdownEditorKernel";
import { MarkdownView } from "./MarkdownView";
import { PAGE_TYPE_OPTIONS } from "./pageFrontmatter";

/** Where the merge happens; it only changes which side each label names. */
export type ContextMergeDialogKind = "publish" | "bring_in" | "save";

export type ContextMergeChoiceRequest = {
  kind: ContextMergeDialogKind;
  workspaceId: string | null;
  pageId: string;
  preview: ContextMergePreview;
  publishedVersionNumber?: number | null;
  /** Writes the person's answer; throws ContextMergeChoiceRequired/PreviewChanged to show a newer preview. */
  confirm: (resolution: ContextMergeResolution, preview: ContextMergePreview) => Promise<void>;
  /** A fresh preview after the server refused a confirm because the content moved. */
  reload?: () => Promise<ContextMergePreview>;
  /** The conflict went away meanwhile; the caller retries its ordinary path. */
  onResolvedElsewhere?: () => void;
  /** The person closed the dialog without combining. */
  onCancel?: () => void;
  errorMessage: (error: unknown) => string;
};

const STALE_CONFIRM_CODES = new Set(["stale_base_version", "context_merge_conflict", "editing_revision_conflict"]);

/**
 * One flow for every caller of the merge dialog: show it, write the answer,
 * and on a stale confirm show the newer preview with a notice instead of
 * overwriting what changed.
 */
export function useContextMergeChoice() {
  const [state, setState] = useState<(ContextMergeChoiceRequest & { refreshed: boolean; busy: boolean; error: string | null }) | null>(null);
  // The open request, read by close() so its cancel runs once, outside a state updater.
  const requestRef = useRef<ContextMergeChoiceRequest | null>(null);
  const open = useCallback((request: ContextMergeChoiceRequest) => {
    requestRef.current = request;
    setState({ ...request, refreshed: false, busy: false, error: null });
  }, []);
  const close = useCallback(() => {
    const request = requestRef.current;
    requestRef.current = null;
    setState(null);
    request?.onCancel?.();
  }, []);

  async function confirm(resolution: ContextMergeResolution) {
    const current = state;
    if (!current) return;
    setState({ ...current, busy: true, error: null });
    try {
      await current.confirm(resolution, current.preview);
      requestRef.current = null;
      setState(null);
    } catch (error) {
      if (error instanceof ContextMergeChoiceRequired || error instanceof ContextMergePreviewChanged) {
        setState({ ...current, preview: error.preview, refreshed: true, busy: false, error: null });
        return;
      }
      if (error instanceof ApiError && error.status === 409 && STALE_CONFIRM_CODES.has(error.body.error_code) && current.reload) {
        try {
          const fresh = await current.reload();
          if (fresh.kind === "conflict") {
            setState({ ...current, preview: fresh, refreshed: true, busy: false, error: null });
          } else {
            requestRef.current = null;
            setState(null);
            current.onResolvedElsewhere?.();
          }
          return;
        } catch (reloadError) {
          setState({ ...current, busy: false, error: current.errorMessage(reloadError) });
          return;
        }
      }
      setState({ ...current, busy: false, error: current.errorMessage(error) });
    }
  }

  const dialog = state ? (
    <ContextMergeDialog
      busy={state.busy}
      error={state.error}
      kind={state.kind}
      onClose={close}
      onConfirm={(resolution) => void confirm(resolution)}
      pageId={state.pageId}
      preview={state.preview}
      publishedVersionNumber={state.publishedVersionNumber}
      refreshed={state.refreshed}
      workspaceId={state.workspaceId}
    />
  ) : null;
  return { open, close, dialog, isOpen: state !== null };
}

type MetadataSource = "ours" | "theirs" | "custom";
type MetadataChoice = { source: MetadataSource; value: string | null };

type ContextMergeDialogProps = {
  kind: ContextMergeDialogKind;
  workspaceId: string | null;
  pageId: string;
  preview: ContextMergePreview;
  publishedVersionNumber?: number | null;
  busy?: boolean;
  error?: string | null;
  /** Shown after a confirm found newer content and a new preview replaced the old one. */
  refreshed?: boolean;
  onClose: () => void;
  onConfirm: (resolution: ContextMergeResolution) => void;
};

/**
 * One screen for every merge that needs a person: publication or approval,
 * bringing the published version into a draft, and two saves of one draft.
 * The two choices show the merge module's result as it is; editing is optional.
 */
export function ContextMergeDialog({
  kind,
  workspaceId,
  pageId,
  preview,
  publishedVersionNumber,
  busy = false,
  error,
  refreshed = false,
  onClose,
  onConfirm,
}: ContextMergeDialogProps) {
  const { t, i18n } = useTranslation("context");
  const titleId = useId();
  const [choice, setChoice] = useState<ContextMergeChoice | null>(null);
  const [view, setView] = useState<"result" | "edit">("result");
  const [editedBody, setEditedBody] = useState<string | null>(null);
  const [metadataChoices, setMetadataChoices] = useState<Partial<Record<ContextMergeMetadataField, MetadataChoice>>>({});
  const previewKey = `${preview.version_id}:${preview.editing_revision}:${preview.target_version_id ?? ""}:${preview.keep_both?.result_hash ?? ""}:${preview.latest?.result_hash ?? ""}`;
  useEffect(() => {
    // A new preview means new content: earlier edits no longer apply to it.
    setEditedBody(null);
    setMetadataChoices({});
    setView("result");
  }, [previewKey]);

  const candidate = choice ? preview[choice] ?? null : null;
  const sourceText = candidate ? mergeSourceText(candidate) : "";
  const ranges = useMemo(() => (candidate && choice ? mergedLineRanges(candidate, choice) : []), [candidate, choice]);
  const fields = conflictedMetadataFields(preview);
  const sides = preview.sides ?? null;
  const latestSide = sides ? sides[preview.latest_side] : null;
  // A virtual-base conflict can need a choice without a text region of its own.
  const conflictCount = Math.max(1, (preview.keep_both?.conflicts.length ?? 0) + fields.length);
  const sideName = (side: ContextMergeSide | null) => side?.saved_by?.display_name || t("merge.someone");
  const sideTime = (side: ContextMergeSide | null) => formatRelativeTimestamp(side?.last_saved_at ?? null, i18n.language);
  const sideLabel = (key: "ours" | "theirs") => {
    if (kind === "save") return key === "ours" ? t("merge.sides.mine") : t("merge.sides.person", { name: sideName(sides?.theirs ?? null) });
    return key === "ours" ? t("merge.sides.draft") : t("merge.sides.published");
  };

  function metadataChoice(field: ContextMergeMetadataField): MetadataChoice {
    const chosen = metadataChoices[field];
    if (chosen) return chosen;
    return { source: preview.latest_side, value: sides ? metadataValue(sides[preview.latest_side].metadata, field) : null };
  }

  function resolution(): ContextMergeResolution | null {
    if (!choice || !candidate) return null;
    const edit: ContextMergeMetadataEdit = {};
    for (const field of fields) {
      const chosen = metadataChoices[field];
      if (!chosen) continue;
      const current = metadataValue(candidate.page_metadata, field);
      if (chosen.value === current) continue;
      if (field === "placement_parent_node_id") edit.placement_parent_node_id = chosen.value;
      else if (field === "page_type" && chosen.value) edit.page_type = chosen.value as ContextMergeMetadataEdit["page_type"];
      else if (field === "title" && chosen.value) edit.title = chosen.value;
    }
    return {
      choice,
      candidate,
      bodyMd: editedBody !== null && editedBody !== sourceText ? editedBody : null,
      pageMetadata: Object.keys(edit).length > 0 ? edit : null,
    };
  }

  const titleInvalid = metadataChoices.title?.source === "custom" && !metadataChoices.title.value?.trim();
  const ready = Boolean(choice && candidate) && !titleInvalid;
  const summary = kind === "save"
    ? t("merge.summarySave", { count: conflictCount, name: sideName(sides?.theirs ?? null), time: sideTime(sides?.theirs ?? null) })
    : t("merge.summary", {
      count: conflictCount,
      version: publishedVersionNumber ? `(v${publishedVersionNumber})` : "",
      name: sideName(sides?.ours ?? null),
      time: sideTime(sides?.ours ?? null),
    });

  return (
    <Dialog
      className="context-merge-dialog"
      dismissible={!busy}
      labelledBy={titleId}
      onClose={onClose}
      title={<span id={titleId}>{t("merge.title")}</span>}
      footer={(
        <div className="context-merge-footer">
          {error ? <p className="form-error" role="alert">{error}</p> : null}
          {!choice ? <p className="context-merge-hint">{t("merge.chooseFirst")}</p> : null}
          <div className="dialog-actions">
            <button className="text-button" disabled={busy} onClick={onClose} type="button">{t("shell:dialog.close")}</button>
            <button
              className="icon-button success"
              disabled={!ready || busy}
              onClick={() => { const value = resolution(); if (value) onConfirm(value); }}
              type="button"
            >
              {busy ? t("merge.confirming") : t("merge.confirm")}
            </button>
          </div>
        </div>
      )}
    >
      <div className="context-merge-body">
        {refreshed ? <p className="context-merge-refreshed" role="status">{t("merge.refreshed")}</p> : null}
        <p className="context-merge-summary">{summary}</p>
        <SegmentedControl
          className="context-merge-choices"
          label={t("merge.choiceLabel")}
          onChange={(value) => setChoice(value as ContextMergeChoice)}
          options={[
            {
              key: "keep_both",
              label: (
                <span className="context-merge-choice">
                  <strong>{t("merge.keepBoth")}</strong>
                  <span>{t("merge.keepBothHint")}</span>
                </span>
              ),
            },
            {
              key: "latest",
              label: (
                <span className="context-merge-choice">
                  <strong>{t("merge.latest")}</strong>
                  <span>{t("merge.latestHint")}</span>
                  {latestSide ? <span className="context-merge-choice-side">{t("merge.latestSide", { name: sideName(latestSide), time: sideTime(latestSide) })}</span> : null}
                </span>
              ),
            },
          ]}
          value={choice ?? ""}
        />
        {candidate ? (
          <section className="context-merge-result" aria-label={t("merge.resultLabel")}>
            <div className="context-merge-result-header">
              <h3>{t("merge.resultLabel")}</h3>
              <SegmentedControl
                compact
                label={t("merge.viewLabel")}
                onChange={(value) => setView(value as "result" | "edit")}
                options={[{ key: "result", label: t("merge.viewResult") }, { key: "edit", label: t("merge.viewEdit") }]}
                value={view}
              />
            </div>
            {ranges.length > 0 ? <p className="context-merge-legend">{t("merge.legend", { count: ranges.length })}</p> : null}
            {view === "result" ? (
              <MergeResultView body={editedBody ?? sourceText} ranges={editedBody === null ? ranges : []} workspaceId={workspaceId} />
            ) : (
              <MergeResultEditor
                ariaLabel={t("merge.editLabel")}
                onChange={setEditedBody}
                ranges={ranges}
                value={editedBody ?? sourceText}
              />
            )}
          </section>
        ) : null}
        {candidate && fields.length > 0 && sides ? (
          <section className="context-merge-metadata" aria-label={t("merge.metadataLabel")}>
            <h3>{t("merge.metadataLabel")}</h3>
            {fields.map((field) => (
              <MetadataConflictRow
                choice={metadataChoice(field)}
                field={field}
                key={field}
                labels={{ ours: sideLabel("ours"), theirs: sideLabel("theirs") }}
                onChange={(next) => setMetadataChoices((current) => ({ ...current, [field]: next }))}
                pageId={pageId}
                sides={sides}
                workspaceId={workspaceId}
              />
            ))}
          </section>
        ) : null}
      </div>
    </Dialog>
  );
}

/** Marks the rendered blocks whose source lines overlap a merged region. */
function MergeResultView({ body, ranges, workspaceId }: { body: string; ranges: Array<[number, number]>; workspaceId: string | null }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  useLayoutEffect(() => {
    const blocks = containerRef.current?.querySelectorAll<HTMLElement>("[data-scroll-sync-anchor]") ?? [];
    for (const block of blocks) {
      const start = Number(block.dataset.sourceLine);
      const end = Number(block.dataset.sourceEndLine ?? block.dataset.sourceLine);
      const merged = ranges.some(([from, to]) => start <= to && end >= from);
      if (merged) block.setAttribute("data-merged-region", "true");
      else block.removeAttribute("data-merged-region");
    }
  });
  return (
    <div className="context-merge-preview" ref={containerRef}>
      <MarkdownView body={body} sourceLineNumbers workspaceId={workspaceId} />
    </div>
  );
}

function mergedLinesField(ranges: Array<[number, number]>): Extension {
  const mark = Decoration.line({ class: "context-merge-line" });
  return StateField.define<DecorationSet>({
    create(state) {
      const builder = new RangeSetBuilder<Decoration>();
      for (let line = 1; line <= state.doc.lines; line += 1) {
        if (ranges.some(([from, to]) => line >= from && line <= to)) {
          const position = state.doc.line(line).from;
          builder.add(position, position, mark);
        }
      }
      return builder.finish();
    },
    update(decorations, transaction) {
      return decorations.map(transaction.changes);
    },
    provide: (field) => EditorView.decorations.from(field),
  });
}

function MergeResultEditor({ ariaLabel, onChange, ranges, value }: {
  ariaLabel: string;
  onChange: (value: string) => void;
  ranges: Array<[number, number]>;
  value: string;
}) {
  // Decorations start from the merge text and then follow the person's edits.
  const [initialRanges] = useState(ranges);
  const extensions = useMemo(() => [
    EditorView.lineWrapping,
    EditorView.contentAttributes.of({ "aria-label": ariaLabel }),
    markdown({ base: markdownLanguage }),
    mergedLinesField(initialRanges),
  ], [ariaLabel, initialRanges]);
  return (
    <div className="context-merge-editor">
      <MarkdownSourceEditor extensions={extensions} onChange={onChange} placeholder="" value={value} />
    </div>
  );
}

function MetadataConflictRow({ choice, field, labels, onChange, pageId, sides, workspaceId }: {
  choice: MetadataChoice;
  field: ContextMergeMetadataField;
  labels: { ours: string; theirs: string };
  onChange: (choice: MetadataChoice) => void;
  pageId: string;
  sides: { ours: ContextMergeSide; theirs: ContextMergeSide };
  workspaceId: string | null;
}) {
  const { t } = useTranslation("context");
  const { t: tPages } = useTranslation("pages");
  const inputId = useId();
  const display = (value: string | null) => {
    if (field === "page_type") return value ? tPages(`editor.pageTypes.${value}.label`, { defaultValue: value }) : "—";
    if (field === "placement_parent_node_id") return <PlacementLabel nodeId={value} workspaceId={workspaceId} />;
    return value ?? "—";
  };
  const sideOption = (key: "theirs" | "ours") => {
    const value = metadataValue(sides[key].metadata, field);
    return {
      key,
      label: (
        <span className="context-merge-metadata-option">
          <span>{labels[key]}</span>
          <strong>{display(value)}</strong>
        </span>
      ),
    };
  };
  return (
    <div className="context-merge-metadata-row">
      <span className="context-merge-metadata-field">{t(`merge.fields.${field}`)}</span>
      <SegmentedControl
        className="context-merge-metadata-choices"
        label={t(`merge.fields.${field}`)}
        onChange={(value) => {
          const source = value as MetadataSource;
          if (source === "custom") onChange({ source, value: choice.value });
          else onChange({ source, value: metadataValue(sides[source].metadata, field) });
        }}
        options={[sideOption("theirs"), sideOption("ours"), { key: "custom", label: t("merge.customValue") }]}
        value={choice.source}
      />
      {choice.source === "custom" ? (
        <div className="context-merge-metadata-custom">
          {field === "title" ? (
            <FormField htmlFor={inputId} label={t("merge.fields.title")}>
              <input
                id={inputId}
                onChange={(event) => onChange({ source: "custom", value: event.target.value })}
                type="text"
                value={choice.value ?? ""}
              />
            </FormField>
          ) : null}
          {field === "page_type" ? (
            <FormField htmlFor={inputId} label={t("merge.fields.page_type")}>
              <select id={inputId} onChange={(event) => onChange({ source: "custom", value: event.target.value })} value={choice.value ?? ""}>
                {PAGE_TYPE_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {tPages(`editor.pageTypes.${option.value}.label`, { defaultValue: option.label })}
                  </option>
                ))}
              </select>
            </FormField>
          ) : null}
          {field === "placement_parent_node_id" ? (
            <PlacementPicker
              onChange={(value) => onChange({ source: "custom", value })}
              pageId={pageId}
              selectedId={choice.value}
              workspaceId={workspaceId}
            />
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function PlacementLabel({ nodeId, workspaceId }: { nodeId: string | null; workspaceId: string | null }) {
  const { t } = useTranslation("pages");
  const [label, setLabel] = useState<{ id: string; text: string } | null>(null);
  useEffect(() => {
    let active = true;
    if (workspaceId && nodeId) {
      void readTreeNode(workspaceId, nodeId).then((node) => {
        if (active) setLabel({ id: nodeId, text: [...node.breadcrumb.filter((entry) => entry.id !== node.id).map((entry) => entry.title), node.title].join(" / ") });
      }).catch(() => undefined);
    }
    return () => { active = false; };
  }, [nodeId, workspaceId]);
  if (!nodeId) return <>{t("editor.destinationEmptyLabel")}</>;
  return <>{label?.id === nodeId ? label.text : t("editor.mergeDestinationUnavailable")}</>;
}

function PlacementPicker({ onChange, pageId, selectedId, workspaceId }: {
  onChange: (value: string | null) => void;
  pageId: string;
  selectedId: string | null;
  workspaceId: string | null;
}) {
  const { t } = useTranslation("pages");
  const [nodes, setNodes] = useState<TreeNodePayload[] | null>(null);
  useEffect(() => {
    let active = true;
    if (workspaceId) {
      void listTree(workspaceId, "context").then((tree) => {
        if (!active) return;
        const own = tree.find((node) => node.target_id === pageId)?.id ?? null;
        const inside = (node: TreeNodePayload) => {
          const byId = new Map(tree.map((item) => [item.id, item]));
          for (let current: TreeNodePayload | undefined = node; current; current = current.parent_id ? byId.get(current.parent_id) : undefined) {
            if (current.id === own) return true;
          }
          return false;
        };
        setNodes(tree.filter((node) => node.can_have_children && !inside(node)));
      }).catch(() => { if (active) setNodes([]); });
    }
    return () => { active = false; };
  }, [pageId, workspaceId]);
  return (
    <FormField label={t("editor.destinationLabel")}>
      <TreePickerDropdown
        ariaLabel={t("editor.destinationLabel")}
        disabled={!nodes || nodes.length === 0}
        emptyLabel={t("editor.destinationEmptyLabel")}
        nodes={nodes ?? []}
        onChange={(value) => onChange(value || null)}
        selectedId={selectedId ?? ""}
      />
    </FormField>
  );
}

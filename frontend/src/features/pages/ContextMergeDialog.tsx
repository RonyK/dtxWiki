import { Fragment, useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { readPage } from "../../shared/api/pages";
import { listTree, readTreeNode, type TreeNodePayload } from "../../shared/api/workspace";
import { ConfirmDialog } from "../../shared/components/ConfirmDialog";
import { Dialog } from "../../shared/components/Dialog";
import { SegmentedControl } from "../../shared/components/SegmentedControl";
import { TreePickerDropdown } from "../../shared/components/TreePickerDropdown";
import { formatRelativeTimestamp } from "../../shared/lib/datetime";
import {
  customMergeResolution,
  fieldResult,
  frontmatterExtent,
  itemOutcome,
  lineChanges,
  mergeItems,
  mergeSourceText,
  regionResult,
  wordChanges,
  type ContextMergeItem,
  type ContextMergeItemChoice,
  type ContextMergeMethod,
  type ContextMergeMetadataField,
  type ContextMergeOutcome,
  type ContextMergePreview,
  type ContextMergeResolution,
  type ContextMergeSide,
  type ContextMergeSideKey,
} from "./contextMerge";
import { MarkdownView } from "./MarkdownView";
import { PAGE_TYPE_OPTIONS } from "./pageFrontmatter";

/** The write the merge belongs to: it names the two sides and the confirm action. */
export type ContextMergeDialogKind = "publish" | "approve" | "bring_in" | "save";

type Method = ContextMergeMethod | "custom";
type RegionItem = Extract<ContextMergeItem, { kind: "region" }>;
type FieldItem = Extract<ContextMergeItem, { kind: "field" }>;
type Editing = { key: string; value: string | null };

/** Lines shown around a place or an automatic change when unchanged lines fold. */
const CONTEXT_LINES = 2;
/** A side longer than this scrolls inside its card and offers the larger views. */
const LARGE_REGION_LINES = 12;

type ContextMergeDialogProps = {
  kind: ContextMergeDialogKind;
  workspaceId: string | null;
  pageId: string;
  preview: ContextMergePreview;
  publishedVersionNumber?: number | null;
  /** The person's own text when it is not a stored version (a save); otherwise the draft is read. */
  oursBody?: string | null;
  busy?: boolean;
  error?: string | null;
  /** Shown after a confirm found newer content and a new preview replaced the old one. */
  refreshed?: boolean;
  onClose: () => void;
  onConfirm: (resolution: ContextMergeResolution) => void;
};

/**
 * One screen for every merge that needs a person: publication or approval,
 * merging the published version into a draft, and two saves of one draft.
 * Each overlapping place is a card in document order with both sides and its
 * result. A merge method decides every place at once; choosing a side or
 * editing a place makes the method "Custom", and the other places keep the
 * result of the method before it.
 */
export function ContextMergeDialog({
  kind,
  workspaceId,
  pageId,
  preview,
  publishedVersionNumber,
  oursBody,
  busy = false,
  error,
  refreshed = false,
  onClose,
  onConfirm,
}: ContextMergeDialogProps) {
  const { t, i18n } = useTranslation("context");
  const titleId = useId();
  const [method, setMethod] = useState<Method>("keep_both");
  const [baseMethod, setBaseMethod] = useState<ContextMergeMethod>("keep_both");
  const [choices, setChoices] = useState<Record<string, ContextMergeItemChoice>>({});
  const [editing, setEditing] = useState<Editing | null>(null);
  const [switchedAt, setSwitchedAt] = useState<number | null>(null);
  const [confirmSwitch, setConfirmSwitch] = useState<ContextMergeMethod | null>(null);
  const [view, setView] = useState<"places" | "document">("places");
  const [currentKey, setCurrentKey] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const [showResult, setShowResult] = useState(false);
  const [compareKey, setCompareKey] = useState<string | null>(null);
  const [readOurs, setReadOurs] = useState<{ key: string; body: string } | null>(null);
  const cardRefs = useRef(new Map<string, HTMLElement>());

  const previewKey = `${preview.version_id}:${preview.editing_revision}:${preview.target_version_id ?? ""}:${preview.keep_both?.result_hash ?? ""}:${preview.latest?.result_hash ?? ""}`;
  const items = useMemo(() => mergeItems(preview), [preview]);
  useEffect(() => {
    // A new preview means new content: earlier answers no longer apply to it.
    setMethod("keep_both");
    setBaseMethod("keep_both");
    setChoices({});
    setEditing(null);
    setSwitchedAt(null);
    setConfirmSwitch(null);
    setExpanded(new Set());
    setCompareKey(null);
    setCurrentKey(null);
  }, [previewKey]);

  // The automatic changes are the lines the other side brought: the result
  // compared with the draft (or the person's own text for a save).
  const oursKey = `${preview.version_id}:${preview.editing_revision}`;
  useEffect(() => {
    if (oursBody != null || kind === "save" || !workspaceId) return;
    let active = true;
    void readPage(workspaceId, pageId, preview.version_id)
      .then((page) => { if (active) setReadOurs({ key: oursKey, body: page.body_md }); })
      .catch(() => undefined);
    return () => { active = false; };
  }, [kind, oursBody, oursKey, pageId, preview.version_id, workspaceId]);
  const oursText = oursBody ?? (readOurs?.key === oursKey ? readOurs.body : null);
  const document = useMemo(() => buildDocument(preview, items, oursText), [preview, items, oursText]);

  const sides = preview.sides ?? null;
  const sideName = (side: ContextMergeSide | null | undefined) => side?.saved_by?.display_name || t("merge.someone");
  const sideTime = (side: ContextMergeSide | null | undefined) => formatRelativeTimestamp(side?.last_saved_at ?? null, i18n.language);
  const sideShort = (key: ContextMergeSideKey) => {
    if (kind === "save") return key === "ours" ? t("merge.sides.mine") : t("merge.sides.person", { name: sideName(sides?.theirs) });
    return key === "ours" ? t("merge.sides.draft") : t("merge.sides.published");
  };
  const sideLabel = (key: ContextMergeSideKey) => (
    kind !== "save" && key === "theirs" && publishedVersionNumber
      ? t("merge.sides.publishedVersion", { version: publishedVersionNumber })
      : sideShort(key)
  );
  // Who brought the automatic changes, as the owner of a change.
  const theirsOwner = kind === "save" ? t("merge.sides.personOwner", { name: sideName(sides?.theirs) }) : t("merge.sides.publishedOwner");
  const sideMeta = (key: ContextMergeSideKey) => (sides ? t("merge.sideMeta", { name: sideName(sides[key]), time: sideTime(sides[key]) }) : "");
  const methodName = (value: ContextMergeMethod) => (value === "keep_both" ? t("merge.keepBoth") : t("merge.latest"));

  const choiceOf = (item: ContextMergeItem) => choices[item.key] ?? { source: "method" as const };
  const outcomeOf = (item: ContextMergeItem) => itemOutcome(preview, item, choiceOf(item), baseMethod);
  const numberOf = (item: ContextMergeItem) => items.indexOf(item) + 1;
  const frontmatterLines = preview.keep_both ? frontmatterExtent(mergeSourceText(preview.keep_both)).lines : 0;
  const placeOf = (item: ContextMergeItem) => (
    item.kind === "field"
      ? t(`merge.fields.${item.field}`)
      : item.headings.slice(-2).join(" › ") || t("merge.placeLine", { line: item.startLine - frontmatterLines })
  );
  const outcomeShort = (outcome: ContextMergeOutcome) => (
    outcome === "both" ? t("merge.outcome.both") : outcome === "edit" ? t("merge.outcome.edit") : sideShort(outcome)
  );

  function decide(item: ContextMergeItem, next: ContextMergeItemChoice) {
    const before = outcomeOf(item);
    const after = itemOutcome(preview, item, next, baseMethod);
    if (method !== "custom" && after === before && next.source !== "edit") return;
    setChoices((current) => ({ ...current, [item.key]: next }));
    if (method !== "custom") {
      setMethod("custom");
      setSwitchedAt(numberOf(item));
    }
  }

  function pick(item: ContextMergeItem, side: ContextMergeSideKey) {
    if (editing) return;
    setCurrentKey(item.key);
    // Pressing the chosen side again lets go of it: a body region keeps both
    // sides; a property, which holds one value, returns to its method.
    if (outcomeOf(item) === side) decide(item, item.kind === "region" ? { source: "both" } : { source: "method" });
    else decide(item, { source: side });
  }

  function startEdit(item: ContextMergeItem) {
    setCurrentKey(item.key);
    const value = item.kind === "region"
      ? regionResult(preview, item, choiceOf(item), baseMethod).replace(/\n$/, "")
      : fieldResult(preview, item.field, choiceOf(item), baseMethod);
    setEditing({ key: item.key, value });
  }

  function finishEdit(item: ContextMergeItem) {
    if (!editing) return;
    decide(item, { source: "edit", value: editing.value });
    setEditing(null);
  }

  function chooseMethod(next: Method) {
    if (next === method) return;
    if (next === "custom") {
      setMethod("custom");
      setSwitchedAt(null);
      return;
    }
    const edited = items.filter((item) => choiceOf(item).source === "edit");
    if (method === "custom" && edited.length > 0) {
      setConfirmSwitch(next);
      return;
    }
    applyMethod(next);
  }

  function applyMethod(next: ContextMergeMethod) {
    setChoices({});
    setBaseMethod(next);
    setMethod(next);
    setSwitchedAt(null);
    setEditing(null);
    setConfirmSwitch(null);
  }

  function goTo(item: ContextMergeItem | undefined) {
    if (!item) return;
    setCurrentKey(item.key);
    cardRefs.current.get(item.key)?.scrollIntoView?.({ block: "start", behavior: "smooth" });
  }

  const currentIndex = Math.max(0, items.findIndex((item) => item.key === currentKey));
  const resolution = (): ContextMergeResolution | null => {
    if (method === "custom") return customMergeResolution(preview, items, choices, baseMethod);
    const candidate = preview[method];
    return candidate ? { choice: method, candidate, bodyMd: null, pageMetadata: null } : null;
  };
  const titleItem = items.find((item): item is FieldItem => item.kind === "field" && item.field === "title");
  const titleMissing = titleItem ? !fieldResult(preview, "title", choiceOf(titleItem), baseMethod)?.trim() : false;
  const ready = !editing && !titleMissing && Boolean(preview[method === "custom" ? "keep_both" : method]);
  const status = editing
    ? t("merge.status.editing")
    : titleMissing ? t("merge.status.titleMissing") : t("merge.status.ready", { count: Math.max(1, items.length) });
  const customCount = items.filter((item) => choiceOf(item).source !== "method").length;
  const latestSide = preview.latest_side;

  const cardProps = (item: ContextMergeItem) => ({
    item,
    number: numberOf(item),
    place: placeOf(item),
    current: item.key === currentKey,
    choice: choiceOf(item),
    outcome: outcomeOf(item),
    method,
    baseMethod,
    preview,
    editing: editing?.key === item.key ? editing : null,
    blocked: Boolean(editing && editing.key !== item.key),
    pageId,
    workspaceId,
    labels: { ours: sideLabel("ours"), theirs: sideLabel("theirs") },
    shorts: { ours: sideShort("ours"), theirs: sideShort("theirs") },
    metas: { ours: sideMeta("ours"), theirs: sideMeta("theirs") },
    methodName,
    onPick: (side: ContextMergeSideKey) => pick(item, side),
    onEdit: () => startEdit(item),
    onEditChange: (value: string | null) => setEditing({ key: item.key, value }),
    onEditCancel: () => setEditing(null),
    onEditDone: () => finishEdit(item),
    onCompare: () => setCompareKey(item.key),
    cardRef: (node: HTMLElement | null) => {
      if (node) cardRefs.current.set(item.key, node);
      else cardRefs.current.delete(item.key);
    },
  });
  const compareItem = items.find((item) => item.key === compareKey) ?? null;
  const resultBody = (() => {
    const value = resolution();
    return value ? value.bodyMd ?? value.candidate.body_md : "";
  })();
  const editedPlaces = items.filter((item) => choiceOf(item).source === "edit");

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
          <div className="context-merge-footer-row">
            <p className={`context-merge-status${ready ? " is-ready" : ""}`} role="status">{status}</p>
            {items.length > 1 ? (
              <div className="context-merge-footer-nav">
                <button aria-label={t("merge.previous")} className="icon-button text-button" disabled={currentIndex <= 0} onClick={() => goTo(items[currentIndex - 1])} type="button">↑</button>
                <span>{t("merge.position", { current: currentIndex + 1, total: items.length })}</span>
                <button aria-label={t("merge.next")} className="icon-button text-button" disabled={currentIndex >= items.length - 1} onClick={() => goTo(items[currentIndex + 1])} type="button">↓</button>
              </div>
            ) : null}
            <div className="dialog-actions">
              <button className="text-button context-merge-preview-button" disabled={busy || !ready} onClick={() => setShowResult(true)} type="button">{t("merge.previewResult")}</button>
              <button className="text-button" disabled={busy} onClick={onClose} type="button">{t("shell:dialog.close")}</button>
              <button
                className={kind === "publish" || kind === "approve" ? "icon-button success" : "icon-button primary"}
                disabled={!ready || busy}
                onClick={() => { const value = resolution(); if (value) onConfirm(value); }}
                type="button"
              >
                {busy ? t(`merge.confirming.${kind}`) : t(`merge.confirm.${kind}`)}
              </button>
            </div>
          </div>
        </div>
      )}
    >
      {/* While the answer is being written nothing in it can change: what was
          confirmed is what is written. */}
      <div className="context-merge-body" inert={busy} aria-busy={busy}>
        <div className="context-merge-top">
          <p className="context-merge-between">
            <span className="context-merge-pill is-ours">{sideLabel("ours")}</span> {sideMeta("ours")}
            <span aria-hidden="true"> ↔ </span>
            <span className="context-merge-pill is-theirs">{sideLabel("theirs")}</span> {sideMeta("theirs")}
          </p>
          {refreshed ? <p className="context-merge-refreshed" role="status">{t("merge.refreshed")}</p> : null}
          <p className="context-merge-notice">
            {t("merge.notice", { count: Math.max(1, items.length) })}
            {document.autoCount ? ` ${t("merge.noticeAuto", { count: document.autoCount })}` : ""}
          </p>
          <SegmentedControl
            className="context-merge-methods"
            label={t("merge.methodLabel")}
            onChange={(value) => chooseMethod(value as Method)}
            options={[
              { key: "keep_both", label: <MethodLabel hint={t("merge.keepBothHint")} name={t("merge.keepBoth")} /> },
              {
                key: "latest",
                label: <MethodLabel hint={t("merge.latestHint", { side: sideShort(latestSide), time: sideTime(sides?.[latestSide]) })} name={t("merge.latest")} />,
              },
              {
                key: "custom",
                label: (
                  <MethodLabel
                    hint={method === "custom"
                      ? t("merge.customPicked", { count: customCount, total: items.length })
                      : t("merge.customHint", { theirs: sideShort("theirs"), ours: sideShort("ours") })}
                    name={t("merge.custom")}
                  />
                ),
              },
            ]}
            value={method}
          />
          {method === "custom" && switchedAt ? (
            <p className="context-merge-switched" role="status">{t("merge.switched", { number: switchedAt, method: methodName(baseMethod) })}</p>
          ) : null}
        </div>
        <div className="context-merge-nav">
          {items.length > 0 ? (
            <div aria-label={t("merge.places")} className="context-merge-chips" role="group">
              {items.map((item) => (
                <button
                  aria-current={item.key === currentKey ? "true" : undefined}
                  className={`context-merge-chip${item.key === currentKey ? " is-current" : ""}`}
                  key={item.key}
                  onClick={() => goTo(item)}
                  type="button"
                >
                  <span className="context-merge-number" aria-hidden="true">{numberOf(item)}</span>
                  <span>{placeOf(item)} · {outcomeShort(outcomeOf(item))}</span>
                </button>
              ))}
            </div>
          ) : null}
          <span className="context-merge-nav-spacer" />
          {items.length > 1 ? (
            <div className="context-merge-steps">
              <button className="icon-button text-button" disabled={currentIndex <= 0} onClick={() => goTo(items[currentIndex - 1])} type="button">↑ {t("merge.previous")}</button>
              <span>{t("merge.position", { current: currentIndex + 1, total: items.length })}</span>
              <button className="icon-button text-button" disabled={currentIndex >= items.length - 1} onClick={() => goTo(items[currentIndex + 1])} type="button">{t("merge.next")} ↓</button>
            </div>
          ) : null}
          <SegmentedControl
            className="context-merge-view"
            compact
            label={t("merge.viewLabel")}
            onChange={(value) => setView(value as "places" | "document")}
            options={[{ key: "places", label: t("merge.viewPlaces") }, { key: "document", label: t("merge.viewDocument") }]}
            value={view}
          />
        </div>
        <div className="context-merge-places">
          {items.length === 0 ? <p className="context-merge-hint">{t("merge.noPlaces")}</p> : null}
          {items.filter((item): item is FieldItem => item.kind === "field").map((item) => <MergeCard key={item.key} {...cardProps(item)} />)}
          {document.blocks.map((block) => (
            block.kind === "card"
              ? <MergeCard key={block.item.key} {...cardProps(block.item)} />
              : (
                <DocumentLines
                  block={block}
                  expanded={expanded}
                  key={block.key}
                  onExpand={(key) => setExpanded((current) => new Set(current).add(key))}
                  theirs={theirsOwner}
                  whole={view === "document"}
                />
              )
          ))}
        </div>
      </div>
      {compareItem ? (
        <CompareDialog onClose={() => setCompareKey(null)} title={t("merge.compareTitle", { place: placeOf(compareItem) })}>
          <MergeCard {...cardProps(compareItem)} expanded />
        </CompareDialog>
      ) : null}
      {showResult ? (
        <CompareDialog onClose={() => setShowResult(false)} title={t("merge.previewTitle")}>
          <div className="context-merge-result-document"><MarkdownView body={resultBody} workspaceId={workspaceId} /></div>
        </CompareDialog>
      ) : null}
      {confirmSwitch ? (
        <SwitchConfirm
          count={Math.max(1, items.length)}
          edited={editedPlaces.map((item) => `${numberOf(item)} · ${placeOf(item)}`)}
          method={confirmSwitch}
          onCancel={() => setConfirmSwitch(null)}
          onConfirm={() => applyMethod(confirmSwitch)}
        />
      ) : null}
    </Dialog>
  );
}

function MethodLabel({ hint, name }: { hint: string; name: string }) {
  return (
    <span className="context-merge-method">
      <strong>{name}</strong>
      <span>{hint}</span>
    </span>
  );
}

function SwitchConfirm({ count, edited, method, onCancel, onConfirm }: {
  count: number;
  edited: string[];
  method: ContextMergeMethod;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const { t } = useTranslation("context");
  const titleId = useId();
  return (
    <ConfirmDialog
      confirmLabel={t("merge.switchConfirm")}
      labelledBy={titleId}
      onCancel={onCancel}
      onConfirm={onConfirm}
      title={<span id={titleId}>{t("merge.switchTitle")}</span>}
      tone="warning"
    >
      <p>{t(`merge.switchBody.${method}`, { count })}</p>
      <p className="context-merge-notice">{t("merge.switchEdited", { count: edited.length, places: edited.join(", ") })}</p>
    </ConfirmDialog>
  );
}

function CompareDialog({ children, onClose, title }: { children: ReactNode; onClose: () => void; title: string }) {
  const titleId = useId();
  return (
    <Dialog className="context-merge-compare" labelledBy={titleId} onClose={onClose} title={<span id={titleId}>{title}</span>}>
      {children}
    </Dialog>
  );
}

type MergeCardProps = {
  item: ContextMergeItem;
  number: number;
  place: string;
  current: boolean;
  choice: ContextMergeItemChoice;
  outcome: ContextMergeOutcome;
  method: Method;
  baseMethod: ContextMergeMethod;
  preview: ContextMergePreview;
  editing: Editing | null;
  /** Another place is being edited: finish it first. */
  blocked: boolean;
  pageId: string;
  workspaceId: string | null;
  labels: Record<ContextMergeSideKey, string>;
  shorts: Record<ContextMergeSideKey, string>;
  metas: Record<ContextMergeSideKey, string>;
  methodName: (method: ContextMergeMethod) => string;
  onPick: (side: ContextMergeSideKey) => void;
  onEdit: () => void;
  onEditChange: (value: string | null) => void;
  onEditCancel: () => void;
  onEditDone: () => void;
  onCompare: () => void;
  cardRef: (node: HTMLElement | null) => void;
  /** The full-screen comparison: three columns, no height limit. */
  expanded?: boolean;
};

/** One place: both sides (press one to use it), then the result and its editor. */
function MergeCard(props: MergeCardProps) {
  const { t } = useTranslation("context");
  const { item, number, place, current, choice, outcome, method, baseMethod, preview, editing, blocked, labels, shorts, metas, expanded = false } = props;
  const [stacked, setStacked] = useState(false);
  const theirsText = item.kind === "region" ? item.conflict.theirs : "";
  const oursText = item.kind === "region" ? item.conflict.ours : "";
  const lineCount = (text: string) => text.replace(/\n$/, "").split("\n").length;
  const large = item.kind === "region" && Math.max(lineCount(theirsText), lineCount(oursText)) > LARGE_REGION_LINES;
  const words = useMemo(() => (item.kind === "region" ? wordChanges(theirsText, oursText) : null), [item.kind, theirsText, oursText]);
  const lines = useMemo(() => {
    if (!large) return null;
    const left = theirsText.replace(/\n$/, "").split("\n");
    const right = oursText.replace(/\n$/, "").split("\n");
    const forward = lineChanges(left, right);
    const backward = lineChanges(right, left);
    return { theirs: backward?.added ?? null, ours: forward?.added ?? null };
  }, [large, theirsText, oursText]);
  const scroll = useSyncedScroll();

  const stateLabel = choice.source === "method"
    ? method === "custom" ? t("merge.state.previous") : t("merge.state.method")
    : choice.source === "edit" ? t("merge.state.edited")
      : choice.source === "both" ? t("merge.state.pickedBoth") : t("merge.state.picked", { side: shorts[choice.source] });
  const resultLabel = editing
    ? t("merge.resultEditing")
    : choice.source === "method" && item.kind === "region" ? t("merge.resultMethod", { method: props.methodName(baseMethod) }) : t("merge.result");
  const hint = editing ? null
    : choice.source === "method" && item.kind === "field" && baseMethod === "keep_both" ? t("merge.scalarNote", { side: shorts[preview.latest_side] })
      : choice.source === "method" && item.kind === "region" && outcome === "both" ? t("merge.pickHint", { theirs: shorts.theirs, ours: shorts.ours })
        : null;

  const side = (key: ContextMergeSideKey) => {
    const selected = outcome === key;
    const included = outcome === "both";
    const text = item.kind === "region"
      ? <MarkedText marks={words ? words[key === "theirs" ? "left" : "right"] : null} text={key === "theirs" ? theirsText : oursText} />
      : <FieldValue field={item.field} value={fieldValueOf(preview, item.field, key)} workspaceId={props.workspaceId} />;
    return (
      <SidePane
        blocked={blocked}
        changedLines={lines ? lines[key] : null}
        dimmed={!selected && !included}
        included={included}
        key={key}
        label={labels[key]}
        large={large && !expanded}
        meta={metas[key]}
        onPick={() => props.onPick(key)}
        scroll={scroll}
        selected={selected}
        side={key}
        tag={selected ? t("merge.inResult") : included ? t("merge.included") : null}
      >
        {text}
      </SidePane>
    );
  };

  const resultText = item.kind === "region"
    ? regionResult(preview, item, choice, baseMethod)
    : fieldResult(preview, item.field, choice, baseMethod);

  return (
    <section
      aria-label={t("merge.cardLabel", { number, place })}
      className={`context-merge-card${current ? " is-current" : ""}${large ? " is-large" : ""}${expanded ? " is-expanded" : ""}`}
      ref={expanded ? undefined : props.cardRef}
      tabIndex={-1}
    >
      <header className="context-merge-card-head">
        <span className="context-merge-chip is-static"><span className="context-merge-number" aria-hidden="true">{number}</span>{place}</span>
        <span className="context-merge-card-title">
          {item.kind === "field" ? t(`merge.fieldChanged.${item.field}`) : t("merge.regionChanged")}
          {large ? ` · ${t("merge.lineCounts", { theirs: shorts.theirs, theirsCount: lineCount(theirsText), ours: shorts.ours, oursCount: lineCount(oursText) })}` : ""}
        </span>
        <span className={`context-merge-state${choice.source === "method" ? "" : " is-custom"}`}>{stateLabel}</span>
        {large && !expanded ? (
          <span className="context-merge-card-tools">
            <SegmentedControl
              compact
              label={t("merge.layoutLabel")}
              onChange={(value) => setStacked(value === "stacked")}
              options={[{ key: "side", label: t("merge.layoutSide") }, { key: "stacked", label: t("merge.layoutStacked") }]}
              value={stacked ? "stacked" : "side"}
            />
            <button className="icon-button text-button" onClick={props.onCompare} type="button">{t("merge.compareFull")}</button>
          </span>
        ) : null}
      </header>
      <div className={`context-merge-sides${stacked ? " is-stacked" : ""}${expanded ? " is-three" : ""}`}>
        {side("theirs")}
        {side("ours")}
        {expanded ? <ResultPane {...props} hint={hint} label={resultLabel} resultText={resultText} /> : null}
      </div>
      {expanded ? null : <ResultPane {...props} hint={hint} label={resultLabel} resultText={resultText} />}
    </section>
  );
}

function fieldValueOf(preview: ContextMergePreview, field: ContextMergeMetadataField, side: ContextMergeSideKey): string | null {
  return fieldResult(preview, field, { source: side }, "keep_both");
}

function ResultPane(props: MergeCardProps & { hint: string | null; label: string; resultText: string | null }) {
  const { t } = useTranslation("context");
  const { item, editing, blocked, hint, label, resultText } = props;
  return (
    <div className="context-merge-result">
      <div className="context-merge-result-head">
        <span className="context-merge-result-label">{label}</span>
        {hint ? <span className="context-merge-hint">{hint}</span> : null}
        <span className="context-merge-nav-spacer" />
        {editing ? (
          <>
            <button className="icon-button text-button" onClick={props.onEditCancel} type="button">{t("merge.editCancel")}</button>
            <button
              className="icon-button primary"
              disabled={item.kind === "field" && item.field === "title" && !editing.value?.trim()}
              onClick={props.onEditDone}
              type="button"
            >
              {t("merge.editDone")}
            </button>
          </>
        ) : (
          <button className="text-button context-merge-edit" disabled={blocked} onClick={props.onEdit} type="button">
            ✎ {t("merge.edit")}
          </button>
        )}
      </div>
      {editing ? (
        <>
          <ResultEditor {...props} editing={editing} />
          <p className="context-merge-hint">{item.kind === "region" ? t("merge.editHint") : t("merge.editFieldHint")}</p>
        </>
      ) : item.kind === "region" ? (
        <pre className="context-merge-result-text">{resultText || " "}</pre>
      ) : (
        <div className="context-merge-result-text"><FieldValue field={item.field} value={resultText} workspaceId={props.workspaceId} /></div>
      )}
    </div>
  );
}

function ResultEditor({ item, editing, onEditChange, pageId, place, workspaceId }: MergeCardProps & { editing: Editing }) {
  const { t } = useTranslation("context");
  const { t: tPages } = useTranslation("pages");
  const label = t("merge.editLabel", { place });
  if (item.kind === "region") {
    const rows = Math.min(16, Math.max(3, (editing.value ?? "").split("\n").length + 1));
    return (
      <textarea
        aria-label={label}
        autoFocus
        className="context-merge-editor"
        onChange={(event) => onEditChange(event.target.value)}
        rows={rows}
        value={editing.value ?? ""}
      />
    );
  }
  if (item.field === "title") {
    return <input aria-label={label} autoFocus className="context-merge-editor" onChange={(event) => onEditChange(event.target.value)} type="text" value={editing.value ?? ""} />;
  }
  if (item.field === "page_type") {
    return (
      <select aria-label={label} className="context-merge-editor" onChange={(event) => onEditChange(event.target.value)} value={editing.value ?? ""}>
        {PAGE_TYPE_OPTIONS.map((option) => (
          <option key={option.value} value={option.value}>{tPages(`editor.pageTypes.${option.value}.label`, { defaultValue: option.label })}</option>
        ))}
      </select>
    );
  }
  return <PlacementPicker onChange={onEditChange} pageId={pageId} selectedId={editing.value} workspaceId={workspaceId} />;
}

type SyncedScroll = { register: (side: ContextMergeSideKey, node: HTMLDivElement | null) => void; onScroll: (side: ContextMergeSideKey) => void };

/** Two panes of one place scroll together, by proportion, so the same part stays side by side. */
function useSyncedScroll(): SyncedScroll {
  const nodes = useRef(new Map<ContextMergeSideKey, HTMLDivElement>());
  const following = useRef<ContextMergeSideKey | null>(null);
  return useMemo(() => ({
    register(side, node) {
      if (node) nodes.current.set(side, node);
      else nodes.current.delete(side);
    },
    onScroll(side) {
      if (following.current === side) {
        following.current = null;
        return;
      }
      const from = nodes.current.get(side);
      const to = nodes.current.get(side === "ours" ? "theirs" : "ours");
      if (!from || !to) return;
      const range = from.scrollHeight - from.clientHeight;
      const target = (range > 0 ? from.scrollTop / range : 0) * (to.scrollHeight - to.clientHeight);
      // Only a real move scrolls the other pane, so only then is its echo skipped.
      if (Math.abs(to.scrollTop - target) < 1) return;
      following.current = side === "ours" ? "theirs" : "ours";
      to.scrollTop = target;
    },
  }), []);
}

function SidePane({ blocked, changedLines, children, dimmed, included, label, large, meta, onPick, scroll, selected, side, tag }: {
  blocked: boolean;
  changedLines: boolean[] | null;
  children: ReactNode;
  dimmed: boolean;
  included: boolean;
  label: string;
  large: boolean;
  meta: string;
  onPick: () => void;
  scroll: SyncedScroll;
  selected: boolean;
  side: ContextMergeSideKey;
  tag: string | null;
}) {
  const classes = ["context-merge-side", `is-${side}`, selected ? "is-selected" : "", included ? "is-included" : "", dimmed ? "is-dimmed" : ""]
    .filter(Boolean).join(" ");
  return (
    // The header button is the keyboard and screen-reader control; the whole
    // pane is only a larger target for the pointer.
    // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-static-element-interactions
    <div
      className={classes}
      onClick={(event) => {
        // A click that selects text reads it; it does not choose the side.
        if (blocked || window.getSelection?.()?.toString() || (event.target as HTMLElement).closest("button")) return;
        onPick();
      }}
    >
      <button aria-pressed={selected} className="context-merge-side-pick" disabled={blocked} onClick={onPick} type="button">
        <span aria-hidden="true" className="context-merge-radio" />
        <span className={`context-merge-pill is-${side}`}>{label}</span>
        <span className="context-merge-side-meta">{meta}</span>
        {tag ? <span className="context-merge-side-tag">{tag}</span> : null}
      </button>
      <div className="context-merge-side-body">
        <div
          className={`context-merge-side-text${large ? " is-large" : ""}`}
          onScroll={large ? () => scroll.onScroll(side) : undefined}
          ref={large ? (node) => scroll.register(side, node) : undefined}
        >
          {children}
        </div>
        {large && changedLines ? (
          <div aria-hidden="true" className="context-merge-minimap">
            {changedLines.map((changed, index) => (changed
              ? <i key={index} style={{ top: `${(index / changedLines.length) * 100}%` }} />
              : null))}
          </div>
        ) : null}
      </div>
    </div>
  );
}

function MarkedText({ marks, text }: { marks: Array<[string, boolean]> | null; text: string }) {
  if (!marks) return <pre>{text}</pre>;
  return <pre>{marks.map(([token, changed], index) => (changed ? <mark key={index}>{token}</mark> : <Fragment key={index}>{token}</Fragment>))}</pre>;
}

function FieldValue({ field, value, workspaceId }: { field: ContextMergeMetadataField; value: string | null; workspaceId: string | null }) {
  const { t: tPages } = useTranslation("pages");
  if (field === "page_type") return <>{value ? tPages(`editor.pageTypes.${value}.label`, { defaultValue: value }) : "—"}</>;
  if (field === "placement_parent_node_id") return <PlacementLabel nodeId={value} workspaceId={workspaceId} />;
  return <>{value || "—"}</>;
}

type DocumentLine = { line: number; text: string; added: boolean; removedBefore: number };
type DocumentBlock =
  | { kind: "lines"; key: string; lines: DocumentLine[]; removedAtEnd: number; beforeCard: boolean; afterCard: boolean }
  | { kind: "card"; item: RegionItem };

/**
 * The body in order: unchanged and automatically merged lines between the
 * places. A line the draft (or the person's own text) does not have came from
 * the other side; so did a removal outside a place.
 */
function buildDocument(preview: ContextMergePreview, items: ContextMergeItem[], ours: string | null) {
  const candidate = preview.keep_both;
  if (!candidate) return { blocks: [] as DocumentBlock[], autoCount: null as number | null };
  const text = mergeSourceText(candidate);
  const all = text.split("\n");
  if (all[all.length - 1] === "") all.pop();
  const start = frontmatterExtent(text).lines;
  const regions = items.filter((item): item is RegionItem => item.kind === "region").sort((left, right) => left.startLine - right.startLine);
  let changes: ReturnType<typeof lineChanges> = null;
  if (ours !== null) {
    const from = ours.split("\n");
    if (from[from.length - 1] === "") from.pop();
    changes = lineChanges(from, all);
  }
  const inRegion = (line: number) => regions.some((region) => line >= region.startLine && line <= region.endLine);
  const blocks: DocumentBlock[] = [];
  let lines: DocumentLine[] = [];
  let autoCount = 0;
  let previousAdded = false;
  const flush = (beforeCard: boolean) => {
    if (lines.length === 0) return;
    const afterCard = blocks[blocks.length - 1]?.kind === "card";
    blocks.push({ kind: "lines", key: `lines:${lines[0].line}`, lines, removedAtEnd: 0, beforeCard, afterCard });
    lines = [];
  };
  for (let line = start + 1; line <= all.length;) {
    const region = regions.find((entry) => entry.startLine === line);
    if (region) {
      flush(true);
      blocks.push({ kind: "card", item: region });
      line = region.endLine + 1;
      previousAdded = false;
      continue;
    }
    const added = changes?.added[line - 1] ?? false;
    const removedBefore = changes && !inRegion(line - 1) && line - 1 > start ? changes.removedBefore[line - 1] : 0;
    if ((added && !previousAdded) || removedBefore > 0) autoCount += 1;
    previousAdded = added;
    lines.push({ line, text: all[line - 1], added, removedBefore });
    line += 1;
  }
  const removedAtEnd = changes && !inRegion(all.length) ? changes.removedBefore[all.length] : 0;
  if (removedAtEnd > 0) autoCount += 1;
  flush(false);
  const last = blocks[blocks.length - 1];
  if (last?.kind === "lines") last.removedAtEnd = removedAtEnd;
  return { blocks, autoCount: changes ? autoCount : null };
}

/** Lines between places; in the places view unchanged runs fold, keeping a little context. */
function DocumentLines({ block, expanded, onExpand, theirs, whole }: {
  block: Extract<DocumentBlock, { kind: "lines" }>;
  expanded: ReadonlySet<string>;
  onExpand: (key: string) => void;
  theirs: string;
  whole: boolean;
}) {
  const { t } = useTranslation("context");
  const count = block.lines.length;
  const keep = block.lines.map((entry, index) => (
    whole
    || entry.added
    || entry.removedBefore > 0
    || (block.afterCard && index < CONTEXT_LINES)
    || (block.beforeCard && index >= count - CONTEXT_LINES)
    || block.lines.slice(Math.max(0, index - CONTEXT_LINES), index + CONTEXT_LINES + 1).some((near) => near.added || near.removedBefore > 0)
  ));
  const parts: ReactNode[] = [];
  let index = 0;
  while (index < count) {
    const entry = block.lines[index];
    if (!keep[index]) {
      let end = index;
      while (end < count && !keep[end]) end += 1;
      const key = `${block.key}:${entry.line}`;
      if (expanded.has(key)) {
        for (let at = index; at < end; at += 1) parts.push(<ContextLine key={block.lines[at].line} text={block.lines[at].text} />);
      } else {
        parts.push(
          <button className="context-merge-fold" key={key} onClick={() => onExpand(key)} type="button">
            {t("merge.fold", { count: end - index })}
          </button>,
        );
      }
      index = end;
      continue;
    }
    if (entry.removedBefore > 0) {
      parts.push(<p className="context-merge-auto is-removed" key={`removed:${entry.line}`}>{t("merge.autoRemoved", { side: theirs, count: entry.removedBefore })}</p>);
    }
    if (entry.added) {
      let end = index;
      while (end < count && block.lines[end].added) end += 1;
      parts.push(
        <div className="context-merge-auto" key={`auto:${entry.line}`}>
          <span className="context-merge-auto-label">{t("merge.autoMerged", { side: theirs })}</span>
          <pre>{block.lines.slice(index, end).map((line) => line.text).join("\n")}</pre>
        </div>,
      );
      index = end;
      continue;
    }
    parts.push(<ContextLine key={entry.line} text={entry.text} />);
    index += 1;
  }
  if (block.removedAtEnd > 0) {
    parts.push(<p className="context-merge-auto is-removed" key="removed:end">{t("merge.autoRemoved", { side: theirs, count: block.removedAtEnd })}</p>);
  }
  return <div className="context-merge-lines">{parts}</div>;
}

function ContextLine({ text }: { text: string }) {
  return <pre className="context-merge-context">{text || " "}</pre>;
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
    <TreePickerDropdown
      ariaLabel={t("editor.destinationLabel")}
      disabled={!nodes || nodes.length === 0}
      emptyLabel={t("editor.destinationEmptyLabel")}
      nodes={nodes ?? []}
      onChange={(value) => onChange(value || null)}
      selectedId={selectedId ?? ""}
    />
  );
}

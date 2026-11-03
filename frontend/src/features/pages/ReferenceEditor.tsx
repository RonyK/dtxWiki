import { autoUpdate, computePosition, flip, offset, shift } from "@floating-ui/dom";
import { AlertCircle, BookOpen, Check, ChevronRight, CornerDownRight, Folder, Link2, Plus, Quote, Search, Trash2, X } from "lucide-react";
import { type KeyboardEvent, type ReactNode, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { searchWorkspace, type RankedSearchResult, type SearchScope } from "../../shared/api/operations";
import { previewLinkTargets } from "../../shared/api/linkTargets";
import { newIdempotencyKey } from "../../shared/api/pages";
import type { DocumentReferenceKind, DocumentReferenceTarget, ReferenceView } from "../../shared/api/references";
import { apiErrorMessage } from "../../shared/api/client";
import { Dialog, DialogCloseButton } from "../../shared/components/Dialog";
import { presentLocation, resultLocation, searchPreview } from "../search/searchResultText";
import { kindLabel, referenceKey } from "./documentReferences";

export type UnresolvedCitation = { href: string; start: number; end: number };

/**
 * The editor's references live behind one header button. The list floats over
 * the editor instead of taking height from it; nothing about the editing
 * surface moves when it opens or closes.
 */
export function ReferencesControl({
  references, unresolved, checkingLocators, open, onOpenChange, disabled,
  onAdd, onInsert, onRemove, onConvertLink, onLocate, onRetry, onReconcile, retryDisabled,
}: {
  references: ReferenceView[];
  unresolved: UnresolvedCitation[];
  checkingLocators?: ReadonlySet<string>;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  disabled: boolean;
  onAdd: () => void;
  onInsert: (reference?: ReferenceView) => void;
  onRemove: (reference: ReferenceView) => void;
  onConvertLink: () => void;
  onLocate: (offset: number) => void;
  onRetry?: () => void;
  onReconcile?: () => void;
  retryDisabled?: boolean;
}) {
  const { t } = useTranslation("pages");
  const popoverId = useId();
  const titleId = useId();
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const popoverRef = useRef<HTMLElement | null>(null);
  const [position, setPosition] = useState<{ x: number; y: number } | null>(null);
  const checking = (href: string) => Boolean(checkingLocators?.has(href));
  const pendingUnresolved = unresolved.filter((item) => !checking(item.href));
  const unavailable = references.filter((item) => item.state === "unavailable" && !item.occurrences?.some((occurrence) => occurrence.href && checking(occurrence.href)));
  const citedPlaces = references.reduce((sum, item) => sum + item.inline_count, 0);
  const uncited = references.filter((item) => item.inline_count === 0).length;
  const attention = Boolean(onRetry) || pendingUnresolved.length > 0 || unavailable.length > 0;

  useEffect(() => {
    if (!open) return undefined;
    function onPointerDown(event: PointerEvent) {
      const target = event.target;
      if (!(target instanceof Node) || popoverRef.current?.contains(target) || triggerRef.current?.contains(target)) return;
      onOpenChange(false);
    }
    function onKeyDown(event: globalThis.KeyboardEvent) {
      const target = event.target;
      if (event.key !== "Escape" || !(target instanceof Node)) return;
      if (!popoverRef.current?.contains(target) && !triggerRef.current?.contains(target)) return;
      event.preventDefault();
      onOpenChange(false);
      triggerRef.current?.focus();
    }
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown, true);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown, true);
    };
  }, [open, onOpenChange]);

  useLayoutEffect(() => {
    const reference = triggerRef.current;
    const floating = popoverRef.current;
    if (!open || !reference || !floating) { setPosition(null); return undefined; }
    let disposed = false;
    const update = () => {
      void computePosition(reference, floating, {
        placement: "bottom-end",
        strategy: "fixed",
        middleware: [offset(8), flip({ fallbackPlacements: ["bottom-start"], padding: 12 }), shift({ padding: 12 })],
      }).then(({ x, y }) => { if (!disposed) setPosition({ x, y }); });
    };
    const cleanup = autoUpdate(reference, floating, update);
    update();
    return () => { disposed = true; cleanup(); };
  }, [open]);

  const title = (reference: ReferenceView) => reference.state === "unavailable"
    ? t(reference.occurrences?.some((item) => item.href && checking(item.href)) ? "checkingReference.label" : "references.unavailable")
    : reference.title ?? "";
  const itemLabel = (reference: ReferenceView, action: string) => t("references.itemAction", { action, number: reference.number, title: title(reference) });

  return (
    <>
      <button
        aria-controls={open ? popoverId : undefined}
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-label={t(attention ? "references.triggerAttention" : "references.trigger", { count: references.length })}
        className="editor-references-trigger icon-button text-button"
        data-attention={attention || undefined}
        onClick={() => onOpenChange(!open)}
        ref={triggerRef}
        title={t("references.title")}
        type="button"
      >
        <BookOpen aria-hidden="true" size={16} />
        <span className="editor-references-trigger-label">{t("references.title")}</span>
        <span aria-hidden="true" className="editor-references-count">{references.length}</span>
      </button>
      {open ? (
        <section
          aria-labelledby={titleId}
          className="editor-references-popover"
          id={popoverId}
          ref={popoverRef}
          role="dialog"
          style={position ? { left: position.x, top: position.y } : { visibility: "hidden" }}
        >
          <header className="editor-references-head">
            <BookOpen aria-hidden="true" size={16} />
            <h2 id={titleId}>{t("references.title")}</h2>
            <p className="editor-references-summary">
              <span>{t("references.summaryCited", { count: citedPlaces })}</span>
              {uncited ? <span className="is-warning">{t("references.summaryUncited", { count: uncited })}</span> : null}
              {pendingUnresolved.length ? <span className="is-warning">{t("references.summaryUnresolved", { count: pendingUnresolved.length })}</span> : null}
            </p>
            <button aria-label={t("references.close")} className="icon-button" onClick={() => { onOpenChange(false); triggerRef.current?.focus(); }} type="button">
              <X aria-hidden="true" size={16} />
            </button>
          </header>
          <div className="editor-references-actions">
            <button className="icon-button text-button" disabled={disabled} onClick={onAdd} type="button"><Plus aria-hidden="true" size={15} />{t("references.add")}</button>
            <button className="icon-button text-button" disabled={disabled} onClick={() => onInsert()} type="button"><Quote aria-hidden="true" size={15} />{t("references.insert")}</button>
            <button className="icon-button text-button" disabled={disabled} onClick={onConvertLink} type="button"><Link2 aria-hidden="true" size={15} />{t("references.convertLink")}</button>
          </div>
          {onRetry ? (
            <div className="editor-references-retry" role="status">
              <p>{t("references.ambiguous")}</p>
              <button className="icon-button text-button" disabled={retryDisabled} onClick={onRetry} type="button">{t("references.retry")}</button>
              {onReconcile ? <button className="icon-button text-button" disabled={retryDisabled} onClick={onReconcile} type="button">{t("references.reconcile")}</button> : null}
            </div>
          ) : null}
          {references.length ? (
            <ol className="editor-references-list">
              {references.map((reference) => (
                <li className="editor-reference-row" key={referenceKey(reference)} value={reference.number}>
                  <span aria-hidden="true" className={`editor-reference-number${reference.inline_count ? "" : " is-dim"}`}>{reference.number}</span>
                  <span className="editor-reference-main">
                    <span className="editor-reference-title-line">
                      <span className="search-result-kind" data-identity={kindLabel(reference.target_kind)}>{t(`references.kinds.${kindLabel(reference.target_kind)}`)}</span>
                      <span className="editor-reference-title" title={title(reference)}>{title(reference)}</span>
                    </span>
                    {reference.state === "deleted" ? <span className="editor-reference-meta">{t("references.deleted")}</span> : null}
                    {reference.state !== "unavailable" && reference.location_warning ? <span className="editor-reference-meta">{t("references.fragmentUnavailable")}</span> : null}
                    {reference.state !== "unavailable" && reference.note ? <span className="editor-reference-meta">{t("references.noteLabel")} · {reference.note}</span> : null}
                  </span>
                  {reference.inline_count === 0 ? (
                    <span className="editor-reference-uncited"><AlertCircle aria-hidden="true" size={13} />{t("references.notCited")}</span>
                  ) : (
                    <button aria-label={itemLabel(reference, t("references.inlineCount", { count: reference.inline_count }))} className="editor-reference-occurrences" disabled={!reference.occurrences?.length} onClick={() => onLocate(reference.occurrences![0].start)} type="button">
                      <CornerDownRight aria-hidden="true" size={14} />{t("references.inlineCount", { count: reference.inline_count })}
                    </button>
                  )}
                  <span className="editor-reference-actions">
                    <button aria-label={itemLabel(reference, t("references.insert"))} className="icon-button" disabled={disabled || !reference.href || !reference.target_id} onClick={() => onInsert(reference)} title={t("references.insert")} type="button"><Quote aria-hidden="true" size={15} /></button>
                    {reference.metadata_present || reference.metadata_id ? (
                      <button aria-label={itemLabel(reference, t("references.remove"))} className="icon-button" disabled={disabled} onClick={() => onRemove(reference)} title={t("references.remove")} type="button"><Trash2 aria-hidden="true" size={15} /></button>
                    ) : null}
                  </span>
                </li>
              ))}
            </ol>
          ) : <p className="editor-references-empty">{t("references.empty")}</p>}
          {unresolved.length ? (
            <section aria-label={t("references.unresolvedTitle")} className="editor-references-unresolved">
              <h3>{t("references.unresolvedTitle")}</h3>
              <ul>
                {unresolved.map((item) => (
                  <li key={`${item.start}:${item.href}`}>
                    <code title={item.href}>{`@cite{${item.href}}`}</code>
                    <span className={checking(item.href) ? "" : "is-warning"}>{t(checking(item.href) ? "checkingReference.label" : "references.unresolvedState")}</span>
                    <button className="editor-reference-occurrences" onClick={() => onLocate(item.start)} type="button">
                      <CornerDownRight aria-hidden="true" size={14} />{t("references.locate")}
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
        </section>
      ) : null}
    </>
  );
}

export type ReferenceChoice = DocumentReferenceTarget & { title: string; location: string | null };
export type ReferenceAction = "inline" | "metadata";

type PickerOption = {
  key: string;
  target_kind: DocumentReferenceKind;
  target_id: string;
  title: string;
  snippet: string;
  heading: string | null;
  location: string[];
};

const CITABLE = new Set<string>(["context_page", "library_item", "agent_page"]);

function optionFromResult(result: RankedSearchResult): PickerOption {
  // A page's snippet begins with its own heading; the title is already shown.
  const preview = searchPreview(result.snippet ?? "") || searchPreview(result.summary ?? "");
  const snippet = preview.startsWith(`${result.title} `) ? preview.slice(result.title.length + 1) : preview;
  const heading = result.snippet && result.heading_path
    ? result.heading_path.filter((part) => part.trim() !== result.title.trim()).at(-1) ?? null
    : null;
  return {
    key: `${result.type}:${result.id}`, target_kind: result.type as DocumentReferenceKind, target_id: result.id,
    title: result.title, snippet, heading, location: resultLocation(result),
  };
}

function optionFromReference(reference: ReferenceView): PickerOption | null {
  if (!reference.target_id || reference.state === "unavailable") return null;
  return {
    key: referenceKey(reference), target_kind: reference.target_kind, target_id: reference.target_id,
    title: reference.title ?? "", snippet: reference.note ?? "", heading: null,
    location: reference.location ? reference.location.split(" / ") : [],
  };
}

/** A pasted URL or system ref, not words to search for. */
function looksLikeAddress(value: string) {
  return /^\S+$/.test(value) && (value.includes("/") || value.startsWith("dtxwiki:"));
}

function highlight(text: string, query: string): ReactNode {
  const terms = [...new Set(query.trim().split(/\s+/).filter((term) => term.length > 0))];
  if (!terms.length || !text) return text;
  const pattern = new RegExp(`(${terms.map((term) => term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "giu");
  return text.split(pattern).map((part, index) => index % 2 === 1 ? <mark key={index}>{part}</mark> : part);
}

/**
 * One list for both actions: choose a document, then cite it in the text or
 * register it alone. Results read like Search results; documents this page
 * already cites carry their reference number.
 */
export function ReferencePicker({ workspaceId, references, mode, onClose, onSelect, busy = false, initialQuery = "" }: {
  workspaceId: string;
  references: ReferenceView[];
  mode: ReferenceAction;
  onClose: () => void;
  onSelect: (choice: ReferenceChoice, action: ReferenceAction) => void;
  busy?: boolean;
  initialQuery?: string;
}) {
  const { t } = useTranslation("pages");
  const id = useId();
  const queryRef = useRef<HTMLInputElement>(null);
  const searchGeneration = useRef(0);
  const controllers = useRef(new Set<AbortController>());
  const [query, setQuery] = useState(initialQuery);
  const [scope, setScope] = useState<SearchScope>("all");
  const [note, setNote] = useState("");
  const [results, setResults] = useState<RankedSearchResult[]>([]);
  const [addressMatch, setAddressMatch] = useState<PickerOption | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const trimmed = query.trim();
  const address = looksLikeAddress(trimmed);
  useEffect(() => { queryRef.current?.focus(); }, []);
  useEffect(() => {
    const generation = ++searchGeneration.current;
    const controller = new AbortController();
    const pendingControllers = controllers.current;
    pendingControllers.add(controller);
    setNextCursor(null);
    setAddressMatch(null);
    if (!trimmed) { setResults([]); setLoading(false); return () => { controller.abort(); pendingControllers.delete(controller); }; }
    setLoading(true);
    setError(null);
    const timer = window.setTimeout(() => {
      const current = () => !controller.signal.aborted && generation === searchGeneration.current;
      // A pasted address takes the save-time conversion through the editor's
      // read-only preview, so the picker names exactly what saving will cite.
      const addressLookup = address
        ? previewLinkTargets(workspaceId, { body_md: `@cite{${trimmed}}`, source_page_id: null, source_version_id: null, request_id: newIdempotencyKey() }, controller.signal)
          .then((preview) => {
            const found = preview.references?.find((item) => item.target_id && item.state !== "unavailable");
            if (current()) setAddressMatch(found ? optionFromReference(found) : null);
          })
          .catch(() => undefined)
        : Promise.resolve();
      const search = searchWorkspace(workspaceId, { query: trimmed, scope, limit: 30 }, { signal: controller.signal }).then((data) => {
        if (!current()) return;
        setResults(data.ranked_results.filter((item) => CITABLE.has(item.type)));
        setNextCursor(data.has_more ? data.next_cursor ?? null : null);
      }).catch((cause: unknown) => {
        if (!controller.signal.aborted) setError(apiErrorMessage(cause, t("references.searchFailed")));
      });
      void Promise.all([addressLookup, search]).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    }, 250);
    return () => { window.clearTimeout(timer); pendingControllers.forEach((item) => item.abort()); pendingControllers.clear(); };
  }, [address, trimmed, scope, workspaceId, t]);

  async function loadMore() {
    if (!nextCursor || loading) return;
    const generation = searchGeneration.current;
    const controller = new AbortController();
    controllers.current.add(controller);
    setLoading(true);
    try {
      const data = await searchWorkspace(workspaceId, { query: trimmed, scope, limit: 30, cursor: nextCursor }, { signal: controller.signal });
      if (controller.signal.aborted || generation !== searchGeneration.current) return;
      setResults((previous) => [...new Map([...previous, ...data.ranked_results.filter((item) => CITABLE.has(item.type))].map((item) => [`${item.type}:${item.id}`, item])).values()]);
      setNextCursor(data.has_more ? data.next_cursor ?? null : null);
    } catch (cause) {
      if (!controller.signal.aborted && generation === searchGeneration.current) setError(apiErrorMessage(cause, t("references.searchFailed")));
    } finally {
      controllers.current.delete(controller);
      if (!controller.signal.aborted && generation === searchGeneration.current) setLoading(false);
    }
  }

  const citedNumbers = useMemo(() => new Map(references.filter((item) => item.target_id).map((item) => [referenceKey(item), item.number])), [references]);
  const options = useMemo(() => {
    if (!trimmed) {
      return references.map(optionFromReference)
        .filter((item): item is PickerOption => item !== null && (scope === "all" || item.target_kind === scope));
    }
    const found = new Map<string, PickerOption>();
    if (addressMatch) found.set(addressMatch.key, addressMatch);
    for (const result of results) if (!found.has(`${result.type}:${result.id}`)) found.set(`${result.type}:${result.id}`, optionFromResult(result));
    return [...found.values()];
  }, [addressMatch, references, results, scope, trimmed]);
  const selected = options.find((item) => item.key === selectedKey) ?? null;
  const highlightQuery = address ? "" : trimmed;

  function choose(action: ReferenceAction, option = selected) {
    if (!option || busy) return;
    onSelect({
      target_kind: option.target_kind, target_id: option.target_id, title: option.title,
      location: option.location.join(" / ") || null, note: note.trim() || null,
    }, action);
  }

  function onQueryKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (!options.length) return;
      event.preventDefault();
      const index = options.findIndex((item) => item.key === selectedKey);
      const step = event.key === "ArrowDown" ? 1 : -1;
      const next = options[index < 0 ? (step > 0 ? 0 : options.length - 1) : (index + step + options.length) % options.length];
      setSelectedKey(next.key);
      document.getElementById(`${id}-option-${next.key}`)?.scrollIntoView({ block: "nearest" });
    } else if (event.key === "Enter" && selected) {
      event.preventDefault();
      choose(mode);
    }
  }

  const kinds: Array<[SearchScope, string]> = [
    ["all", t("references.allKinds")],
    ["context_page", t("references.kinds.context")],
    ["library_item", t("references.kinds.library")],
    ["agent_page", t("references.kinds.agent")],
  ];
  const actionButtons = (
    <>
      <button className={`icon-button text-button${mode === "metadata" ? " primary" : ""}`} disabled={busy || !selected} onClick={() => choose("metadata")} type="button">{t("references.addOnly")}</button>
      <button className={`icon-button text-button${mode === "inline" ? " primary" : ""}`} disabled={busy || !selected} onClick={() => choose("inline")} type="button"><Quote aria-hidden="true" size={15} />{t("references.insert")}</button>
    </>
  );

  return (
    <Dialog className="reference-picker-dialog" labelledBy={`${id}-title`} onClose={onClose} dismissible={!busy}
      header={<><h2 id={`${id}-title`}><BookOpen aria-hidden="true" size={18} />{t("references.choose")}</h2><DialogCloseButton disabled={busy} label={t("references.close")} onClick={onClose} /></>}
      footer={
        <div className="reference-picker-confirm">
          {selected ? (
            <span className="reference-picker-selection">
              <span className="search-result-kind" data-identity={kindLabel(selected.target_kind)}>{t(`references.kinds.${kindLabel(selected.target_kind)}`)}</span>
              <strong title={selected.title}>{selected.title}</strong>
            </span>
          ) : <span className="reference-picker-selection is-empty">{t("references.selectPrompt")}</span>}
          <input aria-label={t("references.note")} className="reference-picker-note" disabled={busy} maxLength={2000} onChange={(event) => setNote(event.target.value)} placeholder={t("references.note")} value={note} />
          <span className="reference-picker-buttons">{actionButtons}</span>
        </div>
      }>
      <div className="reference-picker">
        <div aria-busy={loading || undefined} className="search-field reference-picker-search">
          <Search aria-hidden="true" size={15} />
          <input
            aria-activedescendant={selected ? `${id}-option-${selected.key}` : undefined}
            aria-autocomplete="list"
            aria-controls={`${id}-results`}
            aria-expanded={options.length > 0}
            aria-label={t("references.search")}
            autoComplete="off"
            disabled={busy}
            id={`${id}-query`}
            onChange={(event) => { setQuery(event.target.value); setSelectedKey(null); }}
            onKeyDown={onQueryKeyDown}
            placeholder={t("references.searchPlaceholder")}
            ref={queryRef}
            role="combobox"
            type="search"
            value={query}
          />
        </div>
        <p className="reference-picker-hint">{t("references.searchHint")}</p>
        <div aria-label={t("references.kind")} className="reference-picker-kinds" role="group">
          {kinds.map(([value, label]) => (
            <button aria-pressed={scope === value} className="reference-picker-kind" disabled={busy} key={value} onClick={() => setScope(value)} type="button">{label}</button>
          ))}
        </div>
        {!trimmed && options.length ? <h3 className="reference-picker-section">{t("references.currentReferences")}</h3> : null}
        {loading ? <p className="reference-picker-status" role="status">{t("references.searching")}</p> : null}
        {error ? <p className="reference-picker-status" role="alert">{error}</p> : null}
        {!loading && trimmed && options.length === 0 && !error ? <p className="reference-picker-status">{t("references.noResults")}</p> : null}
        <ul aria-label={t("references.results")} className="reference-picker-results" id={`${id}-results`} role="listbox">
          {options.map((option) => {
            const number = citedNumbers.get(referenceKey(option));
            const isSelected = option.key === selectedKey;
            const parts = presentLocation(option.location);
            return (
              // Keyboard selection happens in the combobox input above.
              // eslint-disable-next-line jsx-a11y/click-events-have-key-events
              <li
                aria-selected={isSelected}
                className={`search-result-row reference-picker-option${isSelected ? " is-selected" : ""}${number ? " is-cited" : ""}`}
                id={`${id}-option-${option.key}`}
                key={option.key}
                onClick={() => setSelectedKey(option.key)}
                onDoubleClick={() => choose(mode, option)}
                role="option"
              >
                <div className="search-result-body">
                  <p className="search-result-title">
                    <span className="search-result-kind" data-identity={kindLabel(option.target_kind)}>{t(`references.kinds.${kindLabel(option.target_kind)}`)}</span>
                    <span className="search-result-title-text">{highlight(option.title, highlightQuery)}</span>
                    {option === addressMatch ? <span className="reference-picker-address">{t("references.addressMatch")}</span> : null}
                  </p>
                  {option.snippet ? (
                    <p className="search-result-snippet">{option.heading ? <><strong>{option.heading}</strong>{" · "}</> : null}{highlight(option.snippet, highlightQuery)}</p>
                  ) : null}
                  {parts.length ? (
                    <p className="search-result-location">
                      <Folder aria-hidden="true" size={13} />
                      <span className="search-result-location-parts">
                        {parts.map((part, index) => (
                          <span className="search-result-location-part" data-desktop-visible={part.desktop} data-mobile-visible={part.mobile} key={index}>
                            {index > 0 ? <ChevronRight aria-hidden="true" size={12} /> : null}
                            <span>{part.text}</span>
                          </span>
                        ))}
                      </span>
                    </p>
                  ) : null}
                </div>
                {number ? <span className="reference-picker-cited"><Check aria-hidden="true" size={14} />{t("references.alreadyCited", { number })}</span> : <span />}
              </li>
            );
          })}
        </ul>
        {nextCursor && trimmed ? <button className="icon-button text-button reference-picker-more" disabled={busy || loading} onClick={() => { void loadMore(); }} type="button">{t("references.loadMore")}</button> : null}
      </div>
    </Dialog>
  );
}

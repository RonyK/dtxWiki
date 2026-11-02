import { useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { searchWorkspace, type RankedSearchResult, type SearchScope } from "../../shared/api/operations";
import type { DocumentReferenceKind, DocumentReferenceTarget, ReferenceView } from "../../shared/api/references";
import { apiErrorMessage } from "../../shared/api/client";
import { Dialog, DialogCloseButton } from "../../shared/components/Dialog";
import { kindLabel, referenceKey } from "./documentReferences";

export function ReferenceEditor({ references, disabled, onAdd, onInsert, onRemove, onConvertLink, onLocate, checkingLocators, onRetry, onReconcile, retryDisabled }: {
  references: ReferenceView[];
  disabled: boolean;
  onAdd: () => void;
  onInsert: (reference?: ReferenceView) => void;
  onRemove: (reference: ReferenceView) => void;
  onConvertLink: () => void;
  onLocate: (offset: number) => void;
  checkingLocators?: ReadonlySet<string>;
  onRetry?: () => void;
  onReconcile?: () => void;
  retryDisabled?: boolean;
}) {
  const { t } = useTranslation("pages");
  return (
    <section className="editor-references" aria-label={t("references.title")}>
      <div className="editor-references-header">
        <h2>{t("references.title")}</h2>
        <button className="icon-button text-button" disabled={disabled} onClick={onAdd} type="button">{t("references.add")}</button>
        <button className="icon-button text-button" disabled={disabled} onClick={() => onInsert()} type="button">{t("references.insert")}</button>
        <button className="icon-button text-button" disabled={disabled} onClick={onConvertLink} type="button">{t("references.convertLink")}</button>
        {onRetry ? <button className="icon-button text-button" disabled={retryDisabled} onClick={onRetry} type="button">{t("references.retry")}</button> : null}
        {onReconcile ? <button className="icon-button text-button" disabled={retryDisabled} onClick={onReconcile} type="button">{t("references.reconcile")}</button> : null}
      </div>
      {onRetry ? <p role="status">{t("references.ambiguous")}</p> : null}
      {references.length ? <ol>{references.map((reference) => (
        <li key={referenceKey(reference)} value={reference.number}>
          <span>{reference.state === "unavailable" ? t(reference.occurrences?.some((item) => item.href && checkingLocators?.has(item.href)) ? "checkingReference.label" : "references.unavailable") : reference.title}</span>
          <span className="reference-kind">{t(`references.kinds.${kindLabel(reference.target_kind)}`)}</span>
          {reference.state === "deleted" ? <span>{t("references.deleted")}</span> : null}
          {reference.state !== "unavailable" && reference.location_warning ? <span>{t("references.fragmentUnavailable")}</span> : null}
          {reference.state !== "unavailable" && reference.note ? <span>{reference.note}</span> : null}
          {reference.inline_count === 0 ? <span>{t("references.notCited")}</span> : (
            <button aria-label={t("references.itemAction", { action: t("references.inlineCount", { count: reference.inline_count }), number: reference.number, title: reference.state === "unavailable" ? t("references.unavailable") : reference.title })} className="icon-button text-button" disabled={!reference.occurrences?.length} onClick={() => onLocate(reference.occurrences![0].start)} type="button">
              {t("references.inlineCount", { count: reference.inline_count })}
            </button>
          )}
          <button aria-label={t("references.itemAction", { action: t("references.insert"), number: reference.number, title: reference.state === "unavailable" ? t("references.unavailable") : reference.title })} className="icon-button text-button" disabled={disabled || !reference.href || !reference.target_id} onClick={() => onInsert(reference)} type="button">{t("references.insert")}</button>
          {reference.metadata_present || reference.metadata_id ? <button aria-label={t("references.itemAction", { action: t("references.remove"), number: reference.number, title: reference.state === "unavailable" ? t("references.unavailable") : reference.title })} className="icon-button text-button" disabled={disabled} onClick={() => onRemove(reference)} type="button">{t("references.remove")}</button> : null}
        </li>
      ))}</ol> : <p>{t("references.empty")}</p>}
    </section>
  );
}

export type ReferenceChoice = DocumentReferenceTarget & { title: string; location: string | null };

export function ReferencePicker({ workspaceId, onClose, onSelect, busy = false, showNote = true, initialQuery = "" }: {
  workspaceId: string;
  onClose: () => void;
  onSelect: (choice: ReferenceChoice) => void;
  busy?: boolean;
  showNote?: boolean;
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
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  useEffect(() => { queryRef.current?.focus(); }, []);
  useEffect(() => {
    const generation = ++searchGeneration.current;
    const controller = new AbortController();
    const pendingControllers = controllers.current;
    pendingControllers.add(controller);
    setNextCursor(null);
    if (!query.trim()) { setResults([]); setLoading(false); return () => { controller.abort(); pendingControllers.delete(controller); }; }
    setLoading(true);
    setError(null);
    const timer = window.setTimeout(() => {
      void searchWorkspace(workspaceId, { query, scope, limit: 30 }, { signal: controller.signal }).then((data) => {
        if (!controller.signal.aborted && generation === searchGeneration.current) {
          setResults(data.ranked_results.filter((item) => ["context_page", "library_item", "agent_page"].includes(item.type)));
          setNextCursor(data.has_more ? data.next_cursor ?? null : null);
        }
      }).catch((cause: unknown) => {
        if (!controller.signal.aborted) setError(apiErrorMessage(cause, t("references.searchFailed")));
      }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    }, 250);
    return () => { window.clearTimeout(timer); pendingControllers.forEach((item) => item.abort()); pendingControllers.clear(); };
  }, [query, scope, workspaceId, t]);
  async function loadMore() {
    if (!nextCursor || loading) return;
    const generation = searchGeneration.current;
    const controller = new AbortController();
    controllers.current.add(controller);
    setLoading(true);
    try {
      const data = await searchWorkspace(workspaceId, { query, scope, limit: 30, cursor: nextCursor }, { signal: controller.signal });
      if (controller.signal.aborted || generation !== searchGeneration.current) return;
      setResults((previous) => [...new Map([...previous, ...data.ranked_results.filter((item) => ["context_page", "library_item", "agent_page"].includes(item.type))].map((item) => [`${item.type}:${item.id}`, item])).values()]);
      setNextCursor(data.has_more ? data.next_cursor ?? null : null);
    } catch (cause) {
      if (!controller.signal.aborted && generation === searchGeneration.current) setError(apiErrorMessage(cause, t("references.searchFailed")));
    } finally {
      controllers.current.delete(controller);
      if (!controller.signal.aborted && generation === searchGeneration.current) setLoading(false);
    }
  }
  return (
    <Dialog labelledBy={`${id}-title`} onClose={onClose} dismissible={!busy}
      header={<><h2 id={`${id}-title`}>{t("references.choose")}</h2><DialogCloseButton disabled={busy} label={t("references.close")} onClick={onClose} /></>}>
      <div className="reference-picker">
        <label htmlFor={`${id}-query`}>{t("references.search")}</label>
        <input disabled={busy} id={`${id}-query`} ref={queryRef} onChange={(event) => setQuery(event.target.value)} value={query} />
        <label htmlFor={`${id}-kind`}>{t("references.kind")}</label>
        <select disabled={busy} id={`${id}-kind`} onChange={(event) => setScope(event.target.value as SearchScope)} value={scope}>
          <option value="all">{t("references.allKinds")}</option>
          <option value="context_page">{t("references.kinds.context")}</option>
          <option value="library_item">{t("references.kinds.library")}</option>
          <option value="agent_page">{t("references.kinds.agent")}</option>
        </select>
        {showNote ? <><label htmlFor={`${id}-note`}>{t("references.note")}</label>
          <input disabled={busy} id={`${id}-note`} maxLength={1000} onChange={(event) => setNote(event.target.value)} value={note} /></> : null}
        {loading ? <p role="status">{t("references.searching")}</p> : null}
        {error ? <p role="alert">{error}</p> : null}
        {!loading && query.trim() && results.length === 0 ? <p>{t("references.noResults")}</p> : null}
        <ul>{results.map((result) => (
          <li key={`${result.type}:${result.id}`}>
            <button className="icon-button text-button" disabled={busy} onClick={() => onSelect({
              target_kind: result.type as DocumentReferenceKind, target_id: result.id,
              title: result.title, location: result.location?.join(" / ") ?? null, note: showNote ? note || null : null,
            })} type="button">
              <strong>{result.title}</strong><span>{t(`references.kinds.${kindLabel(result.type as DocumentReferenceKind)}`)}</span>
              {result.location?.length ? <small>{result.location.join(" / ")}</small> : null}
            </button>
          </li>
        ))}</ul>
        {nextCursor ? <button className="icon-button text-button" disabled={busy || loading} onClick={() => { void loadMore(); }} type="button">{t("references.loadMore")}</button> : null}
      </div>
    </Dialog>
  );
}

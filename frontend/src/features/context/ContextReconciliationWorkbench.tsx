import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { MarkdownView } from "../pages/MarkdownView";
import { ApiError, apiErrorMessage } from "../../shared/api/client";
import {
  getContextReconciliation,
  reconcileContextBranch,
  type ContextReconciliationCommit,
  type ContextReconciliationConflict,
  type ContextReconciliationDetail,
  type ContextReconciliationResolution,
  type ContextReconciliationResolutionChoice,
  type ContextReconciliationResult,
} from "../../shared/api/contextReconciliation";
import { discardAlternativeDraft } from "../../shared/api/pages";
import { Dialog, DialogCloseButton } from "../../shared/components/Dialog";
import { RelativeTime } from "../../shared/components/RelativeTime";
import { StatusBanner } from "../../shared/components/StatusBanner";
import { createUuidV4 } from "../../shared/lib/uuidV4";

type ResolutionDraft = {
  choice: ContextReconciliationResolutionChoice;
  content: string;
};

export function ContextReconciliationWorkbench({
  branchVersionId,
  pageId,
  workspaceId,
  onClose,
  onCompleted,
}: {
  branchVersionId: string;
  pageId: string;
  workspaceId: string;
  onClose: () => void;
  // Awaited: the host reloads the page and rewrites the URL in response, and
  // a rejection there must surface here rather than becoming an unhandled
  // promise the user never sees.
  onCompleted: (
    result: ContextReconciliationCommit | "discarded",
  ) => Promise<void> | void;
}) {
  const { t } = useTranslation("context");
  const titleId = useId();
  const progressId = useId();
  const blockButtonRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const intentKeyRef = useRef<string | null>(null);
  const loadGenerationRef = useRef(0);
  const loadControllerRef = useRef<AbortController | null>(null);
  const [detail, setDetail] = useState<ContextReconciliationDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedBlockId, setSelectedBlockId] = useState<string | null>(null);
  const [resolutions, setResolutions] = useState<Record<string, ResolutionDraft>>(
    {},
  );
  const [excludedCleanChangeIds, setExcludedCleanChangeIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [reason, setReason] = useState("");
  const [preview, setPreview] = useState<string | null>(null);
  const [busy, setBusy] = useState<"preview" | "submit" | "discard" | null>(
    null,
  );
  const [staleHeadVersionId, setStaleHeadVersionId] = useState<string | null>(
    null,
  );
  const [discardOpen, setDiscardOpen] = useState(false);
  const [discardReason, setDiscardReason] = useState("");

  const loadDetail = useCallback(
    async (mode: "replace" | "probe") => {
      const generation = ++loadGenerationRef.current;
      if (mode === "replace") {
        setLoading(true);
        setError(null);
      }
      loadControllerRef.current?.abort();
      const controller = new AbortController();
      loadControllerRef.current = controller;
      try {
        const next = await getContextReconciliation(workspaceId, pageId, branchVersionId, {
          signal: controller.signal,
        });
        if (generation !== loadGenerationRef.current) {
          return;
        }
        if (mode === "probe" && detail) {
          if (next.head.version_id !== detail.head.version_id) {
            setStaleHeadVersionId(next.head.version_id);
          }
          // A focus probe is only a fencing check. Replacing the snapshot when
          // HEAD is unchanged would also clear the editor's local choices.
          return;
        }
        setDetail(next);
        setStaleHeadVersionId(null);
        setPreview(null);
        setResolutions({});
        setExcludedCleanChangeIds(new Set());
        setSelectedBlockId(
          next.conflicts[0]?.block_id ?? next.blocks[0]?.block_id ?? null,
        );
      } catch (exc) {
        if (generation !== loadGenerationRef.current) {
          return;
        }
        if (exc instanceof DOMException && exc.name === "AbortError") {
          return;
        }
        if (mode === "replace") {
          setError(
            apiErrorMessage(
              exc,
              t("reconciliation.loadError", {
                defaultValue: "Could not load reconciliation details.",
              }),
            ),
          );
        }
      } finally {
        if (generation === loadGenerationRef.current && mode === "replace") {
          setLoading(false);
        }
        if (loadControllerRef.current === controller) {
          loadControllerRef.current = null;
        }
      }
    },
    [branchVersionId, detail, pageId, workspaceId, t],
  );

  useEffect(() => {
    void loadDetail("replace");
    return () => {
      loadGenerationRef.current += 1;
      loadControllerRef.current?.abort();
    };
    // Loading is keyed strictly to the route-owned IDs. `loadDetail` also reads
    // the current snapshot for probes, so including it here would reload after
    // every successful response.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [branchVersionId, pageId]);

  useEffect(() => {
    function probeOnFocus() {
      if (document.visibilityState === "visible" && detail && !busy) {
        void loadDetail("probe");
      }
    }
    window.addEventListener("focus", probeOnFocus);
    return () => window.removeEventListener("focus", probeOnFocus);
  }, [busy, detail, loadDetail]);

  const selectedConflict = useMemo(
    () =>
      detail?.conflicts.find((conflict) => conflict.block_id === selectedBlockId)
      ?? null,
    [detail, selectedBlockId],
  );
  const selectedBlock = useMemo(
    () => detail?.blocks.find((block) => block.block_id === selectedBlockId) ?? null,
    [detail, selectedBlockId],
  );
  const branchOnlyCleanChanges = useMemo(
    () => detail?.clean_changes.filter((change) => change.state === "branch_only") ?? [],
    [detail],
  );
  const resolvedCount = detail
    ? detail.conflicts.filter((conflict) =>
        resolutionIsComplete(resolutions[conflict.block_id]),
      ).length
    : 0;
  const allResolved = Boolean(
    detail
      && detail.conflicts.every((conflict) =>
        resolutionIsComplete(resolutions[conflict.block_id]),
      ),
  );
  const effectiveReason =
    reason.trim() ||
    (detail?.conflicts.length === 0
      ? t("reconciliation.zeroConflictReason", {
          defaultValue: "Merge branch after verifying that no conflicts remain.",
        })
      : "");
  const canMutate = Boolean(
    detail
      && !staleHeadVersionId
      && allResolved
      && effectiveReason.length > 0
      && !busy,
  );
  const editingDisabled = Boolean(busy) || Boolean(staleHeadVersionId);

  useEffect(() => {
    intentKeyRef.current = null;
    setPreview(null);
  }, [detail?.head.version_id, excludedCleanChangeIds, reason, resolutions]);

  function selectResolution(
    conflict: ContextReconciliationConflict,
    choice: ContextReconciliationResolutionChoice,
  ) {
    if (choice === "restore_yours" && !conflict.restore_yours_available) {
      return;
    }
    setResolutions((current) => ({
      ...current,
      [conflict.block_id]: {
        choice,
        content:
          choice === "custom"
            ? (current[conflict.block_id]?.content ?? conflict.yours?.text ?? "")
            : "",
      },
    }));
  }

  function requestResolutions(): ContextReconciliationResolution[] {
    if (!detail) {
      return [];
    }
    return detail.conflicts.map((conflict) => {
      const resolution = resolutions[conflict.block_id];
      return {
        block_id: conflict.block_id,
        choice: resolution.choice,
        expected_hash: conflict.head.hash,
        content: resolution.choice === "custom" ? resolution.content : null,
      };
    });
  }

  function setCleanChangeIncluded(blockId: string, included: boolean) {
    setExcludedCleanChangeIds((current) => {
      const next = new Set(current);
      if (included) {
        next.delete(blockId);
      } else {
        next.add(blockId);
      }
      return next;
    });
  }

  function currentIntentKey() {
    if (!intentKeyRef.current) {
      intentKeyRef.current = createUuidV4();
    }
    return intentKeyRef.current;
  }

  async function execute(dryRun: boolean) {
    if (!detail || !canMutate) {
      return;
    }
    setError(null);
    setBusy(dryRun ? "preview" : "submit");
    try {
      const result: ContextReconciliationResult = await reconcileContextBranch(
        workspaceId,
        pageId,
        branchVersionId,
        {
          expected_head_version_id: detail.head.version_id,
          reconciles: [branchVersionId],
          resolutions: requestResolutions(),
          // Only server-projected branch-only clean-change IDs are eligible.
          // Filtering again here keeps stale or synthetic client IDs off the
          // wire; the backend also validates this as a fail-closed subset.
          excluded_clean_change_ids: branchOnlyCleanChanges
            .filter((change) => excludedCleanChangeIds.has(change.block_id))
            .map((change) => change.block_id),
          reason: effectiveReason,
          // dry_run is not part of the write fingerprint. Preview and commit
          // must therefore share one intent key so generated block IDs in the
          // predicted map are the IDs the commit will use. Edits reset it;
          // transport retries retain it.
          idempotency_key: currentIntentKey(),
          dry_run: dryRun,
        },
      );
      if (dryRun && result.dry_run) {
        setPreview(result.preview.merged_body_markdown);
      } else if (!dryRun && !result.dry_run) {
        await onCompleted(result.commit);
      } else {
        throw new Error("reconciliation_response_mismatch");
      }
    } catch (exc) {
      if (isHeadMismatch(exc)) {
        setStaleHeadVersionId(
          exc instanceof ApiError
            ? (exc.body.server_version_id ?? "advanced")
            : "advanced",
        );
      }
      setError(
        apiErrorMessage(
          exc,
          t("reconciliation.submitError", {
            defaultValue: "Could not reconcile this branch.",
          }),
        ),
      );
    } finally {
      setBusy(null);
    }
  }

  async function discardBranch() {
    if (!detail || busy || discardReason.trim().length === 0) {
      return;
    }
    setBusy("discard");
    setError(null);
    try {
      await discardAlternativeDraft(workspaceId, pageId, branchVersionId, {
        reason: discardReason.trim(),
      });
      await onCompleted("discarded");
    } catch (exc) {
      setError(
        apiErrorMessage(
          exc,
          t("reconciliation.discardError", {
            defaultValue: "Could not discard this branch.",
          }),
        ),
      );
    } finally {
      setBusy(null);
    }
  }

  function handleBlockKeyDown(
    event: React.KeyboardEvent<HTMLButtonElement>,
    index: number,
  ) {
    if (
      !detail
      || ![
        "ArrowLeft",
        "ArrowRight",
        "ArrowUp",
        "ArrowDown",
        "Home",
        "End",
      ].includes(event.key)
    ) {
      return;
    }
    event.preventDefault();
    const last = detail.blocks.length - 1;
    const nextIndex =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? last
          : event.key === "ArrowLeft" || event.key === "ArrowUp"
            ? (index - 1 + detail.blocks.length) % detail.blocks.length
            : (index + 1) % detail.blocks.length;
    const block = detail.blocks[nextIndex];
    setSelectedBlockId(block.block_id);
    blockButtonRefs.current[nextIndex]?.focus();
  }

  return (
    <Dialog
      className="context-reconciliation-dialog"
      dismissible={!busy}
      labelledBy={titleId}
      onClose={onClose}
      footer={
        detail ? (
          <div className="context-reconciliation-footer">
            <span aria-live="polite" id={progressId}>
              {t("reconciliation.progress", {
                defaultValue: "{{resolved}} of {{total}} conflicts resolved",
                resolved: resolvedCount,
                total: detail.conflicts.length,
              })}
            </span>
            <div className="dialog-actions">
              <button
                className="icon-button text-button"
                disabled={Boolean(busy)}
                onClick={onClose}
                type="button"
              >
                {t("reconciliation.close", { defaultValue: "Close" })}
              </button>
              <button
                className="icon-button"
                disabled={!canMutate}
                onClick={() => void execute(true)}
                type="button"
              >
                {busy === "preview"
                  ? t("reconciliation.previewing", { defaultValue: "Previewing" })
                  : t("reconciliation.preview", { defaultValue: "Preview merged draft" })}
              </button>
              <button
                className="icon-button primary"
                disabled={!canMutate}
                onClick={() => void execute(false)}
                type="button"
              >
                {busy === "submit"
                  ? t("reconciliation.submitting", { defaultValue: "Creating draft" })
                  : t("reconciliation.submit", { defaultValue: "Create reconciled draft" })}
              </button>
            </div>
          </div>
        ) : null
      }
    >
      <header className="context-reconciliation-header">
        <div>
          <p className="eyebrow">
            {t("reconciliation.eyebrow", { defaultValue: "Branch reconciliation" })}
          </p>
          <h2 id={titleId}>
            {detail?.page.title
              ?? t("reconciliation.title", { defaultValue: "Reconcile branch" })}
          </h2>
        </div>
        <DialogCloseButton
          disabled={Boolean(busy)}
          label={t("reconciliation.close", { defaultValue: "Close" })}
          onClick={onClose}
        />
      </header>

      {loading ? (
        <div className="wiki-empty">
          {t("reconciliation.loading", { defaultValue: "Loading reconciliation" })}
        </div>
      ) : null}
      {error ? <StatusBanner tone="warning">{error}</StatusBanner> : null}
      {staleHeadVersionId ? (
        <StatusBanner
          tone="warning"
          action={
            <button
              className="icon-button"
              disabled={Boolean(busy)}
              onClick={() => void loadDetail("replace")}
              type="button"
            >
              {t("reconciliation.refresh", { defaultValue: "Refresh conflicts" })}
            </button>
          }
        >
          {t("reconciliation.headAdvanced", {
            defaultValue:
              "The page HEAD advanced. Refresh before choosing resolutions; selections were not reapplied automatically.",
          })}
        </StatusBanner>
      ) : null}

      {detail ? (
        <div className="context-reconciliation-body">
          <div className="context-reconciliation-summary">
            <span>
              {t("reconciliation.baseToHead", {
                defaultValue: "Base v{{base}} → HEAD v{{head}}",
                base: detail.base.version_no,
                head: detail.head.version_no,
              })}
            </span>
            <span>
              {t("reconciliation.branchVersion", {
                defaultValue: "Branch v{{version}}",
                version: detail.branch.version_no,
              })}
            </span>
            <span
              className="context-reconciliation-actor"
              data-actor-kind={detail.branch.actor_kind}
            >
              {detail.branch.actor_display_name
                ? t("reconciliation.branchActorWithName", {
                    defaultValue: "Branch author: {{kind}} · {{name}}",
                    kind: detail.branch.actor_kind,
                    name: detail.branch.actor_display_name,
                  })
                : t("reconciliation.branchActorKind", {
                    defaultValue: "Branch author: {{kind}}",
                    kind: detail.branch.actor_kind,
                  })}
            </span>
            {detail.head_advance_count > 0 ? (
              <span>
                {t("reconciliation.headAdvanceCount", {
                  defaultValue: "HEAD advanced {{count}} times",
                  count: detail.head_advance_count,
                })}
              </span>
            ) : null}
          </div>

          <div className="context-reconciliation-workspace">
            <div className="context-reconciliation-rail">
              <ol
                aria-label={t("reconciliation.blockStrip", {
                  defaultValue: "Reconciliation blocks",
                })}
                className="context-reconciliation-block-strip"
              >
                {detail.blocks.map((block, index) => (
                  <li
                    className="context-reconciliation-block-item"
                    key={block.block_id}
                  >
                    <button
                      aria-current={
                        selectedBlockId === block.block_id ? "true" : undefined
                      }
                      aria-describedby={progressId}
                      aria-label={`${blockDisplayLabel(block, t)}, ${blockStateLabel(block.state, t)}`}
                      className="context-reconciliation-block"
                      data-state={block.state}
                      onClick={() => setSelectedBlockId(block.block_id)}
                      onKeyDown={(event) => handleBlockKeyDown(event, index)}
                      ref={(element) => {
                        blockButtonRefs.current[index] = element;
                      }}
                      type="button"
                    >
                      <span>{blockDisplayLabel(block, t)}</span>
                      <span>{blockStateLabel(block.state, t)}</span>
                    </button>
                  </li>
                ))}
              </ol>
            </div>

            <div className="context-reconciliation-selection">
              {selectedConflict ? (
                <section
                  aria-labelledby={`${titleId}-selected-conflict`}
                  className="context-reconciliation-conflict"
                >
              <h3 id={`${titleId}-selected-conflict`}>
                {selectedBlock ? blockDisplayLabel(selectedBlock, t) : selectedConflict.kind}
              </h3>
              <div className="context-reconciliation-comparison">
                <ReconciliationSide
                  effectHeading={t("reconciliation.headGapEffect", {
                    defaultValue: "Current blocks in this insertion gap",
                  })}
                  effects={selectedConflict.head_effects}
                  heading={t("reconciliation.head", { defaultValue: "Current HEAD" })}
                  side={selectedConflict.head}
                />
                <ReconciliationSide
                  effectHeading={t("reconciliation.branchInsertEffect", {
                    defaultValue: "Complete branch insertion",
                  })}
                  effects={selectedConflict.yours_effects}
                  heading={t("reconciliation.yours", { defaultValue: "Your branch" })}
                  side={selectedConflict.yours}
                />
              </div>
              <details className="context-reconciliation-base">
                <summary>
                  {t("reconciliation.base", { defaultValue: "Show common base" })}
                </summary>
                <ReconciliationSide side={selectedConflict.base} />
              </details>
              <fieldset className="context-reconciliation-choices">
                <legend>
                  {t("reconciliation.choose", { defaultValue: "Choose a resolution" })}
                </legend>
                <ResolutionRadio
                  checked={resolutions[selectedConflict.block_id]?.choice === "keep_head"}
                  disabled={editingDisabled}
                  label={t("reconciliation.keepHead", { defaultValue: "Keep HEAD" })}
                  name={`${titleId}-${selectedConflict.block_id}-resolution`}
                  onChange={() => selectResolution(selectedConflict, "keep_head")}
                />
                <ResolutionRadio
                  checked={resolutions[selectedConflict.block_id]?.choice === "restore_yours"}
                  disabled={
                    editingDisabled || !selectedConflict.restore_yours_available
                  }
                  label={t("reconciliation.restoreYours", {
                    defaultValue: "Restore your branch",
                  })}
                  name={`${titleId}-${selectedConflict.block_id}-resolution`}
                  onChange={() => selectResolution(selectedConflict, "restore_yours")}
                />
                <ResolutionRadio
                  checked={resolutions[selectedConflict.block_id]?.choice === "custom"}
                  disabled={editingDisabled}
                  label={t("reconciliation.custom", { defaultValue: "Write a new block" })}
                  name={`${titleId}-${selectedConflict.block_id}-resolution`}
                  onChange={() => selectResolution(selectedConflict, "custom")}
                />
                {resolutions[selectedConflict.block_id]?.choice === "custom" ? (
                  <div className="context-reconciliation-custom">
                    <span id={`${titleId}-custom-editor-label`}>
                      {t("reconciliation.customLabel", { defaultValue: "Block Markdown" })}
                    </span>
                    <span className="context-reconciliation-custom-prefill">
                      <button
                        className="icon-button text-button"
                        disabled={editingDisabled || selectedConflict.head.text == null}
                        onClick={() =>
                          setResolutions((current) => ({
                            ...current,
                            [selectedConflict.block_id]: {
                              choice: "custom",
                              content: selectedConflict.head.text ?? "",
                            },
                          }))
                        }
                        type="button"
                      >
                        {t("reconciliation.startFromHead", {
                          defaultValue: "Start from HEAD",
                        })}
                      </button>
                      <button
                        className="icon-button text-button"
                        disabled={editingDisabled || selectedConflict.yours.text == null}
                        onClick={() =>
                          setResolutions((current) => ({
                            ...current,
                            [selectedConflict.block_id]: {
                              choice: "custom",
                              content: selectedConflict.yours.text ?? "",
                            },
                          }))
                        }
                        type="button"
                      >
                        {t("reconciliation.startFromYours", {
                          defaultValue: "Start from your branch",
                        })}
                      </button>
                    </span>
                    <textarea
                      aria-labelledby={`${titleId}-custom-editor-label`}
                      autoFocus
                      disabled={editingDisabled}
                      value={resolutions[selectedConflict.block_id]?.content ?? ""}
                      onChange={(event) =>
                        setResolutions((current) => ({
                          ...current,
                          [selectedConflict.block_id]: {
                            choice: "custom",
                            content: event.currentTarget.value,
                          },
                        }))
                      }
                    />
                  </div>
                ) : null}
                {selectedConflict.identity_retired ? (
                  <p className="form-hint">
                    {t("reconciliation.retiredIdentity", {
                      defaultValue:
                        "This block identity was retired. A restored or custom choice will create a new block operation.",
                    })}
                  </p>
                ) : null}
              </fieldset>
                </section>
              ) : selectedBlock ? (
                <section className="context-reconciliation-clean-change">
                  <h3>{blockDisplayLabel(selectedBlock, t)}</h3>
                  <p>{blockStateDescription(selectedBlock.state, t)}</p>
                </section>
              ) : (
                <div className="wiki-empty">
                  {t("reconciliation.noBlocks", {
                    defaultValue: "No blocks to reconcile.",
                  })}
                </div>
              )}
            </div>
          </div>

          {branchOnlyCleanChanges.length > 0 ? (
            <section
              aria-labelledby={`${titleId}-clean-changes`}
              className="context-reconciliation-clean-changes"
            >
              <h3 id={`${titleId}-clean-changes`}>
                {t("reconciliation.cleanChangesTitle", {
                  defaultValue: "Branch-only changes",
                })}
              </h3>
              <p className="form-hint">
                {t("reconciliation.cleanChangesHint", {
                  defaultValue:
                    "These changes are included by default. Clear a change to leave it out of the reconciled draft.",
                })}
              </p>
              <ul className="context-reconciliation-clean-change-list">
                {branchOnlyCleanChanges.map((change) => {
                  const block = detail.blocks.find(
                    (candidate) => candidate.block_id === change.block_id,
                  );
                  const label = block ? blockDisplayLabel(block, t) : change.kind;
                  return (
                    <li key={change.block_id}>
                      <label className="context-reconciliation-clean-change-toggle">
                        <input
                          checked={!excludedCleanChangeIds.has(change.block_id)}
                          disabled={Boolean(busy) || Boolean(staleHeadVersionId)}
                          onChange={(event) =>
                            setCleanChangeIncluded(
                              change.block_id,
                              event.currentTarget.checked,
                            )
                          }
                          type="checkbox"
                        />
                        <span>
                          {t("reconciliation.includeCleanChange", {
                            defaultValue: "Include {{label}}",
                            label,
                          })}
                        </span>
                      </label>
                      {change.yours?.text ? <pre>{change.yours.text}</pre> : null}
                    </li>
                  );
                })}
              </ul>
            </section>
          ) : null}

          <label className="context-reconciliation-reason">
            <span>
              {t("reconciliation.reason", { defaultValue: "Reconciliation reason" })}
            </span>
            <textarea
              disabled={editingDisabled}
              required={detail.conflicts.length > 0}
              value={reason}
              onChange={(event) => setReason(event.currentTarget.value)}
              placeholder={t("reconciliation.reasonPlaceholder", {
                defaultValue: "Explain why these resolutions preserve the intended change.",
              })}
            />
          </label>

          {preview !== null ? (
            <section className="context-reconciliation-preview">
              <h3>
                {t("reconciliation.previewTitle", { defaultValue: "Server preview" })}
              </h3>
              <p className="form-hint">
                {t("reconciliation.previewHint", {
                  defaultValue:
                    "This preview came from the same server validation path used to create the draft.",
                })}
              </p>
              <MarkdownView body={preview} renderPolicy="untrusted-preview" />
            </section>
          ) : null}

          <section className="context-reconciliation-discard">
            <h3>
              {t("reconciliation.discardTitle", { defaultValue: "Discard branch" })}
            </h3>
            {discardOpen ? (
              <>
                <label>
                  <span>
                    {t("reconciliation.discardReason", {
                      defaultValue: "Discard reason",
                    })}
                  </span>
                  <textarea
                    autoFocus
                    required
                    value={discardReason}
                    onChange={(event) => setDiscardReason(event.currentTarget.value)}
                  />
                </label>
                <div className="dialog-actions">
                  <button
                    className="icon-button text-button"
                    onClick={() => setDiscardOpen(false)}
                    type="button"
                  >
                    {t("reconciliation.cancelDiscard", { defaultValue: "Cancel" })}
                  </button>
                  <button
                    className="icon-button danger"
                    disabled={busy === "discard" || discardReason.trim().length === 0}
                    onClick={() => void discardBranch()}
                    type="button"
                  >
                    {busy === "discard"
                      ? t("reconciliation.discarding", { defaultValue: "Discarding" })
                      : t("reconciliation.confirmDiscard", {
                          defaultValue: "Discard branch permanently",
                        })}
                  </button>
                </div>
              </>
            ) : (
              <button
                className="icon-button danger"
                onClick={() => setDiscardOpen(true)}
                type="button"
              >
                {t("reconciliation.discard", { defaultValue: "Discard branch" })}
              </button>
            )}
          </section>
        </div>
      ) : null}
    </Dialog>
  );
}

function ReconciliationSide({
  effectHeading,
  effects = [],
  heading,
  side,
}: {
  effectHeading?: string;
  effects?: ContextReconciliationConflict["head_effects"];
  heading?: string;
  side: ContextReconciliationConflict["head"] | null;
}) {
  const { t, i18n } = useTranslation("context");
  return (
    <article className="context-reconciliation-side">
      {heading ? <h4>{heading}</h4> : null}
      {side?.text == null ? (
        <p className="wiki-empty">
          {t("reconciliation.deleted", { defaultValue: "Deleted in this version" })}
        </p>
      ) : (
        <pre>{side.text}</pre>
      )}
      {effects.length > 0 ? (
        <div className="context-reconciliation-operation-effects">
          {effectHeading ? <p className="form-hint">{effectHeading}</p> : null}
          {effects.map((effect, index) =>
            effect.content ? (
              <pre key={`${effect.op}-${index}`}>{effect.content}</pre>
            ) : (
              <p className="wiki-empty" key={`${effect.op}-${index}`}>
                {t("reconciliation.emptyInsertionGap", {
                  defaultValue: "No blocks currently occupy this insertion gap.",
                })}
              </p>
            ),
          )}
        </div>
      ) : null}
      {side?.actor_display_name || side?.actor_kind || side?.at ? (
        <p className="form-hint context-reconciliation-side-provenance">
          {side.actor_display_name ? <span>{side.actor_display_name}</span> : null}
          {side.actor_kind ? (
            <span
              className="context-reconciliation-side-actor-kind"
              data-actor-kind={side.actor_kind}
            >
              {side.actor_kind}
            </span>
          ) : null}
          {side.at ? <RelativeTime value={side.at} locale={i18n.language} /> : null}
        </p>
      ) : null}
    </article>
  );
}

function ResolutionRadio({
  checked,
  disabled = false,
  label,
  name,
  onChange,
}: {
  checked: boolean;
  disabled?: boolean;
  label: string;
  name: string;
  onChange: () => void;
}) {
  return (
    <label className="context-reconciliation-choice">
      <input
        checked={checked}
        disabled={disabled}
        name={name}
        onChange={onChange}
        type="radio"
      />
      <span>{label}</span>
    </label>
  );
}

function isHeadMismatch(error: unknown) {
  if (!(error instanceof ApiError)) {
    return false;
  }
  return (
    [
      "stale_base_version",
      "head_version_mismatch",
      "reconcile_head_advanced",
    ].includes(error.body.error_code)
    || (error.status === 412 && Boolean(error.body.server_version_id))
  );
}

function resolutionIsComplete(resolution: ResolutionDraft | undefined) {
  return Boolean(
    resolution
    && (resolution.choice !== "custom" || resolution.content.trim().length > 0),
  );
}

function blockStateLabel(
  state: ContextReconciliationDetail["blocks"][number]["state"],
  t: ReturnType<typeof useTranslation>["t"],
) {
  return t(`reconciliation.blockStates.${state}`, {
    defaultValue: state.replaceAll("_", " "),
  });
}

function blockDisplayLabel(
  block: ContextReconciliationDetail["blocks"][number],
  t: ReturnType<typeof useTranslation>["t"],
) {
  if (block.label) {
    return block.label;
  }
  if (block.granularity === "table_row" && block.row_index != null) {
    return t("reconciliation.tableRow", {
      defaultValue: "Table row {{row}}",
      row: block.row_index + 1,
    });
  }
  return block.kind;
}

function blockStateDescription(
  state: ContextReconciliationDetail["blocks"][number]["state"],
  t: ReturnType<typeof useTranslation>["t"],
) {
  return t(`reconciliation.blockDescriptions.${state}`, {
    defaultValue:
      state === "branch_only"
        ? "This branch-only block is included in the reconciled draft by default."
        : "This block does not need a manual conflict resolution.",
  });
}

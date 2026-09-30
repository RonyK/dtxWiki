import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import {
  libraryOriginalRequiresWarning,
  type LibraryItemDetail,
} from "../../../shared/api/library";
import {
  getLibraryPreviewManifest,
  isPreviewManifestEndpointUnavailable,
} from "../../../shared/api/libraryPreview";
import {
  createAuthenticatedPreviewTransport,
  normalizePreviewContentPath,
} from "./AuthenticatedPreviewTransport";
import { createLegacyPreviewManifest } from "./legacyPreviewManifest";
import { LegacyPreviewRenderer } from "./LegacyPreviewRenderer";
import type { PreviewManifest, PreviewVariant } from "./model";
import {
  previewRendererFailureKind,
  type PreviewRendererFailureKind,
} from "./previewErrors";
import {
  RegisteredPreviewRenderer,
  supportedPreviewVariants,
} from "./PreviewRendererRegistry";
import {
  otherRendition,
  resolveRendition,
  splitPreviewRenditions,
  type RenditionKind,
} from "./previewRenditions";
import "./preview.css";

type ManifestState =
  | { kind: "loading" }
  | { kind: "ready"; manifest: PreviewManifest; compatibility: boolean }
  | { kind: "error" };

type RenditionSlotState = {
  index: number;
  failure: PreviewRendererFailureKind | null;
};

const INITIAL_SLOT_STATE: RenditionSlotState = { index: 0, failure: null };

export function PreviewPane({
  detail,
  legacyPreviewKind,
  workspaceId,
}: {
  detail: LibraryItemDetail;
  legacyPreviewKind: string | null;
  workspaceId: string;
}) {
  const { t } = useTranslation("library");
  const versionId = previewVersionId(detail);
  const hasManifestPointer = Object.hasOwn(
    detail,
    "current_preview_version_id",
  );
  const [retry, setRetry] = useState(0);
  // One fallback-advance slot per rendition, not one shared slot: doc §3-F
  // requires both rendition trees to stay mounted (just hidden) once
  // visited, so each needs to keep tracking its own renderer failures
  // independently of which one is on screen right now.
  const [originalSlot, setOriginalSlot] = useState<RenditionSlotState>(
    INITIAL_SLOT_STATE,
  );
  const [convertedSlot, setConvertedSlot] = useState<RenditionSlotState>(
    INITIAL_SLOT_STATE,
  );
  // Which rendition tree(s) have been shown at least once for the CURRENT
  // manifest. Doc §3-F: switching Original/Converted must not tear down and
  // rebuild the side navigated away from (that resets PDF page position,
  // media playback, and re-issues Range requests) -- so once a side has
  // rendered, it stays mounted and is only toggled with `hidden`.
  const [visited, setVisited] = useState<Record<RenditionKind, boolean>>({
    original: false,
    converted: false,
  });
  // Only ever set by the user clicking the Original/Converted toggle below.
  // `null` means "follow whatever the manifest content says is best" (see
  // `resolveRendition`). Lives only within the same version's screen (doc
  // §3-B/§3-F): the manifest-fetch effect below resets it to `null` whenever
  // `versionId` changes -- a new latest version or a different History
  // selection gets a fresh default, not a carried-over pick that may not
  // even have a candidate there.
  const [preferredRendition, setPreferredRendition] =
    useState<RenditionKind | null>(null);
  // Scoped to `versionId` alone (not folded into the manifest-fetch effect
  // below, which also reruns on `retry` bumps and on every new `detail`
  // object identity -- e.g. each conversion-status poll tick). Resetting the
  // user's pick on those would make the toggle feel randomly sticky-fragile
  // while a still-converting item is being polled in the background.
  const previousVersionIdRef = useRef<string | null | undefined>(versionId);
  useEffect(() => {
    if (previousVersionIdRef.current !== versionId) {
      previousVersionIdRef.current = versionId;
      setPreferredRendition(null);
    }
  }, [versionId]);
  const [state, setState] = useState<ManifestState>(() =>
    versionId && !hasManifestPointer
      ? {
          kind: "ready",
          manifest: createLegacyPreviewManifest(
            detail,
            legacyPreviewKind,
            versionId,
          ),
          compatibility: true,
        }
      : { kind: "loading" },
  );

  useEffect(() => {
    if (!versionId) {
      setState({ kind: "error" });
      return;
    }
    const controller = new AbortController();
    setOriginalSlot(INITIAL_SLOT_STATE);
    setConvertedSlot(INITIAL_SLOT_STATE);
    setVisited({ original: false, converted: false });
    if (hasManifestPointer || retry > 0) {
      setState({ kind: "loading" });
    }
    void getLibraryPreviewManifest(workspaceId, versionId, controller.signal)
      .then((manifest) => setState({ kind: "ready", manifest, compatibility: false }))
      .catch((error) => {
        if (controller.signal.aborted) return;
        if (isPreviewManifestEndpointUnavailable(error)) {
          setState({
            kind: "ready",
            manifest: createLegacyPreviewManifest(detail, legacyPreviewKind, versionId),
            compatibility: true,
          });
          return;
        }
        setState({ kind: "error" });
      });
    return () => controller.abort();
  }, [
    detail,
    hasManifestPointer,
    legacyPreviewKind,
    retry,
    versionId,
    workspaceId,
  ]);

  const transport = useMemo(
    () => versionId
      ? createAuthenticatedPreviewTransport({
          workspaceId,
          itemId: detail.id,
          versionId,
          allowLegacyRoutes: state.kind === "ready" && state.compatibility,
        })
      : null,
    [detail.id, state, versionId, workspaceId],
  );
  const variants = useMemo(
    () => state.kind === "ready"
      ? materializeFallbacks(
          state.manifest,
          detail,
          state.compatibility,
          workspaceId,
        )
      : [],
    [detail, state, workspaceId],
  );
  const previewSourceFormat = state.kind === "ready"
    ? detail.versions.find(
        (version) => version.id === state.manifest.version_id,
      )?.format_id ?? detail.format_hint
    : detail.format_hint;
  const candidates = useMemo(
    () => supportedPreviewVariants(variants).filter(
      (entry) =>
        entry.variant.renderer_contract !== "core.download.v1" ||
        !libraryOriginalRequiresWarning(previewSourceFormat),
    ),
    [previewSourceFormat, variants],
  );
  const downloadCandidate = candidates.find(
    (entry) => entry.variant.renderer_contract === "core.download.v1",
  );
  // Metadata is the last-resort converted-bucket fallback (doc §3-A/D-6), but
  // it carries provider diagnostics that must stay hidden while the manifest
  // itself hasn't reached a terminal ready state -- otherwise a pending or
  // failed item would flash raw failure-reason/provider-id text at the user.
  const metadataAllowed =
    state.kind !== "ready" ||
    state.manifest.status === "ready" ||
    state.manifest.status === "ready_partial";
  const switchableCandidates = candidates.filter(
    (entry) =>
      entry.variant.renderer_contract !== "core.download.v1" &&
      (metadataAllowed || entry.variant.renderer_contract !== "core.metadata.v1"),
  );
  const renditions = splitPreviewRenditions(switchableCandidates);
  const { rendition: effectiveRendition, substituted } = resolveRendition(
    preferredRendition,
    renditions,
  );
  const showRenditionToggle =
    renditions.original.length > 0 && renditions.converted.length > 0;
  const originalCandidate = renditions.original[originalSlot.index];
  const convertedCandidate = renditions.converted[convertedSlot.index];
  const activeCandidate = effectiveRendition === "original"
    ? originalCandidate
    : convertedCandidate;
  const otherCandidate = effectiveRendition === "original"
    ? convertedCandidate
    : originalCandidate;
  const activeFailure = effectiveRendition === "original"
    ? originalSlot.failure
    : convertedSlot.failure;
  const advanceOriginal = useCallback((error: unknown) => {
    setOriginalSlot((current) => ({
      failure: previewRendererFailureKind(error),
      index: current.index + 1,
    }));
  }, []);
  const advanceConverted = useCallback((error: unknown) => {
    setConvertedSlot((current) => ({
      failure: previewRendererFailureKind(error),
      index: current.index + 1,
    }));
  }, []);
  const selectRendition = useCallback((next: RenditionKind) => {
    setPreferredRendition(next);
  }, []);
  useEffect(() => {
    setVisited((current) =>
      current[effectiveRendition] ? current : { ...current, [effectiveRendition]: true },
    );
  }, [effectiveRendition]);

  if (state.kind === "loading") {
    return <div className="wiki-empty" role="status">{t("reader.preview.loading")}</div>;
  }
  if (state.kind === "error" || !transport) {
    return (
      <div className="library-preview-error" role="alert">
        <p>{t("reader.preview.loadError")}</p>
        <button className="icon-button text-button" type="button" onClick={() => setRetry((value) => value + 1)}>
          {t("reader.preview.retry")}
        </button>
      </div>
    );
  }
  if (state.compatibility) {
    return (
      <LegacyPreviewRenderer
        manifest={state.manifest}
        title={detail.title ?? detail.uri}
        transport={transport}
      />
    );
  }
  // Only fires when a preference was actually overridden for this specific
  // version -- never when there was simply no preference yet (that's the
  // ordinary default in `resolveRendition`, not a substitution).
  const substitutionNotice = substituted ? (
    <p className="library-preview-notice" role="status">
      {t(
        effectiveRendition === "original"
          ? "reader.rendition.showingOriginalFallback"
          : "reader.rendition.showingConvertedFallback",
      )}
    </p>
  ) : null;
  // Shown whenever the item has both an inline original and a converted
  // output, so a converted Markdown never again hides a renderable original
  // (doc §3-A/§3-B) -- and the reverse, switching back to see the converted
  // output for a format that does have both.
  const renditionToggle = showRenditionToggle ? (
    <div
      aria-label={t("reader.rendition.label")}
      className="library-preview-rendition-toggle"
      role="group"
    >
      {(["original", "converted"] as const).map((rendition) => (
        <button
          aria-pressed={effectiveRendition === rendition}
          className="icon-button text-button"
          key={rendition}
          type="button"
          onClick={() => selectRendition(rendition)}
        >
          {t(`reader.rendition.${rendition}`)}
        </button>
      ))}
    </div>
  ) : null;
  // Read-only label for the concrete format on screen (doc §3-A/T-3d) --
  // shown next to the toggle when there is one, or alone when a format has
  // only ever had one rendition to begin with.
  const formatChip = activeCandidate ? (
    <span className="library-preview-format-chip">
      {t(`reader.rendition.format.${activeCandidate.variant.kind}`)}
    </span>
  ) : null;
  const toggleRow = renditionToggle || formatChip ? (
    <div className="library-preview-rendition-row">
      {renditionToggle}
      {formatChip}
    </div>
  ) : null;
  const shouldMountOriginal = Boolean(originalCandidate) &&
    (visited.original || effectiveRendition === "original");
  const shouldMountConverted = Boolean(convertedCandidate) &&
    (visited.converted || effectiveRendition === "converted");
  // A "generated" re-encode or a semantic role=preview crop can stand in for
  // the original bytes on the original side (doc §3-A/D-10). Say so rather
  // than silently presenting a derivative as if it were the literal original.
  const showDerivedNote = originalCandidate?.variant.source !== undefined &&
    originalCandidate.variant.source !== "original";
  const convertedDownloadFallback = convertedCandidate?.variant.renderer_contract
    === "core.metadata.v1"
    ? downloadCandidate
    : undefined;

  return (
    <div className="library-preview-pane">
      {substitutionNotice}
      {toggleRow}
      {shouldMountOriginal && originalCandidate ? (
        <div hidden={effectiveRendition !== "original"}>
          {showDerivedNote ? (
            <p className="library-preview-notice" role="status">
              {t("reader.rendition.derivedNote")}
            </p>
          ) : null}
          <RegisteredPreviewRenderer
            key={`${state.manifest.version_id}:original:${originalCandidate.variant.id ?? originalSlot.index}`}
            candidate={originalCandidate}
            fallback={<div className="wiki-empty">{t("reader.preview.rendererLoading")}</div>}
            onRendererError={advanceOriginal}
            title={detail.title ?? detail.uri}
            transport={transport}
          />
        </div>
      ) : null}
      {shouldMountConverted && convertedCandidate ? (
        <div hidden={effectiveRendition !== "converted"}>
          <RegisteredPreviewRenderer
            key={`${state.manifest.version_id}:converted:${convertedCandidate.variant.id ?? convertedSlot.index}`}
            candidate={convertedCandidate}
            fallback={<div className="wiki-empty">{t("reader.preview.rendererLoading")}</div>}
            onRendererError={advanceConverted}
            title={detail.title ?? detail.uri}
            transport={transport}
          />
          {convertedDownloadFallback ? (
            <RegisteredPreviewRenderer
              candidate={convertedDownloadFallback}
              fallback={null}
              onRendererError={() => undefined}
              title={detail.title ?? detail.uri}
              transport={transport}
            />
          ) : null}
        </div>
      ) : null}
      {!activeCandidate ? (
        otherCandidate ? (
          <EmptyRenditionPanel
            action={
              <button
                className="icon-button text-button"
                type="button"
                onClick={() => selectRendition(otherRendition(effectiveRendition))}
              >
                {t(
                  `reader.rendition.go${
                    otherRendition(effectiveRendition) === "original" ? "Original" : "Converted"
                  }`,
                )}
              </button>
            }
            statusText={t(previewStatusKey(
              state.manifest,
              detail,
              Boolean(downloadCandidate),
              activeFailure,
            ))}
          />
        ) : (
          <EmptyRenditionPanel
            action={
              downloadCandidate ? (
                <RegisteredPreviewRenderer
                  candidate={downloadCandidate}
                  fallback={null}
                  onRendererError={() => undefined}
                  title={detail.title ?? detail.uri}
                  transport={transport}
                />
              ) : null
            }
            statusText={t(previewStatusKey(
              state.manifest,
              detail,
              Boolean(downloadCandidate),
              activeFailure,
            ))}
          />
        )
      ) : null}
    </div>
  );
}

function EmptyRenditionPanel({
  action,
  statusText,
}: {
  action?: ReactNode;
  statusText: string;
}) {
  return (
    <div className="library-preview-state">
      <p role="status">{statusText}</p>
      {action ? <div className="library-preview-state-actions">{action}</div> : null}
    </div>
  );
}

/**
 * The single source of truth for "which version is currently displayed" --
 * P4.4: the original-download button must target this exact same version,
 * never a separately-resolved candidate (PreviewPane and the download
 * button must agree, and never silently substitute an older ready version
 * the other one didn't choose).
 *
 * §3-D: always the item's own latest version, full stop. History's
 * past-version preview (P10.2's `selectedVersionId`) and the
 * `current_preview_version_id`/`current_ready_version_id` bridge to an older
 * *ready* version when the latest one's conversion terminally failed are
 * both gone -- a failed latest conversion shows its own failure state
 * instead of silently substituting older content nothing on screen chose.
 */
export function previewVersionId(detail: LibraryItemDetail) {
  return detail.current_version?.id ?? detail.current_version_id ?? null;
}

function materializeFallbacks(
  manifest: PreviewManifest,
  detail: LibraryItemDetail,
  compatibility: boolean,
  workspaceId: string,
) {
  const variants = [...manifest.variants];
  const existingContracts = new Set(variants.map((variant) => variant.renderer_contract));
  const version = detail.versions.find((candidate) => candidate.id === manifest.version_id) ?? null;
  const artifactText = detail.artifacts.find(
    (artifact) =>
      artifact.library_item_version_id === manifest.version_id && artifact.index_text,
  )?.index_text;
  let rank = Math.max(0, ...variants.map((variant) => variant.rank)) + 10;
  for (const fallback of manifest.fallbacks) {
    const contract = `core.${fallback}.v1`;
    if (existingContracts.has(contract)) continue;
    let variant: PreviewVariant | null = null;
    if ((fallback === "markdown" || fallback === "text") && artifactText) {
      variant = {
        kind: fallback,
        media_type: fallback === "markdown" ? "text/markdown" : "text/plain",
        delivery: "inline",
        renderer_contract: contract,
        rank,
        text: artifactText,
      };
    } else if (
      fallback === "metadata" &&
      (manifest.status === "ready" || manifest.status === "ready_partial") &&
      Object.keys(detail.metadata).length > 0
    ) {
      variant = {
        kind: "metadata",
        media_type: "application/json",
        delivery: "inline",
        renderer_contract: contract,
        rank,
        data: detail.metadata,
      };
    } else if (fallback === "download") {
      const originalUrl = manifest.download_url ??
        version?.original_url ??
        (compatibility ? detail.original_url : null);
      if (
        originalUrl &&
        normalizePreviewContentPath(originalUrl, {
          workspaceId,
          itemId: detail.id,
          versionId: manifest.version_id,
          allowLegacyRoutes: compatibility,
        })
      ) {
        variant = {
          kind: "download",
          media_type: "application/octet-stream",
          delivery: "download",
          content_url: originalUrl,
          renderer_contract: contract,
          rank,
          filename: detail.title ?? undefined,
        };
      }
    }
    if (variant) {
      variants.push(variant);
      existingContracts.add(contract);
      rank += 10;
    }
  }
  return variants;
}

/**
 * The subset of `previewStatusKey`'s logic that's about the CONVERSION
 * pipeline (as opposed to the Preview-generation pipeline or a renderer
 * failure) -- factored out so `LibraryReader` can show the same fact at the
 * top of the reader, above `PreviewPane` entirely, for the version it is
 * actually displaying (doc §3-C/§3-E "T-3a").
 */
function conversionLifecycleStatusKey(manifest: PreviewManifest, detail: LibraryItemDetail) {
  const version = detail.versions.find(
    (candidate) => candidate.id === manifest.version_id,
  ) ?? (
    detail.current_version?.id === manifest.version_id
      ? detail.current_version
      : null
  );
  const conversionReason = version?.conversion?.reason ?? null;
  const processingStatus = version?.conversion?.status
    ?? version?.processing_status
    ?? detail.processing_status;
  if (
    conversionReason === "document_conversion_timed_out" || processingStatus === "timed_out"
  ) {
    return "reader.preview.fileSavedConversionTimedOut" as const;
  }
  if (
    conversionReason === "document_conversion_failed" || processingStatus === "failed"
  ) {
    return "reader.preview.fileSavedConversionFailed" as const;
  }
  if (
    conversionReason === "document_conversion_unavailable"
      || processingStatus === "stored_unconverted"
  ) {
    return "reader.preview.originalSavedNoConverter" as const;
  }
  if (
    processingStatus === "pending" ||
    processingStatus === "queued" ||
    processingStatus === "converting" ||
    processingStatus === "projecting"
  ) {
    return "reader.preview.fileSavedConverting" as const;
  }
  return null;
}

function previewStatusKey(
  manifest: PreviewManifest,
  detail: LibraryItemDetail,
  sourceAvailable: boolean,
  rendererFailure: PreviewRendererFailureKind | null,
) {
  if (rendererFailure === "authentication") {
    return "reader.preview.mediaAuthUnavailable" as const;
  }
  if (rendererFailure === "codec") {
    return "reader.preview.mediaCodecUnsupported" as const;
  }
  if (rendererFailure === "runtime") {
    return "reader.preview.mediaRuntimeFailed" as const;
  }
  const lifecycleKey = sourceAvailable
    ? conversionLifecycleStatusKey(manifest, detail)
    : null;
  if (lifecycleKey) {
    return lifecycleKey;
  }
  if (manifest.status === "queued" || manifest.status === "generating") {
    return sourceAvailable
      ? "reader.preview.previewPendingSourceSaved" as const
      : "reader.preview.generating" as const;
  }
  if (manifest.status === "failed" || manifest.status === "timed_out") {
    return sourceAvailable
      ? "reader.preview.previewGenerationFailedSourceSaved" as const
      : "reader.preview.generationFailed" as const;
  }
  return sourceAvailable
    ? "reader.preview.previewUnavailableSourceSaved" as const
    : "reader.preview.unavailable" as const;
}

/**
 * The current version's own conversion-lifecycle status, independent of
 * which Preview version is on screen. Exported so `LibraryReader` can show
 * it (with a retry action) whenever the displayed version IS the current
 * one -- `PreviewPane` keeps using it only for the opposite case, telling
 * a viewer of an OLDER version what's happening to the current one.
 */
export function currentVersionStatusKey(detail: LibraryItemDetail) {
  const version = currentLibraryVersion(detail);
  const sourceAvailable = Boolean(version?.original_url ?? detail.original_url);
  if (!version || !sourceAvailable) return null;
  const reason = version.conversion?.reason ?? null;
  const status = version.conversion?.status ?? version.processing_status;
  if (reason === "document_conversion_timed_out" || status === "timed_out") {
    return "reader.preview.fileSavedConversionTimedOut" as const;
  }
  if (reason === "document_conversion_failed" || status === "failed") {
    return "reader.preview.fileSavedConversionFailed" as const;
  }
  if (reason === "document_conversion_unavailable" || status === "stored_unconverted") {
    return "reader.preview.originalSavedNoConverter" as const;
  }
  if (
    status === "pending" ||
    status === "queued" ||
    status === "converting" ||
    status === "projecting"
  ) {
    return "reader.preview.fileSavedConverting" as const;
  }
  return null;
}

function currentLibraryVersion(detail: LibraryItemDetail) {
  // Same precedence as `previewVersionId`'s own final fallback: the embedded
  // `current_version` object's own id first, not the separate
  // `current_version_id` pointer field -- a payload can carry the latter as a
  // looser/stale reference than the version object actually attached to it.
  const currentVersionId = detail.current_version?.id ?? detail.current_version_id ?? null;
  if (detail.current_version?.id === currentVersionId) {
    return detail.current_version;
  }
  return detail.versions.find((candidate) => candidate.id === currentVersionId) ?? null;
}

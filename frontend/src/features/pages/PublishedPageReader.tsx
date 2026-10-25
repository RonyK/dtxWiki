import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useLocation, useParams } from "react-router-dom";

import { readPublicPage, type PublicPagePayload } from "../../shared/api/operations";
import {
  contextMediaPublicContentUrl,
  type ContextMediaAssetUrlResolver,
} from "../../shared/api/contextMedia";
import { RouteLoading } from "../../shared/components/RouteLoading";
import { formatDisplayDateTime } from "../../shared/lib/datetime";
import { MarkdownView } from "./MarkdownView";

// Unauthenticated public reader for a published page (T16 27-b). Renders ONLY
// the page title + body markdown — no sidebar, context rail, or inspector. The
// public read endpoint guarantees this is the published head version and that
// the page is explicitly public; drafts/private pages return 404. Internal
// links use only public destinations confirmed in this anonymous response;
// unavailable targets remain text and never cause authenticated lookups.
export function PublishedPageReader() {
  const { t, i18n } = useTranslation("pages");
  const { workspaceSlug = "", slug = "" } = useParams();
  const location = useLocation();
  const [page, setPage] = useState<PublicPagePayload | null>(null);
  const [status, setStatus] = useState<"loading" | "loaded" | "not_found" | "error">(
    "loading",
  );
  const shouldNoIndex =
    page?.exposure_scope === "unlisted_web" ||
    status === "not_found" ||
    status === "error";
  const resolveContextMediaAsset = useMemo<ContextMediaAssetUrlResolver>(
    () => (assetId: string) => ({
      status: "ready",
      url: contextMediaPublicContentUrl(workspaceSlug, slug, assetId),
    }),
    [slug, workspaceSlug],
  );

  // Client-side backstop for the shared SPA shell: only an unlisted payload (or
  // a terminal unavailable state) gets noindex/nofollow. Indexed public pages
  // must remain discoverable. The API and page assets independently carry the
  // scope-aware HTTP headers.
  useEffect(() => {
    if (!shouldNoIndex) {
      return;
    }
    const meta = document.createElement("meta");
    meta.name = "robots";
    meta.content = "noindex, nofollow";
    meta.dataset.dtxPublicPageRobots = "true";
    document.head.appendChild(meta);
    return () => {
      meta.remove();
    };
  }, [shouldNoIndex]);

  useEffect(() => {
    let cancelled = false;
    setPage(null);
    setStatus("loading");
    readPublicPage(workspaceSlug, slug)
      .then((result) => {
        if (cancelled) {
          return;
        }
        if (!result) {
          setPage(null);
          setStatus("not_found");
          return;
        }
        setPage(result);
        setStatus("loaded");
      })
      .catch(() => {
        if (cancelled) {
          return;
        }
        setPage(null);
        setStatus("error");
      });
    return () => {
      cancelled = true;
    };
  }, [slug, workspaceSlug]);

  if (status === "loading") {
    return <RouteLoading label={t("publicReader.loading")} />;
  }

  if (status !== "loaded" || !page) {
    return (
      <main
        aria-label={t("publicReader.ariaLabel")}
        className="public-reader public-reader-empty"
      >
        <div className="empty-state">
          <h1>{t("publicReader.unavailableTitle")}</h1>
          <p>
            {status === "error"
              ? t("publicReader.errorDescription")
              : t("publicReader.notFoundDescription")}
          </p>
          {/* Carry ?next= so the visitor returns here after login (the page
              may simply be private to them; safeNextPath accepts /w/...). */}
          <Link
            className="icon-button text-button"
            to={`/login?next=${encodeURIComponent(`${location.pathname}${location.search}`)}`}
          >
            {t("publicReader.signIn")}
          </Link>
        </div>
      </main>
    );
  }

  const updatedAt = formatDisplayDateTime(
    page.updated_at,
    i18n.resolvedLanguage ?? i18n.language,
  );

  return (
    <main className="public-reader" aria-label={t("publicReader.ariaLabel")}>
      <article className="public-reader-article">
        <header className="public-reader-header">
          <h1>{page.title}</h1>
          <time
            className="public-reader-updated"
            dateTime={page.updated_at}
            title={updatedAt}
          >
            {t("publicReader.lastUpdated", { time: updatedAt })}
          </time>
        </header>
        <MarkdownView
          body={page.body_md}
          resolveContextMediaAsset={resolveContextMediaAsset}
          stripLeadingHeading={page.title}
          wikilinkMode="plain"
          linkTargets={(page as PublicPagePayload & { link_targets?: import("../../shared/lib/contextLinks").ContextLinkTarget[] }).link_targets ?? []}
        />
      </article>
    </main>
  );
}

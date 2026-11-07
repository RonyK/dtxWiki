import { Check, Copy, Info, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { readPage } from "../../shared/api/pages";
import { strongText } from "../../shared/i18n/strongText";
import { formatContextPageMarkdown } from "../../shared/lib/contextLinks";
import { copyTextWithFallback, type CopyTextResult } from "../../shared/lib/copyText";

type Suggestion =
  | { kind: "loading" }
  | { kind: "unavailable" }
  | { kind: "ready"; title: string; markdown: string };

/**
 * The lint queue's "link these two pages" remedy, shown above the editor:
 * the link to paste, a copy button, and nothing else. The body is the
 * author's to change, so the suggestion never writes into it. Once the
 * server's own reading of the page (`linkTargets`) contains the link, the
 * notice says so instead.
 */
export function RelatedLinkSuggestion({
  linkTargets,
  relatedPageId,
  workspaceId,
}: {
  linkTargets: ReadonlyArray<{ target_page_id?: string | null }> | undefined;
  relatedPageId: string;
  workspaceId: string;
}) {
  const { t } = useTranslation("pages");
  const [suggestion, setSuggestion] = useState<Suggestion>({ kind: "loading" });
  const [dismissed, setDismissed] = useState(false);
  const [copyFeedback, setCopyFeedback] = useState<CopyTextResult | null>(null);
  const snippetRef = useRef<HTMLElement>(null);

  useEffect(() => {
    let active = true;
    setSuggestion({ kind: "loading" });
    setDismissed(false);
    readPage(workspaceId, relatedPageId)
      .then((related) => {
        if (!active) return;
        setSuggestion({
          kind: "ready",
          title: related.title,
          markdown: formatContextPageMarkdown(related.title, workspaceId, related.id),
        });
      })
      .catch(() => {
        if (active) setSuggestion({ kind: "unavailable" });
      });
    return () => {
      active = false;
    };
  }, [relatedPageId, workspaceId]);

  useEffect(() => {
    if (!copyFeedback) return undefined;
    const timer = window.setTimeout(
      () => setCopyFeedback(null),
      copyFeedback === "copied" ? 1800 : 4000,
    );
    return () => window.clearTimeout(timer);
  }, [copyFeedback]);

  if (dismissed || suggestion.kind === "loading") return null;

  const linked =
    suggestion.kind === "ready" &&
    (linkTargets ?? []).some((target) => target.target_page_id === relatedPageId);
  const dismiss = (
    <button
      aria-label={t("editor.relatedLinkDismiss", { defaultValue: "Dismiss" })}
      className="editor-related-link-notice__close"
      onClick={() => setDismissed(true)}
      type="button"
    >
      <X size={16} aria-hidden="true" />
    </button>
  );

  return (
    <div
      className={`editor-related-link-notice${suggestion.kind !== "ready" || linked ? " is-compact" : ""}`}
      role="status"
    >
      <Info className="editor-related-link-notice__icon" size={16} aria-hidden="true" />
      <p>
        {suggestion.kind === "unavailable"
          ? t("editor.relatedLinkUnavailable", {
              defaultValue: "Couldn't load the page to link.",
            })
          : linked
            ? strongText(
                (values) =>
                  t("editor.relatedLinkPresent", {
                    ...values,
                    defaultValue:
                      "This page already links to {{title}}. Publish it to resolve the lint finding.",
                  }),
                { title: suggestion.title },
              )
            : strongText(
                (values) =>
                  t("editor.relatedLinkSuggest", {
                    ...values,
                    defaultValue:
                      "Add a link to {{title}} to this page. Copy the text below and paste it where it fits.",
                  }),
                { title: suggestion.title },
              )}
      </p>
      {dismiss}
      {suggestion.kind === "ready" && !linked ? (
        <div className="editor-related-link-notice__snippet">
          <code ref={snippetRef}>{suggestion.markdown}</code>
          <button
            aria-label={t("editor.relatedLinkCopyAria", { defaultValue: "Copy the link text" })}
            className={`icon-button${copyFeedback === "copied" ? " is-done" : ""}`}
            onClick={async () => {
              setCopyFeedback(await copyTextWithFallback(suggestion.markdown, snippetRef.current));
            }}
            type="button"
          >
            {copyFeedback === "copied" ? (
              <Check size={15} aria-hidden="true" />
            ) : (
              <Copy size={15} aria-hidden="true" />
            )}
            <span>
              {copyFeedback === "copied"
                ? t("editor.relatedLinkCopied", { defaultValue: "Copied" })
                : t("editor.relatedLinkCopy", { defaultValue: "Copy" })}
            </span>
          </button>
          {copyFeedback && copyFeedback !== "copied" ? (
            <span className="editor-related-link-notice__status">
              {copyFeedback === "selected"
                ? t("editor.relatedLinkSelected", {
                    defaultValue: "The text is selected. Press Ctrl+C to copy it.",
                  })
                : t("editor.relatedLinkCopyUnavailable", {
                    defaultValue: "Copying isn't available here. Select the text and copy it.",
                  })}
            </span>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

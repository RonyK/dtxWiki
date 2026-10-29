import { Bot, Cog, UserRound, X } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import type { ActivityItem } from "../api/operations";
import { contextActivityPresentation } from "../lib/contextActivity";
import { presentActivityItem } from "../lib/activityLabels";
import { RelativeTime } from "./RelativeTime";

export function ActivityEventCard({ item, workspaceId }: {
  item: ActivityItem;
  workspaceId: string | null;
}) {
  const { t, i18n } = useTranslation("activity");
  const model = contextActivityPresentation(item);
  const fallback = model ? null : presentActivityItem(item);
  const actor = item.actor;
  const ActorIcon = actor?.actor_kind === "system" ? Cog
    : actor?.actor_kind === "mcp_client" || actor?.actor_kind === "agent" ? Bot : UserRound;
  const name = actor?.display_name || t("contextActivity.actorUnknown");
  const fullName = actor?.credential_owner_display_name
    ? `${actor.credential_owner_display_name} · ${name}` : name;
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const popup = useRef<HTMLDivElement>(null);
  const popupId = useId();
  const [position, setPosition] = useState({ top: 0, right: 12 });
  const date = new Date(item.created_at);
  const validDate = !Number.isNaN(date.getTime());
  const exactTime = validDate ? new Intl.DateTimeFormat(i18n.language, {
    dateStyle: "long", timeStyle: "long",
  }).format(date) : t("contextActivity.timeUnknown");
  useEffect(() => {
    if (!open) return;
    const bounds = popup.current?.getBoundingClientRect();
    if (bounds && bounds.bottom > window.innerHeight - 12) {
      setPosition(previous => ({ ...previous, top: Math.max(12, window.innerHeight - bounds.height - 12) }));
    }
    popup.current?.querySelector<HTMLButtonElement>("button")?.focus();
    const close = () => setOpen(false);
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setOpen(false);
        trigger.current?.focus();
      }
    };
    const onPointer = (event: PointerEvent) => {
      if (!popup.current?.contains(event.target as Node)
        && !trigger.current?.contains(event.target as Node)) close();
    };
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("pointerdown", onPointer, true);
    window.addEventListener("resize", close);
    window.addEventListener("scroll", close, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      document.removeEventListener("pointerdown", onPointer, true);
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", close, true);
    };
  }, [open]);
  const details = item.details;
  const pageHref = details && workspaceId ? `/workspaces/${encodeURIComponent(workspaceId)}/context/pages/${details.page_id}?tab=history` : null;
  let href: string | null = null;
  let linkLabel = "";
  if (workspaceId && item.review_item_id && details && (
    model?.action === "page.submit_context_edit_for_review"
    || model?.action.startsWith("review.")
    || model?.action === "page.restore_version"
    || model?.action.startsWith("context_page.draft.") && model.action !== "context_page.draft.publish"
  )) {
    href = `/review?review=${encodeURIComponent(item.review_item_id)}`;
    linkLabel = t("contextActivity.openReview");
  } else if (pageHref && details?.version?.immutable && (
    model?.action === "context_page.draft.publish" || model?.action === "page.restore_version"
  )) {
    href = `${pageHref}&compareFrom=empty&compareTo=${details.version.id}`;
    linkLabel = t("contextActivity.openHistory");
    if (model?.action === "context_page.draft.publish" && details.previous_published_version?.immutable) {
      href = `${pageHref}&compareFrom=${details.previous_published_version.id}&compareTo=${details.version.id}`;
      linkLabel = t("contextActivity.compare");
    }
  }
  const pair = model?.pair;
  const longPair = pair && (pair.before.length > 28 || pair.after.length > 28);
  return (
    <article className="context-activity-card context-event-card" data-action={model?.action}>
      <div className="context-event-main">
        <span className="context-event-chip" data-tone={model?.tone ?? "neutral"}>
          {model?.label ?? t("contextActivity.actions.record")}
        </span>
        <div className={`context-event-change${longPair ? " context-event-change-long" : ""}`}>
          {pair ? <>
            <span className={longPair ? "context-event-pair-label" : "context-event-sr-only"}>{t("contextActivity.before")}</span>
            <span className="context-event-before">{pair.before}</span>
            {longPair ? <span className="context-event-pair-label">{t("contextActivity.after")}</span>
              : <span className="context-event-arrow" aria-hidden="true">→</span>}
            {!longPair ? <span className="context-event-sr-only">{t("contextActivity.after")}</span> : null}
            <strong>{pair.after}</strong>
          </> : <strong>{model?.headline ?? fallback?.title}</strong>}
        </div>
      </div>
      <div className="context-event-actor">
        <button
          ref={trigger} type="button" className="context-event-author"
          aria-label={t("contextActivity.actorInfoLabel", { name: fullName })}
          aria-haspopup="dialog"
          aria-expanded={open} aria-controls={open ? popupId : undefined}
          onClick={() => {
            const rect = trigger.current?.getBoundingClientRect();
            if (rect) setPosition({
              top: Math.max(8, Math.min(rect.bottom + 6, window.innerHeight - 200)),
              right: Math.max(12, window.innerWidth - rect.right),
            });
            setOpen(!open);
          }}
        ><span className="context-event-avatar"><ActorIcon size={13} aria-hidden="true" /></span>
          <span className="context-event-author-name">{fullName}</span>
        </button>
        <RelativeTime value={item.created_at} locale={i18n.language} className="context-event-time" />
      </div>
      {model?.notes.length ? <p className="context-event-support">{model.notes.join(" · ")}</p> : null}
      {model?.reason ? model.reason.length > 80 ? (
        <details className="context-event-support context-event-reason"><summary>{t("contextActivity.reason")} · {t("contextActivity.showAll")}</summary><p>{model.reason}</p></details>
      ) : <p className="context-event-support"><span className="context-event-muted">{t("contextActivity.reason")}</span> {model.reason}</p> : null}
      {!model && fallback?.body ? <p className="context-event-support">{fallback.body}</p> : null}
      {model?.currentPath ? <p className="context-event-support context-event-muted">{t("contextActivity.currentPath")}</p> : null}
      {model?.paths ? <details className="context-event-support context-event-paths">
        <summary>{t("contextActivity.fullPath")}</summary>
        <dl><dt>{t("contextActivity.before")}</dt><dd>{model.paths.before}</dd>
          <dt>{t("contextActivity.after")}</dt><dd>{model.paths.after}</dd></dl>
      </details> : null}
      {href ? <div className="context-event-links"><Link to={href}>{linkLabel} <span aria-hidden="true">→</span></Link></div> : null}
      {open ? createPortal(<div
        id={popupId} ref={popup} role="dialog" aria-label={t("contextActivity.recordInfo")}
        className="context-event-popover" style={position}
      ><div className="context-event-popover-heading">{t("contextActivity.recordInfo")}<button type="button" className="icon-button" aria-label={t("contextActivity.close")}
        onClick={() => { setOpen(false); trigger.current?.focus(); }}><X size={16} aria-hidden="true" /></button></div>
        <dl><dt>{t("contextActivity.actor")}</dt><dd>{fullName}</dd>
          <dt>{t("contextActivity.time")}</dt><dd>{exactTime}</dd></dl>
      </div>, document.body) : null}
    </article>
  );
}

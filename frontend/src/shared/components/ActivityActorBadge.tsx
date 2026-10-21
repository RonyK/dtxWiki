import { autoUpdate, computePosition, flip, offset, shift } from "@floating-ui/dom";
import { X } from "lucide-react";
import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";

import type { ActivityItem } from "../api/operations";

/** One compact actor, with both unabridged identities available without navigation. */
export function ActivityActorBadge({ actor }: { actor: ActivityItem["actor"] }) {
  const { t } = useTranslation("activity");
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const popover = useRef<HTMLDivElement>(null);
  const suppressFocusPreview = useRef(false);
  const suppressPointerPreview = useRef(false);
  const focusPopoverOnOpen = useRef(false);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [mode, setMode] = useState<"closed" | "preview" | "pinned">("closed");
  const [position, setPosition] = useState({ left: 12, top: 12 });
  const open = mode !== "closed";
  const name = actor?.actor_kind === "agent"
    ? t("actors.agentWorker")
    : actor?.actor_kind === "system"
      ? t("kind.options.system")
      : actor?.display_name ?? "";
  const owner = actor?.actor_kind === "mcp_client"
    ? actor.credential_owner_display_name
    : null;
  const fullName = owner ? `${owner} · ${name}` : name;
  const closeAndReturnFocus = useCallback(() => {
    // WebKit can deliver focus and pointer entry after focus() returns and
    // scrolls this chip under a stationary pointer. Keep both previews
    // dismissed until focus or the pointer actually leaves the trigger.
    suppressFocusPreview.current = true;
    suppressPointerPreview.current = true;
    setMode("closed");
    trigger.current?.focus();
  }, []);

  useLayoutEffect(() => {
    if (!open || !trigger.current || !popover.current) return;
    const anchor = trigger.current;
    const surface = popover.current;
    if (focusPopoverOnOpen.current) {
      focusPopoverOnOpen.current = false;
      surface.focus();
    }
    let active = true;
    const update = () => {
      void computePosition(anchor, surface, {
        placement: "bottom-start",
        strategy: "fixed",
        middleware: [offset(6), flip({ padding: 12 }), shift({ padding: 12 })],
      }).then(({ x, y }) => {
        if (active) setPosition({ left: x, top: y });
      });
    };
    const cleanup = autoUpdate(anchor, surface, update);
    return () => { active = false; cleanup(); };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const dismiss = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!trigger.current?.contains(target) && !popover.current?.contains(target)) {
        setMode("closed");
      }
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      closeAndReturnFocus();
    };
    document.addEventListener("pointerdown", dismiss);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", dismiss);
      document.removeEventListener("keydown", escape);
    };
  }, [open, closeAndReturnFocus]);

  useEffect(() => () => { if (closeTimer.current) clearTimeout(closeTimer.current); }, []);
  const keepOpen = () => { if (closeTimer.current) clearTimeout(closeTimer.current); };
  const leave = () => {
    keepOpen();
    closeTimer.current = setTimeout(() => {
      if (document.activeElement !== trigger.current && document.activeElement !== popover.current) {
        setMode((current) => current === "preview" ? "closed" : current);
      }
    }, 160);
  };
  if (!actor) return null;
  const blur = (relatedTarget: EventTarget | null) => {
    if (!(relatedTarget instanceof Node)
      || (!trigger.current?.contains(relatedTarget) && !popover.current?.contains(relatedTarget))) {
      suppressFocusPreview.current = false;
      suppressPointerPreview.current = false;
      setMode("closed");
    }
  };
  return (
    <>
      <button
        ref={trigger}
        className="activity-actor-disclosure"
        type="button"
        aria-label={t("actors.fullNames", { names: fullName })}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        aria-describedby={open ? `${id}-help` : undefined}
        onFocus={() => {
          if (!suppressFocusPreview.current) setMode((current) => current === "closed" ? "preview" : current);
        }}
        onKeyDown={(event) => {
          if (event.key !== "ArrowDown") return;
          event.preventDefault();
          suppressFocusPreview.current = false;
          suppressPointerPreview.current = false;
          setMode("pinned");
          if (popover.current) popover.current.focus();
          else focusPopoverOnOpen.current = true;
        }}
        onBlur={(event) => blur(event.relatedTarget)}
        onPointerEnter={(event) => {
          keepOpen();
          if (!suppressPointerPreview.current && event.pointerType !== "touch") setMode((current) => current === "closed" ? "preview" : current);
        }}
        onPointerLeave={() => {
          suppressPointerPreview.current = false;
          leave();
        }}
        onClick={() => {
          if (mode === "pinned") closeAndReturnFocus();
          else {
            suppressFocusPreview.current = false;
            suppressPointerPreview.current = false;
            setMode("pinned");
          }
        }}
      >
        {actor.actor_kind === "mcp_client" ? (
          <span className="activity-actor-split">
            {owner && <span className="activity-actor-split__owner"><span>{owner}</span></span>}
            <span className="activity-actor-split__ai"><span>{name}</span></span>
          </span>
        ) : (
          <span className="activity-actor-pill" data-actor={actor.actor_kind === "agent" || actor.actor_kind === "system" ? actor.actor_kind : "human"}>
            <span>{name}</span>
          </span>
        )}
      </button>
      {open && createPortal(
        // Keyboard users can focus and scroll the non-modal name disclosure.
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
        <div ref={popover} tabIndex={0}
          className="activity-actor-popover"
          id={id}
          role="dialog"
          aria-modal="false"
          aria-label={t("actors.details")}
          style={{ position: "fixed", ...position }}
          onPointerEnter={keepOpen}
          onPointerLeave={leave}
          onBlur={(event) => blur(event.relatedTarget)}
        >
          <p className="activity-actor-popover__help" id={`${id}-help`}>{t("actors.keyboardHelp")}</p>
          <dl>
            {owner && <><dt>{t("actors.user")}</dt><dd>{owner}</dd></>}
            <dt>{t(actor.actor_kind === "mcp_client" ? "actors.mcp" : "actors.user")}</dt>
            <dd>{name}</dd>
          </dl>
          <button className="activity-actor-popover__close" type="button" onClick={closeAndReturnFocus}><X size={14} aria-hidden="true" />{t("actors.close")}</button>
        </div>, document.body,
      )}
    </>
  );
}

import type { TFunction } from "i18next";
import type { LucideIcon } from "lucide-react";

import type { InboxItem } from "../../shared/api/inbox";
import { accessKeyExpiryRenderer } from "./kinds/accessKey";
import { reviewAttentionRenderer, reviewResultRenderer } from "./kinds/review";
import { workspaceJoinRenderer } from "./kinds/workspaceJoin";

// How one kind presents its items. Every part is optional: what a kind leaves
// out falls back to what the server projected for the viewer.
export type InboxRenderer = {
  title?: (item: InboxItem, t: TFunction<"inbox">) => string;
  summary?: (item: InboxItem, t: TFunction<"inbox">) => string | null;
  icon?: LucideIcon;
};

// The composition point for presentation: a kind defines its renderer in its
// own module and lists it here; the screen never branches on a kind.
export const INBOX_RENDERERS: Readonly<Record<string, InboxRenderer>> = {
  review_attention: reviewAttentionRenderer,
  review_result: reviewResultRenderer,
  workspace_join: workspaceJoinRenderer,
  access_key_expiry: accessKeyExpiryRenderer,
};

export type InboxPresentation = { title: string; summary: string | null; icon: LucideIcon | null };

// A kind the server no longer has shows a fallback without content or link.
export function presentInboxItem(
  item: InboxItem,
  t: TFunction<"inbox">,
  renderers: Readonly<Record<string, InboxRenderer>> = INBOX_RENDERERS,
): InboxPresentation {
  if (!item.display) return { title: t("item.unknown"), summary: null, icon: null };
  const renderer = renderers[item.kind] ?? {};
  return {
    title: renderer.title?.(item, t) ?? item.display.title ?? t("item.untitled"),
    summary: renderer.summary?.(item, t) ?? null,
    icon: renderer.icon ?? null,
  };
}

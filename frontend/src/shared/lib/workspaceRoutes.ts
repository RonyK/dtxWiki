import { useLocation } from "react-router-dom";

import type { TreeNodePayload } from "../api/workspace";
import { formatCanonicalContextUrl, isContextId } from "./contextLinks";

export type WorkspaceSection = "dashboard" | "context" | "library" | "agent" | "activity" | "review" | "lint" | "chats" | "ask" | "search" | "settings" | "setup" | "inbox";
export type WorkspaceRoute = {
  workspaceId: string;
  section: WorkspaceSection;
  targetId: string | null;
  targetKind: "page" | "folder" | "item" | "artifact" | "review" | "finding" | "chat" | "session" | null;
  edit: boolean;
  create: boolean;
  suffix: string;
};

const sections = new Set<WorkspaceSection>(["dashboard", "context", "library", "agent", "activity", "review", "lint", "chats", "ask", "search", "settings", "setup", "inbox"]);
const identityQueries = ["target", "edit", "review", "finding", "new", "newFolder", "parentNodeId", "parentId", "workspaceId", "workspace_id", "ws"];

export function workspacePath(workspaceId: string, path = ""): string {
  if (!workspaceId || /[/?#\\]/.test(workspaceId)) throw new Error("An explicit workspace id is required");
  return `/workspaces/${encodeURIComponent(workspaceId)}${path ? `/${path.replace(/^\/+/, "")}` : ""}`;
}

export function contextPageHref(workspaceId: string, pageId: string, fragment?: string | null): string {
  return formatCanonicalContextUrl(workspaceId, pageId, fragment);
}

export function contextEditHref(workspaceId: string, pageId: string): string {
  return `${contextPageHref(workspaceId, pageId)}/edit`;
}

export function contextNewHref(workspaceId: string, parentId?: string | null): string {
  return workspacePath(workspaceId, "context/pages/new") + (parentId ? `?parent=${encodeURIComponent(parentId)}` : "");
}

export function workspaceTargetHref(workspaceId: string, section: "context" | "library" | "agent" | "ask", targetId: string, kind?: "page" | "folder" | "item" | "artifact" | "session"): string {
  const id = encodeURIComponent(targetId);
  if (kind === "folder") return workspacePath(workspaceId, `${section}/folders/${id}`);
  if (section === "ask") return workspacePath(workspaceId, `ask/${id}`);
  return workspacePath(workspaceId, `${section}/${section === "library" ? kind === "artifact" ? "artifacts" : "items" : "pages"}/${id}`);
}

export function treeNodeHref(workspaceId: string, node: TreeNodePayload): string | null {
  const section = node.section;
  if (!["context", "library", "agent"].includes(section)) return null;
  const domain = section as "context" | "library" | "agent";
  if (node.node_kind === "folder") return workspaceTargetHref(workspaceId, domain, node.id, "folder");
  if (!node.target_id) return workspacePath(workspaceId, domain);
  return workspaceTargetHref(workspaceId, domain, node.target_id, node.target_kind === "library_artifact" ? "artifact" : undefined);
}

export function parseWorkspaceRoute(pathname: string): WorkspaceRoute | null {
  const match = /^\/workspaces\/([^/]+)(?:\/(.*))?$/.exec(pathname.replace(/\/+$/, ""));
  if (!match) return null;
  let workspaceId: string;
  try { workspaceId = decodeURIComponent(match[1]); } catch { return null; }
  if (/[/?#\\]/.test(workspaceId) || workspaceId === "find") return null;
  if (isContextId(workspaceId)) workspaceId = workspaceId.toLowerCase();
  const suffix = match[2] ?? "dashboard";
  const parts = suffix.split("/");
  const section = parts[0] as WorkspaceSection;
  if (!sections.has(section)) return null;
  let targetId: string | null = null;
  let targetKind: WorkspaceRoute["targetKind"] = null;
  const create = parts[2] === "new" && ["pages", "items", "folders"].includes(parts[1]);
  if (["pages", "items", "artifacts", "folders", "findings"].includes(parts[1]) && parts[2] && !create) {
    targetId = parts[2];
    targetKind = ({ pages: "page", items: "item", artifacts: "artifact", folders: "folder", findings: "finding" } as const)[parts[1] as "pages" | "items" | "artifacts" | "folders" | "findings"];
  } else if (["review", "chats", "ask"].includes(section) && parts[1]) {
    targetId = parts[1];
    targetKind = section === "review" ? "review" : section === "chats" ? "chat" : "session";
  }
  if (create && parts[1] === "folders") targetKind = "folder";
  if (targetId) { try { targetId = decodeURIComponent(targetId); } catch { return null; } }
  if (targetId && isContextId(targetId)) targetId = targetId.toLowerCase();
  return { workspaceId, section, targetId, targetKind, edit: parts.at(-1) === "edit", create, suffix };
}

export function useWorkspaceRoute(): WorkspaceRoute | null {
  return parseWorkspaceRoute(useLocation().pathname);
}

export function useRouteWorkspaceId(): string {
  const route = useWorkspaceRoute();
  if (!route) throw new Error("This screen requires an explicit workspace URL");
  return route.workspaceId;
}

export function hasConflictingWorkspaceQuery(search: string, allowParent = false): boolean {
  const params = new URLSearchParams(search);
  return identityQueries.some((key) => params.has(key)) || (!allowParent && params.has("parent")) || params.getAll("parent").length > 1;
}

export function isValidWorkspaceRoute(route: WorkspaceRoute, search: string): boolean {
  const params = new URLSearchParams(search);
  const parent = params.get("parent");
  return isContextId(route.workspaceId) && (!route.targetId || isContextId(route.targetId)) && (!params.has("parent") || (parent !== null && isContextId(parent))) && !hasConflictingWorkspaceQuery(search, route.create);
}

/** Preserve view state while changing target identity in the pathname. */
export function withWorkspaceQuery(href: string, search: URLSearchParams): string {
  const params = new URLSearchParams(search);
  for (const key of [...identityQueries, "parent", "version", "reconcile", "compareFrom", "compareTo", "reviewItemId", "review_item_id", "title", "category"]) params.delete(key);
  const query = params.toString();
  return href + (query ? `${href.includes("?") ? "&" : "?"}${query}` : "");
}

export function workspaceSwitchHref(workspaceId: string, pathname: string): string {
  const route = parseWorkspaceRoute(pathname);
  return workspacePath(workspaceId, route?.section ?? "dashboard");
}

/** Only target-free legacy entry points can use the last-used landing hint. */
export function legacyWorkspaceEntry(pathname: string, search: string): string | null {
  if (hasConflictingWorkspaceQuery(search)) return null;
  const path = pathname.replace(/\/+$/, "") || "/";
  if (path === "/") return "dashboard";
  if (path === "/settings/integrations") return "settings/integrations";
  if (/^\/activity\/(?:pending|advisory|history)$/.test(path)) return path.slice(1);
  const section = path.slice(1) as WorkspaceSection;
  return sections.has(section) ? section : null;
}

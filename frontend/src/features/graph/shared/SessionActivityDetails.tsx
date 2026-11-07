import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import type { GraphEdge, GraphNode } from "../../../shared/api/graph";
import { readMcpChat, type McpChatArtifactLink, type McpChatTimelineItem } from "../../../shared/api/operations";
import { useOptionalAuth } from "../../../shared/auth/AuthContext";
import { formatVersionTimestamp } from "../../../shared/lib/datetime";
import { workspacePath } from "../../../shared/lib/workspaceRoutes";
import { groupChatFlow, type ChatFlowPhase } from "../../mcp-chats/chatFlow";
import { artifactVerbPillLabel } from "../../mcp-chats/chatLabels";
import { chatPresentationLine } from "../../mcp-chats/chatPresentation";
import { sessionRelationSummary } from "../sessionActivity";
import { isSessionGroupNode, sessionActionRank } from "./sessionNodes";

/** What a session's own screen adds to the graph: per-verb counts and its flow. */
type SessionDetailData = {
  links: (Pick<McpChatArtifactLink, "verb" | "occurrence_count"> & { resourceKind: string; resourceId: string })[];
  steps: { id: string; phase: ChatFlowPhase; items: McpChatTimelineItem[] }[];
};

type SessionDetailSource = {
  /** The session list screen; a group node opens it. */
  list: "chats" | "ask";
  /** Fetched only while one of its sessions is selected. */
  load?: (workspaceId: string, id: string, signal: AbortSignal) => Promise<SessionDetailData>;
};

/**
 * One entry per session kind. A new kind of conversation registers here and
 * needs no other branch in Details.
 */
const SESSION_DETAIL_SOURCES: Record<string, SessionDetailSource> = {
  mcp_chat_session: {
    list: "chats",
    load: async (workspaceId, id, signal) => {
      const chat = await readMcpChat(workspaceId, id, { signal });
      return {
        links: chat.linked_artifacts.map((link) => ({
          resourceKind: link.resource_kind,
          resourceId: link.resource_id,
          verb: link.verb,
          occurrence_count: link.occurrence_count,
        })),
        steps: groupChatFlow(chat.timeline ?? []).steps,
      };
    },
  },
  mcp_chat_session_group: { list: "chats" },
  ask_session: { list: "ask" },
  ask_session_group: { list: "ask" },
};

const FLOW_STEP_LIMIT = 6;
const VERSION_OUTCOMES = new Set(["published_snapshot", "superseded_snapshot", "draft", "editing", "discarded"]);

type LoadState = { scope: string; data: SessionDetailData | null; loading: boolean; error: boolean };

export function hasSessionDetails(node: GraphNode | null | undefined): boolean {
  return Boolean(node && Object.hasOwn(SESSION_DETAIL_SOURCES, node.entity_kind));
}

/**
 * Details for a conversation node: what it did to this document and with what
 * result, and the order it worked in. The graph answers the first part at
 * once; the chat's own detail fills in per-verb counts and the flow.
 */
export function SessionActivityDetails({ active, edges, node, nodes, href, workspaceId }: {
  active: boolean;
  edges: GraphEdge[];
  node: GraphNode;
  nodes: GraphNode[];
  href: string | null;
  workspaceId: string | null;
}) {
  const { t } = useTranslation("graph");
  const { t: tChats } = useTranslation("chats");
  const headingId = useId();
  const source = SESSION_DETAIL_SOURCES[node.entity_kind];
  const actorId = useOptionalAuth()?.user?.user_id ?? null;
  const scope = JSON.stringify([active, actorId, workspaceId, node.entity_kind, node.entity_id]);
  const liveScope = useRef(scope);
  useLayoutEffect(() => { liveScope.current = scope; }, [scope]);
  const [state, setState] = useState<LoadState>({ scope: "", data: null, loading: false, error: false });
  const current = state.scope === scope ? state : null;
  const loader = source?.load;
  const load = useCallback(async (signal: AbortSignal) => {
    if (!active || !loader || !workspaceId) return;
    setState({ scope, data: null, loading: true, error: false });
    try {
      const data = await loader(workspaceId, node.entity_id, signal);
      // A late answer for an earlier selection is dropped.
      if (signal.aborted || liveScope.current !== scope) return;
      setState({ scope, data, loading: false, error: false });
    } catch {
      if (!signal.aborted && liveScope.current === scope) setState({ scope, data: null, loading: false, error: true });
    }
  }, [active, loader, node.entity_id, scope, workspaceId]);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load, attempt]);

  if (!source) return null;
  const byId = new Map(nodes.map((entry) => [entry.id, entry]));
  const own = edges.filter((edge) => edge.source === node.id);

  if (isSessionGroupNode(node)) {
    return (
      <section aria-labelledby={headingId} className="graph-session-details">
        <h5 id={headingId}>{t("graph.session.groupHeading")}</h5>
        <p>{t("graph.session.groupBody", { count: node.aggregate_count ?? 0 })}</p>
        {workspaceId ? <Link className="graph-session-list-link" to={workspacePath(workspaceId, source.list)}>
          {t(`graph.session.list.${source.list}`)}</Link> : null}
      </section>
    );
  }

  const seedEdges = own.filter((edge) => byId.get(edge.target)?.is_seed);
  const versions = own.flatMap((edge) => {
    const target = byId.get(edge.target);
    return target?.entity_kind === "context_page_version" ? [target] : [];
  });
  const formatTime = (value: string) => formatVersionTimestamp(value);
  const verbsFor = (seed: GraphNode, edge: GraphEdge) => {
    const links = current?.data?.links.filter((link) => link.resourceKind === seed.entity_kind
      && link.resourceId === seed.entity_id && link.verb !== "search_match") ?? [];
    if (!links.length) return sessionRelationSummary(edge, t, formatTime);
    return [...links].sort((left, right) => sessionActionRank(left.verb) - sessionActionRank(right.verb))
      .map((link) => artifactVerbPillLabel(link, tChats)).join(" · ");
  };
  const steps = current?.data?.steps ?? [];
  return (
    <section aria-labelledby={headingId} className="graph-session-details">
      <h5 id={headingId}>{t("graph.session.thisDocument")}</h5>
      <ul className="graph-session-facts">
        {seedEdges.map((edge) => {
          const seed = byId.get(edge.target)!;
          return <li key={edge.id}>{seedEdges.length > 1 ? <strong>{seed.label} </strong> : null}{verbsFor(seed, edge)}</li>;
        })}
        {versions.map((version) => <li key={version.id} data-version-status={version.status ?? undefined}>
          {t("graph.session.produced", {
            version: version.label,
            outcome: VERSION_OUTCOMES.has(version.status ?? "")
              ? t(`graph.session.outcome.${version.status}`) : version.status ?? "",
          })}
        </li>)}
      </ul>
      {loader && workspaceId && active ? <>
        <h5>{t("graph.session.flow")}</h5>
        {!current || current.loading ? <p role="status">{t("graph.session.loading")}</p> : null}
        {current?.error ? <p role="alert">{t("graph.session.loadFailed")} <button className="inline-action" type="button"
          onClick={() => setAttempt((value) => value + 1)}>{t("graph.session.retry")}</button></p> : null}
        {steps.length ? <ol className="graph-session-flow">
          {steps.slice(0, FLOW_STEP_LIMIT).map((step) => <li key={step.id} data-phase={step.phase}>
            <strong>{tChats(`detail.flow.phaseLabel.${step.phase}`)}</strong>{" "}
            {step.items.length > 1
              ? t("graph.session.stepMore", { title: chatPresentationLine(step.items[0], tChats), count: step.items.length - 1 })
              : chatPresentationLine(step.items[0], tChats)}
          </li>)}
        </ol> : null}
        {steps.length > FLOW_STEP_LIMIT && href ? <Link className="graph-session-list-link" to={href}>
          {t("graph.session.fullFlow", { count: steps.length })}</Link> : null}
      </> : null}
    </section>
  );
}

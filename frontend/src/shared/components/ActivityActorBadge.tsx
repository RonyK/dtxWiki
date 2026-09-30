import { useTranslation } from "react-i18next";

import type { ActivityItem } from "../api/operations";

/**
 * Who did this. A single tone for human/agent/system; an MCP actor splits the
 * pill in two, since it is two facts (who granted the access, which
 * registered AI ran under it), not one -- collapsing both into a single
 * "MCP client" label was the actual gap: the data existed
 * (MCPClient.name / issued_to_user_id) but was never surfaced.
 *
 * Shared by the Activity feed and the Dashboard's recent-activity list, which
 * render the same rows from the same endpoint. It resolves its own labels
 * from the `activity` namespace rather than taking a `t` prop, so a caller on
 * a different namespace (Dashboard is on `dashboard`) cannot mislabel it.
 */
export function ActivityActorBadge({
  actor,
}: {
  actor: ActivityItem["actor"];
}) {
  const { t } = useTranslation("activity");
  if (!actor) return null;
  if (actor.actor_kind === "mcp_client") {
    if (actor.credential_owner_display_name) {
      return (
        <span className="activity-actor-split">
          <span className="activity-actor-split__owner">
            {actor.credential_owner_display_name}
          </span>
          <span className="activity-actor-split__ai">{actor.display_name}</span>
        </span>
      );
    }
    // No owner on record (legacy key, or issued to a since-deleted user):
    // the AI-toned half on its own, rather than a split pill with an empty
    // side.
    return (
      <span className="activity-actor-split">
        <span className="activity-actor-split__ai">{actor.display_name}</span>
      </span>
    );
  }
  if (actor.actor_kind === "agent") {
    return (
      <span className="activity-actor-pill" data-actor="agent">
        {t("actors.agentWorker", { defaultValue: "Internal agent worker" })}
      </span>
    );
  }
  if (actor.actor_kind === "system") {
    return (
      <span className="activity-actor-pill" data-actor="system">
        {t("kind.options.system", { defaultValue: "System" })}
      </span>
    );
  }
  // "human" and any other/legacy actor_kind: the real display_name, in the
  // same tone as "human" so an unrecognized kind never renders blank.
  return (
    <span className="activity-actor-pill" data-actor="human">
      {actor.display_name}
    </span>
  );
}

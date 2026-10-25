import type { McpChatArtifactLink } from "../../shared/api/operations";

// The server keeps one link row per (artifact, verb) -- graph and trail
// projections read the verbs separately, so the rows must stay apart there.
// A document that was found, read, cited and edited therefore arrives as four
// rows; the Chats list hint counts it once, and the detail section shows it as
// one card with a pill per verb.

export type ArtifactVerb = McpChatArtifactLink["verb"];

// Authoring first, then reading, then search exposure. A Record rather than a
// list, so a verb added to the API schema fails the typecheck until it is
// ranked here; chatLabels.ts labels exactly these verbs.
const ARTIFACT_VERB_RANK: Record<ArtifactVerb, number> = {
  created: 0,
  edited: 1,
  cited: 2,
  referenced: 3,
  search_match: 4,
};

export function isArtifactVerb(value: string): value is ArtifactVerb {
  return Object.hasOwn(ARTIFACT_VERB_RANK, value);
}

/** Sort key for a verb; one the schema does not know sorts last. */
export function artifactVerbRank(verb: string): number {
  return isArtifactVerb(verb)
    ? ARTIFACT_VERB_RANK[verb]
    : Object.keys(ARTIFACT_VERB_RANK).length;
}

/** The count worth showing for a verb: only when it repeated. */
export function repeatedCount(
  link: Pick<McpChatArtifactLink, "occurrence_count">,
): number | null {
  return link.occurrence_count > 1 ? link.occurrence_count : null;
}

export interface LinkedArtifactGroup {
  key: string;
  /** The most recently touched row: its title, path and time stand for the artifact. */
  latest: McpChatArtifactLink;
  /** One row per verb, in verb rank order. */
  links: McpChatArtifactLink[];
}

/** Linked-artifact rows merged per artifact, most recently touched first. */
export function groupLinkedArtifacts(
  links: readonly McpChatArtifactLink[],
): LinkedArtifactGroup[] {
  const groups = new Map<string, LinkedArtifactGroup>();
  for (const link of links) {
    const key = `${link.resource_kind}:${link.resource_id}`;
    const group = groups.get(key);
    if (!group) {
      groups.set(key, { key, latest: link, links: [link] });
      continue;
    }
    group.links.push(link);
    if (Date.parse(link.last_occurred_at) > Date.parse(group.latest.last_occurred_at)) {
      group.latest = link;
    }
  }
  const ordered = Array.from(groups.values());
  for (const group of ordered) {
    group.links.sort((a, b) => artifactVerbRank(a.verb) - artifactVerbRank(b.verb));
  }
  return ordered.sort(
    (a, b) => Date.parse(b.latest.last_occurred_at) - Date.parse(a.latest.last_occurred_at),
  );
}

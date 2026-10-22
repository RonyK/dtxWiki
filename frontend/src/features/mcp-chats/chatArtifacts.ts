import type { McpChatArtifactLink } from "../../shared/api/operations";

// The server keeps one link row per (artifact, verb) -- graph and trail
// projections read the verbs separately, so the rows must stay apart there.
// A document that was found, read, cited and edited therefore arrives as four
// rows; the list card and the detail section show it once, with one pill per
// verb: authoring first, then reading, then search exposure.
const ARTIFACT_VERB_ORDER = ["created", "edited", "cited", "referenced", "search_match"];

export interface LinkedArtifactGroup {
  key: string;
  /** The most recently touched row: its title, path and time stand for the artifact. */
  latest: McpChatArtifactLink;
  /** One row per verb, in ARTIFACT_VERB_ORDER. */
  links: McpChatArtifactLink[];
}

export function artifactVerbRank(verb: string): number {
  const rank = ARTIFACT_VERB_ORDER.indexOf(verb);
  return rank === -1 ? ARTIFACT_VERB_ORDER.length : rank;
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

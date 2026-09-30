import type { TreeNodePayload } from "../api/workspace";

/**
 * The section root ("Context", "Agent", "Library") is an implementation detail
 * of the tree, not a place users pick. TreeRail hides it from the explorer and
 * NewItemDialog hides it from the location picker (where its synthetic root row
 * already represents the same destination) — both must agree, so the rule lives
 * here rather than being re-derived per surface.
 */
export function collectHiddenSystemRootNodeIds(
  nodes: readonly TreeNodePayload[],
  surfaceTitle: string,
) {
  const topLevelSystemFolders = nodes.filter(
    (node) =>
      node.parent_id === null &&
      node.is_system_node &&
      node.target_id === null &&
      node.can_have_children,
  );
  return new Set(
    topLevelSystemFolders
      .filter((node) => isSemanticSectionRoot(node, surfaceTitle))
      .map((node) => node.id),
  );
}

export function isCanonicalSectionRoot(node: TreeNodePayload) {
  // A section root's slug matches its section. The former
  // `section === "library" && (slug === "library" || slug === "library")`
  // disjunct was a copy-paste no-op fully subsumed by this check (C21).
  return node.slug === node.section;
}

export function isSemanticSectionRoot(
  node: TreeNodePayload,
  surfaceTitle: string,
) {
  return (
    node.parent_id === null &&
    node.is_system_node &&
    node.target_id === null &&
    node.can_have_children &&
    (isCanonicalSectionRoot(node) ||
      isLegacySectionRoot(node) ||
      isNamedSurfaceRoot(node, surfaceTitle))
  );
}

const LEGACY_SECTION_ROOT_SLUGS: Readonly<Record<string, readonly string[]>> = {
  context: ["content"],
  library: ["source-artifacts", "sources-artifacts", "sourcesartifacts"],
};

function isLegacySectionRoot(node: TreeNodePayload) {
  const normalizedSlug = normalizeRootTitle(node.slug ?? "");
  return (LEGACY_SECTION_ROOT_SLUGS[node.section] ?? []).some(
    (slug) => normalizeRootTitle(slug) === normalizedSlug,
  );
}

function isNamedSurfaceRoot(node: TreeNodePayload, surfaceTitle: string) {
  const normalizedNodeTitle = normalizeRootTitle(node.title);
  const normalizedSurfaceTitle = normalizeRootTitle(surfaceTitle);
  if (normalizedNodeTitle === normalizedSurfaceTitle) {
    return true;
  }
  return (
    (node.section === "library" &&
      (normalizedNodeTitle === "sourcesartifacts" ||
        normalizedNodeTitle === "sourceartifacts") &&
      normalizedSurfaceTitle === "library") ||
    (node.section === "context" &&
      normalizedNodeTitle === "content" &&
      normalizedSurfaceTitle === "context")
  );
}

function normalizeRootTitle(value: string) {
  return value.toLowerCase().replace(/[^a-z0-9]/g, "");
}

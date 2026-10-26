import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useLocation, MemoryRouter } from "react-router-dom";
import { expect, it } from "vitest";

import "../../../shared/i18n";
import type { TreeNodePayload } from "../../../shared/api/workspace";
import {
  RoutingIndexPreview,
  routingIndexLinkTargets,
} from "../RoutingIndexPreview";

const workspaceId = "11111111-1111-4111-8111-111111111111";
const pageId = "22222222-2222-4222-8222-222222222222";
const libraryItemId = "33333333-3333-4333-8333-333333333333";
const contextFolderId = "44444444-4444-4444-8444-444444444444";
const agentFolderId = "55555555-5555-4555-8555-555555555555";

function treeNode(overrides: Partial<TreeNodePayload> = {}): TreeNodePayload {
  return {
    id: "node-id",
    section: "context",
    title: "Node",
    slug: "node",
    node_kind: "folder",
    parent_id: null,
    sort_key: "0001",
    nav_summary: null,
    target_kind: null,
    target_id: null,
    is_system_node: false,
    can_have_children: true,
    primary_placement: true,
    visibility_status: "visible",
    children_count: 0,
    breadcrumb: [],
    ...overrides,
  };
}

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{location.pathname}</output>;
}

it("routes only visible generated index locators through the explicit workspace", async () => {
  const nodes = [
    treeNode({ id: "context-root", slug: "context", node_kind: "section_root" }),
    treeNode({
      id: contextFolderId,
      slug: "회의록",
      parent_id: "context-root",
    }),
    treeNode({
      id: "context-page-ref",
      node_kind: "page_ref",
      parent_id: contextFolderId,
      target_kind: "context_page",
      target_id: pageId,
    }),
    treeNode({
      id: "library-root",
      section: "library",
      slug: "library",
      node_kind: "section_root",
    }),
    treeNode({
      id: "library-item-ref",
      section: "library",
      slug: null,
      node_kind: "item_ref",
      parent_id: "library-root",
      target_kind: "library_item",
      target_id: libraryItemId,
    }),
    treeNode({
      id: "agent-root",
      section: "agent",
      slug: "agent",
      node_kind: "section_root",
    }),
    treeNode({
      id: agentFolderId,
      section: "agent",
      slug: "persona",
      parent_id: "agent-root",
    }),
    treeNode({
      id: "hidden-folder",
      slug: "비공개",
      parent_id: "context-root",
      visibility_status: "restricted",
    }),
  ];

  const targets = routingIndexLinkTargets(workspaceId, nodes);
  const locatorByRoute = new Map(targets.map((target) => [target.locator, target.href]));
  expect(locatorByRoute.get("node:/context/회의록")).toBe(
    `/workspaces/${workspaceId}/context/folders/${contextFolderId}`,
  );
  expect(locatorByRoute.get("node:/agent/persona")).toBe(
    `/workspaces/${workspaceId}/agent/folders/${agentFolderId}`,
  );
  expect(locatorByRoute.get(`/p/${workspaceId}/${pageId}`)).toBe(
    `/workspaces/${workspaceId}/context/pages/${pageId}`,
  );
  expect(locatorByRoute.get(`/library/${libraryItemId}.md`)).toBe(
    `/workspaces/${workspaceId}/library/items/${libraryItemId}`,
  );
  expect(locatorByRoute.has("node:/context/비공개")).toBe(false);

  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={[`/workspaces/${workspaceId}/context`]}>
      <LocationProbe />
      <RoutingIndexPreview
        body={[
          "* [회의록/](node:/context/회의록)",
          `* [Policy](/p/${workspaceId}/${pageId})`,
          `* [Brief](/library/${libraryItemId}.md)`,
          "* [Agent folder](node:/agent/persona)",
          "* [Missing locator](node:/context/없는-폴더)",
        ].join("\n")}
        nodes={nodes}
        workspaceId={workspaceId}
      />
    </MemoryRouter>,
  );

  expect(screen.getByRole("link", { name: "Missing locator" })).toHaveAttribute(
    "href", "node:/context/%EC%97%86%EB%8A%94-%ED%8F%B4%EB%8D%94",
  );
  await user.click(screen.getByRole("link", { name: /회의록/ }));
  expect(screen.getByTestId("location")).toHaveTextContent(
    `/workspaces/${workspaceId}/context/folders/${contextFolderId}`,
  );
  await user.click(screen.getByRole("link", { name: "Policy" }));
  expect(screen.getByTestId("location")).toHaveTextContent(
    `/workspaces/${workspaceId}/context/pages/${pageId}`,
  );
  await user.click(screen.getByRole("link", { name: "Brief" }));
  expect(screen.getByTestId("location")).toHaveTextContent(
    `/workspaces/${workspaceId}/library/items/${libraryItemId}`,
  );
  await user.click(screen.getByRole("link", { name: "Agent folder" }));
  expect(screen.getByTestId("location")).toHaveTextContent(
    `/workspaces/${workspaceId}/agent/folders/${agentFolderId}`,
  );
});

import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useWorkspaceGraphCapability, type WorkspaceGraphCapabilityState } from "../useWorkspaceGraphCapability";
import { WorkspaceGraphGate } from "../WorkspaceGraphGate";

const { capabilityState, labs } = vi.hoisted(() => ({
  capabilityState: { current: null as unknown as WorkspaceGraphCapabilityState },
  labs: { knowledgeMap: true, ready: true, status: "ready" },
}));

vi.mock("../../../shared/hooks/useLabsPreferences", () => ({
  useLabsPreferences: () => ({ preferences: labs, ready: labs.ready, status: labs.status }),
}));
vi.mock("../useWorkspaceGraphCapability", () => ({
  invalidateWorkspaceGraphCapability: vi.fn(),
  useWorkspaceGraphCapability: vi.fn(() => capabilityState.current),
}));
vi.mock("../../../shared/workspace/WorkspaceContext", () => ({
  useWorkspaceContext: () => ({ selectedId: "11111111-1111-4111-8111-111111111111" }),
  useOptionalWorkspaceContext: () => null,
}));

function ready(
  reason: "enabled" | "disabled_by_workspace" | "service_unavailable",
  canManage: boolean,
): WorkspaceGraphCapabilityState {
  return {
    status: "ready",
    enabled: reason === "enabled",
    capability: {
      workspace_map: {
        effective_enabled: reason === "enabled",
        workspace_enabled: reason !== "disabled_by_workspace",
        service_available: reason !== "service_unavailable",
        reason,
      },
      usage_relation_signals_enabled: true,
      can_manage: canManage,
    },
  };
}

function renderGate() {
  return render(gateElement());
}

function gateElement() {
  return (
    <MemoryRouter>
      <WorkspaceGraphGate>
        <div data-testid="knowledge-map-screen" />
      </WorkspaceGraphGate>
    </MemoryRouter>
  );
}

describe("WorkspaceGraphGate", () => {
  beforeEach(() => {
    labs.knowledgeMap = true;
    labs.ready = true;
    labs.status = "ready";
    capabilityState.current = { status: "loading", capability: null, enabled: false };
  });

  it("keeps the canvas unmounted while off and points the owner to Labs", () => {
    capabilityState.current = ready("enabled", true);
    labs.knowledgeMap = false;
    const view = renderGate();

    expect(screen.queryByTestId("knowledge-map-screen")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Settings" })).toHaveAttribute("href", "/workspaces/11111111-1111-4111-8111-111111111111/settings?section=labs");
    expect(useWorkspaceGraphCapability).toHaveBeenLastCalledWith("11111111-1111-4111-8111-111111111111");

    labs.knowledgeMap = true;
    view.rerender(gateElement());
    expect(screen.getByTestId("knowledge-map-screen")).toBeInTheDocument();
    expect(useWorkspaceGraphCapability).toHaveBeenLastCalledWith("11111111-1111-4111-8111-111111111111");

    labs.knowledgeMap = false;
    view.rerender(gateElement());
    expect(screen.queryByTestId("knowledge-map-screen")).not.toBeInTheDocument();
    expect(useWorkspaceGraphCapability).toHaveBeenLastCalledWith("11111111-1111-4111-8111-111111111111");
  });

  it("does not mount the map while Labs is loading or after its load fails", () => {
    labs.ready = false;
    labs.status = "loading";
    const view = renderGate();

    expect(screen.queryByTestId("knowledge-map-screen")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Checking whether the Knowledge Map is on for this workspace",
    );
    labs.status = "error";
    view.rerender(gateElement());
    expect(screen.getByRole("heading", { name: "We couldn't find that page" })).toBeInTheDocument();
    expect(screen.queryByTestId("knowledge-map-screen")).not.toBeInTheDocument();
  });

  it("mounts the map when the workspace has it on", () => {
    capabilityState.current = ready("enabled", true);

    renderGate();

    expect(screen.getByTestId("knowledge-map-screen")).toBeInTheDocument();
  });

  it("points an owner to Settings when the workspace has it off", () => {
    labs.knowledgeMap = false;
    capabilityState.current = ready("disabled_by_workspace", true);

    renderGate();

    expect(screen.queryByTestId("knowledge-map-screen")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Settings" })).toHaveAttribute(
      "href",
      "/workspaces/11111111-1111-4111-8111-111111111111/settings?section=labs",
    );
  });

  it("blocks member direct URLs even if a cached map capability is available", () => {
    labs.ready = false;
    labs.status = "idle";
    capabilityState.current = ready("enabled", true);

    renderGate();

    expect(screen.getByRole("heading", { name: "We couldn't find that page" })).toBeInTheDocument();
    expect(screen.queryByTestId("knowledge-map-screen")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Open Settings" })).not.toBeInTheDocument();
  });

  it("does not send an owner to Settings when the server has it off", () => {
    capabilityState.current = ready("service_unavailable", true);

    renderGate();

    expect(screen.getByRole("alert")).toHaveTextContent(
      "temporarily turned off on this server",
    );
    expect(screen.queryByRole("link", { name: "Open Settings" })).not.toBeInTheDocument();
  });
});

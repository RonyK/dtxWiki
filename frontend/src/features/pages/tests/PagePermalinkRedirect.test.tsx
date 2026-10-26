import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { createReferenceReturnState } from "../../../shared/lib/navigationRecovery";
import { PagePermalinkRedirect } from "../PagePermalinkRedirect";

const WORKSPACE_ID = "22222222-2222-4222-8222-000000000001";
const PAGE_ID = "33333333-3333-4333-8333-333333333333";
const SOURCE = `/workspaces/${WORKSPACE_ID}/context/pages/33333333-3333-4333-8333-333333333334`;

function LocationStateProbe() {
  const location = useLocation();
  return (
    <output data-testid="location-state">
      {JSON.stringify({ pathname: location.pathname, search: location.search, hash: location.hash, state: location.state })}
    </output>
  );
}

describe("PagePermalinkRedirect", () => {
  it("preserves trusted router state while redirecting the short UUID permalink", async () => {
    render(
      <MemoryRouter
        initialEntries={[{
          pathname: `/p/${WORKSPACE_ID}/${PAGE_ID}`,
          search: "?tab=history",
          hash: "#v2",
          state: createReferenceReturnState(SOURCE),
        }]}
      >
        <Routes>
          <Route path="/p/:workspaceId/:pageId" element={<PagePermalinkRedirect />} />
          <Route path="*" element={<LocationStateProbe />} />
        </Routes>
      </MemoryRouter>,
    );

    const output = await screen.findByTestId("location-state");
    expect(JSON.parse(output.textContent ?? "{}")).toEqual({
      pathname: `/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`,
      search: "?tab=history",
      hash: "#v2",
      state: createReferenceReturnState(SOURCE),
    });
  });
});

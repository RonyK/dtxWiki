import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { PagePermalinkRedirect } from "../PagePermalinkRedirect";

const WORKSPACE_ID = "22222222-2222-4222-8222-000000000001";
const PAGE_ID = "33333333-3333-4333-8333-333333333333";

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="location">{JSON.stringify(location)}</output>;
}

describe("PagePermalinkRedirect", () => {
  it("preserves allowed query, fragment, and router state for the explicit workspace permalink", async () => {
    const state = { from: "shared", keep: true };
    render(
      <MemoryRouter initialEntries={[{
        pathname: `/p/${WORKSPACE_ID}/${PAGE_ID}`,
        search: "?tab=history",
        hash: "#v2",
        state,
      }]}>
        <Routes>
          <Route path="/p/:workspaceId/:pageId" element={<PagePermalinkRedirect />} />
          <Route path="*" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>,
    );

    const location = JSON.parse((await screen.findByTestId("location")).textContent ?? "{}");
    expect(location.pathname).toBe(`/workspaces/${WORKSPACE_ID}/context/pages/${PAGE_ID}`);
    expect(location.search).toBe("?tab=history");
    expect(location.hash).toBe("#v2");
    expect(location.state).toEqual(state);
  });

  it.each([
    "/p/not-a-workspace/33333333-3333-4333-8333-333333333333",
    "/p/22222222-2222-4222-8222-000000000001/not-a-page",
  ])("rejects a malformed permalink %s", async (entry) => {
    render(
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/p/:workspaceId/:pageId" element={<PagePermalinkRedirect />} />
          <Route path="*" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByRole("heading", { name: "We couldn't find that page" })).toBeInTheDocument();
  });

  it("rejects legacy object identity in the query string", async () => {
    render(
      <MemoryRouter initialEntries={[`/p/${WORKSPACE_ID}/${PAGE_ID}?target=${PAGE_ID}`]}>
        <Routes>
          <Route path="/p/:workspaceId/:pageId" element={<PagePermalinkRedirect />} />
          <Route path="*" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByRole("heading", { name: "We couldn't find that page" })).toBeInTheDocument();
  });
});

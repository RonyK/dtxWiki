import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "jest-axe";
import { useEffect } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { i18n } from "../../../shared/i18n";
import {
  useWorkspaceContext,
  WorkspaceProvider,
} from "../../../shared/workspace/WorkspaceContext";
import { SearchScreen } from "../SearchScreen";

const BASE = "http://127.0.0.1:21080";
const WORKSPACE_ID = "workspace-1";

describe("SearchScreen", () => {
  beforeEach(async () => {
    vi.restoreAllMocks();
    await i18n.changeLanguage("en");
  });

  it("submits keyword search and renders results in server order", async () => {
    let searchBody: unknown = null;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/search`) {
        searchBody = JSON.parse(String(init?.body));
        return jsonResponse({
          data: {
            ranked_results: [
              result("context-1", "context_page", "Decision Log", "", 1, {
                match_reason: "trigram",
                matched_name: "Decision Log",
                location: ["Team", "Decisions"],
              }),
              result("source-1", "library_item", "Library Source", "Supporting hit", 0.7, {
                concept_path: "/library/source-1.md",
                match_reason: "keyword",
              }),
              result("context-2", "context_page", "Decision Log", "Another document with the same title", 0.6, {
                match_reason: "exact_title",
                location: ["Team", "Decisions"],
              }),
              result("change-1", "change_set", "Review Change", "Workflow hit", 0.4, {
                source_ref: { kind: "review_item", id: "review-1" },
              }),
            ],
            supported_retrieval_modes: ["keyword"],
            permission_note: "RAW BACKEND PERMISSION NOTE",
            warnings: ["permission_limited", "unsupported_filter_tag"],
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    const { container } = render(
      <MemoryRouter initialEntries={["/workspaces/workspace-1/search"]}>
        <SearchScreen />
      </MemoryRouter>,
    );

    expect(screen.getByLabelText("Query")).toHaveAttribute("type", "search");
    expect(screen.getByLabelText("Query")).toHaveAttribute("maxLength", "2048");
    expect(screen.getByRole("button", { name: "Search" })).toHaveAttribute(
      "aria-busy",
      "false",
    );
    await userEvent.type(screen.getByLabelText("Query"), "needle");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));

    await screen.findByRole("link", { name: "Open Library Source" });
    const rows = Array.from(container.querySelectorAll(".operations-list > .search-result-row"));
    expect(rows.map((row) => row.getAttribute("href"))).toEqual([
      "/workspaces/workspace-1/context/pages/context-1",
      "/workspaces/workspace-1/library/items/source-1",
      "/workspaces/workspace-1/context/pages/context-2",
      "/workspaces/workspace-1/review/review-1",
    ]);
    for (const row of screen.getAllByRole("link", { name: "Open Decision Log" })) {
      expect(row.querySelector(".search-result-location")).toHaveTextContent(/Team.*Decisions/);
    }
    expect(searchBody).toEqual({
      query: "needle",
      scope: "all",
      filters: { include_agents: false },
      retrieval_mode: "keyword",
      limit: 20,
    });
    expect(screen.getByText("Results")).toBeInTheDocument();
    expect(screen.queryByText("Supporting")).not.toBeInTheDocument();
    expect(screen.queryByText("Workflow")).not.toBeInTheDocument();
    expect(container.querySelector(".search-result-reason")).toHaveTextContent("Decision Log");
    expect(
      screen.getByText(
        "Restricted Library items and artifacts are hidden for your current permissions · Tag filter not applied yet",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("RAW BACKEND PERMISSION NOTE")).not.toBeInTheDocument();
    expect(screen.queryByText("Keyword", { exact: true })).not.toBeInTheDocument();
    // M-11 (260908 UI audit): ".md" is a backend identifier, not a real file.
    expect(
      screen.getByRole("link", { name: "Open Library Source" })
        .querySelector(".search-result-location"),
    ).toHaveTextContent("/library/source-1");
    expect(screen.queryByText("/library/source-1.md")).not.toBeInTheDocument();
    expect(screen.getByText("4 results")).toBeInTheDocument();
    // The header states the result count; the rows themselves carry no chips.
    expect(container.querySelectorAll(".search-result-row .count-chip")).toHaveLength(0);
    expect(container.querySelector(".score-pill")).toBeNull();
    expect(screen.queryByText("1.00")).not.toBeInTheDocument();
    expect((await axe(container)).violations).toEqual([]);
  });

  it("renders permission advisories from the Korean warning-code translation", async () => {
    await i18n.changeLanguage("ko");
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/search`) {
        return jsonResponse({
          data: {
            ranked_results: [],
            supported_retrieval_modes: ["keyword"],
            permission_note: "Restricted Library items and artifacts were excluded.",
            warnings: ["permission_limited"],
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/workspace-1/search"]}>
        <SearchScreen />
      </MemoryRouter>,
    );

    await userEvent.type(screen.getByLabelText("검색어"), "needle");
    await userEvent.click(screen.getByRole("button", { name: "검색" }));

    expect(
      await screen.findByText(
        "현재 권한으로 볼 수 없는 제한된 라이브러리 항목과 자료는 검색 결과에서 제외되었습니다",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Restricted Library items and artifacts were excluded."),
    ).not.toBeInTheDocument();
  });

  it("shows the retrieval-mode selector only when the server supports more than keyword and sends the chosen mode", async () => {
    const user = userEvent.setup();
    const bodies: Array<{ retrieval_mode?: string }> = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/search/capabilities`) {
        return jsonResponse({
          data: { supported_retrieval_modes: ["keyword", "semantic", "hybrid"] },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/search`) {
        bodies.push(JSON.parse(String(init?.body)));
        return jsonResponse({
          data: {
            ranked_results: [result("c1", "context_page", "Mode Result", "hit", 1)],
            supported_retrieval_modes: ["keyword", "semantic", "hybrid"],
            permission_note: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/workspace-1/search"]}>
        <SearchScreen />
        <LocationProbe />
      </MemoryRouter>,
    );

    const modeSelect = await screen.findByLabelText("Mode");

    await user.type(screen.getByLabelText("Query"), "needle");
    await user.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("Mode Result")).toBeInTheDocument();
    expect(bodies[0]).toMatchObject({ retrieval_mode: "keyword" });

    // Now that the server advertises 3 modes, the selector appears; choosing
    // semantic sends retrieval_mode=semantic (RAG reachable from the UI — I4).
    await user.selectOptions(modeSelect, "semantic");
    await waitFor(() =>
      expect(bodies[bodies.length - 1]).toMatchObject({
        retrieval_mode: "semantic",
      }),
    );
    expect(screen.getByTestId("location")).toHaveTextContent(
      "?q=needle&mode=semantic",
    );
  });

  it("renders a destination tone and chevron for a linkable result", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/search`) {
        return jsonResponse({
          data: {
            ranked_results: [
              result("source-1", "library_item", "Source Title", "Hit", 0.9),
            ],
            supported_retrieval_modes: ["keyword"],
            permission_note: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/workspace-1/search"]}>
        <SearchScreen />
      </MemoryRouter>,
    );

    await user.type(screen.getByLabelText("Query"), "source");
    await user.click(screen.getByRole("button", { name: "Search" }));

    expect(await screen.findByText("Source Title")).toBeInTheDocument();
    const row = screen.getByRole("link", { name: "Open Source Title" });
    expect(row.querySelector(".search-result-kind")).toHaveAttribute("data-identity", "library");
    expect(row.querySelector(".search-result-chevron")).toHaveAttribute("aria-hidden", "true");
    expect(within(row).queryByText("Open Library")).not.toBeInTheDocument();
    expect(screen.queryByText("library_item")).not.toBeInTheDocument();
  });

  it("keeps destination tones and links for claim, feedback, change, and artifact results", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/search`) {
        return jsonResponse({
          data: {
            ranked_results: [
              result("claim-1", "context_claim", "Claim Hit", "Claim hit", 1, {
                source_ref: { kind: "context_page", id: "page-1" },
              }),
              result("feedback-1", "feedback", "Feedback Hit", "Feedback hit", 0.9, {
                source_ref: { kind: "library_item", id: "item-1" },
              }),
              result("change-1", "change_set", "Change Hit", "Change hit", 0.8, {
                source_ref: { kind: "review_item", id: "review-1" },
              }),
              result("artifact-1", "library_artifact", "Artifact Hit", "Artifact hit", 0.7, {
                concept_path: "/workspaces/workspace-1/library/item-1.md",
                source_ref: { kind: "library_artifact", id: "artifact-1" },
              }),
            ],
            supported_retrieval_modes: ["keyword"],
            permission_note: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/workspace-1/search"]}>
        <SearchScreen />
      </MemoryRouter>,
    );

    await user.type(screen.getByLabelText("Query"), "needle");
    await user.click(screen.getByRole("button", { name: "Search" }));

    expect(await screen.findByRole("link", { name: "Open Claim Hit" }))
      .toHaveAttribute("href", "/workspaces/workspace-1/context/pages/page-1");
    expect(screen.getByRole("link", { name: "Open Feedback Hit" }))
      .toHaveAttribute("href", "/workspaces/workspace-1/library/items/item-1");
    expect(screen.getByRole("link", { name: "Open Change Hit" }))
      .toHaveAttribute("href", "/workspaces/workspace-1/review/review-1");
    expect(screen.getByRole("link", { name: "Open Artifact Hit" }))
      .toHaveAttribute("href", "/workspaces/workspace-1/library/artifacts/artifact-1");
    for (const [title, destination] of [
      ["Claim Hit", "context"],
      ["Feedback Hit", "library"],
      ["Change Hit", "review"],
      ["Artifact Hit", "library"],
    ]) {
      const row = screen.getByRole("link", { name: `Open ${title}` });
      expect(row.querySelector(".search-result-kind")).toHaveAttribute("data-identity", destination);
      expect(row.querySelector(".search-result-chevron")).toHaveAttribute("aria-hidden", "true");
    }
    expect(screen.queryByText("Open unavailable")).not.toBeInTheDocument();
  });

  it("renders accessible C2 search cards with a selected workspace and honest unavailable states", async () => {
    const pageId = "aa123456-1111-4111-8111-111111111111";
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: {
          current_workspace_id: WORKSPACE_ID,
          workspaces: [{
            id: WORKSPACE_ID, slug: "selected-workspace", title: "Selected workspace",
            name: "Selected workspace", goal: "", default_language: "en", visibility: "private",
          }],
        } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/search/capabilities`) {
        return jsonResponse({ data: { supported_retrieval_modes: ["keyword"] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/search`) {
        return jsonResponse({ data: {
          ranked_results: [
            result(pageId, "context_page", "Embedding policy", "", 1, {
              source_ref: { kind: "context_page", id: pageId },
              concept_path: `/workspaces/workspace-1/context/p-${pageId}.md`,
              matched_name: "Embedding policy", match_reason: "exact_title",
              summary: "**Embedding** policy explains [retrieval](https://example.com).",
              location: ["Product", "Research", "Very long document title kept in the accessible description", "Search", "Policies"],
              stale: true, citation_available: false,
            }),
            result("human-path", "context_page", "Search handbook", "A **clean** `snippet`. model_name and model_version. Keep x < 10 and y > 2. `a*b*c` and _emphasis_. vector<T>.", 0.8, {
              concept_path: "/context/search-handbook.md", match_reason: "keyword",
              heading_path: ["Search handbook", "Storage"],
            }),
            // Old API responses and explicit audit scopes may still carry
            // unmapped results, even though ordinary all searches exclude them.
            result("unmapped-change", "change_set", "Unmapped change", "", 0.7),
            result("unmapped-artifact", "library_artifact", "Unmapped artifact", "", 0.6),
          ],
          supported_retrieval_modes: ["keyword"],
        } });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    const { container } = render(
      <MemoryRouter initialEntries={["/workspaces/workspace-1/search?q=Embedding%20policy"]}>
        <WorkspaceProvider>
          <WorkspaceSearchHarness />
        </WorkspaceProvider>
      </MemoryRouter>,
    );

    const row = await screen.findByRole("link", { name: "Open Embedding policy" });
    expect(row).toHaveAttribute("href", `/workspaces/${WORKSPACE_ID}/context/pages/${pageId}`);
    expect(row.querySelector(".search-result-kind")).toHaveAttribute("data-identity", "context");
    expect(row.querySelector(".search-result-chevron")).toHaveAttribute("aria-hidden", "true");
    expect(within(row).queryByText("Open unavailable")).not.toBeInTheDocument();
    expect(within(row).queryByText("Matched name: Embedding policy")).not.toBeInTheDocument();
    expect(within(row).getByText("Embedding policy explains retrieval.")).toBeInTheDocument();
    const title = row.querySelector(".search-result-title");
    const snippet = row.querySelector(".search-result-snippet");
    const location = row.querySelector(".search-result-location");
    const reason = row.querySelector(".search-result-reason");
    expect(location).toHaveTextContent("Product");
    expect(location).toHaveTextContent("Search");
    expect(reason).toHaveTextContent("Title match");
    expect(title!.compareDocumentPosition(snippet!)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(snippet!.compareDocumentPosition(location!)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(location!.compareDocumentPosition(reason!)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(row).toHaveAccessibleDescription(
      "Product › Research › Very long document title kept in the accessible description › Search › Policies. Title match",
    );
    expect(row.querySelector("nav, a, button")).toBeNull();
    expect(container.textContent).not.toContain(`/workspaces/workspace-1/context/p-${pageId}`);
    const handbook = screen.getByRole("link", { name: "Open Search handbook" });
    expect(handbook).toHaveTextContent("/context/search-handbook");
    expect(handbook).toHaveTextContent("A clean snippet.");
    expect(handbook).toHaveTextContent("model_name and model_version. Keep x < 10 and y > 2. a*b*c and emphasis. vector<T>.");
    expect(handbook.querySelector(".search-result-snippet strong")).toHaveTextContent("Storage");
    expect(within(handbook).queryByText("Keyword", { exact: true })).not.toBeInTheDocument();
    const unavailable = container.querySelectorAll(".search-result-row.is-disabled");
    expect(unavailable).toHaveLength(1);
    expect(unavailable[0].tagName).toBe("DIV");
    expect(unavailable[0]).toHaveTextContent(/Cannot open directly from search.*review/i);
    expect(unavailable[0].querySelector(".search-result-chevron")).toBeNull();
    expect(screen.queryByRole("link", { name: "Open Unmapped change" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Unmapped artifact" })).toHaveAttribute("href", `/workspaces/${WORKSPACE_ID}/library/artifacts/unmapped-artifact`);
    expect(container.querySelector(".search-source-chip")).toBeNull();
    expect(screen.queryByText("No citation")).not.toBeInTheDocument();
    expect(screen.queryByText("stale", { exact: true })).not.toBeInTheDocument();
    expect((await axe(container)).violations).toEqual([]);
  });

  it("shows the exact total_count in the header chip even when the displayed list is capped", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/search`) {
        return jsonResponse({
          data: {
            ranked_results: [
              result("context-cap", "context_page", "Capped Result", "Hit", 1),
            ],
            supported_retrieval_modes: ["keyword"],
            permission_note: null,
            total_count: 42,
            has_more: true,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/workspace-1/search"]}>
        <SearchScreen />
      </MemoryRouter>,
    );

    await user.type(screen.getByLabelText("Query"), "codex");
    await user.click(screen.getByRole("button", { name: "Search" }));

    expect(await screen.findByText("Capped Result")).toBeInTheDocument();
    // total_count is the exact match count (search materializes the full ranked
    // set before truncating to the display limit), so the header chip shows the
    // exact total with no "+", not the capped ranked_results length.
    expect(screen.getByText("42 results")).toBeInTheDocument();
    expect(screen.queryByText("42+ results")).not.toBeInTheDocument();
    // L-9 (260908 UI audit): the count chip used to sit in the page header,
    // disconnected from the "Results" section label below it -- every other
    // list surface pairs its count chip directly with the section title.
    const resultsHeading = screen.getByText("Results");
    expect(
      resultsHeading.closest(".panel-header"),
    ).toContainElement(screen.getByText("42 results"));
  });

  it("opens a legacy Changes URL in All with an explanatory notice", async () => {
    const user = userEvent.setup();
    let searchBody: unknown = null;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/search`) {
        searchBody = JSON.parse(String(init?.body));
        return jsonResponse({
          data: {
            ranked_results: [],
            supported_retrieval_modes: ["keyword"],
            permission_note: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    const view = render(
      <MemoryRouter initialEntries={["/workspaces/workspace-1/search?q=review&scope=change"]}>
        <SearchScreen />
      </MemoryRouter>,
    );

    await waitFor(() =>
      expect(searchBody).toMatchObject({ query: "review", scope: "all" }),
    );
    expect(screen.getByLabelText("Scope")).toHaveValue("all");
    expect(screen.queryByRole("option", { name: "Changes" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Activity" })).toHaveAttribute("href", "/workspaces/workspace-1/activity");
    expect(screen.getByRole("link", { name: "Review" })).toHaveAttribute("href", "/workspaces/workspace-1/review");
    view.unmount();
    searchBody = null;
    render(
      <MemoryRouter initialEntries={["/workspaces/workspace-1/search?scope=change"]}>
        <SearchScreen />
        <LocationProbe />
      </MemoryRouter>,
    );
    await user.selectOptions(screen.getByLabelText("Scope"), "context_page");
    expect(screen.getByTestId("location")).toHaveTextContent("?scope=context_page");
    expect(screen.queryByText(/Showing all documents/)).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("Query"), "review");
    await waitFor(() =>
      expect(searchBody).toMatchObject({ query: "review", scope: "context_page" }),
    );
  });

  it("runs a search from an empty-state suggestion", async () => {
    const user = userEvent.setup();
    let searchBody: unknown = null;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/search`) {
        searchBody = JSON.parse(String(init?.body));
        return jsonResponse({
          data: {
            ranked_results: [
              result("context-suggestion", "context_page", "Policy Result", "Hit", 1),
            ],
            supported_retrieval_modes: ["keyword"],
            permission_note: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/workspace-1/search"]}>
        <SearchScreen />
      </MemoryRouter>,
    );

    await user.click(screen.getByRole("button", { name: "embedding policy" }));

    expect(screen.getByLabelText("Query")).toHaveValue("embedding policy");
    expect(await screen.findByText("Policy Result")).toBeInTheDocument();
    // The total is rendered once in the header, without a duplicate section chip.
    expect(screen.getAllByText("1 result")).toHaveLength(1);
    expect(searchBody).toMatchObject({ query: "embedding policy" });
  });

  it("distinguishes unavailable search endpoint from empty results", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/search`) {
        return jsonResponse(
          {
            error_code: "not_found",
            message: "raw search endpoint secret",
            request_id: "req-search",
            warnings: [],
          },
          404,
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/workspace-1/search"]}>
        <SearchScreen />
      </MemoryRouter>,
    );

    await user.type(screen.getByLabelText("Query"), "review");
    await user.click(screen.getByRole("button", { name: "Search" }));

    expect(
      await screen.findByText(/Search index is not available in this build/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("raw search endpoint secret"),
    ).not.toBeInTheDocument();
    expect(screen.getByText("Search unavailable")).toBeInTheDocument();
    expect(screen.queryByText("No search run")).not.toBeInTheDocument();
  });

  it("runs search from the q URL parameter with a disabled Agent scope normalized to all", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/search`) {
        expect(JSON.parse(String(init?.body))).toMatchObject({ query: "needle", scope: "all" });
        return jsonResponse({
          data: {
            ranked_results: [
              result("context-url", "context_page", "URL Result", "Hit", 1),
            ],
            supported_retrieval_modes: ["keyword"],
            permission_note: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/workspace-1/search?q=needle&scope=agent_page"]}>
        <SearchScreen />
      </MemoryRouter>,
    );

    expect(screen.getByLabelText("Query")).toHaveValue("needle");
    expect(await screen.findByText("URL Result")).toBeInTheDocument();
  });

  it("clears the q URL parameter while preserving scope", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/search`) {
        return jsonResponse({
          data: {
            ranked_results: [],
            supported_retrieval_modes: ["keyword"],
            permission_note: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/workspace-1/search?q=needle&scope=library_item"]}>
        <SearchScreen />
        <LocationProbe />
      </MemoryRouter>,
    );

    expect(screen.getByLabelText("Query")).toHaveValue("needle");
    expect(screen.getByLabelText("Scope")).toHaveValue("library_item");

    await user.click(screen.getByRole("button", { name: "Clear search" }));

    expect(screen.getByLabelText("Query")).toHaveValue("");
    expect(screen.getByTestId("location")).toHaveTextContent("?scope=library_item");
  });

  it("syncs scope changes to the URL while a query is active", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/search`) {
        return jsonResponse({
          data: {
            ranked_results: [],
            supported_retrieval_modes: ["keyword"],
            permission_note: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/workspace-1/search?q=needle"]}>
        <SearchScreen />
        <LocationProbe />
      </MemoryRouter>,
    );

    expect(screen.getByLabelText("Query")).toHaveValue("needle");
    expect(await screen.findByText("No results")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Scope"), "library_item");

    await waitFor(() =>
      expect(screen.getByTestId("location")).toHaveTextContent(
        "?q=needle&scope=library_item",
      ),
    );

    await user.selectOptions(screen.getByLabelText("Scope"), "all");

    await waitFor(() =>
      expect(screen.getByTestId("location").textContent).toBe("?q=needle"),
    );
  });

  it("leaves the URL untouched when changing scope without an active query", async () => {
    const user = userEvent.setup();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/workspace-1/search"]}>
        <SearchScreen />
        <LocationProbe />
      </MemoryRouter>,
    );

    await user.selectOptions(screen.getByLabelText("Scope"), "library_item");

    expect(screen.getByLabelText("Scope")).toHaveValue("library_item");
    expect(screen.getByTestId("location").textContent).toBe("");
  });

  it("debounces keyword search after query changes", async () => {
    vi.useFakeTimers();
    let searchCount = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/search`) {
        searchCount += 1;
        expect(JSON.parse(String(init?.body))).toMatchObject({
          query: "delayed",
        });
        return jsonResponse({
          data: {
            ranked_results: [
              result("context-2", "context_page", "Delayed Result", "Hit", 1),
            ],
            supported_retrieval_modes: ["keyword"],
            permission_note: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    try {
      render(
        <MemoryRouter initialEntries={["/workspaces/workspace-1/search"]}>
          <SearchScreen />
        </MemoryRouter>,
      );

      fireEvent.change(screen.getByLabelText("Query"), {
        target: { value: "delayed" },
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(299);
      });
      expect(searchCount).toBe(0);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1);
      });
      expect(screen.getByText("Delayed Result")).toBeInTheDocument();
      expect(searchCount).toBe(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not render a late search response after clearing query", async () => {
    let resolveSearch: ((response: Response) => void) | undefined;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/search`) {
        return new Promise<Response>((resolve) => {
          resolveSearch = resolve;
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/workspace-1/search"]}>
        <SearchScreen />
      </MemoryRouter>,
    );

    fireEvent.change(screen.getByLabelText("Query"), {
      target: { value: "stale" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(resolveSearch).toBeDefined());

    fireEvent.change(screen.getByLabelText("Query"), {
      target: { value: "" },
    });
    await act(async () => {
      resolveSearch?.(
        jsonResponse({
          data: {
            ranked_results: [
              result("context-stale", "context_page", "Stale Result", "Late", 1),
            ],
            supported_retrieval_modes: ["keyword"],
            permission_note: null,
          },
        }),
      );
    });

    expect(screen.queryByText("Stale Result")).not.toBeInTheDocument();
    expect(screen.getByText("Search your workspace")).toBeInTheDocument();
  });

  it("drops a stale scope response after a fast scope change", async () => {
    const responders: Array<(scope: string, response: Response) => void> = [];
    const queue: Array<{ scope: string; resolve: (response: Response) => void }> = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/search`) {
        const body = JSON.parse(String(init?.body)) as { scope: string };
        return new Promise<Response>((resolve) => {
          queue.push({ scope: body.scope, resolve });
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });
    void responders;

    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/workspaces/workspace-1/search"]}>
        <SearchScreen />
      </MemoryRouter>,
    );

    await user.type(screen.getByLabelText("Query"), "race");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(queue).toHaveLength(1));

    await user.selectOptions(screen.getByLabelText("Scope"), "context_page");
    await waitFor(() => expect(queue).toHaveLength(2));

    // Resolve the *first* (stale) request after the scope change; its payload
    // must not surface because a newer request is now in flight.
    await act(async () => {
      queue[0].resolve(
        jsonResponse({
          data: {
            ranked_results: [
              result("stale", "context_page", "Stale Scope Result", "old", 1),
            ],
            supported_retrieval_modes: ["keyword"],
            permission_note: null,
          },
        }),
      );
    });

    expect(screen.queryByText("Stale Scope Result")).not.toBeInTheDocument();

    // Resolving the fresh second request should surface its payload.
    await act(async () => {
      queue[1].resolve(
        jsonResponse({
          data: {
            ranked_results: [
              result("fresh", "context_page", "Fresh Scope Result", "new", 1),
            ],
            supported_retrieval_modes: ["keyword"],
            permission_note: null,
          },
        }),
      );
    });

    expect(await screen.findByText("Fresh Scope Result")).toBeInTheDocument();
    expect(screen.queryByText("Stale Scope Result")).not.toBeInTheDocument();
  });

  it("retries the same failed query on submit", async () => {
    let searchCount = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/search`) {
        searchCount += 1;
        if (searchCount === 1) {
          return jsonResponse(
            {
              error_code: "internal_error",
              message: "Search failed",
              request_id: "request-1",
              warnings: [],
            },
            500,
          );
        }
        return jsonResponse({
          data: {
            ranked_results: [
              result("context-retry", "context_page", "Recovered Result", "Hit", 1),
            ],
            supported_retrieval_modes: ["keyword"],
            permission_note: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/workspace-1/search"]}>
        <SearchScreen />
      </MemoryRouter>,
    );

    fireEvent.change(screen.getByLabelText("Query"), {
      target: { value: "retry" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(
      await screen.findByText(
        "The server could not complete the request. Please try again.",
      ),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    expect(await screen.findByText("Recovered Result")).toBeInTheDocument();
    expect(searchCount).toBe(2);
  });

  it("hides stale results, counts, and pagination when a same-query reload fails", async () => {
    let searchCount = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/search`) {
        searchCount += 1;
        if (searchCount === 2) {
          return jsonResponse(
            {
              error_code: "internal_error",
              message: "Search reload failed",
              request_id: "request-reload",
              warnings: [],
            },
            500,
          );
        }
        return jsonResponse({
          data: {
            ranked_results: [
              result("context-stale", "context_page", "Previously Loaded", "Hit", 1),
            ],
            next_cursor: "cursor-offset-20",
            total_count: 2,
            has_more: true,
            supported_retrieval_modes: ["keyword"],
            permission_note: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/workspace-1/search"]}>
        <SearchScreen />
      </MemoryRouter>,
    );

    await userEvent.type(screen.getByLabelText("Query"), "needle");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("Previously Loaded")).toBeInTheDocument();
    expect(screen.getByText("2 results")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Load more" })).toBeEnabled();

    await userEvent.click(screen.getByRole("button", { name: "Search" }));

    expect(await screen.findByText("Search unavailable")).toBeInTheDocument();
    expect(screen.queryByText("Previously Loaded")).not.toBeInTheDocument();
    expect(screen.queryByText("2 results")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
    expect(searchCount).toBe(2);
  });

  it("drops appended Load more pages when the same query is re-submitted", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/search`) {
        const body = JSON.parse(String(init?.body)) as { cursor?: string };
        if (body.cursor) {
          // page 2 (Load more)
          return jsonResponse({
            data: {
              ranked_results: [
                result("p2a", "context_page", "Page2 Alpha", "hit", 0.5),
                result("p2b", "context_page", "Page2 Beta", "hit", 0.4),
              ],
              next_cursor: null,
              total_count: 4,
              has_more: false,
              supported_retrieval_modes: ["keyword"],
              permission_note: null,
            },
          });
        }
        // page 1, returned for both the initial submit and the re-submit. The
        // keyset cursor is an opaque offset, identical across re-runs.
        return jsonResponse({
          data: {
            ranked_results: [
              result("p1a", "context_page", "Page1 Alpha", "hit", 1),
              result("p1b", "context_page", "Page1 Beta", "hit", 0.9),
            ],
            next_cursor: "cursor-offset-20",
            total_count: 4,
            has_more: true,
            supported_retrieval_modes: ["keyword"],
            permission_note: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/workspace-1/search"]}>
        <SearchScreen />
      </MemoryRouter>,
    );

    await userEvent.type(screen.getByLabelText("Query"), "needle");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("Page1 Alpha")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByText("Page2 Alpha")).toBeInTheDocument();

    // Re-submit the SAME query. Page 1's cursor is unchanged, so a reset keyed
    // on next_cursor would leave the stale page-2 rows mixed in. Keying on the
    // payload identity drops them.
    await userEvent.click(screen.getByRole("button", { name: "Search" }));

    await waitFor(() =>
      expect(screen.queryByText("Page2 Alpha")).not.toBeInTheDocument(),
    );
    expect(screen.queryByText("Page2 Beta")).not.toBeInTheDocument();
    expect(screen.getByText("Page1 Alpha")).toBeInTheDocument();
  });

  it("shows an error when Load more fails and keeps the button for retry", async () => {
    let loadMoreCalls = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({ data: { current_workspace_id: WORKSPACE_ID, workspaces: [] } });
      }
      if (url === `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/search`) {
        const body = JSON.parse(String(init?.body)) as { cursor?: string };
        if (body.cursor) {
          loadMoreCalls += 1;
          if (loadMoreCalls === 1) {
            return jsonResponse(
              {
                error_code: "internal_error",
                message: "boom",
                request_id: "req-load-more",
                warnings: [],
              },
              500,
            );
          }
          return jsonResponse({
            data: {
              ranked_results: [
                result("p2", "context_page", "Recovered Page2", "hit", 0.5),
              ],
              next_cursor: null,
              total_count: 2,
              has_more: false,
              supported_retrieval_modes: ["keyword"],
              permission_note: null,
            },
          });
        }
        return jsonResponse({
          data: {
            ranked_results: [
              result("p1", "context_page", "First Page", "hit", 1),
            ],
            next_cursor: "cursor-offset-20",
            total_count: 2,
            has_more: true,
            supported_retrieval_modes: ["keyword"],
            permission_note: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/workspaces/workspace-1/search"]}>
        <SearchScreen />
      </MemoryRouter>,
    );

    await userEvent.type(screen.getByLabelText("Query"), "needle");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("First Page")).toBeInTheDocument();

    // A failed Load more surfaces a localized error and leaves the button
    // available (cursor preserved) instead of silently swallowing the rejection.
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(
      await screen.findByText("Failed to load more results"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Load more" })).toBeEnabled();
    expect(screen.getByText("First Page")).toBeInTheDocument();

    // Retrying the same cursor succeeds: the page appends and the error clears.
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByText("Recovered Page2")).toBeInTheDocument();
    expect(
      screen.queryByText("Failed to load more results"),
    ).not.toBeInTheDocument();
    expect(loadMoreCalls).toBe(2);
  });
});

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function result(
  id: string,
  type: string,
  title: string,
  snippet: string,
  score: number,
  overrides: Record<string, unknown> = {},
) {
  return {
    id,
    type,
    title,
    snippet,
    score,
    citation_available: true,
    stale: false,
    archived: false,
    ...overrides,
  };
}

function LocationProbe() {
  const location = useLocation();
  return <span data-testid="location">{location.search}</span>;
}

function WorkspaceSearchHarness() {
  const { refreshWorkspaces, selectedId } = useWorkspaceContext();
  useEffect(() => {
    void refreshWorkspaces();
  }, [refreshWorkspaces]);
  return selectedId ? <SearchScreen /> : null;
}

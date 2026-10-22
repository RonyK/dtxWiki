import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatsListScreen } from "../ChatsListScreen";

const BASE = "http://127.0.0.1:21080";
const WORKSPACE_ID = "workspace-1";

describe("ChatsListScreen", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    Reflect.deleteProperty(window, "matchMedia");
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.useRealTimers();
    Reflect.deleteProperty(window, "matchMedia");
  });

  it("renders MCP chat cards with client, summary, artifacts, counts, and time", async () => {
    const chat = chatFixture();
    // A second verb row for the same page is still one linked artifact, so the
    // hint names it instead of reporting "2 linked".
    chat.linked_artifacts.push({
      ...chat.linked_artifacts[0],
      id: "artifact-link-2",
      verb: "cited",
    });
    renderWithChats({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?period=all&writes_only=false&archived=false&sort=last_activity_desc`]:
        chatsResponse([chat]),
    });

    expect(
      await screen.findByRole("heading", { name: "Chats" }),
    ).toBeInTheDocument();
    // A next cursor exists, so the chip reports a lower bound rather than
    // claiming the loaded page is the whole set.
    expect(screen.getByText("1 session")).toBeInTheDocument();
    const card = screen.getByRole("link", { name: /Codex Research Session/ });
    expect(card).toHaveAttribute("href", "/chats/chat-1");
    expect(
      within(card).getByText("Codex Research Session"),
    ).toBeInTheDocument();
    expect(within(card).getByLabelText("Codex CLI")).toBeInTheDocument();
    expect(within(card).queryByText(/Search and read activity/)).toBeNull();
    expect(within(card).getByText(/Authentication policy/)).toBeInTheDocument();
    expect(within(card).getByText("3 calls")).toBeInTheDocument();
    expect(within(card).getByText("1 write")).toBeInTheDocument();
    expect(within(card).getByLabelText("Archived")).toBeInTheDocument();
    const timeEl = card.querySelector("time");
    expect(timeEl).not.toBeNull();
    expect(timeEl).toHaveAttribute("datetime", "2026-05-27T09:03:00Z");
  });

  it("does not show a linked-artifact hint for a search-only chat", async () => {
    renderWithChats({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?period=all&writes_only=false&archived=false&sort=last_activity_desc`]:
        chatsResponse([
          {
            ...chatFixture(),
            title: "Search: retention policy",
            subject: "retention policy",
            title_source: "query",
            linked_artifacts: [],
          },
        ]),
    });

    const card = await screen.findByRole("link", {
      name: /Search: “retention policy”/,
    });
    expect(card.querySelector(".mcp-chat-card-artifacts")).toBeNull();
    expect(within(card).queryByText("Authentication policy")).toBeNull();
  });

  it("shows visible filter group labels", async () => {
    renderWithChats({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?period=all&writes_only=false&archived=false&sort=last_activity_desc`]:
        chatsResponse([]),
    });

    expect(
      await screen.findByRole("heading", { name: "Chats" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Find")).toBeInTheDocument();
    expect(screen.getByText("Period")).toBeInTheDocument();
    expect(screen.getByText("Sort")).toBeInTheDocument();
    expect(screen.getByText("View")).toBeInTheDocument();
    expect(
      screen.getByRole("radiogroup", { name: "Period" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("radiogroup", { name: "Sort" }),
    ).toBeInTheDocument();
  });

  it("announces archive success, focuses the safe list heading, and consumes navigation state", async () => {
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?period=all&writes_only=false&archived=false&sort=last_activity_desc`]:
        chatsResponse([]),
    });
    render(
      <MemoryRouter
        initialEntries={[
          {
            pathname: "/chats",
            state: { chatArchiveSuccess: { chatId: "chat-1" } },
          },
        ]}
      >
        <ChatsListScreen />
        <LocationStateProbe />
      </MemoryRouter>,
    );

    const heading = await screen.findByRole("heading", { name: "Chats" });
    expect(
      screen.getByText("Archived 1 chat. Focus moved to the Chats list."),
    ).toBeInTheDocument();
    await waitFor(() => expect(heading).toHaveFocus());
    await waitFor(() => {
      expect(screen.getByTestId("location-state")).toHaveTextContent("cleared");
    });
  });

  it("does not steal focus when a passive list load settles", async () => {
    let resolveChats!: (value: unknown) => void;
    const pendingChats = new Promise<unknown>((resolve) => {
      resolveChats = resolve;
    });
    renderWithChats({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?period=all&writes_only=false&archived=false&sort=last_activity_desc`]:
        () => pendingChats,
    });

    const search = screen.getByLabelText("Search Chats");
    search.focus();
    expect(search).toHaveFocus();
    resolveChats(chatsResponse([chatFixture()]));

    expect(
      await screen.findByRole("link", { name: /Codex Research Session/ }),
    ).toBeInTheDocument();
    expect(search).toHaveFocus();
  });

  it("keeps client scopes and the full max-length identity out of the list chip", async () => {
    const fullName = "OAuth-client-".repeat(20).slice(0, 200);
    renderWithChats({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?period=all&writes_only=false&archived=false&sort=last_activity_desc`]:
        chatsResponse([
          {
            ...chatFixture(),
            client: {
              ...chatFixture().client,
              name: fullName,
              scopes: ["mcp:read", "mcp:write", "secret:scope"],
            },
          },
        ]),
    });

    const card = await screen.findByRole("link", {
      name: /Codex Research Session/,
    });
    const chip = card.querySelector(".mcp-client-chip");
    expect(chip).not.toBeNull();
    expect(chip).not.toHaveAttribute("title");
    expect(chip).not.toHaveAccessibleName(fullName);
    expect(chip?.getAttribute("aria-label")?.length).toBeLessThanOrEqual(48);
    expect(card).not.toHaveTextContent("mcp:read");
    expect(card).not.toHaveTextContent("secret:scope");
  });

  it("distinguishes the Writes-only empty state from a general filtered result", async () => {
    renderWithChats(
      {
        [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?period=all&writes_only=true&archived=false&sort=last_activity_desc`]:
          chatsResponse([]),
      },
      "/chats?writes_only=true",
    );

    expect(await screen.findByText("No chats with writes")).toBeInTheDocument();
    expect(screen.queryByText("No chats match these filters")).toBeNull();
  });

  it("offers a localized retry without rendering a false empty state", async () => {
    const user = userEvent.setup();
    let listCalls = 0;
    renderWithChats({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?period=all&writes_only=false&archived=false&sort=last_activity_desc`]:
        () => {
          listCalls += 1;
          if (listCalls === 1) {
            throw new Error("raw request req-private");
          }
          return chatsResponse([chatFixture()]);
        },
    });

    expect(await screen.findByText("Failed to load Chats")).toBeInTheDocument();
    expect(screen.queryByText(/req-private/)).toBeNull();
    expect(screen.queryByText("No external chats yet")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Retry" }));

    expect(
      await screen.findByRole("link", { name: /Codex Research Session/ }),
    ).toBeInTheDocument();
    expect(listCalls).toBe(2);
  });

  it("adds date ranges to rolling bucket labels while preserving bucket order", async () => {
    // Pin the clock to midday so daysAgoIso()/rollingRangeLabel() and the
    // component's own `new Date()` calls cannot disagree across a real-clock
    // midnight crossing. shouldAdvanceTime keeps waitFor/findBy polling alive.
    vi.useFakeTimers({
      now: new Date(2026, 4, 27, 12, 0, 0),
      shouldAdvanceTime: true,
    });
    const today = {
      ...chatFixture(),
      id: "chat-today",
      title: "Today session",
      last_activity_at: daysAgoIso(0),
    };
    const yesterday = {
      ...chatFixture(),
      id: "chat-yesterday",
      title: "Yesterday session",
      last_activity_at: daysAgoIso(1),
    };
    const last7 = {
      ...chatFixture(),
      id: "chat-last-7",
      title: "Earlier this week",
      last_activity_at: daysAgoIso(3),
    };
    const older = {
      ...chatFixture(),
      id: "chat-older",
      title: "Older session",
      last_activity_at: daysAgoIso(40),
    };
    renderWithChats({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?period=all&writes_only=false&archived=false&sort=last_activity_desc`]:
        chatsResponse([older, last7, yesterday, today]),
    });

    const todayHeader = await screen.findByText("Today");
    const yesterdayHeader = screen.getByText("Yesterday");
    const last7Header = screen.getByText(
      `Last 7 days - ${rollingRangeLabel(6)}`,
    );
    const olderHeader = screen.getByText("Older");
    expect(
      todayHeader.compareDocumentPosition(yesterdayHeader) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      yesterdayHeader.compareDocumentPosition(last7Header) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      last7Header.compareDocumentPosition(olderHeader) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("collapses a conversation group with summed counts and expands every session link from the keyboard", async () => {
    const user = userEvent.setup();
    const conversationGroupId = "11111111-1111-4111-8111-111111111111";
    const groupedOldest = {
      ...chatFixture(),
      id: "chat-group-oldest",
      title: "First fragment",
      last_activity_at: "2026-04-01T09:00:00Z",
      write_count: 1,
      error_count: 0,
      conversation_group_id: conversationGroupId,
    };
    const groupedMiddle = {
      ...chatFixture(),
      id: "chat-group-middle",
      title: "Second fragment",
      last_activity_at: "2026-04-02T09:00:00Z",
      write_count: 2,
      error_count: 1,
      conversation_group_id: conversationGroupId,
    };
    const groupedNewest = {
      ...chatFixture(),
      id: "chat-group-newest",
      title: "Third fragment",
      last_activity_at: "2026-04-03T09:00:00Z",
      write_count: 3,
      error_count: 2,
      conversation_group_id: conversationGroupId,
    };
    renderWithChats({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?period=all&writes_only=false&archived=false&sort=last_activity_desc`]:
        chatsResponse([groupedNewest, groupedMiddle, groupedOldest]),
    });

    const toggle = await screen.findByRole("button", { name: /Conversation/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveAttribute("aria-controls");
    expect(within(toggle).getByText("3 sessions")).toBeInTheDocument();
    expect(within(toggle).getByText("6 writes")).toBeInTheDocument();
    expect(within(toggle).getByText("3 errors")).toBeInTheDocument();
    expect(toggle.querySelector("time")).toHaveAttribute(
      "datetime",
      groupedNewest.last_activity_at,
    );
    expect(visibleChatLink("/chats/chat-group-oldest")).toBeUndefined();

    toggle.focus();
    await user.keyboard("{Enter}");

    expect(toggle).toHaveAttribute("aria-expanded", "true");
    for (const id of [
      "chat-group-newest",
      "chat-group-middle",
      "chat-group-oldest",
    ]) {
      expect(visibleChatLink(`/chats/${id}`)).toBeInTheDocument();
    }

    await user.keyboard(" ");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
  });

  it("keeps an ungrouped session as its unchanged linked card beside a group", async () => {
    const grouped = [
      {
        ...chatFixture(),
        id: "chat-group-newer",
        title: "Latest fragment",
        conversation_group_id: "11111111-1111-4111-8111-111111111111",
      },
      {
        ...chatFixture(),
        id: "chat-group-older",
        title: "Earlier fragment",
        last_activity_at: "2026-05-27T09:02:00Z",
        conversation_group_id: "11111111-1111-4111-8111-111111111111",
      },
    ];
    const ungrouped = {
      ...chatFixture(),
      id: "chat-ungrouped",
      title: "Independent session",
      last_activity_at: "2026-05-27T09:02:30Z",
    };
    renderWithChats({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?period=all&writes_only=false&archived=false&sort=last_activity_desc`]:
        chatsResponse([grouped[0], ungrouped, grouped[1]]),
    });

    await screen.findByRole("button", { name: /Conversation/ });
    const independent = visibleChatLink("/chats/chat-ungrouped");
    expect(independent).toBeDefined();
    expect(independent).toHaveClass("mcp-chat-card");
    expect(independent).toHaveAttribute("href", "/chats/chat-ungrouped");
    expect(within(independent!).getByText("3 calls")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Conversation/ }),
    ).toHaveAttribute("aria-expanded", "false");
    expect(visibleChatLink("/chats/chat-group-newer")).toBeUndefined();
  });

  it("anchors a group at its newest member in the current sort and preserves payload order inside it", async () => {
    const user = userEvent.setup();
    const conversationGroupId = "11111111-1111-4111-8111-111111111111";
    const groupedOlder = {
      ...chatFixture(),
      id: "chat-group-older",
      title: "High-write older fragment",
      last_activity_at: "2026-04-01T09:00:00Z",
      write_count: 9,
      conversation_group_id: conversationGroupId,
    };
    const independent = {
      ...chatFixture(),
      id: "chat-independent",
      title: "Middle sorted session",
      last_activity_at: "2026-04-02T09:00:00Z",
      write_count: 5,
    };
    const groupedNewest = {
      ...chatFixture(),
      id: "chat-group-newest",
      title: "Low-write newest fragment",
      last_activity_at: "2026-04-03T09:00:00Z",
      write_count: 1,
      conversation_group_id: conversationGroupId,
    };
    renderWithChats(
      {
        [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?period=all&writes_only=false&archived=false&sort=write_count_desc`]:
          chatsResponse([groupedOlder, independent, groupedNewest]),
      },
      "/chats?sort=write_count_desc",
    );

    const toggle = await screen.findByRole("button", { name: /Conversation/ });
    const middle = visibleChatLink("/chats/chat-independent");
    expect(middle).toBeDefined();
    expect(
      middle!.compareDocumentPosition(toggle) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    await user.click(toggle);
    const contentId = toggle.getAttribute("aria-controls");
    const content = contentId ? document.getElementById(contentId) : null;
    expect(content).not.toBeNull();
    const memberLinks = within(content!).getAllByRole("link");
    expect(memberLinks.map((link) => link.getAttribute("href"))).toEqual([
      "/chats/chat-group-older",
      "/chats/chat-group-newest",
    ]);
  });

  it("renders a one-session conversation group as the plain linked card", async () => {
    const onlySession = {
      ...chatFixture(),
      conversation_group_id: "11111111-1111-4111-8111-111111111111",
    };
    renderWithChats({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?period=all&writes_only=false&archived=false&sort=last_activity_desc`]:
        chatsResponse([onlySession]),
    });

    await screen.findByRole("heading", { name: "Chats" });
    const card = visibleChatLink("/chats/chat-1");
    expect(card).toBeDefined();
    expect(card).toHaveClass("mcp-chat-card");
    expect(card).toHaveAttribute("href", "/chats/chat-1");
    expect(screen.queryByRole("button", { name: /Conversation/ })).toBeNull();
  });

  it("parses active filters and offers a reset action for no-result states", async () => {
    const user = userEvent.setup();
    renderWithChats(
      {
        [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?q=missing&client=client-1&period=week&writes_only=true&archived=true&sort=write_count_desc`]:
          chatsResponse([]),
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?period=all&writes_only=false&archived=false&sort=last_activity_desc`]:
          chatsResponse([]),
      },
      "/chats?q=missing&client=client-1&period=week&writes_only=true&archived=true&sort=write_count_desc",
    );

    expect(
      await screen.findByText("No chats match these filters"),
    ).toBeInTheDocument();
    expect(screen.getByText("0 matches")).toBeInTheDocument();
    expect(screen.getByLabelText("Search Chats")).toHaveValue("missing");
    expect(screen.getByLabelText("Client")).toHaveValue("client-1");
    expect(screen.getByRole("radio", { name: "Week" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByRole("button", { name: "Writes only" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(
      screen.getByRole("button", { name: "Show archived" }),
    ).toHaveAttribute("aria-pressed", "true");

    await user.click(
      within(screen.getByRole("search", { name: "Chats filters" })).getByRole(
        "button",
        { name: "Reset filters" },
      ),
    );
    expect(screen.getByLabelText("Search Chats")).toHaveValue("");
  });

  it("keeps search, active summary, and reset available while mobile filters are collapsed", async () => {
    setViewportWidth(390);
    const initialSearch =
      "?q=missing&client=client-1&period=week&writes_only=true&archived=true&sort=write_count_desc";
    mockFetch({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?q=missing&client=client-1&period=week&writes_only=true&archived=true&sort=write_count_desc`]:
        chatsResponse([chatFixture()]),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?period=all&writes_only=false&archived=false&sort=last_activity_desc`]:
        chatsResponse([]),
    });
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={[`/chats${initialSearch}`]}>
        <ChatsListScreen />
        <LocationSearchProbe />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("link", { name: /Codex Research Session/ }),
    ).toBeInTheDocument();
    const filtersRegion = screen.getByRole("search", {
      name: "Chats filters",
    });
    const toggle = within(filtersRegion).getByRole("button", {
      name: "Chats filters",
    });
    const panelId = toggle.getAttribute("aria-controls");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(panelId).toBeTruthy();
    expect(document.getElementById(panelId!)).toHaveAttribute("hidden");
    expect(within(filtersRegion).getByLabelText("Search Chats")).toHaveValue(
      "missing",
    );
    expect(
      within(filtersRegion).getByLabelText(
        /Chats filters: Find: missing · Client: Codex CLI/,
      ),
    ).toBeInTheDocument();
    expect(
      within(filtersRegion).getByRole("button", { name: "Reset filters" }),
    ).toBeInTheDocument();
    expect(
      within(filtersRegion).queryByRole("combobox", { name: "Client" }),
    ).toBeNull();
    expect(screen.getByTestId("location-search")).toHaveTextContent(
      initialSearch,
    );

    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const client = within(filtersRegion).getByRole("combobox", {
      name: "Client",
    });
    expect(client).toHaveValue("client-1");
    expect(screen.getByRole("radio", { name: "Week" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByTestId("location-search")).toHaveTextContent(
      initialSearch,
    );

    client.focus();
    await user.keyboard("{Escape}");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveFocus();
    expect(screen.getByTestId("location-search")).toHaveTextContent(
      initialSearch,
    );

    await user.click(
      within(filtersRegion).getByRole("button", { name: "Reset filters" }),
    );
    await waitFor(() =>
      expect(screen.getByTestId("location-search")).toHaveTextContent("empty"),
    );
    expect(within(filtersRegion).getByLabelText("Search Chats")).toHaveValue(
      "",
    );
  });

  it.each([768, 1024])(
    "keeps all filters open at %ipx",
    async (viewportWidth) => {
      setViewportWidth(viewportWidth);
      renderWithChats({
        [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
        [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?period=all&writes_only=false&archived=false&sort=last_activity_desc`]:
          chatsResponse([]),
      });

      await screen.findByRole("heading", { name: "Chats" });
      const filtersRegion = screen.getByRole("search", {
        name: "Chats filters",
      });
      expect(
        within(filtersRegion).getByRole("button", { name: "Chats filters" }),
      ).toHaveAttribute("aria-expanded", "true");
      expect(
        within(filtersRegion).getByRole("combobox", { name: "Client" }),
      ).toBeInTheDocument();
      expect(
        within(filtersRegion).getByRole("radiogroup", { name: "Period" }),
      ).toBeInTheDocument();
    },
  );

  it("keeps in-progress typing when the debounced URL write echoes back", async () => {
    const user = userEvent.setup();
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?period=all&writes_only=false&archived=false&sort=last_activity_desc`]:
        chatsResponse([chatFixture()]),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?q=hello&period=all&writes_only=false&archived=false&sort=last_activity_desc`]:
        chatsResponse([]),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?q=hello+world&period=all&writes_only=false&archived=false&sort=last_activity_desc`]:
        chatsResponse([]),
    });
    render(
      <MemoryRouter initialEntries={["/chats"]}>
        <ChatsListScreen />
      </MemoryRouter>,
    );

    const input = await screen.findByLabelText("Search Chats");
    await user.type(input, "hello ");

    // The debounce commits the TRIMMED draft (q=hello) to the URL; the echo
    // back into the input must not delete the trailing space mid-phrase.
    await waitFor(() => {
      expect(fetchMock.mock.calls.map((call) => String(call[0]))).toContain(
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?q=hello&period=all&writes_only=false&archived=false&sort=last_activity_desc`,
      );
    });
    expect(input).toHaveValue("hello ");

    await user.type(input, "world");
    expect(input).toHaveValue("hello world");
    await waitFor(() => {
      expect(fetchMock.mock.calls.map((call) => String(call[0]))).toContain(
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?q=hello+world&period=all&writes_only=false&archived=false&sort=last_activity_desc`,
      );
    });
    expect(input).toHaveValue("hello world");
  });

  it("appends the next cursor page when Load more is clicked", async () => {
    const user = userEvent.setup();
    const olderChat = {
      ...chatFixture(),
      id: "chat-2",
      title: "Older Codex Session",
      subject: "Older Codex Session",
    };
    renderWithChats({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?period=all&writes_only=false&archived=false&sort=last_activity_desc`]:
        chatsResponse([chatFixture()], "cursor-2"),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?period=all&writes_only=false&archived=false&sort=last_activity_desc&cursor=cursor-2`]:
        chatsResponse([olderChat]),
    });

    expect(
      await screen.findByRole("link", { name: /Codex Research Session/ }),
    ).toBeInTheDocument();
    // A next cursor exists, so the chip reports a lower bound rather than
    // claiming the loaded page is the whole set.
    expect(screen.getByText("1+ session")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Load more" }));

    expect(
      await screen.findByRole("link", { name: /Older Codex Session/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /Codex Research Session/ }),
    ).toBeInTheDocument();
    // The final page has no next_cursor, so the control disappears.
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
    expect(screen.getByText("2 sessions")).toBeInTheDocument();
  });

  it("auto-advances empty search continuations before showing an empty state", async () => {
    const query = "bounded search";
    const prefix = `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?q=bounded+search&period=all&writes_only=false&archived=false&sort=last_activity_desc`;
    const found = {
      ...chatFixture(),
      title: "Bounded search result",
      subject: "Bounded search result",
    };
    const fetchMock = mockFetch({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [prefix]: chatsResponse([], "chat-search-v1:20"),
      [`${prefix}&cursor=chat-search-v1%3A20`]: chatsResponse(
        [],
        "chat-search-v1:40",
      ),
      [`${prefix}&cursor=chat-search-v1%3A40`]: chatsResponse([found]),
    });
    render(
      <MemoryRouter initialEntries={[`/chats?q=${encodeURIComponent(query)}`]}>
        <ChatsListScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("link", { name: /Bounded search result/ }),
    ).toBeInTheDocument();
    expect(screen.queryByText("No chats match these filters")).toBeNull();
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
    await waitFor(() => {
      const requested = fetchMock.mock.calls.map((call) => String(call[0]));
      expect(requested).toContain(`${prefix}&cursor=chat-search-v1%3A20`);
      expect(requested).toContain(`${prefix}&cursor=chat-search-v1%3A40`);
    });
  });

  it("does not append an old query continuation after the search changes", async () => {
    const oldPrefix = `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?q=old&period=all&writes_only=false&archived=false&sort=last_activity_desc`;
    const newPrefix = `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?q=new&period=all&writes_only=false&archived=false&sort=last_activity_desc`;
    let resolveOldContinuation!: (response: Response) => void;
    const oldChat = {
      ...chatFixture(),
      id: "old-chat",
      title: "Old query result",
      subject: "Old query result",
    };
    const newChat = {
      ...chatFixture(),
      id: "new-chat",
      title: "New query result",
      subject: "New query result",
    };
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input) => {
        const url = input.toString();
        if (url === `${BASE}/api/v1/workspaces`) {
          return jsonResponse(workspaceResponse());
        }
        if (url === oldPrefix) {
          return jsonResponse(chatsResponse([], "chat-search-v1:20"));
        }
        if (url === `${oldPrefix}&cursor=chat-search-v1%3A20`) {
          return new Promise<Response>((resolve) => {
            resolveOldContinuation = resolve;
          });
        }
        if (url === newPrefix) {
          return jsonResponse(chatsResponse([newChat]));
        }
        throw new Error(`Unexpected request: ${url}`);
      });
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/chats?q=old"]}>
        <ChatsListScreen />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(fetchMock.mock.calls.map((call) => String(call[0]))).toContain(
        `${oldPrefix}&cursor=chat-search-v1%3A20`,
      );
    });
    const input = screen.getByLabelText("Search Chats");
    await user.clear(input);
    await user.type(input, "new");
    expect(
      await screen.findByRole("link", { name: /New query result/ }),
    ).toBeInTheDocument();

    resolveOldContinuation(jsonResponse(chatsResponse([oldChat])));
    await waitFor(() => {
      expect(
        screen.queryByRole("link", { name: /Old query result/ }),
      ).toBeNull();
    });
    expect(
      screen.getByRole("link", { name: /New query result/ }),
    ).toBeInTheDocument();
  });

  it("shows the zero-data empty state separately from filtered empty state", async () => {
    renderWithChats({
      [`${BASE}/api/v1/workspaces`]: workspaceResponse(),
      [`${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?period=all&writes_only=false&archived=false&sort=last_activity_desc`]:
        chatsResponse([]),
    });

    expect(
      await screen.findByText("No external chats yet"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Open MCP setup" }),
    ).toHaveAttribute("href", "/settings/integrations");
    expect(screen.queryByRole("link", { name: "Open Ask" })).not.toBeInTheDocument();
  });

  it("keeps the empty state hidden while the initial chat list is loading", async () => {
    let resolveChats!: (response: Response) => void;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse(workspaceResponse());
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/mcp-chats?period=all&writes_only=false&archived=false&sort=last_activity_desc`
      ) {
        return new Promise<Response>((resolve) => {
          resolveChats = resolve;
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(
      <MemoryRouter initialEntries={["/chats"]}>
        <ChatsListScreen />
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("status", { name: "Loading Chats" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("No external chats yet")).toBeNull();

    resolveChats(jsonResponse(chatsResponse([])));
    expect(
      await screen.findByText("No external chats yet"),
    ).toBeInTheDocument();
  });
});

function renderWithChats(
  responses: Record<string, unknown | (() => unknown | Promise<unknown>)>,
  initialEntry = "/chats",
) {
  mockFetch(responses);
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <ChatsListScreen />
    </MemoryRouter>,
  );
}

function mockFetch(
  responses: Record<string, unknown | (() => unknown | Promise<unknown>)>,
) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = input.toString();
    const body = responses[url];
    if (!body) {
      throw new Error(`Unexpected request: ${url}`);
    }
    const resolved = typeof body === "function" ? await body() : body;
    return resolved instanceof Response ? resolved : jsonResponse(resolved);
  });
}

function LocationStateProbe() {
  const location = useLocation();
  return (
    <output data-testid="location-state">
      {location.state ? "present" : "cleared"}
    </output>
  );
}

function LocationSearchProbe() {
  const location = useLocation();
  return (
    <output data-testid="location-search">{location.search || "empty"}</output>
  );
}

function setViewportWidth(width: number) {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: query === "(max-width: 640px)" ? width <= 640 : false,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
}

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
  });
}

function visibleChatLink(href: string) {
  return screen
    .queryAllByRole("link")
    .find((link) => link.getAttribute("href") === href);
}

function workspaceResponse() {
  return {
    data: {
      current_workspace_id: WORKSPACE_ID,
      workspaces: [
        {
          id: WORKSPACE_ID,
          slug: "default",
          title: "Default",
          name: "Default",
          goal: "Test",
          default_language: "en",
          visibility: "private",
        },
      ],
    },
  };
}

function chatsResponse(
  items: Array<ReturnType<typeof chatFixture>>,
  nextCursor: string | null = null,
) {
  return {
    data: {
      items,
      next_cursor: nextCursor,
      clients: [
        {
          id: "client-1",
          name: "Codex CLI",
          client_type: "codex",
          brand_hint: "codex",
          scopes: ["mcp:read"],
        },
      ],
    },
  };
}

function chatFixture() {
  return {
    id: "chat-1",
    workspace_id: WORKSPACE_ID,
    client: {
      id: "client-1",
      name: "Codex CLI",
      client_type: "codex",
      brand_hint: "codex",
      scopes: ["mcp:read"],
    },
    mcp_session_id: "session-1",
    title: "Codex Research Session",
    summary: "Search and read activity",
    started_at: "2026-05-27T09:00:00Z",
    last_activity_at: "2026-05-27T09:03:00Z",
    tool_call_count: 3,
    write_count: 1,
    error_count: 0,
    failed_count: 0,
    rejected_count: 0,
    archived_at: "2026-05-27T10:00:00Z",
    conversation_group_id: null as string | null,
    title_user_edited: true,
    title_source: "user",
    action: "search",
    subject: "Codex Research Session",
    subject_kind: null,
    primary_tool_name: "search",
    linked_artifacts: [
      {
        id: "artifact-link-1",
        resource_kind: "context_page",
        resource_id: "page-1",
        artifact_title: "Authentication policy",
        navigation_path: "/context?target=page-1",
        verb: "referenced",
        first_occurred_at: "2026-05-27T09:01:00Z",
        last_occurred_at: "2026-05-27T09:02:00Z",
        occurrence_count: 2,
        last_mcp_tool_call_id: "tool-1",
      },
    ],
  };
}

function daysAgoIso(days: number) {
  const value = new Date();
  value.setHours(12, 0, 0, 0);
  value.setDate(value.getDate() - days);
  return value.toISOString();
}

function rollingRangeLabel(daysBack: number) {
  const end = new Date();
  const start = new Date(end);
  start.setDate(end.getDate() - daysBack);
  return `${formatBucketDate(start)}-${formatBucketDate(end)}`;
}

function formatBucketDate(value: Date) {
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
  }).format(value);
}

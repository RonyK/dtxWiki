import { EditorView } from "@codemirror/view";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode, type ReactNode } from "react";
import {
  createMemoryRouter,
  createRoutesFromElements,
  Route,
  RouterProvider,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";

import { i18n } from "../../../shared/i18n";
import {
  AUTOSAVE_CHECKPOINT_MS,
  editorMeta,
  PageEditor,
  RoutedPageEditor,
} from "../PageEditor";
import { getMarkdownCommandState } from "../editorCommands";
import { resetCodeMirrorStyleRegistry } from "./codeMirrorStyleRegistry";

const authState = vi.hoisted(() => ({
  user: {
    display_name: "Test User",
    email: "test@example.com",
    status: "active",
    user_id: "user-1",
  },
}));

vi.mock("../../../shared/auth/AuthContext", () => ({
  useAuth: () => authState,
}));

// A COMPLETE ContextPageDetail. The fields below the first block all carry a
// schema `default`, which openapi-typescript emits as REQUIRED (the server
// always sends them), so a fixture missing them is not a valid PagePayload --
// it only used to compile because nothing passed it to a function typed on
// the full payload. The 260908 audit branch's editorMeta(savedPage, ...)
// assertions do exactly that, so the fixture is completed here rather than
// cast away at the call site.
const page = {
  id: "p1",
  workspace_id: "workspace-1",
  slug: "hello",
  title: "Hello",
  // `as const` because page_type is a literal union in the generated schema,
  // not a free string; without it the inferred `string` fails assignment.
  page_type: "overview" as const,
  tags: [],
  aliases: [],
  current_version_id: "v1",
  updated_at: "2026-05-08T00:00:00Z",
  content_hash: "a".repeat(64),
  body_md: "# Hello",
  version_id: "v1",
  parent_version_id: null,
  created_at: "2026-05-08T00:00:00Z",
  backlink_count: 0,
  children_count: 0,
  editing_revision: 1,
  is_alternative: false,
  is_discarded: false,
  is_draft_head: false,
  is_public: false,
  is_published_head: false,
  is_visible_head: false,
  preserved_as_alternative: false,
  reconciliation_available: false,
  reconciliation_pending_count: 0,
  reconciliation_pending_count_truncated: false,
  superseded_review_count: 0,
};
const BASE = "http://127.0.0.1:21080";

async function advanceAutosaveCheckpoint(): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(AUTOSAVE_CHECKPOINT_MS);
  });
}

// Moves a fake clock that is not advancing on its own to an absolute instant.
async function advanceFakeClockTo(instant: number): Promise<void> {
  const remaining = instant - Date.now();
  expect(remaining).toBeGreaterThanOrEqual(0);
  await act(async () => {
    await vi.advanceTimersByTimeAsync(remaining);
  });
}

// Replaces performance.now() with a clock that only moves when the test moves
// it. Production code that measures a window with performance.now() rather
// than with a timer (the linked-scroll echo and geometry-authority windows)
// would otherwise see real time pass whenever the test process is descheduled
// between two statements. The clock starts at a whole millisecond at or after
// the real reading, so a timestamp taken before the install never runs ahead
// of it and every sum and comparison the window arithmetic makes is exact. The
// real clock is back when the test finishes.
function installLogicalPerformanceClock() {
  let instant = Math.ceil(performance.now());
  const spy = vi.spyOn(performance, "now").mockImplementation(() => instant);
  onTestFinished(() => spy.mockRestore());
  return {
    now: () => instant,
    advance(ms: number) {
      expect(ms).toBeGreaterThanOrEqual(0);
      instant += ms;
    },
  };
}

function renderWithRouter(initialEntry: string, routes: ReactNode) {
  const router = createMemoryRouter(createRoutesFromElements(
    <>
      {routes}
      <Route path="/context" element={<PublishedContextDestination />} />
    </>,
  ), {
    initialEntries: [initialEntry],
  });
  render(
    <RouterProvider
      router={router}
    />,
  );
  return router;
}

function PublishedContextDestination() {
  const location = useLocation();
  return (
    <div data-testid="published-context-destination">
      {`${location.pathname}${location.search}`}
    </div>
  );
}

async function openContextExplorer(): Promise<HTMLElement> {
  const toggle = screen.getByRole("button", { name: "Open Context explorer" });
  expect(toggle).toHaveAttribute("aria-expanded", "false");

  const drawerId = toggle.getAttribute("aria-controls");
  expect(drawerId).toBeTruthy();
  const closedDrawer = document.getElementById(drawerId!);
  expect(closedDrawer).toHaveAttribute("aria-hidden", "true");
  expect(closedDrawer).toHaveAttribute("inert", "");

  await typist().click(toggle);

  const drawer = await screen.findByRole("dialog", { name: "Context drawer" });
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  expect(drawer).toHaveAttribute("data-mobile-open", "true");
  expect(drawer).not.toHaveAttribute("aria-hidden");
  expect(drawer).not.toHaveAttribute("inert");
  return drawer;
}

// Publish is looked up again after the wait rather than held across it: a
// handle taken before the save settles can be removed by the re-render that
// settles it, and clicking a removed button does nothing.
async function saveDraftAndPublish(): Promise<void> {
  await typist().click(publicationActions().getByRole("button", { name: "Save draft" }));
  await waitFor(() =>
    expect(publicationActions().getByRole("button", { name: "Publish" })).toBeEnabled(),
  );
  await typist().click(publicationActions().getByRole("button", { name: "Publish" }));
}

// Save draft and Publish sit in one labelled group. Scoping the role query to
// it keeps testing-library from computing the accessible name of every button
// in the tree rail and toolbar on each lookup, and on each poll of a waitFor.
function publicationActions() {
  return within(
    screen.getByLabelText(i18n.t("pages:editor.publicationActions")),
  );
}

function syncStatus(): HTMLElement {
  const status = document.querySelector<HTMLElement>(".sync-pill");
  if (!status) {
    throw new Error("Expected the editor sync status");
  }
  return status;
}

function mockPublicationFlow({
  publishWarnings = [],
  rejectAutosave = false,
  rejectPublishWithoutOverride = false,
  publishResponse,
  reviewDecisionResponse,
  rejectWarnings = [
    {
      code: "lint_publish_blocked",
      message: "Not published. Unclosed code fence opened at line 9.",
    },
  ],
}: {
  publishWarnings?: { code: string; message: string }[];
  rejectAutosave?: boolean;
  rejectPublishWithoutOverride?: boolean;
  publishResponse?: Promise<Response>;
  reviewDecisionResponse?: Promise<Response>;
  rejectWarnings?: { code: string; message: string }[];
} = {}) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    if (url === `${BASE}/api/v1/workspaces`) {
      return workspaceResponse();
    }
    if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
      return jsonResponse({ data: contextTreeNodes() });
    }
    if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
      return jsonResponse({ data: [] });
    }
    if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
      return jsonResponse({ data: [page], next_cursor: null });
    }
    if (init?.method === "POST" && url.endsWith("/autosave")) {
      if (rejectAutosave) {
        return jsonResponse(
          {
            error_code: "internal_error",
            message: "Draft storage is unavailable.",
            request_id: "request-save-failed",
            warnings: [],
          },
          500,
        );
      }
      const payload = JSON.parse(String(init.body));
      return jsonResponse({
        data: {
          ...page,
          body_md: payload.body_md,
          content_hash: "b".repeat(64),
          current_version_id: "v2",
          parent_version_id: "v1",
          version_id: "v2",
        },
      });
    }
    if (init?.method === "POST" && url.endsWith("/publish")) {
      const payload = JSON.parse(String(init.body));
      if (rejectPublishWithoutOverride && !payload.publish_despite_findings) {
        return jsonResponse(
          {
            error_code: "rejected_by_policy",
            message: "Publishing was rejected.",
            request_id: "request-1",
            warnings: rejectWarnings,
          },
          403,
        );
      }
      if (publishResponse) {
        return publishResponse;
      }
      return jsonResponse({
        data: {
          ...page,
          content_hash: "b".repeat(64),
          current_version_id: "v2",
          parent_version_id: "v1",
          version_id: "v2",
        },
        ...(publishWarnings.length
          ? {
              envelope: {
                result_status: "partial",
                warnings: publishWarnings,
              },
            }
          : {}),
      });
    }
    if (
      init?.method === "POST" &&
      url.includes("/reviews/") &&
      url.endsWith("/decision") &&
      reviewDecisionResponse
    ) {
      return reviewDecisionResponse;
    }
    return jsonResponse({ data: page });
  });
}

describe("PageEditor", () => {
  beforeEach(() => {
    // Every test mounts its own editors; without this the stylesheet that the
    // earlier tests' editors left in the shared document slows each later
    // test down (see codeMirrorStyleRegistry.ts).
    resetCodeMirrorStyleRegistry();
    vi.restoreAllMocks();
    // PageEditor tests exercise the desktop workspace unless a case opts into
    // a responsive breakpoint explicitly. jsdom's 1024px default is itself
    // the drawer breakpoint and would otherwise hide the Context rail.
    vi.stubGlobal("innerWidth", 1280);
    authState.user = {
      display_name: "Test User",
      email: "test@example.com",
      status: "active",
      user_id: "user-1",
    };
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    // jsdom has no sendBeacon; the tests that need one install a stub. Left in
    // place it would carry the next test's unload flush away from fetch.
    Reflect.deleteProperty(window.navigator, "sendBeacon");
  });

  it("localizes editor chrome in Korean: title placeholder, mode tabs, shortcuts panel, block menu (I18N-01)", async () => {
    await i18n.changeLanguage("ko");
    try {
      vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
        const url = String(input);
        if (url === `${BASE}/api/v1/workspaces`) {
          return workspaceResponse();
        }
        if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
          return jsonResponse({ data: contextTreeNodes() });
        }
        if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
          return jsonResponse({ data: [] });
        }
        if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
          return jsonResponse({ data: [], next_cursor: null });
        }
        return jsonResponse({ data: page });
      });

      renderWithRouter(
        "/pages/new",
        <Route path="/pages/new" element={<PageEditor mode="create" />} />,
      );

      const titleInput = await findTitleEditor();
      const localizedBodyLabel = i18n.t("pages:editor.bodyAria");
      const bodyTextbox = await screen.findByRole("textbox", {
        name: localizedBodyLabel,
      });
      expect(bodyTextbox).toHaveClass("cm-content");
      expect(bodyTextbox).toHaveAttribute("aria-label", localizedBodyLabel);
      // The single most direct regression oracle for I18N-01: this is the
      // exact hardcoded string the finding was written against
      // (PageEditor.tsx originally had a bare `placeholder="Untitled"`).
      expect(titleInput).not.toHaveAttribute("placeholder", "Untitled");
      expect(screen.queryByPlaceholderText("Untitled")).toBeNull();

      // Mode tabs: previously bare JSX text nodes ("Source"/"Reading"/"Split"),
      // not routed through t() at all.
      expect(screen.getByRole("tab", { name: "소스" })).toBeInTheDocument();
      expect(screen.getByRole("tab", { name: "읽기" })).toBeInTheDocument();
      expect(screen.queryByRole("tab", { name: "Source" })).toBeNull();
      expect(screen.queryByRole("tab", { name: "Reading" })).toBeNull();
      const localizedActionBar = screen.getByRole("toolbar", {
        name: i18n.t("pages:editor.actionBar.toolbar"),
      });
      expect(within(localizedActionBar).getByRole("button", {
        name: i18n.t("pages:editor.commands.attachFiles"),
      })).toBeInTheDocument();

      // Keyboard shortcuts panel: SHORTCUT_GROUPS was a module-level constant
      // with hardcoded English group/action labels, unreachable from any
      // useTranslation binding.
      await typist().click(
        screen.getByRole("button", { name: "키보드 단축키" }),
      );
      const shortcutDialog = await screen.findByRole("dialog", {
        name: /단축키/,
      });
      expect(within(shortcutDialog).getByText("탐색")).toBeInTheDocument();
      expect(within(shortcutDialog).getByText("서식")).toBeInTheDocument();
      expect(within(shortcutDialog).queryByText("Navigation")).toBeNull();
      expect(within(shortcutDialog).queryByText("Format")).toBeNull();
      await typist().keyboard("{Escape}");

      // Block menu ("/" command): editorBlocks.ts group names and entry
      // labels were plain strings with no i18n seam at all. Not reusing
      // openBlockMenuFromEditor() here -- it asserts the English dialog
      // name ("Insert block"), which is exactly what must NOT render in ko.
      pressEditorShortcut(codeMirrorView(), { key: "/", ctrlKey: true });
      const blockMenu = await screen.findByRole("dialog", { name: "블록 삽입" });
      expect(within(blockMenu).getByText("텍스트")).toBeInTheDocument();
      expect(within(blockMenu).getByText("목록")).toBeInTheDocument();
      expect(within(blockMenu).getByText("제목 1")).toBeInTheDocument();
      expect(within(blockMenu).queryByText("Text")).toBeNull();
      expect(within(blockMenu).queryByText("Heading 1")).toBeNull();
    } finally {
      await i18n.changeLanguage("en");
    }
  });

  it("opens the block palette through the WebKit-safe Ctrl+/ editor DOM handler", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    await screen.findByLabelText("Markdown body");
    const view = codeMirrorView();
    const bodyBeforeShortcut = view.state.doc.toString();
    const keydown = new KeyboardEvent("keydown", {
      bubbles: true,
      cancelable: true,
      code: "Slash",
      ctrlKey: true,
      key: "/",
    });
    act(() => {
      view.contentDOM.dispatchEvent(keydown);
    });
    expect(keydown.defaultPrevented).toBe(true);
    expect(
      await screen.findByRole("dialog", { name: "Insert block" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("combobox", { name: "Filter block commands" }),
    ).toHaveFocus();
    expect(view.state.doc.toString()).toBe(bodyBeforeShortcut);
    await typist().keyboard("{Escape}");
    await waitFor(() => expect(view.contentDOM).toHaveFocus());

    const outsideKeydown = new KeyboardEvent("keydown", {
      bubbles: true,
      cancelable: true,
      code: "Slash",
      ctrlKey: true,
      key: "/",
    });
    act(() => {
      document.body.dispatchEvent(outsideKeydown);
    });
    expect(outsideKeydown.defaultPrevented).toBe(false);
    expect(
      screen.queryByRole("dialog", { name: "Insert block" }),
    ).not.toBeInTheDocument();
  });

  it("creates pages with title, slug, and markdown body", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages/tags?")) {
        return jsonResponse({ data: ["Migration", "Infra"] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [], next_cursor: null });
      }
      if (init?.method === "POST") {
        return jsonResponse({ data: { ...page, slug: "release-notes", title: "Release notes" } }, 201);
      }
      return jsonResponse({ data: page });
    });

    const router = renderWithRouter(
      "/pages/new",
      <>
          <Route path="/pages/new" element={<PageEditor mode="create" />} />
          <Route path="/pages/:slug/edit" element={<div>editing created page</div>} />
          <Route path="/context/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    const titleInput = await findTitleEditor();
    expect(titleInput.tagName).toBe("TEXTAREA");
    expect(titleInput).toHaveValue("");
    expect(screen.queryByDisplayValue("Untitled page")).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue("untitled-page")).not.toBeInTheDocument();
    expect(await screen.findByText("Not published yet")).toBeInTheDocument();

    await setEditorTitle(titleInput, "Release notes");
    expect(screen.queryByRole("button", { name: /Page icon/ })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Page type")).not.toBeInTheDocument();
    const tagsInput = screen.getByLabelText("Tags");
    commitChip(tagsInput, "Migration");
    commitChip(tagsInput, "Infra");
    await openPageProperties();
    const pageType = screen.getByLabelText("Page type");
    expect(within(pageType).getAllByRole("option")).toHaveLength(10);
    expect(screen.getByText("A high-level summary of a topic or area."))
      .toBeInTheDocument();
    await userEvent.selectOptions(pageType, "decision");
    expect(screen.getByText("A record of a decision, its rationale, and its outcome."))
      .toBeInTheDocument();
    await act(async () => {
      await i18n.changeLanguage("ko");
    });
    expect(screen.getByLabelText("페이지 유형")).toHaveValue("decision");
    expect(screen.getByText("결정 내용, 근거, 결과 기록")).toBeInTheDocument();
    await act(async () => {
      await i18n.changeLanguage("en");
    });
    expect(screen.queryByLabelText("Aliases")).not.toBeInTheDocument();
    await typist().click(pageProperties().getByRole("button", { name: "Advanced settings" }));
    const aliasesInput = screen.getByLabelText("Aliases");
    commitChip(aliasesInput, "Launch Plan");
    expect(screen.queryByLabelText("Cover URL")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Slug")).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "s", ctrlKey: true });

    await waitFor(() => {
      const createCall = fetchMock.mock.calls.find((call) => call[1]?.method === "POST");
      const createBody = JSON.parse(String(createCall?.[1]?.body));
      expect(createBody).toMatchObject({
        title: "Release notes",
        page_type: "decision",
        tags: ["Migration", "Infra"],
        aliases: ["Launch Plan"],
        // A hand-authored page lands at the Context root. Inbox stages writes
        // that arrive with no directory (MCP tool writes, Ask captures), which
        // never pass through this editor.
        placement_parent_node_id: "context-root",
      });
      expect(createBody.body_md).toContain('title: "Release notes"');
      expect(createBody.body_md).toContain("slug: release-notes");
      expect(createBody.body_md).toContain('icon: "lucide:file-text"');
      expect(createBody.body_md).not.toContain("\ncover:");
      expect(createBody.body_md).toContain("type: decision");
      expect(createBody.body_md).toContain('tags: ["Migration", "Infra"]');
      expect(createBody.body_md).toContain('aliases: ["Launch Plan"]');
    });
    expect(fetchMock.mock.calls.map((call) => call[0])).toContain(
      `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`,
    );
    expect(fetchMock.mock.calls.some((call) => String(call[0]).includes("/publish"))).toBe(false);
    await waitFor(() =>
      expect(router.state.location.pathname).toBe(
        "/context/release-notes/edit",
      ),
    );
    expect(screen.getByLabelText("Context tree")).toBeInTheDocument();
    expect(document.querySelector(".page-list")).not.toBeInTheDocument();
  });

  it("restores the unpublished-changes indicator when a draft exists on load", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages/tags?")) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [], next_cursor: null });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/context/pages/p1/versions?limit=100`) {
        return jsonResponse({
          data: [
            {
              version_id: "v2",
              version_no: 2,
              parent_version_id: "v1",
              version_status: "draft",
              graph_status: "linear",
              actor_type: "user",
              actor_display_name: null,
              created_at: page.updated_at,
              content_hash: "b".repeat(64),
              is_current: true,
              is_alternative: false,
              is_discarded: false,
            },
            {
              version_id: "v1",
              version_no: 1,
              parent_version_id: null,
              // The versions API returns the stored status verbatim, and the
              // stored value for a live published version is
              // "published_snapshot" -- a mock that says "published" would let
              // a wrong comparison pass here and mislabel the page in the app.
              version_status: "published_snapshot",
              graph_status: "linear",
              actor_type: "user",
              actor_display_name: null,
              created_at: page.created_at,
              content_hash: page.content_hash,
              is_current: false,
              is_alternative: false,
              is_discarded: false,
            },
          ],
        });
      }
      return jsonResponse({
        data:
          url.includes("/by-slug/")
            ? {
                ...page,
                version_id: "v2",
                current_version_id: "v2",
                current_published_version_id: "v1",
                version_status: "draft",
                parent_version_id: "v1",
                content_hash: "b".repeat(64),
              }
            : page,
      });
    });

    renderWithRouter(
      "/pages/release-notes/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    await findTitleEditor();
    // PUB-09: publicationState is "staged" here (a published head plus a
    // staged draft), so the pill names that instead of a bare "Saved".
    expect(syncStatus()).toHaveTextContent("Unpublished draft saved");
    expect(publicationActions().getByRole("button", { name: "Publish" })).toBeInTheDocument();
    expect(document.querySelector(".status-banner")).not.toBeInTheDocument();
  });

  it("cancels fence publication after saving the draft without publishing", async () => {
    const fetchMock = mockPublicationFlow();
    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    await findTitleEditor();
    replaceEditorSelection(codeMirrorView(), "# Hello\n\n```ts\nconst value = 1;");
    expect(
      await screen.findByText(/Unclosed code fence at line 3/),
    ).toBeInTheDocument();
    await saveDraftAndPublish();

    const dialog = await screen.findByRole("dialog", {
      name: "Check the code fence before publishing",
    });
    expect(within(dialog).getByText(/opened at line 3/)).toBeInTheDocument();
    await typist().click(within(dialog).getByRole("button", { name: "Cancel" }));

    expect(
      screen.queryByRole("dialog", {
        name: "Check the code fence before publishing",
      }),
    ).not.toBeInTheDocument();
    expect(writeCalls(fetchMock).filter((call) => String(call[0]).endsWith("/autosave"))).toHaveLength(1);
    expect(writeCalls(fetchMock).filter((call) => String(call[0]).endsWith("/publish"))).toHaveLength(0);
    expect(codeMirrorView().state.doc.toString()).not.toContain("\n```\n");
  });

  it("closes the fence in the editor buffer, stages it, and publishes", async () => {
    const fetchMock = mockPublicationFlow();
    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    await findTitleEditor();
    // Publishing leaves the editor route, so the buffer is read through the
    // view captured here: looking the editor up again after the publish races
    // the navigation that unmounts it.
    const view = codeMirrorView();
    replaceEditorSelection(view, "# Hello\n\n~~~ts\nconst value = 1;");
    await saveDraftAndPublish();
    await typist().click(
      within(
        await screen.findByRole("dialog", {
          name: "Check the code fence before publishing",
        }),
      ).getByRole("button", { name: "Close the fence and publish" }),
    );

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/publish")),
      ).toBe(true);
    });
    const autosaveCall = fetchMock.mock.calls.find((call) =>
      String(call[0]).endsWith("/autosave") &&
      JSON.parse(String(call[1]?.body)).body_md.includes("~~~\n"),
    );
    expect(JSON.parse(String(autosaveCall?.[1]?.body)).body_md).toContain(
      "~~~ts\nconst value = 1;\n~~~\n",
    );
    await waitFor(() => {
      expect(view.state.doc.toString()).toBe(
        "# Hello\n\n~~~ts\nconst value = 1;\n~~~\n",
      );
    });
  });

  it("publishes an unclosed fence only when the author chooses the override", async () => {
    const fetchMock = mockPublicationFlow({
      publishWarnings: [
        {
          code: "published_with_findings",
          message:
            "Published despite a format finding. Unclosed code fence opened at line 3.",
        },
      ],
    });
    const router = renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    await findTitleEditor();
    replaceEditorSelection(codeMirrorView(), "# Hello\n\n```ts\nconst value = 1;");
    await saveDraftAndPublish();
    await typist().click(
      within(
        await screen.findByRole("dialog", {
          name: "Check the code fence before publishing",
        }),
      ).getByRole("button", { name: "Publish anyway" }),
    );

    const publishCall = await waitFor(() => {
      const call = fetchMock.mock.calls.find((candidate) =>
        String(candidate[0]).endsWith("/publish"),
      );
      expect(call).toBeTruthy();
      return call;
    });
    // Publishing names the content it staged, so a concurrent session of the
    // same author cannot swap the body between staging and publishing.
    expect(JSON.parse(String(publishCall?.[1]?.body))).toEqual({
      reason: "Published from Page editor.",
      publish_despite_findings: true,
      expected_content_hash: expect.any(String),
    });
    // I18N-08(b): the warning is now resolved through `apiWarnings.<code>`
    // rather than rendering the server's raw message verbatim, so a ko
    // locale doesn't see English. The specific dynamic detail ("Unclosed
    // code fence opened at line 3") is backend-authored prose that isn't
    // itself localized, so the mapped copy is a general (but accurate)
    // description of the code rather than a literal translation of it.
    await waitFor(() => {
      expect(router.state.location).toMatchObject({
        pathname: "/context",
        search: "?target=p1",
        state: { contextPublishNotice: { kind: "published_with_findings" } },
      });
    });
  });

  it("localizes the published-with-findings status in Korean (I18N-08b)", async () => {
    await i18n.changeLanguage("ko");
    try {
      mockPublicationFlow({
        publishWarnings: [
          {
            code: "published_with_findings",
            message:
              "Published despite a format finding. Unclosed code fence opened at line 3.",
          },
        ],
      });
      const router = renderWithRouter(
        "/pages/hello/edit",
        <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
      );

      await findTitleEditor();
      replaceEditorSelection(codeMirrorView(), "# Hello\n\n```ts\nconst value = 1;");
      await typist().click(publicationActions().getByRole("button", { name: "초안 저장" }));
      await waitFor(() => expect(publicationActions().getByRole("button", { name: "게시" })).toBeEnabled());
      await typist().click(publicationActions().getByRole("button", { name: "게시" }));
      await typist().click(
        within(
          await screen.findByRole("dialog", {
            name: "게시하기 전에 코드 펜스를 확인하세요",
          }),
        ).getByRole("button", { name: "그래도 게시" }),
      );

      await waitFor(() => {
        expect(router.state.location.state).toEqual({
          contextPublishNotice: { kind: "published_with_findings" },
        });
      });
    } finally {
      await i18n.changeLanguage("en");
    }
  });

  it("ignores unrelated publish warnings without exposing their raw message (I18N-08b)", async () => {
    mockPublicationFlow({
      publishWarnings: [
        { code: "some_future_code", message: "Not the code this status checks for." },
      ],
    });
    const router = renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    await findTitleEditor();
    replaceEditorSelection(codeMirrorView(), "# Hello\n\n```ts\nconst value = 1;");
    await saveDraftAndPublish();
    await typist().click(
      within(
        await screen.findByRole("dialog", {
          name: "Check the code fence before publishing",
        }),
      ).getByRole("button", { name: "Publish anyway" }),
    );

    await waitFor(() => {
      expect(router.state.location.state).toEqual({
        contextPublishNotice: { kind: "published" },
      });
    });
  });

  it("renders authoritative server findings in the same publish modal", async () => {
    mockPublicationFlow({ rejectPublishWithoutOverride: true });
    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    await setEditorTitle(await findTitleEditor(), "Server-only finding");
    await saveDraftAndPublish();

    const dialog = await screen.findByRole("dialog", {
      name: "Check the code fence before publishing",
    });
    // I18N-13: same seam as above -- `lint_publish_blocked` now resolves
    // through `apiWarnings.lint_publish_blocked` instead of the server's
    // raw, unlocalized sentence.
    expect(
      within(dialog).getByText(
        "Not published — a formatting issue is blocking it.",
      ),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("button", { name: "Close the fence and publish" }),
    ).toBeInTheDocument();
    expect(
      within(dialog).getByRole("button", { name: "Publish anyway" }),
    ).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeInTheDocument();
  });

  it("localizes the fence-publish rejection warning in Korean (I18N-13)", async () => {
    await i18n.changeLanguage("ko");
    try {
      mockPublicationFlow({ rejectPublishWithoutOverride: true });
      renderWithRouter(
        "/pages/hello/edit",
        <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
      );

      await setEditorTitle(await findTitleEditor(), "Server-only finding");
      await typist().click(publicationActions().getByRole("button", { name: "초안 저장" }));
      await waitFor(() => expect(publicationActions().getByRole("button", { name: "게시" })).toBeEnabled());
      await typist().click(publicationActions().getByRole("button", { name: "게시" }));

      const dialog = await screen.findByRole("dialog", {
        name: "게시하기 전에 코드 펜스를 확인하세요",
      });
      expect(
        within(dialog).getByText("게시되지 않았습니다 — 형식 문제로 막혀 있습니다."),
      ).toBeInTheDocument();
      expect(
        within(dialog).queryByText(
          "Not published. Unclosed code fence opened at line 9.",
        ),
      ).toBeNull();
    } finally {
      await i18n.changeLanguage("en");
    }
  });

  it("uses a safe localized fallback for a warning code with no mapped translation (I18N-13)", async () => {
    mockPublicationFlow({
      rejectPublishWithoutOverride: true,
      rejectWarnings: [
        { code: "some_future_code", message: "A brand-new kind of rejection." },
      ],
    });
    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    await setEditorTitle(await findTitleEditor(), "Server-only finding");
    await saveDraftAndPublish();

    const dialog = await screen.findByRole("dialog", {
      name: "Check the code fence before publishing",
    });
    expect(
      within(dialog).getByText(/Not published.*formatting issue is blocking it/),
    ).toBeInTheDocument();
    expect(within(dialog).queryByText("A brand-new kind of rejection.")).toBeNull();
    expect(within(dialog).queryByText("Publishing was rejected.")).toBeNull();
  });

  it("separates Save draft from exact-version Publish", async () => {
    const fetchMock = mockPublicationFlow();
    const router = renderWithRouter(
      "/context/hello/edit",
      <Route path="/context/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    await setEditorTitle(await findTitleEditor(), "Published title");
    await typist().click(publicationActions().getByRole("button", { name: "Save draft" }));
    await waitFor(() => {
      expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/autosave"))).toBe(true);
    });
    expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/publish"))).toBe(false);
    expect(router.state.location).toMatchObject({
      pathname: "/context/hello/edit",
      search: "",
    });
    // Publish stays disabled while the draft is in flight and then publishes
    // the version that save returned, so wait for the save to settle.
    await waitFor(() =>
      expect(publicationActions().getByRole("button", { name: "Publish" })).toBeEnabled(),
    );
    await typist().click(publicationActions().getByRole("button", { name: "Publish" }));
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/publish")),
      ).toBe(true);
    });

    const autosaveIndex = fetchMock.mock.calls.findIndex((call) =>
      String(call[0]).endsWith("/autosave"),
    );
    const publishIndex = fetchMock.mock.calls.findIndex((call) =>
      String(call[0]).endsWith("/publish"),
    );
    expect(autosaveIndex).toBeGreaterThan(-1);
    expect(publishIndex).toBeGreaterThan(autosaveIndex);
    expect(
      JSON.parse(String(fetchMock.mock.calls[publishIndex][1]?.body)),
    ).toMatchObject({
      publish_despite_findings: false,
    });
    expect(String(fetchMock.mock.calls[publishIndex][0])).toContain(
      "/versions/v2/publish",
    );
    await waitFor(() => {
      expect(router.state.location).toMatchObject({
        pathname: "/context",
        search: "?target=p1",
      });
      expect(router.state.historyAction).toBe("REPLACE");
    });
  });

  it("enables Save draft and Publish together and publishes a dirty edit directly", async () => {
    const fetchMock = mockPublicationFlow();
    const router = renderWithRouter(
      "/context/hello/edit",
      <Route path="/context/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    await setEditorTitle(await findTitleEditor(), "Published directly");

    expect(publicationActions().getByRole("button", { name: "Save draft" })).toBeEnabled();
    const publishButton = publicationActions().getByRole("button", { name: "Publish" });
    expect(publishButton).toBeEnabled();

    await typist().click(publishButton);

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/publish")),
      ).toBe(true);
    });

    const autosaveIndex = fetchMock.mock.calls.findIndex((call) =>
      String(call[0]).endsWith("/autosave"),
    );
    const publishIndex = fetchMock.mock.calls.findIndex((call) =>
      String(call[0]).endsWith("/publish"),
    );
    expect(autosaveIndex).toBeGreaterThan(-1);
    expect(publishIndex).toBeGreaterThan(autosaveIndex);
    expect(String(fetchMock.mock.calls[publishIndex][0])).toContain(
      "/versions/v2/publish",
    );
    expect(
      JSON.parse(String(fetchMock.mock.calls[publishIndex][1]?.body)),
    ).toEqual({
      reason: "Published from Page editor.",
      publish_despite_findings: false,
      expected_content_hash: "b".repeat(64),
    });
    await waitFor(() => {
      expect(router.state.location).toMatchObject({
        pathname: "/context",
        search: "?target=p1",
      });
      expect(router.state.historyAction).toBe("REPLACE");
    });
  });

  it("enables direct Publish for a new Context page", async () => {
    const fetchMock = mockPublicationFlow();
    const router = renderWithRouter(
      "/context/new",
      <Route path="/context/new" element={<PageEditor mode="create" />} />,
    );

    await setEditorTitle(await findTitleEditor(), "Publish without draft");
    expect(publicationActions().getByRole("button", { name: "Save draft" })).toBeEnabled();
    expect(publicationActions().getByRole("button", { name: "Publish" })).toBeEnabled();

    await typist().click(publicationActions().getByRole("button", { name: "Publish" }));
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(
          (call) =>
            String(call[0]) === `${BASE}/api/v1/workspaces/workspace-1/context/pages` &&
            call[1]?.method === "POST",
        ),
      ).toBe(true);
      expect(
        fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/publish")),
      ).toBe(true);
    });
    await waitFor(() => {
      expect(router.state.location).toMatchObject({
        pathname: "/context",
        search: "?target=p1",
      });
    });
  });

  it("does not claim a saved draft when direct publish fails before staging", async () => {
    const fetchMock = mockPublicationFlow({ rejectAutosave: true });
    const router = renderWithRouter(
      "/context/hello/edit",
      <Route path="/context/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    await setEditorTitle(await findTitleEditor(), "Cannot stage this edit");
    await typist().click(publicationActions().getByRole("button", { name: "Publish" }));

    expect(
      await screen.findByText(
        "Could not save your latest changes, so nothing was published. Try again.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/saved draft is still available/i)).toBeNull();
    expect(
      fetchMock.mock.calls.filter(([input]) => String(input).endsWith("/publish")),
    ).toHaveLength(0);
    expect(router.state.location).toMatchObject({
      pathname: "/context/hello/edit",
      search: "",
    });
  });

  it("keeps newer edits in Context editor when an earlier publish finishes", async () => {
    let resolvePublish!: (response: Response) => void;
    const pendingPublish = new Promise<Response>((resolve) => {
      resolvePublish = resolve;
    });
    const fetchMock = mockPublicationFlow({ publishResponse: pendingPublish });
    const router = renderWithRouter(
      "/context/hello/edit",
      <Route path="/context/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    await setEditorTitle(await findTitleEditor(), "Version being published");
    await typist().click(publicationActions().getByRole("button", { name: "Publish" }));
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/publish")),
      ).toBe(true);
    });

    await setEditorTitle(getTitleEditor(), "Newer editor change");
    resolvePublish(jsonResponse({
      data: {
        ...page,
        content_hash: "b".repeat(64),
        current_version_id: "v2",
        parent_version_id: "v1",
        version_id: "v2",
      },
    }));

    expect(
      await screen.findByText(
        "An earlier version was published. Your newer changes remain in the editor.",
      ),
    ).toBeInTheDocument();
    expect(router.state.location).toMatchObject({
      pathname: "/context/hello/edit",
      search: "",
    });
    expect(getTitleEditor()).toHaveValue("Newer editor change");
  });

  it("maps Ctrl+S to Save draft without publishing", async () => {
    const fetchMock = mockPublicationFlow();
    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    await setEditorTitle(await findTitleEditor(), "Honest label");
    expect(syncStatus()).toHaveTextContent("Editing");
    expect(publicationActions().getByRole("button", { name: "Save draft" })).toHaveClass("primary");
    fireEvent.keyDown(window, { key: "s", ctrlKey: true });
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/autosave")),
      ).toBe(true);
    });
    expect(fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/publish"))).toBe(false);
  });

  it.each([
    [
      "a page with a published head and a staged draft",
      { version_status: "draft", current_published_version_id: "v0" },
      /unpublished draft saved/i,
    ],
    [
      "a page that has never been published",
      { version_status: "draft", current_published_version_id: null },
      /draft saved · not published yet/i,
    ],
    [
      "a fully published page",
      { version_status: "published_snapshot", current_published_version_id: "v1" },
      /^saved$/i,
    ],
  ])(
    // PUB-09: the clean-state SyncPill used to say "Saved" for every
    // publicationState, so a page with unpublished changes and a fully
    // published page were indistinguishable except for the presence of a
    // "Publish" button. The i18n strings existed (syncPillDraftSaved /
    // syncPillDraftNeverPublished) but nothing rendered them.
    "labels the clean SyncPill by publicationState for %s",
    async (_label, overrides, expectedText) => {
      vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
        const url = String(input);
        if (url === `${BASE}/api/v1/workspaces`) return workspaceResponse();
        if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
          return jsonResponse({ data: contextTreeNodes() });
        }
        if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
          return jsonResponse({ data: [page], next_cursor: null });
        }
        return jsonResponse({ data: { ...page, ...overrides } });
      });

      renderWithRouter(
        "/pages/hello/edit",
        <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
      );

      await findTitleEditor();
      expect(syncStatus()).toHaveTextContent(expectedText);
    },
  );

  it("does not duplicate the Publish CTA between the SyncPill label and its button", async () => {
    // Regression guard: the initial (rejected) fix wired the catalog strings
    // ("...Publish now") straight through, which rendered next to the
    // already-present Publish button and duplicated the call to action.
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) return workspaceResponse();
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      return jsonResponse({
        data: { ...page, version_status: "draft", current_published_version_id: null },
      });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    await findTitleEditor();
    expect(syncStatus()).toHaveTextContent(/draft saved · not published yet/i);
    expect(screen.queryAllByText(/publish now/i)).toHaveLength(0);
    expect(publicationActions().getByRole("button", { name: "Publish" })).toBeInTheDocument();
  });

  it("keeps the create editor unavailable until its workspace context is loaded", async () => {
    let resolveWorkspace!: (response: Response) => void;
    const delayedWorkspace = new Promise<Response>((resolve) => {
      resolveWorkspace = resolve;
    });
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return delayedWorkspace;
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [], next_cursor: null });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    renderWithRouter(
      "/pages/new?title=Release%20plan",
      <Route path="/pages/new" element={<PageEditor mode="create" />} />,
    );

    expect(screen.getByRole("heading", { name: "Loading page" })).toBeInTheDocument();
    expect(screen.queryByTestId("page-title-editor")).toBeNull();

    await act(async () => {
      resolveWorkspace(workspaceResponse());
      await delayedWorkspace;
    });

    expect(await findTitleEditor()).toHaveValue("Release plan");
  });

  it("does not touch default-slug preferences before the selected workspace resolves", async () => {
    const defaultPreferenceKey = "dtxwiki:editor:default:hello";
    const personalPreferenceKey = "dtxwiki:editor:personal-space:hello";
    const defaultPreference = JSON.stringify({
      mode: "reading",
      splitPercent: 17,
    });
    window.localStorage.setItem(defaultPreferenceKey, defaultPreference);
    window.localStorage.setItem(
      personalPreferenceKey,
      JSON.stringify({ mode: "split", splitPercent: 42 }),
    );
    let resolveWorkspace!: (response: Response) => void;
    const delayedWorkspace = new Promise<Response>((resolve) => {
      resolveWorkspace = resolve;
    });
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return delayedWorkspace;
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/workspace-personal/tree?section=context`
      ) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/workspace-personal/library/items?limit=100`
      ) {
        return jsonResponse({ data: [] });
      }
      if (
        url.includes(
          "/api/v1/workspaces/workspace-personal/context/pages?",
        )
      ) {
        return jsonResponse({
          data: [{ ...page, workspace_id: "workspace-personal" }],
          next_cursor: null,
        });
      }
      return jsonResponse({
        data: { ...page, workspace_id: "workspace-personal" },
      });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    expect(screen.getByRole("heading", { name: "Loading page" })).toBeInTheDocument();
    expect(window.localStorage.getItem(defaultPreferenceKey)).toBe(
      defaultPreference,
    );

    await act(async () => {
      resolveWorkspace(
        jsonResponse({
          data: {
            current_workspace_id: "workspace-personal",
            default_workspace_id: "workspace-personal",
            personal_workspace_id: "workspace-personal",
            workspaces: [
              {
                id: "workspace-personal",
                slug: "personal-space",
                title: "Personal Space",
                name: "Personal Space",
                goal: "Personal notes",
                default_language: "en",
                visibility: "private",
                role: "owner",
              },
            ],
          },
        }),
      );
      await delayedWorkspace;
    });

    expect(await screen.findByLabelText("Markdown body")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Split" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(window.localStorage.getItem(defaultPreferenceKey)).toBe(
      defaultPreference,
    );
    await waitFor(() => {
      expect(
        JSON.parse(window.localStorage.getItem(personalPreferenceKey) ?? "{}"),
      ).toEqual({ mode: "split", splitPercent: 42 });
    });
  });

  it("prefills new page title and destination from tree item query params", async () => {
    vi.stubGlobal("innerWidth", 768);
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages/tags?")) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [], next_cursor: null });
      }
      if (init?.method === "POST") {
        return jsonResponse({
          data: { ...page, slug: "nested-brief", title: "Nested Brief" },
        }, 201);
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/new?from=context&parentNodeId=context-inbox&title=Nested%20Brief",
      <>
          <Route path="/pages/new" element={<PageEditor mode="create" />} />
          <Route path="/pages/:slug/edit" element={<div>editing created page</div>} />
          <Route path="/context/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    await expectEditorTitle("Nested Brief");
    const drawer = await openContextExplorer();
    const contextTree = within(drawer).getByLabelText("Context tree");
    expect(within(contextTree).queryByRole("treeitem", { name: "Context" }))
      .not.toBeInTheDocument();
    expect(within(contextTree).getByRole("treeitem", { name: "Inbox" }))
      .toBeInTheDocument();
    fireEvent.keyDown(window, { key: "s", ctrlKey: true });

    await waitFor(() => {
      const createCall = fetchMock.mock.calls.find((call) => call[1]?.method === "POST");
      expect(JSON.parse(String(createCall?.[1]?.body))).toMatchObject({
        title: "Nested Brief",
        placement_parent_node_id: "context-inbox",
      });
    });
    expect(screen.getByLabelText("Context tree")).toBeInTheDocument();
    expect(document.querySelector(".page-list")).not.toBeInTheDocument();
  });

  it("keeps legacy cover frontmatter inert and exact without requesting it", async () => {
    const legacyCoverUrl = "https://tracking.example/legacy-cover.png";
    const coverLine = `cover: "${legacyCoverUrl}"`;
    const fetchMock = mockEditorPage([
      "---",
      'title: "Hello"',
      "slug: hello",
      coverLine,
      "type: overview",
      "---",
      "# Hello",
    ].join("\n"));

    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    await setEditorTitle(await findTitleEditor(), "Release notes");
    await openPageProperties();
    expect(screen.queryByLabelText("Cover URL")).not.toBeInTheDocument();
    fireEvent.keyDown(window, { key: "s", ctrlKey: true });

    await waitFor(() => expect(writeCalls(fetchMock)).toHaveLength(1));
    const request = JSON.parse(String(writeCalls(fetchMock)[0]?.[1]?.body));
    expect(request.body_md.split("\n").filter((line: string) => line === coverLine))
      .toHaveLength(1);
    expect(fetchMock.mock.calls.some(([input]) => String(input) === legacyCoverUrl)).toBe(false);
  });

  it("surfaces a rejected over-long alias and keeps it out of the saved payload", async () => {
    const fetchMock = mockEditorPage("# Hello");
    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    await screen.findByLabelText("Markdown body");
    await openPageProperties();
    await typist().click(pageProperties().getByRole("button", { name: "Advanced settings" }));
    const aliasesInput = screen.getByLabelText("Aliases");
    // Commit one valid alias first: it makes the draft dirty, so the Ctrl+S
    // below actually writes and can prove which aliases reached the payload.
    fireEvent.change(aliasesInput, { target: { value: "launch plan" } });
    fireEvent.keyDown(aliasesInput, { key: "Enter" });
    const longAlias = "a".repeat(81);
    fireEvent.change(aliasesInput, { target: { value: longAlias } });
    fireEvent.keyDown(aliasesInput, { key: "Enter" });

    // Aliases run the ChipInput with truncateItems={false}: an over-long entry
    // is refused, not silently trimmed, so no chip is committed and the input
    // keeps the text for the user to fix. The refusal has to be *visible* —
    // before onItemTooLong was wired, Enter was a silent no-op.
    expect(screen.queryByRole("button", { name: `Remove ${longAlias}` }))
      .not.toBeInTheDocument();
    expect(aliasesInput).toHaveAttribute("aria-invalid", "true");
    expect(aliasesInput).toHaveAccessibleDescription(
      `Alternate page names used by search and wikilinks. "${longAlias}" is too long.` +
        " Each alias can be at most 80 characters.",
    );
    fireEvent.change(aliasesInput, { target: { value: "corrected alias" } });
    fireEvent.keyDown(aliasesInput, { key: "Enter" });
    expect(aliasesInput).not.toHaveAttribute("aria-invalid");
    // Nothing invalid was committed, so saving the rest of the page stays
    // allowed — the same contract CustomFormatEditor and FormatAliasGroupList
    // use for a rejected item.
    fireEvent.keyDown(window, { key: "s", ctrlKey: true });
    await waitFor(() => expect(writeCalls(fetchMock)).toHaveLength(1));
    const saved = String(
      JSON.parse(String(writeCalls(fetchMock)[0]?.[1]?.body)).body_md,
    );
    expect(saved).toContain("launch plan");
    expect(saved).toContain("corrected alias");
    expect(saved).not.toContain(longAlias);
  });

  it("appends per-keystroke typing into the title without reversing the order", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages/tags?")) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [], next_cursor: null });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/new",
      <>
        <Route path="/pages/new" element={<PageEditor mode="create" />} />
      </>,
    );

    const titleInput = await findTitleEditor();
    expect(titleInput.tagName).toBe("TEXTAREA");
    const user = userEvent.setup();
    titleInput.focus();
    await user.keyboard("Hello world");

    expect(titleInput).toHaveValue("Hello world");
  });

  it("does not submit the title while IME composition is active", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <>
        <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    const titleInput = await findTitleEditor();
    const view = codeMirrorView();
    const focusSpy = vi.spyOn(view, "focus");
    const event = new KeyboardEvent("keydown", {
      bubbles: true,
      cancelable: true,
      key: "Enter",
    });
    Object.defineProperty(event, "isComposing", { value: true });

    await act(async () => {
      titleInput.dispatchEvent(event);
    });

    expect(focusSpy).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(false);
  });

  it("completes a routed review edit after a manual save", async () => {
    let decisionBody: Record<string, unknown> | null = null;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/context/pages/by-slug/hello`) {
        return jsonResponse({ data: page });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/context/pages/p1/autosave` && init?.method === "POST") {
        return jsonResponse({
          data: {
            ...page,
            title: "Hello revised",
            version_id: "v2",
            current_version_id: "v2",
            updated_at: "2026-05-08T00:05:00Z",
          },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/workspace-1/context/pages/p1/versions/v2/publish` &&
        init?.method === "POST"
      ) {
        return jsonResponse({
          data: {
            ...page,
            title: "Hello revised",
            version_id: "v2",
            current_version_id: "v2",
            updated_at: "2026-05-08T00:05:00Z",
          },
        });
      }
      if (
        url ===
          `${BASE}/api/v1/workspaces/workspace-1/reviews/review-1/decision` &&
        init?.method === "POST"
      ) {
        decisionBody = JSON.parse(String(init.body));
        return jsonResponse({ data: { id: "review-1", status: "approved" } });
      }
      return jsonResponse({ data: page });
    });

    const router = renderWithRouter(
      "/pages/hello/edit?from=review&reviewItemId=review-1",
      <>
        <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    await setEditorTitle(await findTitleEditor(), "Hello revised");
    fireEvent.keyDown(window, { key: "s", ctrlKey: true });
    await waitFor(() => expect(publicationActions().getByRole("button", { name: "Publish" })).toBeEnabled());
    await typist().click(publicationActions().getByRole("button", { name: "Publish" }));

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(
          (call) =>
            call[0] ===
              `${BASE}/api/v1/workspaces/workspace-1/reviews/review-1/decision` &&
            call[1]?.method === "POST",
        ),
      ).toBe(true);
    });
    expect(decisionBody).toEqual({
      decision_type: "approve",
      decision_reason: "Edited directly in Page editor.",
    });
    await waitFor(() => {
      expect(router.state.location).toMatchObject({
        pathname: "/context",
        search: "?target=p1",
        state: { contextPublishNotice: { kind: "review_completed" } },
      });
    });
  });

  it.each(["resolves", "rejects"] as const)(
    "does not redirect when a pending review completion %s after the editor route unmounts",
    async (outcome) => {
      let settleDecision!: (response: Response) => void;
      const pendingDecision = new Promise<Response>((resolve, reject) => {
        settleDecision = outcome === "resolves"
          ? resolve
          : () => reject(new Error("review service unavailable"));
      });
      let resolvePublish!: (response: Response) => void;
      const pendingPublish = new Promise<Response>((resolve) => {
        resolvePublish = resolve;
      });
      const fetchMock = mockPublicationFlow({
        publishResponse: pendingPublish,
        reviewDecisionResponse: pendingDecision,
      });
      const router = renderWithRouter(
        "/pages/hello/edit?from=review&reviewItemId=review-1",
        <>
          <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
          <Route path="/settings" element={<div>Settings destination</div>} />
        </>,
      );

      await setEditorTitle(await findTitleEditor(), "Hello revised");
      await typist().click(publicationActions().getByRole("button", { name: "Publish" }));
      await waitFor(() => {
        expect(
          fetchMock.mock.calls.some(([input]) => String(input).endsWith("/publish")),
        ).toBe(true);
      });

      // Handling the publish response requests the review decision in the same
      // turn, but the render that records "published", and the effects that give
      // the router a guard that lets the route go, are left to the scheduler.
      // Outside act the decision can be seen before they have run, and leaving
      // then is refused as unsaved work. Inside act they have run by the time
      // it returns, with the decision still pending.
      await act(async () => {
        resolvePublish(jsonResponse({
          data: {
            ...page,
            content_hash: "b".repeat(64),
            current_version_id: "v2",
            parent_version_id: "v1",
            version_id: "v2",
          },
        }));
        await vi.waitFor(() => {
          expect(
            fetchMock.mock.calls.some(
              ([input, init]) =>
                String(input).endsWith("/reviews/review-1/decision") &&
                init?.method === "POST",
            ),
          ).toBe(true);
        });
      });
      // The page shows nothing for that render -- the sync pill reads "Saving…"
      // until the decision settles -- but the beforeunload handler is
      // re-registered in the same commit as the router's guard, and stops
      // asking to keep the page once that commit's effects have run.
      expect(
        window.dispatchEvent(new Event("beforeunload", { cancelable: true })),
      ).toBe(true);

      const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
      await act(async () => {
        await router.navigate("/settings");
      });
      expect(confirmSpy).not.toHaveBeenCalled();
      expect(router.state.location.pathname).toBe("/settings");
      expect(await screen.findByText("Settings destination")).toBeInTheDocument();

      await act(async () => {
        settleDecision(jsonResponse({
          data: { id: "review-1", status: "approved" },
        }));
        await pendingDecision.catch(() => undefined);
        await Promise.resolve();
        await Promise.resolve();
      });

      expect(router.state.location).toMatchObject({
        pathname: "/settings",
        search: "",
      });
    },
  );

  it("shows not-found state without mounting an empty editor draft", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [], next_cursor: null });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/context/pages/by-slug/missing`) {
        return jsonResponse(
          {
            error_code: "not_found",
            message: "page not found",
            request_id: "r1",
            warnings: [],
          },
          404,
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    renderWithRouter(
      "/pages/missing/edit",
      <>
        <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    // Shares the route-level 404's copy and action order (NotFoundScreen), so
    // a missing page reads the same whether it was opened for edit or reading.
    expect(
      await screen.findByRole("heading", {
        name: "We couldn't find that page",
      }),
    ).toBeInTheDocument();
    const actions = within(
      document.querySelector(".empty-state-actions") as HTMLElement,
    ).getAllByRole("link");
    expect(actions[0]).toHaveAccessibleName("Back to dashboard");
    expect(actions[0]).toHaveClass("primary");
    expect(actions[1]).toHaveAccessibleName("Open Context");
    expect(screen.queryByTestId("page-title-editor")).toBeNull();
    expect(document.querySelector(".cm-editor")).toBeNull();
  });

  it("keeps the editor usable when citation suggestions fail to load", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ message: "source load failed" }, 500);
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <>
        <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    await expectEditorTitle("Hello");
    expect(screen.getByText(/Citation suggestions could not be loaded/)).toBeInTheDocument();
    await waitFor(() => {
      expect(document.querySelector(".cm-content")?.textContent).toContain("# Hello");
    });
  });

  it("shows a non-editable failure state when the page body cannot be loaded", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/context/pages/by-slug/hello`) {
        return jsonResponse(
          {
            error_code: "internal_error",
            message: "database read failed",
            request_id: "req-load-failure",
            warnings: [],
          },
          500,
        );
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    renderWithRouter(
      "/pages/hello/edit",
      <>
        <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    expect(
      await screen.findByRole("heading", { name: /Load failed/ }),
    ).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/editing is disabled/);
    expect(screen.queryByTestId("page-title-editor")).toBeNull();
    expect(document.querySelector(".cm-editor")).toBeNull();
    expect(screen.queryByText(/database read failed/)).toBeNull();
  });

  it("uses API metadata over stale frontmatter when loading properties", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      return jsonResponse({
        data: {
          ...page,
          page_type: "guide",
          tags: ["Server Tag", "R&D"],
          aliases: ["API Alias"],
          body_md: [
            "---",
            "type: decision",
            'tags: ["stale-tag"]',
            'aliases: ["stale alias"]',
            "---",
            "",
            "# Hello",
          ].join("\n"),
        },
      });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <>
          <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    await expectEditorTitle("Hello");
    expect(screen.queryByLabelText("Page type")).not.toBeInTheDocument();
    await openPageProperties();
    const destination = screen.getByLabelText("Destination");
    expect(destination).not.toBeDisabled();
    expect(destination).toHaveTextContent("Context / Inbox");
    expect(destination).toHaveAttribute("title", "Context / Inbox");
    expect(screen.getByText("Server Tag")).toBeInTheDocument();
    expect(screen.getByText("R&D")).toBeInTheDocument();
    await typist().click(pageProperties().getByRole("button", { name: "Advanced settings" }));
    expect(screen.getByText("API Alias")).toBeInTheDocument();
  });

  it("omits the current page branch from destination choices", async () => {
    vi.stubGlobal("innerWidth", 768);
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodesWithPageBranch() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <>
          <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    await expectEditorTitle("Hello");
    const drawer = await openContextExplorer();
    const contextTree = within(drawer).getByLabelText("Context tree");
    expect(contextTree).toBeInTheDocument();
    await typist().click(
      within(drawer).getByRole("button", { name: "Close Context explorer" }),
    );
    await waitFor(() => expect(drawer).toHaveAttribute("aria-hidden", "true"));
    expect(drawer).toHaveAttribute("inert", "");

    await openPageProperties();
    await typist().click(pageProperties().getByRole("button", { name: "Destination" }));

    expect(await screen.findByRole("treeitem", { name: /Inbox/ })).toBeInTheDocument();
    expect(screen.queryByRole("treeitem", { name: /Hello Page/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("treeitem", { name: /Nested Folder/ })).not.toBeInTheDocument();
  });

  it("expands the context rail to the page being edited (item 23 follow-up)", async () => {
    vi.stubGlobal("innerWidth", 768);
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({
          data: contextTreeNodesWithPageBranch().map((node) =>
            node.id === "context-inbox"
              ? { ...node, children_count: 1 }
              : node,
          ),
        });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit?from=context",
      <>
          <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    await expectEditorTitle("Hello");
    await openContextExplorer();
    // Edit mode carries no parentNodeId query param, but the rail must still
    // expand the edited page's resolved path (Inbox > Hello Page) instead of
    // collapsing it. Deeper branches (Nested Folder) stay collapsed.
    expect(await screen.findByRole("treeitem", { name: /Hello Page/ }))
      .toBeInTheDocument();
    expect(screen.getByRole("treeitem", { name: /Inbox/ })).toBeInTheDocument();
    expect(screen.queryByRole("treeitem", { name: /Nested Folder/ }))
      .not.toBeInTheDocument();

    await typist().click(screen.getByRole("button", { name: "Collapse Inbox" }));
    expect(screen.queryByRole("treeitem", { name: /Hello Page/ }))
      .not.toBeInTheDocument();
    await typist().click(
      screen.getByRole("button", { name: "Refresh Context tree" }),
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Expand Inbox" })).toBeEnabled(),
    );
    expect(screen.queryByRole("treeitem", { name: /Hello Page/ }))
      .not.toBeInTheDocument();
  });

  it("expands the context rail to the fallback destination on a no-parent create (item 23 follow-up)", async () => {
    vi.stubGlobal("innerWidth", 768);
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodesWithPageBranch() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages/tags?")) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [], next_cursor: null });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/new?from=context",
      <>
          <Route path="/pages/new" element={<PageEditor mode="create" />} />
      </>,
    );

    await openContextExplorer();

    // The topbar create route carries no parentNodeId; the rail must still
    // expand the resolved fallback destination so its path is open, not
    // collapsed. That destination is now the Context root rather than Inbox --
    // Inbox stages writes arriving without a directory (MCP, Ask captures), so
    // routing hand-authored pages there was wrong. Expanding the root reveals
    // its children; anything deeper (Hello Page under Inbox, Nested Folder)
    // stays collapsed.
    expect(await screen.findByRole("treeitem", { name: /Inbox/ }))
      .toBeInTheDocument();
    expect(screen.queryByRole("treeitem", { name: /Hello Page/ }))
      .not.toBeInTheDocument();
    expect(screen.queryByRole("treeitem", { name: /Nested Folder/ }))
      .not.toBeInTheDocument();
  });

  it("opens and closes the non-modal page properties popover", async () => {
    vi.stubGlobal("innerWidth", 1440);
    vi.stubGlobal("innerHeight", 900);
    const rectSpy = vi
      .spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockImplementation(function getMockRect(this: HTMLElement) {
        if (this === document.documentElement || this === document.body) {
          return domRect({ height: 900, left: 0, top: 0, width: 1440 });
        }
        if (this.classList.contains("editor-properties-button")) {
          return domRect({ height: 28, left: 1260, top: 104, width: 32 });
        }
        if (this.classList.contains("editor-properties-popover")) {
          return domRect({ height: 300, left: 0, top: 0, width: 340 });
        }
        return domRect({ height: 0, left: 0, top: 0, width: 0 });
      });
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <>
          <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    await screen.findByLabelText("Markdown body");
    const toggle = screen.getByRole("button", { name: "Page properties" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    await typist().click(toggle);
    const popover = await screen.findByRole("region", { name: "Page properties" });
    expect(popover).toBeInTheDocument();
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    await waitFor(() => {
      expect(popover).toHaveStyle({ top: "140px" });
    });
    expect(popover.style.left).not.toBe("");
    expect(popover.style.visibility).toBe("");
    expect(rectSpy).toHaveBeenCalled();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("region", { name: "Page properties" })).not.toBeInTheDocument();
    expect(toggle).toHaveFocus();

    await typist().click(toggle);
    expect(await screen.findByRole("region", { name: "Page properties" })).toBeInTheDocument();
    fireEvent.pointerDown(screen.getByTestId("page-title-editor"));
    expect(screen.queryByRole("region", { name: "Page properties" })).not.toBeInTheDocument();
  });

  it("keeps a portaled Destination picker owned by page properties while expanding branches", async () => {
    const fetchMock = await renderDestinationPickerEditor();
    const propertiesToggle = screen.getByRole("button", { name: "Page properties" });
    await typist().click(propertiesToggle);
    const properties = await screen.findByRole("region", { name: "Page properties" });
    const destination = within(properties).getByRole("button", { name: "Destination" });
    const originalDestination = destination.textContent;

    await typist().click(destination);
    const picker = await screen.findByRole("tree", { name: "Destination tree" });
    // The menu intentionally remains a body portal for floating geometry; the
    // properties popover must still recognize it as its owned child surface.
    expect(properties.contains(picker)).toBe(false);

    const contextRow = within(picker).getByRole("treeitem", { name: /Context/ });
    await typist().click(
      within(contextRow).getByRole("button", { name: "Expand Context" }),
    );
    const inboxRow = await within(picker).findByRole("treeitem", { name: /Inbox/ });
    const inboxToggle = within(inboxRow).getByRole("button", { name: "Expand Inbox" });

    // This is the production failure sequence: parent document pointerdown
    // used to dismiss the properties surface before the chevron click ran.
    fireEvent.pointerDown(inboxToggle);
    expect(properties).toBeInTheDocument();
    expect(picker).toBeInTheDocument();
    expect(destination).toHaveTextContent(originalDestination ?? "");
    fireEvent.click(inboxToggle);

    expect(inboxToggle).toHaveAttribute("aria-expanded", "true");
    const pageRow = await within(picker).findByRole("treeitem", { name: /Hello Page/ });
    expect(properties).toBeInTheDocument();
    expect(destination).toHaveTextContent(originalDestination ?? "");
    expect(
      fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH" || init?.method === "POST"),
    ).toBe(false);

    await typist().click(pageRow);
    expect(properties).toBeInTheDocument();
    expect(screen.queryByRole("tree", { name: "Destination tree" })).not.toBeInTheDocument();
    expect(destination).toHaveTextContent(/Hello Page/);
    expect(destination).toHaveFocus();
  });

  it("dismisses the Destination picker and Page properties one layer at a time", async () => {
    await renderDestinationPickerEditor();
    const propertiesToggle = screen.getByRole("button", { name: "Page properties" });
    await typist().click(propertiesToggle);
    const properties = await screen.findByRole("region", { name: "Page properties" });
    const destination = within(properties).getByRole("button", { name: "Destination" });

    await typist().click(destination);
    await screen.findByRole("tree", { name: "Destination tree" });
    await typist().keyboard("{Escape}");
    expect(properties).toBeInTheDocument();
    expect(screen.queryByRole("tree", { name: "Destination tree" })).not.toBeInTheDocument();
    expect(destination).toHaveFocus();

    await typist().keyboard("{Escape}");
    expect(screen.queryByRole("region", { name: "Page properties" })).not.toBeInTheDocument();
    expect(propertiesToggle).toHaveFocus();

    await typist().click(propertiesToggle);
    const reopenedProperties = await screen.findByRole("region", { name: "Page properties" });
    const reopenedDestination = within(reopenedProperties).getByRole("button", {
      name: "Destination",
    });
    await typist().click(reopenedDestination);
    await screen.findByRole("tree", { name: "Destination tree" });
    fireEvent.pointerDown(within(reopenedProperties).getByLabelText("Page type"));
    await waitFor(() =>
      expect(screen.queryByRole("tree", { name: "Destination tree" })).not.toBeInTheDocument(),
    );
    expect(reopenedProperties).toBeInTheDocument();

    await typist().click(reopenedDestination);
    await screen.findByRole("tree", { name: "Destination tree" });
    fireEvent.pointerDown(screen.getByTestId("page-title-editor"));
    expect(screen.queryByRole("region", { name: "Page properties" })).not.toBeInTheDocument();
  });

  it("preserves stale updates as alternative drafts and rebases the editor onto the server head", async () => {
    // PUB-02 (2): _preserve_alternative parks the caller's bytes on a
    // branched version that never becomes page.current_version_id, so
    // adopting the PATCH response directly (old behavior) would leave the
    // session autosaving onto a version the server no longer treats as
    // editable -- guaranteeing the very next autosave 409s again. The fix
    // rebases onto the server head via reloadLatest instead, so this test
    // asserts on the server head content, not the preserved alternative's.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const serverHead = {
      ...page,
      title: "Server head",
      body_md: "# Server head",
      version_id: "v2",
      current_version_id: "v2",
      content_hash: "b".repeat(64),
    };
    let pageReadCount = 0;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages/by-slug/hello")) {
        // First read is the editor's initial load; the reload triggered by
        // preserveAlternative's rebase is the one that must see the head
        // that made the earlier autosave stale.
        pageReadCount += 1;
        return jsonResponse({ data: pageReadCount === 1 ? page : serverHead });
      }
      if (init?.method === "POST" && url.endsWith("/autosave")) {
        const body = JSON.parse(String(init.body));
        if (body.base_version_id === "v1") {
          return jsonResponse(
            {
              error_code: "stale_base_version",
              message: "stale",
              request_id: "r1",
              warnings: [],
              server_version_id: "v2",
              server_content_hash: "b".repeat(64),
            },
            409,
          );
        }
        return jsonResponse({
          data: {
            ...serverHead,
            title: body.title,
            version_id: "v3",
            current_version_id: "v3",
            content_hash: "c".repeat(64),
          },
        });
      }
      if (init?.method === "PATCH") {
        const body = JSON.parse(String(init.body));
        if (body.on_stale === "preserve_as_alternative") {
          return jsonResponse({
            data: {
              ...page,
              version_id: "v-alt",
              parent_version_id: "v1",
              alternative_draft_id: "v-alt",
              change_set_id: "change-1",
              review_item_id: "review-1",
              preserved_as_alternative: true,
            },
          });
        }
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <>
          <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    await setEditorTitle(await findTitleEditor(), "My alternative");
    fireEvent.keyDown(window, { key: "s", ctrlKey: true });

    expect(await screen.findByText(/Someone else updated this page/)).toBeInTheDocument();
    expect(syncStatus()).toHaveTextContent("Out of date · Resolve");
    expect(document.querySelectorAll(".status-banner")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Reload latest" })).toBeInTheDocument();
    await typist().click(
      screen.getByRole("button", { name: "Preserve mine as alternative" }),
    );

    await screen.findByText(/Preserved as alternative draft v-alt/);
    const preserveCall = fetchMock.mock.calls.find((call) =>
      String(call[1]?.body).includes("preserve_as_alternative"),
    );
    expect(JSON.parse(String(preserveCall?.[1]?.body))).toMatchObject({
      on_stale: "preserve_as_alternative",
      base_version_id: "v1",
      base_content_hash: "a".repeat(64),
    });

    // Rebased onto the server head, not left on the preserved branch: the
    // buffer now shows "Server head", not "My alternative".
    expect(
      fetchMock.mock.calls.some((call) => String(call[0]).includes("/by-slug/hello")),
    ).toBe(true);
    await expectEditorTitle("Server head");

    const titleEditor = getTitleEditor();
    await setEditorTitle(titleEditor, "Temporary title");
    await setEditorTitle(titleEditor, "Server head edited");
    await advanceAutosaveCheckpoint();
    // Two /autosave POSTs total across the whole test: the original stale
    // attempt (base v1, 409'd before the reload) and this new checkpoint.
    // The latter must carry the server head as its base, proving the
    // session rebased instead of continuing to autosave onto the parked
    // alternative branch.
    await waitFor(() => expect(autosaveCalls(fetchMock)).toHaveLength(2));
    expect(
      JSON.parse(String(autosaveCalls(fetchMock)[1][1]?.body)),
    ).toMatchObject({
      base_version_id: "v2",
      base_content_hash: "b".repeat(64),
    });
    await waitFor(() => {
      expect(document.querySelector(".page-workspace")).toHaveAttribute(
        "data-save-state",
        "clean",
      );
    });
  });

  it("shows a conflict banner on stale updates and blocks stale retry saves", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let pageReadCount = 0;
    const serverLatest = {
      ...page,
      title: "Server latest",
      body_md: "# Server latest",
      version_id: "v2",
      current_version_id: "v2",
      content_hash: "b".repeat(64),
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages/by-slug/hello")) {
        pageReadCount += 1;
        return jsonResponse({ data: pageReadCount === 1 ? page : serverLatest });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages/p1/diff")) {
        expect(url).toContain("from_version_id=v1");
        expect(url).toContain("to_version_id=v2");
        return jsonResponse({
          data: {
            from_version_id: "v1",
            to_version_id: "v2",
            from_version_no: 1,
            to_version_no: 2,
            lines: [
              "--- v1",
              "+++ v2",
              "@@ -1 +1 @@",
              "-Old server text",
              "+New server text",
            ],
            additions: 1,
            deletions: 1,
          },
        });
      }
      if (init?.method === "POST" && url.endsWith("/autosave")) {
        return jsonResponse(
          {
            error_code: "stale_base_version",
            message: "stale",
            request_id: "r1",
            warnings: [],
            server_version_id: "v2",
            server_content_hash: "b".repeat(64),
          },
          409,
        );
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <>
          <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    await setEditorTitle(await findTitleEditor(), "My stale edit");
    fireEvent.keyDown(window, { key: "s", ctrlKey: true });

    expect(await screen.findByText(/Someone else updated this page/)).toBeInTheDocument();
    await typist().click(screen.getByRole("button", { name: "View their changes" }));
    const serverChanges = await screen.findByLabelText("Server changes");
    expect(await within(serverChanges).findByLabelText("Diff v1 to v2"))
      .toHaveTextContent("New server text");
    expect(within(serverChanges).getByText("+1 / -1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload latest" })).toBeInTheDocument();
    expect(publicationActions().getByRole("button", { name: "Save draft" })).toBeDisabled();

    fireEvent.keyDown(window, { key: "s", ctrlKey: true });
    const staleAutosaveCalls = fetchMock.mock.calls.filter(
      (call) => call[1]?.method === "POST" && String(call[0]).endsWith("/autosave"),
    );
    expect(staleAutosaveCalls).toHaveLength(1);

    await typist().click(screen.getByRole("button", { name: "Reload latest" }));
    await expectEditorTitle("Server latest");
    const titleEditor = getTitleEditor();
    await setEditorTitle(titleEditor, "Temporary server title");
    await setEditorTitle(titleEditor, "Server latest");
    await advanceAutosaveCheckpoint();
    expect(autosaveCalls(fetchMock)).toHaveLength(1);
    expect(document.querySelector(".page-workspace")).toHaveAttribute(
      "data-save-state",
      "clean",
    );
  });

  it("does not expose a raw generic error when loading conflict changes", async () => {
    const rawSecret = "raw-diff-secret=internal-host.example";
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages/p1/diff")) {
        throw new Error(rawSecret);
      }
      if (init?.method === "POST" && url.endsWith("/autosave")) {
        return jsonResponse(
          {
            error_code: "stale_base_version",
            message: "stale",
            request_id: "r-secret-diff",
            warnings: [],
            server_version_id: "v2",
            server_content_hash: "b".repeat(64),
          },
          409,
        );
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    await setEditorTitle(await findTitleEditor(), "My stale edit");
    fireEvent.keyDown(window, { key: "s", ctrlKey: true });
    expect(await screen.findByText(/Someone else updated this page/)).toBeInTheDocument();

    await typist().click(screen.getByRole("button", { name: "View their changes" }));
    const serverChanges = await screen.findByLabelText("Server changes");
    expect(
      await within(serverChanges).findByText("Failed to load server changes"),
    ).toBeInTheDocument();
    expect(screen.queryByText(rawSecret)).toBeNull();
  });

  it("does not request a version diff when a conflict names the editor's own version", async () => {
    // PUB-02 (3): an in-place autosave overwrite (two tabs on the same
    // draft) makes the server report server_version_id === the editor's own
    // page.version_id. Diffing a version against itself is always empty and
    // misleading, so "View their changes" must short-circuit to an
    // explanatory message instead of firing /diff.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      if (init?.method === "POST" && url.endsWith("/autosave")) {
        return jsonResponse(
          {
            error_code: "stale_base_version",
            message: "stale",
            request_id: "r1",
            warnings: [],
            // Same version id the editor already has -- the in-place
            // overwrite shape, not a genuine different-version conflict.
            server_version_id: "v1",
            server_content_hash: "d".repeat(64),
          },
          409,
        );
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <>
          <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    await setEditorTitle(await findTitleEditor(), "My in-place edit");
    fireEvent.keyDown(window, { key: "s", ctrlKey: true });

    expect(await screen.findByText(/Someone else updated this page/)).toBeInTheDocument();
    await typist().click(screen.getByRole("button", { name: "View their changes" }));

    const serverChanges = await screen.findByLabelText("Server changes");
    expect(within(serverChanges).getByText(
      /overwritten in place by another session/i,
    )).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some((call) => String(call[0]).includes("/diff")),
    ).toBe(false);
  });

  it("treats a stale publish target as a publish-eligibility failure, not a base conflict", async () => {
    // PUB-07: a 409 stale_context_draft means the publish target is not the
    // latest linear eligible draft (a second session, or a review that
    // forked a successor draft) -- the staged bytes are fine. The old catch
    // classified every 409 as a base conflict, which opened the conflict
    // banner (with no server_version_id to compare against, since this error
    // body carries none) and froze autosave via Boolean(conflict) for a
    // failure that has nothing to do with content staleness.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      if (init?.method === "POST" && url.endsWith("/publish")) {
        return jsonResponse(
          {
            error_code: "stale_context_draft",
            message: "context draft is stale or is not the latest linear draft",
            request_id: "req-stale-draft",
            warnings: [],
          },
          409,
        );
      }
      if (init?.method === "POST" && url.endsWith("/autosave")) {
        const payload = JSON.parse(String(init.body));
        return jsonResponse({
          data: {
            ...page,
            title: payload.title,
            version_id: "v2",
            current_version_id: "v2",
            content_hash: "b".repeat(64),
          },
        });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    // An unedited page in "published" state short-circuits save() entirely
    // (nothing to publish), so a real edit is needed to reach the explicit
    // publish step regardless of publicationState.
    await setEditorTitle(await findTitleEditor(), "Staged edit");
    fireEvent.keyDown(window, { key: "s", ctrlKey: true });
    await waitFor(() => expect(publicationActions().getByRole("button", { name: "Publish" })).toBeEnabled());
    await typist().click(publicationActions().getByRole("button", { name: "Publish" }));

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/publish")),
      ).toBe(true);
    });
    // Must NOT open the conflict banner -- this is not a base conflict.
    expect(
      screen.queryByText(/Someone else updated this page/),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Reload latest" }),
    ).not.toBeInTheDocument();
    await screen.findByText(/no longer the latest one for this page/);
    expect(publicationActions().getByRole("button", { name: "Publish" })).toBeDisabled();
    expect(
      fetchMock.mock.calls.filter((call) =>
        String(call[0]).endsWith("/publish"),
      ),
    ).toHaveLength(1);

    await typist().click(
      screen.getByRole("button", { name: "Reload current draft" }),
    );
    await waitFor(() =>
      expect(
        screen.queryByText(/no longer the latest one for this page/),
      ).not.toBeInTheDocument(),
    );

    // Reloading the eligible draft re-bases the editor. Autosave must not be
    // frozen after that explicit recovery: a further edit still schedules and
    // sends a checkpoint.
    // One autosave already happened staging "Staged edit" above, so a
    // second one landing here (not stuck at 1) is the proof.
    await setEditorTitle(await findTitleEditor(), "Edited after stale publish");
    await advanceAutosaveCheckpoint();
    await waitFor(() => expect(autosaveCalls(fetchMock)).toHaveLength(2));
  });

  it("checkpoints one dirty edit exactly 60 seconds after it begins", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      if (init?.method === "POST" && url.endsWith("/api/v1/workspaces/workspace-1/context/pages/p1/autosave")) {
        return jsonResponse({
          data: {
            ...page,
            title: "Autosaved title",
            version_id: "v2",
            current_version_id: "v2",
            content_hash: "b".repeat(64),
            // PUB-06: an in-place draft autosave never moves `updated_at`
            // (the published/visible instant); the real server response
            // carries the checkpoint time as `draft_saved_at` instead. A
            // mock that puts "now" on `updated_at` here is not something
            // the backend can ever send and would hide this regression.
            draft_saved_at: new Date().toISOString(),
          },
        });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <>
          <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
          <Route path="/pages/:slug" element={<div>reader page</div>} />
      </>,
    );

    const titleEditor = await findTitleEditor();
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-05-20T00:00:00Z"));
    titleEditor.focus();
    fireEvent.change(titleEditor, { target: { value: "Autosaved title" } });
    expect(document.activeElement).toBe(titleEditor);
    expect(publicationActions().getByRole("button", { name: "Save draft" })).toBeInTheDocument();
    expect(fetchMock.mock.calls.some((call) => call[1]?.method === "POST")).toBe(false);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(AUTOSAVE_CHECKPOINT_MS - 1);
    });
    expect(autosaveCalls(fetchMock)).toHaveLength(0);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });

    const autosaveCall = fetchMock.mock.calls.find(
      (call) =>
        call[1]?.method === "POST" &&
        String(call[0]).endsWith("/api/v1/workspaces/workspace-1/context/pages/p1/autosave"),
    );
    expect(JSON.parse(String(autosaveCall?.[1]?.body))).toMatchObject({
      title: "Autosaved title",
      base_version_id: "v1",
      session_version_id: null,
      destination_node_id: "context-inbox",
    });
    expect(screen.queryByText("reader page")).not.toBeInTheDocument();
    // PUB-09: autosaving a published page moves publicationState to
    // "staged" (save()'s edit branch), so the clean-state pill names that
    // instead of the generic "Saved just now".
    expect(syncStatus()).toHaveTextContent("Unpublished draft saved");
    expect(publicationActions().getByRole("button", { name: "Publish" })).toBeInTheDocument();
    expect(document.activeElement).toBe(titleEditor);
    expect(
      fetchMock.mock.calls.some((call) => String(call[0]).endsWith("/publish")),
    ).toBe(false);
  });

  it("shows the meta line's Last saved instant from draft_saved_at, not the frozen updated_at", async () => {
    // PUB-06: an in-place draft autosave never moves page.updated_at (the
    // published/visible instant), so the "Last saved {time}" meta line
    // (editorMeta, unconditional -- unlike the SyncPill text, which PUB-09
    // now keys off publicationState rather than the timestamp) used to keep
    // showing the stale pre-edit time forever after an autosave. This is a
    // more reliable oracle for the fix than the SyncPill text: a page with
    // an outstanding draft always shows the PUB-09 draft-state label there
    // regardless of how fresh the checkpoint is, so it can no longer reveal
    // whether the timestamp wiring is right.
    const publishedUpdatedAt = "2026-05-08T00:00:00Z";
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) return workspaceResponse();
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      if (init?.method === "POST" && url.endsWith("/api/v1/workspaces/workspace-1/context/pages/p1/autosave")) {
        return jsonResponse({
          data: {
            ...page,
            title: "Aging checkpoint",
            version_id: "v2",
            current_version_id: "v2",
            content_hash: "b".repeat(64),
            updated_at: publishedUpdatedAt,
            // L-2 (260908 UI audit): the meta line goes blank for the first
            // minute after a save (the sync pill says "Saved just now" for
            // that window instead), so this is deliberately far enough in
            // the past that the checkpoint is already outside that window by
            // the time the autosave response lands -- otherwise this test
            // could not tell "shows draft_saved_at" apart from "shows
            // nothing yet".
            draft_saved_at: "2026-05-19T00:00:00.000Z",
          },
        });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    const titleEditor = await findTitleEditor();
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-05-20T00:00:00Z"));
    expect(document.querySelector(".meta-line")).toHaveTextContent("2026-05-08");

    titleEditor.focus();
    fireEvent.change(titleEditor, { target: { value: "Aging checkpoint" } });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(AUTOSAVE_CHECKPOINT_MS);
    });
    expect(autosaveCalls(fetchMock)).toHaveLength(1);

    const metaLine = document.querySelector(".meta-line");
    expect(metaLine).toHaveTextContent("2026-05-19");
    expect(metaLine).not.toHaveTextContent("2026-05-08");
  });

  it("blanks the meta line while the sync pill would say 'Saved just now' (L-2)", () => {
    // Unit-level: reaching "Saved just now" through the full component
    // requires a page with no outstanding draft (publicationState
    // "published"), which PUB-09 shows an ordinary autosave never produces
    // (autosaving a published page always moves it to "staged", where the
    // pill names that state instead). editorMeta's own contract -- blank
    // while savedJustNow, timestamped once that window closes -- is what
    // L-2 actually changed, so test it directly against the same page shape
    // and translator PageEditor renders with.
    const savedPage = {
      ...page,
      is_public: false,
      workspace_id: "workspace-1",
      editing_revision: 1,
      draft_saved_at: "2026-05-20T00:01:00.000Z",
    };
    const t = i18n.getFixedT("en", "pages");
    expect(editorMeta(savedPage, false, t, true)).toBe("");
    expect(editorMeta(savedPage, false, t, false)).toContain("Last saved");
  });

  it("acknowledges a trailing-space title once without losing title focus or resaving", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      if (init?.method === "POST" && url.endsWith("/api/v1/workspaces/workspace-1/context/pages/p1/autosave")) {
        return jsonResponse({
          data: {
            ...page,
            title: "Trailing space",
            version_id: "v2",
            current_version_id: "v2",
            content_hash: "b".repeat(64),
          },
        });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    const titleEditor = await findTitleEditor();
    await setEditorTitle(titleEditor, "Trailing space ");
    expect(document.activeElement).toBe(titleEditor);

    await advanceAutosaveCheckpoint();
    await waitFor(() => {
      expect(autosaveCalls(fetchMock)).toHaveLength(1);
    });
    expect(JSON.parse(String(autosaveCalls(fetchMock)[0][1]?.body))).toMatchObject({
      title: "Trailing space",
    });
    expect(titleEditor).toHaveValue("Trailing space ");
    expect(document.activeElement).toBe(titleEditor);
    expect(screen.queryByText(/newer edits are not yet staged/i)).not.toBeInTheDocument();
    fireEvent.blur(titleEditor);
    expect(
      await screen.findByText("Leading and trailing spaces are removed when saving."),
    ).toBeInTheDocument();

    await advanceAutosaveCheckpoint();
    expect(autosaveCalls(fetchMock)).toHaveLength(1);
    expect(screen.queryByText(/newer edits are not yet staged/i)).not.toBeInTheDocument();
    await waitFor(() => {
      expect(document.querySelector(".page-workspace")).toHaveAttribute(
        "data-save-state",
        "clean",
      );
    });
  });

  it("accepts new tags typed while an autosave is still in flight", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let resolveTagAutosave!: (response: Response) => void;
    let autosavedTags: string[] = [];
    const pendingTagAutosave = new Promise<Response>((resolve) => {
      resolveTagAutosave = resolve;
    });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages/tags?")) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      if (init?.method === "POST" && url.endsWith("/api/v1/workspaces/workspace-1/context/pages/p1/autosave")) {
        const payload = JSON.parse(String(init.body));
        autosavedTags = payload.tags;
        return pendingTagAutosave;
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    await expectEditorTitle("Hello");
    const tagsInput = screen.getByLabelText("Tags");
    await typist().type(tagsInput, "Focus tag{Enter}");

    // Asserting focus AFTER the request settles proves nothing: user-event
    // simply drops keystrokes aimed at a disabled input, and jsdom does not
    // blur on disable the way a browser does. The lockout is only observable
    // by typing WHILE the request is open and checking the text arrived.
    await advanceAutosaveCheckpoint();
    await waitFor(() => {
      expect(autosaveCalls(fetchMock)).toHaveLength(1);
    });
    await typist().type(tagsInput, "Second tag{Enter}");
    expect(document.activeElement).toBe(tagsInput);
    expect(await screen.findByText("Second tag")).toBeInTheDocument();

    expect(autosavedTags).toEqual(["Focus tag"]);
    resolveTagAutosave(
      jsonResponse({
        data: {
          ...page,
          tags: autosavedTags,
          version_id: "v2",
          current_version_id: "v2",
          content_hash: "c".repeat(64),
        },
      }),
    );
    await waitFor(() => {
      expect(screen.getByText("Second tag")).toBeInTheDocument();
    });
  });

  it("keeps newer typing focused and banner-free when an in-flight save completes", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let resolveAutosave!: (response: Response) => void;
    const pendingAutosave = new Promise<Response>((resolve) => {
      resolveAutosave = resolve;
    });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      if (init?.method === "POST" && url.endsWith("/api/v1/workspaces/workspace-1/context/pages/p1/autosave")) {
        return pendingAutosave;
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    const titleEditor = await findTitleEditor();
    await setEditorTitle(titleEditor, "First title");
    fireEvent.keyDown(window, { key: "s", ctrlKey: true });
    await waitFor(() => {
      expect(autosaveCalls(fetchMock)).toHaveLength(1);
    });

    await typist().type(titleEditor, " plus newer typing");
    expect(titleEditor).toHaveValue("First title plus newer typing");
    expect(titleEditor).toHaveFocus();
    expect(titleEditor.closest(".page-workspace")).toHaveAttribute(
      "data-save-state",
      "saving_with_newer_edits",
    );

    resolveAutosave(jsonResponse({
      data: {
        ...page,
        title: "Server canonical title",
        version_id: "v2",
        current_version_id: "v2",
        content_hash: "d".repeat(64),
      },
    }));

    await waitFor(() => {
      expect(titleEditor.closest(".page-workspace")).toHaveAttribute(
        "data-save-state",
        "dirty",
      );
    });
    expect(titleEditor).toHaveValue("First title plus newer typing");
    expect(titleEditor).toHaveFocus();
    expect(screen.queryByText(/newer edits are not yet staged/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Retry/ })).not.toBeInTheDocument();
  });

  it("does not postpone the first checkpoint while typing continuously", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      if (init?.method === "POST" && url.endsWith("/api/v1/workspaces/workspace-1/context/pages/p1/autosave")) {
        return jsonResponse({
          data: {
            ...page,
            title: "Burst title 3",
            version_id: "v2",
            current_version_id: "v2",
            content_hash: "c".repeat(64),
            updated_at: new Date().toISOString(),
          },
        });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <>
        <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    const titleEditor = await findTitleEditor();
    // The assertions below are exact to the millisecond, so the clock may only
    // move when this test moves it. `shouldAdvanceTime` would also add a tick
    // for every ~20ms of real time the host spends rendering and polling, and
    // user-event/waitFor would then be waiting on those ticks. Edits are single
    // change events under act and every advance goes to an absolute instant
    // measured from the first edit, never by an amount that assumes how much
    // time the steps in between took.
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-05-20T00:00:00Z"));
    const editTitle = (value: string) => {
      fireEvent.change(titleEditor, { target: { value } });
    };

    editTitle("Burst title 1");
    const firstEditAt = Date.now();
    await advanceFakeClockTo(firstEditAt + 400);
    editTitle("Burst title 2");
    await advanceFakeClockTo(firstEditAt + 800);
    editTitle("Burst title 3");
    // The checkpoint remains anchored to the first edit rather than sliding
    // forward after either later edit. (A title edited through clear-and-type
    // would not show that: emptying the title blocks saving, which cancels the
    // armed timer, so every such edit would legitimately start a new one.)
    expect(autosaveCalls(fetchMock)).toHaveLength(0);

    await advanceFakeClockTo(firstEditAt + AUTOSAVE_CHECKPOINT_MS - 1);
    expect(autosaveCalls(fetchMock)).toHaveLength(0);
    await advanceFakeClockTo(firstEditAt + AUTOSAVE_CHECKPOINT_MS);

    expect(autosaveCalls(fetchMock)).toHaveLength(1);
    expect(JSON.parse(String(autosaveCalls(fetchMock)[0][1]?.body))).toMatchObject({
      title: "Burst title 3",
    });
  });

  it("coalesces a checkpoint that expires during a save into one follow-up", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date("2026-05-20T00:00:00Z"));
    let resolveFirst!: (response: Response) => void;
    const firstAutosave = new Promise<Response>((resolve) => {
      resolveFirst = resolve;
    });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) return workspaceResponse();
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      if (init?.method === "POST" && url.endsWith("/autosave")) {
        if (autosaveCalls(fetchMock).length === 1) return firstAutosave;
        const payload = JSON.parse(String(init.body));
        return jsonResponse({
          data: {
            ...page,
            title: payload.title,
            version_id: "v3",
            current_version_id: "v3",
            content_hash: "c".repeat(64),
            updated_at: new Date().toISOString(),
          },
        });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    const titleEditor = await findTitleEditor();
    await setEditorTitle(titleEditor, "First checkpoint");
    fireEvent.keyDown(window, { key: "s", ctrlKey: true });
    await waitFor(() => expect(autosaveCalls(fetchMock)).toHaveLength(1));
    expect(syncStatus()).toHaveTextContent("Saving…");

    await setEditorTitle(titleEditor, "Second checkpoint");
    expect(syncStatus()).toHaveTextContent(
      "Saving · newer changes pending",
    );
    await advanceAutosaveCheckpoint();
    // PUB-08 moved pagehide off save() and onto the dismissal backstop
    // (which now deliberately fires even with a save in flight), so it is
    // no longer a no-op here; that interaction is covered separately, not
    // as an incidental check bolted onto this coalescing test.
    expect(autosaveCalls(fetchMock)).toHaveLength(1);

    resolveFirst(jsonResponse({
      data: {
        ...page,
        title: "First checkpoint",
        version_id: "v2",
        current_version_id: "v2",
        content_hash: "b".repeat(64),
      },
    }));
    await waitFor(() => expect(autosaveCalls(fetchMock)).toHaveLength(2));
    expect(JSON.parse(String(autosaveCalls(fetchMock)[1][1]?.body))).toMatchObject({
      title: "Second checkpoint",
      base_version_id: "v2",
    });
    await waitFor(() => {
      // PUB-09: this fixture starts "published", so autosaving it moves
      // publicationState to "staged", and the clean-state pill names that
      // instead of "Saved just now".
      expect(syncStatus()).toHaveTextContent("Unpublished draft saved");
    });
    expect(document.querySelector(".status-banner")).not.toBeInTheDocument();
  });

  it.each([
    ["Ctrl+S", () => fireEvent.keyDown(window, { key: "s", ctrlKey: true })],
    ["visibilitychange", () => {
      vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
      fireEvent(document, new Event("visibilitychange"));
    }],
    ["pagehide", () => fireEvent(window, new Event("pagehide"))],
  ])("flushes dirty content immediately on %s", async (_label, trigger) => {
    const fetchMock = mockPublicationFlow();
    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );
    await setEditorTitle(await findTitleEditor(), `Flush ${_label}`);
    trigger();
    await waitFor(() => expect(autosaveCalls(fetchMock)).toHaveLength(1));
  });

  it("sends exactly one dismissal autosave when beforeunload and pagehide both fire", async () => {
    // PUB-08: a browser fires beforeunload then pagehide for the same
    // dismissal. Both used to independently trigger a flush -- beforeunload
    // via the backstop, pagehide via save() -- and the backstop never
    // updates lastSavedFingerprintRef, so save()'s own contentUnchanged
    // shortcut cannot dedupe the second one either. Stub sendBeacon so both
    // calls route through it (not a mix of sendBeacon/keepalive fetch) and
    // sum sendBeacon + fetch to count transmissions regardless of which path
    // carried them.
    const fetchMock = mockPublicationFlow();
    const sendBeacon = vi.fn<(url: string, data?: BodyInit | null) => boolean>(() => true);
    Object.defineProperty(window.navigator, "sendBeacon", {
      configurable: true,
      value: sendBeacon,
    });
    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );
    await setEditorTitle(await findTitleEditor(), "Dismissal fingerprint");

    fireEvent(window, new Event("beforeunload", { cancelable: true }));
    fireEvent(window, new Event("pagehide"));

    await waitFor(() => {
      expect(sendBeacon.mock.calls.length + autosaveCalls(fetchMock).length).toBe(1);
    });
  });

  it("still flushes a later dismissal after an earlier one was cancelled and more was typed", async () => {
    // Latch regression: a boolean "already flushed this session" latch would
    // permanently suppress every dismissal after the first, including a
    // genuine one following a cancelled "stay on page" -- remembering the
    // shipped fingerprint (not a boolean) is what lets new bytes flush
    // again.
    const fetchMock = mockPublicationFlow();
    const sendBeacon = vi.fn<(url: string, data?: BodyInit | null) => boolean>(() => true);
    Object.defineProperty(window.navigator, "sendBeacon", {
      configurable: true,
      value: sendBeacon,
    });
    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );
    const titleEditor = await findTitleEditor();
    await setEditorTitle(titleEditor, "First dismissal");

    fireEvent(window, new Event("beforeunload", { cancelable: true }));
    await waitFor(() => {
      expect(sendBeacon.mock.calls.length + autosaveCalls(fetchMock).length).toBe(1);
    });

    // The user stayed (dismissal cancelled) and kept editing.
    await setEditorTitle(titleEditor, "Second dismissal after staying");
    fireEvent(window, new Event("pagehide"));

    await waitFor(() => {
      expect(sendBeacon.mock.calls.length + autosaveCalls(fetchMock).length).toBe(2);
    });
  });

  // PUB-01: a create-mode autosave checkpoint must stage a draft, never
  // publish it. Ctrl+S/closeEditor/useBlocker/link-click all synchronously
  // navigate or unmount first, which makes the create branch bail on
  // isEditorMountedRef before it would ever reach publishPageVersion -- so
  // asserting against any of those leaves this regression unpinned. Only
  // visibilitychange -> hidden leaves the editor mounted while the checkpoint
  // runs createPage -> (guarded) publishPageVersion to completion, so it is
  // the only oracle that can tell "0 because guarded" apart from "0 because
  // unmounted."
  it("does not publish a brand-new page from a visibilitychange autosave checkpoint", async () => {
    const created = {
      ...page,
      id: "p-new",
      slug: "new-page",
      title: "New page",
      version_id: "v1",
      current_version_id: "v1",
      parent_version_id: null,
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) return workspaceResponse();
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [], next_cursor: null });
      }
      if (init?.method === "POST" && url === `${BASE}/api/v1/workspaces/workspace-1/context/pages`) {
        return jsonResponse({ data: created }, 201);
      }
      if (init?.method === "POST" && url.includes("/publish")) {
        return jsonResponse({
          data: { ...created, version_id: "v2", current_version_id: "v2" },
        });
      }
      return jsonResponse({ data: created });
    });

    renderWithRouter(
      "/pages/new",
      <Route path="/pages/new" element={<PageEditor mode="create" />} />,
    );

    const titleInput = await findTitleEditor();
    await setEditorTitle(titleInput, "New page");
    replaceEditorSelection(codeMirrorView(), "Body written before switching tabs.");

    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    fireEvent(document, new Event("visibilitychange"));

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.filter(
          (call) => call[1]?.method === "POST" && String(call[0]) === `${BASE}/api/v1/workspaces/workspace-1/context/pages`,
        ),
      ).toHaveLength(1);
    });
    expect(
      fetchMock.mock.calls.some((call) => String(call[0]).includes("/publish")),
    ).toBe(false);
    // The editor must still be mounted -- otherwise a 0-publish count would
    // just mean the mount-guard at isEditorMountedRef caught it, not the
    // source === "auto" guard this test exists to pin.
    expect(screen.getByTestId("page-title-editor")).toBeInTheDocument();
  });

  it("queues an autosave backstop before leaving after a failed SPA flush", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) return workspaceResponse();
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      if (init?.method === "POST" && url.endsWith("/autosave")) {
        return jsonResponse(
          {
            error_code: "internal_error",
            message: "save failed",
            request_id: "req-nav",
            warnings: [],
          },
          500,
        );
      }
      return jsonResponse({ data: page });
    });
    const sendBeacon = vi.fn<(url: string, data?: BodyInit | null) => boolean>(() => true);
    Object.defineProperty(window.navigator, "sendBeacon", {
      configurable: true,
      value: sendBeacon,
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderWithRouter(
      "/pages/hello/edit",
      <>
        <Route
          path="/pages/:slug/edit"
          element={(
            <>
              <PageEditor mode="edit" />
              <ProgrammaticNavigationButton />
            </>
          )}
        />
        <Route path="/dashboard" element={<div>dashboard route</div>} />
      </>,
    );
    await setEditorTitle(await findTitleEditor(), "Leaving now");
    fireEvent.click(screen.getByRole("button", { name: "Programmatic nav" }));
    await waitFor(() => expect(autosaveCalls(fetchMock)).toHaveLength(1));
    expect(sendBeacon).toHaveBeenCalledTimes(1);
    expect(sendBeacon.mock.calls[0][0]).toBe(`${BASE}/api/v1/workspaces/workspace-1/context/pages/p1/autosave`);
    expect(JSON.parse(await readBlobText(sendBeacon.mock.calls[0][1] as Blob))).toMatchObject({
      title: "Leaving now",
      base_version_id: "v1",
      base_content_hash: "a".repeat(64),
    });
    expect(await screen.findByText("dashboard route")).toBeInTheDocument();
  });

  it("does not request a checkpoint when the outbound fingerprint is unchanged", async () => {
    const fetchMock = mockPublicationFlow();
    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );
    const titleEditor = await findTitleEditor();
    await setEditorTitle(titleEditor, "Changed then restored");
    await setEditorTitle(titleEditor, "Hello");
    fireEvent.keyDown(window, { key: "s", ctrlKey: true });
    fireEvent(window, new Event("pagehide"));
    await waitFor(() => expect(writeCalls(fetchMock)).toHaveLength(0));
  });

  it("defers a checkpoint until IME composition ends", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = mockPublicationFlow();
    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );
    const titleEditor = await findTitleEditor();
    fireEvent.compositionStart(titleEditor);
    fireEvent.change(titleEditor, { target: { value: "작성 중" } });
    await advanceAutosaveCheckpoint();
    expect(autosaveCalls(fetchMock)).toHaveLength(0);
    expect(titleEditor).toHaveValue("작성 중");
    fireEvent.compositionEnd(titleEditor);
    await waitFor(() => expect(autosaveCalls(fetchMock)).toHaveLength(1));
  });

  it("clears a queued composition autosave when compositionend dispatches publish", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let resolveFirstAutosave!: (response: Response) => void;
    const firstAutosave = new Promise<Response>((resolve) => {
      resolveFirstAutosave = resolve;
    });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) return workspaceResponse();
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      if (init?.method === "POST" && url.endsWith("/autosave")) {
        if (autosaveCalls(fetchMock).length === 1) return firstAutosave;
        const payload = JSON.parse(String(init.body));
        return jsonResponse({
          data: {
            ...page,
            title: payload.title,
            version_id: "v3",
            current_version_id: "v3",
            content_hash: "c".repeat(64),
          },
        });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    const titleEditor = await findTitleEditor();
    fireEvent.compositionStart(titleEditor);
    fireEvent.change(titleEditor, { target: { value: "Composed title" } });
    fireEvent(window, new Event("pagehide"));
    fireEvent.keyDown(window, { key: "s", ctrlKey: true });
    expect(autosaveCalls(fetchMock)).toHaveLength(0);

    fireEvent.compositionEnd(titleEditor);
    await waitFor(() => expect(autosaveCalls(fetchMock)).toHaveLength(1));
    await setEditorTitle(titleEditor, "Newer title");
    await act(async () => {
      resolveFirstAutosave(jsonResponse({
        data: {
          ...page,
          title: "Composed title",
          version_id: "v2",
          current_version_id: "v2",
          content_hash: "b".repeat(64),
        },
      }));
      await Promise.resolve();
    });
    await waitFor(() => {
      expect(document.querySelector(".page-workspace")).toHaveAttribute(
        "data-save-state",
        "dirty",
      );
    });
    expect(autosaveCalls(fetchMock)).toHaveLength(1);

    await advanceAutosaveCheckpoint();
    await waitFor(() => expect(autosaveCalls(fetchMock)).toHaveLength(2));
  });

  it("sends a best-effort autosave beacon before unloading a dirty saved page", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      return jsonResponse({ data: page });
    });
    const sendBeacon = vi.fn<(url: string, data?: BodyInit | null) => boolean>(() => true);
    Object.defineProperty(window.navigator, "sendBeacon", {
      configurable: true,
      value: sendBeacon,
    });

    renderWithRouter(
      "/pages/hello/edit",
      <>
        <Route
          path="/pages/:slug/edit"
          element={(
            <>
              <PageEditor mode="edit" />
              <ProgrammaticNavigationButton />
            </>
          )}
        />
        <Route path="/dashboard" element={<div>dashboard</div>} />
      </>,
    );

    await setEditorTitle(await findTitleEditor(), "Beacon title");
    const unloadEvent = new Event("beforeunload", { cancelable: true });
    expect(window.dispatchEvent(unloadEvent)).toBe(false);

    expect(sendBeacon).toHaveBeenCalledTimes(1);
    const [url, data] = sendBeacon.mock.calls[0];
    expect(url).toBe(`${BASE}/api/v1/workspaces/workspace-1/context/pages/p1/autosave`);
    const payload = JSON.parse(await readBlobText(data as Blob));
    expect(payload).toMatchObject({
      title: "Beacon title",
      base_version_id: "v1",
      base_content_hash: "a".repeat(64),
    });
    expect(payload.body_md).toContain('title: "Beacon title"');
  });

  it("still sends a dismissal backstop through beforeunload with a save already in flight in the same turn", async () => {
    // PUB-08 (a): the backstop now passes { allowInFlight: true } for
    // dismissals, matching closeEditor/useBlocker's policy -- an in-flight
    // save is going to be aborted by the unload anyway, so beforeunload must
    // still ship a backstop rather than silently trusting the in-flight
    // request to land. Before this fix the backstop refused to fire while
    // inFlightRevisionRef was set, so this exact sequence shipped nothing if
    // the in-flight save lost the unload race.
    let resolveAutosave!: (response: Response) => void;
    const pendingAutosave = new Promise<Response>((resolve) => {
      resolveAutosave = resolve;
    });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) return workspaceResponse();
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      if (init?.method === "POST" && url.endsWith("/autosave")) {
        return pendingAutosave;
      }
      return jsonResponse({ data: page });
    });
    const sendBeacon = vi.fn<(url: string, data?: BodyInit | null) => boolean>(() => true);
    Object.defineProperty(window.navigator, "sendBeacon", {
      configurable: true,
      value: sendBeacon,
    });

    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    await setEditorTitle(await findTitleEditor(), "One in-flight save");
    let unloadAccepted = true;
    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "s", ctrlKey: true }));
      unloadAccepted = window.dispatchEvent(new Event("beforeunload", { cancelable: true }));
    });
    await waitFor(() => expect(autosaveCalls(fetchMock)).toHaveLength(1));
    expect(unloadAccepted).toBe(false);
    expect(sendBeacon).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveAutosave(jsonResponse({
        data: {
          ...page,
          title: "One in-flight save",
          version_id: "v2",
          current_version_id: "v2",
          content_hash: "b".repeat(64),
        },
      }));
      await Promise.resolve();
    });
  });

  it("does not send an unload autosave after a save error", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      if (init?.method === "POST" && url.endsWith("/autosave")) {
        return jsonResponse(
          {
            error_code: "internal_error",
            message: "save failed",
            request_id: "req-1",
            warnings: [],
          },
          500,
        );
      }
      return jsonResponse({ data: page });
    });
    const sendBeacon = vi.fn<(url: string, data?: BodyInit | null) => boolean>(() => true);
    Object.defineProperty(window.navigator, "sendBeacon", {
      configurable: true,
      value: sendBeacon,
    });

    renderWithRouter(
      "/pages/hello/edit",
      <>
          <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    await setEditorTitle(await findTitleEditor(), "Broken save title");
    await typist().click(publicationActions().getByRole("button", { name: "Save draft" }));
    await screen.findByRole("button", { name: /Couldn't save/ });
    expect(syncStatus()).toHaveTextContent("Couldn't save or publish · Retry");
    expect(document.querySelectorAll(".status-banner")).toHaveLength(1);

    const unloadEvent = new Event("beforeunload", { cancelable: true });
    expect(window.dispatchEvent(unloadEvent)).toBe(false);
    expect(sendBeacon).not.toHaveBeenCalled();
  });

  it("does not autosave new pages before an explicit create", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [], next_cursor: null });
      }
      if (init?.method === "POST") {
        return jsonResponse({ data: { ...page, title: "Draft", slug: "draft" } }, 201);
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/new",
      <>
          <Route path="/pages/new" element={<PageEditor mode="create" />} />
      </>,
    );

    await setEditorTitle(await findTitleEditor(), "Draft");
    await advanceAutosaveCheckpoint();

    expect(fetchMock.mock.calls.filter((call) => call[1]?.method === "POST")).toHaveLength(0);
    expect(publicationActions().getByRole("button", { name: "Save draft" })).toBeInTheDocument();
  });

  it("disables Save draft without a title and names the blocker on click and Ctrl+S", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages/tags?")) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [], next_cursor: null });
      }
      if (init?.method === "POST") {
        return jsonResponse({ data: page }, 201);
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/new",
      <>
          <Route path="/pages/new" element={<PageEditor mode="create" />} />
      </>,
    );

    const titleInput = await findTitleEditor();
    replaceEditorSelection(codeMirrorView(), "Body without a title");

    const saveButton = await publicationActions().findByRole("button", { name: "Save draft" });
    expect(saveButton).toHaveAttribute("aria-disabled", "true");
    expect(saveButton).toHaveAttribute("title", "Add a title before saving.");

    fireEvent.click(saveButton);

    expect(await screen.findByText("Add a title before saving.")).toBeInTheDocument();
    expect(titleInput).toHaveFocus();
    expect(fetchMock.mock.calls.some((call) => call[1]?.method === "POST")).toBe(false);

    fireEvent.keyDown(window, { key: "s", ctrlKey: true });
    expect(fetchMock.mock.calls.some((call) => call[1]?.method === "POST")).toBe(false);

    await setEditorTitle(titleInput, "Now titled");
    await waitFor(() => {
      expect(publicationActions().getByRole("button", { name: "Save draft" })).not.toHaveAttribute(
        "aria-disabled",
      );
    });
    expect(publicationActions().getByRole("button", { name: "Save draft" })).not.toHaveAttribute("title");
  });

  it("preserves edits made while a new page create is in flight", async () => {
    let resolveCreate!: (response: Response) => void;
    let resolveFollowUp!: (response: Response) => void;
    const createPromise = new Promise<Response>((resolve) => {
      resolveCreate = resolve;
    });
    const followUpAutosave = new Promise<Response>((resolve) => {
      resolveFollowUp = resolve;
    });
    // What the server holds once the last autosave lands: a staged draft of a
    // page that was never published, read by the editor that loads the page by
    // slug after the author is moved to its edit URL.
    let savedDraft: typeof page = page;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [], next_cursor: null });
      }
      if (init?.method === "POST" && url === `${BASE}/api/v1/workspaces/workspace-1/context/pages`) {
        return createPromise;
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages/by-slug/first-title")) {
        return jsonResponse({
          data: {
            ...savedDraft,
            version_status: "draft",
            current_published_version_id: null,
          },
        });
      }
      if (init?.method === "POST" && url.endsWith("/api/v1/workspaces/workspace-1/context/pages/p1/autosave")) {
        const autosaveCount = fetchMock.mock.calls.filter(
          (call) => String(call[0]).endsWith("/autosave"),
        ).length;
        if (autosaveCount === 1) {
          return followUpAutosave;
        }
        const body = JSON.parse(String(init.body));
        savedDraft = {
          ...page,
          slug: "first-title",
          title: body.title,
          body_md: body.body_md,
          version_id: body.session_version_id,
          current_version_id: body.session_version_id,
          content_hash: "c".repeat(64),
        };
        return jsonResponse({ data: savedDraft });
      }
      return jsonResponse({ data: page });
    });

    const router = renderWithRouter(
      "/pages/new",
      <>
        <Route path="/pages/new" element={<PageEditor mode="create" />} />
        <Route path="/pages/:slug/edit" element={<div>editing created page</div>} />
        <Route path="/context/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    await setEditorTitle(await findTitleEditor(), "First title");
    fireEvent.keyDown(window, { key: "s", ctrlKey: true });
    await waitFor(() => {
      expect(fetchMock.mock.calls.filter((call) => call[1]?.method === "POST")).toHaveLength(1);
    });

    await setEditorTitle(getTitleEditor(), "Second title");
    await openPageProperties();
    await typist().click(pageProperties().getByRole("button", { name: "Advanced settings" }));
    expect(screen.getByLabelText("Slug")).toHaveValue("first-title");
    resolveCreate(jsonResponse({
      data: {
        ...page,
        slug: "first-title",
        title: "First title",
        body_md: "",
      },
    }, 201));

    await waitFor(() => {
      const autosaveCall = fetchMock.mock.calls.find((call) =>
        String(call[0]).endsWith("/autosave"),
      );
      expect(JSON.parse(String(autosaveCall?.[1]?.body))).toMatchObject({
        title: "Second title",
        base_version_id: "v1",
        base_content_hash: "a".repeat(64),
        session_version_id: "v1",
      });
    });
    const liveTitleEditor = getTitleEditor();
    await setEditorTitle(liveTitleEditor, "Third title");
    expect(liveTitleEditor).toHaveFocus();
    expect(liveTitleEditor.closest(".page-workspace")).toHaveAttribute(
      "data-save-state",
      "saving_with_newer_edits",
    );
    expect(screen.queryByText(/newer edits are not yet staged/i)).not.toBeInTheDocument();
    resolveFollowUp(jsonResponse({
      data: {
        ...page,
        slug: "first-title",
        title: "Second title",
        body_md: "",
        version_id: "v2",
        current_version_id: "v2",
        content_hash: "b".repeat(64),
      },
    }));
    await waitFor(() => {
      expect(liveTitleEditor.closest(".page-workspace")).toHaveAttribute(
        "data-save-state",
        "dirty",
      );
    });
    expect(screen.queryByText(/newer edits are not yet staged/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Retry/ })).not.toBeInTheDocument();
    expect(screen.queryByText("editing created page")).not.toBeInTheDocument();
    expect(getTitleEditor()).toHaveValue("Third title");
    expect(getTitleEditor()).toHaveFocus();

    // This save settles with no newer edits behind it, so it also moves the
    // author from /pages/new to the page's edit URL and remounts the editor
    // there. Publish from the editor that takes over: a Publish button taken
    // from the one being replaced would be clicked after it has been removed.
    await typist().click(publicationActions().getByRole("button", { name: "Save draft" }));
    await waitFor(() => {
      expect(liveTitleEditor).not.toBeInTheDocument();
    });
    await expectEditorTitle("Third title");
    await waitFor(() =>
      expect(publicationActions().getByRole("button", { name: "Publish" })).toBeEnabled(),
    );
    await typist().click(publicationActions().getByRole("button", { name: "Publish" }));
    await waitFor(() => {
      expect(writeCalls(fetchMock)).toHaveLength(4);
    });
    const finalWrite = writeCalls(fetchMock)[2];
    expect(finalWrite?.[1]?.method).toBe("POST");
    expect(JSON.parse(String(finalWrite?.[1]?.body))).toMatchObject({
      title: "Third title",
      base_version_id: "v2",
      base_content_hash: "b".repeat(64),
      session_version_id: "v2",
    });
    await waitFor(() => {
      expect(router.state.location).toMatchObject({
        pathname: "/context",
        search: "?target=p1",
      });
    });
    expect(await screen.findByTestId("published-context-destination")).toHaveTextContent(
      "/context?target=p1",
    );
  });

  it("does not expose a raw generic save exception", async () => {
    const rawSecret = "raw-save-secret=postgres://private-host";
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      if (init?.method === "POST" && url.endsWith("/autosave")) {
        throw new Error(rawSecret);
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    await setEditorTitle(await findTitleEditor(), "Save without leakage");
    await typist().click(publicationActions().getByRole("button", { name: "Save draft" }));

    expect(
      await screen.findByText("Could not save changes. Try again."),
    ).toBeInTheDocument();
    expect(screen.queryByText(rawSecret)).toBeNull();
  });

  it("retries with autosave when a post-create follow-up save fails", async () => {
    let resolveCreate!: (response: Response) => void;
    const createPromise = new Promise<Response>((resolve) => {
      resolveCreate = resolve;
    });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [], next_cursor: null });
      }
      if (init?.method === "POST" && url === `${BASE}/api/v1/workspaces/workspace-1/context/pages`) {
        return createPromise;
      }
      if (init?.method === "POST" && url.endsWith("/autosave")) {
        const autosaveCount = fetchMock.mock.calls.filter(
          (call) => String(call[0]).endsWith("/autosave"),
        ).length;
        if (autosaveCount === 1) {
          return jsonResponse({
            error_code: "save_failed",
            message: "follow-up failed",
            request_id: "req-1",
            warnings: [],
          }, 500);
        }
        const body = JSON.parse(String(init.body));
        return jsonResponse({
          data: {
            ...page,
            slug: "first-title",
            title: body.title,
            body_md: body.body_md,
            version_id: "v2",
            current_version_id: "v2",
            content_hash: "b".repeat(64),
          },
        });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/new",
      <>
          <Route path="/pages/new" element={<PageEditor mode="create" />} />
          <Route path="/pages/:slug/edit" element={<div>editing created page</div>} />
          <Route path="/context/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    await setEditorTitle(await findTitleEditor(), "First title");
    fireEvent.keyDown(window, { key: "s", ctrlKey: true });
    await waitFor(() => {
      expect(fetchMock.mock.calls.filter((call) => call[1]?.method === "POST")).toHaveLength(1);
    });

    await setEditorTitle(getTitleEditor(), "Second title");
    resolveCreate(jsonResponse({
      data: {
        ...page,
        slug: "first-title",
        title: "First title",
        body_md: "",
      },
    }, 201));

    expect(await screen.findByText("Could not save changes. Try again.")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.filter((call) => call[0] === `${BASE}/api/v1/workspaces/workspace-1/context/pages`),
    ).toHaveLength(1);
    expect(
      fetchMock.mock.calls.filter((call) => String(call[0]).endsWith("/autosave")),
    ).toHaveLength(1);

    await typist().click(screen.getByRole("button", { name: /Retry/ }));
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.filter((call) => String(call[0]).endsWith("/autosave")),
      ).toHaveLength(2);
    });
    const retryAutosave = fetchMock.mock.calls.filter((call) =>
      String(call[0]).endsWith("/autosave"),
    )[1];
    expect(JSON.parse(String(retryAutosave?.[1]?.body))).toMatchObject({
      title: "Second title",
      base_version_id: "v1",
      base_content_hash: "a".repeat(64),
      session_version_id: "v1",
    });
    expect(await screen.findByLabelText("Context tree")).toBeInTheDocument();
  });

  it("serializes overlapping saves and still publishes the second request", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date("2026-05-20T00:00:00Z"));
    let resolveFirst!: (response: Response) => void;
    const firstAutosave = new Promise<Response>((resolve) => {
      resolveFirst = resolve;
    });
    const secondContentHash = "c".repeat(64);
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      if (init?.method === "POST" && url.endsWith("/api/v1/workspaces/workspace-1/context/pages/p1/autosave")) {
        const autosaveCount = fetchMock.mock.calls.filter(
          (call) => String(call[0]).endsWith("/autosave"),
        ).length;
        if (autosaveCount === 1) {
          return firstAutosave;
        }
        const body = JSON.parse(String(init.body));
        return jsonResponse({
          data: {
            ...page,
            title: body.title,
            body_md: body.body_md,
            version_id: body.session_version_id,
            current_version_id: body.session_version_id,
            content_hash: secondContentHash,
            updated_at: new Date().toISOString(),
          },
        });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <>
          <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    await setEditorTitle(await findTitleEditor(), "First title");
    fireEvent.keyDown(window, { key: "s", ctrlKey: true });
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.filter((call) => String(call[0]).endsWith("/autosave")),
      ).toHaveLength(1);
    });

    await setEditorTitle(getTitleEditor(), "Second title");
    fireEvent.keyDown(window, { key: "s", ctrlKey: true });

    expect(
      fetchMock.mock.calls.filter((call) => String(call[0]).endsWith("/autosave")),
    ).toHaveLength(1);
    resolveFirst(jsonResponse({
      data: {
        ...page,
        title: "First title",
        version_id: "v2",
        current_version_id: "v2",
        content_hash: "b".repeat(64),
        updated_at: new Date().toISOString(),
      },
    }));
    // The second Ctrl+S arrived while the first save was in flight. It is
    // remembered and stages the newer buffer after the first settles.
    const autosaveBodies = () =>
      fetchMock.mock.calls
        .filter((call) => String(call[0]).endsWith("/autosave"))
        .map((call) => JSON.parse(String(call[1]?.body)));
    await waitFor(() => {
      expect(autosaveBodies().some((body) => body.title === "Second title")).toBe(
        true,
      );
    });
    const staged = autosaveBodies().find((body) => body.title === "Second title");
    expect(staged).toMatchObject({
      base_version_id: "v2",
      base_content_hash: "b".repeat(64),
      session_version_id: "v2",
    });
    expect(fetchMock.mock.calls.filter((call) => String(call[0]).endsWith("/publish"))).toHaveLength(0);
  });

  it("guards close navigation when the editor has unsaved changes", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [], next_cursor: null });
      }
      return jsonResponse({ data: page });
    });
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);

    renderWithRouter(
      "/pages/new",
      <>
          <Route path="/pages/new" element={<PageEditor mode="create" />} />
          <Route path="/pages" element={<div>pages landing</div>} />
      </>,
    );

    await setEditorTitle(await findTitleEditor(), "Draft");
    const unloadEvent = new Event("beforeunload", { cancelable: true });
    expect(window.dispatchEvent(unloadEvent)).toBe(false);
    await typist().click(screen.getByRole("button", { name: "Close editor" }));

    expect(confirmSpy).toHaveBeenCalledWith(
      "Leave this editor? Your changes are saved as a draft but are not yet visible to readers.",
    );
    expect(screen.queryByText("pages landing")).not.toBeInTheDocument();
  });

  it("does not guard same-origin links that inherit a non-self base target", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [], next_cursor: null });
      }
      return jsonResponse({ data: page });
    });
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    const base = document.createElement("base");
    base.setAttribute("target", "_blank");
    const link = document.createElement("a");
    link.href = "/pages";
    link.textContent = "external target pages";
    document.head.appendChild(base);
    document.body.appendChild(link);

    try {
      renderWithRouter(
        "/pages/new",
        <>
            <Route path="/pages/new" element={<PageEditor mode="create" />} />
        </>,
      );

      await setEditorTitle(await findTitleEditor(), "Draft");
      await typist().click(link);

      expect(confirmSpy).not.toHaveBeenCalled();
    } finally {
      base.remove();
      link.remove();
    }
  });

  it("shows TreeRail instead of PageList on the legacy create route", async () => {
    vi.stubGlobal("innerWidth", 768);
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      return jsonResponse({ data: page });
    });
    renderWithRouter(
      "/pages/new",
      <>
          <Route path="/pages/new" element={<PageEditor mode="create" />} />
          <Route path="/pages/:slug" element={<div>reader page</div>} />
      </>,
    );

    const drawer = await openContextExplorer();
    const tree = within(drawer).getByLabelText("Context tree");
    expect(document.querySelector(".page-list")).not.toBeInTheDocument();
    expect(await within(tree).findByRole("treeitem", { name: "Inbox" })).toBeInTheDocument();
  });

  it("guards programmatic navigation when the editor has unsaved changes", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [], next_cursor: null });
      }
      return jsonResponse({ data: page });
    });
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);

    renderWithRouter(
      "/pages/new",
      <>
        <Route
          path="/pages/new"
          element={(
            <>
              <PageEditor mode="create" />
              <ProgrammaticNavigationButton />
            </>
          )}
        />
        <Route path="/dashboard" element={<div>dashboard</div>} />
      </>,
    );

    await setEditorTitle(await findTitleEditor(), "Draft");
    await typist().click(screen.getByRole("button", { name: "Programmatic nav" }));

    expect(confirmSpy).toHaveBeenCalledWith(
      "Leave this editor? Your changes are saved as a draft but are not yet visible to readers.",
    );
    expect(screen.queryByText("dashboard")).not.toBeInTheDocument();
  });

  it("proceeds with programmatic navigation when the confirm dialog is accepted", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [], next_cursor: null });
      }
      return jsonResponse({ data: page });
    });
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);

    renderWithRouter(
      "/pages/new",
      <>
        <Route
          path="/pages/new"
          element={(
            <>
              <PageEditor mode="create" />
              <ProgrammaticNavigationButton />
            </>
          )}
        />
        <Route path="/dashboard" element={<div>dashboard</div>} />
      </>,
    );

    await setEditorTitle(await findTitleEditor(), "Draft");
    await typist().click(screen.getByRole("button", { name: "Programmatic nav" }));

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(confirmSpy).toHaveBeenCalledWith(
      "Leave this editor? Your changes are saved as a draft but are not yet visible to readers.",
    );
    expect(await screen.findByText("dashboard")).toBeInTheDocument();
  });

  it("does not double-fire the programmatic navigation confirm under StrictMode", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [], next_cursor: null });
      }
      return jsonResponse({ data: page });
    });
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    const router = createMemoryRouter(
      createRoutesFromElements(
        <>
          <Route
            path="/pages/new"
            element={(
              <>
                <PageEditor mode="create" />
                <ProgrammaticNavigationButton />
              </>
            )}
          />
          <Route path="/dashboard" element={<div>dashboard</div>} />
        </>,
      ),
      { initialEntries: ["/pages/new"] },
    );

    render(
      <StrictMode>
        <RouterProvider router={router} />
      </StrictMode>,
    );

    await setEditorTitle(await findTitleEditor(), "Draft");
    await typist().click(screen.getByRole("button", { name: "Programmatic nav" }));

    await waitFor(() => {
      expect(confirmSpy).toHaveBeenCalledTimes(1);
    });
    expect(confirmSpy).toHaveBeenCalledWith(
      "Leave this editor? Your changes are saved as a draft but are not yet visible to readers.",
    );
    expect(screen.queryByText("dashboard")).not.toBeInTheDocument();
  });

  it("clears stale edit content while another slug is loading", async () => {
    let resolveOtherRead!: (response: Response) => void;
    const otherRead = new Promise<Response>((resolve) => {
      resolveOtherRead = resolve;
    });
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page, { ...page, id: "p2", slug: "other", title: "Other" }], next_cursor: null });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages/by-slug/other")) {
        return otherRead;
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <>
          <Route
            path="/pages/:slug/edit"
            element={(
              <>
                <PageEditor mode="edit" />
                <EditOtherButton />
              </>
            )}
          />
      </>,
    );

    await expectEditorTitle("Hello");
    expect(document.querySelector(".cm-content")?.textContent).toContain("# Hello");

    await typist().click(screen.getByRole("button", { name: "Edit other page" }));
    await within(screen.getByRole("navigation", { name: "Page location" }))
      .findByText("other");
    expect(document.querySelector(".cm-content")?.textContent ?? "").not.toContain("# Hello");

    resolveOtherRead(jsonResponse({ data: { ...page, id: "p2", slug: "other", title: "Other", body_md: "# Other" } }));
    await expectEditorTitle("Other");
  });

  it("keeps a late autosave response scoped to the page that dispatched it", async () => {
    let resolveHelloAutosave!: (response: Response) => void;
    const helloAutosave = new Promise<Response>((resolve) => {
      resolveHelloAutosave = resolve;
    });
    const otherPage = {
      ...page,
      id: "p2",
      slug: "other",
      title: "Other",
      body_md: "# Other",
      content_hash: "d".repeat(64),
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) return workspaceResponse();
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page, otherPage], next_cursor: null });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages/by-slug/hello")) {
        return jsonResponse({ data: page });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages/by-slug/other")) {
        return jsonResponse({ data: otherPage });
      }
      if (init?.method === "POST" && url.endsWith("/api/v1/workspaces/workspace-1/context/pages/p1/autosave")) {
        return helloAutosave;
      }
      if (init?.method === "POST" && url.endsWith("/api/v1/workspaces/workspace-1/context/pages/p2/autosave")) {
        const payload = JSON.parse(String(init.body));
        return jsonResponse({
          data: {
            ...otherPage,
            title: payload.title,
            version_id: "v-other-2",
            current_version_id: "v-other-2",
            content_hash: "e".repeat(64),
          },
        });
      }
      return jsonResponse({ data: page });
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const sendBeacon = vi.fn<(url: string, data?: BodyInit | null) => boolean>(() => true);
    Object.defineProperty(window.navigator, "sendBeacon", {
      configurable: true,
      value: sendBeacon,
    });

    renderWithRouter(
      "/pages/hello/edit",
      <Route
        path="/pages/:slug/edit"
        element={(
          <>
            <RoutedPageEditor mode="edit" />
            <EditOtherButton />
          </>
        )}
      />,
    );

    await setEditorTitle(await findTitleEditor(), "Hello pending");
    fireEvent.keyDown(window, { key: "s", ctrlKey: true });
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.filter((call) =>
          String(call[0]).endsWith("/api/v1/workspaces/workspace-1/context/pages/p1/autosave"),
        ),
      ).toHaveLength(1);
    });

    await typist().click(screen.getByRole("button", { name: "Edit other page" }));
    await expectEditorTitle("Other");
    await act(async () => {
      resolveHelloAutosave(jsonResponse({
        data: {
          ...page,
          title: "Hello pending",
          version_id: "v2",
          current_version_id: "v2",
          content_hash: "b".repeat(64),
        },
      }));
      await Promise.resolve();
    });
    await expectEditorTitle("Other");

    await setEditorTitle(getTitleEditor(), "Other updated");
    fireEvent.keyDown(window, { key: "s", ctrlKey: true });
    await waitFor(() => {
      const otherSave = fetchMock.mock.calls.find((call) =>
        String(call[0]).endsWith("/api/v1/workspaces/workspace-1/context/pages/p2/autosave"),
      );
      expect(JSON.parse(String(otherSave?.[1]?.body))).toMatchObject({
        title: "Other updated",
      });
    });
    const helloBodies = fetchMock.mock.calls
      .filter((call) => String(call[0]).endsWith("/api/v1/workspaces/workspace-1/context/pages/p1/autosave"))
      .map((call) => JSON.parse(String(call[1]?.body)));
    expect(helloBodies).toHaveLength(1);
    expect(helloBodies[0]).toMatchObject({ title: "Hello pending" });
  });

  it("switches to split mode with a resizable separator", async () => {
    await renderEditorOnHello();
    expect(screen.getByRole("navigation", { name: "Page location" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /Edit Hello/ })).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Split" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /split preview/i })).not.toBeInTheDocument();
    expect(syncStatus()).toHaveTextContent("Saved");
    expect(document.querySelector(".editor-command-status")).not.toBeInTheDocument();
    expect(document.querySelector(".preview-pane")).not.toBeInTheDocument();

    await typist().click(screen.getByRole("tab", { name: "Split" }));
    const separator = screen.getByRole("separator", {
      name: "Resize editor panes",
    }) as HTMLDivElement;
    expect(separator).toHaveAttribute("aria-valuenow", "50");
    expect(separator).toHaveAttribute("aria-orientation", "vertical");
    expect(separator).toHaveAttribute(
      "aria-controls",
      "page-editor-source-panel page-editor-reading-panel",
    );
    fireEvent.keyDown(separator, { key: "ArrowRight" });
    expect(separator).toHaveAttribute("aria-valuenow", "52");
    expect(separator).toHaveAttribute("aria-valuetext", "Source pane 52 percent");

    vi.stubGlobal("innerWidth", 1023);
    fireEvent(window, new Event("resize"));
    expect(separator).toHaveAttribute("aria-orientation", "horizontal");
    fireEvent.keyDown(separator, { key: "ArrowDown" });
    expect(separator).toHaveAttribute("aria-valuenow", "54");

    vi.stubGlobal("innerWidth", 1280);
    fireEvent(window, new Event("resize"));
    expect(separator).toHaveAttribute("aria-orientation", "vertical");

    const editorGrid = separator.closest(".editor-grid") as HTMLDivElement;
    vi.spyOn(editorGrid, "getBoundingClientRect").mockReturnValue(
      domRect({ height: 700, left: 100, top: 20, width: 1016 }),
    );
    vi.spyOn(separator, "getBoundingClientRect").mockReturnValue(
      domRect({ height: 700, left: 640, top: 20, width: 16 }),
    );
    let capturedPointer: number | null = null;
    separator.setPointerCapture = vi.fn((pointerId) => {
      capturedPointer = pointerId;
    });
    separator.hasPointerCapture = vi.fn((pointerId) => capturedPointer === pointerId);
    separator.releasePointerCapture = vi.fn(() => {
      capturedPointer = null;
    });
    fireEvent.pointerDown(separator, { clientX: 708, clientY: 200, pointerId: 7 });
    expect(separator).toHaveAttribute("aria-valuenow", "60");
    fireEvent.pointerMove(separator, { clientX: 508, clientY: 200, pointerId: 7 });
    expect(separator).toHaveAttribute("aria-valuenow", "40");
    fireEvent.pointerUp(separator, { clientX: 508, clientY: 200, pointerId: 7 });
    expect(separator.releasePointerCapture).toHaveBeenCalledWith(7);
    fireEvent.pointerMove(separator, { clientX: 708, clientY: 200, pointerId: 7 });
    expect(separator).toHaveAttribute("aria-valuenow", "40");
    expect(editorGrid.style.getPropertyValue("--editor-split-start")).toBe("40fr");
    expect(editorGrid.style.getPropertyValue("--editor-split-end")).toBe("60fr");
    const previewPane = document.querySelector(".preview-pane");
    expect(previewPane).toBeInstanceOf(HTMLElement);
    expect(within(previewPane as HTMLElement).queryByRole("heading", { name: "Hello" }))
      .not.toBeInTheDocument();

    fireEvent.keyDown(window, { key: "p", ctrlKey: true, shiftKey: true });
    expect(document.querySelector(".preview-pane")).toBeInTheDocument();

    fireEvent.keyDown(window, { key: "\\", ctrlKey: true });
    expect(document.querySelector(".preview-pane")).not.toBeInTheDocument();
  });

  it("toggles focus mode and the keyboard shortcuts dialog", async () => {
    await renderEditorOnHello();

    expect(screen.getByLabelText("Context tree")).toBeInTheDocument();
    await openPageProperties();
    expect(await screen.findByRole("region", { name: "Page properties" })).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "o", ctrlKey: true, shiftKey: true });
    expect(screen.queryByLabelText("Context tree")).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Page properties" })).not.toBeInTheDocument();
    expect(document.body).toHaveAttribute("data-focus-mode", "true");
    expect(document.querySelector(".page-workspace")).toHaveAttribute("data-focus-mode", "on");
    expect(screen.getByRole("button", { name: /Exit focus mode/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await typist().click(screen.getByRole("button", { name: /Exit focus mode/ }));
    expect(screen.getByLabelText("Context tree")).toBeInTheDocument();
    expect(document.body).not.toHaveAttribute("data-focus-mode");
    expect(document.querySelector(".page-workspace")).toHaveAttribute("data-focus-mode", "off");

    fireEvent.keyDown(window, { key: "?" });
    const shortcutsDialog = await screen.findByRole("dialog", { name: "Keyboard shortcuts" });
    expect(within(shortcutsDialog).getByText("Save draft")).toBeInTheDocument();
    expect(screen.getByText("Source, reading, or split")).toBeInTheDocument();
    expect(screen.getByText("Focus mode")).toBeInTheDocument();
    expect(within(shortcutsDialog).getByText("Code block")).toBeInTheDocument();
    expect(within(shortcutsDialog).getByText("Task list")).toBeInTheDocument();
    expect(within(shortcutsDialog).getByText("Link")).toBeInTheDocument();
    expect(within(shortcutsDialog).getByText("Comment")).toBeInTheDocument();
    expect(screen.queryByText("AI placeholder (selected text)")).not.toBeInTheDocument();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Keyboard shortcuts" })).not.toBeInTheDocument();
    fireEvent.keyDown(document.querySelector(".cm-content") as HTMLElement, { key: "?" });
    expect(screen.queryByRole("dialog", { name: "Keyboard shortcuts" })).not.toBeInTheDocument();
    await typist().click(screen.getByRole("button", { name: "Keyboard shortcuts" }));
    expect(await screen.findByRole("dialog", { name: "Keyboard shortcuts" })).toBeInTheDocument();
    await typist().click(screen.getByRole("button", { name: "Close keyboard shortcuts" }));
    expect(screen.queryByRole("dialog", { name: "Keyboard shortcuts" })).not.toBeInTheDocument();
  });

  it("cycles source, reading and split with Ctrl+E and inserts toolbar markup", async () => {
    await renderEditorOnHello();

    fireEvent.keyDown(window, { key: "e", ctrlKey: true });
    expect(screen.getByLabelText("Markdown body")).toBeInTheDocument();
    expect(document.querySelector("#page-editor-source-panel")).toHaveAttribute("hidden");
    expect(document.querySelector("#page-editor-source-panel")).toHaveAttribute("inert");
    const readingPane = document.querySelector(".preview-pane");
    expect(readingPane).toBeInstanceOf(HTMLElement);
    expect(within(readingPane as HTMLElement).queryByRole("heading", { name: "Hello" }))
      .not.toBeInTheDocument();

    fireEvent.keyDown(window, { key: "e", ctrlKey: true });
    expect(await screen.findByLabelText("Markdown body")).toBeInTheDocument();
    expect(document.querySelector("#page-editor-source-panel")).not.toHaveAttribute("hidden");
    expect(document.querySelector(".preview-pane")).toBeInTheDocument();

    fireEvent.keyDown(window, { key: "e", ctrlKey: true });
    expect(document.querySelector(".preview-pane")).not.toBeInTheDocument();

    await typist().click(screen.getByRole("tab", { name: "Source" }));
    await screen.findByLabelText("Markdown body");
    pressEditorShortcut(codeMirrorView(), { key: "b", ctrlKey: true });

    await waitFor(() => {
      expect(document.querySelector(".cm-content")?.textContent)
        .toContain("**bold text**");
    });

    fireEvent.keyDown(document.querySelector(".cm-content") as HTMLElement, {
      key: "c",
      ctrlKey: true,
      shiftKey: true,
    });
    await waitFor(() => {
      expect(document.querySelector(".cm-content")?.textContent)
        .toContain("@cite{bold text}");
    });
  });

  it("syncs both panes once per frame, preserves user takeover, invalidates, and toggles off", async () => {
    const body = [
      "# Hello",
      "",
      "Alpha",
      "",
      "## Middle",
      "",
      "Omega",
      "",
      "## End",
      "",
      "Done",
    ].join("\n");
    mockEditorPage(body);

    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );
    await screen.findByLabelText("Markdown body");

    const animationFrames: FrameRequestCallback[] = [];
    const requestFrame = vi.fn((callback: FrameRequestCallback) => {
      animationFrames.push(callback);
      return animationFrames.length;
    });
    vi.stubGlobal("requestAnimationFrame", requestFrame);
    vi.stubGlobal("cancelAnimationFrame", vi.fn());

    let resizeCallback: ResizeObserverCallback | null = null;
    class TestResizeObserver {
      constructor(callback: ResizeObserverCallback) {
        resizeCallback = callback;
      }
      observe() {}
      disconnect() {}
      unobserve() {}
    }
    vi.stubGlobal("ResizeObserver", TestResizeObserver);

    // The sync reads two windows from performance.now(): the 160 ms during
    // which a scroll event is taken for the echo of the sync's own write
    // (programmaticScroll), and the 250 ms during which a geometry
    // invalidation leaves the authority with the pane the user moved last
    // (invalidateAnchors). The test owns that clock from before Split is
    // activated, so a pause between two statements can neither close a window
    // the test still means to be inside nor leave one open.
    const ANIMATION_FRAME_MS = 16;
    const ECHO_WINDOW_MS = 160;
    const GEOMETRY_AUTHORITY_MS = 250;
    const clock = installLogicalPerformanceClock();

    await typist().click(screen.getByRole("tab", { name: "Split" }));
    expect(screen.getByRole("button", { name: "Linked scrolling" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    const view = codeMirrorView();
    const sourcePane = view.scrollDOM;
    const previewPane = document.querySelector(".preview-pane") as HTMLElement;
    expect(previewPane).toBeInstanceOf(HTMLElement);
    expect(previewPane).toHaveAttribute("tabindex", "0");

    Object.defineProperties(sourcePane, {
      clientHeight: { configurable: true, value: 200 },
      scrollHeight: { configurable: true, value: 400 },
    });
    Object.defineProperties(previewPane, {
      clientHeight: { configurable: true, value: 200 },
      clientTop: { configurable: true, value: 1 },
      scrollHeight: { configurable: true, value: 1_000 },
    });
    vi.spyOn(previewPane, "getBoundingClientRect").mockReturnValue(
      domRect({ height: 200, left: 0, top: 0, width: 600 }),
    );
    const previewGeometry = new Map([
      [1, { top: 0, bottom: 50 }],
      [3, { top: 100, bottom: 400 }],
      [5, { top: 430, bottom: 480 }],
      [7, { top: 500, bottom: 550 }],
      [9, { top: 570, bottom: 620 }],
      [11, { top: 640, bottom: 1_000 }],
    ]);
    for (const anchor of previewPane.querySelectorAll<HTMLElement>(
      '[data-scroll-sync-anchor="true"]',
    )) {
      const startLine = Number(anchor.dataset.sourceLine);
      const geometry = previewGeometry.get(startLine) ?? {
        top: (startLine - 1) * 40,
        bottom: startLine * 40,
      };
      vi.spyOn(anchor, "getBoundingClientRect").mockImplementation(() =>
        domRect({
          height: geometry.bottom - geometry.top,
          left: 0,
          top: geometry.top + previewPane.clientTop - previewPane.scrollTop,
          width: 560,
        }),
      );
    }

    const sourceBoundaries = [0, 20, 40, 140, 160, 180, 200, 220, 240, 260, 280, 400];
    vi.spyOn(view, "documentPadding", "get").mockReturnValue({ top: 8, bottom: 6 });
    vi.spyOn(view, "scaleY", "get").mockReturnValue(2);
    const sourcePaddingTop = view.documentPadding.top;
    const sourceScaleY = view.scaleY;
    function normalBlockAtHeight(height: number) {
      const contentOffset = (height + sourcePaddingTop) / sourceScaleY;
      let lineNo = 1;
      while (
        lineNo < view.state.doc.lines &&
        sourceBoundaries[lineNo] <= contentOffset
      ) {
        lineNo += 1;
      }
      return {
        from: view.state.doc.line(lineNo).from,
        top: sourceBoundaries[lineNo - 1] * sourceScaleY - sourcePaddingTop,
        to: view.state.doc.line(lineNo).to,
      } as ReturnType<
        EditorView["lineBlockAtHeight"]
      >;
    }
    function normalBlockAt(position: number) {
      const lineNo = view.state.doc.lineAt(position).number;
      return {
        from: view.state.doc.line(lineNo).from,
        top: sourceBoundaries[lineNo - 1] * sourceScaleY - sourcePaddingTop,
        to: view.state.doc.line(lineNo).to,
      } as ReturnType<EditorView["lineBlockAt"]>;
    }
    const lineBlockAtHeightSpy = vi.spyOn(view, "lineBlockAtHeight")
      .mockImplementation(normalBlockAtHeight);
    const lineBlockAtSpy = vi.spyOn(view, "lineBlockAt")
      .mockImplementation(normalBlockAt);

    // A requested frame runs one display frame later, and its callback gets
    // the instant that production reads from performance.now().
    function flushFrame() {
      const callbacks = animationFrames.splice(0);
      clock.advance(ANIMATION_FRAME_MS);
      act(() => callbacks.forEach((callback) => callback(clock.now())));
    }

    flushFrame();
    requestFrame.mockClear();
    sourcePane.scrollTop = 50;
    fireEvent.scroll(sourcePane);
    expect(requestFrame).toHaveBeenCalledTimes(1);
    flushFrame();
    expect(lineBlockAtHeightSpy).toHaveBeenLastCalledWith(192);
    // The viewport anchor is 60% through a visually wrapped source line and
    // lands inside the tall rendered paragraph. A shared 25% scroll ratio
    // would be 200px, so this proves the mapping follows content geometry.
    expect(previewPane.scrollTop).toBeCloseTo(180, 5);

    requestFrame.mockClear();
    fireEvent.scroll(previewPane);
    fireEvent.scroll(previewPane);
    expect(requestFrame).not.toHaveBeenCalled();

    fireEvent.wheel(previewPane);
    previewPane.scrollTop = 181;
    fireEvent.scroll(previewPane);
    expect(requestFrame).toHaveBeenCalledTimes(1);
    flushFrame();
    expect(sourcePane.scrollTop).toBeGreaterThan(50);
    expect(sourcePane.scrollTop).toBeLessThan(51);

    // A larger preview movement is also a real takeover and must drive the
    // source pane in the reverse direction.
    previewPane.scrollTop = 500;
    fireEvent.scroll(previewPane);
    flushFrame();
    expect(sourcePane.scrollTop).toBeCloseTo(126, 5);

    requestFrame.mockClear();
    sourcePane.scrollTop = 140;
    for (let index = 0; index < 120; index += 1) {
      fireEvent.scroll(sourcePane);
    }
    expect(requestFrame).toHaveBeenCalledTimes(1);
    flushFrame();

    const anchorQuery = vi.spyOn(previewPane, "querySelectorAll");
    sourcePane.scrollTop = 40;
    fireEvent.scroll(sourcePane);
    flushFrame();
    expect(anchorQuery).toHaveBeenCalledTimes(0);

    requestFrame.mockClear();
    act(() => resizeCallback?.([], {} as ResizeObserver));
    expect(requestFrame).toHaveBeenCalledTimes(1);
    const sourceTopBeforeLayoutShift = sourcePane.scrollTop;
    previewPane.scrollTop += 17;
    fireEvent.scroll(previewPane);
    expect(sourcePane.scrollTop).toBe(sourceTopBeforeLayoutShift);
    flushFrame();
    expect(anchorQuery).toHaveBeenCalledTimes(1);
    expect(sourcePane.scrollTop).toBe(sourceTopBeforeLayoutShift);

    sourcePane.scrollTop = 200;
    fireEvent.scroll(sourcePane);
    flushFrame();
    expect(previewPane.scrollTop).toBe(800);

    previewPane.scrollTop = 0;
    fireEvent.scroll(previewPane);
    flushFrame();
    expect(sourcePane.scrollTop).toBe(0);

    const foldedBlock = {
      from: view.state.doc.line(3).from,
      to: view.state.doc.line(5).to,
      top: sourceBoundaries[2] * sourceScaleY - sourcePaddingTop,
    } as ReturnType<EditorView["lineBlockAt"]>;
    lineBlockAtHeightSpy.mockImplementation((height) => {
      const contentOffset = (height + sourcePaddingTop) / sourceScaleY;
      return contentOffset >= sourceBoundaries[2] &&
        contentOffset < sourceBoundaries[5]
        ? foldedBlock
        : normalBlockAtHeight(height);
    });
    lineBlockAtSpy.mockImplementation((position) => {
      const lineNo = view.state.doc.lineAt(position).number;
      return lineNo >= 3 && lineNo <= 5
        ? foldedBlock
        : normalBlockAt(position);
    });
    sourcePane.scrollTop = 50;
    fireEvent.scroll(sourcePane);
    flushFrame();
    expect(Number.isFinite(previewPane.scrollTop)).toBe(true);
    const foldedPreviewTop = previewPane.scrollTop;
    fireEvent.pointerDown(previewPane);
    previewPane.scrollTop = foldedPreviewTop + 1;
    fireEvent.scroll(previewPane);
    flushFrame();
    expect(sourcePane.scrollTop).toBeGreaterThan(45);
    expect(sourcePane.scrollTop).toBeLessThan(55);

    requestFrame.mockClear();
    const previewTopBeforeLayoutShift = previewPane.scrollTop;
    const testImage = document.createElement("img");
    previewPane.append(testImage);
    act(() => {
      resizeCallback?.([], {} as ResizeObserver);
      previewPane.dispatchEvent(new CustomEvent(
        "dtxwiki:markdown-geometry-change",
        { bubbles: true },
      ));
      fireEvent.load(testImage);
      fireEvent.error(testImage);
    });
    expect(requestFrame).toHaveBeenCalledTimes(1);
    sourcePane.scrollTop += 13;
    fireEvent.scroll(sourcePane);
    expect(previewPane.scrollTop).toBe(previewTopBeforeLayoutShift);
    flushFrame();
    expect(previewPane.scrollTop).toBe(previewTopBeforeLayoutShift);
    testImage.remove();

    // Both windows are read from the logical clock, so their edges can be
    // pinned exactly. Every case starts from user input on the pane that
    // drives the sync (which claims that pane and drops any earlier
    // authority), opens a window of its own, lets exactly `elapsed` ms pass,
    // and then delivers the one event the window has to classify. Plain line
    // geometry is restored so that the positions below do not depend on the
    // fold above; it maps source 100 to preview 400, source 126 to preview 500
    // and source 140 to preview 540.
    lineBlockAtHeightSpy.mockImplementation(normalBlockAtHeight);
    lineBlockAtSpy.mockImplementation(normalBlockAt);

    function scrollByUser(pane: HTMLElement, top: number) {
      fireEvent.wheel(pane);
      pane.scrollTop = top;
      fireEvent.scroll(pane);
      flushFrame();
    }

    // The sync writes the pane the user did not move, which opens its echo
    // window; after `elapsed` ms that pane reports its own, unchanged position.
    function deliverEchoAfter(
      elapsed: number,
      userPane: HTMLElement,
      userTop: number,
      echoPane: HTMLElement,
    ) {
      const echoTopBeforeWrite = echoPane.scrollTop;
      scrollByUser(userPane, userTop);
      expect(echoPane.scrollTop).not.toBe(echoTopBeforeWrite);
      clock.advance(elapsed);
      requestFrame.mockClear();
      fireEvent.scroll(echoPane);
    }

    // The echo window is inclusive: at its deadline the event is still the
    // write's own echo, one millisecond later it is user input.
    deliverEchoAfter(ECHO_WINDOW_MS, sourcePane, 100, previewPane);
    expect(requestFrame).not.toHaveBeenCalled();
    deliverEchoAfter(ECHO_WINDOW_MS + 1, sourcePane, 120, previewPane);
    expect(requestFrame).toHaveBeenCalledTimes(1);
    deliverEchoAfter(ECHO_WINDOW_MS, previewPane, 500, sourcePane);
    expect(requestFrame).not.toHaveBeenCalled();
    deliverEchoAfter(ECHO_WINDOW_MS + 1, previewPane, 300, sourcePane);
    expect(requestFrame).toHaveBeenCalledTimes(1);
    flushFrame();

    // A geometry invalidation hands the pane the user moved the authority for
    // 250 ms. After `elapsed` ms the other pane reports a scroll to
    // `shiftedTop`, and the frame the invalidation requested runs.
    function shiftLayoutAfter(
      elapsed: number,
      ownerPane: HTMLElement,
      ownerTop: number,
      shiftedPane: HTMLElement,
      shiftedTop: number,
    ) {
      scrollByUser(ownerPane, ownerTop);
      requestFrame.mockClear();
      act(() => resizeCallback?.([], {} as ResizeObserver));
      expect(requestFrame).toHaveBeenCalledTimes(1);
      clock.advance(elapsed);
      shiftedPane.scrollTop = shiftedTop;
      fireEvent.scroll(shiftedPane);
      flushFrame();
    }

    // Authority is inclusive as well. At the deadline the shifted pane is
    // layout moving under the owner, so the owner stays and the shifted pane
    // is put back; one millisecond later it is a takeover, so the owner
    // follows the shifted pane and the shifted pane stays where it was moved.
    shiftLayoutAfter(GEOMETRY_AUTHORITY_MS, sourcePane, 100, previewPane, 500);
    expect(sourcePane.scrollTop).toBe(100);
    expect(previewPane.scrollTop).toBeCloseTo(400, 5);
    shiftLayoutAfter(GEOMETRY_AUTHORITY_MS + 1, sourcePane, 100, previewPane, 500);
    expect(sourcePane.scrollTop).toBeCloseTo(126, 5);
    expect(previewPane.scrollTop).toBe(500);
    shiftLayoutAfter(GEOMETRY_AUTHORITY_MS, previewPane, 500, sourcePane, 140);
    expect(previewPane.scrollTop).toBe(500);
    expect(sourcePane.scrollTop).toBeCloseTo(126, 5);
    shiftLayoutAfter(GEOMETRY_AUTHORITY_MS + 1, previewPane, 500, sourcePane, 140);
    expect(sourcePane.scrollTop).toBe(140);
    expect(previewPane.scrollTop).toBeCloseTo(540, 5);

    await typist().click(screen.getByRole("button", { name: "Linked scrolling" }));
    expect(screen.getByRole("button", { name: "Linked scrolling" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
    const independentPreviewTop = previewPane.scrollTop;
    const independentSourceTop = sourcePane.scrollTop;
    requestFrame.mockClear();
    sourcePane.scrollTop = 180;
    fireEvent.scroll(sourcePane);
    previewPane.scrollTop = 420;
    fireEvent.scroll(previewPane);
    expect(requestFrame).not.toHaveBeenCalled();
    expect(previewPane.scrollTop).toBe(420);
    expect(sourcePane.scrollTop).toBe(180);
    expect(previewPane.scrollTop).not.toBe(independentPreviewTop);
    expect(sourcePane.scrollTop).not.toBe(independentSourceTop);
  });

  it("does not offer Split and renders one pane at 390px", async () => {
    await renderMobileEditor();
    expect(screen.queryByRole("tab", { name: "Split" })).not.toBeInTheDocument();
    expect(document.querySelectorAll(".source-pane, .preview-pane")).toHaveLength(1);
    expect(document.querySelector(".preview-pane")).not.toBeInTheDocument();

    const actionBar = screen.getByRole("toolbar", { name: "Editor actions" });
    expect(actionBar.closest(".editor-action-bar-shell")).toHaveAttribute(
      "data-viewport",
      "mobile",
    );
    expect(within(actionBar).getByRole("button", { name: "Attach files" }))
      .toBeInTheDocument();
    expect(imageUploadInput()).toHaveAttribute("aria-hidden", "true");
    expect(imageUploadInput()).toHaveAttribute("tabindex", "-1");
  });

  it("keeps tags in the Page properties popover at 390px", async () => {
    await renderMobileEditor();

    expect(screen.queryByLabelText("Tags")).not.toBeInTheDocument();
    const propertiesButton = screen.getByRole("button", { name: "Page properties" });
    await typist().click(propertiesButton);
    const properties = await screen.findByRole("region", { name: "Page properties" });
    const mobileTags = within(properties).getByLabelText("Tags");
    expect(mobileTags).toHaveAttribute("id", "editor-mobile-tags");
    commitChip(mobileTags, "Mobile");
    expect(within(properties).getByText("Mobile")).toBeInTheDocument();
    await typist().click(within(properties).getByRole("button", { name: "Close properties" }));
    expect(screen.queryByRole("region", { name: "Page properties" })).not.toBeInTheDocument();
    expect(propertiesButton).toHaveFocus();
    await typist().click(propertiesButton);
    expect(within(await screen.findByRole("region", { name: "Page properties" })).getByText("Mobile"))
      .toBeInTheDocument();
    await typist().click(screen.getByRole("button", { name: "Close properties" }));
  });

  it("opens the Context explorer as a modal drawer at 390px and closes it on Escape", async () => {
    await renderMobileEditor();

    const railToggle = screen.getByRole("button", {
      name: "Open Context explorer",
    });
    expect(railToggle).toHaveAttribute("aria-expanded", "false");
    const closedRail = document.querySelector("#page-editor-context-rail");
    expect(closedRail).toHaveAttribute("data-mobile-open", "false");
    expect(closedRail).toHaveAttribute("aria-hidden", "true");
    expect(closedRail).toHaveAttribute("inert");
    await typist().click(railToggle);
    expect(railToggle).toHaveAttribute("aria-expanded", "true");
    const drawer = screen.getByRole("dialog", { name: "Context drawer" });
    expect(drawer).toHaveAttribute("data-mobile-open", "true");
    expect(drawer).toHaveAttribute("aria-modal", "true");
    await waitFor(() => expect(drawer.contains(document.activeElement)).toBe(true));
    expect(document.querySelector(".workspace-mobile-drawer-scrim"))
      .toHaveAccessibleName("Close Context explorer");
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    await waitFor(() => expect(railToggle).toHaveFocus());
    expect(railToggle).toHaveAttribute("aria-expanded", "false");
    expect(closedRail).toHaveAttribute("aria-hidden", "true");
    expect(closedRail).toHaveAttribute("inert");
  });

  it("moves between the Source and Reading tabs with the keyboard at 390px", async () => {
    await renderMobileEditor();

    const sourceTab = screen.getByRole("tab", { name: "Source" });
    sourceTab.focus();
    fireEvent.keyDown(sourceTab, { key: "End" });
    const readingTab = screen.getByRole("tab", { name: "Reading" });
    expect(readingTab).toHaveFocus();
    expect(readingTab).toHaveAttribute("aria-selected", "true");
    const readingBold = within(
      screen.getByRole("toolbar", { name: "Editor actions" }),
    ).getByRole("button", { name: "Bold" });
    expect(readingBold).toHaveAttribute("aria-disabled", "true");
    expect(readingBold).toHaveAccessibleDescription(
      "Editing commands are unavailable in Reading mode.",
    );
    fireEvent.keyDown(readingTab, { key: "Home" });
    expect(screen.getByRole("tab", { name: "Source" })).toHaveFocus();
  });

  it("uses an inert, focus-trapped Context drawer at 768px without collapsing editor modes", async () => {
    vi.stubGlobal("innerWidth", 768);
    window.localStorage.setItem(
      "dtxwiki:editor:default:hello",
      JSON.stringify({ mode: "split" }),
    );
    mockEditorPage("# Hello\n\nBody");

    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    expect(await screen.findByLabelText("Markdown body")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Split" })).toBeInTheDocument();
    expect(document.querySelectorAll(".source-pane, .preview-pane")).toHaveLength(2);
    expect(
      screen.getByRole("toolbar", { name: "Editor actions" })
        .closest(".editor-action-bar-shell"),
    ).toHaveAttribute("data-viewport", "desktop");

    const railToggle = screen.getByRole("button", {
      name: "Open Context explorer",
    });
    const closedRail = document.querySelector("#page-editor-context-rail");
    expect(closedRail).toHaveAttribute("role", "dialog");
    expect(closedRail).toHaveAttribute("aria-hidden", "true");
    expect(closedRail).toHaveAttribute("inert");
    expect(screen.queryByRole("dialog", { name: "Context drawer" }))
      .not.toBeInTheDocument();

    await typist().click(railToggle);
    const drawer = screen.getByRole("dialog", { name: "Context drawer" });
    await waitFor(() => expect(drawer.contains(document.activeElement)).toBe(true));

    const focusable = Array.from(
      drawer.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ),
    ).filter((element) => !element.hasAttribute("aria-hidden"));
    expect(focusable.length).toBeGreaterThan(1);
    focusable.at(-1)?.focus();
    fireEvent.keyDown(focusable.at(-1) as HTMLElement, { key: "Tab" });
    expect(focusable[0]).toHaveFocus();
    fireEvent.keyDown(focusable[0], { key: "Tab", shiftKey: true });
    expect(focusable.at(-1)).toHaveFocus();

    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    await waitFor(() => expect(railToggle).toHaveFocus());
    expect(railToggle).toHaveAttribute("aria-expanded", "false");
    expect(closedRail).toHaveAttribute("aria-hidden", "true");
    expect(closedRail).toHaveAttribute("inert");

    await typist().click(railToggle);
    await waitFor(() => expect(drawer.contains(document.activeElement)).toBe(true));
    await typist().click(
      within(drawer).getByRole("button", { name: "Close Context explorer" }),
    );
    await waitFor(() => expect(railToggle).toHaveFocus());
    expect(railToggle).toHaveAttribute("aria-expanded", "false");
  });

  it("compacts the ActionBar at the 720px zoom-equivalent width without collapsing modes", async () => {
    vi.stubGlobal("innerWidth", 720);
    mockEditorPage("# Hello\n\nBody");

    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    expect(await screen.findByLabelText("Markdown body")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Split" })).toBeInTheDocument();
    const toolbar = screen.getByRole("toolbar", { name: "Editor actions" });
    expect(toolbar.closest(".editor-action-bar-shell")).toHaveAttribute(
      "data-viewport",
      "mobile",
    );
    expect(within(toolbar).getByRole("button", { name: "Attach files" })).toBeInTheDocument();
    expect(within(toolbar).getByRole("button", { name: "More editor actions" })).toBeInTheDocument();
  });

  it("keeps the CodeMirror upload-anchor owner mounted while Reading is active", async () => {
    mockEditorPage("# Hello\n\nBody");
    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    await screen.findByLabelText("Markdown body");
    const view = codeMirrorView();
    const sourcePane = document.querySelector("#page-editor-source-panel");
    expect(sourcePane).not.toHaveAttribute("hidden");

    await typist().click(screen.getByRole("tab", { name: "Reading" }));
    expect(sourcePane).toHaveAttribute("hidden");
    expect(sourcePane).toHaveAttribute("aria-hidden", "true");
    expect(sourcePane).toHaveAttribute("inert");

    await typist().click(screen.getByRole("tab", { name: "Source" }));
    expect(sourcePane).not.toHaveAttribute("hidden");
    expect(codeMirrorView()).toBe(view);
  });

  it("routes ActionBar text toggles through exactly one document-changing CodeMirror dispatch", async () => {
    mockEditorPage("alpha");
    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    await screen.findByLabelText("Markdown body");
    const view = codeMirrorView();
    act(() => {
      view.dispatch({ selection: { anchor: 0, head: 5 } });
      view.focus();
    });
    const dispatchSpy = vi.spyOn(view, "dispatch");
    const actionBar = screen.getByRole("toolbar", { name: "Editor actions" });

    dispatchSpy.mockClear();
    act(() => fireEvent.click(within(actionBar).getByRole("button", { name: "Bold" })));
    expect(documentChangingDispatches(dispatchSpy.mock.calls)).toHaveLength(1);
    expect(view.state.doc.toString()).toBe("**alpha**");

    dispatchSpy.mockClear();
    act(() => fireEvent.click(within(actionBar).getByRole("button", { name: "Bold" })));
    expect(documentChangingDispatches(dispatchSpy.mock.calls)).toHaveLength(1);
    expect(view.state.doc.toString()).toBe("alpha");
  });

  it("opens the hidden attachment input synchronously from visible Attach", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) return workspaceResponse();
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/context/media/capabilities`) {
        return jsonResponse({
          data: {
            max_upload_bytes: 1024,
            policy_revision: "phase2-action-bar",
            uploadable_media_types: ["image/png", "image/jpeg"],
          },
        });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [{ ...page, body_md: "alpha" }], next_cursor: null });
      }
      return jsonResponse({ data: { ...page, body_md: "alpha" } });
    });
    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    await screen.findByLabelText("Markdown body");
    const input = imageUploadInput();
    await waitFor(() => expect(input).toBeEnabled());
    const clickSpy = vi.spyOn(input, "click");
    const attach = within(
      screen.getByRole("toolbar", { name: "Editor actions" }),
    ).getByRole("button", { name: "Attach files" });

    fireEvent.click(attach);
    expect(clickSpy).toHaveBeenCalledTimes(1);
  });

  it("opens the block insertion menu and inserts dtxWiki blocks", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <>
          <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    await screen.findByLabelText("Markdown body");
    const menu = await openBlockMenuFromEditor();
    const combobox = within(menu).getByRole("combobox", {
      name: "Filter block commands",
    });
    const listbox = within(menu).getByRole("listbox");
    expect(combobox).toHaveAttribute("aria-controls", listbox.id);
    expect(combobox).toHaveAttribute("aria-expanded", "true");
    const activeOptionId = combobox.getAttribute("aria-activedescendant");
    expect(activeOptionId).toBeTruthy();
    expect(document.getElementById(activeOptionId ?? "")).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(within(menu).getByRole("option", { name: "Heading 2" }))
      .toHaveAttribute("data-command-id", "heading");
    fillField(within(menu).getByLabelText("Filter block commands"), "decision");
    await typist().click(
      within(menu).getByRole("option", { name: "Decision block" }),
    );

    await waitFor(() => {
      expect(document.querySelector(".cm-content")?.textContent)
        .toMatch(/<!-- decision id=dec-[a-z0-9]{4,6} status=proposed owner=@owner -->/);
      expect(document.querySelector(".cm-content")?.textContent)
        .toContain("<!-- /decision -->");
    });
  });

  it("applies keyboard formatting shortcuts for code and lists", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <>
        <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    await screen.findByLabelText("Markdown body");
    const view = codeMirrorView();

    replaceEditorSelection(view, "Inline");
    pressEditorShortcut(view, { key: ".", ctrlKey: true, shiftKey: true });
    expect(view.state.doc.toString()).toBe("`Inline`");

    replaceEditorSelection(view, "alpha\nbeta");
    pressEditorShortcut(view, { key: "k", ctrlKey: true, shiftKey: true });
    expect(view.state.doc.toString()).toBe("```\nalpha\nbeta\n```");

    replaceEditorSelection(view, "alpha\nbeta");
    pressEditorShortcut(view, { key: "7", ctrlKey: true, shiftKey: true });
    expect(view.state.doc.toString()).toBe("1. alpha\n2. beta");

    replaceEditorSelection(view, "alpha\nbeta");
    pressEditorShortcut(view, { key: "8", ctrlKey: true, shiftKey: true });
    expect(view.state.doc.toString()).toBe("- alpha\n- beta");

    replaceEditorSelection(view, "alpha\nbeta");
    pressEditorShortcut(view, { key: "9", ctrlKey: true, shiftKey: true });
    expect(view.state.doc.toString()).toBe("- [ ] alpha\n- [ ] beta");
  });

  it("opens an emoji picker from the slash block menu and inserts Unicode", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <>
          <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    await screen.findByLabelText("Markdown body");
    const menu = await openBlockMenuFromEditor();
    fillField(within(menu).getByLabelText("Filter block commands"), "emoji");
    await typist().click(within(menu).getByRole("option", { name: "Emoji" }));

    const picker = await screen.findByRole("dialog", { name: "Emoji picker" });
    fillField(within(picker).getByLabelText("Search emoji"), "rocket");
    await typist().click(within(picker).getByRole("option", { name: "Insert emoji Rocket" }));

    await waitFor(() => {
      expect(document.querySelector(".cm-content")?.textContent).toContain("🚀");
    });
  });

  it("hides AI slash menu commands while rewrite controls are disabled", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <>
          <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    await screen.findByLabelText("Markdown body");
    await waitFor(() => {
      expect(document.querySelector(".cm-content")?.textContent).toContain("# Hello");
    });
    const menu = await openBlockMenuFromEditor();
    fillField(within(menu).getByLabelText("Filter block commands"), "summarize");

    expect(within(menu).queryByRole("option", { name: "Summarize selection" }))
      .not.toBeInTheDocument();
    expect(within(menu).getByText("No matching blocks")).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "AI inline edit unavailable" }))
      .not.toBeInTheDocument();
  });

  it("shows a floating formatting toolbar for selected editor text", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <>
          <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    await screen.findByLabelText("Markdown body");
    const editor = document.querySelector(".cm-content") as HTMLElement;
    await waitFor(() => {
      expect(editor.textContent).toContain("# Hello");
    });
    const view = codeMirrorView();
    mockEditorGeometry(view);
    view.dispatch({ selection: { anchor: 0, head: view.state.doc.length } });

    const inlineToolbar = await screen.findByRole("toolbar", {
      name: "Selection formatting toolbar",
    });
    expect(within(inlineToolbar).getByRole("button", { name: "Bold" }))
      .toBeInTheDocument();
    expect(within(inlineToolbar).getByRole("button", { name: "Comment" }))
      .toBeInTheDocument();

    await typist().click(within(inlineToolbar).getByRole("button", { name: "Bold" }));
    await waitFor(() => {
      expect(editor.textContent).toContain("**# Hello**");
    });

    await typist().click(getTitleEditor());
    await waitFor(() => {
      expect(screen.queryByRole("toolbar", { name: "Selection formatting toolbar" }))
        .not.toBeInTheDocument();
    });
  });

  it("marks toolbar buttons active when selected Markdown is already formatted", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      return jsonResponse({ data: { ...page, body_md: "**Hello** and `code`" } });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <>
          <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    await screen.findByLabelText("Markdown body");
    await waitFor(() => {
      expect(document.querySelector(".cm-content")?.textContent).toContain("**Hello**");
    });
    const view = codeMirrorView();
    mockEditorGeometry(view);
    act(() => {
      view.dispatch({ selection: { anchor: 2, head: 7 } });
      view.focus();
    });

    const inlineToolbar = await screen.findByRole("toolbar", {
      name: "Selection formatting toolbar",
    });
    await waitFor(() => {
      expect(within(inlineToolbar).getByRole("button", { name: "Bold" }))
        .toHaveAttribute("aria-pressed", "true");
    });
    expect(within(inlineToolbar).getByRole("button", { name: "Italic" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("detects registered Markdown command state around selected text", () => {
    expect(getMarkdownCommandState({
      body: "**Hello**",
      commandId: "bold",
      selection: { anchor: 2, head: 7 },
    })).toBe("active");
    expect(getMarkdownCommandState({
      body: "**Hello**",
      commandId: "italic",
      selection: { anchor: 2, head: 7 },
    })).toBe("inactive");
    expect(getMarkdownCommandState({
      body: "*Hello*",
      commandId: "italic",
      selection: { anchor: 1, head: 6 },
    })).toBe("active");
    expect(getMarkdownCommandState({
      body: "[[hello]] @cite{src}",
      commandId: "wikilink",
      selection: { anchor: 2, head: 7 },
    })).toBe("active");
    expect(getMarkdownCommandState({
      body: "[[hello]] @cite{src}",
      commandId: "citation",
      selection: { anchor: 16, head: 19 },
    })).toBe("active");
  });

  it("formats italic selections with asterisks instead of underscores", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      return jsonResponse({ data: { ...page, body_md: "foo_bar_baz" } });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <>
          <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    await screen.findByLabelText("Markdown body");
    const editor = document.querySelector(".cm-content") as HTMLElement;
    await waitFor(() => {
      expect(editor.textContent).toContain("foo_bar_baz");
    });
    const view = codeMirrorView();
    mockEditorGeometry(view);
    act(() => {
      view.dispatch({ selection: { anchor: 0, head: view.state.doc.length } });
      view.focus();
    });

    const inlineToolbar = await screen.findByRole("toolbar", {
      name: "Selection formatting toolbar",
    });
    await typist().click(
      within(inlineToolbar).getByRole("button", { name: "Italic" }),
    );

    await waitFor(() => {
      expect(editor.textContent).toContain("*foo_bar_baz*");
    });
    expect(editor.textContent).not.toContain("_foo_bar_baz_");
  });

  it("creates an inline comment anchored to selected editor text", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/context/pages/p1/comments` && init?.method === "POST") {
        return jsonResponse({
          data: {
            id: "review-1",
            page_id: "p1",
            review_item_id: "review-1",
            change_set_id: "change-1",
            change_operation_id: "op-1",
            status: "pending",
            risk_level: "low",
            note: "Needs a source.",
            anchor: JSON.parse(String(init.body)).anchor,
            created_at: "2026-05-20T00:00:00Z",
            updated_at: "2026-05-20T00:00:00Z",
          },
        }, 201);
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <>
          <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    await screen.findByLabelText("Markdown body");
    await waitFor(() => {
      expect(document.querySelector(".cm-content")?.textContent).toContain("# Hello");
    });
    const view = codeMirrorView();
    mockEditorGeometry(view);
    act(() => {
      view.dispatch({ selection: { anchor: 2, head: 7 } });
      view.focus();
    });
    await screen.findByRole("toolbar", { name: "Selection formatting toolbar" });

    pressEditorShortcut(view, { key: "m", ctrlKey: true, shiftKey: true });

    const commentDialog = await screen.findByRole("dialog", { name: "Comment preview" });
    expect(within(commentDialog).getByLabelText("Selected text")).toHaveTextContent("Hello");
    fillField(within(commentDialog).getByLabelText("Note"), "Needs a source.");
    expect(within(commentDialog).getByLabelText("Note")).toHaveValue("Needs a source.");
    expect(within(commentDialog).getByRole("button", { name: "Create" })).toBeEnabled();
    expect(within(commentDialog).getByRole("status"))
      .toHaveTextContent("Anchored to the current saved page version.");

    await typist().click(within(commentDialog).getByRole("button", { name: "Create" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Comment preview" }))
        .not.toBeInTheDocument();
    });
    expect(await screen.findByText(/Comment created for review review-1/)).toBeInTheDocument();
    const commentCall = fetchMock.mock.calls.find(
      (call) => String(call[0]) === `${BASE}/api/v1/workspaces/workspace-1/context/pages/p1/comments`,
    );
    expect(JSON.parse(String(commentCall?.[1]?.body))).toMatchObject({
      note: "Needs a source.",
      anchor: {
        anchor_text: "Hello",
        context_before: "# ",
        context_after: "",
        version_id: "v1",
        char_offset_hint: 2,
      },
    });
    await waitFor(() => {
      expect(view.hasFocus).toBe(true);
    });
  });

  it("localizes comment selection validation in Korean", async () => {
    await i18n.changeLanguage("ko");
    try {
      vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
        const url = String(input);
        if (url === `${BASE}/api/v1/workspaces`) {
          return workspaceResponse();
        }
        if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
          return jsonResponse({ data: contextTreeNodes() });
        }
        if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
          return jsonResponse({ data: [] });
        }
        if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
          return jsonResponse({ data: [page], next_cursor: null });
        }
        return jsonResponse({ data: page });
      });

      renderWithRouter(
        "/pages/hello/edit",
        <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
      );

      await waitFor(() => {
        expect(document.querySelector(".cm-content")?.textContent).toContain("# Hello");
      });
      const view = codeMirrorView();
      act(() => {
        view.dispatch({ selection: { anchor: 0 } });
        view.focus();
      });

      pressEditorShortcut(view, { key: "m", ctrlKey: true, shiftKey: true });

      expect(
        await screen.findByText("댓글을 추가하려면 먼저 텍스트를 선택하세요."),
      ).toBeInTheDocument();
      expect(screen.queryByText("Select text before adding a comment.")).toBeNull();
    } finally {
      await i18n.changeLanguage("en");
    }
  });

  it("keeps disabled AI rewrite hidden and routes Ctrl+K through the registered link command", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <>
          <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    await screen.findByLabelText("Markdown body");
    const editor = document.querySelector(".cm-content") as HTMLElement;
    await waitFor(() => {
      expect(editor.textContent).toContain("# Hello");
    });
    const view = codeMirrorView();
    mockEditorGeometry(view);
    act(() => {
      view.dispatch({ selection: { anchor: 0, head: view.state.doc.length } });
      view.focus();
    });
    await screen.findByRole("toolbar", { name: "Selection formatting toolbar" });

    fireEvent.keyDown(view.contentDOM, { key: "k", ctrlKey: true });

    expect(screen.queryByRole("dialog", { name: "AI inline edit unavailable" }))
      .not.toBeInTheDocument();
    const selectedLinkDialog = await screen.findByRole("dialog", { name: "Insert link" });
    expect(within(selectedLinkDialog).getByLabelText("Text")).toHaveValue("# Hello");
    expect(editor.textContent).not.toContain("[# Hello](url)");
    await typist().click(
      within(selectedLinkDialog).getByRole("button", { name: "Cancel link insertion" }),
    );

    act(() => {
      view.dispatch({ selection: { anchor: 0, head: 0 } });
      view.focus();
    });
    fireEvent.keyDown(view.contentDOM, { key: "k", ctrlKey: true });
    const linkDialog = await screen.findByRole("dialog", { name: "Insert link" });
    fillField(within(linkDialog).getByLabelText("Text"), "Read more");
    fillField(within(linkDialog).getByLabelText("URL"), "https://example.com/read-more");
    await typist().click(within(linkDialog).getByRole("button", { name: "Insert" }));

    await waitFor(() => {
      expect(editor.textContent).toContain("[Read more](https://example.com/read-more)");
    });
  });

  it("reclamps an open link popover inside a keyboard-reduced visual viewport", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      return jsonResponse({ data: page });
    });
    const visualViewport = Object.assign(new EventTarget(), {
      height: 844,
      offsetLeft: 0,
      offsetTop: 0,
      width: 390,
    });
    vi.stubGlobal("innerWidth", 390);
    vi.stubGlobal("innerHeight", 844);
    vi.stubGlobal("visualViewport", visualViewport);
    vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockImplementation(
      function (this: HTMLElement) {
        return this.classList.contains("editor-link-popover") ? 360 : 0;
      },
    );
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockImplementation(
      function (this: HTMLElement) {
        return this.classList.contains("editor-link-popover") ? 262 : 0;
      },
    );

    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );
    await screen.findByLabelText("Markdown body");
    const view = codeMirrorView();
    mockEditorGeometry(view);
    act(() => {
      view.dispatch({ selection: { anchor: 0, head: 0 } });
      view.focus();
    });
    fireEvent.keyDown(view.contentDOM, { key: "k", ctrlKey: true });
    const linkDialog = await screen.findByRole("dialog", { name: "Insert link" });

    visualViewport.offsetTop = 96;
    visualViewport.height = 320;
    act(() => visualViewport.dispatchEvent(new Event("resize")));

    await waitFor(() => {
      const top = Number.parseFloat(linkDialog.style.top);
      expect(top).toBeGreaterThanOrEqual(104);
      expect(top).toBeLessThanOrEqual(146);
    });
  });

  it("converts rich HTML paste into Markdown", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <>
          <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    await screen.findByLabelText("Markdown body");
    const editor = document.querySelector(".cm-content") as HTMLElement;
    fireEvent.paste(editor, {
      clipboardData: {
        files: [],
        getData: (type: string) =>
          type === "text/html"
            ? '<p><strong>Pasted</strong> <a data-internal-slug="hello" href="/pages/hello">Hello</a></p>'
            : "",
      },
    });

    await waitFor(() => {
      expect(editor.textContent).toContain("**Pasted** [[hello|Hello]]");
    });
  });

  it("uploads pasted images and replaces the editor placeholder", async () => {
    vi.spyOn(crypto, "randomUUID").mockReturnValue("00000000-0000-4000-8000-000000000123");
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/workspace-1/context/media/capabilities`
      ) {
        return jsonResponse({
          data: {
            policy_revision: "context-images-page-editor-test",
            uploadable_media_types: ["image/png", "image/jpeg"],
            max_upload_bytes: 1024 * 1024,
          },
        });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/context/pages/p1/media` && init?.method === "POST") {
        return jsonResponse({
          data: {
            asset_id: "00000000-0000-4000-8000-000000000123",
            logical_ref:
              "dtx-media://asset/00000000-0000-4000-8000-000000000123",
            content_url:
              "/api/v1/workspaces/workspace-1/context/pages/p1/media/00000000-0000-4000-8000-000000000123/content",
            mime: "image/png",
            bytes: 24,
            sha256: "a".repeat(64),
          },
        });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <>
          <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    await screen.findByLabelText("Markdown body");
    const editor = document.querySelector(".cm-content") as HTMLElement;
    fireEvent.paste(editor, {
      clipboardData: {
        files: [new File(["image"], "diagram.png", { type: "image/png" })],
        getData: () => "<p>ignored</p>",
      },
    });

    await waitFor(() => {
      expect(editor.textContent).toContain(
        "![diagram.png](dtx-media://asset/00000000-0000-4000-8000-000000000123)",
      );
    });
    expect(editor.textContent).not.toContain("Uploading diagram.png");
    const uploadCall = fetchMock.mock.calls.find(
      (call) => call[0] === `${BASE}/api/v1/workspaces/workspace-1/context/pages/p1/media`,
    );
    const uploadBody = uploadCall?.[1]?.body;
    expect(uploadBody).toBeInstanceOf(FormData);
    expect((uploadBody as FormData).get("workspace_id")).toBeNull();
    expect((uploadBody as FormData).get("page_id")).toBeNull();
    expect((uploadBody as FormData).get("marker_uuid")).toBe(
      "00000000-0000-4000-8000-000000000123",
    );
    expect(
      new Headers(uploadCall?.[1]?.headers).get("X-DTX-Media-Policy-Revision"),
    ).toBe("context-images-page-editor-test");
    expect(
      fetchMock.mock.calls.filter(
        ([input, init]) =>
          String(input) === `${BASE}/api/v1/uploads` && init?.method === "POST",
      ),
    ).toHaveLength(0);
  });

  it.each([
    {
      locale: "en",
      bodyLabel: "Markdown body",
      queueName: "Image upload queue",
      status: "Failed",
      requestFailure: "Request failed. Please try again.",
      uploadFailure:
        "Image upload failed for secret.png: Request failed. Please try again.",
    },
    {
      locale: "ko",
      bodyLabel: "Markdown 본문",
      queueName: "이미지 업로드 대기열",
      status: "실패",
      requestFailure: "요청을 처리하지 못했습니다. 다시 시도하세요.",
      uploadFailure:
        "secret.png 이미지 업로드 실패: 요청을 처리하지 못했습니다. 다시 시도하세요.",
    },
  ])("does not expose a raw generic image-upload exception in $locale", async ({
    locale,
    bodyLabel,
    queueName,
    status,
    requestFailure,
    uploadFailure,
  }) => {
    const rawSecret = "raw-upload-secret=storage-token";
    await i18n.changeLanguage(locale);
    try {
      vi.spyOn(crypto, "randomUUID").mockReturnValue(
        "00000000-0000-4000-8000-000000000124",
      );
      vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
        const url = String(input);
        if (url === `${BASE}/api/v1/workspaces`) {
          return workspaceResponse();
        }
        if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
          return jsonResponse({ data: contextTreeNodes() });
        }
        if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
          return jsonResponse({ data: [] });
        }
        if (
          url ===
          `${BASE}/api/v1/workspaces/workspace-1/context/media/capabilities`
        ) {
          return jsonResponse({
            data: {
              policy_revision: "context-images-secret-test",
              uploadable_media_types: ["image/png", "image/jpeg"],
              max_upload_bytes: 1024 * 1024,
            },
          });
        }
        if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
          return jsonResponse({ data: [page], next_cursor: null });
        }
        if (url === `${BASE}/api/v1/workspaces/workspace-1/context/pages/p1/media` && init?.method === "POST") {
          throw new Error(rawSecret);
        }
        return jsonResponse({ data: page });
      });

      renderWithRouter(
        "/pages/hello/edit",
        <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
      );

      await screen.findByLabelText(bodyLabel);
      const editor = document.querySelector(".cm-content") as HTMLElement;
      fireEvent.paste(editor, {
        clipboardData: {
          files: [new File(["image"], "secret.png", { type: "image/png" })],
          getData: () => "",
        },
      });

      const queue = await screen.findByRole("region", { name: queueName });
      expect(await within(queue).findByText(status)).toBeInTheDocument();
      expect(within(queue).getByText(requestFailure)).toBeInTheDocument();
      expect(await screen.findByText(uploadFailure)).toBeInTheDocument();
      expect(screen.queryByText(rawSecret)).toBeNull();
    } finally {
      await i18n.changeLanguage("en");
    }
  });

  it.each([
    {
      locale: "en",
      bodyLabel: "Markdown body",
      queueName: "Image upload queue",
      expected:
        "Image upload failed for second.png: Too many files were selected for one upload batch.",
    },
    {
      locale: "ko",
      bodyLabel: "Markdown 본문",
      queueName: "이미지 업로드 대기열",
      expected:
        "second.png 이미지 업로드 실패: 한 번에 업로드할 수 있는 파일 수를 초과했습니다.",
    },
  ])("localizes a batch file-count rejection in $locale", async ({
    locale,
    bodyLabel,
    queueName,
    expected,
  }) => {
    await i18n.changeLanguage(locale);
    try {
      vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
        const url = String(input);
        if (url === `${BASE}/api/v1/workspaces`) {
          return workspaceResponse();
        }
        if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
          return jsonResponse({ data: contextTreeNodes() });
        }
        if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
          return jsonResponse({ data: [] });
        }
        if (
          url ===
          `${BASE}/api/v1/workspaces/workspace-1/context/media/capabilities`
        ) {
          return jsonResponse({
            data: {
              policy_revision: "context-images-batch-rejection-test",
              uploadable_media_types: ["image/png", "image/jpeg"],
              max_upload_bytes: 1024 * 1024,
              max_files_per_batch: 1,
              max_batch_bytes: 1024 * 1024,
              max_parallel_uploads: 1,
            },
          });
        }
        if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
          return jsonResponse({ data: [page], next_cursor: null });
        }
        if (url === `${BASE}/api/v1/workspaces/workspace-1/context/pages/p1/media` && init?.method === "POST") {
          throw new Error("batch rejection must happen before upload");
        }
        return jsonResponse({ data: page });
      });

      renderWithRouter(
        "/pages/hello/edit",
        <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
      );

      await screen.findByLabelText(bodyLabel);
      fireEvent.paste(document.querySelector(".cm-content") as HTMLElement, {
        clipboardData: {
          files: [
            new File(["first"], "first.png", { type: "image/png" }),
            new File(["second"], "second.png", { type: "image/png" }),
          ],
          getData: () => "",
        },
      });

      expect(
        await screen.findByRole("region", { name: queueName }),
      ).toBeInTheDocument();
      expect(await screen.findByText(expected)).toBeInTheDocument();
      expect(document.body).not.toHaveTextContent("Choose at most 1 file(s).");
    } finally {
      await i18n.changeLanguage("en");
    }
  });

  it("uploads images from the slash image command instead of inserting a placeholder URL", async () => {
    vi.spyOn(crypto, "randomUUID").mockReturnValue("00000000-0000-4000-8000-000000000456");
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/context/media/capabilities`) {
        return currentContextMediaCapabilitiesResponse();
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/context/pages/p1/media` && init?.method === "POST") {
        return jsonResponse({
          data: {
            asset_id: "00000000-0000-4000-8000-000000000456",
            logical_ref:
              "dtx-media://asset/00000000-0000-4000-8000-000000000456",
            content_url:
              "/api/v1/workspaces/workspace-1/context/pages/p1/media/00000000-0000-4000-8000-000000000456/content",
            mime: "image/png",
            bytes: 24,
            sha256: "b".repeat(64),
          },
        });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <>
          <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    await screen.findByLabelText("Markdown body");
    const menu = await openBlockMenuFromEditor();
    fillField(within(menu).getByLabelText("Filter block commands"), "image");
    await typist().click(within(menu).getByRole("option", { name: "Image" }));

    const uploadInput = imageUploadInput();
    expect(uploadInput).toHaveAttribute("aria-hidden", "true");
    expect(uploadInput).toHaveAttribute("tabindex", "-1");
    await typist().upload(
      uploadInput,
      new File(["image"], "slash.png", { type: "image/png" }),
    );

    const editor = document.querySelector(".cm-content") as HTMLElement;
    await waitFor(() => {
      expect(editor.textContent).toContain(
        "![slash.png](dtx-media://asset/00000000-0000-4000-8000-000000000456)",
      );
    });
    expect(editor.textContent).not.toContain("image-url");
    const uploadCall = fetchMock.mock.calls.find(
      (call) => call[0] === `${BASE}/api/v1/workspaces/workspace-1/context/pages/p1/media`,
    );
    const uploadBody = uploadCall?.[1]?.body;
    expect(uploadBody).toBeInstanceOf(FormData);
    expect((uploadBody as FormData).get("workspace_id")).toBeNull();
    expect((uploadBody as FormData).get("page_id")).toBeNull();
    expect((uploadBody as FormData).get("marker_uuid")).toBe(
      "00000000-0000-4000-8000-000000000456",
    );
  });

  it("applies Context media capabilities to picker, paste, and drop before upload", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (
        url ===
        `${BASE}/api/v1/workspaces/workspace-1/context/media/capabilities`
      ) {
        return jsonResponse({
          data: {
            policy_revision: "context-images-test",
            uploadable_media_types: [
              "image/png",
              "image/jpeg",
              "image/gif",
              "image/webp",
            ],
            max_upload_bytes: 1024,
          },
        });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/context/pages/p1/media`) {
        const body = init?.body as FormData;
        const file = body.get("file") as File;
        const marker = String(body.get("marker_uuid"));
        return jsonResponse({
          data: {
            asset_id: marker,
            logical_ref: `dtx-media://asset/${marker}`,
            content_url: `/api/v1/workspaces/workspace-1/context/pages/p1/media/${marker}/content`,
            mime: file.type,
            bytes: file.size,
            sha256: "a".repeat(64),
          },
        });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    await screen.findByLabelText("Markdown body");
    const uploadInput = imageUploadInput();
    expect(uploadInput).toHaveAttribute(
      "accept",
      ".png,.jpg,.jpeg,.gif,.webp,image/png,image/jpeg,image/gif,image/webp",
    );

    const menu = await openBlockMenuFromEditor();
    fillField(within(menu).getByLabelText("Filter block commands"), "image");
    await typist().click(within(menu).getByRole("option", { name: "Image" }));
    fireEvent.change(uploadInput, {
      target: {
        files: [
          new File([new Uint8Array(1025)], "oversized.png", {
            type: "image/png",
          }),
        ],
      },
    });
    expect(await screen.findByText(/exceeds the 1 KiB upload limit/))
      .toBeInTheDocument();

    const editor = document.querySelector(".cm-content") as HTMLElement;
    fireEvent.paste(editor, {
      clipboardData: {
        files: [new File(["gif"], "animation.gif", { type: "image/gif" })],
        getData: () => "",
      },
    });
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.filter(
          ([input]) => String(input) === `${BASE}/api/v1/workspaces/workspace-1/context/pages/p1/media`,
        ),
      ).toHaveLength(1);
    });

    fireEvent.drop(editor, {
      clientX: 0,
      clientY: 0,
      dataTransfer: {
        files: [new File(["webp"], "preview.webp", { type: "image/webp" })],
      },
    });
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.filter(
          ([input]) => String(input) === `${BASE}/api/v1/workspaces/workspace-1/context/pages/p1/media`,
        ),
      ).toHaveLength(2);
    });
    expect(
      fetchMock.mock.calls
        .filter(([input]) => String(input) === `${BASE}/api/v1/workspaces/workspace-1/context/pages/p1/media`)
        .map(([, init]) => (init?.body as FormData).get("file"))
        .map((file) => file instanceof File ? [file.name, file.type] : null),
    ).toEqual([
      ["animation.gif", "image/gif"],
      ["preview.webp", "image/webp"],
    ]);

    fireEvent.drop(editor, {
      clientX: 0,
      clientY: 0,
      dataTransfer: {
        files: [new File(["svg"], "diagram.svg", { type: "image/svg+xml" })],
      },
    });
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.filter(
          ([input]) => String(input) === `${BASE}/api/v1/workspaces/workspace-1/context/pages/p1/media`,
        ),
      ).toHaveLength(2);
    });
    expect(await screen.findByText(
      /Choose an image format allowed by the current workspace upload policy/,
    )).toBeInTheDocument();
    expect(editor.textContent).not.toContain("Uploading");
  });

  it("keeps pending Context media capabilities fail-closed until policy arrives", async () => {
    let resolveCapabilities!: (response: Response) => void;
    const capabilitiesResponse = new Promise<Response>((resolve) => {
      resolveCapabilities = resolve;
    });
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) return workspaceResponse();
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/context/media/capabilities`) {
        return capabilitiesResponse;
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    expect(document.querySelector(
      'input[type="file"][aria-hidden="true"]',
    )).toBeNull();

    await act(async () => {
      resolveCapabilities(jsonResponse({
        data: {
          policy_revision: "context-images-pending-test",
          uploadable_media_types: ["image/gif", "image/webp"],
          max_upload_bytes: 2048,
        },
      }));
    });

    await screen.findByLabelText("Markdown body");
    const uploadInput = imageUploadInput();
    await waitFor(() => expect(uploadInput).toBeEnabled());
    expect(uploadInput).toHaveAttribute(
      "accept",
      ".gif,.webp,image/gif,image/webp",
    );
  });

  it("keeps the capability gate closed for mixed known and unknown media types", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) return workspaceResponse();
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/context/media/capabilities`) {
        return jsonResponse({
          data: {
            policy_revision: "context-images-schema-drift",
            uploadable_media_types: ["image/png", "image/avif"],
            max_upload_bytes: 2048,
            max_files_per_batch: 10,
            max_batch_bytes: 16_384,
            max_parallel_uploads: 2,
          },
        });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    await screen.findByLabelText("Markdown body");
    expect(imageUploadInput()).toBeDisabled();
    expect(imageUploadInput()).toHaveAttribute("accept", "");
    const editor = document.querySelector(".cm-content") as HTMLElement;
    fireEvent.paste(editor, {
      clipboardData: {
        files: [new File(["image"], "gated.png", { type: "image/png" })],
        getData: () => "",
      },
    });

    expect(await screen.findByText(
      /Current media upload limits are unavailable/,
    )).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(
      ([input]) => String(input) === `${BASE}/api/v1/workspaces/workspace-1/context/pages/p1/media`,
    )).toHaveLength(0);
    expect(editor.textContent).not.toContain("Uploading gated.png");
  });

  it("gates picker, paste, and upload requests when current capabilities fail to load", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) return workspaceResponse();
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/context/media/capabilities`) {
        return jsonResponse({
          error_code: "internal_error",
          message: "capabilities unavailable",
          request_id: "request-capabilities",
          warnings: [],
        }, 500);
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    await screen.findByLabelText("Markdown body");
    expect(imageUploadInput()).toBeDisabled();
    expect(imageUploadInput()).toHaveAttribute("accept", "");
    const editor = document.querySelector(".cm-content") as HTMLElement;
    fireEvent.paste(editor, {
      clipboardData: {
        files: [new File(["image"], "gated.png", { type: "image/png" })],
        getData: () => "",
      },
    });

    expect(await screen.findByText(
      /Current media upload limits are unavailable/,
    )).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(
      ([input]) => String(input) === `${BASE}/api/v1/workspaces/workspace-1/context/pages/p1/media`,
    )).toHaveLength(0);
    expect(editor.textContent).not.toContain("Uploading gated.png");
  });

  it("refreshes and atomically replays a stale-policy batch with the same operation", async () => {
    const marker = "00000000-0000-4000-8000-000000000831";
    vi.spyOn(crypto, "randomUUID").mockReturnValue(marker);
    let capabilityRequests = 0;
    let uploadRequests = 0;
    let resolvePolicyRefresh!: (response: Response) => void;
    const policyRefreshResponse = new Promise<Response>((resolve) => {
      resolvePolicyRefresh = resolve;
    });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input, init) => {
        const url = String(input);
        if (url === `${BASE}/api/v1/workspaces`) return workspaceResponse();
        if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
          return jsonResponse({ data: contextTreeNodes() });
        }
        if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
          return jsonResponse({ data: [] });
        }
        if (url === `${BASE}/api/v1/workspaces/workspace-1/context/media/capabilities`) {
          capabilityRequests += 1;
          if (capabilityRequests === 1) {
            return jsonResponse({ data: {
              policy_revision: "policy-v1",
              uploadable_media_types: ["image/png"],
              max_upload_bytes: 100,
              max_files_per_batch: 1,
              max_batch_bytes: 100,
              max_parallel_uploads: 1,
            } });
          }
          return policyRefreshResponse;
        }
        if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
          return jsonResponse({ data: [page], next_cursor: null });
        }
        if (url === `${BASE}/api/v1/workspaces/workspace-1/context/pages/p1/media` && init?.method === "POST") {
          uploadRequests += 1;
          if (uploadRequests === 1) {
            return jsonResponse({
              error_code: "media_policy_changed",
              message: "Refresh upload policy.",
              request_id: "request-policy",
              warnings: [],
            }, 409);
          }
          return jsonResponse({ data: {
            asset_id: marker,
            logical_ref: `dtx-media://asset/${marker}`,
            content_url: `/api/v1/workspaces/workspace-1/context/pages/p1/media/${marker}/content`,
            mime: "image/png",
            bytes: 5,
            sha256: "e".repeat(64),
          } });
        }
        return jsonResponse({ data: page });
      },
    );

    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );
    await screen.findByLabelText("Markdown body");
    const editor = document.querySelector(".cm-content") as HTMLElement;
    fireEvent.paste(editor, {
      clipboardData: {
        files: [new File(["image"], "policy.png", { type: "image/png" })],
        getData: () => "",
      },
    });

    const queue = await screen.findByRole("region", { name: "Image upload queue" });
    expect(await within(queue).findByText(
      "Upload policy changed. Refreshing limits and checking the original batch.",
    )).toBeInTheDocument();
    expect(within(queue).queryByRole("button", {
      name: "Retry upload for policy.png",
    })).toBeNull();

    resolvePolicyRefresh(jsonResponse({ data: {
      policy_revision: "policy-v2",
      uploadable_media_types: ["image/png"],
      max_upload_bytes: 100,
      max_files_per_batch: 1,
      max_batch_bytes: 100,
      max_parallel_uploads: 1,
    } }));

    await waitFor(() => {
      expect(editor.textContent).toContain(`![policy.png](dtx-media://asset/${marker})`);
    });
    expect(capabilityRequests).toBe(2);
    expect(uploadRequests).toBe(2);
    const uploadCalls = fetchMock.mock.calls.filter(
      ([input]) => String(input) === `${BASE}/api/v1/workspaces/workspace-1/context/pages/p1/media`,
    );
    expect(uploadCalls.map(([, init]) =>
      new Headers(init?.headers).get("X-DTX-Media-Policy-Revision")))
      .toEqual(["policy-v1", "policy-v2"]);
    expect(uploadCalls.map(([, init]) =>
      (init?.body as FormData).get("marker_uuid")))
      .toEqual([marker, marker]);
  });

  it("blocks all stale-policy replays when fresh limits reject the original batch", async () => {
    const markers = [
      "00000000-0000-4000-8000-000000000841",
      "00000000-0000-4000-8000-000000000842",
    ] as const;
    let markerIndex = 0;
    vi.spyOn(crypto, "randomUUID").mockImplementation(
      () => markers[Math.min(markerIndex++, markers.length - 1)],
    );
    let capabilityRequests = 0;
    let uploadRequests = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) return workspaceResponse();
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/context/media/capabilities`) {
        capabilityRequests += 1;
        return jsonResponse({ data: {
          policy_revision: capabilityRequests === 1 ? "policy-v1" : "policy-v2",
          uploadable_media_types: ["image/png"],
          max_upload_bytes: 100,
          max_files_per_batch: capabilityRequests === 1 ? 2 : 1,
          max_batch_bytes: 100,
          max_parallel_uploads: 2,
        } });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/context/pages/p1/media` && init?.method === "POST") {
        uploadRequests += 1;
        return jsonResponse({
          error_code: "media_policy_changed",
          message: "Refresh upload policy.",
          request_id: `request-policy-${uploadRequests}`,
          warnings: [],
        }, 409);
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );
    await screen.findByLabelText("Markdown body");
    const editor = document.querySelector(".cm-content") as HTMLElement;
    fireEvent.paste(editor, {
      clipboardData: {
        files: [
          new File(["a"], "a.png", { type: "image/png" }),
          new File(["b"], "b.png", { type: "image/png" }),
        ],
        getData: () => "",
      },
    });

    const queue = await screen.findByRole("region", { name: "Image upload queue" });
    expect(await within(queue).findByText(
      "The original batch no longer meets current limits. Remove unresolved items and choose the batch again.",
    )).toBeInTheDocument();
    expect(uploadRequests).toBe(2);
    expect(capabilityRequests).toBe(2);
    expect(within(queue).queryByRole("button", {
      name: "Retry upload for a.png",
    })).toBeNull();
    expect(within(queue).queryByRole("button", {
      name: "Retry upload for b.png",
    })).toBeNull();
    expect(within(queue).getByRole("button", {
      name: "Remove upload for a.png",
    })).toBeInTheDocument();
    expect(within(queue).getByRole("button", {
      name: "Remove upload for b.png",
    })).toBeInTheDocument();
  });

  it("cancels and retries an image upload from the accessible queue", async () => {
    vi.spyOn(crypto, "randomUUID").mockReturnValue(
      "00000000-0000-4000-8000-000000000789",
    );
    let uploadAttempts = 0;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input, init) => {
        const url = String(input);
        if (url === `${BASE}/api/v1/workspaces`) {
          return workspaceResponse();
        }
        if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
          return jsonResponse({ data: contextTreeNodes() });
        }
        if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
          return jsonResponse({ data: [] });
        }
        if (url === `${BASE}/api/v1/workspaces/workspace-1/context/media/capabilities`) {
          return currentContextMediaCapabilitiesResponse();
        }
        if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
          return jsonResponse({ data: [page], next_cursor: null });
        }
        if (url === `${BASE}/api/v1/workspaces/workspace-1/context/pages/p1/media` && init?.method === "POST") {
          uploadAttempts += 1;
          if (uploadAttempts === 1) {
            return new Promise<Response>((_resolve, reject) => {
              const rejectAbort = () => reject(
                new DOMException("The operation was aborted.", "AbortError"),
              );
              if (init.signal?.aborted) {
                rejectAbort();
                return;
              }
              init.signal?.addEventListener("abort", rejectAbort, { once: true });
            });
          }
          return jsonResponse({
            data: {
              asset_id: "00000000-0000-4000-8000-000000000789",
              logical_ref:
                "dtx-media://asset/00000000-0000-4000-8000-000000000789",
              content_url:
                "/api/v1/workspaces/workspace-1/context/pages/p1/media/00000000-0000-4000-8000-000000000789/content",
              mime: "image/png",
              bytes: 5,
              sha256: "d".repeat(64),
            },
          });
        }
        return jsonResponse({ data: page });
      },
    );

    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    await screen.findByLabelText("Markdown body");
    const editor = document.querySelector(".cm-content") as HTMLElement;
    fireEvent.paste(editor, {
      clipboardData: {
        files: [new File(["image"], "retry.png", { type: "image/png" })],
        getData: () => "",
      },
    });

    const queue = await screen.findByRole("region", { name: "Image upload queue" });
    // The region appears as soon as either its entries or its batches have an
    // item, and an entry's status and the aggregate summary come from separate
    // state, so either can reach the page before the other.
    expect(await within(queue).findByText("Uploading")).toBeInTheDocument();
    expect(await within(queue).findByText(/0 of 1 files finished \(5 B total\).*indeterminate/))
      .toBeInTheDocument();
    expect(within(queue).getByRole("progressbar", {
      name: "Aggregate upload progress",
    })).not.toHaveAttribute("value");
    await typist().click(
      within(queue).getByRole("button", { name: "Cancel upload for retry.png" }),
    );

    expect(await within(queue).findByText("Outcome unknown")).toBeInTheDocument();
    expect(within(queue).getByText(
      "Replay each outcome-unknown upload or remove it before saving.",
    )).toBeInTheDocument();
    expect(editor.textContent).not.toMatch(/Uploading retry\.png|\u200b|<!--/);
    expect(publicationActions().getByRole("button", { name: "Save draft" })).toHaveAttribute(
      "title",
      "Retry or remove every unresolved image upload before saving.",
    );
    expect(within(queue).queryByRole("button", { name: "Retry failed" }))
      .toBeNull();
    const retry = within(queue).getByRole("button", {
      name: "Retry upload for retry.png",
    });
    await waitFor(() => expect(retry).toBeEnabled());
    await typist().click(retry);

    await waitFor(() => {
      expect(editor.textContent).toContain(
        "![retry.png](dtx-media://asset/00000000-0000-4000-8000-000000000789)",
      );
    });
    expect(await within(queue).findByText("Succeeded")).toBeInTheDocument();
    expect(screen.queryByText(/Image upload failed for retry.png/)).toBeNull();
    expect(
      fetchMock.mock.calls.filter(
        ([input]) => String(input) === `${BASE}/api/v1/workspaces/workspace-1/context/pages/p1/media`,
      ),
    ).toHaveLength(2);
  });

  it("aborts and fences a late image upload when the editor route changes", async () => {
    const assetId = "00000000-0000-4000-8000-000000000790";
    vi.spyOn(crypto, "randomUUID").mockReturnValue(assetId);
    let uploadSignal!: AbortSignal;
    let resolveUpload!: (response: Response) => void;
    const uploadResponse = new Promise<Response>((resolve) => {
      resolveUpload = resolve;
    });
    const otherPage = {
      ...page,
      id: "p2",
      slug: "other",
      title: "Other",
      body_md: "# Other",
      content_hash: "b".repeat(64),
      current_version_id: "v-other-1",
      version_id: "v-other-1",
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input, init) => {
        const url = String(input);
        if (url === `${BASE}/api/v1/workspaces`) return workspaceResponse();
        if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
          return jsonResponse({ data: contextTreeNodes() });
        }
        if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
          return jsonResponse({ data: [] });
        }
        if (url === `${BASE}/api/v1/workspaces/workspace-1/context/media/capabilities`) {
          return currentContextMediaCapabilitiesResponse();
        }
        if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
          return jsonResponse({ data: [page, otherPage], next_cursor: null });
        }
        if (url.includes("/api/v1/workspaces/workspace-1/context/pages/by-slug/hello")) {
          return jsonResponse({ data: page });
        }
        if (url.includes("/api/v1/workspaces/workspace-1/context/pages/by-slug/other")) {
          return jsonResponse({ data: otherPage });
        }
        if (url === `${BASE}/api/v1/workspaces/workspace-1/context/pages/p1/media` && init?.method === "POST") {
          uploadSignal = init.signal as AbortSignal;
          return uploadResponse;
        }
        return jsonResponse({ data: page });
      },
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);

    renderWithRouter(
      "/pages/hello/edit",
      <Route
        path="/pages/:slug/edit"
        element={(
          <>
            <PageEditor mode="edit" />
            <EditOtherButton />
          </>
        )}
      />,
    );

    await screen.findByLabelText("Markdown body");
    const editor = document.querySelector(".cm-content") as HTMLElement;
    fireEvent.paste(editor, {
      clipboardData: {
        files: [new File(["image"], "route.png", { type: "image/png" })],
        getData: () => "",
      },
    });
    await screen.findByText("Uploading");
    await waitFor(() => expect(uploadSignal).toBeDefined());

    await typist().click(screen.getByRole("button", { name: "Edit other page" }));
    await expectEditorTitle("Other");
    await waitFor(() => expect(uploadSignal.aborted).toBe(true));

    await act(async () => {
      resolveUpload(jsonResponse({
        data: {
          asset_id: assetId,
          logical_ref: `dtx-media://asset/${assetId}`,
          content_url: `/api/v1/workspaces/workspace-1/context/pages/p1/media/${assetId}/content`,
          mime: "image/png",
          bytes: 5,
          sha256: "e".repeat(64),
        },
      }));
      await Promise.resolve();
      await Promise.resolve();
    });

    await waitFor(() => {
      const currentEditor = document.querySelector(".cm-content") as HTMLElement;
      expect(currentEditor.textContent).toContain("# Other");
      expect(currentEditor.textContent).not.toContain(assetId);
      expect(currentEditor.textContent).not.toContain("Uploading route.png");
      expect(screen.queryByRole("region", { name: "Image upload queue" })).toBeNull();
      expect(screen.queryByText(/Image upload failed for route.png/)).toBeNull();
    });
    expect(
      fetchMock.mock.calls.filter(
        ([input]) => String(input) === `${BASE}/api/v1/workspaces/workspace-1/context/pages/p1/media`,
      ),
    ).toHaveLength(1);
  });

  it("settles cancellation while a new-page creation is still loading", async () => {
    let resolvePageCreation!: (response: Response) => void;
    const pageCreationResponse = new Promise<Response>((resolve) => {
      resolvePageCreation = resolve;
    });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input, init) => {
        const url = String(input);
        if (url === `${BASE}/api/v1/workspaces`) {
          return workspaceResponse();
        }
        if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
          return jsonResponse({ data: contextTreeNodes() });
        }
        if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
          return jsonResponse({ data: [] });
        }
        if (url === `${BASE}/api/v1/workspaces/workspace-1/context/media/capabilities`) {
          return currentContextMediaCapabilitiesResponse();
        }
        if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
          return jsonResponse({ data: [], next_cursor: null });
        }
        if (url === `${BASE}/api/v1/workspaces/workspace-1/context/pages` && init?.method === "POST") {
          return pageCreationResponse;
        }
        if (url === `${BASE}/api/v1/workspaces/workspace-1/context/pages/new-page-1/media`) {
          throw new Error("cancelled upload must not reach the upload endpoint");
        }
        return jsonResponse({ data: page });
      },
    );

    renderWithRouter(
      "/pages/new",
      <Route path="/pages/new" element={<PageEditor mode="create" />} />,
    );

    await screen.findByLabelText("Markdown body");
    const editor = document.querySelector(".cm-content") as HTMLElement;
    fireEvent.paste(editor, {
      clipboardData: {
        files: [new File(["image"], "session.png", { type: "image/png" })],
        getData: () => "",
      },
    });

    const queue = await screen.findByRole("region", { name: "Image upload queue" });
    await typist().click(
      within(queue).getByRole("button", { name: "Cancel upload for session.png" }),
    );
    expect(await within(queue).findByText("Cancelled")).toBeInTheDocument();
    await waitFor(() => {
      expect(
        within(queue).getByRole("button", { name: "Retry upload for session.png" }),
      ).toBeEnabled();
    });
    expect(editor.textContent).not.toMatch(/Uploading session\.png|\u200b|<!--/);
    await typist().click(
      within(queue).getByRole("button", { name: "Remove upload for session.png" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("region", { name: "Image upload queue" })).toBeNull();
    });
    expect(editor.textContent).not.toContain("Uploading session.png");
    expect(
      fetchMock.mock.calls.filter(
        ([input]) => String(input) === `${BASE}/api/v1/workspaces/workspace-1/context/pages/new-page-1/media`,
      ),
    ).toHaveLength(0);

    resolvePageCreation(jsonResponse({
      data: {
        ...page,
        id: "new-page-1",
        slug: "p-new-page-1",
        version_id: "version-1",
        current_version_id: "version-1",
      },
    }, 201));
    await Promise.resolve();
  });

  it("eagerly creates the page on first paste and targets its editing version", async () => {
    const assetId = "123e4567-e89b-12d3-a456-426614174000";
    vi.spyOn(crypto, "randomUUID").mockReturnValue(assetId);
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/context/media/capabilities`) {
        return currentContextMediaCapabilitiesResponse();
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages/tags?")) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [], next_cursor: null });
      }
      // Page-first creation: no slug in the request body.
      if (url === `${BASE}/api/v1/workspaces/workspace-1/context/pages` && init?.method === "POST") {
        return jsonResponse({
          data: {
            ...page,
            id: "new-page-1",
            slug: "media-brief",
            title: "Media Brief",
            version_id: "version-1",
            current_version_id: "version-1",
          },
        }, 201);
      }
      if (
        url === `${BASE}/api/v1/workspaces/workspace-1/context/pages/new-page-1/media` &&
        init?.method === "POST"
      ) {
        return jsonResponse({
          data: {
            mime: "image/png",
            bytes: 24,
            sha256: "a".repeat(64),
            asset_id: assetId,
            logical_ref: `dtx-media://asset/${assetId}`,
            content_url:
              `/api/v1/workspaces/workspace-1/context/pages/new-page-1/versions/version-1/media/${assetId}/content`,
          },
        });
      }
      if (
        url === `${BASE}/api/v1/workspaces/workspace-1/context/pages/new-page-1/autosave` &&
        init?.method === "POST"
      ) {
        return jsonResponse({
          data: {
            ...page,
            id: "new-page-1",
            slug: "media-brief",
            title: "Media Brief",
            version_id: "version-1",
            current_version_id: "version-1",
          },
        });
      }
      return jsonResponse({ data: page });
    });

    const router = renderWithRouter(
      "/pages/new?title=Media%20Brief",
      <>
        <Route path="/pages/new" element={<PageEditor mode="create" />} />
        <Route
          path="/context/:slug/edit"
          element={<div>editing persisted page</div>}
        />
      </>,
    );

    await screen.findByLabelText("Markdown body");
    const editor = document.querySelector(".cm-content") as HTMLElement;
    fireEvent.paste(editor, {
      clipboardData: {
        files: [new File(["image"], "diagram.png", { type: "image/png" })],
        getData: () => "",
      },
    });

    await waitFor(() => {
      expect(editor.textContent).toContain(
        `![diagram.png](dtx-media://asset/${assetId})`,
      );
    });
    // The first paste eagerly created the page exactly once -- a race between
    // several pasted images racing into this same call is covered elsewhere;
    // this asserts the single-flight case doesn't double-create.
    expect(
      fetchMock.mock.calls.filter(
        ([input, callInit]) =>
          String(input) === `${BASE}/api/v1/workspaces/workspace-1/context/pages` && callInit?.method === "POST",
      ),
    ).toHaveLength(1);
    const uploadCall = fetchMock.mock.calls.find(
      (call) => String(call[0]) === `${BASE}/api/v1/workspaces/workspace-1/context/pages/new-page-1/media`,
    );
    expect((uploadCall?.[1]?.body as FormData).get("workspace_id")).toBeNull();
    expect((uploadCall?.[1]?.body as FormData).get("editing_version_id")).toBe(
      "version-1",
    );
    expect((uploadCall?.[1]?.body as FormData).get("marker_uuid")).toBe(assetId);
    expect(
      fetchMock.mock.calls.filter(
        ([input, init]) =>
          String(input) === `${BASE}/api/v1/uploads` && init?.method === "POST",
      ),
    ).toHaveLength(0);

    // Save draft is refused while the pasted image's upload is still settling.
    await waitFor(() => {
      const saveDraft = publicationActions().getByRole("button", { name: "Save draft" });
      expect(saveDraft).toBeEnabled();
      expect(saveDraft).not.toHaveAttribute("aria-disabled");
    });
    await typist().click(publicationActions().getByRole("button", { name: "Save draft" }));
    const autosaveCall = await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (candidate) =>
          String(candidate[0]) === `${BASE}/api/v1/workspaces/workspace-1/context/pages/new-page-1/autosave` &&
          candidate[1]?.method === "POST",
      );
      expect(call).toBeTruthy();
      return call;
    });
    expect(JSON.parse(String(autosaveCall?.[1]?.body))).toMatchObject({
      body_md: expect.stringContaining(`dtx-media://asset/${assetId}`),
    });
    // The page already existed (eagerly created on paste), so "Save draft"
    // updates it in place instead of creating a second page. The explicit
    // save still leaves the transient /new route for the durable edit URL.
    expect(
      fetchMock.mock.calls.filter(
        ([input, callInit]) =>
          String(input) === `${BASE}/api/v1/workspaces/workspace-1/context/pages` && callInit?.method === "POST",
      ),
    ).toHaveLength(1);
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/context/media-brief/edit");
    });
    expect(await screen.findByText("editing persisted page")).toBeInTheDocument();
  });

  it("opens the page media manager for a persisted page", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      if (url.endsWith("/api/v1/workspaces/workspace-1/context/pages/p1/media?usage=used")) {
        return jsonResponse({ data: [] });
      }
      if (url.endsWith("/api/v1/workspaces/workspace-1/context/pages/p1/media?usage=unused")) {
        return jsonResponse({ data: [] });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
    );

    await screen.findByLabelText("Markdown body");
    await typist().click(screen.getByRole("button", { name: "Manage page media" }));

    const dialog = await screen.findByRole("dialog", { name: "Page media" });
    expect(await within(dialog).findByText("No unused media.")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some((call) => String(call[0]).includes("usage=used")),
    ).toBe(true);
    await typist().click(within(dialog).getByRole("button", { name: "Close page media" }));
    expect(screen.queryByRole("dialog", { name: "Page media" })).not.toBeInTheDocument();
  });

  it("always opens new pages in source mode even when reading mode was stored", async () => {
    window.localStorage.setItem(
      "dtxwiki:editor:default:new",
      JSON.stringify({ mode: "reading", split: true }),
    );
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [], next_cursor: null });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/new",
      <>
          <Route path="/pages/new" element={<PageEditor mode="create" />} />
      </>,
    );

    expect(await screen.findByLabelText("Markdown body")).toBeInTheDocument();
    expect(document.querySelector(".preview-pane")).not.toBeInTheDocument();
  });

  it("migrates legacy split preview preference into split mode", async () => {
    const preferenceKey = "dtxwiki:editor:default:hello";
    window.localStorage.setItem(
      preferenceKey,
      JSON.stringify({ mode: "source", split: true }),
    );
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/hello/edit",
      <>
        <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    expect(await screen.findByLabelText("Markdown body")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Split" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(document.querySelector(".preview-pane")).toBeInTheDocument();
    await waitFor(() => {
      expect(JSON.parse(window.localStorage.getItem(preferenceKey) ?? "{}")).toEqual({
        mode: "split",
        splitPercent: 50,
      });
    });
  });

  it("does not double-submit while a save request is in flight", async () => {
    let resolveCreate!: (response: Response) => void;
    const createPromise = new Promise<Response>((resolve) => {
      resolveCreate = resolve;
    });
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input, init) => {
        const url = String(input);
        if (url === `${BASE}/api/v1/workspaces`) {
          return workspaceResponse();
        }
        if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
          return jsonResponse({ data: contextTreeNodes() });
        }
        if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
          return jsonResponse({ data: [], next_cursor: null });
        }
        if (init?.method === "POST" && url === `${BASE}/api/v1/workspaces/workspace-1/context/pages`) {
          return createPromise;
        }
        if (init?.method === "POST" && url.endsWith("/publish")) {
          return jsonResponse({
            data: { ...page, slug: "release-notes", title: "Release notes" },
          });
        }
        return jsonResponse({ data: page });
      });

    renderWithRouter(
      "/pages/new",
      <>
          <Route path="/pages/new" element={<PageEditor mode="create" />} />
          <Route path="/pages/:slug/edit" element={<div>editing created page</div>} />
          <Route path="/context/:slug/edit" element={<PageEditor mode="edit" />} />
      </>,
    );

    await setEditorTitle(await findTitleEditor(), "Release notes");

    const saveButton = await publicationActions().findByRole("button", { name: "Save draft" });
    fireEvent.click(saveButton);
    fireEvent.click(saveButton);
    fireEvent.click(saveButton);

    const createCalls = fetchMock.mock.calls.filter(
      (call) => call[1]?.method === "POST",
    );
    expect(createCalls).toHaveLength(1);
    expect(saveButton).toBeDisabled();
    expect(syncStatus()).toHaveTextContent("Saving…");
    await openPageProperties();
    await typist().click(pageProperties().getByRole("button", { name: "Advanced settings" }));
    expect(screen.getByLabelText("Slug")).toBeDisabled();
    expect(screen.getByLabelText("Destination")).toBeDisabled();
    const unloadEvent = new Event("beforeunload", { cancelable: true });
    expect(window.dispatchEvent(unloadEvent)).toBe(false);

    resolveCreate(
      jsonResponse(
        { data: { ...page, slug: "release-notes", title: "Release notes" } },
        201,
      ),
    );
    expect(await screen.findByLabelText("Context tree")).toBeInTheDocument();
  });

  it("preserves explicit title input and rejects controls or more than 256 code points", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages/tags?")) {
        return jsonResponse({ data: [] });
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [], next_cursor: null });
      }
      return jsonResponse({ data: page });
    });

    renderWithRouter(
      "/pages/new",
      <>
        <Route path="/pages/new" element={<PageEditor mode="create" />} />
      </>,
    );

    const titleInput = await findTitleEditor();
    expect(titleInput).not.toHaveAttribute("maxlength");

    const multiline = "Line one\nLine two\n\tLine three";
    await setEditorTitle(titleInput, multiline);
    fireEvent.input(titleInput);
    expect(titleInput).toHaveValue(multiline);
    expect(titleInput).toBeInvalid();

    fireEvent.change(titleInput, { target: { value: "😀".repeat(257) } });
    fireEvent.input(titleInput);
    expect(titleInput).toHaveValue("😀".repeat(257));
    expect(titleInput).toBeInvalid();
    fireEvent.change(titleInput, { target: { value: "😀".repeat(256) } });
    fireEvent.input(titleInput);
    expect(titleInput).toBeValid();
  });

  it("percent-encodes link dialog URLs and rejects unsafe schemes", async () => {
    await renderEditorOnHello();
    const editor = document.querySelector(".cm-content") as HTMLElement;
    await waitFor(() => {
      expect(editor.textContent).toContain("# Hello");
    });
    const view = codeMirrorView();
    mockEditorGeometry(view);

    // Ctrl+K with the caret at the start of the document.
    async function openLinkDialog() {
      act(() => {
        view.dispatch({ selection: { anchor: 0, head: 0 } });
        view.focus();
      });
      fireEvent.keyDown(view.contentDOM, { key: "k", ctrlKey: true });
      return screen.findByRole("dialog", { name: "Insert link" });
    }

    const linkDialog = await openLinkDialog();
    fillField(within(linkDialog).getByLabelText("Text"), "Wiki");
    fillField(
      within(linkDialog).getByLabelText("URL"),
      "https://en.wikipedia.org/wiki/Foo (disambiguation)",
    );
    await typist().click(within(linkDialog).getByRole("button", { name: "Insert" }));

    await waitFor(() => {
      expect(editor.textContent).toContain(
        "[Wiki](https://en.wikipedia.org/wiki/Foo%20%28disambiguation%29)",
      );
    });

    // Open the dialog again in the same editor. fillField replaces whatever a
    // field holds, so a value left over from the first insertion would go
    // unseen unless both fields are checked to start empty.
    const reopenedDialog = await openLinkDialog();
    expect(within(reopenedDialog).getByLabelText("Text")).toHaveValue("");
    expect(within(reopenedDialog).getByLabelText("URL")).toHaveValue("");
    fillField(within(reopenedDialog).getByLabelText("Text"), "Nope");
    fillField(within(reopenedDialog).getByLabelText("URL"), "javascript:alert(1)");

    expect(within(reopenedDialog).getByRole("button", { name: "Insert" })).toBeDisabled();
    fireEvent.submit(reopenedDialog);
    expect(editor.textContent).not.toContain("javascript:");
  });

  it("blocks autosave and the unload beacon while an image upload is pending", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const assetId = "00000000-0000-4000-8000-000000000999";
    vi.spyOn(crypto, "randomUUID").mockReturnValue(assetId);
    let resolveUpload!: (response: Response) => void;
    const uploadResponse = new Promise<Response>((resolve) => {
      resolveUpload = resolve;
    });
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === `${BASE}/api/v1/workspaces`) {
        return workspaceResponse();
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
        return jsonResponse({ data: contextTreeNodes() });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
        return jsonResponse({ data: [] });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/context/media/capabilities`) {
        return currentContextMediaCapabilitiesResponse();
      }
      if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
        return jsonResponse({ data: [page], next_cursor: null });
      }
      if (url === `${BASE}/api/v1/workspaces/workspace-1/context/pages/p1/media` && init?.method === "POST") {
        return uploadResponse;
      }
      if (init?.method === "POST" && url.endsWith("/api/v1/workspaces/workspace-1/context/pages/p1/autosave")) {
        return jsonResponse({
          data: {
            ...page,
            version_id: "v2",
            current_version_id: "v2",
            content_hash: "b".repeat(64),
            updated_at: new Date().toISOString(),
          },
        });
      }
      return jsonResponse({ data: page });
    });
    const sendBeacon = vi.fn<(url: string, data?: BodyInit | null) => boolean>(() => true);
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    Object.defineProperty(window.navigator, "sendBeacon", {
      configurable: true,
      value: sendBeacon,
    });

    renderWithRouter(
      "/pages/hello/edit",
      <>
        <Route
          path="/pages/:slug/edit"
          element={(
            <>
              <PageEditor mode="edit" />
              <ProgrammaticNavigationButton />
            </>
          )}
        />
        <Route path="/dashboard" element={<div>dashboard</div>} />
      </>,
    );

    await screen.findByLabelText("Markdown body");
    const editor = document.querySelector(".cm-content") as HTMLElement;
    await waitFor(() => {
      expect(editor.textContent).toContain("# Hello");
    });
    fireEvent.paste(editor, {
      clipboardData: {
        files: [new File(["image"], "diagram.png", { type: "image/png" })],
        getData: () => "",
      },
    });
    await screen.findByRole("region", { name: "Image upload queue" });
    expect(codeMirrorView().state.doc.toString()).not.toMatch(/Uploading|\u200b|<!--/);

    const uploadOnlyUnload = new Event("beforeunload", { cancelable: true });
    expect(window.dispatchEvent(uploadOnlyUnload)).toBe(false);
    expect(sendBeacon).not.toHaveBeenCalled();
    await typist().click(screen.getByRole("button", { name: "Programmatic nav" }));
    expect(confirmSpy).toHaveBeenCalledWith(
      "Leave this editor? Unresolved uploads and unsaved edits cannot be recovered and will be lost.",
    );
    expect(screen.queryByText("dashboard")).not.toBeInTheDocument();

    await advanceAutosaveCheckpoint();
    expect(
      fetchMock.mock.calls.filter(
        (call) =>
          call[1]?.method === "POST" &&
          String(call[0]).endsWith("/api/v1/workspaces/workspace-1/context/pages/p1/autosave"),
      ),
    ).toHaveLength(0);

    const pendingBody = codeMirrorView().state.doc.toString();
    replaceEditorSelection(codeMirrorView(), "# Hello\n\nBody changed while upload is pending");
    expect(editor.textContent).not.toContain("Uploading diagram.png");
    await advanceAutosaveCheckpoint();
    expect(
      fetchMock.mock.calls.filter(
        (call) =>
          call[1]?.method === "POST" &&
          String(call[0]).endsWith("/api/v1/workspaces/workspace-1/context/pages/p1/autosave"),
      ),
    ).toHaveLength(0);
    replaceEditorSelection(codeMirrorView(), pendingBody);

    const saveButton = publicationActions().getByRole("button", { name: "Save draft" });
    expect(saveButton).toHaveAttribute("aria-disabled", "true");
    expect(saveButton).toHaveAttribute(
      "title",
      "Retry or remove every unresolved image upload before saving.",
    );

    const unloadEvent = new Event("beforeunload", { cancelable: true });
    expect(window.dispatchEvent(unloadEvent)).toBe(false);
    expect(sendBeacon).not.toHaveBeenCalled();

    resolveUpload(jsonResponse({
      data: {
        asset_id: assetId,
        logical_ref: `dtx-media://asset/${assetId}`,
        content_url: `/api/v1/workspaces/workspace-1/context/pages/p1/media/${assetId}/content`,
        mime: "image/png",
        bytes: 24,
        sha256: "a".repeat(64),
      },
    }));
    await waitFor(() => {
      expect(editor.textContent).toContain(
        `![diagram.png](dtx-media://asset/${assetId})`,
      );
    });
    await waitFor(() => {
      expect(publicationActions().getByRole("button", { name: "Save draft" })).not.toHaveAttribute(
        "title",
      );
    });

    await advanceAutosaveCheckpoint();
    await waitFor(() => {
      const autosaveCall = fetchMock.mock.calls.find(
        (call) =>
          call[1]?.method === "POST" &&
          String(call[0]).endsWith("/api/v1/workspaces/workspace-1/context/pages/p1/autosave"),
      );
      expect(autosaveCall).toBeTruthy();
      const autosaveBody = JSON.parse(String(autosaveCall?.[1]?.body));
      expect(autosaveBody.body_md).toContain(
        `![diagram.png](dtx-media://asset/${assetId})`,
      );
      expect(autosaveBody.body_md).not.toContain("Uploading diagram.png");
    });
  });
});

function ProgrammaticNavigationButton() {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate("/dashboard")}>
      Programmatic nav
    </button>
  );
}

function EditOtherButton() {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate("/pages/other/edit")}>
      Edit other page
    </button>
  );
}

// One keyboard session for the whole file. `delay: null` drops the timer wait
// user-event inserts between keystrokes: every key still dispatches the real
// keydown/input/keyup sequence, which is why the tests whose subject is typing
// (or the focus a typed-into field keeps) still type, but they no longer pay a
// tick per character.
//
// Every other test only needs a field to hold a value, and typing it costs one
// render of the whole editor per character -- plus a CodeMirror reconfigure
// each, see codeMirrorStyleRegistry.ts -- so those use setEditorTitle,
// commitChip and fillField, which deliver the value in a single change event.
let keyboard: ReturnType<typeof userEvent.setup> | null = null;

function typist() {
  if (keyboard === null) {
    keyboard = userEvent.setup({ delay: null });
  }
  return keyboard;
}

// Fixture edit: the title becomes `value` in one change event. The field is
// focused first, as typing into it would have left it.
async function setEditorTitle(element: HTMLElement, value: string) {
  act(() => {
    element.focus();
  });
  fireEvent.change(element, { target: { value } });
}

// Fixture edit for a plain field: `value` arrives in one change event.
function fillField(element: HTMLElement, value: string) {
  fireEvent.change(element, { target: { value } });
}

// Fixture edit for a chip input: the text arrives in one change event and
// Enter commits it, as the alias test above does.
function commitChip(element: HTMLElement, value: string) {
  fillField(element, value);
  fireEvent.keyDown(element, { key: "Enter" });
}

async function findTitleEditor() {
  return screen.findByTestId("page-title-editor");
}

function getTitleEditor() {
  return screen.getByTestId("page-title-editor");
}

async function openBlockMenuFromEditor() {
  pressEditorShortcut(codeMirrorView(), { key: "/", ctrlKey: true });
  return screen.findByRole("dialog", { name: "Insert block" });
}

// A new page opened from the Context rail at desktop width, with a page branch
// in the Context tree and the geometry stubbed so the Page properties popover
// and the portaled Destination picker can position themselves. Returns the
// fetch mock so a test can assert that nothing was written.
async function renderDestinationPickerEditor() {
  vi.stubGlobal("innerWidth", 1440);
  vi.stubGlobal("innerHeight", 900);
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
    function getMockRect(this: HTMLElement) {
      if (this === document.documentElement || this === document.body) {
        return domRect({ height: 900, left: 0, top: 0, width: 1440 });
      }
      if (this.classList.contains("editor-properties-button")) {
        return domRect({ height: 28, left: 1260, top: 104, width: 32 });
      }
      if (this.classList.contains("editor-properties-popover")) {
        return domRect({ height: 300, left: 0, top: 0, width: 340 });
      }
      if (this.classList.contains("tree-picker-trigger")) {
        return domRect({ height: 38, left: 980, top: 180, width: 260 });
      }
      return domRect({ height: 0, left: 0, top: 0, width: 0 });
    },
  );
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    if (url === `${BASE}/api/v1/workspaces`) {
      return workspaceResponse();
    }
    if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
      return jsonResponse({ data: contextTreeNodesWithPageBranch() });
    }
    if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
      return jsonResponse({ data: [] });
    }
    // P9.1 deleted the top-level /api/v1/pages* family; the audit branch
    // this test came from predates that. Left stale, the canonical calls
    // fell through to the catch-all below and got an object where a list
    // was expected, so the editor never finished mounting.
    if (url.includes("/api/v1/workspaces/workspace-1/context/pages/tags")) {
      return jsonResponse({ data: [] });
    }
    if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
      return jsonResponse({ data: [], next_cursor: null });
    }
    return jsonResponse({ data: page });
  });

  renderWithRouter(
    "/pages/new?from=context",
    <Route path="/pages/new" element={<PageEditor mode="create" />} />,
  );

  await screen.findByLabelText("Markdown body");
  return fetchMock;
}

// Mounts the editor at a 390px viewport with a stored Split preference, which
// that width must not honour.
async function renderMobileEditor() {
  vi.stubGlobal("innerWidth", 390);
  window.localStorage.setItem(
    "dtxwiki:editor:default:hello",
    JSON.stringify({ mode: "split" }),
  );
  mockEditorPage("# Hello\n\nBody");

  renderWithRouter(
    "/pages/hello/edit",
    <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
  );

  expect(await screen.findByLabelText("Markdown body")).toBeInTheDocument();
}

// Mounts the editor on "# Hello" in its default Source mode.
async function renderEditorOnHello() {
  mockEditorPage("# Hello");
  renderWithRouter(
    "/pages/hello/edit",
    <Route path="/pages/:slug/edit" element={<PageEditor mode="edit" />} />,
  );
  await screen.findByLabelText("Markdown body");
}

// The Page properties controls live in one popover. Scoping the role query to
// it keeps testing-library from computing the accessible name of every button
// in the tree rail, toolbar and header on each lookup.
function pageProperties() {
  return within(screen.getByRole("region", { name: "Page properties" }));
}

function pagePropertiesToggle() {
  const toggle = document.querySelector<HTMLElement>(".editor-properties-button");
  if (!toggle) {
    throw new Error("Expected the Page properties toggle");
  }
  return toggle;
}

// The popover is rendered hidden until its floating position has been
// computed, and testing-library treats a hidden region as absent, so a lookup
// made right after the click races that position. Waiting for the region here
// (and with findByRole where a test clicks the toggle itself) removes the race.
async function openPageProperties() {
  const toggle = pagePropertiesToggle();
  if (toggle.getAttribute("aria-expanded") === "true") {
    return;
  }
  await typist().click(toggle);
  await screen.findByRole("region", { name: "Page properties" });
}

async function expectEditorTitle(value: string) {
  const title = await findTitleEditor();
  await waitFor(() => {
    expect(title).toHaveValue(value);
  });
}

function codeMirrorView() {
  const editor = document.querySelector(".cm-editor");
  if (!(editor instanceof HTMLElement)) {
    throw new Error("CodeMirror editor was not mounted");
  }
  const view = EditorView.findFromDOM(editor);
  if (!view) {
    throw new Error("CodeMirror view was not found");
  }
  return view;
}

function imageUploadInput() {
  const input = document.querySelector<HTMLInputElement>(
    'input[type="file"][aria-hidden="true"]',
  );
  if (!input) {
    throw new Error("Hidden editor attachment input was not mounted");
  }
  return input;
}

function documentChangingDispatches(calls: unknown[][]) {
  return calls.filter(([first]) => {
    if (!first || typeof first !== "object" || !("changes" in first)) {
      return false;
    }
    const changes = (first as { changes?: unknown }).changes;
    return Boolean(changes) && !(
      typeof changes === "object" &&
      changes !== null &&
      "empty" in changes &&
      (changes as { empty: boolean }).empty
    );
  });
}

function replaceEditorSelection(view: EditorView, text: string) {
  act(() => {
    view.dispatch({
      changes: { from: 0, to: view.state.doc.length, insert: text },
      selection: { anchor: 0, head: text.length },
    });
    view.focus();
  });
}

function pressEditorShortcut(
  view: EditorView,
  event: {
    key: string;
    ctrlKey?: boolean;
    metaKey?: boolean;
    shiftKey?: boolean;
  },
) {
  const alpha = /^[a-z]$/i.test(event.key);
  const browserKey = alpha && event.shiftKey
    ? event.key.toUpperCase()
    : event.key;
  const keyCode = alpha
    ? event.key.toUpperCase().charCodeAt(0)
    : undefined;
  act(() => {
    fireEvent.keyDown(view.contentDOM, {
      ...event,
      code: alpha ? `Key${event.key.toUpperCase()}` : undefined,
      key: browserKey,
      keyCode,
      which: keyCode,
    });
  });
}

function mockEditorGeometry(view: EditorView) {
  vi.spyOn(view, "coordsAtPos").mockReturnValue(
    domRect({ left: 96, top: 128, width: 0, height: 20 }),
  );
}

function domRect({
  height,
  left,
  top,
  width,
}: {
  height: number;
  left: number;
  top: number;
  width: number;
}) {
  return {
    bottom: top + height,
    height,
    left,
    right: left + width,
    toJSON: () => ({}),
    top,
    width,
    x: left,
    y: top,
  } as DOMRect;
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function mockEditorPage(bodyMd: string) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    if (url === `${BASE}/api/v1/workspaces`) {
      return workspaceResponse();
    }
    if (url === `${BASE}/api/v1/workspaces/workspace-1/tree?section=context`) {
      return jsonResponse({ data: contextTreeNodes() });
    }
    if (url === `${BASE}/api/v1/workspaces/workspace-1/library/items?limit=100`) {
      return jsonResponse({ data: [] });
    }
    if (url.includes("/api/v1/workspaces/workspace-1/context/pages/tags?")) {
      return jsonResponse({ data: [] });
    }
    if (url.includes("/api/v1/workspaces/workspace-1/context/pages?")) {
      return jsonResponse({ data: [{ ...page, body_md: bodyMd }], next_cursor: null });
    }
    return jsonResponse({ data: { ...page, body_md: bodyMd } });
  });
}

function writeCalls(fetchMock: { mock: { calls: Array<[unknown, RequestInit?]> } }) {
  return fetchMock.mock.calls.filter((call) => {
    const method = call[1]?.method;
    return method === "PATCH" || method === "POST";
  });
}

function autosaveCalls(fetchMock: { mock: { calls: Array<[unknown, RequestInit?]> } }) {
  return fetchMock.mock.calls.filter(
    (call) => call[1]?.method === "POST" && String(call[0]).endsWith("/autosave"),
  );
}

function readBlobText(blob: Blob) {
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(reader.error ?? new Error("Failed to read blob"));
    reader.readAsText(blob);
  });
}

function workspaceResponse() {
  return jsonResponse({
    data: {
      current_workspace_id: "workspace-1",
      workspaces: [
        {
          id: "workspace-1",
          slug: "default",
          title: "Default",
          name: "Default",
          goal: "Build a source-grounded workspace for cited answers, reusable notes, and team knowledge.",
          default_language: "en",
          visibility: "private",
          role: "owner",
        },
      ],
    },
  });
}

function currentContextMediaCapabilitiesResponse() {
  return jsonResponse({
    data: {
      policy_revision: "context-images-test",
      uploadable_media_types: ["image/png", "image/jpeg"],
      max_upload_bytes: 10 * 1024 * 1024,
      max_files_per_batch: 10,
      max_batch_bytes: 10 * 1024 * 1024,
      max_parallel_uploads: 3,
    },
  });
}

function contextTreeNodes() {
  return [
    {
      id: "context-root",
      section: "context",
      title: "Context",
      slug: "context",
      node_kind: "folder",
      parent_id: null,
      sort_key: "0001",
      target_kind: null,
      target_id: null,
      is_system_node: true,
      can_have_children: true,
      primary_placement: false,
      visibility_status: "visible",
      children_count: 1,
      breadcrumb: [],
    },
    {
      id: "context-inbox",
      section: "context",
      title: "Inbox",
      slug: "inbox",
      node_kind: "folder",
      parent_id: "context-root",
      sort_key: "0001",
      target_kind: null,
      target_id: null,
      is_system_node: false,
      can_have_children: true,
      primary_placement: false,
      visibility_status: "visible",
      children_count: 0,
      breadcrumb: [
        {
          id: "context-root",
          section: "context",
          title: "Context",
          slug: "context",
          node_kind: "folder",
        },
      ],
    },
  ];
}

function contextTreeNodesWithPageBranch() {
  return [
    ...contextTreeNodes(),
    {
      id: "context-page-node",
      section: "context",
      title: "Hello Page",
      slug: "hello",
      node_kind: "page_ref",
      parent_id: "context-inbox",
      sort_key: "0002",
      target_kind: "context_page",
      target_id: "p1",
      is_system_node: false,
      can_have_children: true,
      primary_placement: true,
      visibility_status: "visible",
      children_count: 1,
      breadcrumb: [
        {
          id: "context-root",
          section: "context",
          title: "Context",
          slug: "context",
          node_kind: "folder",
        },
        {
          id: "context-inbox",
          section: "context",
          title: "Inbox",
          slug: "inbox",
          node_kind: "folder",
        },
      ],
    },
    {
      id: "nested-folder",
      section: "context",
      title: "Nested Folder",
      slug: "nested-folder",
      node_kind: "folder",
      parent_id: "context-page-node",
      sort_key: "0003",
      target_kind: null,
      target_id: null,
      is_system_node: false,
      can_have_children: true,
      primary_placement: false,
      visibility_status: "visible",
      children_count: 0,
      breadcrumb: [
        {
          id: "context-root",
          section: "context",
          title: "Context",
          slug: "context",
          node_kind: "folder",
        },
        {
          id: "context-inbox",
          section: "context",
          title: "Inbox",
          slug: "inbox",
          node_kind: "folder",
        },
        {
          id: "context-page-node",
          section: "context",
          title: "Hello Page",
          slug: "hello",
          node_kind: "page_ref",
        },
      ],
    },
  ];
}

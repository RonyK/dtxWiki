import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { i18n } from "../../../shared/i18n";
import { ContextMediaManagerDialog } from "../ContextMediaManagerDialog";

const BASE = "http://127.0.0.1:21080";
const USED_ASSET_ID = "123e4567-e89b-12d3-a456-426614174000";
const UNUSED_ASSET_ID = "123e4567-e89b-12d3-a456-426614174001";

describe("ContextMediaManagerDialog", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    delete window.__DTXWIKI_RUNTIME_CONFIG__;
  });

  afterEach(async () => {
    await i18n.changeLanguage("en");
  });

  it("confirms permanent deletion, preserves If-Match, and refreshes the list", async () => {
    let deleted = false;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (init?.method === "DELETE") {
        deleted = true;
        return jsonResponse({
          data: {
            id: UNUSED_ASSET_ID,
            association_state: "tombstoned",
            gc_state: "queued",
          },
        });
      }
      if (url.endsWith("/api/v1/workspaces/w1/context/pages/page-1/media?usage=used")) {
        return jsonResponse({ data: [mediaAsset("history_only")] });
      }
      if (url.endsWith("/api/v1/workspaces/w1/context/pages/page-1/media?usage=unused")) {
        return jsonResponse({ data: deleted ? [] : [mediaAsset("unused")] });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(<ContextMediaManagerDialog onClose={vi.fn()} pageId="page-1" workspaceId="w1" />);

    const dialog = await screen.findByRole("dialog", { name: "Page media" });
    expect(await within(dialog).findByText("retained.png")).toBeInTheDocument();
    expect(within(dialog).getByLabelText("1 used media item")).toHaveTextContent(
      "1",
    );
    expect(within(dialog).getByLabelText("1 unused media item")).toHaveTextContent(
      "1",
    );
    expect(within(dialog).getByText("History only")).toBeInTheDocument();
    expect(
      within(dialog).getByText("Draft 0, published 1, history 3"),
    ).toBeInTheDocument();
    expect(within(dialog).getByText("unused.png")).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Delete retained.png" }))
      .not.toBeInTheDocument();
    expect(
      within(dialog).getByText(
        "Retained by a current or historical page version and cannot be deleted.",
      ),
    ).toBeInTheDocument();
    expect(
      dialog.querySelector(
        `img[src="/api/v1/workspaces/w1/context/pages/page-1/versions/v1/media/${USED_ASSET_ID}/content"]`,
      ),
    ).toBeInTheDocument();

    await userEvent.click(
      within(dialog).getByRole("button", { name: "Delete unused.png" }),
    );
    expect(within(dialog).getByRole("alert")).toHaveTextContent(
      "Permanently delete unused.png?",
    );
    expect(
      fetchMock.mock.calls.some((call) => call[1]?.method === "DELETE"),
    ).toBe(false);

    await userEvent.click(
      within(dialog).getByRole("button", { name: "Delete permanently" }),
    );

    await waitFor(() => {
      expect(within(dialog).getByText("No unused media.")).toBeInTheDocument();
    });
    expect(within(dialog).getByRole("status")).toHaveTextContent(
      "unused.png was deleted.",
    );
    const deleteCall = fetchMock.mock.calls.find(
      (call) => call[1]?.method === "DELETE",
    );
    expect(String(deleteCall?.[0])).toBe(
      `${BASE}/api/v1/workspaces/w1/context/media/assets/${UNUSED_ASSET_ID}`,
    );
    expect(new Headers(deleteCall?.[1]?.headers).get("If-Match")).toBe(
      "cmu1_unused",
    );
    expect(
      fetchMock.mock.calls.filter((call) => String(call[0]).includes("usage=used")),
    ).toHaveLength(2);
    expect(
      fetchMock.mock.calls.filter((call) => String(call[0]).includes("usage=unused")),
    ).toHaveLength(2);
  });

  it("cancels permanent deletion without issuing DELETE", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input) => {
        const url = String(input);
        if (url.endsWith("/api/v1/workspaces/w1/context/pages/page-1/media?usage=used")) {
          return jsonResponse({ data: [] });
        }
        if (url.endsWith("/api/v1/workspaces/w1/context/pages/page-1/media?usage=unused")) {
          return jsonResponse({ data: [mediaAsset("unused")] });
        }
        throw new Error(`Unexpected request: ${url}`);
      },
    );

    render(<ContextMediaManagerDialog onClose={vi.fn()} pageId="page-1" workspaceId="w1" />);

    const dialog = await screen.findByRole("dialog", { name: "Page media" });
    await userEvent.click(
      within(dialog).getByRole("button", { name: "Delete unused.png" }),
    );
    expect(
      within(dialog).getByText("Permanently delete unused.png?"),
    ).toBeInTheDocument();

    await userEvent.click(
      within(dialog).getByRole("button", { name: "Cancel" }),
    );

    expect(within(dialog).queryByText("Permanently delete unused.png?")).not
      .toBeInTheDocument();
    expect(
      within(dialog).getByRole("button", { name: "Delete unused.png" }),
    ).toBeEnabled();
    expect(
      fetchMock.mock.calls.some((call) => call[1]?.method === "DELETE"),
    ).toBe(false);
  });

  it("blocks Escape, backdrop, and close-button dismissal while delete is pending", async () => {
    let deleteStarted = false;
    let releaseDelete!: (response: Response) => void;
    const deleteGate = new Promise<Response>((resolve) => {
      releaseDelete = resolve;
    });
    const onClose = vi.fn();
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(
      async (input, init) => {
        const url = String(input);
        if (init?.method === "DELETE") {
          deleteStarted = true;
          return deleteGate;
        }
        if (url.endsWith("/api/v1/workspaces/w1/context/pages/page-1/media?usage=used")) {
          return jsonResponse({ data: [] });
        }
        if (url.endsWith("/api/v1/workspaces/w1/context/pages/page-1/media?usage=unused")) {
          return jsonResponse({
            data: deleteStarted ? [] : [mediaAsset("unused")],
          });
        }
        throw new Error(`Unexpected request: ${url}`);
      },
    );

    render(<ContextMediaManagerDialog onClose={onClose} pageId="page-1" workspaceId="w1" />);

    const dialog = await screen.findByRole("dialog", { name: "Page media" });
    const closeButton = within(dialog).getByRole("button", {
      name: "Close page media",
    });
    await userEvent.click(
      within(dialog).getByRole("button", { name: "Delete unused.png" }),
    );
    await userEvent.click(
      within(dialog).getByRole("button", { name: "Delete permanently" }),
    );
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.filter((call) => call[1]?.method === "DELETE"),
      ).toHaveLength(1);
    });

    expect(closeButton).toBeDisabled();
    fireEvent.keyDown(window, { key: "Escape" });
    fireEvent.mouseDown(dialog.parentElement!);
    await userEvent.click(closeButton);

    expect(onClose).not.toHaveBeenCalled();
    expect(dialog).toBeInTheDocument();

    await act(async () => {
      releaseDelete(jsonResponse({
        data: {
          id: UNUSED_ASSET_ID,
          association_state: "tombstoned",
          gc_state: "queued",
        },
      }));
      await deleteGate;
    });
    expect(await within(dialog).findByRole("status")).toHaveTextContent(
      "unused.png was deleted.",
    );
    expect(closeButton).toBeEnabled();
  });

  it("announces loading and shows separate empty states", async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
      await gate;
      return jsonResponse({ data: [] });
    });

    render(<ContextMediaManagerDialog onClose={vi.fn()} pageId="page-1" workspaceId="w1" />);

    expect(screen.getByRole("status")).toHaveTextContent("Loading page media…");
    await act(async () => {
      release();
      await gate;
    });
    expect(await screen.findByText(/No media is referenced/)).toBeInTheDocument();
    expect(screen.getByText("No unused media.")).toBeInTheDocument();
    expect(screen.getByLabelText("0 used media items")).toHaveTextContent("0");
    expect(screen.getByLabelText("0 unused media items")).toHaveTextContent("0");
  });

  it("shows a retryable load error", async () => {
    let shouldFail = true;
    vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      shouldFail
        ? jsonResponse({
            error_code: "internal_error",
            message: "failed",
            request_id: "request-1",
            warnings: [],
          }, 500)
        : jsonResponse({ data: [] }),
    );

    render(<ContextMediaManagerDialog onClose={vi.fn()} pageId="page-1" workspaceId="w1" />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load page media. Try again.",
    );
    shouldFail = false;
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("No unused media.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it.each([
    {
      expected: "This media changed. Refresh the list and try again.",
      status: 412,
    },
    {
      expected: "This media is now in use and cannot be deleted.",
      status: 409,
    },
    {
      expected: "Could not delete this media. Try again.",
      status: 500,
    },
  ])(
    "shows a refreshable delete error for HTTP $status",
    async ({ expected, status }) => {
      let listCalls = 0;
      vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
        const url = String(input);
        if (init?.method === "DELETE") {
          return jsonResponse(
            {
              error_code: "media_state_changed",
              message: "failed",
              request_id: "request-delete",
              warnings: [],
            },
            status,
          );
        }
        listCalls += 1;
        if (url.endsWith("/api/v1/workspaces/w1/context/pages/page-1/media?usage=used")) {
          return jsonResponse({ data: [] });
        }
        if (url.endsWith("/api/v1/workspaces/w1/context/pages/page-1/media?usage=unused")) {
          return jsonResponse({ data: [mediaAsset("unused")] });
        }
        throw new Error(`Unexpected request: ${url}`);
      });

      render(<ContextMediaManagerDialog onClose={vi.fn()} pageId="page-1" workspaceId="w1" />);

      await userEvent.click(
        await screen.findByRole("button", { name: "Delete unused.png" }),
      );
      await userEvent.click(
        screen.getByRole("button", { name: "Delete permanently" }),
      );
      expect(await screen.findByRole("alert")).toHaveTextContent(expected);

      await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
      await waitFor(() => expect(listCalls).toBe(4));
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    },
  );

  it("renders Korean labels, counts, usage facts, and retained-history policy", async () => {
    await i18n.changeLanguage("ko");
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/api/v1/workspaces/w1/context/pages/page-1/media?usage=used")) {
        return jsonResponse({ data: [mediaAsset("history_only")] });
      }
      if (url.endsWith("/api/v1/workspaces/w1/context/pages/page-1/media?usage=unused")) {
        return jsonResponse({ data: [mediaAsset("unused")] });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(<ContextMediaManagerDialog onClose={vi.fn()} pageId="page-1" workspaceId="w1" />);

    const dialog = await screen.findByRole("dialog", { name: "페이지 미디어" });
    expect(within(dialog).getByText("사용 중")).toBeInTheDocument();
    expect(within(dialog).getByLabelText("사용 중인 미디어 1개")).toHaveTextContent(
      "1",
    );
    expect(within(dialog).getByLabelText("미사용 미디어 1개")).toHaveTextContent(
      "1",
    );
    expect(within(dialog).getByText("이전 버전에서만 사용")).toBeInTheDocument();
    expect(
      within(dialog).getByText("초안 0개, 게시본 1개, 이전 버전 3개"),
    ).toBeInTheDocument();
    expect(within(dialog).getByText("2.0 킬로바이트")).toBeInTheDocument();
    expect(
      within(dialog).getByText(
        "현재 또는 이전 페이지 버전에서 참조 중이므로 삭제할 수 없습니다.",
      ),
    ).toBeInTheDocument();
    expect(
      within(dialog).queryByRole("button", { name: "retained.png 삭제" }),
    ).not.toBeInTheDocument();
    expect(
      within(dialog).getByRole("button", { name: "unused.png 삭제" }),
    ).toBeEnabled();
    expect(
      within(dialog).getByRole("button", { name: "페이지 미디어 닫기" }),
    ).toBeInTheDocument();
  });

  it("closes on Escape and restores focus through the shared Dialog contract", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ data: [] }),
    );
    const user = userEvent.setup();

    render(<ManagerDialogHarness />);
    const opener = screen.getByRole("button", { name: "Open media manager" });
    await user.click(opener);

    const dialog = await screen.findByRole("dialog", { name: "Page media" });
    expect(dialog).toHaveFocus();

    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog", { name: "Page media" })).not
      .toBeInTheDocument();
    expect(opener).toHaveFocus();
  });
});

function ManagerDialogHarness() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)} type="button">
        Open media manager
      </button>
      {open ? (
        <ContextMediaManagerDialog
          onClose={() => setOpen(false)}
          pageId="page-1"
          workspaceId="w1"
        />
      ) : null}
    </>
  );
}

function mediaAsset(usageState: "history_only" | "unused") {
  const unused = usageState === "unused";
  const id = unused ? UNUSED_ASSET_ID : USED_ASSET_ID;
  return {
    id,
    logical_ref: `dtx-media://asset/${id}`,
    original_filename: unused ? "unused.png" : "retained.png",
    media_type: "image/png",
    bytes: unused ? 512 : 2048,
    association_state: "active",
    usage_state: usageState,
    latest_draft_reference_count: 0,
    current_published_reference_count: unused ? 0 : 1,
    historical_reference_count: unused ? 0 : 3,
    can_delete: unused,
    usage_token: unused ? "cmu1_unused" : "cmu1_used",
    content_url: `/api/v1/workspaces/w1/context/pages/page-1/versions/v1/media/${id}/content`,
  };
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

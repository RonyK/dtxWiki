import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { setLocale } from "../../../shared/i18n";
import { AdminJobsSection } from "../AdminJobsSection";

const BASE = "http://127.0.0.1:21080";
const JOB_ID = "00000000-0000-0000-0000-000000000201";
// Matches job()'s default workspace_id below -- retryAdminJob (Epic-2 Phase
// 6c) now carries workspace_id as a required query param, sourced from the
// job row's own workspace_id, so the mocked retry URL below has to include it.
const WORKSPACE_ID = "00000000-0000-0000-0000-000000000101";
const RETRY_URL = `${BASE}/api/v1/admin/jobs/${JOB_ID}/retry?workspace_id=${WORKSPACE_ID}`;

describe("AdminJobsSection", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    setLocale("en", { persist: false });
  });

  it("reloads summary and page one after retrying a problem job", async () => {
    const user = userEvent.setup();
    let summaryRequests = 0;
    let listRequests = 0;
    const failedJob = job({ status: "failed", retryable: true });
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async (input, init) => {
        const url = input.toString();
        if (url === `${BASE}/api/v1/admin/jobs/summary`) {
          summaryRequests += 1;
          return adminSummaryResponse(
            summaryRequests === 1
              ? {
                  queued_count: 0,
                  running_count: 0,
                  failed_count: 1,
                  lease_expired_count: 0,
                }
              : {
                  queued_count: 1,
                  running_count: 0,
                  failed_count: 0,
                  lease_expired_count: 0,
                },
          );
        }
        if (url === `${BASE}/api/v1/admin/jobs?problems_only=true&limit=50`) {
          listRequests += 1;
          return adminJobsResponse(
            listRequests === 1
              ? { items: [failedJob], next_cursor: "stale-offset-cursor" }
              : { items: [], next_cursor: null },
          );
        }
        if (url === RETRY_URL && init?.method === "POST") {
          return jsonResponse({
            data: job({
              status: "queued",
              retryable: false,
              progress: 0,
              completed_at: null,
            }),
          });
        }
        throw new Error(`Unexpected request: ${url}`);
      });

    render(<AdminJobsSection />);

    expect(await screen.findByText("feedback resolution")).toBeInTheDocument();
    expect(screen.getByText("The job failed.")).toBeInTheDocument();
    expect(screen.queryByText("secret-sentinel")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Load more" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Retry" }));

    expect(await screen.findByText("No problem jobs")).toBeInTheDocument();
    expect(screen.queryByText("feedback resolution")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
    await waitFor(() => {
      expect(summaryRequests).toBe(2);
      expect(listRequests).toBe(2);
    });
    const queuedSummary = screen.getByText("Queued").closest("div");
    const failedSummary = screen.getByText("Failed").closest("div");
    expect(queuedSummary).not.toBeNull();
    expect(failedSummary).not.toBeNull();
    expect(within(queuedSummary!).getByText("1")).toBeInTheDocument();
    expect(within(failedSummary!).getByText("0")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      RETRY_URL,
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("blocks every retry action while one job retry is pending", async () => {
    const user = userEvent.setup();
    const secondJobId = "00000000-0000-0000-0000-000000000202";
    const firstJob = job({ status: "failed", retryable: true });
    const secondJob = {
      ...job({ status: "failed", retryable: true }),
      id: secondJobId,
      job_type: "library_discovery",
    };
    let listRequests = 0;
    let retryRequests = 0;
    let resolveRetry!: (response: Response) => void;
    const retryResponse = new Promise<Response>((resolve) => {
      resolveRetry = resolve;
    });

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/admin/jobs/summary`) {
        return adminSummaryResponse({
          queued_count: 0,
          running_count: 0,
          failed_count: 2,
          lease_expired_count: 0,
        });
      }
      if (url === `${BASE}/api/v1/admin/jobs?problems_only=true&limit=50`) {
        listRequests += 1;
        return adminJobsResponse({
          items: listRequests === 1 ? [firstJob, secondJob] : [secondJob],
          next_cursor: null,
        });
      }
      if (url === RETRY_URL && init?.method === "POST") {
        retryRequests += 1;
        return retryResponse;
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(<AdminJobsSection />);
    expect(await screen.findByText("feedback resolution")).toBeInTheDocument();
    const [firstRetry] = screen.getAllByRole("button", { name: "Retry" });

    await user.click(firstRetry);

    const retrying = await screen.findByRole("button", { name: /Retrying/ });
    const otherRetry = screen.getByRole("button", { name: "Retry" });
    expect(retrying).toBeDisabled();
    expect(otherRetry).toBeDisabled();

    await user.click(otherRetry);
    await user.click(retrying);
    expect(retryRequests).toBe(1);

    resolveRetry(
      jsonResponse({
        data: job({
          status: "queued",
          retryable: false,
          progress: 0,
          completed_at: null,
        }),
      }),
    );
    await waitFor(() => expect(listRequests).toBe(2));
    expect(screen.getByRole("button", { name: "Retry" })).toBeEnabled();
  });

  it("keeps the latest filter and ignores older list responses while retrying", async () => {
    const user = userEvent.setup();
    const failedJob = job({ status: "failed", retryable: true });
    const latestAllJob = {
      ...job({ status: "queued", retryable: false, completed_at: null }),
      id: "00000000-0000-0000-0000-000000000202",
      job_type: "library_discovery",
    };
    const staleAllJob = {
      ...job({ status: "completed", retryable: false }),
      id: "00000000-0000-0000-0000-000000000203",
      job_type: "lint_tick",
    };
    let problemsRequests = 0;
    let allRequests = 0;
    let resolveRetry!: (response: Response) => void;
    let resolveStaleAll!: (response: Response) => void;
    const retryResponse = new Promise<Response>((resolve) => {
      resolveRetry = resolve;
    });
    const staleAllResponse = new Promise<Response>((resolve) => {
      resolveStaleAll = resolve;
    });

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/admin/jobs/summary`) {
        return adminSummaryResponse({
          queued_count: 1,
          running_count: 0,
          failed_count: 0,
          lease_expired_count: 0,
        });
      }
      if (url === `${BASE}/api/v1/admin/jobs?problems_only=true&limit=50`) {
        problemsRequests += 1;
        return adminJobsResponse({ items: [failedJob], next_cursor: null });
      }
      if (url === `${BASE}/api/v1/admin/jobs?problems_only=false&limit=50`) {
        allRequests += 1;
        if (allRequests === 1) {
          return staleAllResponse;
        }
        return adminJobsResponse({ items: [latestAllJob], next_cursor: null });
      }
      if (url === RETRY_URL && init?.method === "POST") {
        return retryResponse;
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(<AdminJobsSection />);
    expect(await screen.findByText("feedback resolution")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Retry" }));
    await user.click(
      screen.getByRole("checkbox", {
        name: "Show only failures and jobs past their lease",
      }),
    );
    await waitFor(() => expect(allRequests).toBe(1));

    resolveRetry(
      jsonResponse({
        data: job({
          status: "queued",
          retryable: false,
          progress: 0,
          completed_at: null,
        }),
      }),
    );
    await waitFor(() => expect(allRequests).toBe(2));
    expect(await screen.findByText("library discovery")).toBeInTheDocument();

    resolveStaleAll(
      adminJobsResponse({ items: [staleAllJob], next_cursor: "stale-cursor" }),
    );
    await waitFor(() => {
      expect(screen.getByText("library discovery")).toBeInTheDocument();
      expect(screen.queryByText("lint tick")).not.toBeInTheDocument();
      expect(problemsRequests).toBe(1);
      expect(screen.getByRole("checkbox")).not.toBeChecked();
    });
  });

  it("does not let an older load-more completion unlock the current request", async () => {
    const user = userEvent.setup();
    const firstProblem = job({ status: "failed", retryable: true });
    const currentAllJob = {
      ...job({ status: "queued", retryable: false, completed_at: null }),
      id: "00000000-0000-0000-0000-000000000204",
      job_type: "library_discovery",
    };
    const currentNextJob = {
      ...job({ status: "completed", retryable: false }),
      id: "00000000-0000-0000-0000-000000000205",
      job_type: "library_preview",
    };
    let resolveOldPage!: (response: Response) => void;
    let resolveCurrentPage!: (response: Response) => void;
    let oldPageReturned = false;
    let currentPageRequests = 0;
    const oldPageResponse = new Promise<Response>((resolve) => {
      resolveOldPage = resolve;
    });
    const currentPageResponse = new Promise<Response>((resolve) => {
      resolveCurrentPage = resolve;
    });

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/admin/jobs/summary`) {
        return adminSummaryResponse({
          queued_count: 1,
          running_count: 0,
          failed_count: 1,
          lease_expired_count: 0,
        });
      }
      if (url === `${BASE}/api/v1/admin/jobs?problems_only=true&limit=50`) {
        return adminJobsResponse({
          items: [firstProblem],
          next_cursor: "problem-cursor",
        });
      }
      if (
        url ===
        `${BASE}/api/v1/admin/jobs?problems_only=true&limit=50&cursor=problem-cursor`
      ) {
        const response = await oldPageResponse;
        oldPageReturned = true;
        return response;
      }
      if (url === `${BASE}/api/v1/admin/jobs?problems_only=false&limit=50`) {
        return adminJobsResponse({
          items: [currentAllJob],
          next_cursor: "all-cursor",
        });
      }
      if (
        url ===
        `${BASE}/api/v1/admin/jobs?problems_only=false&limit=50&cursor=all-cursor`
      ) {
        currentPageRequests += 1;
        return currentPageResponse;
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(<AdminJobsSection />);
    expect(await screen.findByText("feedback resolution")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Load more" }));
    await user.click(
      screen.getByRole("checkbox", {
        name: "Show only failures and jobs past their lease",
      }),
    );
    expect(await screen.findByText("library discovery")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Load more" }));
    expect(currentPageRequests).toBe(1);

    resolveOldPage(
      adminJobsResponse({
        items: [{ ...firstProblem, job_type: "stale_problem_page" }],
        next_cursor: null,
      }),
    );
    await waitFor(() => expect(oldPageReturned).toBe(true));
    const currentLoadMore = screen.getByRole("button", { name: /Loading|Load more/ });
    await user.click(currentLoadMore);
    expect(currentPageRequests).toBe(1);
    expect(currentLoadMore).toBeDisabled();

    resolveCurrentPage(
      adminJobsResponse({ items: [currentNextJob], next_cursor: null }),
    );
    expect(await screen.findByText("library preview")).toBeInTheDocument();
    expect(screen.queryByText("stale problem page")).not.toBeInTheDocument();
  });

  it("flags an unreachable workspace shard instead of a silently short result, and keeps flagging it across load-more", async () => {
    const user = userEvent.setup();
    const firstProblem = job({ status: "failed", retryable: true });
    const nextPageJob = {
      ...job({ status: "failed", retryable: true }),
      id: "00000000-0000-0000-0000-000000000206",
      job_type: "library_discovery",
    };
    const summaryUnreachableId = "00000000-0000-0000-0000-0000000000a1";
    const page1UnreachableId = "00000000-0000-0000-0000-0000000000a2";
    // Deliberately a THIRD id distinct from both of the above: this is the
    // shape the fan-out cursor design does not obviously handle -- a shard
    // that was reachable on page one and goes unreachable only on the next
    // page. The banner (and the id set behind it) must grow to cover it,
    // not replace what page one already reported.
    const page2UnreachableId = "00000000-0000-0000-0000-0000000000a3";

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/admin/jobs/summary`) {
        return adminSummaryResponse({
          queued_count: 0,
          running_count: 0,
          failed_count: 1,
          lease_expired_count: 0,
          unreachable_workspace_ids: [summaryUnreachableId],
        });
      }
      if (url === `${BASE}/api/v1/admin/jobs?problems_only=true&limit=50`) {
        return adminJobsResponse({
          items: [firstProblem],
          next_cursor: "page-2-cursor",
          unreachable_workspace_ids: [page1UnreachableId],
        });
      }
      if (
        url ===
        `${BASE}/api/v1/admin/jobs?problems_only=true&limit=50&cursor=page-2-cursor`
      ) {
        return adminJobsResponse({
          items: [nextPageJob],
          next_cursor: null,
          unreachable_workspace_ids: [page2UnreachableId],
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(<AdminJobsSection />);
    expect(await screen.findByText("feedback resolution")).toBeInTheDocument();
    // Two distinct ids so far (summary's + page one's) -- not a silent short
    // list and not a 500: the reachable job is shown *and* the gap is named.
    expect(
      await screen.findByText(
        "2 workspaces could not be reached; the totals and list below may be missing their jobs.",
      ),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Load more" }));

    expect(await screen.findByText("library discovery")).toBeInTheDocument();
    // Grew to 3 (unioned page two's id in), not reset to 1: the banner has
    // to stay accurate for every job currently on screen, including page
    // one's, whose shard is still unreported by this later fetch.
    expect(
      await screen.findByText(
        "3 workspaces could not be reached; the totals and list below may be missing their jobs.",
      ),
    ).toBeInTheDocument();
  });

  it("shows no incompleteness banner once every shard answers", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url === `${BASE}/api/v1/admin/jobs/summary`) {
        return adminSummaryResponse({
          queued_count: 0,
          running_count: 0,
          failed_count: 0,
          lease_expired_count: 0,
        });
      }
      if (url === `${BASE}/api/v1/admin/jobs?problems_only=true&limit=50`) {
        return adminJobsResponse({ items: [], next_cursor: null });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    render(<AdminJobsSection />);

    expect(await screen.findByText("No problem jobs")).toBeInTheDocument();
    expect(
      screen.queryByText(/could not be reached/),
    ).not.toBeInTheDocument();
  });

  it("switches to the admin endpoints when scope flips after the first render", async () => {
    // SettingsScreen decides `scope` from isServerAdmin, which is false until
    // /auth/me resolves -- so this component genuinely mounts as "workspace"
    // and becomes "server" a moment later. Every other test here renders with
    // a fixed scope, so none of them can see a callback that captured the
    // first value and kept it.
    let adminListRequests = 0;
    let workspaceListRequests = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url.startsWith(`${BASE}/api/v1/workspaces/`) && url.includes("/jobs")) {
        workspaceListRequests += 1;
        return adminJobsResponse({ items: [], next_cursor: null });
      }
      if (url === `${BASE}/api/v1/workspaces`) {
        return jsonResponse({
          data: { current_workspace_id: WORKSPACE_ID, workspaces: [] },
        });
      }
      if (url === `${BASE}/api/v1/admin/jobs/summary`) {
        return adminSummaryResponse({
          queued_count: 0,
          running_count: 0,
          failed_count: 0,
          lease_expired_count: 0,
        });
      }
      if (url.startsWith(`${BASE}/api/v1/admin/jobs`)) {
        adminListRequests += 1;
        return adminJobsResponse({ items: [], next_cursor: null });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    const view = render(<AdminJobsSection scope="workspace" />);
    await waitFor(() => expect(workspaceListRequests).toBe(1));
    expect(adminListRequests).toBe(0);

    view.rerender(<AdminJobsSection scope="server" />);

    // The admin endpoints must be reached because the scope changed, not
    // because something else refetched: the workspace count must stay put.
    await waitFor(() => expect(adminListRequests).toBe(1));
    expect(workspaceListRequests).toBe(1);
  });

  it("drops the previous workspace's jobs the moment the selection moves (UI-01)", async () => {
    // The component outlives a workspace change -- SettingsScreen keeps it
    // mounted -- and it used to read the module-global current workspace
    // inside load(), with an effect that did not depend on the workspace at
    // all. So nothing refetched and workspace A's rows, carrying A's slug,
    // stayed on screen under workspace B.
    const WORKSPACE_A = "00000000-0000-0000-0000-0000000000a1";
    const WORKSPACE_B = "00000000-0000-0000-0000-0000000000b2";
    let releaseA: () => void = () => {};
    const aPending = new Promise<void>((resolve) => {
      releaseA = resolve;
    });

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url.startsWith(`${BASE}/api/v1/workspaces/${WORKSPACE_A}/jobs`)) {
        // Held open so it can land AFTER the switch, which is the race the
        // generation fence has to lose gracefully.
        await aPending;
        return jsonResponse({
          data: {
            items: [{ ...job(), workspace_slug: "alpha" }],
            next_cursor: null,
          },
        });
      }
      if (url.startsWith(`${BASE}/api/v1/workspaces/${WORKSPACE_B}/jobs`)) {
        return jsonResponse({ data: { items: [], next_cursor: null } });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    const view = render(
      <AdminJobsSection scope="workspace" workspaceId={WORKSPACE_A} />,
    );
    releaseA();
    expect(await screen.findByText("alpha")).toBeInTheDocument();

    view.rerender(
      <AdminJobsSection scope="workspace" workspaceId={WORKSPACE_B} />,
    );

    // Cleared immediately, not once B's response arrives: the fence can stop a
    // late paint but cannot un-render rows already on screen.
    await waitFor(() =>
      expect(screen.queryByText("alpha")).not.toBeInTheDocument(),
    );
  });

  it("does not let a slow response from the old workspace repaint the new one (UI-01)", async () => {
    const WORKSPACE_A = "00000000-0000-0000-0000-0000000000a1";
    const WORKSPACE_B = "00000000-0000-0000-0000-0000000000b2";
    let releaseA: () => void = () => {};
    const aPending = new Promise<void>((resolve) => {
      releaseA = resolve;
    });

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url.startsWith(`${BASE}/api/v1/workspaces/${WORKSPACE_A}/jobs`)) {
        await aPending;
        return jsonResponse({
          data: {
            items: [{ ...job(), workspace_slug: "alpha" }],
            next_cursor: null,
          },
        });
      }
      if (url.startsWith(`${BASE}/api/v1/workspaces/${WORKSPACE_B}/jobs`)) {
        return jsonResponse({
          data: {
            items: [{ ...job(), id: "bbbbbbbb-0000-0000-0000-000000000001", workspace_slug: "bravo" }],
            next_cursor: null,
          },
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    const view = render(
      <AdminJobsSection scope="workspace" workspaceId={WORKSPACE_A} />,
    );
    view.rerender(
      <AdminJobsSection scope="workspace" workspaceId={WORKSPACE_B} />,
    );
    expect(await screen.findByText("bravo")).toBeInTheDocument();

    // A's request only now completes. It must be discarded, not merged.
    releaseA();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByText("alpha")).not.toBeInTheDocument();
    expect(screen.getByText("bravo")).toBeInTheDocument();
  });

  it("clears on switch even while the new workspace is still loading (UI-01)", async () => {
    // Refetching alone is not enough. If the new workspace's request is slow
    // -- or never answers -- the previous workspace's rows stay on screen for
    // as long as it takes, under the new workspace's name. This is what makes
    // the explicit reset load-bearing rather than decorative: it is the only
    // assertion here that fails if the reset is deleted while the effect
    // dependency stays correct.
    const WORKSPACE_A = "00000000-0000-0000-0000-0000000000a1";
    const WORKSPACE_B = "00000000-0000-0000-0000-0000000000b2";

    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      if (url.startsWith(`${BASE}/api/v1/workspaces/${WORKSPACE_A}/jobs`)) {
        return jsonResponse({
          data: {
            items: [{ ...job(), workspace_slug: "alpha" }],
            next_cursor: "cursor-from-alpha",
          },
        });
      }
      if (url.startsWith(`${BASE}/api/v1/workspaces/${WORKSPACE_B}/jobs`)) {
        // Never resolves: the switch must stand on its own.
        return new Promise<Response>(() => {});
      }
      throw new Error(`Unexpected request: ${url}`);
    });

    const view = render(
      <AdminJobsSection scope="workspace" workspaceId={WORKSPACE_A} />,
    );
    expect(await screen.findByText("alpha")).toBeInTheDocument();
    // A cursor is workspace-specific, and "Load more" renders on the cursor
    // alone -- it is not behind the `loading` gate that hides the rows.
    expect(screen.getByRole("button", { name: /Load more/i })).toBeInTheDocument();

    view.rerender(
      <AdminJobsSection scope="workspace" workspaceId={WORKSPACE_B} />,
    );

    await waitFor(() =>
      expect(screen.queryByText("alpha")).not.toBeInTheDocument(),
    );
    expect(
      screen.queryByRole("button", { name: /Load more/i }),
    ).not.toBeInTheDocument();
  });
});

function job(
  overrides: Partial<{
    status: "queued" | "running" | "completed" | "failed";
    retryable: boolean;
    progress: number;
    completed_at: string | null;
  }> = {},
) {
  return {
    id: JOB_ID,
    workspace_id: "00000000-0000-0000-0000-000000000101",
    workspace_slug: "default",
    job_type: "feedback_resolution",
    trigger_type: "manual",
    status: overrides.status ?? "failed",
    stage: null,
    progress: overrides.progress ?? 40,
    retry_count: 1,
    error_code: "job_failed",
    // Defense in depth: even a stale/malicious server response carrying a raw
    // diagnostic must not be rendered when a stable error_code is available.
    last_error: "secret-sentinel",
    started_at: "2026-08-28T04:00:00Z",
    next_attempt_at: null,
    completed_at:
      "completed_at" in overrides
        ? overrides.completed_at
        : "2026-08-28T04:01:00Z",
    created_at: "2026-08-28T03:59:00Z",
    updated_at: "2026-08-28T04:01:00Z",
    retryable: overrides.retryable ?? true,
    lease_expired: false,
  };
}

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

// Server-admin summary/list responses include warnings and unreachable workspaces.
// These helpers preserve that response shape; tests override the empty workspace
// list when exercising the incomplete fan-out banner.
function adminSummaryResponse(data: {
  queued_count: number;
  running_count: number;
  failed_count: number;
  lease_expired_count: number;
  unreachable_workspace_ids?: string[];
}) {
  return jsonResponse({
    envelope: { warnings: [] },
    data: { unreachable_workspace_ids: [], ...data },
  });
}

function adminJobsResponse(data: {
  items: unknown[];
  next_cursor: string | null;
  unreachable_workspace_ids?: string[];
}) {
  return jsonResponse({
    envelope: { warnings: [] },
    data: { unreachable_workspace_ids: [], ...data },
  });
}

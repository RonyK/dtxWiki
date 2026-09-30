import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../../../shared/api/contextMediaAttachments", () => ({
  listWorkspaceContextMediaAttachments: vi.fn(),
}));
vi.mock("../../../shared/api/contextMediaOrphans", () => ({
  listAdminContextMediaOrphans: vi.fn(),
}));
vi.mock("../../../shared/api/contextMedia", () => ({
  deletePageContextMedia: vi.fn(),
}));
vi.mock("../../../shared/api/workspace", () => ({
  getCurrentWorkspaceId: vi.fn(),
}));
vi.mock("../../../shared/api/client", async (importOriginal) => ({
  // Only the base URL is faked. ApiError and apiErrorMessage stay real:
  // mediaDeleteErrorKind narrows with `instanceof`, and a stubbed class
  // would make the 409 case pass for the wrong reason.
  ...(await importOriginal<typeof import("../../../shared/api/client")>()),
  getApiBaseUrl: vi.fn(() => ""),
}));

import { ApiError, getApiBaseUrl } from "../../../shared/api/client";
import { deletePageContextMedia } from "../../../shared/api/contextMedia";
import { listWorkspaceContextMediaAttachments } from "../../../shared/api/contextMediaAttachments";
import { listAdminContextMediaOrphans } from "../../../shared/api/contextMediaOrphans";
import { getCurrentWorkspaceId } from "../../../shared/api/workspace";
import { AttachmentsSection } from "../AttachmentsSection";

const ATTACHMENT = {
  asset_id: "00000000-0000-0000-0000-000000000301",
  workspace_id: "ws-1",
  page_id: "00000000-0000-0000-0000-000000000601",
  page_title: "Release Notes Draft",
  created_for_version_id: "00000000-0000-0000-0000-000000000801",
  logical_ref: "dtx-media://asset/00000000-0000-0000-0000-000000000301",
  original_filename: "diagram.png",
  kind: "image" as const,
  media_type: "image/png",
  bytes: 2_400_000,
  association_state: "active" as const,
  usage_state: "unused" as const,
  latest_draft_reference_count: 0,
  current_published_reference_count: 0,
  historical_reference_count: 0,
  is_orphan: true,
  is_mine: true,
  can_delete: true,
  usage_token: "token-301",
  content_url:
    "/api/v1/workspaces/ws-1/context/pages/00000000-0000-0000-0000-000000000601/versions/00000000-0000-0000-0000-000000000801/media/00000000-0000-0000-0000-000000000301/content",
  uploaded_by_display_name: "Tester",
  created_by_actor_kind: "human",
  created_by_actor_id: "00000000-0000-0000-0000-000000000501",
  created_at: "2026-08-20T10:00:00Z",
};

const OTHERS_ATTACHMENT = {
  ...ATTACHMENT,
  asset_id: "00000000-0000-0000-0000-000000000302",
  page_id: "00000000-0000-0000-0000-000000000602",
  page_title: null,
  original_filename: "screenshot.jpg",
  media_type: "image/jpeg",
  bytes: 900_000,
  usage_state: "active" as const,
  current_published_reference_count: 1,
  is_orphan: false,
  is_mine: false,
  can_delete: false,
  usage_token: "token-302",
  uploaded_by_display_name: "MCP client",
  created_by_actor_kind: "mcp_client",
  created_by_actor_id: "00000000-0000-0000-0000-000000000701",
};

const ORPHAN_ROW = {
  asset_id: "00000000-0000-0000-0000-000000000303",
  workspace_id: "ws-1",
  page_id: "00000000-0000-0000-0000-000000000603",
  page_title: "Server-only view",
  uploaded_by_display_name: "Tester",
  original_filename: "stray.png",
  media_type: "image/png" as const,
  bytes: 4_096,
  association_state: "active" as const,
  created_by_actor_kind: "human",
  created_by_actor_id: "00000000-0000-0000-0000-000000000501",
  created_at: "2026-08-20T10:00:00Z",
  orphaned_since: "2026-08-20T11:00:00Z",
};

function page(items: unknown[], nextCursor: string | null = null) {
  return { items, nextCursor, scope: "mine" as const };
}

describe("AttachmentsSection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getCurrentWorkspaceId).mockResolvedValue("ws-1");
    vi.mocked(getApiBaseUrl).mockReturnValue("");
  });

  it("asks for only this uploader's files, and offers no All segment, below owner", async () => {
    // The endpoint answers 403 for scope=workspace below owner. Rendering the
    // segment anyway would put a control on screen whose only outcome is an
    // error banner.
    vi.mocked(listWorkspaceContextMediaAttachments).mockResolvedValue(
      page([ATTACHMENT]) as never,
    );
    render(<AttachmentsSection scope="workspace" workspaceId="ws-1" />);

    await waitFor(() =>
      expect(listWorkspaceContextMediaAttachments).toHaveBeenCalledWith(
        "ws-1",
        expect.objectContaining({ scope: "mine" }),
      ),
    );
    expect(
      screen.queryByRole("radio", { name: "All" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Mine" })).toBeInTheDocument();
  });

  it("lets an owner ask for the whole workspace and says what that excludes", async () => {
    vi.mocked(listWorkspaceContextMediaAttachments).mockResolvedValue(
      page([ATTACHMENT, OTHERS_ATTACHMENT]) as never,
    );
    render(
      <AttachmentsSection
        canViewEveryUpload
        scope="workspace"
        workspaceId="ws-1"
      />,
    );

    await waitFor(() =>
      expect(listWorkspaceContextMediaAttachments).toHaveBeenCalledWith(
        "ws-1",
        expect.objectContaining({ scope: "workspace" }),
      ),
    );
    // The P8.4 mask hides assets staged in another author's open draft even
    // from an owner. An "All" that silently is not all has to say so.
    expect(
      screen.getByText(/staged in someone else's unsaved draft/),
    ).toBeInTheDocument();
  });

  it("asks for orphan_only rather than usage=unused on the Unused filter", async () => {
    // These are not the same question: `unused` still includes assets held by
    // an open editing version, which are not reclaimable and must not be
    // offered up for cleanup.
    vi.mocked(listWorkspaceContextMediaAttachments).mockResolvedValue(
      page([ATTACHMENT]) as never,
    );
    render(<AttachmentsSection scope="workspace" workspaceId="ws-1" />);
    await screen.findByText("diagram.png");

    await userEvent.click(screen.getByRole("radio", { name: "Unused" }));

    await waitFor(() =>
      expect(listWorkspaceContextMediaAttachments).toHaveBeenLastCalledWith(
        "ws-1",
        expect.objectContaining({ orphanOnly: true, scope: "mine" }),
      ),
    );
    expect(
      vi.mocked(listWorkspaceContextMediaAttachments).mock.calls.at(-1)?.[1],
    ).not.toHaveProperty("usage");
  });

  it("keeps an owner's Unused filter workspace-wide", async () => {
    // Reclaiming storage is a workspace job. Narrowing Unused to the owner's
    // own uploads would make this screen answer less than the orphan screen it
    // replaces, which listed every uploader's orphans to an owner.
    vi.mocked(listWorkspaceContextMediaAttachments).mockResolvedValue(
      page([ATTACHMENT]) as never,
    );
    render(
      <AttachmentsSection
        canViewEveryUpload
        scope="workspace"
        workspaceId="ws-1"
      />,
    );
    await screen.findByText("diagram.png");

    await userEvent.click(screen.getByRole("radio", { name: "Unused" }));

    await waitFor(() =>
      expect(listWorkspaceContextMediaAttachments).toHaveBeenLastCalledWith(
        "ws-1",
        expect.objectContaining({ orphanOnly: true, scope: "workspace" }),
      ),
    );
  });

  it("names people and pages instead of printing their identifiers", async () => {
    vi.mocked(listWorkspaceContextMediaAttachments).mockResolvedValue(
      page([ATTACHMENT, OTHERS_ATTACHMENT]) as never,
    );
    render(
      <AttachmentsSection
        canViewEveryUpload
        scope="workspace"
        workspaceId="ws-1"
      />,
    );

    expect(await screen.findByText("Tester")).toBeInTheDocument();
    expect(screen.getByText("MCP client")).toBeInTheDocument();
    expect(
      screen.queryByText(OTHERS_ATTACHMENT.created_by_actor_id),
    ).not.toBeInTheDocument();

    const pageLink = screen.getByRole("link", { name: "Release Notes Draft" });
    expect(pageLink).toHaveAttribute(
      "href",
      `/context?target=${ATTACHMENT.page_id}`,
    );
  });

  it("points the thumbnail at the API origin, not the page origin", async () => {
    // The payload's content_url is root-relative. Whenever the app is not
    // served from the API origin -- a Vite dev server, a packaged shell aimed
    // at a remote API -- a bare relative src asks the wrong host and every
    // thumbnail paints as a broken-image glyph.
    vi.mocked(getApiBaseUrl).mockReturnValue("http://api.example.test");
    vi.mocked(listWorkspaceContextMediaAttachments).mockResolvedValue(
      page([ATTACHMENT]) as never,
    );
    render(<AttachmentsSection scope="workspace" workspaceId="ws-1" />);
    await screen.findByText("diagram.png");

    expect(document.querySelector("img.attachments-preview")).toHaveAttribute(
      "src",
      `http://api.example.test${ATTACHMENT.content_url}`,
    );
  });

  it("does not request a preview whose bytes the caller cannot read", async () => {
    // An unused upload was never published as content, so its content route
    // answers 404 to everyone but its uploader. Requesting it anyway paints
    // the browser's broken-image glyph, which reads as a corrupt file rather
    // than a private one. Only measured by rendering: nothing in the payload
    // says "this preview will 404".
    vi.mocked(listWorkspaceContextMediaAttachments).mockResolvedValue(
      page([
        { ...ATTACHMENT, is_orphan: true, is_mine: false },
        { ...OTHERS_ATTACHMENT, is_orphan: false },
      ]) as never,
    );
    render(
      <AttachmentsSection
        canViewEveryUpload
        scope="workspace"
        workspaceId="ws-1"
      />,
    );
    await screen.findByText("diagram.png");

    // One row is in use and previewable; the other is someone else's orphan.
    expect(document.querySelectorAll("img.attachments-preview")).toHaveLength(1);
    expect(
      document.querySelectorAll("span.attachments-preview-kind"),
    ).toHaveLength(1);
  });

  it("reports a small asset in bytes rather than rounding it up to a kilobyte", async () => {
    // A 70-byte stray rounded to "1 KB" looks like a real file worth keeping.
    vi.mocked(listWorkspaceContextMediaAttachments).mockResolvedValue(
      page([{ ...ATTACHMENT, bytes: 70 }]) as never,
    );
    render(<AttachmentsSection scope="workspace" workspaceId="ws-1" />);

    expect(await screen.findByText(/70 B/)).toBeInTheDocument();
    expect(screen.queryByText(/1 KB/)).not.toBeInTheDocument();
  });

  it("keeps the reduced admin-orphan endpoint for a server admin who is not a member", async () => {
    // The attachment endpoint is membership-gated. Routing this reader there
    // would 403 and take away a listing they can use today.
    vi.mocked(listAdminContextMediaOrphans).mockResolvedValue([
      ORPHAN_ROW,
    ] as never);
    render(<AttachmentsSection scope="server" workspaceId="ws-1" />);

    await waitFor(() =>
      expect(listAdminContextMediaOrphans).toHaveBeenCalledWith("ws-1"),
    );
    expect(listWorkspaceContextMediaAttachments).not.toHaveBeenCalled();
    expect(await screen.findByText("stray.png")).toBeInTheDocument();
    // No previews and no delete: that payload carries neither a content_url
    // nor a usage_token.
    expect(
      screen.queryByRole("button", { name: "Delete" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText(/not a member of this workspace/),
    ).toBeInTheDocument();
  });

  it("shows a reassuring empty state, not an error, when there is nothing to clean up", async () => {
    vi.mocked(listWorkspaceContextMediaAttachments).mockResolvedValue(
      page([]) as never,
    );
    render(<AttachmentsSection scope="workspace" workspaceId="ws-1" />);
    await screen.findByText("You have not uploaded anything yet");

    await userEvent.click(screen.getByRole("radio", { name: "Unused" }));

    expect(await screen.findByText("Nothing to clean up")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("deletes with the usage_token the row was listed with", async () => {
    vi.mocked(listWorkspaceContextMediaAttachments).mockResolvedValue(
      page([ATTACHMENT]) as never,
    );
    vi.mocked(deletePageContextMedia).mockResolvedValue({} as never);
    render(<AttachmentsSection scope="workspace" workspaceId="ws-1" />);
    await screen.findByText("diagram.png");

    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    await userEvent.click(
      screen.getByRole("button", { name: "Delete attachment" }),
    );

    await waitFor(() =>
      expect(deletePageContextMedia).toHaveBeenCalledWith(
        "ws-1",
        ATTACHMENT.asset_id,
        "token-301",
      ),
    );
  });

  it("explains a 409 as someone else using the file, not as a generic failure", async () => {
    // can_delete derives from viewer-MASKED usage, so a row referenced only by
    // another author's open draft looks free and answers 409. Unmasking it
    // would disclose that someone is editing; explaining it does not.
    vi.mocked(listWorkspaceContextMediaAttachments).mockResolvedValue(
      page([ATTACHMENT]) as never,
    );
    vi.mocked(deletePageContextMedia).mockRejectedValue(
      new ApiError(409, {
        error_code: "context_media_asset_in_use",
        message: "in use",
        request_id: "req-1",
        warnings: [],
      }),
    );
    render(<AttachmentsSection scope="workspace" workspaceId="ws-1" />);
    await screen.findByText("diagram.png");

    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    await userEvent.click(
      screen.getByRole("button", { name: "Delete attachment" }),
    );

    expect(
      await screen.findByText(/using this attachment right now/),
    ).toBeInTheDocument();
  });

  it("counts one file as one file", async () => {
    // The count chip used to be composed as "<n> <unit>" with a hardcoded
    // plural unit, so a single result read "1 files" -- and Korean got
    // "1 개", which puts a space where Korean does not.
    vi.mocked(listWorkspaceContextMediaAttachments).mockResolvedValue(
      page([ATTACHMENT]) as never,
    );
    render(<AttachmentsSection scope="workspace" workspaceId="ws-1" />);
    await screen.findByText("diagram.png");

    expect(screen.getByText("1 file")).toBeInTheDocument();
    expect(screen.queryByText("1 files")).not.toBeInTheDocument();
  });

  it("keeps keyboard focus inside the row when the confirmation opens and closes", async () => {
    // Measured on the running app before this was fixed: the trigger is
    // REPLACED by the panel, so the focused node leaves the document and
    // focus falls to <body>. A keyboard reader then has to tab from the top
    // of the page to reach "Delete attachment", and again to get back.
    vi.mocked(listWorkspaceContextMediaAttachments).mockResolvedValue(
      page([ATTACHMENT]) as never,
    );
    render(<AttachmentsSection scope="workspace" workspaceId="ws-1" />);
    await screen.findByText("diagram.png");

    const trigger = screen.getByRole("button", { name: "Delete" });
    await userEvent.click(trigger);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus(),
    );

    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Delete" })).toHaveFocus(),
    );
  });

  it("closes a stale confirmation when the list is refreshed", async () => {
    // The 412 copy tells the reader to refresh and try again. If Refresh left
    // the panel and its error standing, the advice would appear not to work --
    // and the panel would be pointed at a row the refetch may have replaced.
    vi.mocked(listWorkspaceContextMediaAttachments).mockResolvedValue(
      page([ATTACHMENT]) as never,
    );
    vi.mocked(deletePageContextMedia).mockRejectedValue(
      new ApiError(412, {
        error_code: "media_usage_token_stale",
        message: "stale",
        request_id: "req-2",
        warnings: [],
      }),
    );
    render(<AttachmentsSection scope="workspace" workspaceId="ws-1" />);
    await screen.findByText("diagram.png");

    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    await userEvent.click(
      screen.getByRole("button", { name: "Delete attachment" }),
    );
    expect(await screen.findByText(/Refresh and try again/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "Delete attachment" }),
      ).not.toBeInTheDocument(),
    );
    expect(screen.queryByText(/Refresh and try again/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete" })).toBeInTheDocument();
  });

  it("offers no delete control on a row the caller may not delete", async () => {
    vi.mocked(listWorkspaceContextMediaAttachments).mockResolvedValue(
      page([OTHERS_ATTACHMENT]) as never,
    );
    render(
      <AttachmentsSection
        canViewEveryUpload
        scope="workspace"
        workspaceId="ws-1"
      />,
    );
    await screen.findByText("screenshot.jpg");

    expect(
      screen.queryByRole("button", { name: "Delete" }),
    ).not.toBeInTheDocument();
  });

  it("appends the next page instead of replacing the loaded rows", async () => {
    vi.mocked(listWorkspaceContextMediaAttachments)
      .mockResolvedValueOnce(page([ATTACHMENT], "cursor-2") as never)
      .mockResolvedValueOnce(page([OTHERS_ATTACHMENT]) as never);
    render(
      <AttachmentsSection
        canViewEveryUpload
        scope="workspace"
        workspaceId="ws-1"
      />,
    );
    await screen.findByText("diagram.png");

    await userEvent.click(screen.getByRole("button", { name: "Load more" }));

    expect(await screen.findByText("screenshot.jpg")).toBeInTheDocument();
    expect(screen.getByText("diagram.png")).toBeInTheDocument();
    expect(listWorkspaceContextMediaAttachments).toHaveBeenLastCalledWith(
      "ws-1",
      expect.objectContaining({ cursor: "cursor-2" }),
    );
  });

  it("surfaces a load failure as a warning banner", async () => {
    vi.mocked(listWorkspaceContextMediaAttachments).mockRejectedValue(
      new Error("boom"),
    );
    render(<AttachmentsSection scope="workspace" workspaceId="ws-1" />);

    expect(
      await screen.findByText("Could not load attachments."),
    ).toBeInTheDocument();
  });

  it("enlarges a thumbnail and hands focus back when the viewer closes", async () => {
    vi.mocked(getApiBaseUrl).mockReturnValue("http://api.example.test");
    vi.mocked(listWorkspaceContextMediaAttachments).mockResolvedValue(
      page([ATTACHMENT]) as never,
    );
    render(<AttachmentsSection scope="workspace" workspaceId="ws-1" />);
    await screen.findByText("diagram.png");

    // The img keeps its tag and class; the button around it carries the name,
    // because the img itself is alt="" decoration.
    const trigger = screen.getByRole("button", { name: "Expand diagram.png" });
    expect(trigger.querySelector("img.attachments-preview")).not.toBeNull();

    await userEvent.click(trigger);
    const viewer = await screen.findByRole("dialog");
    expect(
      within(viewer).getByRole("img", { name: "diagram.png" }),
    ).toHaveAttribute(
      "src",
      `http://api.example.test${ATTACHMENT.content_url}`,
    );

    await userEvent.click(within(viewer).getByRole("button", { name: "Close preview" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(trigger).toHaveFocus();
  });

  it("offers no enlargement for a preview the caller cannot read", async () => {
    // The placeholder stands in for bytes this viewer is not allowed to fetch,
    // so there is nothing to enlarge. A focusable control that opens an empty
    // dialog would be worse than no control.
    vi.mocked(listWorkspaceContextMediaAttachments).mockResolvedValue(
      page([
        { ...ATTACHMENT, is_orphan: true, is_mine: false },
        { ...OTHERS_ATTACHMENT, is_orphan: false },
      ]) as never,
    );
    render(
      <AttachmentsSection canViewEveryUpload scope="workspace" workspaceId="ws-1" />,
    );
    await screen.findByText("diagram.png");

    // Both directions in one case: asserting only the absence would pass
    // even if the feature had never been built. The readable row must offer the
    // enlargement, and the private one must not.
    expect(document.querySelectorAll("span.attachments-preview-kind")).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: /Expand/ })).toHaveLength(1);
    expect(
      screen.queryByRole("button", { name: "Expand diagram.png" }),
    ).toBeNull();
  });
});

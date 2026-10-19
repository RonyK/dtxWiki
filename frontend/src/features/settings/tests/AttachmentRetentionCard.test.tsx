import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  AttachmentRetentionCard,
  RetentionNotice,
} from "../AttachmentRetentionCard";
import { ApiError, apiRequest } from "../../../shared/api/client";
import type { AttachmentRetentionPolicy } from "../../../shared/api/contextMediaAttachments";

vi.mock("../../../shared/api/client", async (original) => ({
  ...(await original<typeof import("../../../shared/api/client")>()),
  apiRequest: vi.fn(),
}));
const baseline: AttachmentRetentionPolicy = {
  orphan_retention_hours: null,
  effective_orphan_retention_hours: 168,
  default_orphan_retention_hours: 168,
  max_orphan_retention_hours: 876000,
  source: "server",
  revision: "a".repeat(64),
};
function Host({
  initial = baseline,
  owner = true,
  onReload = vi.fn(),
}: {
  initial?: AttachmentRetentionPolicy;
  owner?: boolean;
  onReload?: () => void;
}) {
  const [policy, setPolicy] = useState(initial);
  return (
    <AttachmentRetentionCard
      workspaceId="workspace-a"
      policy={policy}
      canEdit={owner}
      onApplied={setPolicy}
      onReload={onReload}
    />
  );
}
function success(hours: number | null) {
  const policy = {
    ...baseline,
    orphan_retention_hours: hours,
    effective_orphan_retention_hours: hours ?? 168,
    source: hours === null ? "server" : "workspace",
  };
  return {
    data: {
      attachments: {
        policy,
        revision: (hours === 0 ? "d" : hours === null ? "c" : "b").repeat(64),
      },
    },
  };
}

describe("attachment retention policy", () => {
  beforeEach(() => vi.clearAllMocks());

  it("keeps the applied period until save succeeds and cancels without saving", async () => {
    const user = userEvent.setup();
    render(<Host />);
    const period = screen.getByRole("spinbutton", { name: "Retention period" });
    await user.clear(period);
    await user.type(period, "14");
    expect(
      screen.getByText("Unused file retention: 7 days"),
    ).toBeInTheDocument();
    await user.click(
      screen.getByRole("button", { name: "Cancel policy changes" }),
    );
    expect(period).toHaveValue(7);
    expect(apiRequest).not.toHaveBeenCalled();
    await user.clear(period);
    await user.type(period, "14");
    vi.mocked(apiRequest).mockResolvedValue(success(336));
    await user.click(screen.getByRole("button", { name: "Save policy" }));
    await screen.findByText("Unused file retention: 14 days");
    expect(
      JSON.parse(vi.mocked(apiRequest).mock.calls[0][1]!.body as string),
    ).toEqual({
      group: "attachments",
      expected_revision: baseline.revision,
      changes: { orphan_retention_hours: 336 },
    });
  });

  it("requires confirmation for shortening, including restoring a shorter default", async () => {
    const user = userEvent.setup();
    render(
      <Host
        initial={{
          ...baseline,
          source: "workspace",
          orphan_retention_hours: 336,
          effective_orphan_retention_hours: 336,
        }}
      />,
    );
    await user.click(
      screen.getByRole("button", { name: "Use server default" }),
    );
    expect(apiRequest).not.toHaveBeenCalled();
    expect(
      screen.getByText("Unused file retention: 14 days"),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save policy" }));
    expect(screen.getByText(/Existing unused uploads/)).toBeInTheDocument();
    const cancel = screen
      .getAllByRole("button", { name: "Cancel policy changes" })
      .find((button) => !button.hasAttribute("disabled"))!;
    expect(cancel).toHaveFocus();
    await user.click(cancel);
    expect(apiRequest).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Save policy" }));
    vi.mocked(apiRequest).mockResolvedValue(success(null));
    await user.click(screen.getByRole("button", { name: "Save and apply" }));
    await screen.findByText("Unused file retention: 7 days");
    expect(
      JSON.parse(vi.mocked(apiRequest).mock.calls[0][1]!.body as string)
        .changes,
    ).toEqual({ orphan_retention_hours: null });
  });

  it("edits 36 hours without rounding, rejects fractions and keeps failed edits", async () => {
    const user = userEvent.setup();
    render(
      <Host
        initial={{
          ...baseline,
          source: "workspace",
          orphan_retention_hours: 36,
          effective_orphan_retention_hours: 36,
        }}
      />,
    );
    expect(
      screen.getByText("Unused file retention: 1 day 12 hours"),
    ).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Unit" })).toHaveValue("hours");
    const period = screen.getByRole("spinbutton");
    await user.clear(period);
    await user.type(period, "36.5");
    expect(screen.getByRole("button", { name: "Save policy" })).toBeDisabled();
    await user.clear(period);
    await user.type(period, "48");
    vi.mocked(apiRequest).mockRejectedValue(new TypeError("network"));
    await user.click(screen.getByRole("button", { name: "Save policy" }));
    await screen.findByRole("alert");
    expect(period).toHaveValue(48);
    expect(
      screen.getByText("Unused file retention: 1 day 12 hours"),
    ).toBeInTheDocument();
  });

  it("reloads a conflicting policy and never falls back to single-key writes", async () => {
    const user = userEvent.setup();
    const reload = vi.fn();
    render(<Host onReload={reload} />);
    await user.clear(screen.getByRole("spinbutton"));
    await user.type(screen.getByRole("spinbutton"), "14");
    vi.mocked(apiRequest).mockRejectedValue(
      new ApiError(409, {
        error_code: "settings_revision_conflict",
        message: "conflict",
        request_id: "test",
        warnings: [],
      }),
    );
    await user.click(screen.getByRole("button", { name: "Save policy" }));
    await waitFor(() => expect(reload).toHaveBeenCalledOnce());
    expect(apiRequest).toHaveBeenCalledTimes(1);
  });

  it("shows unknown metadata without enabling edits, and renders read-only policy for members", () => {
    const { rerender } = render(
      <AttachmentRetentionCard
        workspaceId="a"
        policy={null}
        canEdit
        onApplied={vi.fn()}
        onReload={vi.fn()}
      />,
    );
    expect(screen.getByRole("spinbutton")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save policy" })).toBeDisabled();
    rerender(<Host owner={false} />);
    expect(screen.getByText("Using server default")).toBeInTheDocument();
    expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
  });

  it.each([
    [168, "7 days"],
    [72, "3 days"],
    [36, "1 day 12 hours"],
    [12, "12 hours"],
  ])("displays %s hours exactly", (hours, duration) => {
    render(
      <RetentionNotice
        policy={{
          ...baseline,
          effective_orphan_retention_hours: hours as number,
        }}
      />,
    );
    expect(
      screen.getByText(`Unused file retention: ${duration}`),
    ).toBeInTheDocument();
  });
});

it("ignores a superseded save without unlocking a newer request", async () => {
  vi.mocked(apiRequest).mockReset();
  const user = userEvent.setup();
  const applied = vi.fn();
  let finishOld!: () => void;
  let finishNew!: () => void;
  vi.mocked(apiRequest)
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishOld = () => resolve(success(336) as never);
        }),
    )
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishNew = () => resolve(success(96) as never);
        }),
    );
  const view = render(
    <AttachmentRetentionCard
      workspaceId="a"
      policy={baseline}
      canEdit
      onApplied={applied}
      onReload={vi.fn()}
    />,
  );
  await user.clear(screen.getByRole("spinbutton"));
  await user.type(screen.getByRole("spinbutton"), "14");
  await user.click(screen.getByRole("button", { name: "Save policy" }));
  const newer = {
    ...baseline,
    orphan_retention_hours: 72,
    effective_orphan_retention_hours: 72,
    source: "workspace" as const,
    revision: "c".repeat(64),
  };
  view.rerender(
    <AttachmentRetentionCard
      workspaceId="a"
      policy={newer}
      canEdit
      onApplied={applied}
      onReload={vi.fn()}
    />,
  );
  await waitFor(() => expect(screen.getByRole("spinbutton")).toHaveValue(3));
  expect(screen.getByRole("spinbutton")).not.toBeDisabled();
  await user.clear(screen.getByRole("spinbutton"));
  await user.type(screen.getByRole("spinbutton"), "4");
  await user.click(screen.getByRole("button", { name: "Save policy" }));
  await act(async () => finishOld());
  expect(applied).not.toHaveBeenCalled();
  expect(screen.getByRole("spinbutton")).toBeDisabled();
  expect(screen.getByRole("button", { name: "Save policy" })).toBeDisabled();
  await act(async () => finishNew());
  expect(applied).toHaveBeenCalledOnce();
  expect(applied.mock.calls[0][0].effective_orphan_retention_hours).toBe(96);
});

it("disables reclamation explicitly and confirms enabling with a new exact hour period", async () => {
  vi.mocked(apiRequest).mockReset();
  const user = userEvent.setup();
  render(<Host />);
  await user.click(
    screen.getByRole("checkbox", { name: "Enable auto reclamation" }),
  );
  expect(screen.getByRole("spinbutton")).toBeDisabled();
  vi.mocked(apiRequest).mockResolvedValueOnce(success(0));
  await user.click(screen.getByRole("button", { name: "Save policy" }));
  await screen.findByText("Unused file auto reclamation is off");
  expect(
    JSON.parse(vi.mocked(apiRequest).mock.calls[0][1]!.body as string).changes,
  ).toEqual({ orphan_retention_hours: 0 });
  await waitFor(() => expect(screen.getByRole("spinbutton")).toHaveValue(null));
  await user.click(
    screen.getByRole("checkbox", { name: "Enable auto reclamation" }),
  );
  expect(screen.getByRole("button", { name: "Save policy" })).toBeDisabled();
  await user.selectOptions(screen.getByRole("combobox"), "hours");
  await user.clear(screen.getByRole("spinbutton"));
  await user.type(screen.getByRole("spinbutton"), "36");
  await user.click(screen.getByRole("button", { name: "Save policy" }));
  expect(apiRequest).toHaveBeenCalledTimes(1);
  expect(screen.getByText(/Existing unused uploads/)).toBeInTheDocument();
  vi.mocked(apiRequest).mockResolvedValueOnce(success(36));
  await user.click(screen.getByRole("button", { name: "Save and apply" }));
  await screen.findByText("Unused file retention: 1 day 12 hours");
  expect(
    JSON.parse(vi.mocked(apiRequest).mock.calls[1][1]!.body as string).changes,
  ).toEqual({ orphan_retention_hours: 36 });
});

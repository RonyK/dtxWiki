import {
  fireEvent,
  render as testingRender,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import {
  type ComponentProps,
  type ReactNode,
  useLayoutEffect,
  useRef,
} from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { invalidateWorkspaceListCache } from "../../../shared/api/workspace";
import {
  LibraryPolicyView,
  PolicyGroupView,
  ReviewAutoApproveByRiskSettings,
} from "../SettingsScreen";

// The generic policy cards, the Review auto-approval card and the Library
// upload card each keep an editable draft beside the server baseline and
// re-baseline it when the baseline changes. That reset used to run in a passive
// effect: one still pending when the first edit arrived (on mount too, where
// the draft already equals the baseline) was flushed after the edit had queued
// its update, and replaced the very edit it should have preceded. Save then
// sent only the field edited second. These tests deliver the first edit inside
// the commit, so the ordering no longer depends on the scheduler.

const WORKSPACE_ID = "11111111-1111-4111-8111-111111111111";

function ScopedRouter({ children }: { children: ReactNode }) {
  return (
    <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/settings?section=policies`]}>
      {children}
    </MemoryRouter>
  );
}

function render(ui: React.ReactElement) {
  return testingRender(ui, { wrapper: ScopedRouter });
}

type SaveHandler = ComponentProps<typeof PolicyGroupView>["onFieldsSave"];

function savedChanges(save: ReturnType<typeof vi.fn<SaveHandler>>) {
  expect(save).toHaveBeenCalledTimes(1);
  const [group, updates] = save.mock.calls[0]!;
  return {
    group,
    changes: Object.fromEntries(
      updates.map(({ field, value }) => [field.key, value]),
    ),
  };
}

// One user edit as the browser delivers it: the value goes in through the
// prototype setter, which React's value tracker cannot see, and a real
// bubbling `change` event follows. fireEvent is avoided on purpose, because
// its act() wrapper would change when React processes the update.
function userEdit(input: HTMLElement, value: string) {
  const setValue = Object.getOwnPropertyDescriptor(
    HTMLInputElement.prototype,
    "value",
  )!.set!;
  setValue.call(input, value);
  input.dispatchEvent(new Event("change", { bubbles: true }));
}

// Runs `edit` from a layout effect: the commit is in the DOM but none of its
// passive effects has run yet. That is the window a fast user, or a loaded CI
// box that runs React's passive-effect task late, gets to edit first, and
// dispatching from here makes it deterministic instead of a race against the
// scheduler. A new `edit` runs on the commit that carries it.
function EditDuringCommit({
  children,
  edit,
}: {
  children: ReactNode;
  edit: (host: HTMLElement) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    edit(host.current!);
  }, [edit]);
  return <div ref={host}>{children}</div>;
}

const noEdit = () => {};

describe("PolicyGroupView drafts", () => {
  const DEFAULT_LABEL = "Reference refresh interval";
  const MAX_LABEL = "Maximum reference reuse";

  function ttlFields(
    defaultSeconds: number,
    maxSeconds: number,
  ): ComponentProps<typeof PolicyGroupView>["fields"] {
    return [
      {
        key: "bundle_ttl_default_seconds",
        label: DEFAULT_LABEL,
        rawValue: defaultSeconds,
        value: String(defaultSeconds),
        editor: "duration",
      },
      {
        key: "bundle_ttl_max_seconds",
        label: MAX_LABEL,
        rawValue: maxSeconds,
        value: String(maxSeconds),
        editor: "duration",
      },
    ];
  }

  function ttlCard(
    defaultSeconds: number,
    maxSeconds: number,
    save: SaveHandler,
    edit: (host: HTMLElement) => void = noEdit,
  ) {
    const fields = ttlFields(defaultSeconds, maxSeconds);
    return (
      <EditDuringCommit edit={edit}>
        <PolicyGroupView
          busyKey={null}
          card={{
            name: "agent_injection",
            title: "Agent injection policy",
            revision: "e".repeat(64),
            policy: Object.fromEntries(
              fields.map((field) => [field.key, field.rawValue]),
            ),
          }}
          fields={fields}
          group="agent_injection"
          onFieldsSave={save}
          readOnly={false}
          saveNotice={null}
          summary="Reference reuse"
        />
      </EditDuringCommit>
    );
  }

  const spinbutton = (scope: HTMLElement, name: string) =>
    within(scope).getByRole("spinbutton", { name });

  it("keeps an edit made before the mount effects have run", () => {
    // The first TTL edit was erased by a reset effect that also ran on mount,
    // so Save sent only the maximum.
    const save = vi.fn<SaveHandler>(async () => false);
    const editDefault = (host: HTMLElement) =>
      userEdit(spinbutton(host, DEFAULT_LABEL), "2");

    const view = render(ttlCard(3600, 86400, save, editDefault));
    const card = within(view.container);

    expect(spinbutton(view.container, DEFAULT_LABEL)).toHaveDisplayValue("2");
    fireEvent.change(spinbutton(view.container, MAX_LABEL), {
      target: { value: "48" },
    });
    fireEvent.click(card.getByRole("button", { name: "Save changes" }));

    expect(savedChanges(save)).toEqual({
      group: "agent_injection",
      changes: {
        bundle_ttl_default_seconds: 7200,
        bundle_ttl_max_seconds: 172800,
      },
    });
  });

  it("keeps an edit made on the commit that carries a new baseline", () => {
    // The same race on the re-baseline path: a save (or another tab) hands the
    // card new values, and an edit lands before the reset for them has run.
    const save = vi.fn<SaveHandler>(async () => false);
    const editDefault = (host: HTMLElement) =>
      userEdit(spinbutton(host, DEFAULT_LABEL), "2");

    const view = render(ttlCard(3600, 86400, save));
    view.rerender(ttlCard(3600, 172800, save, editDefault));
    const card = within(view.container);

    expect(spinbutton(view.container, DEFAULT_LABEL)).toHaveDisplayValue("2");
    // The new baseline is adopted for the field that was not edited.
    expect(spinbutton(view.container, MAX_LABEL)).toHaveDisplayValue("48");
    fireEvent.click(card.getByRole("button", { name: "Save changes" }));

    expect(savedChanges(save)).toEqual({
      group: "agent_injection",
      changes: { bundle_ttl_default_seconds: 7200 },
    });
  });

  it("replaces an unsaved draft when the policy baseline changes", () => {
    const save = vi.fn<SaveHandler>(async () => false);
    const view = render(ttlCard(3600, 86400, save));
    const card = within(view.container);

    fireEvent.change(spinbutton(view.container, DEFAULT_LABEL), {
      target: { value: "2" },
    });
    expect(card.getAllByText("Unsaved changes").length).toBeGreaterThan(0);

    view.rerender(ttlCard(5400, 86400, save));

    expect(spinbutton(view.container, DEFAULT_LABEL)).toHaveDisplayValue("1");
    expect(
      spinbutton(view.container, `${DEFAULT_LABEL} Minutes`),
    ).toHaveDisplayValue("30");
    expect(card.queryByText("Unsaved changes")).not.toBeInTheDocument();
    expect(card.getByRole("button", { name: "Save changes" })).toBeDisabled();
  });

  it("keeps an unsaved draft when a parent rebuilds equivalent fields", () => {
    // Parents build `fields` anew on every render, so identity is not a
    // baseline change. Only different values are.
    const save = vi.fn<SaveHandler>(async () => false);
    const view = render(ttlCard(3600, 86400, save));
    const card = within(view.container);

    fireEvent.change(spinbutton(view.container, DEFAULT_LABEL), {
      target: { value: "2" },
    });
    view.rerender(ttlCard(3600, 86400, save));

    expect(spinbutton(view.container, DEFAULT_LABEL)).toHaveDisplayValue("2");
    expect(card.getByRole("button", { name: "Save changes" })).toBeEnabled();
  });
});

describe("ReviewAutoApproveByRiskSettings drafts", () => {
  const ACK_LABEL = /^Danger noted/;

  function reviewPolicyValues(lowDelayDays: number) {
    return {
      review_required: { from_risk: "medium" },
      automation: {
        auto_approve_by_risk: {
          low: { allowed: false, delay_days: lowDelayDays },
          medium: { allowed: false, delay_days: 14 },
          high: { allowed: false, delay_days: 28 },
        },
      },
    };
  }

  function reviewCard(
    lowDelayDays: number,
    save: SaveHandler,
    edit: (host: HTMLElement) => void = noEdit,
  ) {
    return (
      <EditDuringCommit edit={edit}>
          <ReviewAutoApproveByRiskSettings
            busyKey={null}
            onFieldsSave={save}
            policy={reviewPolicyValues(lowDelayDays)}
            readOnly={false}
            saveNotice={null}
          />
        </EditDuringCommit>
    );
  }

  const row = (scope: HTMLElement, risk: "low" | "medium" | "high") =>
    within(scope.querySelector<HTMLElement>(`#auto-approve-${risk}`)!);
  const delay = (scope: HTMLElement, risk: "low" | "medium" | "high") =>
    row(scope, risk).getByRole("spinbutton", { name: "Delay days" });
  const ack = (scope: HTMLElement) =>
    within(scope).getByRole("checkbox", { name: ACK_LABEL });

  it("keeps an edit made before the mount effects have run", () => {
    const save = vi.fn<SaveHandler>(async () => false);
    const editLow = (host: HTMLElement) => userEdit(delay(host, "low"), "3");

    const view = render(reviewCard(7, save, editLow));
    const card = within(view.container);

    expect(delay(view.container, "low")).toHaveDisplayValue("3");
    fireEvent.change(delay(view.container, "medium"), {
      target: { value: "21" },
    });
    fireEvent.click(card.getByRole("button", { name: "Save changes" }));

    expect(savedChanges(save)).toEqual({
      group: "review",
      changes: {
        "automation.auto_approve_by_risk.low.delay_days": 3,
        "automation.auto_approve_by_risk.medium.delay_days": 21,
      },
    });
  });

  it("keeps the danger acknowledgement ticked before the mount effects have run", () => {
    // The acknowledgement is reset by the same effect as the rows, and High
    // cannot be allowed until it is given.
    const save = vi.fn<SaveHandler>(async () => false);
    const tickAck = (host: HTMLElement) => ack(host).click();

    const view = render(reviewCard(7, save, tickAck));
    const card = within(view.container);

    expect(ack(view.container)).toBeChecked();
    const highAllowed = row(view.container, "high").getByRole("checkbox", {
      name: "High",
    });
    expect(highAllowed).toBeEnabled();
    fireEvent.click(highAllowed);
    fireEvent.click(card.getByRole("button", { name: "Save changes" }));

    expect(savedChanges(save)).toEqual({
      group: "review",
      changes: { "automation.auto_approve_by_risk.high.allowed": true },
    });
  });

  it("keeps an edit made on the commit that carries a new baseline", () => {
    const save = vi.fn<SaveHandler>(async () => false);
    const editMedium = (host: HTMLElement) =>
      userEdit(delay(host, "medium"), "21");

    const view = render(reviewCard(7, save));
    view.rerender(reviewCard(10, save, editMedium));
    const card = within(view.container);

    expect(delay(view.container, "medium")).toHaveDisplayValue("21");
    // The new baseline is adopted for the row that was not edited.
    expect(delay(view.container, "low")).toHaveDisplayValue("10");
    fireEvent.click(card.getByRole("button", { name: "Save changes" }));

    expect(savedChanges(save)).toEqual({
      group: "review",
      changes: { "automation.auto_approve_by_risk.medium.delay_days": 21 },
    });
  });

  it("resets the drafts and the danger acknowledgement together on a new baseline", () => {
    const save = vi.fn<SaveHandler>(async () => false);
    const view = render(reviewCard(7, save));
    const card = within(view.container);

    fireEvent.click(ack(view.container));
    fireEvent.click(
      row(view.container, "high").getByRole("checkbox", { name: "High" }),
    );
    fireEvent.change(delay(view.container, "low"), { target: { value: "3" } });
    expect(card.getByRole("button", { name: "Save changes" })).toBeEnabled();

    view.rerender(reviewCard(10, save));

    expect(delay(view.container, "low")).toHaveDisplayValue("10");
    expect(ack(view.container)).not.toBeChecked();
    expect(
      row(view.container, "high").getByRole("checkbox", { name: "High" }),
    ).not.toBeChecked();
    expect(card.getByRole("button", { name: "Save changes" })).toBeDisabled();
  });

  it("keeps an unsaved draft when the policy is rebuilt with the same values", () => {
    const save = vi.fn<SaveHandler>(async () => false);
    const view = render(reviewCard(7, save));
    const card = within(view.container);

    fireEvent.change(delay(view.container, "low"), { target: { value: "3" } });
    view.rerender(reviewCard(7, save));

    expect(delay(view.container, "low")).toHaveDisplayValue("3");
    expect(card.getByRole("button", { name: "Save changes" })).toBeEnabled();
  });
});

describe("LibraryPolicyView drafts", () => {
  const BASE = "https://wiki.example.test";
  const WORKSPACE_ID = "11111111-1111-4111-8111-111111111111";
  const CAPABILITIES_URL = `${BASE}/api/v1/workspaces/${WORKSPACE_ID}/library/capabilities`;
  const LIMIT_LABEL = "Manual upload limit";

  beforeEach(() => {
    vi.stubEnv("VITE_API_BASE_URL", BASE);
    window.localStorage.clear();
    invalidateWorkspaceListCache();
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = input.toString();
      const respond = (data: unknown) =>
        new Response(JSON.stringify({ data }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      if (url === `${BASE}/api/v1/workspaces`) {
        return respond({
          current_workspace_id: WORKSPACE_ID,
          workspaces: [
            {
              id: WORKSPACE_ID,
              slug: "default",
              title: "Default Workspace",
              name: "Default Workspace",
              goal: "Test workspace",
              default_language: "en",
              visibility: "private",
              role: "owner",
            },
          ],
        });
      }
      if (url === CAPABILITIES_URL) {
        return respond({
          policy_revision: "test-policy",
          capability_revision: "test-capabilities",
          max_files_per_batch: 20,
          formats: [],
        });
      }
      throw new Error(`Unexpected request: ${url}`);
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllEnvs();
  });

  // The Library baseline follows the identity of the card's policy object, as
  // the screen hands it one object per fetched policy, so a test that means
  // "the same policy" reuses the card and one that means a new policy builds a
  // new one. The revision stays put: a new one would also reload the
  // capabilities and leave a request in flight when the test ends.
  function libraryPolicyCard(maxUploadBytes: number) {
    return {
      name: "library",
      title: "Library policy",
      revision: "0".repeat(64),
      policy: {
        allowed_sources: [{ type: "manual_upload" }],
        denied_sources: [],
        max_upload_bytes: maxUploadBytes,
      },
    };
  }

  function libraryCard(
    card: ComponentProps<typeof LibraryPolicyView>["card"],
    save: ComponentProps<typeof LibraryPolicyView>["onFieldsSave"],
    edit: (host: HTMLElement) => void = noEdit,
  ) {
    return (
      <EditDuringCommit edit={edit}>
        <LibraryPolicyView
          busyKey={null}
          card={card}
          isServerAdmin={false}
          onFieldsSave={save}
          onOpenDeploymentSettings={() => {}}
          readOnly={false}
          saveNotice={null}
        />
      </EditDuringCommit>
    );
  }

  const limitOf = (scope: HTMLElement) =>
    within(scope).getByRole("spinbutton", { name: LIMIT_LABEL });

  // The controls stay disabled until the format capabilities have loaded.
  async function loadedLimit() {
    const limit = await screen.findByRole("spinbutton", { name: LIMIT_LABEL });
    await waitFor(() => expect(limit).toBeEnabled());
    return limit;
  }

  it("keeps an edit made on the commit that carries a new baseline", async () => {
    const save = vi.fn<
      ComponentProps<typeof LibraryPolicyView>["onFieldsSave"]
    >(async () => false);
    const editLimit = (host: HTMLElement) => userEdit(limitOf(host), "5");

    const view = render(libraryCard(libraryPolicyCard(2_000_000), save));
    await loadedLimit();
    view.rerender(libraryCard(libraryPolicyCard(3_000_000), save, editLimit));

    expect(limitOf(view.container)).toHaveDisplayValue("5");
    fireEvent.click(
      within(view.container).getByRole("button", { name: "Save changes" }),
    );

    expect(save).toHaveBeenCalledTimes(1);
    const [updates] = save.mock.calls[0]!;
    expect(
      Object.fromEntries(updates.map(({ field, value }) => [field.key, value])),
    ).toEqual({ max_upload_bytes: 5_000_000 });
  });

  it("resets the draft and the editor state together on a new baseline", async () => {
    const save = vi.fn<
      ComponentProps<typeof LibraryPolicyView>["onFieldsSave"]
    >(async () => false);
    const view = render(libraryCard(libraryPolicyCard(2_000_000), save));
    const card = within(view.container);
    const limit = await loadedLimit();

    fireEvent.change(limit, { target: { value: "5" } });
    fireEvent.change(card.getByLabelText("Add an individual format limit"), {
      target: { value: "pdf" },
    });
    expect(card.getByRole("button", { name: "Save changes" })).toBeEnabled();

    view.rerender(libraryCard(libraryPolicyCard(3_000_000), save));

    expect(limitOf(view.container)).toHaveDisplayValue("3");
    expect(
      card.getByLabelText("Add an individual format limit"),
    ).toHaveDisplayValue("");
    expect(card.getByRole("button", { name: "Save changes" })).toBeDisabled();
  });

  it("keeps an unsaved draft when the same card renders again", async () => {
    const save = vi.fn<
      ComponentProps<typeof LibraryPolicyView>["onFieldsSave"]
    >(async () => false);
    const policyCard = libraryPolicyCard(2_000_000);
    const view = render(libraryCard(policyCard, save));
    const card = within(view.container);
    const limit = await loadedLimit();

    fireEvent.change(limit, { target: { value: "5" } });
    view.rerender(libraryCard(policyCard, save));

    expect(limitOf(view.container)).toHaveDisplayValue("5");
    expect(card.getByRole("button", { name: "Save changes" })).toBeEnabled();
  });
});

import { fireEvent, render, within } from "@testing-library/react";
import { type ReactNode, useLayoutEffect, useRef } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { WorkspaceDangerZone } from "../SettingsScreen";

// The Danger Zone takes the active slug from the workspace context and falls
// back to the slug in the policy card while there is none.
const { workspaceContext } = vi.hoisted(() => ({
  workspaceContext: { current: null as Record<string, unknown> | null },
}));
vi.mock("../../../shared/workspace/WorkspaceContext", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useOptionalWorkspaceContext: () => workspaceContext.current,
}));

// The Danger Zone keeps the slug being typed in a draft beside the active slug
// and re-baselines it when the active slug changes. That reset used to run in a
// passive effect: one still pending when the first edit arrived (on mount too,
// where the draft already equals the slug) was flushed after the edit had
// queued its update, and replaced the very edit it should have preceded. The
// typed slug vanished and "Change slug" stayed disabled. These tests deliver
// the first edit inside the commit, so the ordering no longer depends on the
// scheduler (the same harness as PolicyGroupView.test.tsx).

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

const WORKSPACE_ID = "workspace-1";
const TYPED_SLUG = "research-space-v2";

function dangerZone(
  currentSlug: string,
  edit: (host: HTMLElement) => void = noEdit,
) {
  return (
    <MemoryRouter initialEntries={[`/workspaces/${WORKSPACE_ID}/settings?section=workspace`]}>
      <EditDuringCommit edit={edit}>
        <WorkspaceDangerZone
          currentSlug={currentSlug}
          onPoliciesChanged={() => {}}
          readOnly={false}
        />
      </EditDuringCommit>
    </MemoryRouter>
  );
}

const slugInput = (scope: HTMLElement) =>
  within(scope).getByRole("textbox", { name: "Workspace slug" });
const changeSlug = (scope: HTMLElement) =>
  within(scope).getByRole("button", { name: "Change slug" });
const typeSlug = (host: HTMLElement) => userEdit(slugInput(host), TYPED_SLUG);

beforeEach(() => {
  workspaceContext.current = null;
});

describe("WorkspaceDangerZone slug draft", () => {
  it("keeps a slug typed before the mount effects have run", () => {
    // The first edit was erased by a reset effect that also ran on mount.
    const view = render(dangerZone("research-space", typeSlug));

    expect(slugInput(view.container)).toHaveDisplayValue(TYPED_SLUG);
    expect(changeSlug(view.container)).toBeEnabled();
  });

  it("keeps a slug typed on the commit that carries a new active slug", () => {
    // The same race on the re-baseline path: the workspace is renamed, and an
    // edit lands before the reset for the new slug has run.
    const view = render(dangerZone("research-space"));
    view.rerender(dangerZone("research-space-renamed", typeSlug));

    expect(slugInput(view.container)).toHaveDisplayValue(TYPED_SLUG);
    expect(changeSlug(view.container)).toBeEnabled();
  });

  it("replaces an unsaved draft when the active slug changes", () => {
    const view = render(dangerZone("research-space"));

    fireEvent.change(slugInput(view.container), {
      target: { value: TYPED_SLUG },
    });
    expect(changeSlug(view.container)).toBeEnabled();

    view.rerender(dangerZone("research-space-renamed"));

    expect(slugInput(view.container)).toHaveDisplayValue(
      "research-space-renamed",
    );
    expect(changeSlug(view.container)).toBeDisabled();
  });

  it("keeps an unsaved draft when the same slug renders again", () => {
    // Parents render again for reasons that have nothing to do with the slug,
    // and only a different slug is a new baseline.
    const view = render(dangerZone("research-space"));

    fireEvent.change(slugInput(view.container), {
      target: { value: TYPED_SLUG },
    });
    view.rerender(dangerZone("research-space"));

    expect(slugInput(view.container)).toHaveDisplayValue(TYPED_SLUG);
    expect(changeSlug(view.container)).toBeEnabled();
  });
});

describe("WorkspaceDangerZone slug draft with a workspace context", () => {
  function contextAt(slug: string) {
    return {
      selectedWorkspace: {
        id: "workspace-1",
        slug,
        title: "Research Space",
        role: "owner",
      },
    };
  }

  it("follows the slug of the workspace rather than the one in the policy", () => {
    // The workspace list and the policy card refresh separately, so the slug of
    // the workspace is the baseline whenever there is one.
    workspaceContext.current = contextAt("research-space");
    const view = render(dangerZone(""));
    expect(slugInput(view.container)).toHaveDisplayValue("research-space");

    fireEvent.change(slugInput(view.container), {
      target: { value: TYPED_SLUG },
    });
    workspaceContext.current = contextAt("research-space-renamed");
    view.rerender(dangerZone(""));

    expect(slugInput(view.container)).toHaveDisplayValue(
      "research-space-renamed",
    );
    expect(changeSlug(view.container)).toBeDisabled();
  });

  it("keeps an unsaved draft while only the policy slug changes", () => {
    // A save hands the policy card its new slug before the workspace list has
    // been refreshed, and the draft is not re-baselined until the slug of the
    // workspace itself changes.
    workspaceContext.current = contextAt("research-space");
    const view = render(dangerZone("research-space"));

    fireEvent.change(slugInput(view.container), {
      target: { value: TYPED_SLUG },
    });
    view.rerender(dangerZone(TYPED_SLUG));

    expect(slugInput(view.container)).toHaveDisplayValue(TYPED_SLUG);
    expect(changeSlug(view.container)).toBeEnabled();
  });
});

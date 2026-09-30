import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { buildSplitRows, DiffViewer } from "../DiffViewer";

// jsdom never applies authored stylesheets, so getComputedStyle cannot tell
// us whether a className actually hides its text. Instead, read the real
// CSS source and confirm the rendered className resolves to a rule that
// clips its content -- this fails if the component references a class name
// (e.g. "sr-only") that has no such rule anywhere in styles/*.css, which is
// exactly the defect this test exists to pin.
const stylesDir = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../../styles",
);
const allCss = readdirSync(stylesDir)
  .filter((name) => name.endsWith(".css"))
  .map((name) => readFileSync(path.join(stylesDir, name), "utf-8"))
  .join("\n");

function isVisuallyHiddenClass(className: string): boolean {
  const rule = new RegExp(`\\.${className}\\s*\\{[^}]*\\}`, "s").exec(allCss);
  if (!rule) return false;
  return /overflow\s*:\s*hidden/.test(rule[0]) && /clip(-path)?\s*:/.test(rule[0]);
}

const diff = {
  from_version_no: 1,
  to_version_no: 2,
  lines: ["--- v1", "+++ v2", "@@ -1,3 +1,4 @@", " unchanged", "-Old", "+New", "+Added"],
  additions: 2,
  deletions: 1,
};

describe("DiffViewer", () => {
  it("renders split diff by default with paired before and after cells", () => {
    render(<DiffViewer diff={diff} />);

    const viewer = screen.getByLabelText("Diff v1 to v2");
    expect(within(viewer).getByText("v1 -> v2")).toBeInTheDocument();
    expect(within(viewer).getByText("+2 / -1")).toBeInTheDocument();
    expect(within(viewer).getByRole("table", { name: "Side-by-side diff" }))
      .toBeInTheDocument();
    // Default wording stays neutral: a caller that does not normalize its
    // pair must not have the viewer claim which side is older.
    expect(within(viewer).getByText("Previous")).toBeInTheDocument();
    expect(within(viewer).getByText("Current")).toBeInTheDocument();
    expect(within(viewer).getByText("Old")).toHaveClass("diff-viewer-cell-del");
    expect(within(viewer).getByText("New")).toHaveClass("diff-viewer-cell-add");
    expect(within(viewer).getByText("Added")).toHaveClass("diff-viewer-cell-add");
    expect(within(viewer).queryByText("−")).toBeNull();
  });

  it("can add visible and accessible non-colour cues without changing the default", () => {
    render(<DiffViewer diff={diff} showChangeMarkers />);

    const removedCell = screen.getByText("Old").closest("code");
    const addedCell = screen.getByText("New").closest("code");
    expect(removedCell).not.toBeNull();
    expect(addedCell).not.toBeNull();
    if (!removedCell || !addedCell) throw new Error("Diff cells were not rendered");
    expect(within(removedCell).getByText("−")).toHaveAttribute("aria-hidden", "true");
    const removedLabel = within(removedCell).getByText(/Removed line/);
    expect(within(addedCell).getByText("+")).toHaveAttribute("aria-hidden", "true");
    const addedLabel = within(addedCell).getByText(/Added line/);
    // Pin the actual defect: the label's class must be a real, defined CSS
    // rule that visually hides it -- not merely a plausible-looking name.
    for (const label of [removedLabel, addedLabel]) {
      const hidingClass = Array.from(label.classList).find(isVisuallyHiddenClass);
      expect(hidingClass, `expected ${label.className} to resolve to a hiding rule`)
        .toBeDefined();
    }
  });

  it("accepts endpoint labels and marks only the published endpoint", () => {
    render(
      <DiffViewer
        baseEndpointLabel="Older v1"
        comparedEndpointLabel="Newer v2"
        diff={diff}
        publishedEndpoint="to"
        publishedVersionLabel="Current published v2 · visible to readers"
      />,
    );

    expect(screen.getByText("Current published v2 · visible to readers"))
      .toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: /Older v1/ }))
      .not.toHaveAttribute("data-current-published");
    expect(screen.getByRole("columnheader", { name: /Newer v2/ }))
      .toHaveAttribute("data-current-published", "true");
  });

  it("explains an empty body diff instead of rendering a blank panel", () => {
    // Reachable whenever two different versions differ only in metadata, so the
    // copy has to be translatable rather than a hardcoded English sentence.
    render(
      <DiffViewer
        diff={{
          from_version_no: 2,
          to_version_no: 3,
          lines: [],
          additions: 0,
          deletions: 0,
        }}
      />,
    );

    const viewer = screen.getByLabelText("Diff v2 to v3");
    expect(within(viewer).getByText("No body changes between these versions."))
      .toBeInTheDocument();
    // No view-mode toggle when there is nothing to toggle between.
    expect(within(viewer).queryByRole("button", { name: "Split" })).toBeNull();
    expect(within(viewer).queryByRole("button", { name: "Unified" })).toBeNull();
  });

  it("distinguishes metadata-only changes from identical versions", () => {
    const metadataDiff = {
      from_version_no: 2,
      to_version_no: 3,
      lines: [],
      additions: 0,
      deletions: 0,
      metadata_changes: ["title", "tags"],
    };
    render(<DiffViewer diff={metadataDiff} />);

    expect(
      screen.getByText(
        "Page metadata changed between these versions; the body did not.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("No body changes between these versions."),
    ).toBeNull();
  });

  it("switches to unified source lines without losing diff signs", async () => {
    render(<DiffViewer diff={diff} />);

    const viewer = screen.getByLabelText("Diff v1 to v2");
    await userEvent.click(within(viewer).getByRole("button", { name: "Unified" }));

    expect(within(viewer).getByText("-Old")).toHaveClass("diff-line-del");
    expect(within(viewer).getByText("+New")).toHaveClass("diff-line-add");
  });

  it("builds stable split rows from unified diff file headers", () => {
    expect(buildSplitRows(diff.lines)).toEqual([
      { id: "hunk-2", kind: "hunk", label: "@@ -1,3 +1,4 @@" },
      {
        id: "context-3",
        kind: "line",
        oldKind: "context",
        oldText: "unchanged",
        newKind: "context",
        newText: "unchanged",
      },
      {
        id: "change-4-0",
        kind: "line",
        oldKind: "del",
        oldText: "Old",
        newKind: "add",
        newText: "New",
      },
      {
        id: "change-4-1",
        kind: "line",
        oldKind: "empty",
        oldText: "",
        newKind: "add",
        newText: "Added",
      },
    ]);
  });
});

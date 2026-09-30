import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { DiffViewerDialog } from "../DiffViewerDialog";
import type { DiffViewerDiff } from "../DiffViewer";

const diff: DiffViewerDiff = {
  from_version_no: 1,
  to_version_no: 2,
  lines: ["--- v1", "+++ v2", "@@ -1 +1 @@", "-Old", "+New"],
  additions: 1,
  deletions: 1,
};

describe("DiffViewerDialog", () => {
  it("shows accessible spinner while loading is true", () => {
    render(
      <DiffViewerDialog
        diff={null}
        loading={true}
        onClose={vi.fn()}
        title="Compare v1 with current"
      />,
    );

    const status = screen.getByRole("status", { name: "Loading diff" });
    expect(status).toBeInTheDocument();
    expect(status).toHaveClass("spinner");
  });

  it("omits the spinner once loading finishes", () => {
    render(
      <DiffViewerDialog
        diff={diff}
        loading={false}
        onClose={vi.fn()}
        title="Compare v1 with current"
      />,
    );

    expect(screen.queryByRole("status", { name: "Loading diff" })).toBeNull();
  });

  it("marks the restore button as busy while restoring", () => {
    const onClose = vi.fn();
    render(
      <DiffViewerDialog
        diff={diff}
        loading={false}
        onClose={onClose}
        onRestore={vi.fn()}
        restoreDisabled={true}
        title="Compare v1 with current"
      />,
    );

    const button = screen.getByRole("button", { name: "Restoring" });
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(button).toBeDisabled();

    const headerClose = screen.getByRole("button", { name: "Close diff" });
    expect(headerClose).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Close" })).toBeNull();
    headerClose.removeAttribute("disabled");
    fireEvent.click(headerClose);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("leaves aria-busy off when restore is idle", () => {
    render(
      <DiffViewerDialog
        diff={diff}
        loading={false}
        onClose={vi.fn()}
        onRestore={vi.fn()}
        restoreDisabled={false}
        title="Compare v1 with current"
      />,
    );

    const button = screen.getByRole("button", { name: "Restore this version" });
    expect(button).toHaveAttribute("aria-busy", "false");
  });
});

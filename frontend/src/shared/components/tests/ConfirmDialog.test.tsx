import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ConfirmDialog } from "../ConfirmDialog";

describe("ConfirmDialog", () => {
  it("asks one question and blocks every way out while busy", async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    const onConfirm = vi.fn();
    const dialog = (props: { busy?: boolean; confirmDisabled?: boolean }) => (
      <ConfirmDialog
        busyLabel="Deleting"
        confirmLabel="Delete"
        error="Could not delete"
        labelledBy="confirm-title"
        onCancel={onCancel}
        onConfirm={onConfirm}
        title="Delete this?"
        tone="danger"
        {...props}
      >
        <p>Details</p>
      </ConfirmDialog>
    );
    const { rerender } = render(dialog({ confirmDisabled: true }));

    expect(screen.getByRole("dialog", { name: "Delete this?" })).toHaveTextContent("Details");
    expect(screen.getByRole("alert")).toHaveTextContent("Could not delete");
    const confirm = screen.getByRole("button", { name: "Delete" });
    expect(confirm).toHaveClass("danger");
    expect(confirm).toBeDisabled();

    rerender(dialog({}));
    await user.click(confirm);
    expect(onConfirm).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledTimes(1);

    rerender(dialog({ busy: true }));
    expect(screen.getByRole("button", { name: "Deleting" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    await user.keyboard("{Escape}");
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});

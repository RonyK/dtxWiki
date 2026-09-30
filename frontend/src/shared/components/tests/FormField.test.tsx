import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { FormField, joinIds } from "../FormField";

describe("FormField", () => {
  it("links the label to the field and renders hint and error text", () => {
    render(
      <FormField
        className="custom-field"
        error="Use an http or https URL."
        errorId="cover-error"
        hint="Shown at the top of the page."
        hintId="cover-hint"
        htmlFor="cover-url"
        label="Cover URL"
      >
        <input id="cover-url" />
      </FormField>,
    );

    expect(screen.getByLabelText("Cover URL")).toBeInTheDocument();
    expect(screen.getByText("Shown at the top of the page.")).toHaveAttribute(
      "id",
      "cover-hint",
    );
    expect(screen.getByText("Use an http or https URL.")).toHaveAttribute(
      "id",
      "cover-error",
    );
    expect(screen.getByText("Cover URL").closest(".form-field")).toHaveClass(
      "custom-field",
    );
  });

  it("renders a non-labelling text label when no htmlFor is provided", () => {
    render(
      <FormField label="Tags">
        <div role="group" aria-label="Tags" />
      </FormField>,
    );

    expect(screen.getByText("Tags")).toHaveClass("form-field-label");
    expect(screen.getByRole("group", { name: "Tags" })).toBeInTheDocument();
  });

  it("forwards hintId and errorId to a render-prop child", () => {
    render(
      <FormField
        error="Use an http or https URL."
        errorId="cover-error"
        hint="Shown at the top of the page."
        hintId="cover-hint"
        htmlFor="cover-url"
        label="Cover URL"
      >
        {(ids) => (
          <input id="cover-url" aria-describedby={joinIds(ids.hintId, ids.errorId)} />
        )}
      </FormField>,
    );

    expect(screen.getByLabelText("Cover URL")).toHaveAttribute(
      "aria-describedby",
      "cover-hint cover-error",
    );
  });

  it("omits hint/error ids when hint or error text is absent", () => {
    render(
      <FormField
        errorId="unused-error"
        hintId="unused-hint"
        htmlFor="field"
        label="Plain"
      >
        {(ids) => {
          const describedBy = joinIds(ids.hintId, ids.errorId);
          return (
            <input
              id="field"
              aria-describedby={describedBy}
              data-described-by={describedBy ?? ""}
            />
          );
        }}
      </FormField>,
    );

    expect(screen.getByLabelText("Plain")).not.toHaveAttribute(
      "aria-describedby",
    );
  });

  it("joinIds filters empties and returns undefined when nothing remains", () => {
    expect(joinIds("a", undefined, "b")).toBe("a b");
    expect(joinIds(undefined, "only")).toBe("only");
    expect(joinIds(undefined, false, null)).toBeUndefined();
    expect(joinIds()).toBeUndefined();
  });
});

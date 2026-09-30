import { describe, expect, it } from "vitest";

import {
  snapshotStableCustomOwners,
  stableCustomNameOwnerConflict,
  stableCustomOwnerConflict,
  withStableBuiltinExtensionOwners,
} from "../customFormatOwnership";

describe("stable custom-format ownership", () => {
  const owners = snapshotStableCustomOwners([{
    format_id: "custom-archive",
    display_name: "  Project   Archive ",
    extensions: ["FOO", ".bar"],
  }]);

  it("keeps normalized extension and display-name owners stable", () => {
    expect(owners.extensions.get(".foo")).toEqual({
      displayName: "  Project   Archive ",
      formatId: "custom-archive",
    });
    expect(owners.displayNames.get("project archive")).toEqual({
      displayName: "  Project   Archive ",
      formatId: "custom-archive",
    });
  });

  it("allows the stable owner but blocks a new or different custom identity", () => {
    expect(stableCustomOwnerConflict("foo", "custom-archive", owners.extensions)).toBeNull();
    expect(stableCustomOwnerConflict(".foo", "custom-other", owners.extensions))
      .toMatchObject({ formatId: "custom-archive" });
    expect(stableCustomOwnerConflict(".foo", null, owners.extensions))
      .toMatchObject({ formatId: "custom-archive" });
    expect(stableCustomNameOwnerConflict(
      " project archive ",
      "custom-archive",
      owners.displayNames,
    )).toBeNull();
    expect(stableCustomNameOwnerConflict(
      "PROJECT  ARCHIVE",
      "custom-other",
      owners.displayNames,
    ))
      .toMatchObject({ formatId: "custom-archive" });
    expect(stableCustomNameOwnerConflict("Project Archive", null, owners.displayNames))
      .toMatchObject({ formatId: "custom-archive" });
  });

  it("retains persisted built-in extension owners across a pending release", () => {
    const combined = withStableBuiltinExtensionOwners(owners, [{
      display_name: "Word DOCX",
      extensions: [".docx", ".word"],
      format_id: "docx",
      search_aliases: [],
    }]);

    expect(stableCustomOwnerConflict(".word", "docx", combined.extensions)).toBeNull();
    expect(stableCustomOwnerConflict(".word", "custom-office", combined.extensions))
      .toEqual({ displayName: "Word DOCX", formatId: "docx" });
  });
});

import { describe, expect, it } from "vitest";

import {
  parseAliasGroupInput,
  serializeAliasGroups,
} from "../aliasGroupInput";

describe("Library format alias-group input", () => {
  it("turns a plain extension list into one group named from its first extension", () => {
    expect(parseAliasGroupInput(" .FOO, boo ")).toEqual({
      ok: true,
      explicit: false,
      groups: [{ label: "foo", extensions: [".foo", ".boo"] }],
    });
  });

  it("parses several explicit groups without splitting commas inside parentheses", () => {
    expect(parseAliasGroupInput("[ First alias ](A1, .A2), [Second](b1,b2)")).toEqual({
      ok: true,
      explicit: true,
      groups: [
        { label: "First alias", extensions: [".a1", ".a2"] },
        { label: "Second", extensions: [".b1", ".b2"] },
      ],
    });
  });

  it("supports escaped closing brackets in labels and serializes canonical text", () => {
    const parsed = parseAliasGroupInput(String.raw`[Data \] archive](RAW,raw_alt)`);
    expect(parsed).toEqual({
      ok: true,
      explicit: true,
      groups: [{ label: "Data ] archive", extensions: [".raw", ".raw_alt"] }],
    });
    if (!parsed.ok) throw new Error("expected valid alias groups");
    expect(serializeAliasGroups(parsed.groups))
      .toBe(String.raw`[Data \] archive](.raw,.raw_alt)`);
  });

  it("normalizes serializer labels and extensions", () => {
    expect(serializeAliasGroups([
      { label: "  Report   files ", extensions: ["REPORT", " .Rpt "] },
      { label: "Backup", extensions: ["bak"] },
    ])).toBe("[Report files](.report,.rpt), [Backup](.bak)");
  });

  it("counts normalized alias labels as code points at the 80/81 emoji boundary", () => {
    const validLabel = "😀".repeat(80);
    expect(parseAliasGroupInput(`[  ${validLabel}  ](.emoji)`)).toMatchObject({
      ok: true,
      groups: [{ label: validLabel, extensions: [".emoji"] }],
    });
    expect(parseAliasGroupInput(`[${validLabel}😀](.emoji)`)).toMatchObject({
      ok: false,
      error: { code: "invalid_label" },
    });
  });

  it.each([
    ["", "empty_input"],
    [".foo,.FOO", "duplicate_extension"],
    ["foo,", "invalid_extension"],
    [".not.valid", "invalid_extension"],
    ["[One](.a), [one](.b)", "duplicate_label"],
    ["[One](.a), [Two](A)", "duplicate_extension"],
    ["[One]()", "invalid_extension"],
    ["[One](.a", "invalid_syntax"],
    ["[One](.a), .b", "invalid_syntax"],
    [".a, [Two](.b)", "invalid_syntax"],
    ["[One]((.a))", "invalid_syntax"],
  ])("rejects %j atomically with %s", (value, code) => {
    expect(parseAliasGroupInput(value)).toMatchObject({
      ok: false,
      error: { code },
    });
  });

  it("rejects a group with more than the custom-format alias limit", () => {
    const extensions = Array.from({ length: 17 }, (_, index) => `.x${index}`).join(",");
    expect(parseAliasGroupInput(`[Many](${extensions})`)).toMatchObject({
      ok: false,
      error: { code: "too_many_extensions", groupIndex: 0 },
    });
  });

  it("returns no partial groups after a later explicit group fails", () => {
    const result = parseAliasGroupInput("[Good](.good), [Bad](.not.valid)");
    expect(result).toMatchObject({
      ok: false,
      error: { code: "invalid_extension", groupIndex: 1 },
    });
    expect("groups" in result).toBe(false);
  });
});

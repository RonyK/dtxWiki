import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { canonicalizeMarkdown, contentHash } from "./contentHash";

type Fixture = {
  server_assigned_frontmatter_fields: string[];
  vectors: Array<{
    id: string;
    input: string;
    expected_hex: string;
    equivalent_inputs?: string[];
  }>;
};

const fixture = JSON.parse(
  readFileSync("../shared/fixtures/content_hash_vectors.json", "utf-8"),
) as Fixture;

describe("contentHash", () => {
  it("matches the shared backend vectors", async () => {
    for (const vector of fixture.vectors) {
      await expect(contentHash(vector.input), vector.id).resolves.toBe(
        vector.expected_hex,
      );
      for (const input of vector.equivalent_inputs ?? []) {
        await expect(contentHash(input), vector.id).resolves.toBe(
          vector.expected_hex,
        );
      }
    }
  });

  it("exposes canonical text for debugging", () => {
    expect(canonicalizeMarkdown("a   \r\nb\t\n\n")).toBe("a\nb");
  });

  it("returns null instead of a fake digest when Web Crypto is unavailable", async () => {
    // Insecure contexts (plain-HTTP LAN deploys) have no crypto.subtle. The
    // contract is null — never a fabricated 64-hex value that could be compared
    // against the backend SHA-256 and produce a guaranteed mismatch.
    const originalCrypto = globalThis.crypto;
    Object.defineProperty(globalThis, "crypto", {
      configurable: true,
      value: {},
    });
    try {
      await expect(contentHash("# Draft")).resolves.toBeNull();
    } finally {
      Object.defineProperty(globalThis, "crypto", {
        configurable: true,
        value: originalCrypto,
      });
    }
  });
});

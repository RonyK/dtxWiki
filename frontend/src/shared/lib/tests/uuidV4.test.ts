import { afterEach, describe, expect, it, vi } from "vitest";

import { createUuidV4 } from "../uuidV4";

describe("createUuidV4", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("formats getRandomValues bytes as an RFC 9562 UUIDv4", () => {
    const getRandomValues = vi.fn((bytes: Uint8Array) => {
      bytes.set(Array.from({ length: 16 }, (_, index) => index));
      return bytes;
    });

    expect(createUuidV4({ getRandomValues } as Pick<Crypto, "getRandomValues">))
      .toBe("00010203-0405-4607-8809-0a0b0c0d0e0f");
    expect(getRandomValues).toHaveBeenCalledOnce();
  });

  it("fails closed when getRandomValues is unavailable", () => {
    vi.stubGlobal("crypto", undefined);

    expect(() => createUuidV4()).toThrow(
      "Secure random UUID generation is unavailable.",
    );
  });
});

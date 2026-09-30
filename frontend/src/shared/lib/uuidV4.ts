type RandomValuesSource = Pick<Crypto, "getRandomValues">;

/**
 * Generates an RFC 9562 UUIDv4 from the Web Crypto random source that remains
 * available to browsers on plain-HTTP LAN origins.
 *
 * Do not add a Math.random fallback here. Upload operation IDs are retained
 * across retries as idempotency identities and must fail closed when a secure
 * random source is unavailable.
 */
export function createUuidV4(
  randomSource: RandomValuesSource | undefined = globalThis.crypto,
) {
  if (!randomSource || typeof randomSource.getRandomValues !== "function") {
    throw new Error("Secure random UUID generation is unavailable.");
  }

  const bytes = new Uint8Array(16);
  randomSource.getRandomValues(bytes);

  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;

  const hex = Array.from(
    bytes,
    (byte) => byte.toString(16).padStart(2, "0"),
  ).join("");

  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20),
  ].join("-");
}

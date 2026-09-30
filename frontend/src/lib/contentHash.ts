// Canonicalization must stay byte-identical to backend/utils/content_hash.py.
// The shared golden vectors in shared/fixtures/content_hash_vectors.json are
// what hold the two implementations together; both sides consume them.
//
// This used to also strip `version_no`/`last_modified_at` lines out of a
// leading `---` block, mirroring the backend. That exclusion was removed on
// both sides: no write path ever produced server-stamped frontmatter, so it
// defended against a shape the system never emits while making two bodies that
// differ only in such a line hash identically -- which silently defeated every
// stale-write check built on this digest. A `---` block here is caller-authored
// content (imported Jekyll/Hugo/Obsidian markdown conventionally opens with
// one), so it must be hashed like any other content.
export function canonicalizeMarkdown(markdown: string): string {
  return markdown
    .normalize("NFC")
    .replace(/\r\n/g, "\n")
    .replace(/\r/g, "\n")
    .split("\n")
    .map((line) => line.replace(/[ \t]+$/g, ""))
    .join("\n")
    .replace(/\n+$/g, "");
}

// Returns the canonical SHA-256 digest matching backend/utils/content_hash.py,
// or null when SubtleCrypto is unavailable (insecure contexts, e.g. plain-HTTP
// LAN deployments). Callers must treat null as "no client-side hash": skip the
// stale-write comparison rather than fabricating a digest that can never match
// the server's.
export async function contentHash(markdown: string): Promise<string | null> {
  if (!globalThis.crypto?.subtle?.digest) {
    return null;
  }
  const bytes = new TextEncoder().encode(canonicalizeMarkdown(markdown));
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

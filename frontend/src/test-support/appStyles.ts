import { readFileSync } from "node:fs";
import { join } from "node:path";

// The app's stylesheet was split from a single file into ordered @import
// partials (src/styles/index.css is now just the @import list). Static CSS
// regression tests assert on the *bundled* stylesheet, so this reads the
// partials in import order and concatenates them exactly as Vite/PostCSS
// inlines them — byte-identical to the pre-split single file, which keeps
// index-based ordering assertions valid.
export function readAppStyles(
  stylesDir = join(process.cwd(), "src/styles"),
): string {
  const index = readFileSync(join(stylesDir, "index.css"), "utf-8");
  const partials = [...index.matchAll(/@import\s+"\.\/([^"]+)"/g)].map(
    (match) => match[1],
  );
  return partials
    .map((file) => readFileSync(join(stylesDir, file), "utf-8"))
    .join("\n");
}

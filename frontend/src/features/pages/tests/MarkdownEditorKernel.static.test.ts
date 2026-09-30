import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

describe("shared Markdown editor kernel architecture", () => {
  it("keeps CodeMirror behind the neutral shared source surface", () => {
    const kernelSource = readFileSync(
      join(process.cwd(), "src/features/pages/MarkdownEditorKernel.tsx"),
      "utf-8",
    );

    expect(kernelSource).toContain("export function MarkdownSourceEditor");
    expect(kernelSource).toContain("<CodeMirror");
    expect(kernelSource).toContain("<MarkdownSourceEditor");
  });

  it("routes Context and Library through the same shared module", () => {
    const pageEditorSource = readFileSync(
      join(process.cwd(), "src/features/pages/PageEditor.tsx"),
      "utf-8",
    );
    const libraryEditorSource = readFileSync(
      join(
        process.cwd(),
        "src/features/library/LibrarySemanticMarkdownEditor.tsx",
      ),
      "utf-8",
    );

    expect(pageEditorSource).toContain(
      'import { MarkdownSourceEditor } from "./MarkdownEditorKernel";',
    );
    expect(pageEditorSource).toContain("<MarkdownSourceEditor");
    expect(pageEditorSource).not.toContain('from "@uiw/react-codemirror"');

    expect(libraryEditorSource).toContain(
      'import { MarkdownEditorKernel } from "../pages/MarkdownEditorKernel";',
    );
    expect(libraryEditorSource).toContain("<MarkdownEditorKernel");
  });
});

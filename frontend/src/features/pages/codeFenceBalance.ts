export type UnclosedCodeFence = {
  closingFence: string;
  line: number;
};

export function findUnclosedCodeFence(body: string): UnclosedCodeFence | null {
  const lines = body.replace(/\r\n?/g, "\n").split("\n");
  let marker: "`" | "~" | null = null;
  let markerLength = 0;
  let openingLine = 0;

  for (let index = 0; index < lines.length; index += 1) {
    const stripped = lines[index].trimStart();
    if (marker) {
      const closing = stripped.trimEnd();
      if (
        closing.length >= markerLength &&
        [...closing].every((character) => character === marker)
      ) {
        marker = null;
        markerLength = 0;
        openingLine = 0;
      }
      continue;
    }
    if (!stripped.startsWith("```") && !stripped.startsWith("~~~")) {
      continue;
    }
    marker = stripped[0] as "`" | "~";
    markerLength = stripped.length - stripped.replace(new RegExp(`^\\${marker}+`), "").length;
    openingLine = index + 1;
  }

  if (!marker) {
    return null;
  }
  return {
    closingFence: marker.repeat(markerLength),
    line: openingLine,
  };
}

export function appendClosingCodeFence(body: string, closingFence: string) {
  const separator = body.length === 0 || body.endsWith("\n") ? "" : "\n";
  return `${body}${separator}${closingFence}\n`;
}

export type UnclosedCodeFence = {
  closingFence: string;
  line: number;
};

type OpenFence = { marker: "`" | "~"; length: number; openingLine: number };

// The CommonMark fence grammar of backend/utils/markdown_fence.py, kept
// identical so the editor and the server agree on what is fenced: at most
// three leading spaces, three or more markers, no backtick in a backtick
// fence's info string, and a closer of the same marker followed by blanks.
function fenceMarkerStart(line: string): number {
  let index = 0;
  while (index < Math.min(3, line.length) && line[index] === " ") {
    index += 1;
  }
  return index;
}

function markerRunEnd(line: string, start: number, marker: string): number {
  let end = start;
  while (end < line.length && line[end] === marker) {
    end += 1;
  }
  return end;
}

function fenceOpener(line: string): { marker: "`" | "~"; length: number } | null {
  const start = fenceMarkerStart(line);
  const marker = line[start];
  if (marker !== "`" && marker !== "~") {
    return null;
  }
  const end = markerRunEnd(line, start, marker);
  if (end - start < 3 || (marker === "`" && line.slice(end).includes("`"))) {
    return null;
  }
  return { marker, length: end - start };
}

function isFenceCloser(line: string, open: OpenFence): boolean {
  const start = fenceMarkerStart(line);
  if (line[start] !== open.marker) {
    return false;
  }
  const end = markerRunEnd(line, start, open.marker);
  return end - start >= open.length && /^[ \t]*$/.test(line.slice(end));
}

/** Walk the lines once, reporting for each whether it belongs to a fence. */
function scanCodeFences(
  lines: string[],
  visit: (index: number, fenced: boolean) => void = () => undefined,
): OpenFence | null {
  let open: OpenFence | null = null;

  for (let index = 0; index < lines.length; index += 1) {
    if (open) {
      visit(index, true);
      if (isFenceCloser(lines[index], open)) {
        open = null;
      }
      continue;
    }
    const opener = fenceOpener(lines[index]);
    if (!opener) {
      visit(index, false);
      continue;
    }
    open = { ...opener, openingLine: index + 1 };
    visit(index, true);
  }
  return open;
}

function bodyLines(body: string) {
  return body.replace(/\r\n?/g, "\n").split("\n");
}

export function findUnclosedCodeFence(body: string): UnclosedCodeFence | null {
  const open = scanCodeFences(bodyLines(body));
  if (!open) {
    return null;
  }
  return {
    closingFence: open.marker.repeat(open.length),
    line: open.openingLine,
  };
}

/** For each line of the body, whether it is a fence line or fenced content. */
export function codeFenceLineMask(body: string): boolean[] {
  const lines = bodyLines(body);
  const mask = new Array<boolean>(lines.length).fill(false);
  scanCodeFences(lines, (index, fenced) => {
    mask[index] = fenced;
  });
  return mask;
}

export function appendClosingCodeFence(body: string, closingFence: string) {
  const separator = body.length === 0 || body.endsWith("\n") ? "" : "\n";
  return `${body}${separator}${closingFence}\n`;
}

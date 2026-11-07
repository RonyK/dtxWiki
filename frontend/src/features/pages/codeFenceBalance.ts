export type UnclosedCodeFence = {
  closingFence: string;
  line: number;
};

type OpenFence = { marker: "`" | "~"; length: number; openingLine: number };

/** Walk the lines once, reporting for each whether it belongs to a fence. */
function scanCodeFences(
  lines: string[],
  visit: (index: number, fenced: boolean) => void = () => undefined,
): OpenFence | null {
  let open: OpenFence | null = null;

  for (let index = 0; index < lines.length; index += 1) {
    const stripped = lines[index].trimStart();
    if (open) {
      visit(index, true);
      const closing = stripped.trimEnd();
      const marker = open.marker;
      if (
        closing.length >= open.length &&
        [...closing].every((character) => character === marker)
      ) {
        open = null;
      }
      continue;
    }
    if (!stripped.startsWith("```") && !stripped.startsWith("~~~")) {
      visit(index, false);
      continue;
    }
    const marker = stripped[0] as "`" | "~";
    open = {
      marker,
      length: stripped.length - stripped.replace(new RegExp(`^\\${marker}+`), "").length,
      openingLine: index + 1,
    };
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

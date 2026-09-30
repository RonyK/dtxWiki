export type ScrollAnchor = {
  line: number;
  offset: number;
};

export type ScrollAnchorSpan = {
  startLine: number;
  endLine: number;
  startOffset: number;
  endOffset: number;
};

export type ScrollDocumentEdges = {
  firstLine: number;
  lastLine: number;
  topOffset: number;
  bottomOffset: number;
};

export type ScrollAnchorSegment = {
  current: ScrollAnchor;
  next: ScrollAnchor;
};

export type ScrollViewportAnchor = {
  contentOffset: number;
  viewportFraction: number;
};

/**
 * Build a line-ordered, offset-monotonic table from rendered block spans.
 * Markdown end lines are inclusive, so every span ends at the following line
 * boundary. A block start wins over a previous block's end on the same line,
 * and document edges win over both, keeping contiguous blocks and the exact
 * top/bottom limits deterministic.
 */
export function buildAnchorTable(
  spans: ScrollAnchorSpan[],
  edges?: ScrollDocumentEdges,
): ScrollAnchor[] {
  const byLine = new Map<number, { offset: number; priority: number }>();

  function setAnchor(line: number, offset: number, priority: number) {
    if (!isFiniteAnchor(line, offset)) {
      return;
    }
    const roundedLine = Math.round(line);
    const current = byLine.get(roundedLine);
    if (!current || priority > current.priority) {
      byLine.set(roundedLine, { offset, priority });
    }
  }

  for (const span of spans) {
    if (
      !isFiniteAnchor(span.startLine, span.startOffset) ||
      !Number.isFinite(span.endLine) ||
      span.endLine < span.startLine
    ) {
      continue;
    }
    setAnchor(span.startLine, span.startOffset, 2);
    const endBoundaryLine = Math.round(span.endLine) + 1;
    if (Number.isFinite(span.endOffset)) {
      setAnchor(endBoundaryLine, span.endOffset, 1);
    }
  }

  if (edges) {
    setAnchor(edges.firstLine, edges.topOffset, 3);
    if (edges.lastLine !== edges.firstLine) {
      setAnchor(edges.lastLine, edges.bottomOffset, 3);
    }
  }

  let previousOffset = Number.NEGATIVE_INFINITY;
  return [...byLine]
    .map(([line, anchor]) => ({ line, offset: anchor.offset }))
    .sort((left, right) => left.line - right.line)
    .map((anchor) => {
      const offset = Math.max(previousOffset, anchor.offset);
      previousOffset = offset;
      return { ...anchor, offset };
    });
}

export function lineToOffset(line: number, table: ScrollAnchor[]): number {
  const segment = findAnchorSegmentByLine(line, table);
  return segment ? interpolate(line, segment.current.line, segment.next.line,
    segment.current.offset, segment.next.offset) : table[0]?.offset ?? 0;
}

export function offsetToLine(offset: number, table: ScrollAnchor[]): number {
  const segment = findAnchorSegmentByOffset(offset, table);
  return segment ? interpolate(offset, segment.current.offset, segment.next.offset,
    segment.current.line, segment.next.line) : table[0]?.line ?? 1;
}

/**
 * Pick the semantic point within a scrolling viewport that should stay aligned.
 * The centre is stable during ordinary scrolling, while the reference eases to
 * the top and bottom edges so both panes can converge exactly at their limits.
 */
export function viewportAnchor(
  scrollTop: number,
  clientHeight: number,
  scrollHeight: number,
): ScrollViewportAnchor {
  const safeClientHeight = finiteNonNegative(clientHeight);
  const safeScrollHeight = Math.max(
    safeClientHeight,
    finiteNonNegative(scrollHeight),
  );
  const maxScroll = Math.max(0, safeScrollHeight - safeClientHeight);
  const safeScrollTop = clamp(finiteNonNegative(scrollTop), 0, maxScroll);
  if (maxScroll === 0) {
    return { contentOffset: 0, viewportFraction: 0 };
  }
  if (safeClientHeight === 0) {
    return { contentOffset: safeScrollTop, viewportFraction: 0 };
  }

  const edgeRange = Math.min(safeClientHeight / 2, maxScroll / 2);
  let viewportFraction = 0.5;
  if (edgeRange > 0 && safeScrollTop < edgeRange) {
    viewportFraction = 0.5 * (safeScrollTop / edgeRange);
  } else if (edgeRange > 0 && safeScrollTop > maxScroll - edgeRange) {
    viewportFraction = 0.5 +
      0.5 * ((safeScrollTop - (maxScroll - edgeRange)) / edgeRange);
  }

  return {
    contentOffset: clamp(
      safeScrollTop + viewportFraction * safeClientHeight,
      0,
      safeScrollHeight,
    ),
    viewportFraction,
  };
}

/** Invert viewportAnchor for a target pane with its own viewport geometry. */
export function scrollTopForAnchor(
  contentOffset: number,
  clientHeight: number,
  scrollHeight: number,
): number {
  const safeClientHeight = finiteNonNegative(clientHeight);
  const safeScrollHeight = Math.max(
    safeClientHeight,
    finiteNonNegative(scrollHeight),
  );
  const maxScroll = Math.max(0, safeScrollHeight - safeClientHeight);
  const safeContentOffset = clamp(
    finiteNonNegative(contentOffset),
    0,
    safeScrollHeight,
  );
  if (maxScroll === 0) {
    return 0;
  }
  if (safeClientHeight === 0) {
    return clamp(safeContentOffset, 0, maxScroll);
  }

  const edgeRange = Math.min(safeClientHeight / 2, maxScroll / 2);
  if (edgeRange === 0) {
    return clamp(safeContentOffset, 0, maxScroll);
  }
  const edgeScale = 1 + safeClientHeight / (2 * edgeRange);
  const topEdgeContent = edgeRange + safeClientHeight / 2;
  const bottomEdgeContent = maxScroll - edgeRange + safeClientHeight / 2;
  if (safeContentOffset < topEdgeContent) {
    return clamp(safeContentOffset / edgeScale, 0, maxScroll);
  }
  if (safeContentOffset > bottomEdgeContent) {
    return clamp(
      maxScroll - (safeScrollHeight - safeContentOffset) / edgeScale,
      0,
      maxScroll,
    );
  }
  return clamp(safeContentOffset - safeClientHeight / 2, 0, maxScroll);
}

/** Find the neighbouring line anchors in O(log n), extrapolating at edges. */
export function findAnchorSegmentByLine(
  line: number,
  table: ScrollAnchor[],
): ScrollAnchorSegment | null {
  return findAnchorSegment(line, table, (anchor) => anchor.line);
}

/** Find the neighbouring offset anchors in O(log n), extrapolating at edges. */
export function findAnchorSegmentByOffset(
  offset: number,
  table: ScrollAnchor[],
): ScrollAnchorSegment | null {
  return findAnchorSegment(offset, table, (anchor) => anchor.offset);
}

function findAnchorSegment(
  value: number,
  table: ScrollAnchor[],
  select: (anchor: ScrollAnchor) => number,
): ScrollAnchorSegment | null {
  if (table.length < 2) {
    return null;
  }
  let low = 0;
  let high = table.length;
  while (low < high) {
    const middle = low + Math.floor((high - low) / 2);
    if (select(table[middle]) <= value) {
      low = middle + 1;
    } else {
      high = middle;
    }
  }
  const currentIndex = Math.max(0, Math.min(table.length - 2, low - 1));
  return {
    current: table[currentIndex],
    next: table[currentIndex + 1],
  };
}

function interpolate(
  value: number,
  fromInput: number,
  toInput: number,
  fromOutput: number,
  toOutput: number,
) {
  if (toInput <= fromInput) {
    return fromOutput;
  }
  const fraction = (value - fromInput) / (toInput - fromInput);
  return fromOutput + fraction * (toOutput - fromOutput);
}

function isFiniteAnchor(line: number, offset: number) {
  return Number.isFinite(line) && Number.isFinite(offset) && line >= 1;
}

function finiteNonNegative(value: number) {
  return Number.isFinite(value) ? Math.max(0, value) : 0;
}

function clamp(value: number, minimum: number, maximum: number) {
  return Math.min(maximum, Math.max(minimum, value));
}

import { describe, expect, it } from "vitest";

import {
  buildAnchorTable,
  findAnchorSegmentByLine,
  findAnchorSegmentByOffset,
  lineToOffset,
  offsetToLine,
  scrollTopForAnchor,
  viewportAnchor,
} from "../editorScrollMapping";

describe("editor scroll mapping", () => {
  const table = buildAnchorTable([
    { startLine: 20, endLine: 39, startOffset: 300, endOffset: 700 },
    { startLine: 10, endLine: 19, startOffset: 100, endOffset: 300 },
  ]);

  it("builds a sorted table and finds neighbouring anchors with binary search", () => {
    expect(table).toEqual([
      { line: 10, offset: 100 },
      { line: 20, offset: 300 },
      { line: 40, offset: 700 },
    ]);
    expect(findAnchorSegmentByLine(30, table)).toEqual({
      current: { line: 20, offset: 300 },
      next: { line: 40, offset: 700 },
    });
    expect(findAnchorSegmentByOffset(500, table)).toEqual({
      current: { line: 20, offset: 300 },
      next: { line: 40, offset: 700 },
    });
  });

  it("round-trips lines and offsets, including outside the first and last anchors", () => {
    for (const line of [0, 10, 15, 30, 40, 50]) {
      const offset = lineToOffset(line, table);
      expect(offsetToLine(offset, table)).toBeCloseTo(line, 6);
    }
    for (const offset of [-100, 100, 200, 500, 700, 900]) {
      const line = offsetToLine(offset, table);
      expect(lineToOffset(line, table)).toBeCloseTo(offset, 6);
    }
  });

  it("uses document sentinels for the exact scrollable edges", () => {
    expect(buildAnchorTable(
      [{ startLine: 1, endLine: 8, startOffset: 24, endOffset: 520 }],
      { firstLine: 1, lastLine: 9, topOffset: 0, bottomOffset: 480 },
    )).toEqual([
      { line: 1, offset: 0 },
      { line: 9, offset: 480 },
    ]);
  });

  it("maps one-line blocks across a full semantic line boundary", () => {
    expect(buildAnchorTable([
      { startLine: 5, endLine: 5, startOffset: 100, endOffset: 180 },
    ])).toEqual([
      { line: 5, offset: 100 },
      { line: 6, offset: 180 },
    ]);
    expect(lineToOffset(5.5, buildAnchorTable([
      { startLine: 5, endLine: 5, startOffset: 100, endOffset: 180 },
    ]))).toBe(140);
  });

  it("uses a centre anchor in the body and exact edge anchors at both limits", () => {
    expect(viewportAnchor(0, 200, 1_000)).toEqual({
      contentOffset: 0,
      viewportFraction: 0,
    });
    expect(viewportAnchor(100, 200, 1_000)).toEqual({
      contentOffset: 200,
      viewportFraction: 0.5,
    });
    expect(viewportAnchor(400, 200, 1_000)).toEqual({
      contentOffset: 500,
      viewportFraction: 0.5,
    });
    expect(viewportAnchor(800, 200, 1_000)).toEqual({
      contentOffset: 1_000,
      viewportFraction: 1,
    });

    expect(scrollTopForAnchor(0, 300, 1_500)).toBe(0);
    expect(scrollTopForAnchor(750, 300, 1_500)).toBe(600);
    expect(scrollTopForAnchor(1_500, 300, 1_500)).toBe(1_200);

    for (const scrollTop of [0, 50, 150, 600, 1_050, 1_200]) {
      const anchor = viewportAnchor(scrollTop, 300, 1_500);
      expect(scrollTopForAnchor(anchor.contentOffset, 300, 1_500))
        .toBeCloseTo(scrollTop, 6);
    }
  });

  it("eases across short scroll ranges without producing invalid offsets", () => {
    expect(viewportAnchor(50, 500, 600)).toEqual({
      contentOffset: 300,
      viewportFraction: 0.5,
    });
    expect(viewportAnchor(100, 500, 600)).toEqual({
      contentOffset: 600,
      viewportFraction: 1,
    });
    expect(viewportAnchor(Number.NaN, 0, Number.POSITIVE_INFINITY)).toEqual({
      contentOffset: 0,
      viewportFraction: 0,
    });
  });

  it("round-trips a takeover across panes with different content heights", () => {
    for (const sourceTop of [1, 50, 400, 750, 799]) {
      const sourceAnchor = viewportAnchor(sourceTop, 200, 1_000);
      const previewOffset = sourceAnchor.contentOffset * 2;
      const previewTop = scrollTopForAnchor(previewOffset, 200, 2_000);
      const previewAnchor = viewportAnchor(previewTop, 200, 2_000);
      const sourceOffset = previewAnchor.contentOffset / 2;
      expect(scrollTopForAnchor(sourceOffset, 200, 1_000))
        .toBeCloseTo(sourceTop, 6);
    }
  });
});

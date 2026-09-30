import { describe, expect, it } from "vitest";

import { readAppStyles } from "../../../test-support/appStyles";

describe("Context reader static layout regressions", () => {
  it("keeps Edit and More equal-height on desktop and mobile", () => {
    const styles = readAppStyles();

    expect(styles).toMatch(
      /\.context-reader \.context-reader-header-action \{[^}]*height: 38px;[^}]*min-height: 38px;[^}]*font-size: 13px;[^}]*line-height: 1;/s,
    );
    expect(styles).toMatch(
      /\.context-reader \.context-reader-more-action \{[^}]*width: 38px;[^}]*padding: 0;/s,
    );
    expect(styles).toMatch(
      /@media \(max-width: 1024px\) \{[^}]*\.context-reader \.context-reader-header-action \{[^}]*height: 44px;[^}]*min-height: 44px;/s,
    );
    expect(styles).toMatch(
      /\.context-reader \.context-reader-edit-action \{[^}]*padding-inline: 12px;/s,
    );
  });

  it("spaces the visible Share sections inside the Dialog scroll wrapper", () => {
    const styles = readAppStyles();

    expect(styles).toMatch(
      /\.dialog-panel\.context-share-dialog > \.dialog-scroll-region \{[^}]*display: grid;[^}]*gap: 16px;/s,
    );
    expect(styles).toMatch(
      /\.dialog-panel\.context-share-dialog \{[^}]*gap: 0;/s,
    );
  });

  it("keeps History authors clear of neighboring status and action rows", () => {
    const styles = readAppStyles();

    expect(styles).toMatch(
      /\.history-card-author \{[^}]*margin-block: 4px;/s,
    );
  });

  it("stacks the Recent updates filter below its title on phones", () => {
    const styles = readAppStyles();

    expect(styles).toMatch(
      /@media \(max-width: 640px\) \{[\s\S]*?\.context-screen \.recent-items-list \.panel-header \{[^}]*align-items: stretch;[^}]*flex-direction: column;/,
    );
    expect(styles).toMatch(
      /\.context-screen \.recent-items-list \.context-list-filters \{[^}]*grid-template-columns: auto minmax\(0, 1fr\);[^}]*width: 100%;/,
    );
  });

  it("top-aligns the Recent updates title against the two-row Draft state filter (L-4)", () => {
    const styles = readAppStyles();

    // .context-list-filters stacks "Draft state" over its <select> (two
    // rows), so the shared .panel-header's default align-items:center would
    // center the single-line title against that whole stack instead of
    // matching either row -- measured as a baseline mismatch.
    expect(styles).toMatch(
      /\.panel-header:has\(\.context-list-filters\)\s*\{[^}]*align-items:\s*flex-start;/s,
    );
  });

  it("collapses reconciliation comparisons and actions to one column on mobile", () => {
    const styles = readAppStyles();

    expect(styles).toMatch(
      /\.dialog-panel\.context-reconciliation-dialog \{[^}]*display: flex;[^}]*flex-direction: column;[^}]*overflow: hidden;/s,
    );
    expect(styles).toMatch(
      /\.dialog-panel\.context-reconciliation-dialog > \.dialog-scroll-region \{[^}]*flex: 1 1 auto;[^}]*min-height: 0;[^}]*max-height: none;/s,
    );
    expect(styles).toMatch(
      /\.dialog-panel\.context-reconciliation-dialog > \.dialog-footer \{[^}]*flex: 0 0 auto;/s,
    );
    expect(styles).toMatch(
      /\.context-reconciliation-footer button \{[^}]*min-height: 44px;/s,
    );
    expect(styles).toMatch(
      /@media \(max-width: 720px\) \{[\s\S]*?\.dialog-backdrop:has\(\.context-reconciliation-dialog\) \{[^}]*padding: 0;/,
    );
    expect(styles).toMatch(
      /\.context-reconciliation-workspace \{[^}]*grid-template-columns: minmax\(180px, 240px\) minmax\(0, 1fr\);/,
    );
    expect(styles).toMatch(
      /@media \(max-width: 720px\) \{[\s\S]*?\.context-reconciliation-workspace \{[^}]*grid-template-columns: minmax\(0, 1fr\);/,
    );
    expect(styles).toMatch(
      /@media \(max-width: 720px\) \{[\s\S]*?\.context-reconciliation-comparison \{[^}]*grid-template-columns: minmax\(0, 1fr\);/,
    );
    expect(styles).toMatch(
      /@media \(max-width: 720px\) \{[\s\S]*?\.context-reconciliation-footer \.dialog-actions \{[^}]*grid-template-columns: minmax\(0, 1fr\);/,
    );
  });

  it("centers the folder card's pagination under its index preview and item list (260912)", () => {
    const styles = readAppStyles();

    expect(styles).toMatch(
      /\.context-children-pagination-status \{[^}]*text-align: center;/s,
    );
    expect(styles).toMatch(
      /\.context-children-pagination nav \{[^}]*justify-content: center;/s,
    );
  });
});

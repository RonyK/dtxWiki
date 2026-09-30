import "@testing-library/jest-dom/vitest";

import { webcrypto } from "node:crypto";
import { beforeEach } from "vitest";

import { i18n } from "../shared/i18n";

beforeEach(async () => {
  window.localStorage.removeItem("dtxwiki.uiLanguage");
  // changeLanguage re-runs i18next's whole language-change pipeline and emits
  // to every subscriber, and almost every test is already in English. Only the
  // handful that switch to ko need this to do any work.
  if (i18n.language !== "en") {
    await i18n.changeLanguage("en");
  }
});

if (!globalThis.crypto?.subtle) {
  Object.defineProperty(globalThis, "crypto", {
    configurable: true,
    value: webcrypto,
  });
}

if (!Element.prototype.scrollIntoView) {
  Object.defineProperty(Element.prototype, "scrollIntoView", {
    configurable: true,
    value: () => {},
  });
}

const emptyRect = new DOMRect(0, 0, 0, 0);

if (!Range.prototype.getBoundingClientRect) {
  Object.defineProperty(Range.prototype, "getBoundingClientRect", {
    configurable: true,
    value: () => emptyRect,
  });
}

if (!Range.prototype.getClientRects) {
  Object.defineProperty(Range.prototype, "getClientRects", {
    configurable: true,
    value: () =>
      ({
        length: 0,
        item: () => null,
        [Symbol.iterator]: () => [][Symbol.iterator](),
      }) satisfies DOMRectList,
  });
}

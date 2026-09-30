import type { Preview } from "@storybook/react";

import "../src/styles/index.css";
import { initI18n } from "../src/shared/i18n";

// Without this, `useTranslation` never resolves a namespace and every
// `t("status.<enum>", {defaultValue})` call (StatusPill, among others)
// falls back to the raw enum instead of its label -- the one fidelity gap
// in mounting a real component here instead of a hand-built HTML harness.
initI18n();

const preview: Preview = {
  parameters: {
    controls: {
      expanded: true,
    },
    layout: "padded",
  },
};

export default preview;

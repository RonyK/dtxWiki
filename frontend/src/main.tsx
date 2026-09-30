import React from "react";
import ReactDOM from "react-dom/client";
import { I18nextProvider } from "react-i18next";

import { App } from "./app/App";
import { registerVitePreloadErrorRecovery } from "./app/preloadRecovery";
import { i18n } from "./shared/i18n";
import "./styles/index.css";

registerVitePreloadErrorRecovery({ entryUrl: import.meta.url });

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <I18nextProvider i18n={i18n}>
      <App />
    </I18nextProvider>
  </React.StrictMode>,
);

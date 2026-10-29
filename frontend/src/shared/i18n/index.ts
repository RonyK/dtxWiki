import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import { attachJosa, JOSA_FORMAT_NAMES } from "./josa";
import authEn from "./locales/en/auth.json";
import agentEn from "./locales/en/agent.json";
import activityEn from "./locales/en/activity.json";
import contextEn from "./locales/en/context.json";
import dashboardEn from "./locales/en/dashboard.json";
import inboxEn from "./locales/en/inbox.json";
import libraryEn from "./locales/en/library.json";
import lintEn from "./locales/en/lint.json";
import chatsEn from "./locales/en/chats.json";
import pagesEn from "./locales/en/pages.json";
import reviewEn from "./locales/en/review.json";
import searchEn from "./locales/en/search.json";
import settingsEn from "./locales/en/settings.json";
import shellEn from "./locales/en/shell.json";
import treeEn from "./locales/en/tree.json";
import authKo from "./locales/ko/auth.json";
import agentKo from "./locales/ko/agent.json";
import activityKo from "./locales/ko/activity.json";
import contextKo from "./locales/ko/context.json";
import dashboardKo from "./locales/ko/dashboard.json";
import inboxKo from "./locales/ko/inbox.json";
import libraryKo from "./locales/ko/library.json";
import lintKo from "./locales/ko/lint.json";
import chatsKo from "./locales/ko/chats.json";
import pagesKo from "./locales/ko/pages.json";
import reviewKo from "./locales/ko/review.json";
import searchKo from "./locales/ko/search.json";
import settingsKo from "./locales/ko/settings.json";
import shellKo from "./locales/ko/shell.json";
import treeKo from "./locales/ko/tree.json";

export type SupportedLocale = "en" | "ko";

const UI_LANGUAGE_STORAGE_KEY = "dtxwiki.uiLanguage";

const resources = {
  en: {
    agent: agentEn,
    activity: activityEn,
    auth: authEn,
    context: contextEn,
    dashboard: dashboardEn,
    inbox: inboxEn,
    library: libraryEn,
    lint: lintEn,
    chats: chatsEn,
    pages: pagesEn,
    review: reviewEn,
    search: searchEn,
    settings: settingsEn,
    shell: shellEn,
    tree: treeEn,
  },
  ko: {
    agent: agentKo,
    activity: activityKo,
    auth: authKo,
    context: contextKo,
    dashboard: dashboardKo,
    inbox: inboxKo,
    library: libraryKo,
    lint: lintKo,
    chats: chatsKo,
    pages: pagesKo,
    review: reviewKo,
    search: searchKo,
    settings: settingsKo,
    shell: shellKo,
    tree: treeKo,
  },
} as const;

let initialized = false;

/**
 * Keep the document's declared language in sync with the active UI locale.
 * Without this, screen readers keep reading Korean text with an English
 * pronunciation model and `:lang()` selectors never match — see I18N-09.
 */
function applyDocumentLanguage(locale: SupportedLocale) {
  if (typeof document !== "undefined") {
    document.documentElement.lang = locale;
  }
}

export function initI18n(initialLocale = resolveInitialLocale()) {
  if (initialized) {
    return i18n;
  }
  initialized = true;
  void i18n.use(initReactI18next).init({
    defaultNS: "shell",
    fallbackLng: "en",
    interpolation: { escapeValue: false },
    lng: normalizeLocale(initialLocale),
    ns: ["activity", "agent", "auth", "chats", "context", "dashboard", "library", "lint", "pages", "review", "search", "settings", "shell", "tree"],
    react: { useSuspense: false },
    resources,
  });
  // Korean josa (조사) particles depend on whether the interpolated value
  // ends in a syllable with batchim (종성). Registering each format name on
  // the built-in Formatter service — rather than the deprecated
  // `interpolation.format` callback, which i18next's own `init()`
  // unconditionally overwrites with `services.formatter.format` — lets
  // `{{version, josaEulReul}}` pick the correct particle at render time
  // instead of shipping both forms as a parenthesized pair (see I18N-06).
  // `init()` populates `services.formatter` synchronously, so this is safe
  // to call immediately without awaiting the returned promise.
  for (const name of JOSA_FORMAT_NAMES) {
    i18n.services.formatter?.add(name, (value: unknown, _lng: string | undefined, _options: unknown) =>
      attachJosa(String(value), name),
    );
  }
  applyDocumentLanguage(normalizeLocale(initialLocale));
  return i18n;
}

export function resolveInitialLocale(
  workspaceLanguage?: string | null,
  userLanguage?: string | null,
) {
  return storedUiLocale() ?? normalizeLocale(userLanguage ?? workspaceLanguage ?? "en");
}

export function setLocale(locale: string, options: { persist?: boolean } = {}) {
  const nextLocale = normalizeLocale(locale);
  if (options.persist !== false && typeof window !== "undefined") {
    window.localStorage.setItem(UI_LANGUAGE_STORAGE_KEY, nextLocale);
  }
  void i18n.changeLanguage(nextLocale);
  applyDocumentLanguage(nextLocale);
  if (typeof window !== "undefined") {
    window.dispatchEvent(
      new CustomEvent("dtxwiki:ui-language-changed", {
        detail: { language: nextLocale },
      }),
    );
  }
}

export function syncLocaleFromWorkspace(workspaceLanguage: string | null | undefined) {
  if (storedUiLocale()) {
    return;
  }
  setLocale(normalizeLocale(workspaceLanguage), { persist: false });
}

/** True when this browser holds a personal language choice. */
export function hasStoredUiLocale() {
  return storedUiLocale() !== null;
}

/**
 * Drop the personal choice and follow the workspace default again. Without
 * this, one click of the language toggle silently outranked the workspace
 * setting forever, with nothing in the UI to say so or undo it.
 */
export function clearStoredUiLocale(workspaceLanguage: string | null | undefined) {
  if (typeof window !== "undefined") {
    window.localStorage.removeItem(UI_LANGUAGE_STORAGE_KEY);
  }
  setLocale(normalizeLocale(workspaceLanguage), { persist: false });
}

export function normalizeLocale(locale: string | null | undefined): SupportedLocale {
  return locale === "ko" || locale === "ko-KR" ? "ko" : "en";
}

export function translate(key: string, fallback: string) {
  return i18n.t(key, { defaultValue: fallback });
}

function storedUiLocale(): SupportedLocale | null {
  if (typeof window === "undefined") {
    return null;
  }
  const stored = window.localStorage.getItem(UI_LANGUAGE_STORAGE_KEY);
  return stored ? normalizeLocale(stored) : null;
}

initI18n();

export { i18n };

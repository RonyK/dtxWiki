import { render, screen } from "@testing-library/react";
import { I18nextProvider } from "react-i18next";
import {
  createMemoryRouter,
  RouterProvider,
  type RouteObject,
} from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { i18n } from "../shared/i18n";
import { appRoutes } from "./App";

describe("root route error boundary", () => {
  it.each([
    {
      locale: "en",
      title: "This page couldn't be loaded",
      reload: "Load the latest version",
      dashboard: "Find workspaces",
    },
    {
      locale: "ko",
      title: "페이지를 불러올 수 없습니다",
      reload: "최신 버전 불러오기",
      dashboard: "워크스페이스 찾기",
    },
  ])(
    "renders localized recovery UI without the raw error in $locale",
    async ({ locale, title, reload, dashboard }) => {
      await i18n.changeLanguage(locale);
      const consoleError = vi
        .spyOn(console, "error")
        .mockImplementation(() => undefined);
      const rootErrorElement = (appRoutes[0] as RouteObject).errorElement;
      const router = createMemoryRouter(
        [
          {
            path: "/",
            errorElement: rootErrorElement,
            children: [
              {
                path: "broken",
                element: <BrokenRoute />,
              },
            ],
          },
        ],
        { initialEntries: ["/broken"] },
      );

      render(
        <I18nextProvider i18n={i18n}>
          <RouterProvider router={router} />
        </I18nextProvider>,
      );

      expect(
        await screen.findByRole("heading", { name: title }),
      ).toBeInTheDocument();
      expect(screen.getByRole("button", { name: reload })).toBeInTheDocument();
      expect(screen.getByRole("link", { name: dashboard })).toHaveAttribute(
        "href",
        "/workspaces/find",
      );
      expect(
        screen.queryByText("RAW_CHUNK_STACK_MUST_STAY_HIDDEN"),
      ).not.toBeInTheDocument();
      consoleError.mockRestore();
    },
  );
});

function BrokenRoute(): never {
  throw new Error("RAW_CHUNK_STACK_MUST_STAY_HIDDEN");
}

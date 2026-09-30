import { expect, isTransientNetworkImportError, test } from "./fixtures";

/**
 * M-2: one-time recovery tokens stay out of request lines and the address bar.
 *
 * Email-verification links carry their token in the URL fragment
 * (`/verify-email#token=...`), which the browser never sends, and the screen
 * submits it in a POST body. Links mailed before that change used
 * `?token=` and still open. In both forms the screen takes the token out of
 * the address bar as soon as it has read it. (The password-reset screen is
 * covered the same way, with live seeded tokens, in
 * workspace_v3_acceptance.spec.ts.)
 *
 * The token here was never issued, so the backend answers "invalid" -- what
 * is under test is where the token travels, not whether it verifies. That
 * also keeps a real credential out of the step titles and failure traces of
 * the published report.
 */
const UNISSUED_TOKEN = "dtxe_e2e-unissued-verification-token";
const CONFIRM_PATH = "/api/v1/auth/email-verification/confirm";

const LINKS = [
  { form: "fragment", path: `/verify-email#token=${UNISSUED_TOKEN}` },
  { form: "legacy query", path: `/verify-email?token=${UNISSUED_TOKEN}` },
] as const;

for (const link of LINKS) {
  test(`verify-email posts a ${link.form} token and clears it from the address bar`, async ({
    page,
  }) => {
    const pageErrors: string[] = [];
    page.on("pageerror", (error) => {
      if (!isTransientNetworkImportError(page, error.message)) {
        pageErrors.push(error.message);
      }
    });
    // Request lines the page sends after its own navigation. Counted, not
    // recorded: a recorded URL would put the token into the report.
    let tokenBearingApiRequestLines = 0;
    page.on("request", (request) => {
      if (request.isNavigationRequest()) {
        return;
      }
      const sent = new URL(request.url());
      if (`${sent.pathname}${sent.search}`.includes(UNISSUED_TOKEN)) {
        tokenBearingApiRequestLines += 1;
      }
    });
    const confirmation = page.waitForRequest(
      (request) => new URL(request.url()).pathname === CONFIRM_PATH,
    );

    await page.goto(link.path);

    const confirm = await confirmation;
    expect(confirm.method()).toBe("POST");
    expect(
      confirm.postDataJSON()?.token === UNISSUED_TOKEN,
      "the confirmation must send the token in its JSON body",
    ).toBe(true);
    await expect(
      page.getByText(
        "This verification link is invalid, expired, or already used.",
        { exact: true },
      ),
    ).toBeVisible();
    await expect
      .poll(() => page.url().includes(UNISSUED_TOKEN), {
        message: "the screen must clear the token from the address bar",
      })
      .toBe(false);
    expect(new URL(page.url()).pathname).toBe("/verify-email");
    expect(
      tokenBearingApiRequestLines,
      "no API request may carry the token in its path or query string",
    ).toBe(0);
    expect(pageErrors).toEqual([]);
  });
}

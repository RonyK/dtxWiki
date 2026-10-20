import { expect, test } from "./fixtures";

import { evidenceLocatorScreenshot } from "./evidence";

for (const width of [1440, 390, 320]) {
  test(`account input boundaries preserve Unicode and reject invalid submissions at ${width}px @cross-browser`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/login?mode=signup");
    await expect(page.getByRole("heading", { name: "Sign up", exact: true })).toBeVisible();
    const email = page.getByLabel("Email", { exact: true });
    const name = page.getByLabel("Display name", { exact: true });
    const password = page.getByLabel("Password", { exact: true });
    const submit = page.getByRole("button", { name: "Sign up", exact: true });
    const signupRequests: string[] = [];
    page.on("request", (request) => {
      if (request.method() === "POST" && new URL(request.url()).pathname === "/api/v1/auth/signup") {
        // Retain only the route; never attach raw credentials to evidence.
        signupRequests.push("signup");
      }
    });
    const domain = [
      `${testInfo.project.name}-${width}`.padEnd(61, "b"),
      "c".repeat(61),
      "d".repeat(61),
      "com",
    ].join(".");
    const local = "한".repeat(21) + "a";
    const validEmail = `${local}@${domain}`;
    expect(Buffer.byteLength(validEmail, "utf8")).toBe(254);
    await expect(email).toHaveAttribute("type", "text");
    await expect(email).toHaveAttribute("inputmode", "email");

    await email.fill(validEmail);
    await name.fill("😀".repeat(33));
    await password.fill("😀".repeat(128));
    await submit.click();
    await expect(page.getByRole("alert")).toContainText("32");
    expect(signupRequests).toHaveLength(0);
    await expect(name).toHaveValue("😀".repeat(33));

    await name.fill("😀".repeat(32));
    await email.fill(`${validEmail}x`);
    await submit.click();
    await expect(page.getByRole("alert")).toContainText("254");
    expect(signupRequests).toHaveLength(0);

    // Total remains 254 bytes, so this isolates the local-part byte limit.
    await email.fill(`${local}a@${domain.slice(0, -1)}`);
    await submit.click();
    await expect(page.getByRole("alert")).toContainText("64");
    expect(signupRequests).toHaveLength(0);

    await email.fill(validEmail);
    await password.fill("😀".repeat(129));
    await submit.click();
    await expect(page.getByRole("alert")).toContainText("128");
    expect(signupRequests).toHaveLength(0);
    await password.fill("😀".repeat(128));

    const pasteAccepted = await name.evaluate((input) => {
      const data = new DataTransfer();
      data.setData("text/plain", "Alice\nBob");
      const event = new ClipboardEvent("paste", {
        bubbles: true, cancelable: true, clipboardData: data,
      });
      // Firefox discards constructor-provided clipboard data for an untrusted
      // event. Give every engine the same raw payload before exercising React.
      Object.defineProperty(event, "clipboardData", { value: data });
      if (event.clipboardData?.getData("text/plain") !== "Alice\nBob") {
        throw new Error("synthetic paste lost its raw clipboard payload");
      }
      return input.dispatchEvent(event);
    });
    expect(pasteAccepted).toBe(false);
    await expect(name).toHaveValue("😀".repeat(32));
    await expect(page.getByRole("alert")).toContainText("control characters");
    expect(signupRequests).toHaveLength(0);

    const compositionEnterAccepted = await name.evaluate((input) => input.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Enter", isComposing: true, bubbles: true, cancelable: true }),
    ));
    expect(compositionEnterAccepted).toBe(false);
    expect(signupRequests).toHaveLength(0);
    const geometry = await page.evaluate(() => ({
      viewport: innerWidth, content: document.documentElement.scrollWidth,
    }));
    expect(geometry.content).toBeLessThanOrEqual(geometry.viewport + 1);
    await evidenceLocatorScreenshot(page.locator(".auth-panel"), testInfo, `account-input-policy-${width}`);

    const response = page.waitForResponse((candidate) =>
      new URL(candidate.url()).pathname === "/api/v1/auth/signup" && candidate.request().method() === "POST",
    );
    await submit.click();
    const created = await response;
    expect(created.status()).toBe(201);
    const body = await created.json();
    expect(body.data.user.email).toBe(validEmail);
    expect(body.data.user.display_name).toBe("😀".repeat(32));
    expect(signupRequests).toHaveLength(1);
  });
}

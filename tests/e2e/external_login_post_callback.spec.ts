import { expect, test, type Request } from "@playwright/test";
import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import https from "node:https";
import os from "node:os";
import path from "node:path";

const loginTest = test.extend<{ httpsFixturePort: string }>({
  httpsFixturePort: [
    async ({}, use, testInfo) => {
      const temporary = await mkdtemp(path.join(os.tmpdir(), "dtxwiki-samesite-"));
      let server: ChildProcess | undefined;
      let closed: Promise<void> | undefined;
      let stdout = "";
      let stderr = "";
      try {
        execFileSync("openssl", [
          "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
          "-subj", "/CN=localhost",
          "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
          "-keyout", path.join(temporary, "key.pem"),
          "-out", path.join(temporary, "certificate.pem"),
        ], { stdio: "ignore" });
        const fixture = spawn(path.join(process.cwd(), ".venv/bin/python"), [
          "tests/e2e/external_login_browser_server.py",
          "--database", path.join(temporary, "database.db"),
          "--certificate", path.join(temporary, "certificate.pem"),
          "--key", path.join(temporary, "key.pem"),
        ], {
          cwd: process.cwd(),
          env: { ...process.env, PYTHONPATH: process.cwd() },
          stdio: ["ignore", "pipe", "pipe"],
        });
        server = fixture;
        closed = new Promise((resolve) => fixture.once("close", () => resolve()));
        let spawnError: Error | undefined;
        fixture.once("error", (error) => { spawnError = error; });
        fixture.stdout.on("data", (chunk) => { stdout += String(chunk); });
        fixture.stderr.on("data", (chunk) => { stderr += String(chunk); });

        let port = "";
        await expect.poll(() => {
          if (spawnError) throw spawnError;
          if (fixture.exitCode !== null || fixture.signalCode !== null) {
            throw new Error(`HTTPS fixture exited (${fixture.exitCode ?? fixture.signalCode})`);
          }
          port = /^HTTPS fixture port (\d+)$/m.exec(stdout)?.[1] ?? "";
          if (!port) return 0;
          return new Promise<number>((resolve) => {
            const request = https.get(`https://127.0.0.1:${port}/fixture/done`, {
              rejectUnauthorized: false,
            }, (response) => { response.resume(); resolve(response.statusCode ?? 0); });
            request.on("error", () => resolve(0));
            request.setTimeout(2_000, () => { request.destroy(); resolve(0); });
          });
        }, { timeout: 45_000, message: "isolated HTTPS auth fixture starts" }).toBe(200);

        await use(port);
      } catch (error) {
        await testInfo.attach("HTTPS auth fixture output", {
          body: `stdout\n${stdout}\nstderr\n${stderr}`,
          contentType: "text/plain",
        });
        throw error;
      } finally {
        try {
          if (server?.pid !== undefined && server.exitCode === null && server.signalCode === null) {
            const forceStop = setTimeout(() => server?.kill("SIGKILL"), 5_000);
            try {
              server.kill("SIGTERM");
              await closed;
            } finally {
              clearTimeout(forceStop);
            }
          }
        } finally {
          await rm(temporary, { recursive: true, force: true });
        }
      }
    },
    // The app and its database start before the browser scenario's timeout.
    { timeout: 60_000 },
  ],
});

// One browser-only scenario: TestClient cannot implement SameSite policy.
// HTTPS localhost and 127.0.0.1 are different sites, not just different ports.
// Assertions are canned; the native cookies and production auth engine are real.
loginTest("cross-site POST restores Lax cookies before SSO reauthentication and linking", async ({
  browser, httpsFixturePort,
}) => {
  const wiki = `https://127.0.0.1:${httpsFixturePort}`;
  const context = await browser.newContext({ ignoreHTTPSErrors: true });
  try {
    const page = await context.newPage();
    await page.goto(`${wiki}/api/v1/auth/sso/company/start?next=/fixture/done`);
    await expect(page).toHaveURL(`${wiki}/fixture/done`);
    const native = (await context.cookies(wiki)).find((cookie) => cookie.name === "dtxwiki_session");
    expect(native).toMatchObject({ sameSite: "Lax", secure: true, httpOnly: true });
    const me = await page.evaluate(async () => (await fetch("/api/v1/auth/me")).json());
    expect(me.data.has_password).toBe(false);

    for (const purpose of ["reauthenticate", "link"] as const) {
      const slug = purpose === "reauthenticate" ? "company" : "secondary";
      const callbackPath = `/api/v1/auth/sso/${slug}/callback`;
      if (purpose === "link") {
        // Exercise the production form's manual fallback when scripts are
        // blocked. Cookie/Origin policy still comes from the real browser.
        await page.route(`**${callbackPath}`, async (route) => {
          const response = await route.fetch();
          await route.fulfill({
            response,
            headers: {
              ...response.headers(),
              "content-security-policy": "default-src 'none'; form-action 'self'",
            },
          });
        });
      }
      const callback = page.waitForRequest((request) =>
        request.method() === "POST" && new URL(request.url()).pathname === callbackPath,
      );
      const continuation = page.waitForRequest((request) =>
        request.method() === "POST" && new URL(request.url()).pathname === `${callbackPath}/continue`,
      );
      await page.evaluate(async ({ slug, purpose }) => {
        const response = await fetch(`/api/v1/auth/sso/${slug}/${purpose}`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ return_to: `/fixture/done?flow=${purpose}` }),
        });
        if (!response.ok) throw new Error(`flow start failed: ${response.status}`);
        window.location.assign((await response.json()).data.redirect_url);
      }, { slug, purpose });
      const crossSite = await callback;
      if (purpose === "link") await page.getByRole("button", { name: "Continue", exact: true }).click();
      const sameSite = await continuation;
      const cookies = async (request: Request) => (await request.allHeaders()).cookie ?? "";
      expect(await cookies(crossSite)).not.toContain("dtxwiki_session=");
      expect((await crossSite.allHeaders()).origin).toBe(`https://localhost:${httpsFixturePort}`);
      expect((await sameSite.allHeaders()).origin).toBe(wiki);
      await expect(page).toHaveURL(`${wiki}/fixture/done?flow=${purpose}`);
      // A redirect answers the continuation, so Playwright cannot report that
      // request's Cookie header on WebKit: ask the server what it received.
      const continued = await page.evaluate(async () => (await fetch("/fixture/continuations")).json());
      expect(continued.at(-1)).toEqual({
        path: `${callbackPath}/continue`,
        cookies: expect.arrayContaining(["dtxwiki_session"]),
      });
    }
    const identities = await page.evaluate(async () => (await fetch("/api/v1/auth/identities")).json());
    expect(identities.data.identities.map((identity: { provider_slug: string }) => identity.provider_slug).sort())
      .toEqual(["company", "secondary"]);
    expect((await context.cookies(`${wiki}/api/v1/auth`)).some((cookie) => cookie.name.endsWith("_reauth_account")))
      .toBe(true);
  } finally {
    await context.close();
  }
});

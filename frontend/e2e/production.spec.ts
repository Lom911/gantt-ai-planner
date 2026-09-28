import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page } from "@playwright/test";

// Checks that only show up in production: the Caddy CSP and a phone-sized viewport.
// Dev and the e2e stack serve the app without the CSP header, so the document response is
// re-served here with the exact policy from deploy/caddy/Caddyfile and the browser enforces it.

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CADDYFILE = path.resolve(__dirname, "../../deploy/caddy/Caddyfile");
const CSP = /Content-Security-Policy "([^"]+)"/.exec(fs.readFileSync(CADDYFILE, "utf8"))?.[1];

// Optional: where to keep the phone screenshot for a human look (not committed).
const SCREENSHOT_DIR = process.env.E2E_SCREENSHOT_DIR;

declare global {
  interface Window {
    __cspViolations?: string[];
  }
}

async function enforceProductionCsp(page: Page) {
  expect(CSP, "CSP header not found in deploy/caddy/Caddyfile").toBeTruthy();
  const requested: string[] = [];
  page.on("request", (request) => requested.push(request.url()));
  await page.addInitScript(() => {
    document.addEventListener("securitypolicyviolation", (e) => {
      (window.__cspViolations ??= []).push(`${e.violatedDirective} ${e.blockedURI}`);
    });
  });
  await page.route("**/*", async (route) => {
    if (route.request().resourceType() !== "document") return route.continue();
    const response = await route.fetch();
    await route.fulfill({ response, headers: { ...response.headers(), "content-security-policy": CSP! } });
  });
  return {
    foreignRequests: () => {
      const origin = new URL(page.url()).origin;
      return requested.filter((u) => u.startsWith("http") && new URL(u).origin !== origin);
    },
    violations: () => page.evaluate(() => window.__cspViolations ?? []),
  };
}

// The SVAR icon font must come from our own origin (self-hosted) and actually be used.
async function iconFontLoaded(page: Page) {
  return page.evaluate(async () => {
    await document.fonts.ready;
    const face = [...document.fonts].find((f) => f.family.replace(/["']/g, "") === "wx-icons");
    return face?.status ?? "missing";
  });
}

test("production CSP: no violations, no third-party requests, icon font renders", async ({ page }) => {
  const csp = await enforceProductionCsp(page);
  // A first visit must not start with 401s (the session is created before anything fetches):
  // the browser logs every failed response as a console error.
  const failed: string[] = [];
  page.on("response", (r) => {
    if (r.status() >= 400) failed.push(`${r.status()} ${r.request().method()} ${r.url()}`);
  });
  await page.goto("/");
  await expect(page.getByText("Сбор требований и приоритизация").last()).toBeVisible();

  // The grid/timeline resizer's expand icons (the SVAR grid toggle) use the wx-icons font.
  const toggle = page.locator(".wx-resizer .wxi-menu-right").first();
  await expect(toggle).toBeAttached();
  const glyph = await toggle.evaluate((el) => {
    const before = getComputedStyle(el, "::before");
    return { font: before.fontFamily, content: before.content };
  });
  expect(glyph.font).toContain("wx-icons");
  expect(glyph.content).not.toBe("none");
  await expect.poll(() => iconFontLoaded(page)).toBe("loaded");

  expect(await csp.violations()).toEqual([]);
  expect(csp.foreignRequests()).toEqual([]);
  expect(failed).toEqual([]);
});

// The chart must be exactly as tall as its pane: when SVAR's wrappers had auto height, the chart
// grew to the height of all rows and the pane clipped it — the rows below the fold were
// unreachable and the timeline's horizontal scrollbar was hidden under the pane's bottom edge.
test("gantt fits its pane: last task reachable, timeline scrollbar inside the pane", async ({ page }) => {
  await page.goto("/");
  const chart = page.locator(".wx-chart");
  await expect(page.locator(".wx-bar").first()).toBeVisible();
  // Compare against the nearest clipping ancestor outside SVAR (the layout pane).
  const fit = await chart.evaluate((el) => {
    let pane = el.parentElement;
    while (pane && (getComputedStyle(pane).overflow === "visible" || pane.className.includes("wx-"))) {
      pane = pane.parentElement;
    }
    return { chartBottom: el.getBoundingClientRect().bottom, paneBottom: pane!.getBoundingClientRect().bottom };
  });
  expect(fit.chartBottom).toBeLessThanOrEqual(fit.paneBottom + 1);

  const last = page.getByText("Релиз и ретроспектива").first();
  const box = (await chart.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  for (let i = 0; i < 10 && !(await last.isVisible()); i++) await page.mouse.wheel(0, 300);
  await expect(last).toBeInViewport();
});

// Where the schedule is counted from: a line down the project start column (the demo's first
// task starts there), plus the dates in the legend.
test("project start is marked on the timeline and in the legend", async ({ page }) => {
  await page.goto("/");
  const firstBar = page.locator(".wx-bar").first();
  await expect(firstBar).toBeVisible();
  const line = (await page.locator(".wx-gantt-holidays > .gantt-project-start").boundingBox())!;
  const bar = (await firstBar.boundingBox())!;
  expect(line.height).toBeGreaterThan(100);
  expect(Math.abs(line.x - bar.x)).toBeLessThanOrEqual(2);
  const legend = page.getByText(
    /^Старт \d{2}\.\d{2}\.\d{4} · Окончание \d{2}\.\d{2}\.\d{4} \(\d+ рабоч(ий день|их дня|их дней), всего \d+ (день|дня|дней)\)$/,
  );
  await expect(legend).toBeVisible();
  // The grid's «Начало» column shows the first task (no predecessors) starting on that date.
  const [, dd, mm] = /^Старт (\d{2})\.(\d{2})/.exec(await legend.innerText())!;
  await expect(page.locator(".wx-cell.wx-col-startLabel").filter({ hasText: /^\d{2}\.\d{2}$/ }).first()).toHaveText(`${dd}.${mm}`);
});

// Deleting a link in the chart (select the arrow, then the ✕ on the bar) must reach the server:
// left to SVAR it only vanished in the browser while the dependency kept driving the dates.
test("deleting a link in the chart removes the dependency on the server", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".wx-bar").first()).toBeVisible();
  const depsOnServer = async () => ((await (await page.request.get("/api/plan")).json()).plan.dependencies as unknown[]).length;
  const before = await depsOnServer();

  // Click the middle of a long segment of some arrow (its ends overlap the bars' link handles).
  const pt = await page.evaluate(() => {
    const view = document.querySelector(".wx-chart")!.getBoundingClientRect();
    let best: { x: number; y: number; len: number } | null = null;
    for (const pl of document.querySelectorAll<SVGPolylineElement>(".wx-line-hitbox")) {
      const box = pl.ownerSVGElement!.getBoundingClientRect();
      for (let i = 1; i < pl.points.numberOfItems - 2; i++) {
        const a = pl.points.getItem(i), b = pl.points.getItem(i + 1);
        const len = Math.abs(a.x - b.x) + Math.abs(a.y - b.y);
        const x = box.left + (a.x + b.x) / 2, y = box.top + (a.y + b.y) / 2;
        const onScreen = x > view.left + 5 && x < view.right - 20 && y > view.top + 80 && y < view.bottom - 20;
        if (onScreen && document.elementFromPoint(x, y) === pl && (!best || len > best.len)) best = { x, y, len };
      }
    }
    return best!;
  });
  await page.mouse.click(pt.x, pt.y);
  await page.locator(".wx-delete-button-icon").first().click();

  await expect.poll(depsOnServer).toBe(before - 1);
  // The ✕ sits inside the bar; deleting the link must not also open that task's card.
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

// The browser fires `click` after the press + release that ends a bar drag; it used to open the
// task modal over the change the user had just made.
test("dragging a bar moves the task and does not open the task modal", async ({ page }) => {
  await page.goto("/");
  const bar = page.locator(".wx-bar").first();
  await expect(bar).toBeVisible();
  const id = Number(await bar.getAttribute("data-id"));
  const startOnServer = async () =>
    ((await (await page.request.get("/api/plan")).json()).plan.tasks as { id: number; start: string }[]).find((t) => t.id === id)!.start;
  const before = await startOnServer();

  const box = (await bar.boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 + 40, box.y + box.height / 2, { steps: 8 });
  await page.mouse.move(box.x + box.width / 2 + 77, box.y + box.height / 2, { steps: 8 }); // ~2 day cells
  await page.mouse.up();

  await expect.poll(startOnServer).not.toBe(before);
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

test.describe("phone (390px)", () => {
  test.use({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });

  test("timeline is visible next to a narrow grid", async ({ page }) => {
    const csp = await enforceProductionCsp(page);
    await page.goto("/");
    const bar = page.locator(".wx-bar").first();
    await expect(bar).toBeVisible();
    if (SCREENSHOT_DIR) {
      await page.screenshot({ path: path.join(SCREENSHOT_DIR, "mobile-390.png") });
    }

    const grid = await page.locator(".wx-grid, .wx-table-container").first().boundingBox();
    expect(grid, "grid not rendered").not.toBeNull();
    expect(grid!.width).toBeLessThanOrEqual(185);

    // At least one bar is drawn in the visible part of the timeline, to the right of the grid.
    const visibleBars = await page.locator(".wx-bar").evaluateAll((bars, gridRight) =>
      bars.filter((b) => {
        const r = b.getBoundingClientRect();
        return r.width > 0 && r.left >= gridRight && r.left < window.innerWidth;
      }).length,
      grid!.x + grid!.width,
    );
    expect(visibleBars).toBeGreaterThan(0);
    expect(await csp.violations()).toEqual([]);
  });

  test("a tap on a date picks its column, a tap on a row's pencil opens the card", async ({ page }) => {
    await page.goto("/");
    await expect(page.locator(".wx-bar").first()).toBeVisible();
    const day = page.locator(".wx-scale > .wx-row:last-child > .wx-cell").nth(2);
    await day.tap();
    await expect(day).toHaveClass(/gantt-selected-day/);
    const pencil = page.locator(".gantt-edit-task").first();
    expect((await pencil.boundingBox())!.width).toBeGreaterThanOrEqual(24); // a finger-sized target
    await pencil.tap();
    await expect(page.getByRole("dialog")).toBeVisible();
  });

  test("switching tabs keeps the chat mounted (a running turn is not aborted)", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: "Чат" }).click();
    const input = page.getByRole("textbox", { name: /сообщение/i });
    await input.fill("черновик, который не должен пропасть");
    await page.getByRole("button", { name: "Диаграмма" }).click();
    await expect(page.locator(".wx-bar").first()).toBeVisible();
    await page.getByRole("button", { name: "Чат" }).click();
    // A remounted ChatPanel would have lost its local state (and aborted any in-flight turn).
    await expect(input).toHaveValue("черновик, который не должен пропасть");
  });
});

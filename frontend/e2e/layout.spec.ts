import { expect, test, type Page } from "@playwright/test";

// Layout the user adjusts by hand — chart/chat split, a collapsed chat, grid/timeline divider,
// zoom — survives a reload (kept in localStorage), and «Сбросить раскладку» brings the defaults back.
const chartPane = (page: Page) => page.locator('[role="separator"]').locator("xpath=preceding-sibling::div[1]");

async function drag(page: Page, handle: ReturnType<Page["locator"]>, dx: number) {
  const box = (await handle.boundingBox())!;
  const x = box.x + box.width / 2, y = box.y + Math.min(box.height / 2, 200);
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x + dx / 2, y, { steps: 5 });
  await page.mouse.move(x + dx, y, { steps: 5 });
  await page.mouse.up();
}

test("hand-adjusted layout survives a reload and can be reset", async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/");
  await expect(page.locator(".wx-bar").first()).toBeVisible();
  const defaultPane = (await chartPane(page).boundingBox())!.width;
  const defaultGrid = (await page.locator(".wx-table-container").boundingBox())!.width;

  await drag(page, page.locator('[role="separator"]'), -250);
  await drag(page, page.locator(".wx-resizer").first(), -80);
  await page.getByRole("button", { name: "Неделя" }).click();
  const pane = (await chartPane(page).boundingBox())!.width;
  const grid = (await page.locator(".wx-table-container").boundingBox())!.width;
  expect(pane).toBeLessThan(defaultPane - 200);
  expect(grid).toBeLessThan(defaultGrid - 50);

  await page.reload();
  await expect(page.locator(".wx-bar").first()).toBeVisible();
  expect(Math.abs((await chartPane(page).boundingBox())!.width - pane)).toBeLessThanOrEqual(3);
  expect(Math.abs((await page.locator(".wx-table-container").boundingBox())!.width - grid)).toBeLessThanOrEqual(3);
  await expect(page.getByRole("button", { name: "Неделя" })).toHaveClass(/bg-primary/);

  await page.getByRole("button", { name: "Ещё" }).click();
  await page.getByRole("menuitem", { name: "Сбросить раскладку" }).click();
  await expect(page.locator(".wx-bar").first()).toBeVisible();
  expect(Math.abs((await chartPane(page).boundingBox())!.width - defaultPane)).toBeLessThanOrEqual(3);
  await expect(page.getByRole("button", { name: "День" })).toHaveClass(/bg-primary/);
});

test("the chat folds into a rail so the chart gets the whole width, and stays folded after a reload", async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/");
  await expect(page.locator(".wx-bar").first()).toBeVisible();
  // By default the chat is a fixed ~380px column, whatever the window width.
  const chatWidth = 1600 - (await chartPane(page).boundingBox())!.width;
  expect(chatWidth).toBeGreaterThan(370);
  expect(chatWidth).toBeLessThan(400);

  const input = page.getByRole("textbox", { name: /сообщение/i });
  await input.fill("Сдвинь все задачи Дмитрия на 3 дня");
  await page.keyboard.press("Enter");
  await expect(page.getByText(/Изменено задач: \d+/).first()).toBeVisible({ timeout: 20_000 });

  await page.getByRole("button", { name: "Свернуть чат" }).click();
  await expect(input).toBeHidden();
  await expect(page.locator('[role="separator"]')).toHaveCount(0);
  expect((await page.locator(".wx-gantt").boundingBox())!.width).toBeGreaterThan(1600 - 60);

  await page.reload();
  await expect(page.locator(".wx-bar").first()).toBeVisible();
  await expect(input).toBeHidden();
  await page.getByRole("button", { name: "Развернуть чат" }).click();
  await expect(input).toBeVisible();
  await expect(page.getByText(/Изменено задач: \d+/).first()).toBeVisible();
  expect(Math.abs(1600 - (await chartPane(page).boundingBox())!.width - chatWidth)).toBeLessThanOrEqual(3);
});

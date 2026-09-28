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

// A click on a date in the day scale tints that day's column down the whole chart, like the
// "today" column; a second click on the same date clears it. It doesn't open any task.
test("clicking a date in the day scale highlights its column until clicked again", async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/");
  await expect(page.locator(".wx-bar").first()).toBeVisible();
  const cell = page.locator(".wx-scale > .wx-row:last-child > .wx-cell").nth(5);
  await cell.click();
  await expect(cell).toHaveClass(/gantt-selected-day/);
  const head = (await cell.boundingBox())!;
  const column = (await page.locator(".wx-gantt-holidays > .gantt-selected-day").boundingBox())!;
  expect(Math.abs(column.x - head.x)).toBeLessThanOrEqual(1);
  expect(column.height).toBeGreaterThan(100);
  await expect(page.getByRole("dialog")).toHaveCount(0);

  await cell.click();
  await expect(page.locator(".gantt-selected-day")).toHaveCount(0);
});

// The same without a mouse: the day numbers are buttons with one tab stop; ←/→ move along the
// row, Enter/Space toggle the column.
test("a day column can be picked from the keyboard", async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/");
  await expect(page.locator(".wx-bar").first()).toBeVisible();
  const days = page.locator(".wx-scale > .wx-row:last-child > .wx-cell");
  await expect(days.first()).toHaveAttribute("role", "button");
  await expect(page.locator('.wx-scale [role="button"][tabindex="0"]')).toHaveCount(1);

  const start = days.nth(5);
  await start.focus();
  await page.keyboard.press("Enter");
  await expect(start).toHaveClass(/gantt-selected-day/);
  await expect(start).toHaveAttribute("aria-pressed", "true");

  await page.keyboard.press("ArrowRight");
  await expect(days.nth(6)).toBeFocused();
  await page.keyboard.press(" ");
  await expect(days.nth(6)).toHaveClass(/gantt-selected-day/);
  await expect(start).not.toHaveClass(/gantt-selected-day/);
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

// A single click on a task only selects it (its grid row is tinted); the card opens on a double
// click — on the row or the bar — or from the pencil at the end of the row.
test("a click selects a task, a double click or the pencil opens its card", async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/");
  await expect(page.locator(".wx-bar").first()).toBeVisible();
  const row = page.locator('.wx-table-container [data-id="4"]').first();
  const dialog = page.getByRole("dialog");

  await row.locator(".wx-col-text").click();
  await expect(row).toHaveClass(/wx-selected/);
  await expect(dialog).toHaveCount(0);
  await page.locator('.wx-bar[data-id="6"]').click();
  await expect(page.locator('.wx-table-container [data-id="6"]').first()).toHaveClass(/wx-selected/);
  await expect(dialog).toHaveCount(0);

  await row.locator(".wx-col-text").dblclick();
  await expect(dialog.getByRole("heading").first()).toHaveText(/^№4 /);
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);

  await page.locator('.wx-bar[data-id="6"]').dblclick();
  await expect(dialog.getByRole("heading").first()).toHaveText(/^№6 /);
  await page.keyboard.press("Escape");

  await page.getByRole("button", { name: "Редактировать задачу №2", exact: true }).click();
  await expect(dialog.getByRole("heading").first()).toHaveText(/^№2 /);
});

// Dragging or resizing a bar selects its task, like a click does (SVAR swallows the click that
// ends a drag). The bar's tooltip is hidden while the bar is dragged (it would show the dates from
// before the drag) and shows the saved dates afterwards, not the ones from before.
test("dragging or resizing a bar selects its task; the tooltip never shows stale dates", async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/");
  await expect(page.locator(".wx-bar").first()).toBeVisible();
  const row = (id: number) => page.locator(`.wx-table-container [data-id="${id}"]`).first();
  const tooltip = page.getByRole("tooltip");
  const duration = async (id: number) => {
    const body = await (await page.request.get("/api/plan")).json();
    return (body.plan.tasks as { id: number; duration: number }[]).find((t) => t.id === id)!.duration;
  };

  await row(4).locator(".wx-col-text").click();
  await expect(row(4)).toHaveClass(/wx-selected/);

  // Shrink «Корзина и оформление заказа» (№14) by two day cells from its right edge.
  const before = await duration(14);
  const bar = page.locator('.wx-bar[data-id="14"]');
  await bar.scrollIntoViewIfNeeded();
  const box = (await bar.boundingBox())!;
  const y = box.y + box.height / 2;
  await page.mouse.move(box.x + box.width / 2, y);
  await expect(tooltip).toContainText(`${before} рабоч`);
  const x = box.x + box.width - 3;
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x - 40, y, { steps: 8 });
  await page.mouse.move(x - 77, y, { steps: 8 });
  await expect(tooltip).toBeHidden();
  await page.mouse.up();
  await expect(row(14).locator(".wx-cell").nth(4)).toHaveText(String(before - 2));
  await expect(row(14)).toHaveClass(/wx-selected/);
  await expect(row(4)).not.toHaveClass(/wx-selected/);
  await page.mouse.move(box.x + 10, y, { steps: 3 });
  await expect(tooltip).toContainText(`${before - 2} рабоч`);

  // Moving a whole bar selects it too.
  await page.locator('.wx-bar[data-id="13"]').scrollIntoViewIfNeeded();
  const moved = (await page.locator('.wx-bar[data-id="13"]').boundingBox())!;
  const mx = moved.x + moved.width / 2, my = moved.y + moved.height / 2;
  await page.mouse.move(mx, my);
  await page.mouse.down();
  await page.mouse.move(mx + 40, my, { steps: 8 });
  await page.mouse.move(mx + 77, my, { steps: 8 });
  await page.mouse.up();
  await expect(row(13)).toHaveClass(/wx-selected/);
  await expect(row(14)).not.toHaveClass(/wx-selected/);

  // So does a drag brought back to where it started (SVAR then sends no `update-task`).
  const back = (await page.locator('.wx-bar[data-id="14"]').boundingBox())!;
  const bx = back.x + back.width / 2, by = back.y + back.height / 2;
  await page.mouse.move(bx, by);
  await page.mouse.down();
  await page.mouse.move(bx + 60, by, { steps: 8 });
  await page.mouse.move(bx + 3, by, { steps: 8 });
  await page.mouse.up();
  await expect(row(14)).toHaveClass(/wx-selected/);
  await expect(row(14).locator(".wx-cell").nth(4)).toHaveText(String(before - 2));
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

// A bar spans weekends but its duration counts working days only: Saturdays and Sundays are tinted
// (scale and chart body), and the tooltip names both numbers — «Push-уведомления» (№15) is 5
// working days over 7 calendar days in the demo plan whenever it crosses a weekend.
test("weekends are tinted and the tooltip tells calendar days from working days", async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/");
  await expect(page.locator(".wx-bar").first()).toBeVisible();
  await expect(page.locator(".wx-scale .gantt-weekend").first()).toBeVisible();
  expect(await page.locator(".wx-gantt-holidays > .gantt-weekend").count()).toBeGreaterThan(1);
  const body = await (await page.request.get("/api/plan")).json();
  const tasks = body.plan.tasks as { id: number; start: string; end: string; duration: number }[];
  const days = (t: { start: string; end: string }) => (Date.parse(t.end) - Date.parse(t.start)) / 86_400_000 + 1;
  const crossing = tasks.find((t) => days(t) > t.duration)!;
  const bar = page.locator(`.wx-bar[data-id="${crossing.id}"]`);
  await bar.scrollIntoViewIfNeeded();
  await bar.hover();
  await expect(page.getByRole("tooltip")).toContainText(`из них ${crossing.duration} рабоч`);
  await expect(page.getByRole("tooltip")).toContainText(`${days(crossing)} д`);
});

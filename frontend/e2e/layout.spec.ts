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

// Hovers until the bar's tooltip shows `text`. SVAR's tooltip appears 300 ms after a mousemove and
// drops on any scroll event — including the one a scrollIntoViewIfNeeded() just before fires a
// frame later — so a single move can leave it hidden for good; wiggle the pointer until it shows.
async function hoverTooltip(page: Page, x: number, y: number, text: string | RegExp) {
  await expect(async () => {
    await page.mouse.move(x + 1, y);
    await page.mouse.move(x, y);
    await expect(page.getByRole("tooltip")).toContainText(text, { timeout: 1_000 });
  }).toPass({ timeout: 10_000 });
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

// A click on a task — its row or its bar — opens the card, as the brief asks, and tints the row as
// selected; so does the pencil at the end of the row. A double click out of habit leaves the card
// open (its second click must not land on the backdrop and close it).
test("a click on a row or a bar opens its card and selects it", async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/");
  await expect(page.locator(".wx-bar").first()).toBeVisible();
  const row = page.locator('.wx-table-container [data-id="4"]').first();
  const dialog = page.getByRole("dialog");

  await row.locator(".wx-col-text").click();
  await expect(dialog.getByRole("heading").first()).toHaveText(/^№4 /);
  await expect(row).toHaveClass(/wx-selected/);
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);

  await page.locator('.wx-bar[data-id="6"]').click();
  await expect(dialog.getByRole("heading").first()).toHaveText(/^№6 /);
  await expect(page.locator('.wx-table-container [data-id="6"]').first()).toHaveClass(/wx-selected/);
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);

  // A person's double click: the card is already open when the second click comes (≈120 ms later).
  const bar6 = (await page.locator('.wx-bar[data-id="6"]').boundingBox())!;
  await page.mouse.click(bar6.x + bar6.width / 2, bar6.y + bar6.height / 2);
  await page.waitForTimeout(120);
  await page.mouse.click(bar6.x + bar6.width / 2, bar6.y + bar6.height / 2, { clickCount: 2 });
  await page.waitForTimeout(500);
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
  await page.keyboard.press("Escape"); // the click opened №4's card; the row stays selected
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(row(4)).toHaveClass(/wx-selected/);

  // Shrink «Корзина и оформление заказа» (№14) by two day cells from its right edge.
  const before = await duration(14);
  const bar = page.locator('.wx-bar[data-id="14"]');
  await bar.scrollIntoViewIfNeeded();
  const box = (await bar.boundingBox())!;
  const y = box.y + box.height / 2;
  await hoverTooltip(page, box.x + box.width / 2, y, `${before} рабоч`);
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
  await hoverTooltip(page, box.x + 10, y, `${before - 2} рабоч`);

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
  await expect(page.getByRole("dialog")).toHaveCount(0); // a drag, even one brought back, isn't a click
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
  const b = (await bar.boundingBox())!;
  await hoverTooltip(page, b.x + b.width / 2, b.y + b.height / 2, `из них ${crossing.duration} рабоч`);
  await expect(page.getByRole("tooltip")).toContainText(`${days(crossing)} д`);
});

// A change to a task that's off screen used to happen unseen. The agent's change scrolls the chart
// to it; one from elsewhere (here: straight to the API, as another tab or an MCP client would)
// raises a notice whose «Показать» scrolls there; a click on a change in the chat scrolls too.
test("changes to tasks off screen are brought into view", async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/");
  await expect(page.locator(".wx-bar").first()).toBeVisible();
  const onScreen = (id: number) =>
    page.evaluate((id) => {
      const bar = document.querySelector(`.wx-bar[data-id="${id}"]`);
      const chart = document.querySelector(".wx-chart")!.getBoundingClientRect();
      const gantt = document.querySelector(".wx-gantt")!.getBoundingClientRect();
      const top = document.querySelector(".wx-scale")?.getBoundingClientRect().bottom ?? gantt.top;
      if (!bar) return false;
      const b = bar.getBoundingClientRect();
      return b.left < chart.right && b.right > chart.left && b.top < gantt.bottom && b.bottom > top;
    }, id);
  const scrollHome = async () => {
    await page.locator(".wx-gantt").evaluate((el) => el.scrollTo({ top: 0 }));
    await page.locator(".wx-chart").evaluate((el) => el.scrollTo({ left: 0 }));
    await expect.poll(() => onScreen(21)).toBe(false);
  };
  await expect.poll(() => onScreen(1)).toBe(true);
  expect(await onScreen(21)).toBe(false); // the last row is below the visible ones

  // 1. The agent changes №21: the chart scrolls to it.
  const input = page.getByRole("textbox", { name: /сообщение/i });
  await input.fill("Назначь задачу 21 на Игоря Петрова");
  await page.keyboard.press("Enter");
  await expect(page.getByText(/Изменено задач: \d+/).first()).toBeVisible({ timeout: 20_000 });
  await expect.poll(() => onScreen(21), { timeout: 10_000 }).toBe(true);

  // 2. A change from elsewhere: no jump, a notice instead; «Показать» scrolls to the task.
  await scrollHome();
  await page.request.post("/api/plan/operations", {
    data: { ops: [{ op: "update_task", id: 21, duration: 6 }] },
    headers: { Origin: new URL(page.url()).origin },
  });
  const notice = page.getByRole("status").filter({ hasText: "вне видимой области" });
  await expect(notice).toContainText(/Изменено \d+ задач/); // №21 and whatever its new duration pushed
  expect(await onScreen(21)).toBe(false);
  await notice.getByRole("button", { name: "Показать" }).click();
  await expect.poll(() => onScreen(21), { timeout: 10_000 }).toBe(true);
  await expect(notice).toHaveCount(0);

  // 3. A click on the change in the chat's summary scrolls to that task as well.
  await scrollHome();
  await page.getByText(/Изменено задач: \d+/).first().click();
  await page.getByRole("button", { name: /№21/ }).first().click();
  await expect.poll(() => onScreen(21), { timeout: 10_000 }).toBe(true);
});

import { expect, test, type Page } from "@playwright/test";

// The chat starts over on every page load and on «Очистить»; the earlier conversations are kept
// under «История» (read-only). Runs against the fake LLM: any text gets a canned reply.
const input = (page: Page) => page.getByRole("textbox", { name: "Сообщение агенту" });
const emptyChat = (page: Page) => page.getByText("Напишите, что изменить в плане, например:");

async function ask(page: Page, text: string) {
  await input(page).fill(text);
  await input(page).press("Enter");
  await expect(page.getByText(text, { exact: true })).toBeVisible();
  await expect(input(page)).toBeEnabled({ timeout: 30_000 });
}

test("a reload and «Очистить» start the chat over, «История» keeps what was there", async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/");
  await expect(page.locator(".wx-bar").first()).toBeVisible();
  const clear = page.getByRole("button", { name: "Очистить" });
  await expect(emptyChat(page)).toBeVisible();
  await expect(clear).toBeDisabled(); // nothing to clear yet

  await ask(page, "первый вопрос");
  await page.reload();
  await expect(page.locator(".wx-bar").first()).toBeVisible();
  await expect(emptyChat(page)).toBeVisible();
  await expect(page.getByText("первый вопрос", { exact: true })).toHaveCount(0);

  await ask(page, "второй вопрос");
  await expect(clear).toBeEnabled();
  await clear.click();
  await expect(emptyChat(page)).toBeVisible();
  await expect(page.getByText("второй вопрос", { exact: true })).toHaveCount(0);

  await page.getByRole("button", { name: "История" }).click();
  const dialog = page.getByRole("dialog");
  const items = dialog.getByRole("listitem");
  await expect(items).toHaveCount(2);
  await expect(items.nth(0)).toContainText("второй вопрос"); // most recent first
  await expect(items.nth(1)).toContainText("первый вопрос");
  await expect(items.nth(1)).toContainText("2 сообщения");

  await items.nth(1).getByRole("button").click();
  await expect(dialog.getByText("первый вопрос", { exact: true })).toBeVisible();
  await expect(dialog.getByText(/демо-режиме/)).toBeVisible(); // the reply, as saved
  await dialog.getByRole("button", { name: /Все диалоги/ }).click();
  await expect(items).toHaveCount(2);
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(emptyChat(page)).toBeVisible(); // viewing history doesn't bring it back
});

test("«Сегодня» puts today's column in the middle of the chart", async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/");
  await expect(page.locator(".wx-bar").first()).toBeVisible();
  const chart = page.locator(".wx-chart");
  // Scroll away first: the chart opens at the project's start.
  await chart.evaluate((el) => {
    el.scrollLeft = el.scrollWidth;
  });
  await page.getByRole("button", { name: "Сегодня" }).click();
  const today = page.locator(".wx-gantt-holidays > .gantt-today");
  await expect(async () => {
    const view = (await chart.boundingBox())!;
    const column = (await today.boundingBox())!;
    const offset = column.x + column.width / 2 - (view.x + view.width / 2);
    expect(Math.abs(offset)).toBeLessThanOrEqual(column.width);
  }).toPass({ timeout: 5_000 });
});

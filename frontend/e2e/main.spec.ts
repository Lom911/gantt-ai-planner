import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";

// frontend/package.json has "type": "module", so __dirname isn't available here — derive it
// from import.meta.url instead (the brief's snippet assumed CommonJS).
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SAMPLE = path.resolve(__dirname, "../../examples/sample-plan.xlsx");

test("demo → import → chat edit → export", async ({ page }) => {
  await page.goto("/");
  // Each task name is rendered twice — once in the grid's "Задача" column (visible in this
  // build) and once as the Gantt bar label. Both are on-screen, but the bar label comes after
  // the grid cell in the DOM, so `.last()` reliably targets it; the brief's `.first()` would
  // hit the grid cell instead.
  await expect(page.getByText("Сбор требований и приоритизация").last()).toBeVisible();

  await page.getByRole("button", { name: "Загрузить Excel" }).click();
  await page.locator('input[type="file"]').setInputFiles(SAMPLE);
  await page.getByRole("button", { name: "Загрузить", exact: true }).click();
  await expect(page.getByText("Упаковка мебели").last()).toBeVisible();
  await expect(page.getByText("Сбор требований и приоритизация")).toHaveCount(0);

  await page.getByRole("textbox", { name: /сообщение/i }).fill("Сдвинь все задачи Олега на 3 дня");
  await page.keyboard.press("Enter");
  // The fake LLM answers at once; pointed at a live deployment (E2E_BASE_URL) a real model's turn
  // takes 15–40 s.
  const live = !/localhost|127\.0\.0\.1/.test(String(test.info().project.use.baseURL));
  await expect(page.getByText(/Изменено задач: \d+/).first()).toBeVisible({ timeout: live ? 120_000 : 20_000 });

  const download = page.waitForEvent("download");
  await page.getByRole("link", { name: "Экспорт" }).click();
  expect((await download).suggestedFilename()).toMatch(/^plan-\d{4}-\d{2}-\d{2}\.xlsx$/);

  await page.getByText("Упаковка мебели").last().dblclick();
  await expect(page.getByRole("dialog")).toContainText("Упаковка мебели");
});

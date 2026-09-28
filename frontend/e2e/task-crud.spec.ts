import { expect, test, type Page } from "@playwright/test";

// Adding and deleting tasks from the UI: «Добавить после» in a task's card inserts a task into
// an existing A → B link (A → new → B, placed right after A in the list), the toolbar button
// appends one, and «Удалить задачу» removes it and reconnects A → B.
interface Plan {
  last_id: number;
  tasks: { id: number; name: string }[];
  dependencies: { predecessor_id: number; successor_id: number; lag: number }[];
}
const getPlan = async (page: Page): Promise<Plan> => (await (await page.request.get("/api/plan")).json()).plan;
const linked = (p: Plan, a: number, b: number) =>
  p.dependencies.find((d) => d.predecessor_id === a && d.successor_id === b);

test("insert a task between two linked tasks, append one, then delete the inserted one", async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/");
  await expect(page.locator(".wx-bar").first()).toBeVisible();
  const before = await getPlan(page);
  const { predecessor_id: a, successor_id: b, lag } = before.dependencies[0];
  const dialog = page.getByRole("dialog");

  // Insert between A and B from A's card.
  await page.getByRole("button", { name: `Редактировать задачу №${a}`, exact: true }).click();
  await dialog.getByRole("button", { name: "Добавить после" }).click();
  await expect(dialog.getByRole("heading", { name: "Новая задача" })).toBeVisible();
  await dialog.getByLabel("Название").fill("Вставка e2e");
  await dialog.getByLabel("Перед задачей").selectOption(String(b));
  await expect(dialog.getByText(/Встанет между ними/)).toBeVisible();
  await dialog.getByRole("button", { name: "Добавить" }).click();
  await expect(dialog).toHaveCount(0);

  const inserted = before.last_id + 1;
  const afterInsert = await getPlan(page);
  const ids = afterInsert.tasks.map((t) => t.id);
  expect(ids.indexOf(inserted)).toBe(ids.indexOf(a) + 1);
  expect(linked(afterInsert, a, inserted)).toBeTruthy();
  expect(linked(afterInsert, inserted, b)?.lag).toBe(lag);
  expect(linked(afterInsert, a, b)).toBeUndefined();
  await expect(page.locator(`.wx-table-container [data-id="${inserted}"]`).first()).toContainText("Вставка e2e");

  // Append one from the toolbar: end of the list, no links.
  await page.getByRole("button", { name: "Добавить задачу" }).click();
  await dialog.getByLabel("Название").fill("В конец e2e");
  await dialog.getByRole("button", { name: "Добавить" }).click();
  await expect(dialog).toHaveCount(0);
  const afterAppend = await getPlan(page);
  expect(afterAppend.tasks.at(-1)).toMatchObject({ id: inserted + 1, name: "В конец e2e" });
  expect(afterAppend.dependencies.some((d) => d.successor_id === inserted + 1 || d.predecessor_id === inserted + 1)).toBe(false);

  // Delete the inserted task: A → B comes back with its lag.
  await page.getByRole("button", { name: `Редактировать задачу №${inserted}`, exact: true }).click();
  await dialog.getByRole("button", { name: "Удалить задачу" }).click();
  await dialog.getByRole("button", { name: "Удалить", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  const afterDelete = await getPlan(page);
  expect(afterDelete.tasks.some((t) => t.id === inserted)).toBe(false);
  expect(linked(afterDelete, a, b)?.lag).toBe(lag);
});

test("the form refuses a cycle up front, and a cancelled delete doesn't come back on reopen", async ({ page }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/");
  await expect(page.locator(".wx-bar").first()).toBeVisible();
  const before = await getPlan(page);
  const { predecessor_id: a, successor_id: b } = before.dependencies[0];
  const dialog = page.getByRole("dialog");

  // After B and before A, while A → B already: B → new → A → B would be a cycle.
  await page.getByRole("button", { name: "Добавить задачу" }).click();
  await dialog.getByLabel("Название").fill("Цикл");
  await dialog.getByLabel("Начать после задачи").selectOption(String(b));
  await dialog.getByLabel("Перед задачей").selectOption(String(a));
  await dialog.getByRole("button", { name: "Добавить" }).click();
  await expect(dialog.getByText(new RegExp(`№${a} уже идёт раньше №${b}`))).toBeVisible();
  expect((await getPlan(page)).last_id).toBe(before.last_id);
  await dialog.getByRole("button", { name: "Отмена" }).click();

  // «Удалить задачу», then close the card: reopening it shows the normal footer again.
  await page.getByRole("button", { name: `Редактировать задачу №${a}`, exact: true }).click();
  await dialog.getByRole("button", { name: "Удалить задачу" }).click();
  await expect(dialog.getByRole("button", { name: "Не удалять" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await page.getByRole("button", { name: `Редактировать задачу №${a}`, exact: true }).click();
  await expect(dialog.getByRole("button", { name: "Удалить задачу" })).toBeVisible();
  await expect(dialog.getByRole("button", { name: "Не удалять" })).toHaveCount(0);
});

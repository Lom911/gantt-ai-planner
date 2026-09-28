// Full UI run. In CI it runs against the e2e stack (fake LLM); it can also be pointed at a live
// deployment (real LLM):
//   E2E_BASE_URL=https://gantt-ai-planner.duckdns.org npx playwright test -c e2e/playwright.config.ts e2e/full-ui.spec.ts
// Clicks through every control and checks each change three ways: the UI (date caption, every
// grid row, bar colors, «Загрузка») against GET /api/plan, the whole schedule against an
// independent re-derivation (durations, FS deps + lag, «не раньше», project end, critical =
// zero slack), and exact expected values for the action (e.g. drag 2 cells → start +2 days).
// One page = one session (the server allows 20 new sessions per IP per hour). It mutates the
// session's plan and ends with «Удалить мои данные». The chat step depends on the live model.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page } from "@playwright/test";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SAMPLE = path.resolve(__dirname, "../../examples/sample-plan.xlsx");

type Task = { id: number; name: string; description: string; assignee: string | null; duration: number; start: string; end: string;
  slack: number; is_critical: boolean; overallocated_with: number[]; constraint_start: string | null };
type Dep = { predecessor_id: number; successor_id: number; lag: number };
type PlanResp = { version: number; can_undo: boolean; can_redo: boolean;
  plan: { project_start: string; project_end: string; tasks: Task[]; dependencies: Dep[] } };

const ddmm = (iso: string) => `${iso.slice(8, 10)}.${iso.slice(5, 7)}`;
const ru = (iso: string) => `${ddmm(iso)}.${iso.slice(0, 4)}`;
const dow = (iso: string) => new Date(`${iso}T12:00:00Z`).getUTCDay();
function addDays(iso: string, n: number): string {
  const d = new Date(`${iso}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}
const nextWorkday = (iso: string) => (dow(iso) === 6 ? addDays(iso, 2) : dow(iso) === 0 ? addDays(iso, 1) : iso);
function addWorkdays(iso: string, n: number): string {
  let d = iso;
  let left = Math.abs(n);
  while (left > 0) {
    d = addDays(d, n >= 0 ? 1 : -1);
    if (dow(d) !== 0 && dow(d) !== 6) left--;
  }
  return d;
}
function workdaysInclusive(a: string, b: string): number {
  let n = 0;
  for (let d = a; d <= b; d = addDays(d, 1)) if (dow(d) !== 0 && dow(d) !== 6) n++;
  return n;
}

const results: { step: string; ok: boolean; detail: string }[] = [];
function check(step: string, ok: boolean, detail = "") {
  results.push({ step, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"} | ${step}${detail ? " | " + detail : ""}`);
}

const plan = async (page: Page): Promise<PlanResp> => (await page.request.get("/api/plan")).json();
const task = (p: PlanResp, id: number) => p.plan.tasks.find((t) => t.id === id)!;

// Re-derive every task's dates from the plan's own inputs (duration, constraint, FS deps + lag).
function invariants(p: PlanResp): string[] {
  const bad: string[] = [];
  const ps = nextWorkday(p.plan.project_start);
  for (const t of p.plan.tasks) {
    if (t.end !== addWorkdays(t.start, t.duration - 1)) bad.push(`#${t.id} конец ${t.end}≠старт+${t.duration - 1} раб.дн`);
    const cands = [ps];
    if (t.constraint_start) cands.push(nextWorkday(t.constraint_start));
    for (const d of p.plan.dependencies.filter((d) => d.successor_id === t.id)) cands.push(addWorkdays(task(p, d.predecessor_id).end, 1 + d.lag));
    const es = cands.sort().at(-1)!;
    if (t.start !== es) bad.push(`#${t.id} старт ${t.start}≠${es}`);
    if (t.is_critical !== (t.slack === 0)) bad.push(`#${t.id} критичность≠(резерв 0)`);
  }
  const maxEnd = p.plan.tasks.map((t) => t.end).sort().at(-1);
  if (p.plan.project_end !== maxEnd) bad.push(`окончание ${p.plan.project_end}≠max(конец) ${maxEnd}`);
  return bad;
}

async function gridRows(page: Page) {
  return page.$$eval(".wx-table-container [data-id]", (els) =>
    els.filter((e) => e.querySelector(".wx-col-startLabel")).map((e) => ({
      id: Number(e.getAttribute("data-id")),
      start: e.querySelector(".wx-col-startLabel")!.textContent!.trim(),
      days: e.querySelector(".wx-col-workDays")!.textContent!.trim(),
      assignee: e.querySelector(".wx-col-assignee")!.textContent!.trim(),
    })));
}

async function uiDiff(page: Page, p: PlanResp): Promise<{ bad: string[]; rows: number; bars: number }> {
  const bad: string[] = [];
  const caption = await page.getByText(/^Старт \d{2}\.\d{2}\.\d{4} · Окончание/).innerText();
  const span = Math.round((Date.parse(p.plan.project_end) - Date.parse(p.plan.project_start)) / 86_400_000) + 1;
  const work = workdaysInclusive(p.plan.project_start, p.plan.project_end);
  const want = `Старт ${ru(p.plan.project_start)} · Окончание ${ru(p.plan.project_end)} (${work} рабоч`;
  if (!caption.startsWith(want) || !caption.includes(`, всего ${span} `)) bad.push(`подпись «${caption}»≠«${want}…, всего ${span}…»`);
  // all grid rows: top of the list, then scrolled to the bottom (the grid is virtualized)
  const seen = new Map<number, Awaited<ReturnType<typeof gridRows>>[number]>();
  for (const r of await gridRows(page)) seen.set(r.id, r);
  await page.locator(".wx-gantt").evaluate((el) => { el.scrollTop = el.scrollHeight; });
  await page.waitForTimeout(300);
  for (const r of await gridRows(page)) seen.set(r.id, r);
  await page.locator(".wx-gantt").evaluate((el) => { el.scrollTop = 0; });
  for (const r of seen.values()) {
    const t = task(p, r.id);
    if (!t) { bad.push(`строка #${r.id} не на сервере`); continue; }
    if (r.start !== ddmm(t.start)) bad.push(`#${r.id} Начало ${r.start}≠${ddmm(t.start)}`);
    if (r.days !== String(t.duration)) bad.push(`#${r.id} Дн. ${r.days}≠${t.duration}`);
    if (r.assignee !== (t.assignee ?? "")) bad.push(`#${r.id} исп. «${r.assignee}»≠«${t.assignee ?? ""}»`);
  }
  if (seen.size !== p.plan.tasks.length) bad.push(`в таблице ${seen.size} строк, на сервере ${p.plan.tasks.length} задач`);
  const bars = await page.$$eval(".wx-bar[data-id]", (els) => els.map((e) => ({ id: Number(e.getAttribute("data-id")), cls: e.className })));
  for (const b of bars) {
    const t = task(p, b.id);
    if (!t) continue;
    const want = t.is_critical ? "critical" : t.overallocated_with.length ? "conflict" : "";
    const has = /\bchanged\b/.test(b.cls) ? "changed" : /\bcritical\b/.test(b.cls) ? "critical" : /\bconflict\b/.test(b.cls) ? "conflict" : "";
    if (want !== has) bad.push(`полоса #${b.id} цвет ${has || "обычный"}≠${want || "обычный"}`);
  }
  return { bad, rows: seen.size, bars: bars.length };
}

// Wait until the UI shows the server state (no fixed sleeps), then record both checks.
async function consistency(page: Page, label: string): Promise<PlanResp> {
  let last: { bad: string[]; rows: number; bars: number } = { bad: ["not checked"], rows: 0, bars: 0 };
  let p = await plan(page);
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    p = await plan(page);
    last = await uiDiff(page, p);
    if (last.bad.length === 0) break;
    await page.waitForTimeout(500);
  }
  check(`${label}: интерфейс = сервер (${last.rows} строк, ${last.bars} полос)`, last.bad.length === 0, last.bad.slice(0, 5).join("; "));
  const inv = invariants(p);
  check(`${label}: расписание пересчитано верно (все ${p.plan.tasks.length} задач)`, inv.length === 0, inv.slice(0, 5).join("; "));
  return p;
}

// Run an action and wait for the plan version to change on the server.
async function mutate(page: Page, timeoutOrAction: number | (() => Promise<void>), maybeAction?: () => Promise<void>): Promise<PlanResp> {
  const timeout = typeof timeoutOrAction === "number" ? timeoutOrAction : 30_000;
  const action = typeof timeoutOrAction === "number" ? maybeAction! : timeoutOrAction;
  const v = (await plan(page)).version;
  await action();
  await expect.poll(async () => (await plan(page)).version, { timeout }).not.toBe(v);
  return plan(page);
}
async function openTask(page: Page, id: number) {
  await page.locator(`.wx-table-container [data-id="${id}"] .wx-col-text`).first().dblclick();
  await expect(page.getByRole("dialog")).toBeVisible();
  return page.getByRole("dialog");
}
const undo = (page: Page) => mutate(page, () => page.getByRole("button", { name: "Отменить" }).click());
const redo = (page: Page) => mutate(page, () => page.getByRole("button", { name: "Повторить" }).click());
async function saveTask(page: Page, id: number, fill: (dlg: ReturnType<Page["getByRole"]>) => Promise<void>) {
  return mutate(page, async () => {
    const dlg = await openTask(page, id);
    await fill(dlg);
    await dlg.getByRole("button", { name: "Сохранить" }).click();
    await expect(dlg).toBeHidden();
  });
}
async function resetToDemo(page: Page) {
  return mutate(page, async () => {
    await page.getByRole("button", { name: "Сбросить к демо" }).click();
    await page.getByRole("button", { name: "Сбросить", exact: true }).click();
  });
}

test.setTimeout(900_000);

test("full UI run: every control, numbers checked after each action", async ({ page }, testInfo) => {
  const OUT = testInfo.outputDir;
  fs.mkdirSync(OUT, { recursive: true });
  let afterDelete = false;
  const problems: string[] = [];
  const note = (s: string) => problems.push(afterDelete ? `[после удаления] ${s}` : s);
  page.on("console", (m) => { if (m.type() === "error") note(`console: ${m.text()}`); });
  page.on("pageerror", (e) => note(`pageerror: ${e.message}`));
  page.on("response", (r) => { if (r.status() >= 400) note(`${r.status()} ${r.request().method()} ${new URL(r.url()).pathname}`); });

  await page.setViewportSize({ width: 1600, height: 900 });
  await page.goto("/");
  await expect(page.locator(".wx-bar").first()).toBeVisible();

  // 0. known demo state
  await resetToDemo(page);
  let p0 = await consistency(page, "Демо");
  check("Демо: 25 задач, отмена доступна, повтор нет", p0.plan.tasks.length === 25 && p0.can_undo && !p0.can_redo);

  // 1. modal: +2 days on a critical task that has successors
  const succOf = (p: PlanResp, id: number) => p.plan.dependencies.filter((d) => d.predecessor_id === id).map((d) => d.successor_id);
  const crit = p0.plan.tasks.find((t) => t.is_critical && t.id > 1 && succOf(p0, t.id).length > 0 && t.id <= 12)!;
  const p1 = await saveTask(page, crit.id, (dlg) => dlg.getByLabel("Длительность, дн.").fill(String(crit.duration + 2)));
  await consistency(page, `Карточка: #${crit.id} «${crit.name}» +2 дня`);
  check(`#${crit.id} длительность ${crit.duration}→${task(p1, crit.id).duration}`, task(p1, crit.id).duration === crit.duration + 2);
  check(`Окончание проекта ${ru(p0.plan.project_end)}→${ru(p1.plan.project_end)} (задача на крит. пути, +2 раб. дня)`, p1.plan.project_end === addWorkdays(p0.plan.project_end, 2));
  const moved = p1.plan.tasks.filter((t) => t.start !== task(p0, t.id).start).map((t) => `#${t.id} ${ddmm(task(p0, t.id).start)}→${ddmm(t.start)}`);
  check(`Сдвинулись последователи: ${moved.length} задач`, moved.length > 0, moved.slice(0, 6).join(", "));

  // 2. undo / redo
  let p2 = await undo(page);
  await consistency(page, "Отменить");
  check("Отменить: план как до правки, доступен «Повторить»", p2.plan.project_end === p0.plan.project_end && task(p2, crit.id).duration === crit.duration && p2.can_redo);
  p2 = await redo(page);
  await consistency(page, "Повторить");
  check("Повторить: снова +2", p2.plan.project_end === p1.plan.project_end && task(p2, crit.id).duration === crit.duration + 2);
  await undo(page);

  // 3. name + description, assignee → grid + «Загрузка», clearing the assignee
  p0 = await plan(page);
  const t3 = task(p0, 3);
  let p3 = await saveTask(page, 3, async (dlg) => {
    await dlg.getByLabel("Название").fill(`${t3.name} (проверка)`);
    await dlg.getByLabel("Описание").fill("Описание из теста");
    await dlg.getByLabel("Исполнитель").fill("Тестовый Исполнитель");
  });
  await consistency(page, "Карточка: название, описание, исполнитель #3");
  check("Сервер: #3 название/описание/исполнитель", task(p3, 3).name === `${t3.name} (проверка)` && task(p3, 3).description === "Описание из теста" && task(p3, 3).assignee === "Тестовый Исполнитель");
  check("Таблица: новое название #3", await page.locator(`.wx-table-container [data-id="3"] .wx-col-text`).first().innerText() === `${t3.name} (проверка)`);
  await page.getByRole("button", { name: "Загрузка" }).click();
  const byAssignee = new Map<string, { n: number; days: number }>();
  for (const t of p3.plan.tasks) {
    const k = t.assignee ?? "Без исполнителя";
    const v = byAssignee.get(k) ?? { n: 0, days: 0 };
    byAssignee.set(k, { n: v.n + 1, days: v.days + t.duration });
  }
  const items = await page.locator("ul li").evaluateAll((els) => els.map((e) => e.textContent!.replace(/\s+/g, " ")));
  const panelBad: string[] = [];
  for (const [name, v] of byAssignee) {
    const it = items.find((s) => s.startsWith(name));
    if (!it || !it.includes(`${v.n} задач`) || !it.includes(`${v.days} рабоч`)) panelBad.push(`${name}: ждали ${v.n} задач/${v.days} дн, есть «${it?.slice(0, 70)}»`);
  }
  check(`Загрузка: задачи и рабочие дни у всех ${byAssignee.size} исполнителей = сервер`, panelBad.length === 0, panelBad.slice(0, 3).join("; "));
  await page.getByRole("button", { name: "Загрузка" }).click();
  p3 = await saveTask(page, 3, (dlg) => dlg.getByLabel("Исполнитель").fill(""));
  await consistency(page, "Очистка исполнителя #3");
  check("Сервер: у #3 нет исполнителя", task(p3, 3).assignee === null, String(task(p3, 3).assignee));
  await undo(page);
  await undo(page);
  const back = await plan(page);
  check("Две отмены: #3 как было", task(back, 3).name === t3.name && task(back, 3).assignee === t3.assignee && task(back, 3).description === t3.description);

  // 4. «Не раньше» set, then removed with «Убрать»
  p0 = await plan(page);
  const t4 = p0.plan.tasks.find((t) => !t.is_critical && t.slack >= 3 && t.id <= 12) ?? task(p0, 7);
  let target = addDays(t4.start, 7);
  while ([0, 6].includes(dow(target))) target = addDays(target, 1);
  const p4 = await saveTask(page, t4.id, (dlg) => dlg.getByLabel("Не раньше").fill(target));
  await consistency(page, `«Не раньше» #${t4.id} = ${ddmm(target)}`);
  check(`#${t4.id} старт ${ddmm(t4.start)}→${ddmm(task(p4, t4.id).start)} (ждали ${ddmm(target)})`, task(p4, t4.id).start === target && task(p4, t4.id).constraint_start === target);
  const p4b = await saveTask(page, t4.id, (dlg) => dlg.getByRole("button", { name: "Убрать" }).click());
  await consistency(page, "«Убрать» ограничение");
  check(`«Убрать»: #${t4.id} вернулся на ${ddmm(t4.start)}`, task(p4b, t4.id).constraint_start === null && task(p4b, t4.id).start === t4.start, ddmm(task(p4b, t4.id).start));

  // 5. drag +2 cells → exact date; 6. resize end +2 cells → exact duration
  p0 = await plan(page);
  const dragId = 7;
  const bar = page.locator(`.wx-bar[data-id="${dragId}"]`);
  const drag = async (fromX: (b: { x: number; width: number }) => number) => {
    const box = (await bar.boundingBox())!;
    const x = fromX(box), y = box.y + box.height / 2;
    await page.mouse.move(x, y);
    await page.mouse.down();
    await page.mouse.move(x + 40, y, { steps: 8 });
    await page.mouse.move(x + 77, y, { steps: 8 });
    await page.mouse.up();
  };
  const p5 = await mutate(page, () => drag((b) => b.x + b.width / 2));
  check("Перетаскивание: карточка не открылась", (await page.getByRole("dialog").count()) === 0);
  await consistency(page, `Перетаскивание #${dragId} на 2 дня`);
  const wantStart = nextWorkday(addDays(task(p0, dragId).start, 2));
  check(`#${dragId} старт ${ddmm(task(p0, dragId).start)}→${ddmm(task(p5, dragId).start)} (ждали ${ddmm(wantStart)})`, task(p5, dragId).start === wantStart && task(p5, dragId).duration === task(p0, dragId).duration);
  await undo(page);
  p0 = await plan(page);
  const p6 = await mutate(page, () => drag((b) => b.x + b.width - 3));
  check("Растягивание: карточка не открылась", (await page.getByRole("dialog").count()) === 0);
  await consistency(page, `Растягивание #${dragId} на 2 дня`);
  const wantDur = workdaysInclusive(task(p0, dragId).start, addDays(task(p0, dragId).end, 2));
  check(`#${dragId} длительность ${task(p0, dragId).duration}→${task(p6, dragId).duration} (ждали ${wantDur}), старт на месте`, task(p6, dragId).duration === wantDur && task(p6, dragId).start === task(p0, dragId).start);
  await undo(page);

  // 7. draw a link with the mouse: exact edge, then undo
  p0 = await plan(page);
  const src = 7, dst = 10;
  const edge = (p: PlanResp) => p.plan.dependencies.find((d) => d.predecessor_id === src && d.successor_id === dst);
  check(`До: связи #${src}→#${dst} нет`, !edge(p0));
  const p7 = await mutate(page, async () => {
    await page.locator(`.wx-bar[data-id="${src}"]`).hover();
    await page.locator(`.wx-bar[data-id="${src}"] .wx-link.wx-right`).click();
    await page.locator(`.wx-bar[data-id="${dst}"]`).hover();
    await page.locator(`.wx-bar[data-id="${dst}"] .wx-link.wx-left`).click();
  });
  await consistency(page, `Связь #${src}→#${dst} мышью`);
  check(`Сервер: связь #${src}→#${dst}, задержка 0, всего ${p0.plan.dependencies.length}→${p7.plan.dependencies.length}`, edge(p7)?.lag === 0 && p7.plan.dependencies.length === p0.plan.dependencies.length + 1);
  const p7u = await undo(page);
  check("Отменить: связи нет", !edge(p7u) && p7u.plan.dependencies.length === p0.plan.dependencies.length);

  // 8. chat with the real LLM
  p0 = await plan(page);
  const dima = p0.plan.tasks.filter((t) => t.assignee?.startsWith("Дмитрий"));
  const input = page.getByRole("textbox", { name: /сообщение/i });
  const p8 = await mutate(page, 180_000, async () => {
    await input.fill("Сдвинь все задачи Дмитрия на 3 дня");
    await page.keyboard.press("Enter");
  });
  await expect(page.getByText(/Изменено задач: \d+/).first()).toBeVisible({ timeout: 180_000 });
  await consistency(page, "Чат: сдвиг задач Дмитрия на 3 дня");
  const reply = (await page.locator(".bg-secondary").filter({ hasText: /Изменено задач/ }).last().innerText()).split("\n")[0];
  check("Ответ агента: без Markdown-звёздочек и ISO-дат", !reply.includes("**") && !/\d{4}-\d{2}-\d{2}/.test(reply), reply.slice(0, 160));
  const shifted = dima.map((t) => ({ id: t.id, from: t.start, to: task(p8, t.id).start }));
  check(`Чат: все ${dima.length} задач Дмитрия сдвинуты на ≥3 раб. дня`, shifted.every((s) => s.to >= addWorkdays(s.from, 3)), shifted.map((s) => `#${s.id} ${ddmm(s.from)}→${ddmm(s.to)}`).join(", "));
  const p8u = await undo(page);
  check("Отменить ход агента целиком: план как до чата", p8u.plan.project_end === p0.plan.project_end && dima.every((t) => task(p8u, t.id).start === t.start));

  // 9. zoom
  await page.getByRole("button", { name: "Неделя" }).click();
  check("Масштаб «Неделя»: шкала недель", (await page.locator(".wx-scale").innerText()).includes("Нед."));
  await page.getByRole("button", { name: "Месяц" }).click();
  const monthScale = await page.locator(".wx-scale").innerText();
  check("Масштаб «Месяц»: шкала месяцев", /Сентябрь|Октябрь|Ноябрь/.test(monthScale) && !monthScale.includes("Нед."));
  await page.getByRole("button", { name: "День" }).click();

  // 10. modal: «Отмена» saves nothing; predecessor navigation
  p0 = await plan(page);
  let dlg = await openTask(page, 4);
  await dlg.getByLabel("Название").fill("НЕ ДОЛЖНО СОХРАНИТЬСЯ");
  await dlg.getByRole("button", { name: "Отмена" }).click();
  const p10 = await plan(page);
  check("Карточка «Отмена»: на сервере ничего не изменилось", p10.version === p0.version && task(p10, 4).name === task(p0, 4).name);
  dlg = await openTask(page, 4);
  await dlg.getByRole("button", { name: /^№1 / }).click();
  check("Карточка: переход к предшественнику №1", /№1 «/.test(await page.getByRole("dialog").innerText()));
  await page.keyboard.press("Escape");

  // 11. import + export (file content checked afterwards by python)
  await page.getByRole("button", { name: "Загрузить Excel" }).click();
  const importDate = await page.locator('input[type="date"]').inputValue();
  const p11 = await mutate(page, async () => {
    await page.locator('input[type="file"]').setInputFiles(SAMPLE);
    await page.getByRole("button", { name: "Загрузить", exact: true }).click();
  });
  await consistency(page, "Импорт примера Excel");
  check(`Импорт: 15 задач, старт = дата из диалога (${ru(importDate)})`, p11.plan.tasks.length === 15 && p11.plan.project_start === importDate, `${p11.plan.tasks.length} задач, старт ${ru(p11.plan.project_start)}`);
  const download = page.waitForEvent("download");
  await page.getByRole("link", { name: "Экспорт" }).click();
  const file = await download;
  await file.saveAs(path.join(OUT, "fulltest-export.xlsx"));
  fs.writeFileSync(path.join(OUT, "fulltest-export-plan.json"), JSON.stringify(p11));
  check("Экспорт: файл скачан", /^plan-\d{4}-\d{2}-\d{2}\.xlsx$/.test(file.suggestedFilename()), file.suggestedFilename());

  // 12. theme (and it survives a reload)
  await page.getByRole("button", { name: "Ещё" }).click();
  await page.getByRole("menuitemradio", { name: "Тёмная" }).click();
  check("Тема «Тёмная»", await page.evaluate(() => document.documentElement.classList.contains("dark")));
  await page.reload();
  await expect(page.locator(".wx-bar").first()).toBeVisible();
  check("Тема «Тёмная» сохранилась после перезагрузки", await page.evaluate(() => document.documentElement.classList.contains("dark")));
  await page.getByRole("button", { name: "Ещё" }).click();
  await page.getByRole("menuitemradio", { name: "Светлая" }).click();
  check("Тема «Светлая»", !(await page.evaluate(() => document.documentElement.classList.contains("dark"))));

  // 13. MCP token: issue, then revoke
  await page.getByRole("button", { name: "Подключить MCP" }).click();
  await page.getByRole("button", { name: "Выпустить токен" }).click();
  const token = await page.getByRole("dialog").locator("input[readonly]").inputValue();
  check("MCP: токен выпущен", /^mcp_/.test(token));
  await page.getByRole("dialog").getByRole("button", { name: "Отозвать" }).click();
  await expect(page.getByRole("button", { name: "Выпустить токен" })).toBeVisible();
  const probe = await page.request.post("/mcp/", { headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json", Accept: "application/json, text/event-stream" }, data: { jsonrpc: "2.0", id: 1, method: "tools/list" } });
  check("MCP: отозванный токен не пускает (401)", probe.status() === 401, String(probe.status()));
  await page.keyboard.press("Escape");

  // 14. reset
  await resetToDemo(page);
  const p14 = await consistency(page, "Сброс к демо");
  check("Сброс: 25 задач", p14.plan.tasks.length === 25);

  // 15. delete my data → fresh session after reload
  const oldVersion = p14.version;
  await page.getByRole("button", { name: "Ещё" }).click();
  await page.getByRole("menuitem", { name: "Удалить мои данные" }).click();
  afterDelete = true;
  await Promise.all([
    page.waitForResponse((r) => r.url().endsWith("/api/session") && r.request().method() === "DELETE"),
    page.waitForEvent("framenavigated"),
    page.getByRole("button", { name: "Удалить", exact: true }).click(),
  ]);
  await expect(page.locator(".wx-bar").first()).toBeVisible({ timeout: 30_000 });
  const p15 = await consistency(page, "После «Удалить мои данные»");
  check("Новая сессия: версия 1, 25 задач, отмена недоступна", p15.version === 1 && p15.plan.tasks.length === 25 && !p15.can_undo, `v${oldVersion}→v${p15.version}`);

  // the 401 on the MCP probe is deliberate
  const unexpected = problems.filter((s) => !(s.startsWith("[после удаления]") && s.includes("401")));
  check("Консоль и сеть без ошибок за весь прогон", unexpected.length === 0, unexpected.slice(0, 8).join("; "));

  const failed = results.filter((r) => !r.ok);
  console.log(`\nSUMMARY: ${results.length - failed.length}/${results.length} passed`);
  expect(failed.map((f) => `${f.step} | ${f.detail}`)).toEqual([]);
});

// Edge cases: HTML/script in task data, import errors, task-modal limits, two tabs of one
// session, event-stream drop, chat network failure, a 500-task plan, browser time zones, phone.
// Runs against the e2e stack (fake LLM); every check is recorded and the test fails on any miss.
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Browser, type Page } from "@playwright/test";

const FIX = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "fixtures");
const results: { step: string; ok: boolean; detail: string }[] = [];
function check(step: string, ok: boolean, detail = "") {
  results.push({ step, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"} | ${step}${detail ? " | " + detail : ""}`);
}
const ddmm = (iso: string) => `${iso.slice(8, 10)}.${iso.slice(5, 7)}`;
const plan = async (page: Page) => (await page.request.get("/api/plan")).json();

function watch(page: Page, tag: string, sink: string[]) {
  page.on("console", (m) => { if (m.type() === "error") sink.push(`${tag} console: ${m.text()}`); });
  page.on("pageerror", (e) => sink.push(`${tag} pageerror: ${e.message}`));
  page.on("response", (r) => { if (r.status() >= 400) sink.push(`${tag} ${r.status()} ${r.request().method()} ${new URL(r.url()).pathname}`); });
  page.on("dialog", (d) => { sink.push(`${tag} JS dialog: ${d.message()}`); void d.dismiss(); });
}
async function importFile(page: Page, file: string) {
  await page.getByRole("button", { name: "Загрузить Excel" }).click();
  await page.locator('input[type="file"]').setInputFiles(path.join(FIX, file));
  await page.getByRole("button", { name: "Загрузить", exact: true }).click();
}

test.setTimeout(600_000);

test("edge cases", async ({ browser }: { browser: Browser }, testInfo) => {
  const BASE = String(testInfo.project.use.baseURL);
  const ORIGIN = new URL(BASE).origin;
  const OUT = testInfo.outputDir;
  const problems: string[] = [];
  const ctx = await browser.newContext({ baseURL: BASE, viewport: { width: 1600, height: 900 } });
  const page = await ctx.newPage();
  watch(page, "A", problems);
  await page.goto("/");
  await expect(page.locator(".wx-bar").first()).toBeVisible();

  // E1. HTML/script in task names, descriptions, assignees: shown as text, never executed
  await importFile(page, "xss.xlsx");
  await expect(page.getByRole("dialog")).toBeHidden({ timeout: 10_000 });
  await page.waitForTimeout(800);
  const xss = await page.evaluate(() => (window as unknown as { __xss?: number }).__xss);
  check("XSS: скрипт из названия/описания не выполнился", xss === undefined, String(xss));
  const cellText = await page.locator('.wx-table-container [data-id="1"] .wx-col-text').first().innerText();
  check("XSS: в таблице название показано как текст", cellText === '<img src=x onerror="window.__xss=1">', cellText);
  const barText = await page.locator('.wx-bar[data-id="1"]').innerText();
  check("XSS: на полосе название показано как текст", barText.includes("<img"), barText.slice(0, 60));
  await page.locator('.wx-bar[data-id="1"]').hover();
  await page.waitForTimeout(700);
  const tip = await page.locator(".wx-tooltip, [role=tooltip]").first().innerText().catch(() => "(нет подсказки)");
  check("XSS: подсказка при наведении — текст", !tip.includes("undefined") && (tip.includes("<img") || tip === "(нет подсказки)"), tip.slice(0, 80));
  await page.locator('.wx-table-container [data-id="1"] .wx-col-text').first().dblclick();
  const dlgTitle = await page.getByRole("dialog").getByRole("heading").first().innerText();
  check("XSS: заголовок карточки — текст", dlgTitle.includes("<img src=x"), dlgTitle);
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Загрузка" }).click();
  check("XSS: исполнитель <b> в «Загрузке» — текст", await page.getByText("<b>Жирный</b>").first().isVisible());
  await page.getByRole("button", { name: "Загрузка" }).click();
  check("XSS: скрипт так и не выполнился", (await page.evaluate(() => (window as unknown as { __xss?: number }).__xss)) === undefined);

  // E2. Import errors are shown in the dialog, the plan is untouched
  const v0 = (await plan(page)).version;
  for (const [file, want] of [["cycle.xlsx", /Циклическая зависимость/], ["nocols.xlsx", /колонка «Задача»/]] as const) {
    await importFile(page, file);
    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText(want, { timeout: 10_000 });
    check(`Импорт ${file}: ошибка показана в диалоге`, true);
    await page.keyboard.press("Escape");
  }
  check("Импорт с ошибками: план не изменился", (await plan(page)).version === v0);

  // E3. Modal boundaries
  await page.getByRole("button", { name: "Сбросить к демо" }).click();
  await page.getByRole("button", { name: "Сбросить", exact: true }).click();
  await expect.poll(async () => (await plan(page)).plan.tasks.length).toBe(25);
  await page.waitForTimeout(800);
  const open4 = async () => {
    await page.locator('.wx-table-container [data-id="4"] .wx-col-text').first().dblclick();
    return page.getByRole("dialog");
  };
  const vB = (await plan(page)).version;
  for (const [label, field, value] of [
    ["длительность 0", "Длительность, дн.", "0"],
    ["длительность 1000", "Длительность, дн.", "1000"],
    ["пустое название", "Название", "   "],
    ["название 201 символ", "Название", "Я".repeat(201)],
    ["исполнитель 101 символ", "Исполнитель", "И".repeat(101)],
  ] as const) {
    const dlg = await open4();
    await dlg.getByLabel(field).fill(value);
    const save = dlg.getByRole("button", { name: "Сохранить" });
    const disabled = await save.isDisabled();
    if (!disabled) await save.click();
    await page.waitForTimeout(600);
    const stillOpen = await dlg.isVisible();
    const msg = stillOpen ? ((await dlg.innerText()).match(/[^\n]*(не может|должн|больше|меньше|от 1|до 999|символ|пуст)[^\n]*/i)?.[0] ?? "") : "";
    const unchanged = (await plan(page)).version === vB;
    check(`Карточка: ${label} — не сохраняется, есть подсказка`, unchanged && (disabled || (stillOpen && msg !== "")), `disabled=${disabled} msg=«${msg}» unchanged=${unchanged}`);
    if (await dlg.isVisible()) await dlg.getByRole("button", { name: "Отмена" }).click();
  }
  const dlg999 = await open4();
  await dlg999.getByLabel("Длительность, дн.").fill("999");
  await dlg999.getByRole("button", { name: "Сохранить" }).click();
  await expect.poll(async () => (await plan(page)).plan.tasks.find((t: { id: number }) => t.id === 4).duration).toBe(999);
  check("Карточка: длительность 999 (граница) сохраняется", true);
  await page.getByRole("button", { name: "Отменить" }).click();
  await expect.poll(async () => (await plan(page)).plan.tasks.find((t: { id: number }) => t.id === 4).duration).not.toBe(999);

  // E4. Two tabs of one session: live sync and a stale-version save
  const pageB = await ctx.newPage();
  watch(pageB, "B", problems);
  await pageB.goto("/");
  await expect(pageB.locator(".wx-bar").first()).toBeVisible();
  const before = (await plan(page)).plan.tasks.find((t: { id: number }) => t.id === 5);
  await page.locator('.wx-table-container [data-id="5"] .wx-col-text').first().dblclick();
  await page.getByRole("dialog").getByLabel("Длительность, дн.").fill(String(before.duration + 1));
  await page.getByRole("dialog").getByRole("button", { name: "Сохранить" }).click();
  await expect(pageB.locator('.wx-table-container [data-id="5"] .wx-col-workDays').first()).toHaveText(String(before.duration + 1), { timeout: 10_000 });
  check("Две вкладки: правка в A видна в B без перезагрузки", true);
  // B opens the modal, A changes the same plan, B saves with a stale version
  await pageB.locator('.wx-table-container [data-id="6"] .wx-col-text').first().dblclick();
  await pageB.getByRole("dialog").getByLabel("Описание").fill("из вкладки B");
  await page.request.post("/api/plan/operations", { data: { ops: [{ op: "update_task", id: 6, name: "Переименовано в A" }] }, headers: { Origin: ORIGIN } });
  await page.waitForTimeout(1500);
  await pageB.getByRole("dialog").getByRole("button", { name: "Сохранить" }).click();
  await page.waitForTimeout(2000);
  const t6 = (await plan(page)).plan.tasks.find((t: { id: number }) => t.id === 6);
  check("Две вкладки: правка A не затёрта сохранением из B", t6.name === "Переименовано в A", `name=«${t6.name}» desc=«${t6.description}»`);
  await pageB.screenshot({ path: path.join(OUT, "edge-conflict-B.png") });
  if (await pageB.getByRole("dialog").isVisible()) await pageB.keyboard.press("Escape");
  await pageB.close();

  // E5. Event stream drop: changes made while disconnected show up after reconnect
  await page.route("**/api/events", (r) => r.abort());
  await page.request.post("/api/plan/operations", { data: { ops: [{ op: "update_task", id: 2, duration: 9 }] }, headers: { Origin: ORIGIN } });
  await page.waitForTimeout(3000);
  await page.unroute("**/api/events");
  await expect(page.locator('.wx-table-container [data-id="2"] .wx-col-workDays').first()).toHaveText("9", { timeout: 20_000 });
  check("Обрыв потока событий: изменение подтянулось после переподключения", true);

  // E6. Chat: network failure mid-turn → error shown, input usable again
  await page.route("**/api/chat", (r) => r.abort());
  const input = page.getByRole("textbox", { name: /сообщение/i });
  await input.fill("Сдвинь все задачи Дмитрия на 3 дня");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(1500);
  const chatErr = await page.locator("text=/Нет связи|прервалась|Не удалось/i").first().innerText().catch(() => "");
  check("Чат: сбой сети — понятная ошибка по-русски", chatErr !== "" && !/Failed to fetch/.test(await page.content()), chatErr.slice(0, 100));
  check("Чат: после сбоя поле ввода снова доступно", await input.isEnabled());
  await page.unroute("**/api/chat");
  await input.fill("Сдвинь все задачи Дмитрия на 3 дня");
  await page.keyboard.press("Enter");
  await expect(page.getByText(/Изменено задач: \d+/).first()).toBeVisible({ timeout: 30_000 });
  check("Чат: после восстановления сети команда работает", true);

  // E7. Large plan: 500 tasks
  const t0 = Date.now();
  await importFile(page, "big-500.xlsx");
  await expect.poll(async () => (await plan(page)).plan.tasks.length, { timeout: 30_000 }).toBe(500);
  await expect(page.locator('.wx-table-container [data-id="1"]').first()).toBeVisible();
  const renderMs = Date.now() - t0;
  check(`500 задач: импорт и отрисовка за ${renderMs} мс`, renderMs < 10_000);
  const tScroll = Date.now();
  await page.locator(".wx-gantt").evaluate((el) => { el.scrollTop = el.scrollHeight; });
  await expect(page.locator('.wx-table-container [data-id="500"]').first()).toBeVisible({ timeout: 10_000 });
  check(`500 задач: прокрутка до последней за ${Date.now() - tScroll} мс`, true);
  const big = await plan(page);
  const r500 = await page.locator('.wx-table-container [data-id="500"] .wx-col-startLabel').first().innerText();
  check("500 задач: «Начало» последней задачи = сервер", r500 === ddmm(big.plan.tasks.find((t: { id: number }) => t.id === 500).start), r500);
  const tEdit = Date.now();
  await page.locator('.wx-table-container [data-id="500"] .wx-col-text').first().dblclick();
  await page.getByRole("dialog").getByLabel("Длительность, дн.").fill("7");
  await page.getByRole("dialog").getByRole("button", { name: "Сохранить" }).click();
  await expect(page.locator('.wx-table-container [data-id="500"] .wx-col-workDays').first()).toHaveText("7", { timeout: 15_000 });
  check(`500 задач: правка через карточку применилась за ${Date.now() - tEdit} мс`, Date.now() - tEdit < 8000);
  await page.screenshot({ path: path.join(OUT, "edge-500.png") });

  // E8. Timezones: dates in the grid match the server whatever the browser's zone
  for (const tz of ["America/Los_Angeles", "Pacific/Kiritimati", "Asia/Kolkata"]) {
    const c = await browser.newContext({ baseURL: BASE, viewport: { width: 1600, height: 900 }, timezoneId: tz });
    const p = await c.newPage();
    watch(p, tz, problems);
    await p.goto("/");
    await expect(p.locator(".wx-bar").first()).toBeVisible();
    const pl = await plan(p);
    const rows = await p.$$eval(".wx-table-container [data-id]", (els) => els.filter((e) => e.querySelector(".wx-col-startLabel")).map((e) => [Number(e.getAttribute("data-id")), e.querySelector(".wx-col-startLabel")!.textContent!.trim()] as const));
    const bad = rows.filter(([id, s]) => s !== ddmm(pl.plan.tasks.find((t: { id: number }) => t.id === id).start));
    const caption = await p.getByText(/^Старт \d/).innerText();
    const line = await p.locator(".wx-gantt-holidays > .gantt-project-start").boundingBox();
    const firstBar = await p.locator('.wx-bar[data-id="1"]').boundingBox();
    check(`Часовой пояс ${tz}: даты в таблице и подпись = сервер, линия старта у первой задачи`, bad.length === 0 && caption.includes(`${ddmm(pl.plan.project_start)}.`) && !!line && !!firstBar && Math.abs(line.x - firstBar.x) <= 2, `${bad.length} расхождений; ${caption}`);
    // drag 2 cells in this zone → exactly +2 days
    const before1 = pl.plan.tasks.find((t: { id: number }) => t.id === 1);
    const b = firstBar!;
    await p.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
    await p.mouse.down();
    await p.mouse.move(b.x + b.width / 2 + 40, b.y + b.height / 2, { steps: 6 });
    await p.mouse.move(b.x + b.width / 2 + 77, b.y + b.height / 2, { steps: 6 });
    await p.mouse.up();
    await expect.poll(async () => (await plan(p)).plan.tasks.find((t: { id: number }) => t.id === 1).start, { timeout: 10_000 }).not.toBe(before1.start);
    const after1 = (await plan(p)).plan.tasks.find((t: { id: number }) => t.id === 1).start;
    const d = new Date(`${before1.start}T12:00:00Z`); d.setUTCDate(d.getUTCDate() + 2);
    let want = d.toISOString().slice(0, 10);
    const w = new Date(`${want}T12:00:00Z`).getUTCDay(); if (w === 6) { d.setUTCDate(d.getUTCDate() + 2); want = d.toISOString().slice(0, 10); } else if (w === 0) { d.setUTCDate(d.getUTCDate() + 1); want = d.toISOString().slice(0, 10); }
    check(`Часовой пояс ${tz}: перетаскивание на 2 дня → ${ddmm(before1.start)}→${ddmm(after1)} (ждали ${ddmm(want)})`, after1 === want);
    await c.close();
  }

  // E9. Phone: tabs, modal fits, chat works
  const m = await browser.newContext({ baseURL: BASE, viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  const mp = await m.newPage();
  watch(mp, "phone", problems);
  await mp.goto("/");
  await expect(mp.locator(".wx-bar").first()).toBeVisible();
  // A tap on the row only selects it; the pencil at the row's end opens the card.
  await mp.locator('.wx-table-container [data-id="1"] .wx-col-text').first().tap();
  await expect(mp.getByRole("dialog")).toHaveCount(0);
  await mp.getByRole("button", { name: "Редактировать задачу №1", exact: true }).tap();
  const mdlg = mp.getByRole("dialog");
  await expect(mdlg).toBeVisible();
  const mbox = (await mdlg.boundingBox())!;
  check("Телефон: карточка задачи помещается по ширине", mbox.x >= 0 && mbox.x + mbox.width <= 390, JSON.stringify({ x: Math.round(mbox.x), w: Math.round(mbox.width) }));
  const saveBox = await mdlg.getByRole("button", { name: "Сохранить" }).boundingBox();
  check("Телефон: кнопка «Сохранить» достижима", !!saveBox);
  await mdlg.getByRole("button", { name: "Отмена" }).tap();
  await mp.getByRole("button", { name: "Чат" }).tap();
  await mp.getByRole("textbox", { name: /сообщение/i }).fill("Сдвинь все задачи Дмитрия на 3 дня");
  await mp.getByRole("button", { name: "Отправить" }).tap();
  await expect(mp.getByText(/Изменено задач: \d+/).first()).toBeVisible({ timeout: 30_000 });
  check("Телефон: чат работает", true);
  await mp.getByRole("button", { name: "Диаграмма" }).tap();
  await expect(mp.locator(".wx-bar").first()).toBeVisible();
  await mp.screenshot({ path: path.join(OUT, "edge-phone.png") });
  await m.close();

  // Expected noise: the deliberately aborted requests, and the 422 answers to the deliberately
  // broken import files (the browser logs every 4xx as a console error). Everything else is not.
  const unexpected = problems.filter(
    (s) => !/ERR_FAILED|net::ERR|Failed to load resource: net::/.test(s) && !/422 POST \/api\/plan\/import|status of 422/.test(s),
  );
  check("Консоль и сеть без неожиданных ошибок", unexpected.length === 0, unexpected.slice(0, 8).join(" ; "));
  await ctx.close();
  const failed = results.filter((r) => !r.ok);
  console.log(`\nSUMMARY: ${results.length - failed.length}/${results.length} passed`);
  expect(failed.map((f) => `${f.step} | ${f.detail}`)).toEqual([]);
});

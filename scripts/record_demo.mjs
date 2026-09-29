// Records a screen-capture tour of every main feature: import an Excel plan, the zoom levels, two
// edits via the chat and the agent's change list, a task's card with its history, resizing and
// moving a bar with the mouse, drawing a link, undo/redo, adding and deleting a task, «Сегодня»,
// the dark theme, and export. Works against the fake LLM (local stack) and a live model
// (DEMO_BASE_URL=https://gantt-ai-planner.duckdns.org): it waits for the agent's change
// summaries, not for a particular reply text. The session it creates is deleted at the end.
//
// Playwright's `recordVideo` doesn't draw the mouse, so the page gets a drawn pointer that follows
// the real mouse events, a ring on every press, and a caption naming the current step (see
// OVERLAY_SCRIPT). All pointer actions go through `page.mouse` with an eased glide to the target
// instead of `locator.click()`, which would jump there. Drives a chromium instance against an
// already-running full stack (see docs below), then converts the captured .webm into
// docs/demo.mp4 (H.264) and docs/demo.gif.
//
// Usage (from repo root, or via `npm run demo` inside frontend/, which sets cwd there):
//   docker compose --profile full up -d --build --wait
//   DEMO_BASE_URL=http://localhost:8000 node scripts/record_demo.mjs
//
// Requires: @playwright/test + its chromium browser installed (already a frontend devDependency,
// `npx playwright install chromium` if the browser binary itself is missing), and a working
// Docker daemon — the mp4/gif conversion step shells out to `docker run jrottenberg/ffmpeg` (see
// `runFfmpeg` below) rather than depending on an ffmpeg binary installed via npm/pip. We
// deliberately avoid the `ffmpeg-static` package here: it fetches a prebuilt ffmpeg.exe at
// `npm install` time via a postinstall script, which got flagged by antivirus software
// (Kaspersky, "PDM:Trojan.Win32.Generic" behavioural detection on install.js) on the machine this
// was built on — running ffmpeg inside an official Docker image sidesteps that entirely and needs
// no extra devDependency. The gif is squeezed by gifsicle the same way, in an alpine container.
import { execFileSync } from "node:child_process";
import { mkdtempSync, readdirSync, statSync, mkdirSync, copyFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

// This file lives in repo-root/scripts/, but it's meant to be run via the `demo` npm script in
// frontend/package.json (`node ../scripts/record_demo.mjs`), i.e. with `process.cwd()` set to
// frontend/ — that's where @playwright/test is an actual devDependency. Node's ESM resolver walks
// up from *this file's own path* for bare specifiers, which would miss frontend/node_modules
// entirely, so it's loaded here via a `require` rooted at cwd instead of a static top-level
// `import`.
const require = createRequire(path.join(process.cwd(), "package.json"));
const { chromium } = require("@playwright/test");

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..");
const SAMPLE_XLSX = path.join(ROOT, "examples", "sample-plan.xlsx");
const DOCS_DIR = path.join(ROOT, "docs");
const BASE_URL = process.env.DEMO_BASE_URL ?? "http://localhost:8000";
const FFMPEG_IMAGE = "jrottenberg/ffmpeg:6.1-ubuntu";
const GIFSICLE_IMAGE = "alpine:3.20";
// How much of each wait for the model stays in the video.
const KEEP_OF_WAIT_S = 2.5;
// Every pause, glide and keystroke is this much longer than the base timings below: the first
// demo read too fast, so the tour runs 20% slower.
const SPEED = 1.25;
const TYPE_DELAY_MS = Math.round(45 * SPEED);
// The beat before and after a press, in place of the old demo's `slowMo: 300`.
const BEAT_MS = 300;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const pause = (ms) => sleep(ms * SPEED);

// Injected before the app's own scripts. The pointer and the ring listen on `window` in the
// capture phase, so no handler of the app (SVAR's drag code, Radix's dialogs) can hide an event
// from them; `pointer-events: none` keeps them out of the way of the real hit-testing.
const OVERLAY_SCRIPT = () => {
  const install = () => {
    if (document.getElementById("demo-cursor")) return;
    const style = document.createElement("style");
    style.textContent = `
      #demo-cursor { position: fixed; left: 0; top: 0; z-index: 2147483647; pointer-events: none;
        transform: translate(-100px, -100px); }
      #demo-cursor svg { display: block; filter: drop-shadow(0 1px 2px rgba(0,0,0,.5)); }
      #demo-cursor .hold { position: absolute; left: -13px; top: -13px; width: 26px; height: 26px;
        border-radius: 50%; border: 3px solid #ef4444; background: rgba(239,68,68,.2); opacity: 0;
        transition: opacity .12s; }
      #demo-cursor.down .hold { opacity: 1; }
      .demo-ripple { position: fixed; z-index: 2147483646; pointer-events: none; width: 48px;
        height: 48px; margin: -24px 0 0 -24px; border-radius: 50%; border: 3px solid #ef4444;
        background: rgba(239,68,68,.25); animation: demo-ripple .7s ease-out forwards; }
      @keyframes demo-ripple { from { transform: scale(.25); opacity: 1 } to { transform: scale(1.5); opacity: 0 } }
      #demo-caption { position: fixed; top: 8px; left: 50%; z-index: 2147483645; pointer-events: none;
        transform: translateX(-50%); max-width: 640px; padding: 7px 18px; border-radius: 999px;
        background: rgba(17,24,39,.9); color: #fff; font: 600 17px/1.3 system-ui, sans-serif;
        white-space: nowrap; box-shadow: 0 4px 14px rgba(0,0,0,.25); transition: opacity .25s; }
      #demo-caption .n { color: #fca5a5; margin-right: 8px; }
    `;
    document.head.append(style);
    const cursor = document.createElement("div");
    cursor.id = "demo-cursor";
    cursor.innerHTML =
      '<div class="hold"></div><svg width="26" height="30" viewBox="0 0 26 30">' +
      '<path d="M2 2 L2 24 L8 18.5 L12.5 28 L16.5 26.2 L12 17 L20 17 Z" fill="#111" stroke="#fff" ' +
      'stroke-width="2" stroke-linejoin="round"/></svg>';
    const caption = document.createElement("div");
    caption.id = "demo-caption";
    caption.style.opacity = "0";
    document.body.append(cursor, caption);
    window.addEventListener(
      "mousemove",
      (e) => (cursor.style.transform = `translate(${e.clientX}px, ${e.clientY}px)`),
      true,
    );
    window.addEventListener(
      "mousedown",
      (e) => {
        cursor.classList.add("down");
        const ring = document.createElement("div");
        ring.className = "demo-ripple";
        ring.style.left = `${e.clientX}px`;
        ring.style.top = `${e.clientY}px`;
        document.body.append(ring);
        setTimeout(() => ring.remove(), 800);
      },
      true,
    );
    window.addEventListener("mouseup", () => cursor.classList.remove("down"), true);
    window.__demoCaption = (n, text) => {
      caption.innerHTML = n ? `<span class="n">${n}</span>` : "";
      caption.append(text);
      caption.style.opacity = text ? "1" : "0";
    };
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", install);
  else install();
};

// Runs ffmpeg inside a throwaway container, bind-mounting `workDir` (must be a plain ASCII path —
// the repo root itself has non-ASCII/space characters that trip up Docker Desktop's Windows path
// translation) at /work as both input and output location. `args` are plain ffmpeg args using
// filenames relative to workDir (jrottenberg/ffmpeg's ENTRYPOINT is ffmpeg itself).
function runFfmpeg(workDir, args) {
  const dockerArgs = ["run", "--rm", "-v", `${workDir}:/work`, "-w", "/work", FFMPEG_IMAGE, ...args];
  console.log("$ docker", dockerArgs.join(" "));
  execFileSync("docker", dockerArgs, { stdio: "inherit" });
}

// Monday of the week before `date`, as YYYY-MM-DD: the imported plan then spans today, so
// «Сегодня» has a column to scroll to.
function mondayOfLastWeek(date) {
  const d = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
  d.setUTCDate(d.getUTCDate() - ((d.getUTCDay() + 6) % 7) - 7);
  return d.toISOString().slice(0, 10);
}

async function main() {
  const videoDir = mkdtempSync(path.join(tmpdir(), "gantt-demo-"));
  mkdirSync(DOCS_DIR, { recursive: true });

  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    recordVideo: { dir: videoDir, size: { width: 1440, height: 900 } },
  });
  await context.addInitScript(OVERLAY_SCRIPT);
  const page = await context.newPage();
  // The video starts with the page. A live model's turn takes up to a minute or more, so the wait
  // is cut out of the video: `waitCut` keeps the first seconds of it (the agent's progress in the
  // chat) and the moment before the result, and remembers the rest for ffmpeg to drop.
  const videoStart = Date.now();
  const cuts = [];
  // When the demo plan was first on screen: the video opens there (see the ffmpeg step).
  let planShownS = 0;
  const waitCut = async (waiting) => {
    const started = Date.now();
    await waiting;
    const from = (started - videoStart) / 1000 + KEEP_OF_WAIT_S;
    const to = (Date.now() - videoStart) / 1000 - 0.5;
    if (to - from > 1) cuts.push([from, to]);
  };

  // The pointer, moved with an ease-in-out glide from wherever it last was.
  let pointer = { x: 720, y: 450 };
  const glideTo = async (x, y, slower = 1) => {
    const from = pointer;
    const dist = Math.hypot(x - from.x, y - from.y);
    pointer = { x, y };
    if (dist < 1) return;
    const duration = Math.min(900, 250 + dist * 0.6) * SPEED * slower;
    const steps = Math.max(6, Math.round(duration / 20));
    for (let i = 1; i <= steps; i++) {
      const t = i / steps;
      const e = t < 0.5 ? 4 * t ** 3 : 1 - (-2 * t + 2) ** 3 / 2;
      await page.mouse.move(from.x + (x - from.x) * e, from.y + (y - from.y) * e);
      await sleep(duration / steps);
    }
  };
  // A point of a locator's box: its center unless `at` picks one ({x, y} as box fractions).
  const pointOf = async (locator, at = { x: 0.5, y: 0.5 }) => {
    await locator.scrollIntoViewIfNeeded();
    const box = await locator.boundingBox();
    if (!box) throw new Error(`no box for ${locator}`);
    return { x: box.x + box.width * at.x, y: box.y + box.height * at.y };
  };
  const hover = async (locator, at) => {
    const p = await pointOf(locator, at);
    await glideTo(p.x, p.y);
  };
  const click = async (locator, at) => {
    await hover(locator, at);
    await pause(BEAT_MS);
    await page.mouse.down();
    await sleep(90);
    await page.mouse.up();
    await pause(BEAT_MS);
  };
  // Press at `from` (a locator point), glide by `dx` pixels, release — a bar drag or resize.
  const drag = async (locator, at, dx) => {
    await hover(locator, at);
    await pause(BEAT_MS);
    await page.mouse.down();
    await pause(200);
    await glideTo(pointer.x + dx, pointer.y, 1.6);
    await pause(200);
    await page.mouse.up();
    await pause(BEAT_MS);
  };
  const type = async (locator, text) => {
    await click(locator);
    await locator.pressSequentially(text, { delay: TYPE_DELAY_MS });
    await pause(300);
  };
  let stepNo = 0;
  const STEPS = 14;
  const caption = async (text) => {
    stepNo += 1;
    await page.evaluate(([n, t]) => window.__demoCaption?.(n, t), [`${stepNo}/${STEPS}`, text]);
    await pause(900);
  };
  // Resolves once the backend has applied a UI edit (bar drag, link, undo, a form's save…).
  const planSaved = () =>
    page.waitForResponse((r) => /\/api\/plan(\/|$)/.test(new URL(r.url()).pathname) && r.request().method() !== "GET");
  const planNow = () => page.evaluate(async () => (await (await fetch("/api/plan")).json()).plan);
  const bar = (id) => page.locator(`.wx-bar[data-id="${id}"]`);
  // One day column of the chart, in pixels (day zoom): a bar spans its calendar days.
  const dayWidth = async (id) => {
    const t = (await planNow()).tasks.find((x) => x.id === id);
    const days = Math.round((Date.parse(t.end) - Date.parse(t.start)) / 86_400_000) + 1;
    return (await bar(id).boundingBox()).width / days;
  };
  const chart = page.locator(".wx-chart");
  const dialog = page.getByRole("dialog");

  try {
    // 1. Open the app — demo plan visible, critical path highlighted in the legend.
    await page.goto(BASE_URL);
    await page.getByText("Сбор требований и приоритизация").last().waitFor({ state: "visible" });
    await page.getByText("Критический путь").waitFor({ state: "visible" });
    planShownS = (Date.now() - videoStart) / 1000;
    await page.mouse.move(pointer.x, pointer.y);
    await caption("Диаграмма Ганта и чат с ИИ-агентом");
    await pause(1200);

    // 2. Import the sample Excel plan, starting it the Monday before last so it spans today.
    await caption("Загрузка плана из Excel");
    await click(page.getByRole("button", { name: "Загрузить Excel" }));
    await dialog.waitFor({ state: "visible" });
    const chooser = page.waitForEvent("filechooser");
    await click(page.locator('input[type="file"]'), { x: 0.15, y: 0.5 });
    await (await chooser).setFiles(SAMPLE_XLSX);
    await pause(500);
    const startDate = page.locator('input[type="date"]');
    await click(startDate, { x: 0.3, y: 0.5 });
    await startDate.fill(mondayOfLastWeek(new Date()));
    await pause(700);
    await click(page.getByRole("button", { name: "Загрузить", exact: true }));
    await page.getByText("Упаковка мебели").last().waitFor({ state: "visible" });
    await expectGone(page, "Сбор требований и приоритизация");
    await pause(1800);

    // 3. Zoom levels.
    await caption("Масштаб: месяц, неделя, день");
    for (const zoom of ["Месяц", "Неделя", "День"]) {
      await click(page.getByRole("button", { name: zoom, exact: true }));
      await pause(1100);
    }

    // 4. Chat edit #1: bulk move by assignee; chat edit #2: reassign a single task by number.
    await caption("Правка плана через чат с агентом");
    const chatBox = page.getByRole("textbox", { name: /сообщение/i });
    await type(chatBox, "Сдвинь все задачи Олега на 3 дня");
    await page.keyboard.press("Enter");
    const diffButton = page.getByText(/Изменено задач: \d+/).first();
    await waitCut(diffButton.waitFor({ state: "visible", timeout: 180_000 }));
    await pause(800);
    await click(diffButton); // expand the DiffSummary list
    await pause(2200);
    await type(chatBox, "Назначь задачу 5 на Наталью Белову");
    await page.keyboard.press("Enter");
    // Done when the second turn's own change summary shows up (the reply wording varies).
    await waitCut(
      page.waitForFunction(() => (document.body.innerText.match(/Изменено задач: \d+/g) ?? []).length >= 2, null, {
        timeout: 180_000,
      }),
    );
    await pause(2000);

    // 5. Open a task's card with a click on its bar (as the brief asks): the agent's edits are in
    // its history. Then close it.
    await caption("Карточка задачи: поля и история правок агента");
    await click(bar(5));
    await dialog.waitFor({ state: "visible" });
    await pause(2500);
    await hover(dialog.getByRole("heading", { name: "История" }));
    await pause(1500);
    await click(dialog.getByRole("button", { name: "Отмена" }));
    await dialog.waitFor({ state: "hidden" });
    await pause(500);

    // 6. Resize a bar by its right edge: two days longer, then one day shorter.
    await caption("Срок мышкой: тянем правый край — длиннее и короче");
    const cell = await dayWidth(2);
    let saved = planSaved();
    await drag(bar(2), { x: 0.97, y: 0.5 }, cell * 2);
    await saved;
    await pause(1500);
    saved = planSaved();
    await drag(bar(2), { x: 0.97, y: 0.5 }, -cell);
    await saved;
    await pause(1500);

    // 7. Move a whole bar: its successor moves along.
    await caption("Перетаскиваем задачу целиком — зависимые сдвигаются");
    saved = planSaved();
    await drag(bar(6), { x: 0.5, y: 0.5 }, cell * 2);
    await saved;
    await pause(1800);

    // 8. Draw a link: the circle at the end of one bar, then the circle at the start of another.
    await caption("Связь мышкой: от конца одной задачи к началу другой");
    await hover(bar(7));
    await pause(400);
    await click(bar(7).locator(".wx-link.wx-right"));
    await hover(bar(4));
    await pause(300);
    saved = planSaved();
    await click(bar(4).locator(".wx-link.wx-left"));
    await saved;
    await pause(1800);

    // 9. Undo takes the link away, redo brings it back.
    await caption("Отменить и повторить");
    saved = planSaved();
    await click(page.getByRole("button", { name: "Отменить" }));
    await saved;
    await pause(1500);
    saved = planSaved();
    await click(page.getByRole("button", { name: "Повторить" }));
    await saved;
    await pause(1500);

    // 10. Add a task between two linked ones from the toolbar.
    await caption("Новая задача — встаёт между двумя связанными");
    const lastId = Math.max(...(await planNow()).tasks.map((t) => t.id));
    await click(page.getByRole("button", { name: "Добавить задачу" }));
    await dialog.waitFor({ state: "visible" });
    await type(dialog.getByLabel("Название"), "Инструктаж грузчиков");
    await type(dialog.getByLabel("Исполнитель"), "Олег Сидоров");
    await click(dialog.getByLabel("Начать после задачи"));
    await dialog.getByLabel("Начать после задачи").selectOption("5");
    await pause(500);
    await click(dialog.getByLabel("Перед задачей"));
    await dialog.getByLabel("Перед задачей").selectOption("9");
    await pause(1200);
    saved = planSaved();
    await click(dialog.getByRole("button", { name: "Добавить", exact: true }));
    await saved;
    await dialog.waitFor({ state: "hidden" });
    await pause(1800);

    // 11. Delete it again from its card.
    await caption("Удаление задачи");
    await click(page.getByRole("button", { name: `Редактировать задачу №${lastId + 1}`, exact: true }));
    await dialog.waitFor({ state: "visible" });
    await pause(800);
    await click(dialog.getByRole("button", { name: "Удалить задачу" }));
    await pause(700);
    saved = planSaved();
    await click(dialog.getByRole("button", { name: "Удалить", exact: true }));
    await saved;
    await dialog.waitFor({ state: "hidden" });
    await pause(1500);

    // 12. Scroll the chart away from today, then «Сегодня» brings it back to the middle.
    await caption("Кнопка «Сегодня» — возвращает к текущей дате");
    await hover(chart, { x: 0.5, y: 0.6 });
    for (let i = 0; i < 6; i++) {
      await page.mouse.wheel(160, 0);
      await sleep(90);
    }
    await pause(1000);
    await click(page.getByRole("button", { name: "Сегодня", exact: true }));
    await pause(2000);

    // 13. Dark theme and back.
    await caption("Тёмная тема");
    await click(page.getByRole("button", { name: "Ещё" }));
    await click(page.getByRole("menuitemradio", { name: "Тёмная" }));
    await pause(2200);
    await click(page.getByRole("button", { name: "Ещё" }));
    await click(page.getByRole("menuitemradio", { name: "Светлая" }));
    await pause(1200);

    // 14. Export the plan.
    await caption("Экспорт плана в Excel");
    const downloadPromise = page.waitForEvent("download");
    await click(page.getByRole("link", { name: "Экспорт" }));
    await downloadPromise;
    await pause(2500);
  } catch (err) {
    // A live model may answer differently (a question, an error): keep what the screen showed.
    const shot = path.join(videoDir, "failure.png");
    await page.screenshot({ path: shot }).catch(() => {});
    console.error("demo failed; screenshot:", shot);
    throw err;
  } finally {
    await page.close();
    // Leave nothing behind on the server (the context still holds the session cookie).
    await context.request.delete(`${BASE_URL}/api/session`, { headers: { Origin: new URL(BASE_URL).origin } }).catch(() => {});
    await context.close();
    await browser.close();
  }

  const webmName = readdirSync(videoDir)
    .filter((f) => f.endsWith(".webm"))
    .sort((a, b) => statSync(path.join(videoDir, b)).mtimeMs - statSync(path.join(videoDir, a)).mtimeMs)[0];
  if (!webmName) throw new Error(`no .webm recorded in ${videoDir}`);
  console.log("recorded", path.join(videoDir, webmName), statSync(path.join(videoDir, webmName)).size, "bytes");

  // All ffmpeg I/O happens inside videoDir (an ASCII-only temp path, see runFfmpeg's docstring),
  // using filenames relative to it; the finished mp4/gif are copied into docs/ afterwards with
  // plain Node fs calls, which don't share Docker's Windows-path-translation limitations.
  const mp4Name = "demo.mp4";
  const gifName = "demo.gif";
  const rawGifName = "demo-raw.gif";
  const paletteName = "palette.png";

  // webm -> mp4 (H.264, yuv420p — universally playable, incl. GitHub's inline preview).
  // Playwright's recordVideo starts capturing at context/page creation, before the app has
  // painted anything — the raw webm opens on a blank frame, then "Загрузка плана…" while the
  // session is created and the initial GET /api/plan round-trip is in flight, and only then the
  // demo plan itself: ~1.5 s against the local stack, ~5 s against production. `-ss` placed
  // *after* `-i` here is output-side (decode-accurate, not keyframe-snapped) seeking, trimming up
  // to the moment the plan was seen (plus a little: the video starts a moment before
  // `videoStart`), so the mp4/gif both open directly on the rendered demo plan.
  // The model's waits (see waitCut) are dropped here: frames inside them are skipped and the rest
  // re-timed at a constant 25 fps (Playwright's webm has a variable frame rate).
  const drop = cuts.map(([a, b]) => `between(t,${a.toFixed(2)},${b.toFixed(2)})`).join("+");
  console.log("cut from the video (s):", cuts.map(([a, b]) => `${a.toFixed(1)}–${b.toFixed(1)}`).join(", ") || "nothing");
  runFfmpeg(videoDir, [
    "-y",
    "-i",
    webmName,
    "-vf",
    drop ? `fps=25,select='not(${drop})',setpts=N/25/TB` : "fps=25",
    "-ss",
    (planShownS + 0.3).toFixed(2),
    "-c:v",
    "libx264",
    "-pix_fmt",
    "yuv420p",
    "-movflags",
    "+faststart",
    mp4Name,
  ]);

  // mp4 -> gif via a generated palette. The tour is close to three minutes, so the README's gif
  // is smaller than the video (fps=6, width 900, 128 colors, only the changed rectangle of each
  // frame stored), then squeezed by gifsicle: ~9 MB instead of ~36 MB at the old fps=10/1100px.
  runFfmpeg(videoDir, [
    "-y",
    "-i",
    mp4Name,
    "-vf",
    "fps=6,scale=900:-1:flags=lanczos,palettegen=max_colors=128:stats_mode=diff",
    "-update",
    "1",
    paletteName,
  ]);
  runFfmpeg(videoDir, [
    "-y",
    "-i",
    mp4Name,
    "-i",
    paletteName,
    "-lavfi",
    "fps=6,scale=900:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=none:diff_mode=rectangle",
    rawGifName,
  ]);
  // gifsicle isn't in the ffmpeg image; a throwaway alpine container installs it for the one call.
  const gifsicle = `apk add -q gifsicle && gifsicle -O3 --lossy=30 ${rawGifName} -o ${gifName}`;
  const gifArgs = ["run", "--rm", "-v", `${videoDir}:/work`, "-w", "/work", GIFSICLE_IMAGE, "sh", "-c", gifsicle];
  console.log("$ docker", gifArgs.join(" "));
  execFileSync("docker", gifArgs, { stdio: "inherit" });

  const mp4 = path.join(DOCS_DIR, "demo.mp4");
  const gif = path.join(DOCS_DIR, "demo.gif");
  copyFileSync(path.join(videoDir, mp4Name), mp4);
  copyFileSync(path.join(videoDir, gifName), gif);

  console.log("wrote", mp4, statSync(mp4).size, "bytes");
  console.log("wrote", gif, statSync(gif).size, "bytes");
}

async function expectGone(page, text) {
  const count = await page.getByText(text).count();
  if (count !== 0) throw new Error(`expected "${text}" to be gone, found ${count}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});

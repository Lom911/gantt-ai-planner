// Records a short screen-capture demo of the main scenario: import an Excel plan, edit it via
// the chat, open a task's card, and export. Works against the fake LLM (local stack) and a live
// model (DEMO_BASE_URL=https://gantt-ai-planner.duckdns.org): it waits for the agent's change
// summaries, not for a particular reply text. The session it creates is deleted at the end. Drives a chromium instance with Playwright's
// own `recordVideo` against an already-running full stack (see docs below), then converts the
// captured .webm into docs/demo.mp4 (H.264) and docs/demo.gif.
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
// no extra devDependency.
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
// How much of each wait for the model stays in the video.
const KEEP_OF_WAIT_S = 2.5;

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Runs ffmpeg inside a throwaway container, bind-mounting `workDir` (must be a plain ASCII path —
// the repo root itself has non-ASCII/space characters that trip up Docker Desktop's Windows path
// translation) at /work as both input and output location. `args` are plain ffmpeg args using
// filenames relative to workDir (jrottenberg/ffmpeg's ENTRYPOINT is ffmpeg itself).
function runFfmpeg(workDir, args) {
  const dockerArgs = ["run", "--rm", "-v", `${workDir}:/work`, "-w", "/work", FFMPEG_IMAGE, ...args];
  console.log("$ docker", dockerArgs.join(" "));
  execFileSync("docker", dockerArgs, { stdio: "inherit" });
}

async function main() {
  const videoDir = mkdtempSync(path.join(tmpdir(), "gantt-demo-"));
  mkdirSync(DOCS_DIR, { recursive: true });

  const browser = await chromium.launch({ slowMo: 300 });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    recordVideo: { dir: videoDir, size: { width: 1440, height: 900 } },
  });
  const page = await context.newPage();
  // The video starts with the page. A live model's turn takes up to a minute or more, so the wait
  // is cut out of the video: `waitCut` keeps the first seconds of it (the agent's progress in the
  // chat) and the moment before the result, and remembers the rest for ffmpeg to drop.
  const videoStart = Date.now();
  const cuts = [];
  const waitCut = async (waiting) => {
    const started = Date.now();
    await waiting;
    const from = (started - videoStart) / 1000 + KEEP_OF_WAIT_S;
    const to = (Date.now() - videoStart) / 1000 - 0.5;
    if (to - from > 1) cuts.push([from, to]);
  };

  try {
    // 1. Open the app — demo plan visible, critical path highlighted in the legend.
    await page.goto(BASE_URL);
    await page.getByText("Сбор требований и приоритизация").last().waitFor({ state: "visible" });
    await page.getByText("Критический путь").waitFor({ state: "visible" });
    await pause(2000);

    // 2. Import the sample Excel plan.
    await page.getByRole("button", { name: "Загрузить Excel" }).click();
    await pause(600);
    await page.locator('input[type="file"]').setInputFiles(SAMPLE_XLSX);
    await pause(600);
    await page.getByRole("button", { name: "Загрузить", exact: true }).click();
    await page.getByText("Упаковка мебели").last().waitFor({ state: "visible" });
    await expectGone(page, "Сбор требований и приоритизация");
    await pause(1800);

    // 3. Chat edit #1: bulk move by assignee.
    const chatBox = page.getByRole("textbox", { name: /сообщение/i });
    await chatBox.click();
    await chatBox.pressSequentially("Сдвинь все задачи Олега на 3 дня", { delay: 45 });
    await pause(400);
    await page.keyboard.press("Enter");
    const diffButton = page.getByText(/Изменено задач: \d+/).first();
    await waitCut(diffButton.waitFor({ state: "visible", timeout: 180_000 }));
    await pause(800);
    await diffButton.click(); // expand the DiffSummary list
    await pause(2000);

    // 4. Chat edit #2: reassign a single task by number.
    await chatBox.click();
    await chatBox.pressSequentially("Назначь задачу 5 на Наталью Белову", { delay: 45 });
    await pause(400);
    await page.keyboard.press("Enter");
    // Done when the second turn's own change summary shows up (the reply wording varies).
    await waitCut(
      page.waitForFunction(() => (document.body.innerText.match(/Изменено задач: \d+/g) ?? []).length >= 2, null, {
        timeout: 180_000,
      }),
    );
    await pause(1800);

    // 5. Open a task's card with a click on its bar (as the brief asks), then close it.
    // The task name renders at least twice (the grid's "Задача" cell, then the bar's own label)
    // plus once per changed field inside the now-expanded DiffSummary above (each change line is
    // its own clickable button, e.g. "№5 «Заказ грузового транспорта»: начало ..."). Those extra
    // matches come *after* the Gantt's own two in DOM order (chat pane follows the Gantt pane in
    // the layout), so `.nth(1)` (the bar label) is the one that reliably opens the task modal —
    // `.last()` would instead hit a DiffSummary change-line button, which calls onFocusTask, not
    // onOpenTask.
    const bar = page.getByText("Заказ грузового транспорта").nth(1);
    await bar.scrollIntoViewIfNeeded();
    await pause(400);
    await bar.click();
    const dialog = page.getByRole("dialog");
    await dialog.waitFor({ state: "visible" });
    await pause(2500);
    await page.getByRole("button", { name: "Отмена" }).click();
    await dialog.waitFor({ state: "hidden" });
    await pause(500);

    // 6. Export the plan.
    const downloadPromise = page.waitForEvent("download");
    await page.getByRole("link", { name: "Экспорт" }).click();
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
  const paletteName = "palette.png";

  // webm -> mp4 (H.264, yuv420p — universally playable, incl. GitHub's inline preview).
  // Playwright's recordVideo starts capturing at context/page creation, before the app has
  // painted anything — the raw webm opens on a blank frame, then "Загрузка плана…" while the
  // initial GET /api/plan round-trip is in flight, and only then the demo plan itself (measured
  // empirically: blank until ~0.5s, loading text until ~1.3-1.5s). `-ss` placed *after* `-i` here
  // is output-side (decode-accurate, not keyframe-snapped) seeking, trimming the first 1.6s so
  // the mp4/gif both open directly on the rendered demo plan.
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
    "1.6",
    "-c:v",
    "libx264",
    "-pix_fmt",
    "yuv420p",
    "-movflags",
    "+faststart",
    mp4Name,
  ]);

  // mp4 -> gif via a generated palette (fps=10, width ~1100) to keep size down.
  runFfmpeg(videoDir, [
    "-y",
    "-i",
    mp4Name,
    "-vf",
    "fps=10,scale=1100:-1:flags=lanczos,palettegen=stats_mode=diff",
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
    "fps=10,scale=1100:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer",
    gifName,
  ]);

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

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { Gantt, Tooltip, Willow, WillowDark, type IApi } from "@svar-ui/react-gantt";
import "@svar-ui/react-gantt/all.css";
import "./wx-icons/wx-icons.css";
import "./gantt.css";
import { Pencil } from "lucide-react";
import { toast } from "sonner";
import {
  ZOOM_PRESETS,
  boxesOverlap,
  centerDayScroll,
  closestTaskId,
  dayAtOffset,
  durationLabel,
  firstInPlanOrder,
  highlightDay,
  isDragEnd,
  revealScroll,
  toSvarLinks,
  toSvarTasks,
  type ScaleCell,
  type Zoom,
} from "./mapping";
import { interpretBarChange, linkDeletionToOperation, linkToOperation } from "./interactions";
import { RuLocale } from "./locale";
import type { Operation, ScheduledPlan } from "@/api/types";
import { formatRu, addDays, parseISODate, toISODate } from "@/lib/dates";
import { loadLayoutPrefs, saveLayoutPrefs, savedGridWidth } from "@/lib/layoutPrefs";
import { ruPlural } from "@/lib/resourceSummary";
import type { RevealMode } from "@/hooks/useSessionEvents";

const TASK_TYPES = [
  { id: "task", label: "Задача" },
  { id: "critical", label: "Критическая" },
  { id: "changed", label: "Изменена" },
  { id: "conflict", label: "Перегрузка" },
];

// Below this width the grid keeps only № + Задача (see `columns` below) — there isn't room for
// Исполнитель/Дн. too without squeezing the timeline down to nothing (spec review round 1).
const NARROW_BREAKPOINT = 480;

// SVAR enters "compact mode" whenever the chart is 650px wide or less, and compact mode never
// shows grid and timeline side by side: displayMode "all" becomes "grid", and the timeline sits
// behind a toggle icon (documented: docs.svar.dev/react/gantt/guides/appearance/compact-mode).
// On a phone that hides the timeline, the point of the app. Our narrow grid (№ + Задача, 180px)
// leaves the rest of the width to the horizontally scrollable timeline, so keep SVAR out of
// compact mode. There is no prop for it: the flag only reaches the store through
// DataStore.init, which the Gantt calls with its full config on every prop change, so that call
// is wrapped to always pass `_compactMode: false`. Covered by the 390px e2e check.
function disableCompactMode(api: IApi) {
  const store = api.getStores().data;
  const init = store.init.bind(store);
  store.init = (state) => init({ ...state, _compactMode: false } as typeof state);
}

// SVAR's own tooltip (`Tooltip`/`content`) resolves `data-task-id` off the hovered element and hands
// over a snapshot of its own task, taken when the tooltip appears and kept until the pointer moves.
// After a drag or resize that snapshot still held the dates from before it (and the working days
// even after SVAR's optimistic update, which only moves start/end), so the tooltip reads the saved
// task by id from the plan instead: an open tooltip re-renders as soon as the server answers.
const TooltipPlan = createContext<ScheduledPlan | null>(null);

function BarTooltip({ data }: { api: IApi; data: Record<string, unknown> }) {
  const id = (data.task as { id?: number | string } | undefined)?.id;
  const task = useContext(TooltipPlan)?.tasks.find((t) => t.id === Number(id));
  if (!task) return null;
  return (
    <div className="max-w-64 rounded-md border border-border bg-popover px-2.5 py-2 text-xs text-popover-foreground shadow-md">
      <div className="font-medium">{task.name}</div>
      <div className="text-muted-foreground">
        {formatRu(task.start)}–{formatRu(task.end)} · {durationLabel(task.start, task.end, task.duration)}
      </div>
      {task.assignee && <div className="text-muted-foreground">{task.assignee}</div>}
      <div className="text-muted-foreground">Резерв {task.slack} дн.</div>
    </div>
  );
}

// While a bar is pressed (dragged or resized) its tooltip would keep showing the dates from before
// the drag, stuck where the press started, so it's hidden (gantt.css) until the pointer moves again
// after the release — by then SVAR's tooltip has re-resolved it next to the pointer. The class goes
// on <body>: SVAR portals the tooltip out of the chart's container.
const BAR_PRESSED = "gantt-bar-pressed";
function hideTooltipWhilePressed() {
  const host = document.body;
  host.classList.add(BAR_PRESSED);
  const pressed = new AbortController();
  const show = () => host.classList.remove(BAR_PRESSED);
  const release = (e: PointerEvent) => {
    pressed.abort();
    // A touch or pen release may be followed by no pointermove at all (and SVAR shows no tooltip
    // for touch anyway), so only a mouse waits for one.
    if (e.pointerType === "mouse") window.addEventListener("pointermove", show, { once: true });
    else show();
  };
  window.addEventListener("pointerup", release, { signal: pressed.signal });
  window.addEventListener("pointercancel", release, { signal: pressed.signal });
}

// The grid's last column: a visible hint that a row opens its card (any click on a row or bar does,
// see the container's onClick). The button sits inside the row's `[data-id]` element, so the
// container resolves the task with closestTaskId like any other click.
// (`row` is SVAR's grid IRow, `{[key: string]: any}` — our SvarTask at runtime.)
function EditCell({ row }: { row: { [key: string]: unknown } }) {
  return (
    <button
      type="button"
      className="gantt-edit-task inline-flex h-6 w-6 items-center justify-center rounded text-muted-foreground hover:bg-accent hover:text-foreground"
      title="Редактировать"
      aria-label={`Редактировать задачу №${row.id}`}
    >
      <Pencil className="h-3.5 w-3.5" aria-hidden="true" />
    </button>
  );
}
const editColumn = (width: number) => ({ id: "edit", header: "", width, align: "center" as const, resize: false, cell: EditCell });
const EDIT_WIDTH = 36;
// Phone: № + Задача + pencil (+1px grid border) stay within 185px (checked in production.spec.ts) so the timeline
// keeps more than half of a 390px screen.
const NARROW_COLUMNS = [
  { id: "id", header: "№", width: 36, align: "center" as const },
  { id: "text", header: "Задача", width: 116, flexgrow: 1 },
  editColumn(32),
];

// The bottom row of the scale holds the day numbers (in day zoom): the cells a click or a key
// picks a column with (see toggleDayAt and the keyboard handling below).
const DAY_CELL = ".wx-scale > .wx-row:last-child > .wx-cell";

export function GanttView(props: {
  plan: ScheduledPlan;
  zoom: Zoom;
  highlighted: ReadonlySet<number>;
  readOnly: boolean;
  dark?: boolean;
  onOpenTask(id: number): void;
  onApply(ops: Operation[]): Promise<void>;
  // Tasks that just changed, to bring into view if none of them is on screen: `scroll` moves the
  // chart to the topmost one, `notice` offers to (see revealMode). `key` makes each request new.
  reveal?: { ids: number[]; mode: RevealMode; key: number } | null;
  onRevealed?(): void;
  // Highlight tasks the chart has just scrolled to (the app's own flash, held a little longer).
  onFlash?(ids: number[]): void;
  // The toolbar's «Сегодня»: a new value scrolls today to the middle of the chart.
  centerToday?: number;
}) {
  // Handlers passed into `init` are captured once (the Gantt is only initialized once);
  // routing through a ref keeps them current without re-running `init`.
  const handlers = useRef(props);
  useEffect(() => {
    handlers.current = props;
  }, [props]);

  // Measures the actual rendered width of this component (not the window: on desktop it only
  // gets ~70% of it via SplitLayout's split, and the divider is user-draggable) so the grid can
  // drop columns when there truly isn't room, on a phone or a squeezed-down desktop pane alike.
  const containerRef = useRef<HTMLDivElement>(null);
  const [narrow, setNarrow] = useState(false);
  useEffect(() => {
    const el = containerRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(([entry]) => setNarrow((entry?.contentRect.width ?? el.clientWidth) < NARROW_BREAKPOINT));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Set once `init` hands it to us; wraps the chart in SVAR's own hover tooltip (below).
  const [api, setApi] = useState<IApi | null>(null);

  // SVAR applies a drag/resize/link optimistically to its own internal store the instant it
  // happens (before `onApply` below even starts its request) so the bar/arrow already looks
  // moved. When the backend rejects the change, `props.plan` is deliberately left untouched (no
  // refetch), so `tasks`/`links` would keep returning the very same array *references* on
  // re-render and SVAR would never re-sync from them. Bumping `revision` forces new references
  // out of the unchanged plan, which is what makes the Gantt re-init from last-known-good data
  // and the bar/link actually snap back.
  const [revision, setRevision] = useState(0);
  const snapBack = useCallback(() => setRevision((r) => r + 1), []);

  const tasks = useMemo(() => {
    void revision; // not read, but forces a fresh array after a rejected edit (see above)
    return toSvarTasks(props.plan, props.highlighted);
  }, [props.plan, props.highlighted, revision]);
  const links = useMemo(() => {
    void revision;
    return toSvarLinks(props.plan);
  }, [props.plan, revision]);

  // SVAR's own `gridWidth` default (`getDefaultGridWidth`, @svar-ui/gantt-store) sums the
  // *default* column set's widths, not ours — since we never pass `gridWidth` explicitly, a
  // flexgrow-only "text" column was left with only a few leftover pixels (observed ~42px,
  // truncating every task name to nothing useful). Giving it an explicit base `width` (still
  // with `flexgrow` so it grows into any extra space) and passing `gridWidth` computed from our
  // own columns fixes both the baseline and the total.
  //
  // Full Начало/Окончание date columns used to be here too, but that pushed the grid to ~620px,
  // leaving barely a third of a 1440px screen for the actual timeline (spec review round 1).
  // A compact «Начало» (дд.мм) is back: without any date the grid couldn't answer "when does
  // this start / where is the plan counted from" — full dates stay on the bar's tooltip and in
  // the task modal. On a narrow pane, only №+Задача fit; the rest would squeeze the timeline.
  // Column widths and the grid/timeline divider the user dragged last time (desktop layout only;
  // the narrow phone layout always uses its fixed widths). Read once per mount.
  const [prefs] = useState(loadLayoutPrefs);
  const columns = useMemo(() => {
    const w = (id: string, fallback: number) => prefs.columns?.[id] ?? fallback;
    return narrow
      ? NARROW_COLUMNS
      : [
          { id: "id", header: "№", width: w("id", 44), align: "center" as const },
          { id: "text", header: "Задача", width: w("text", 180), flexgrow: 1 },
          { id: "assignee", header: "Исполнитель", width: w("assignee", 130) },
          { id: "startLabel", header: "Начало", width: w("startLabel", 76), align: "center" as const },
          { id: "workDays", header: "Дн.", width: w("workDays", 48), align: "center" as const },
          editColumn(EDIT_WIDTH),
        ];
  }, [narrow, prefs]);
  const gridWidth = useMemo(
    () => (!narrow && savedGridWidth(prefs, EDIT_WIDTH)) || columns.reduce((sum, c) => sum + c.width, 0),
    [columns, narrow, prefs],
  );
  const narrowRef = useRef(narrow);
  useEffect(() => {
    narrowRef.current = narrow;
  }, [narrow]);

  const init = useCallback((api: IApi) => {
    setApi(api);
    disableCompactMode(api);

    // First load opens at the project's start (one day of margin before it). Scrolling to today
    // instead pushed an ongoing plan's first weeks off-screen: the top rows looked empty and bars
    // started cut off at the left edge. The demo plan starts two weeks before today (spec), so on
    // a desktop-width chart the "today" line is on this first screen too.
    const { plan } = handlers.current;
    api.exec("scroll-chart", { date: addDays(parseISODate(plan.project_start), -1) });

    // Remember what the user resizes by hand (see layoutPrefs); the phone layout isn't saved.
    api.on("resize-grid", ({ width }: { width: number }) => {
      if (!narrowRef.current && width > 0) saveLayoutPrefs({ gridWidth: Math.round(width), gridIncludesEdit: true });
    });
    api.on("set-columns", ({ columns: cols }: { columns: { id?: string; width?: number }[] }) => {
      if (narrowRef.current) return;
      const widths = Object.fromEntries(
        cols.filter((c) => c.id && typeof c.width === "number").map((c) => [c.id!, Math.round(c.width!)]),
      );
      saveLayoutPrefs({ columns: widths });
    });

    // A click on a row or bar opens the task's card, as the brief asks («по клику на задачу
    // открывается модалка»), and SVAR's own `select-task` tints the row at the same time; both are
    // handled by the container below, not by SVAR's events (`select-task` also fires on keyboard
    // navigation). SVAR's double-click `show-editor` is blocked: it would open SVAR's built-in
    // editor, and it doesn't fire at all while the chart is read-only (agent busy), when the card
    // should still open to view the task.
    api.intercept("show-editor", () => false);

    // A bar drag or resize is committed as an `update-task` event carrying either `diff` (the
    // number of cells the bar moved/grew by, set by a move/resize commit) or `inProgress` (set
    // while a progress-marker drag is live) — see @svar-ui/gantt-store's DataStore.d.ts
    // (`IDataMethodsConfig["update-task"]`) and the bar-drag handlers in
    // @svar-ui/react-gantt's compiled source. Anything else (a plain field edit from our own
    // task modal, an agent-applied change, etc.) has neither and is left alone here — SVAR
    // already applied it to its own store by the time this fires. By the same point, `ev.task`
    // is the *resolved* task (real `start`/`end` Date objects, not raw pixel deltas), so we don't
    // need to reimplement SVAR's own cell-to-date math.
    //
    // SVAR swallows the click that ends a drag or resize, so the task doesn't get selected the way a
    // click selects it: the previously selected row stayed tinted while another bar changed. The
    // dragged task is selected here instead — on `update-task` with `diff` (a move/resize that
    // changed the dates) and on `drag-task` with `inProgress: false` (one that snapped back to the
    // same cells, which sends no `update-task`).
    api.on("drag-task", (ev: { id: number | string; inProgress?: boolean }) => {
      if (ev.inProgress === false) api.exec("select-task", { id: ev.id });
    });
    api.on(
      "update-task",
      (ev: { id: number | string; task: { start?: Date; end?: Date }; diff?: number; inProgress?: boolean }) => {
        if (handlers.current.readOnly) return;
        if (ev.diff != null) api.exec("select-task", { id: ev.id });
        if (ev.diff == null && ev.inProgress == null) return;
        const { start, end } = ev.task;
        if (!start || !end) return;
        const task = handlers.current.plan.tasks.find((t) => t.id === Number(ev.id));
        if (!task) return;
        const ops = interpretBarChange(task, start, end);
        if (!ops.length) return;
        void handlers.current.onApply(ops).catch(snapBack);
      },
    );

    // We intercept (never let SVAR create the link client-side) rather than `on`: the backend
    // is the only source of truth for dependencies, and only finish-to-start links are valid in
    // this domain (backend/app/domain/operations.py's AddDependencyOp has no `type`).
    api.intercept(
      "add-link",
      ({ link }: { link: { source?: number | string; target?: number | string; type?: string } }) => {
        if (handlers.current.readOnly) return false;
        if (link.type && link.type !== "e2s") {
          toast.error("Поддерживается только связь «окончание–начало»");
          return false;
        }
        if (link.source == null || link.target == null) return false;
        void handlers.current
          .onApply([linkToOperation(Number(link.source), Number(link.target))])
          .catch(snapBack);
        return false;
      },
    );

    // Same for deleting a link (select it, then the ✕ on the bar): left to SVAR, the arrow
    // vanished only in the browser while the dependency stayed on the server — still driving
    // the dates, and back on the next refresh.
    api.intercept("delete-link", ({ id }: { id: number | string }) => {
      if (handlers.current.readOnly) return false;
      const op = linkDeletionToOperation(handlers.current.plan.dependencies, id);
      if (op) void handlers.current.onApply([op]).catch(snapBack);
      return false;
    });
  }, [snapBack]);

  // SVAR ships two skin wrappers (Willow / WillowDark) rather than reacting to CSS custom
  // properties, so the dark toggle picks the whole component rather than restyling it.
  // `fonts={false}` below: by default they inject SVAR's CDN icon/font stylesheet, which the
  // production CSP blocks — the icon font is self-hosted instead (wx-icons/wx-icons.css).
  const projectStart = props.plan.project_start;
  // A day column the user picked by clicking its date in the scale header (a second click on the
  // same date clears it) — to line bars up against one date by eye, like the "today" tint.
  const [selectedDay, setSelectedDay] = useState<string | null>(null);
  const highlightTime = useCallback(
    (d: Date, unit: string) => highlightDay(d, unit, projectStart, new Date(), selectedDay),
    [projectStart, selectedDay],
  );
  const dayOfCell = useCallback(
    (cell: Element): string | null => {
      const scale = cell.closest(".wx-scale");
      // Each cell also carries `date` and `unit` at runtime (the store builds them, and SVAR's own
      // header passes them to highlightTime), but GanttScaleCell's declared type leaves them out.
      const cells = api?.getState()._scales?.rows.at(-1)?.cells as ScaleCell[] | undefined;
      if (!scale || !cells) return null;
      const rect = cell.getBoundingClientRect();
      return dayAtOffset(cells, rect.left + rect.width / 2 - scale.getBoundingClientRect().left);
    },
    [api],
  );
  const toggleDayAt = (cell: Element) => {
    const day = dayOfCell(cell);
    if (day) setSelectedDay((current) => (current === day ? null : day));
  };

  // Keyboard access to the same picking. SVAR renders the day cells as plain divs, so they're
  // turned into buttons here: one roving tab stop (the focused cell, else the picked day, else
  // today, else the first visible day), ←/→ move along the row, Enter/Space toggle (onKeyDown
  // below). SVAR re-renders the virtualized header on scroll and zoom, so a MutationObserver on
  // it re-applies the attributes; it doesn't watch attributes, so setting them can't loop.
  const zoom = props.zoom;
  useEffect(() => {
    const root = containerRef.current;
    if (!root || !api) return;
    const today = toISODate(new Date());
    let frame = 0;
    const decorate = () => {
      frame = 0;
      const cells = Array.from(root.querySelectorAll<HTMLElement>(DAY_CELL));
      let focused: HTMLElement | undefined;
      let picked: HTMLElement | undefined;
      let todays: HTMLElement | undefined;
      let first: HTMLElement | undefined;
      for (const cell of cells) {
        const day = zoom === "day" ? dayOfCell(cell) : null;
        if (!day) {
          for (const attr of ["tabindex", "role", "aria-pressed", "aria-label"]) cell.removeAttribute(attr);
          continue;
        }
        cell.setAttribute("role", "button");
        cell.setAttribute("aria-pressed", String(day === selectedDay));
        cell.setAttribute("aria-label", `Выделить столбец ${formatRu(day)}`);
        cell.tabIndex = -1;
        if (cell === document.activeElement) focused = cell;
        if (day === selectedDay) picked = cell;
        if (day === today) todays = cell;
        first ??= cell;
      }
      const stop = focused ?? picked ?? todays ?? first;
      if (stop) stop.tabIndex = 0;
    };
    const inScale = (node: Node) =>
      (node instanceof Element ? node : node.parentElement)?.closest(".wx-scale") != null ||
      (node instanceof Element && node.querySelector(".wx-scale") != null);
    const observer = new MutationObserver((mutations) => {
      if (!frame && mutations.some((m) => inScale(m.target) || Array.from(m.addedNodes).some(inScale))) {
        frame = requestAnimationFrame(decorate);
      }
    });
    observer.observe(root, { childList: true, subtree: true, characterData: true });
    decorate();
    return () => {
      observer.disconnect();
      cancelAnimationFrame(frame);
    };
  }, [api, zoom, selectedDay, dayOfCell]);

  // Where the last press started and whether the pointer has since moved away, to tell a click
  // from the end of a bar drag (see isDragEnd) — including a drag brought back to where it began.
  const pointerDown = useRef<{ x: number; y: number; type: string; moved: boolean } | null>(null);
  // Unmounted mid-press (e.g. the plan was reset): don't leave the tooltip hidden app-wide.
  useEffect(() => () => document.body.classList.remove(BAR_PRESSED), []);

  // A change to tasks that are all off screen (scrolled away, or below the rendered rows) used to
  // happen unseen. Checked against the DOM once SVAR has drawn the new plan: a bar counts as on
  // screen when part of it is inside the chart's visible area — horizontally the `.wx-chart`
  // viewport, vertically the `.wx-gantt` one below the scale header.
  const barOnScreen = useCallback((id: number): boolean => {
    const root = containerRef.current;
    const bar = root?.querySelector(`.wx-bar[data-id="${id}"]`);
    const chart = root?.querySelector(".wx-chart");
    const gantt = root?.querySelector(".wx-gantt");
    if (!bar || !chart || !gantt) return false;
    const x = chart.getBoundingClientRect();
    const y = gantt.getBoundingClientRect();
    const header = root?.querySelector(".wx-scale")?.getBoundingClientRect().bottom ?? y.top;
    return boxesOverlap(bar.getBoundingClientRect(), { left: x.left, right: x.right, top: header, bottom: y.bottom });
  }, []);
  const scrollToTask = useCallback(
    (id: number) => {
      const task = api?.getTask(id) as { $x?: number; $y?: number } | undefined;
      const chart = containerRef.current?.querySelector(".wx-chart");
      const gantt = containerRef.current?.querySelector(".wx-gantt");
      if (!api || task?.$x == null || task.$y == null || !chart || !gantt) return;
      api.exec("scroll-chart", revealScroll({ x: task.$x, y: task.$y }, { width: chart.clientWidth, height: gantt.clientHeight }));
    },
    [api],
  );
  // Changed tasks offered by the notice («Изменено N задач вне видимой области · Показать»).
  const [offscreen, setOffscreen] = useState<number[] | null>(null);
  const showTasks = (ids: number[]) => {
    const first = firstInPlanOrder(handlers.current.plan, ids);
    if (first == null) return;
    scrollToTask(first);
    handlers.current.onFlash?.(ids);
  };
  const reveal = props.reveal;
  useEffect(() => {
    if (!reveal || !api) return;
    // Two frames: React commits the new tasks, then SVAR lays the bars out.
    let frame = requestAnimationFrame(() => {
      frame = requestAnimationFrame(() => {
        const present = reveal.ids.filter((id) => handlers.current.plan.tasks.some((t) => t.id === id));
        if (present.length === 0 || present.some(barOnScreen)) setOffscreen(null);
        else if (reveal.mode === "scroll") showTasks(present);
        else setOffscreen(present);
        handlers.current.onRevealed?.();
      });
    });
    return () => cancelAnimationFrame(frame);
    // showTasks reads everything through refs/stable callbacks; the request itself is the trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reveal, api, barOnScreen]);
  // The notice goes away by itself after a while, like a toast.
  useEffect(() => {
    if (!offscreen) return;
    const timer = setTimeout(() => setOffscreen(null), 20_000);
    return () => clearTimeout(timer);
  }, [offscreen]);

  const centerToday = props.centerToday;
  useEffect(() => {
    if (!centerToday || !api) return;
    const chart = containerRef.current?.querySelector(".wx-chart");
    const scales = api.getState()._scales;
    // Each cell carries `date` and `unit` at runtime (see dayOfCell).
    const cells = scales?.rows.at(-1)?.cells as ScaleCell[] | undefined;
    // A zero width: the chart is on the phone's other tab. It opens where it was.
    if (!chart?.clientWidth || !scales || !cells) return;
    const left = centerDayScroll(cells, scales.end, new Date(), chart.clientWidth);
    if (left == null) {
      toast.info("Сегодняшнего дня нет на шкале: план начинается позже или уже закончился");
      return;
    }
    api.exec("scroll-chart", { left });
  }, [centerToday, api]);

  const ThemeWrapper = props.dark ? WillowDark : Willow;

  return (
    <div
      ref={containerRef}
      className={`gantt-host relative h-full min-h-0${props.zoom === "day" ? " gantt-day-zoom" : ""}`}
      // Capture phase: SVAR's own drag handling must not be able to hide the press from us.
      onPointerDownCapture={(e) => {
        pointerDown.current = { x: e.clientX, y: e.clientY, type: e.pointerType, moved: false };
        if (e.button === 0 && e.target instanceof Element && e.target.closest(".wx-bar")) {
          hideTooltipWhilePressed();
        }
      }}
      onPointerMoveCapture={(e) => {
        const press = pointerDown.current;
        if (press && !press.moved && isDragEnd(press, { x: e.clientX, y: e.clientY }, press.type)) press.moved = true;
      }}
      onClick={(e) => {
        const press = pointerDown.current;
        pointerDown.current = null;
        // `detail` is 0 for a click from the keyboard (Enter/Space on the pencil): no press to judge.
        if (e.detail > 0 && (press?.moved || isDragEnd(press, { x: e.clientX, y: e.clientY }, press?.type))) return;
        // The bottom scale row holds the day numbers (in day zoom): a click there picks that column.
        const dayCell = e.target instanceof Element ? e.target.closest(DAY_CELL) : null;
        if (dayCell) {
          toggleDayAt(dayCell);
          return;
        }
        const id = closestTaskId(e.target);
        if (id != null) handlers.current.onOpenTask(id);
      }}
      onKeyDown={(e) => {
        const cell = e.target instanceof HTMLElement ? e.target.closest<HTMLElement>(DAY_CELL) : null;
        if (!cell?.hasAttribute("role")) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          toggleDayAt(cell);
          return;
        }
        if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
        const next = e.key === "ArrowRight" ? cell.nextElementSibling : cell.previousElementSibling;
        if (!(next instanceof HTMLElement)) return;
        e.preventDefault();
        cell.tabIndex = -1;
        next.tabIndex = 0;
        next.focus();
      }}
    >
      <RuLocale>
        <ThemeWrapper fonts={false}>
          <TooltipPlan.Provider value={props.plan}>
            <Tooltip api={api ?? undefined} content={BarTooltip}>
              <Gantt
                init={init}
                tasks={tasks}
                links={links}
                columns={columns}
                gridWidth={gridWidth}
                taskTypes={TASK_TYPES}
                readonly={props.readOnly}
                {...ZOOM_PRESETS[props.zoom]}
                highlightTime={highlightTime}
              />
            </Tooltip>
          </TooltipPlan.Provider>
        </ThemeWrapper>
      </RuLocale>
      {offscreen && (
        <div
          role="status"
          className="absolute bottom-4 left-1/2 z-20 flex -translate-x-1/2 items-center gap-3 rounded-md border border-border bg-popover px-3 py-1.5 text-sm text-popover-foreground shadow-md"
        >
          <span>
            Изменено {offscreen.length} {ruPlural(offscreen.length, "задача", "задачи", "задач")} вне видимой области
          </span>
          <button
            type="button"
            className="font-medium text-primary hover:underline"
            onClick={() => {
              showTasks(offscreen);
              setOffscreen(null);
            }}
          >
            Показать
          </button>
          <button
            type="button"
            aria-label="Скрыть"
            className="text-muted-foreground hover:text-foreground"
            onClick={() => setOffscreen(null)}
          >
            ×
          </button>
        </div>
      )}
    </div>
  );
}

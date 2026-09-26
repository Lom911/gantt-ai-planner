import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Gantt, Tooltip, Willow, WillowDark, type IApi } from "@svar-ui/react-gantt";
import "@svar-ui/react-gantt/all.css";
import "./wx-icons/wx-icons.css";
import "./gantt.css";
import { toast } from "sonner";
import { ZOOM_PRESETS, closestTaskId, highlightDay, isDragEnd, toSvarLinks, toSvarTasks, type Zoom } from "./mapping";
import { interpretBarChange, linkDeletionToOperation, linkToOperation } from "./interactions";
import { RuLocale } from "./locale";
import type { Operation, ScheduledPlan } from "@/api/types";
import { formatRu, addDays, parseISODate } from "@/lib/dates";
import { loadLayoutPrefs, saveLayoutPrefs } from "@/lib/layoutPrefs";

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

// SVAR's own tooltip (`Tooltip`/`content`) resolves `data-task-id` off the hovered element for us
// and hands back its own `ITask` (an intentionally loose `[key: string]: any` shape) — declaring
// every field here as optional (rather than importing our stricter `SvarTask`, whose fields are
// required) is what makes this assignable to SVAR's own content-prop type, since a required field
// on our side that ITask can't statically prove it has would fail that check even though the
// object handed over at runtime is exactly the `SvarTask` we fed in via `tasks`.
interface TaskTooltipFields {
  text?: string;
  start?: Date;
  end?: Date;
  workDays?: number;
  assignee?: string;
  slack?: number;
}

function BarTooltip({ data }: { api: IApi; data: Record<string, unknown> }) {
  const task = data.task as TaskTooltipFields | undefined;
  if (!task?.start || !task.end) return null;
  return (
    <div className="max-w-64 rounded-md border border-border bg-popover px-2.5 py-2 text-xs text-popover-foreground shadow-md">
      <div className="font-medium">{task.text}</div>
      <div className="text-muted-foreground">
        {formatRu(task.start)}–{formatRu(addDays(task.end, -1))}
        {task.workDays != null && <> · {task.workDays} раб.дн.</>}
      </div>
      {task.assignee && <div className="text-muted-foreground">{task.assignee}</div>}
      {task.slack != null && <div className="text-muted-foreground">Резерв {task.slack} дн.</div>}
    </div>
  );
}

export function GanttView(props: {
  plan: ScheduledPlan;
  zoom: Zoom;
  highlighted: ReadonlySet<number>;
  readOnly: boolean;
  dark?: boolean;
  onOpenTask(id: number): void;
  onApply(ops: Operation[]): Promise<void>;
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
      ? [
          { id: "id", header: "№", width: 40, align: "center" as const },
          { id: "text", header: "Задача", width: 140, flexgrow: 1 },
        ]
      : [
          { id: "id", header: "№", width: w("id", 44), align: "center" as const },
          { id: "text", header: "Задача", width: w("text", 180), flexgrow: 1 },
          { id: "assignee", header: "Исполнитель", width: w("assignee", 130) },
          { id: "startLabel", header: "Начало", width: w("startLabel", 76), align: "center" as const },
          { id: "workDays", header: "Дн.", width: w("workDays", 48), align: "center" as const },
        ];
  }, [narrow, prefs]);
  const gridWidth = useMemo(
    () => (!narrow && prefs.gridWidth) || columns.reduce((sum, c) => sum + c.width, 0),
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
      if (!narrowRef.current && width > 0) saveLayoutPrefs({ gridWidth: Math.round(width) });
    });
    api.on("set-columns", ({ columns: cols }: { columns: { id?: string; width?: number }[] }) => {
      if (narrowRef.current) return;
      const widths = Object.fromEntries(
        cols.filter((c) => c.id && typeof c.width === "number").map((c) => [c.id!, Math.round(c.width!)]),
      );
      saveLayoutPrefs({ columns: widths });
    });

    // `select-task` also fires on keyboard grid navigation, so opening the task modal from it
    // would pop the modal while the user is just arrowing through rows. Instead, a real pointer
    // click is handled by the container's own onClick below (via `closestTaskId`); double-click
    // still routes through `show-editor`, which we intercept to open our modal instead of
    // SVAR's built-in editor.
    api.intercept("show-editor", ({ id }: { id: number | string | null }) => {
      if (id != null) handlers.current.onOpenTask(Number(id));
      return false;
    });

    // A bar drag or resize is committed as an `update-task` event carrying either `diff` (the
    // number of cells the bar moved/grew by, set by a move/resize commit) or `inProgress` (set
    // while a progress-marker drag is live) — see @svar-ui/gantt-store's DataStore.d.ts
    // (`IDataMethodsConfig["update-task"]`) and the bar-drag handlers in
    // @svar-ui/react-gantt's compiled source. Anything else (a plain field edit from our own
    // task modal, an agent-applied change, etc.) has neither and is left alone here — SVAR
    // already applied it to its own store by the time this fires. By the same point, `ev.task`
    // is the *resolved* task (real `start`/`end` Date objects, not raw pixel deltas), so we don't
    // need to reimplement SVAR's own cell-to-date math.
    api.on(
      "update-task",
      (ev: { id: number | string; task: { start?: Date; end?: Date }; diff?: number; inProgress?: boolean }) => {
        if (handlers.current.readOnly) return;
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
  const highlightTime = useCallback(
    (d: Date, unit: string) => highlightDay(d, unit, projectStart, new Date()),
    [projectStart],
  );

  // Where the last press started, to tell a click from the end of a bar drag (see isDragEnd).
  const pointerDown = useRef<{ x: number; y: number } | null>(null);

  const ThemeWrapper = props.dark ? WillowDark : Willow;

  return (
    <div
      ref={containerRef}
      className="gantt-host h-full min-h-0"
      // Capture phase: SVAR's own drag handling must not be able to hide the press from us.
      onPointerDownCapture={(e) => {
        pointerDown.current = { x: e.clientX, y: e.clientY };
      }}
      onClick={(e) => {
        if (isDragEnd(pointerDown.current, { x: e.clientX, y: e.clientY })) return;
        const id = closestTaskId(e.target);
        if (id != null) handlers.current.onOpenTask(id);
      }}
    >
      <RuLocale>
        <ThemeWrapper fonts={false}>
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
        </ThemeWrapper>
      </RuLocale>
    </div>
  );
}

import type { ScheduledPlan } from "@/api/types";
import { addDays, calendarDaysInclusive, parseISODate, toISODate } from "@/lib/dates";
import { ruPlural } from "@/lib/resourceSummary";
import type { IScaleConfig } from "@svar-ui/react-gantt";

export type Zoom = "day" | "week" | "month";

export interface SvarTask {
  id: number; text: string; start: Date; end: Date; type: "task" | "critical" | "changed" | "conflict";
  progress: number; assignee: string; workDays: number; slack: number; conflict: boolean;
  startLabel: string; // "дд.мм" for the grid's «Начало» column
}
export interface SvarLink { id: number; source: number; target: number; type: "e2s" }

// A task can be critical *and* overloaded at once; `type` only carries one value, so a bar's
// visual state is picked by priority: a just-changed highlight always wins (it's transient and
// most relevant right after an edit), then the critical path (drives the whole project's
// end date), then a plain resourcing conflict.
export function toSvarTasks(plan: ScheduledPlan, highlighted: ReadonlySet<number>): SvarTask[] {
  return plan.tasks.map((t) => ({
    id: t.id,
    text: t.name,
    start: parseISODate(t.start),
    end: addDays(parseISODate(t.end), 1),
    type: highlighted.has(t.id)
      ? "changed"
      : t.is_critical
        ? "critical"
        : t.overallocated_with.length > 0
          ? "conflict"
          : "task",
    progress: 0,
    assignee: t.assignee ?? "",
    workDays: t.duration,
    slack: t.slack,
    conflict: t.overallocated_with.length > 0,
    startLabel: `${t.start.slice(8, 10)}.${t.start.slice(5, 7)}`,
  }));
}

export function toSvarLinks(plan: ScheduledPlan): SvarLink[] {
  return plan.dependencies.map((d, i) => ({ id: i + 1, source: d.predecessor_id, target: d.successor_id, type: "e2s" }));
}

// Browsers fire `click` after any press + release on the same element — including the end of a
// bar drag or resize. Such a click must not open the task modal on top of the change the user
// just made, so a pointer that moved more than a few pixels since pointerdown isn't a click.
const DRAG_CLICK_THRESHOLD_PX = 4;
export function isDragEnd(down: { x: number; y: number } | null, up: { x: number; y: number }): boolean {
  return down != null && Math.hypot(up.x - down.x, up.y - down.y) > DRAG_CLICK_THRESHOLD_PX;
}

// SVAR (@svar-ui/lib-dom `locate`) marks both grid rows and gantt bars with a `data-id`
// attribute holding the task id. We use it to open the task modal from a genuine pointer
// double click or the grid's pencil button (bubbling up from the row/bar to our own
// container), instead of SVAR's API events (`select-task` also fires on keyboard navigation).
export function closestTaskId(target: EventTarget | null): number | null {
  if (!(target instanceof Element)) return null;
  // A click on a bar's link connector (`.wx-link`, the little dot at each end used to draw a
  // dependency) still bubbles up through the bar's own `[data-id]` element — without this guard
  // it would pop the task modal open *and* cover the target connector before the user's second
  // click can land on it, making it impossible to ever finish drawing a link.
  if (target.closest(".wx-link")) return null;
  const el = target.closest("[data-id]");
  const raw = el?.getAttribute("data-id");
  if (!raw) return null;
  const id = Number(raw);
  return Number.isFinite(id) ? id : null;
}

// SVAR IScaleConfig = {unit, step, format?, css?} (@svar-ui/gantt-store). `format` strings use
// SVAR's own token syntax (%F full month, %M short month, %j day-of-month, %Y year); the
// RuLocale wrapper supplies the Russian month/day words those tokens render.
export const ZOOM_PRESETS: Record<Zoom, { scales: IScaleConfig[]; cellWidth: number }> = {
  day: {
    scales: [
      { unit: "month", step: 1, format: "%F %Y" },
      { unit: "day", step: 1, format: "%j" },
    ],
    cellWidth: 38,
  },
  week: {
    scales: [
      { unit: "month", step: 1, format: "%F %Y" },
      { unit: "week", step: 1, format: "Нед. %W" },
    ],
    cellWidth: 100,
  },
  month: {
    scales: [
      { unit: "year", step: 1, format: "%Y" },
      { unit: "month", step: 1, format: "%F" },
    ],
    cellWidth: 130,
  },
};

// SVAR `highlightTime` callback body: CSS classes for a timeline day column (header cell and the
// chart body column). Day scale only — a week or month cell spans many days, so there's no single
// column to mark. "gantt-project-start" draws the line the whole schedule is counted from: tasks
// without predecessors start on the project start date. (SVAR's own `markers` would label it,
// but the MIT build's store resets them — a PRO feature.) "gantt-selected-day" is the column the
// user picked by clicking its date in the scale header (see dayAtOffset). "gantt-weekend" tints
// Saturdays and Sundays: a bar spans them, but a duration counts only working days (the backend's
// calendar is Mon-Fri with no holidays, backend/app/domain/calendar.py), so without the tint a
// 5-working-day task over a weekend looked 2 days too long.
export function highlightDay(
  d: Date,
  unit: string,
  projectStart: string,
  today: Date,
  selectedDay: string | null = null,
): string {
  if (unit !== "day") return "";
  const classes: string[] = [];
  const iso = toISODate(d);
  if (d.toDateString() === today.toDateString()) classes.push("gantt-today");
  if (iso === projectStart) classes.push("gantt-project-start");
  if (iso === selectedDay) classes.push("gantt-selected-day");
  if (d.getDay() === 0 || d.getDay() === 6) classes.push("gantt-weekend");
  return classes.join(" ");
}

// A bar's length in words (its tooltip): the calendar days it spans and how many of them are
// working days — the duration everything else (the grid's «Дн.», the task card) shows.
export function durationLabel(start: string, end: string, workDays: number): string {
  const days = calendarDaysInclusive(parseISODate(start), parseISODate(end));
  if (days === workDays) return `${workDays} ${ruPlural(workDays, "рабочий день", "рабочих дня", "рабочих дней")}`;
  return `${days} ${ruPlural(days, "день", "дня", "дней")}, из них ${workDays} ${ruPlural(workDays, "рабочий", "рабочих", "рабочих")}`;
}

// SVAR's scale header cells carry no date attribute, so a click on one is mapped back to a day
// through the scale's own cell list (`_scales.rows[last].cells`, laid out left to right from the
// scale's left edge): the ISO date of the day cell under `x`, or null off the scale or when the
// bottom row isn't days (week/month zoom).
export interface ScaleCell { date: Date; width: number; unit: string }
export function dayAtOffset(cells: readonly ScaleCell[], x: number): string | null {
  if (x < 0) return null;
  let left = 0;
  for (const cell of cells) {
    if (x < left + cell.width) return cell.unit === "day" ? toISODate(cell.date) : null;
    left += cell.width;
  }
  return null;
}

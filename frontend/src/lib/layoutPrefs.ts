import type { Zoom } from "@/components/gantt/mapping";

// Per-browser layout the user adjusted by hand — chart/chat split, a collapsed chat, the
// grid/timeline divider, grid column widths, zoom — kept in localStorage so a reload or tomorrow's visit opens the same
// way. It's a convenience only: every read/write is guarded (private mode, blocked storage,
// quota) and anything missing, corrupt or out of range falls back to the defaults.
export interface LayoutPrefs {
  splitRatio?: number; // chart pane width / whole width
  gridWidth?: number; // px, grid (table) part of the chart
  // Set with every gridWidth saved since the grid got its pencil (edit) column; a width saved
  // before that is widened by the column on load (see savedGridWidth).
  gridIncludesEdit?: boolean;
  columns?: Record<string, number>; // px per grid column id
  zoom?: Zoom;
  chatCollapsed?: boolean; // chat pane folded into a rail so the chart gets the whole width
}

const KEY = "gantt-ai-planner:layout:v1";
const COLUMN_IDS = new Set(["id", "text", "assignee", "startLabel", "workDays"]);
const ZOOMS = new Set<Zoom>(["day", "week", "month"]);

const inRange = (v: unknown, min: number, max: number): v is number =>
  typeof v === "number" && Number.isFinite(v) && v >= min && v <= max;

function sanitize(raw: unknown): LayoutPrefs {
  if (typeof raw !== "object" || raw === null) return {};
  const r = raw as Record<string, unknown>;
  const out: LayoutPrefs = {};
  if (inRange(r.splitRatio, 0.2, 0.9)) out.splitRatio = r.splitRatio;
  if (inRange(r.gridWidth, 120, 1400)) out.gridWidth = Math.round(r.gridWidth);
  if (typeof r.columns === "object" && r.columns !== null) {
    const cols = Object.fromEntries(
      Object.entries(r.columns as Record<string, unknown>).filter(
        (e): e is [string, number] => COLUMN_IDS.has(e[0]) && inRange(e[1], 30, 800),
      ),
    );
    if (Object.keys(cols).length > 0) out.columns = cols;
  }
  if (typeof r.zoom === "string" && ZOOMS.has(r.zoom as Zoom)) out.zoom = r.zoom as Zoom;
  if (typeof r.chatCollapsed === "boolean") out.chatCollapsed = r.chatCollapsed;
  if (r.gridIncludesEdit === true) out.gridIncludesEdit = true;
  return out;
}

// The grid width to open with, or undefined for the default (the columns' sum). A width saved
// before the pencil column existed would now hide it behind the timeline, so it grows by that
// column's width — once: the next drag saves the new width with `gridIncludesEdit`.
export function savedGridWidth(prefs: LayoutPrefs, editColumnWidth: number): number | undefined {
  if (!prefs.gridWidth) return undefined;
  return prefs.gridIncludesEdit ? prefs.gridWidth : prefs.gridWidth + editColumnWidth;
}

export function loadLayoutPrefs(): LayoutPrefs {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? sanitize(JSON.parse(raw)) : {};
  } catch {
    return {};
  }
}

export function saveLayoutPrefs(patch: LayoutPrefs): void {
  try {
    localStorage.setItem(KEY, JSON.stringify({ ...loadLayoutPrefs(), ...patch }));
  } catch {
    /* storage unavailable: the layout just won't be remembered */
  }
}

export function clearLayoutPrefs(): void {
  try {
    localStorage.removeItem(KEY);
  } catch {
    /* nothing to clear */
  }
}

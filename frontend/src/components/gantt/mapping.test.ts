import {
  boxesOverlap,
  closestTaskId,
  dayAtOffset,
  durationLabel,
  firstInPlanOrder,
  highlightDay,
  isDragEnd,
  revealScroll,
  toSvarLinks,
  toSvarTasks,
} from "./mapping";
import type { ScheduledPlan } from "@/api/types";

const plan: ScheduledPlan = {
  project_start: "2026-09-21", project_end: "2026-09-25", last_id: 2, critical_path: [1],
  tasks: [
    { id: 1, name: "A", description: "", assignee: "Анна", duration: 3, constraint_start: null,
      start: "2026-09-21", end: "2026-09-23", slack: 0, is_critical: true, constrained_by: "project_start", overallocated_with: [] },
    { id: 2, name: "B", description: "", assignee: null, duration: 1, constraint_start: null,
      start: "2026-09-24", end: "2026-09-24", slack: 1, is_critical: false, constrained_by: "predecessor:1", overallocated_with: [] },
  ],
  dependencies: [{ predecessor_id: 1, successor_id: 2, lag: 0 }],
};

test("tasks map with exclusive end and types", () => {
  const [a, b] = toSvarTasks(plan, new Set([2]));
  expect(a.text).toBe("A");
  expect(a.start.getDate()).toBe(21);
  expect(a.end.getDate()).toBe(24); // inclusive 23 + 1
  expect(a.type).toBe("critical");
  expect(a.slack).toBe(0);
  expect(b.type).toBe("changed");
  expect(b.slack).toBe(1);
  expect(a.startLabel).toBe("21.09");
  expect(b.startLabel).toBe("24.09");
});

test("an overloaded, non-critical task not currently highlighted maps to the conflict type", () => {
  const overloaded: ScheduledPlan = {
    ...plan,
    tasks: [{ ...plan.tasks[1], id: 3, is_critical: false, overallocated_with: [4] }],
  };
  const [t] = toSvarTasks(overloaded, new Set());
  expect(t.type).toBe("conflict");
  expect(t.conflict).toBe(true);
});

test("links are finish-to-start", () => {
  expect(toSvarLinks(plan)).toEqual([{ id: 1, source: 1, target: 2, type: "e2s" }]);
});

test("closestTaskId reads data-id off the clicked element or an ancestor", () => {
  const row = document.createElement("div");
  row.setAttribute("data-id", "7");
  const label = document.createElement("span");
  row.appendChild(label);
  expect(closestTaskId(label)).toBe(7);
  expect(closestTaskId(row)).toBe(7);
});

test("closestTaskId returns null without a data-id ancestor or a non-element target", () => {
  const outside = document.createElement("span");
  expect(closestTaskId(outside)).toBeNull();
  expect(closestTaskId(null)).toBeNull();
});

test("closestTaskId ignores a click on a link connector so link-drawing isn't interrupted", () => {
  const row = document.createElement("div");
  row.setAttribute("data-id", "7");
  const dot = document.createElement("div");
  dot.className = "wx-link wx-right wx-target";
  row.appendChild(dot);
  expect(closestTaskId(dot)).toBeNull();
});

test("closestTaskId ignores the delete button of a selected link, which SVAR renders inside the bar", () => {
  const bar = document.createElement("div");
  bar.setAttribute("data-id", "17");
  bar.className = "wx-bar wx-task";
  const button = document.createElement("div");
  button.className = "wx-delete-button";
  const icon = document.createElement("i");
  icon.className = "wx-delete-button-icon";
  button.appendChild(icon);
  bar.appendChild(button);
  expect(closestTaskId(icon)).toBeNull();
  expect(closestTaskId(button)).toBeNull();
});

describe("highlightDay", () => {
  const today = new Date(2026, 8, 26);
  it("marks the project start and today in the day scale", () => {
    expect(highlightDay(new Date(2026, 8, 7), "day", "2026-09-07", today)).toBe("gantt-project-start");
    expect(highlightDay(new Date(2026, 8, 26), "day", "2026-09-07", today)).toBe("gantt-today gantt-weekend");
    expect(highlightDay(new Date(2026, 8, 26), "day", "2026-09-26", today)).toBe("gantt-today gantt-project-start gantt-weekend");
    expect(highlightDay(new Date(2026, 8, 8), "day", "2026-09-07", today)).toBe("");
  });
  it("marks Saturdays and Sundays: they're in a bar's length but not in its working days", () => {
    expect(highlightDay(new Date(2026, 9, 3), "day", "2026-09-07", today)).toBe("gantt-weekend");
    expect(highlightDay(new Date(2026, 9, 4), "day", "2026-09-07", today)).toBe("gantt-weekend");
    expect(highlightDay(new Date(2026, 9, 5), "day", "2026-09-07", today)).toBe("");
    expect(highlightDay(new Date(2026, 9, 3), "week", "2026-09-07", today)).toBe("");
  });
  it("marks nothing in coarser scales (a week/month cell isn't one day)", () => {
    expect(highlightDay(new Date(2026, 8, 7), "week", "2026-09-07", today)).toBe("");
    expect(highlightDay(new Date(2026, 8, 7), "week", "2026-09-01", today, "2026-09-07")).toBe("");
  });
  it("marks the day the user picked in the scale header, alongside today", () => {
    expect(highlightDay(new Date(2026, 8, 15), "day", "2026-09-07", today, "2026-09-15")).toBe("gantt-selected-day");
    expect(highlightDay(new Date(2026, 8, 26), "day", "2026-09-07", today, "2026-09-26")).toBe(
      "gantt-today gantt-selected-day gantt-weekend",
    );
    expect(highlightDay(new Date(2026, 8, 16), "day", "2026-09-07", today, "2026-09-15")).toBe("");
  });
});

describe("dayAtOffset", () => {
  const cells = [
    { date: new Date(2026, 8, 7), width: 38, unit: "day" },
    { date: new Date(2026, 8, 8), width: 38, unit: "day" },
    { date: new Date(2026, 8, 9), width: 38, unit: "day" },
  ];
  it("finds the day cell under an x offset from the scale's left edge", () => {
    expect(dayAtOffset(cells, 0)).toBe("2026-09-07");
    expect(dayAtOffset(cells, 37.9)).toBe("2026-09-07");
    expect(dayAtOffset(cells, 38)).toBe("2026-09-08");
    expect(dayAtOffset(cells, 95)).toBe("2026-09-09");
  });
  it("returns null outside the scale or on a coarser scale", () => {
    expect(dayAtOffset(cells, -1)).toBeNull();
    expect(dayAtOffset(cells, 114)).toBeNull();
    expect(dayAtOffset([{ date: new Date(2026, 8, 7), width: 100, unit: "week" }], 10)).toBeNull();
  });
});

test("a click that ends a bar drag doesn't count as a click on the task", () => {
  expect(isDragEnd({ x: 100, y: 50 }, { x: 177, y: 50 })).toBe(true); // dragged 2 days
  expect(isDragEnd({ x: 100, y: 50 }, { x: 102, y: 51 })).toBe(false); // hand jitter on a click
  expect(isDragEnd(null, { x: 5, y: 5 })).toBe(false); // no pointerdown seen (keyboard)
});

test("a finger may wander a little more than a mouse before a tap stops being a tap", () => {
  expect(isDragEnd({ x: 100, y: 50 }, { x: 107, y: 50 })).toBe(true); // mouse: 7px is a drag
  expect(isDragEnd({ x: 100, y: 50 }, { x: 107, y: 50 }, "touch")).toBe(false); // finger: still a tap
  expect(isDragEnd({ x: 100, y: 50 }, { x: 125, y: 50 }, "touch")).toBe(true); // finger dragged
});

describe("durationLabel", () => {
  it("says how many of a bar's calendar days are working days", () => {
    // Thu 01.10 – Wed 07.10: seven days on the chart, the weekend in the middle doesn't count.
    expect(durationLabel("2026-10-01", "2026-10-07", 5)).toBe("7 дней, из них 5 рабочих");
    expect(durationLabel("2026-09-25", "2026-09-28", 2)).toBe("4 дня, из них 2 рабочих");
    expect(durationLabel("2026-09-21", "2026-10-11", 15)).toBe("21 день, из них 15 рабочих");
  });
  it("names just the working days when the bar has no weekend in it", () => {
    expect(durationLabel("2026-09-21", "2026-09-23", 3)).toBe("3 рабочих дня");
    expect(durationLabel("2026-09-24", "2026-09-24", 1)).toBe("1 рабочий день");
    expect(durationLabel("2026-09-21", "2026-09-25", 5)).toBe("5 рабочих дней");
  });
});

describe("revealing changed tasks off screen", () => {
  const view = { left: 500, top: 150, right: 1200, bottom: 850 };
  it("a bar counts as on screen when any part of it is inside the chart's viewport", () => {
    expect(boxesOverlap({ left: 600, top: 300, right: 700, bottom: 330 }, view)).toBe(true);
    expect(boxesOverlap({ left: 1150, top: 300, right: 1400, bottom: 330 }, view)).toBe(true); // cut by the right edge
    expect(boxesOverlap({ left: 1300, top: 300, right: 1400, bottom: 330 }, view)).toBe(false); // further right
    expect(boxesOverlap({ left: 600, top: 900, right: 700, bottom: 930 }, view)).toBe(false); // below
    expect(boxesOverlap({ left: 600, top: 100, right: 700, bottom: 150 }, view)).toBe(false); // under the scale header
  });
  it("scrolls the task's row to a third of the way down and its bar just right of the left edge", () => {
    expect(revealScroll({ x: 2000, y: 1200 }, { width: 700, height: 600 })).toEqual({ left: 2000 - 160, top: 1000 });
    expect(revealScroll({ x: 50, y: 40 }, { width: 700, height: 600 })).toEqual({ left: 0, top: 0 });
    expect(revealScroll({ x: 300, y: 300 }, { width: 400, height: 300 })).toEqual({ left: 200, top: 200 });
  });
  it("goes to the topmost changed task in the plan's row order", () => {
    expect(firstInPlanOrder(plan, [2, 1])).toBe(1);
    expect(firstInPlanOrder(plan, [2])).toBe(2);
    expect(firstInPlanOrder(plan, [99])).toBeNull(); // deleted meanwhile
  });
});

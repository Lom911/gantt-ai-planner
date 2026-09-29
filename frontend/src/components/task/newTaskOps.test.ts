import type { ScheduledPlan, ScheduledTask } from "@/api/types";
import {
  buildNewTaskOps,
  deletedAnchorNotice,
  dropDeletedAnchors,
  emptyNewTaskForm,
  taskLimitNotice,
  validateNewTaskForm,
} from "./newTaskOps";

const task = (id: number): ScheduledTask => ({
  id,
  name: `Задача ${id}`,
  description: "",
  assignee: null,
  duration: 2,
  constraint_start: null,
  start: "2026-09-07",
  end: "2026-09-08",
  slack: 0,
  is_critical: false,
  constrained_by: "project_start",
  overallocated_with: [],
});

const plan: ScheduledPlan = {
  project_start: "2026-09-07",
  project_end: "2026-09-30",
  last_id: 9,
  tasks: [task(6), task(7), task(9)],
  dependencies: [{ predecessor_id: 6, successor_id: 7, lag: 2 }],
  critical_path: [],
};

test("from the toolbar: goes to the end of the list and depends on nothing", () => {
  const form = emptyNewTaskForm(null);
  expect(form).toMatchObject({ afterId: null, predecessorId: null, successorId: null, duration: 1 });
  expect(buildNewTaskOps(plan, { ...form, name: "  Новая  " })).toEqual([
    { op: "add_task", name: "Новая", description: "", assignee: null, duration: 1, predecessors: [] },
  ]);
});

test("from a task's card: placed right after it and starts after it ends", () => {
  const form = emptyNewTaskForm(6);
  expect(form).toMatchObject({ afterId: 6, predecessorId: 6, successorId: null });
  expect(buildNewTaskOps(plan, { ...form, name: "Ревью", assignee: " Анна ", duration: 3 })).toEqual([
    {
      op: "add_task",
      name: "Ревью",
      description: "",
      assignee: "Анна",
      duration: 3,
      predecessors: [{ id: 6 }],
      after_id: 6,
    },
  ]);
});

test("between 6 and 7: the 6 → 7 link becomes 6 → new → 7, keeping its lag on the second hop", () => {
  const ops = buildNewTaskOps(plan, { ...emptyNewTaskForm(6), name: "Вставка", successorId: 7 });
  expect(ops).toEqual([
    expect.objectContaining({ op: "add_task", after_id: 6, predecessors: [{ id: 6 }] }),
    { op: "remove_dependency", predecessor_id: 6, successor_id: 7 },
    { op: "add_dependency", predecessor_id: 10, successor_id: 7, lag: 2 },
  ]);
});

test("a successor that didn't depend on the predecessor just gets a new link", () => {
  const ops = buildNewTaskOps(plan, { ...emptyNewTaskForm(null), name: "X", successorId: 9 });
  expect(ops.slice(1)).toEqual([{ op: "add_dependency", predecessor_id: 10, successor_id: 9, lag: 0 }]);
});

test("a «не раньше» date becomes a move_task on the new id", () => {
  const ops = buildNewTaskOps(plan, { ...emptyNewTaskForm(null), name: "X", constraint: "2026-10-01" });
  expect(ops[1]).toEqual({ op: "move_task", id: 10, start_date: "2026-10-01" });
});

test("validation: the base fields, and predecessor ≠ successor", () => {
  expect(validateNewTaskForm(plan, { ...emptyNewTaskForm(null), name: " " })).toMatch(/Название/);
  expect(validateNewTaskForm(plan, { ...emptyNewTaskForm(6), name: "X", successorId: 6 })).toMatch(/разными/);
  expect(validateNewTaskForm(plan, { ...emptyNewTaskForm(6), name: "X", successorId: 7 })).toBeNull();
});

test("validation: a successor that already leads to the predecessor would close a cycle", () => {
  // 6 → 7 → 9: a new task after 9 and before 6 would make 6 → 7 → 9 → new → 6.
  const chain = { ...plan, dependencies: [...plan.dependencies, { predecessor_id: 7, successor_id: 9, lag: 0 }] };
  expect(validateNewTaskForm(chain, { ...emptyNewTaskForm(9), name: "X", successorId: 6 })).toMatch(
    /№6 уже идёт раньше №9/,
  );
  expect(validateNewTaskForm(chain, { ...emptyNewTaskForm(6), name: "X", successorId: 9 })).toBeNull();
});

test("a chosen task deleted while the form is open: its selections are cleared and reported", () => {
  // Opened from 6's card (after 6, starting after it), then 7 picked as the successor; the agent
  // deletes 6 and 7. What the user typed stays.
  const form = { ...emptyNewTaskForm(6), name: "Ревью", successorId: 7 };
  const withoutThem = { ...plan, tasks: [task(9)], dependencies: [] };
  expect(dropDeletedAnchors(withoutThem, form)).toEqual({
    form: { ...form, afterId: null, predecessorId: null, successorId: null },
    deleted: { afterId: 6, predecessorId: 6, successorId: 7 },
  });
});

test("every chosen task still in the plan: nothing to drop", () => {
  expect(dropDeletedAnchors(plan, { ...emptyNewTaskForm(6), successorId: 7 })).toBeNull();
  expect(dropDeletedAnchors({ ...plan, tasks: [] }, emptyNewTaskForm(null))).toBeNull();
});

test("the deleted-task notice names the task", () => {
  expect(deletedAnchorNotice(7)).toBe("Задачу №7 удалили, пока форма была открыта — выберите другую");
});

test("the task limit: a notice once the plan has reached it, nothing below it or before meta loads", () => {
  const withTasks = (n: number) => ({ ...plan, tasks: Array.from({ length: n }, (_, i) => task(i + 1)) });
  expect(taskLimitNotice(withTasks(500), 500)).toBe("В плане уже 500 задач — это предел");
  expect(taskLimitNotice(withTasks(2), 2)).toBe("В плане уже 2 задачи — это предел");
  expect(taskLimitNotice(withTasks(499), 500)).toBeNull();
  expect(taskLimitNotice(withTasks(500), undefined)).toBeNull();
});

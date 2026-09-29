import type { AddTaskOp, Operation, ScheduledPlan } from "@/api/types";
import { ruPlural } from "@/lib/resourceSummary";
import { validateTaskForm, type TaskForm } from "./taskOps";

// The new-task form: the task's own fields (as in the task modal) plus where it goes.
// `afterId` is only the row's place in the list (null = the end); `predecessorId` is the task it
// starts after; `successorId` is a task that will wait for it. Picking both a predecessor and a
// successor that were linked inserts the new task into that link ("между 6 и 7").
export interface NewTaskForm extends TaskForm {
  afterId: number | null;
  predecessorId: number | null;
  successorId: number | null;
}

// `anchorId`: the task whose card the user opened «Добавить после» from (the new task goes right
// after it and starts when it ends), or null from the toolbar button.
export function emptyNewTaskForm(anchorId: number | null): NewTaskForm {
  return {
    name: "",
    description: "",
    assignee: "",
    duration: 1,
    constraint: null,
    afterId: anchorId,
    predecessorId: anchorId,
    successorId: null,
  };
}

// The form fields that point at an existing task.
export type AnchorField = "predecessorId" | "successorId" | "afterId";
const ANCHOR_FIELDS: AnchorField[] = ["predecessorId", "successorId", "afterId"];

// The form stays open while the plan moves on: the agent or another tab can delete a task it
// points at, and the server would refuse the batch only after the user filled everything in.
// Returns the form with every such selection cleared plus which task each field had, or null
// when all of them are still in the plan.
export function dropDeletedAnchors(
  plan: ScheduledPlan,
  form: NewTaskForm,
): { form: NewTaskForm; deleted: Partial<Record<AnchorField, number>> } | null {
  const ids = new Set(plan.tasks.map((t) => t.id));
  const next = { ...form };
  const deleted: Partial<Record<AnchorField, number>> = {};
  for (const field of ANCHOR_FIELDS) {
    const id = form[field];
    if (id == null || ids.has(id)) continue;
    next[field] = null;
    deleted[field] = id;
  }
  return Object.keys(deleted).length > 0 ? { form: next, deleted } : null;
}

export function deletedAnchorNotice(id: number): string {
  return `Задачу №${id} удалили, пока форма была открыта — выберите другую`;
}

// The backend refuses a plan over `maxTasks` (GET /api/meta, backend MAX_TASKS). Null while
// there's still room, and while meta hasn't loaded — the server enforces the limit regardless.
export function taskLimitNotice(plan: ScheduledPlan, maxTasks: number | undefined): string | null {
  if (maxTasks == null || plan.tasks.length < maxTasks) return null;
  return `В плане уже ${maxTasks} ${ruPlural(maxTasks, "задача", "задачи", "задач")} — это предел`;
}

// One atomic batch. The new task's id is `last_id + 1` — the backend numbers add_task that way
// within a batch, and the batch is sent with the plan version it was built on, so the id can't
// have been taken in between. An existing predecessor → successor link is replaced by the chain
// through the new task, its lag moved to the second hop (the successor keeps its gap).
export function buildNewTaskOps(plan: ScheduledPlan, form: NewTaskForm): Operation[] {
  const newId = plan.last_id + 1;
  const add: AddTaskOp = {
    op: "add_task",
    name: form.name.trim(),
    description: form.description.trim(),
    assignee: form.assignee.trim() || null,
    duration: form.duration,
    predecessors: form.predecessorId != null ? [{ id: form.predecessorId }] : [],
  };
  if (form.afterId != null) add.after_id = form.afterId;
  const ops: Operation[] = [add];

  if (form.constraint) ops.push({ op: "move_task", id: newId, start_date: form.constraint });

  if (form.successorId != null) {
    const replaced = plan.dependencies.find(
      (d) => d.predecessor_id === form.predecessorId && d.successor_id === form.successorId,
    );
    if (replaced) {
      ops.push({ op: "remove_dependency", predecessor_id: replaced.predecessor_id, successor_id: replaced.successor_id });
    }
    ops.push({ op: "add_dependency", predecessor_id: newId, successor_id: form.successorId, lag: replaced?.lag ?? 0 });
  }
  return ops;
}

export function validateNewTaskForm(plan: ScheduledPlan, form: NewTaskForm): string | null {
  const base = validateTaskForm(form);
  if (base) return base;
  const { predecessorId: pred, successorId: succ } = form;
  if (pred != null && pred === succ) {
    return "Предшественник и последователь должны быть разными задачами";
  }
  // pred → new → succ closes a cycle exactly when succ already leads to pred. The backend would
  // refuse the batch anyway; saying so here names the two tasks instead of a generic cycle error.
  if (pred != null && succ != null && leadsTo(plan, succ, pred)) {
    return `№${succ} уже идёт раньше №${pred} по цепочке связей — новая задача между ними замкнула бы цикл`;
  }
  return null;
}

function leadsTo(plan: ScheduledPlan, from: number, to: number): boolean {
  const seen = new Set([from]);
  const queue = [from];
  while (queue.length) {
    const id = queue.shift()!;
    for (const d of plan.dependencies) {
      if (d.predecessor_id !== id || seen.has(d.successor_id)) continue;
      if (d.successor_id === to) return true;
      seen.add(d.successor_id);
      queue.push(d.successor_id);
    }
  }
  return false;
}

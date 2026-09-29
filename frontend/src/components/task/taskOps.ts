import type { MoveTaskOp, Operation, ScheduledTask, UpdateTaskOp } from "@/api/types";
import { formatRu } from "@/lib/dates";

export interface TaskForm {
  name: string;
  description: string;
  assignee: string;
  duration: number;
  constraint: string | null;
}

type TaskField = keyof TaskForm;
type FieldValue = TaskForm[TaskField];

// In the order the card shows them.
const TASK_FIELDS: readonly TaskField[] = ["name", "description", "assignee", "duration", "constraint"];

export function formFromTask(task: ScheduledTask): TaskForm {
  return {
    name: task.name,
    description: task.description,
    assignee: task.assignee ?? "",
    duration: task.duration,
    constraint: task.constraint_start,
  };
}

// One `update_task` op with only the fields that actually changed (assignee "" when cleared),
// plus a `move_task {start_date}` if a "не раньше" constraint was set/changed, or
// `clear_constraint` if it was removed. `[]` if the form matches `baseline` exactly.
//
// Diffing against `baseline` (not the live `task`) rather than `task` directly matters once the
// modal can stay open across a server-side refetch of the same task (agent edit, another tab):
// `baseline` only moves forward for fields the user hasn't touched (see `rebaseForm`), so a
// field the agent changed elsewhere while the user is mid-edit of some *other* field doesn't
// show up as a spurious local diff that Save would silently revert. `task.id` (and any op that
// only needs the id) still comes from the live `task`. `baseline` defaults to `formFromTask(task)`
// so existing 2-arg callers keep diffing directly against the task, unchanged.
export function buildTaskOps(
  task: ScheduledTask,
  form: TaskForm,
  baseline: TaskForm = formFromTask(task),
): Operation[] {
  const ops: Operation[] = [];

  const name = form.name.trim();
  const description = form.description.trim();
  const assignee = form.assignee.trim();
  const baseName = baseline.name.trim();
  const baseDescription = baseline.description.trim();
  const baseAssignee = baseline.assignee.trim();

  const update: UpdateTaskOp = { op: "update_task", id: task.id };
  let changed = false;
  if (name !== baseName) {
    update.name = name;
    changed = true;
  }
  if (description !== baseDescription) {
    update.description = description;
    changed = true;
  }
  if (assignee !== baseAssignee) {
    update.assignee = assignee;
    changed = true;
  }
  if (form.duration !== baseline.duration) {
    update.duration = form.duration;
    changed = true;
  }
  if (changed) ops.push(update);

  if (form.constraint !== baseline.constraint) {
    if (form.constraint) {
      const move: MoveTaskOp = { op: "move_task", id: task.id, start_date: form.constraint };
      ops.push(move);
    } else {
      ops.push({ op: "clear_constraint", id: task.id });
    }
  }

  return ops;
}

// Whether a refetched `fresh` snapshot of the same task should trigger `rebaseForm`. Only fields
// the user has NOT touched (`form[key] === baseline[key]`) count: `rebaseForm` leaves a field
// the user is editing alone, so if that same field also changed server-side the baseline would
// stay "stale" forever and a render-time `setState` on it would loop until React throws
// "Too many re-renders". Such a field is deliberately ignored here; `findConflicts` is what
// surfaces that conflict to the user. The version precondition on save (`expected_version`)
// can't: the version comes from the cache, which already holds the other change.
export function needsRebase(form: TaskForm, baseline: TaskForm, fresh: TaskForm): boolean {
  return (Object.keys(fresh) as (keyof TaskForm)[]).some(
    (key) => form[key] === baseline[key] && fresh[key] !== baseline[key],
  );
}

// Carries a `baseline` (the form fields as last known from the server) forward when the same
// task refetches with new values: any field the user hasn't edited yet (`form[field] ===
// baseline[field]`) picks up the fresh server value in both `form` and `baseline`; a field the
// user has already started editing (`form[field] !== baseline[field]`) is left alone in both —
// the user's in-progress edit stays, and `baseline` keeps the *pre-edit* value so `buildTaskOps`
// still reports that field as changed.
export function rebaseForm(
  form: TaskForm,
  baseline: TaskForm,
  fresh: TaskForm,
): { form: TaskForm; baseline: TaskForm } {
  const nextForm = { ...form };
  const nextBaseline = { ...baseline };

  if (form.name === baseline.name) {
    nextForm.name = fresh.name;
    nextBaseline.name = fresh.name;
  }
  if (form.description === baseline.description) {
    nextForm.description = fresh.description;
    nextBaseline.description = fresh.description;
  }
  if (form.assignee === baseline.assignee) {
    nextForm.assignee = fresh.assignee;
    nextBaseline.assignee = fresh.assignee;
  }
  if (form.duration === baseline.duration) {
    nextForm.duration = fresh.duration;
    nextBaseline.duration = fresh.duration;
  }
  if (form.constraint === baseline.constraint) {
    nextForm.constraint = fresh.constraint;
    nextBaseline.constraint = fresh.constraint;
  }

  return { form: nextForm, baseline: nextBaseline };
}

// A field the user is editing that also changed server-side (agent, another tab) since they began:
// `was` is the value they started from (`baseline`), `now` the server's current one, `mine` theirs.
export interface FieldConflict {
  field: TaskField;
  was: FieldValue;
  now: FieldValue;
  mine: FieldValue;
}

// Text is compared trimmed, as `buildTaskOps` sends it.
function sameValue(a: FieldValue, b: FieldValue): boolean {
  return typeof a === "string" && typeof b === "string" ? a.trim() === b.trim() : a === b;
}

// Saving such a field would silently overwrite the other change, so the card asks first. A field
// the user hasn't edited (it just follows the server, see `rebaseForm`) or one the server changed
// to exactly the user's value is not a conflict.
export function findConflicts(form: TaskForm, baseline: TaskForm, fresh: TaskForm): FieldConflict[] {
  return TASK_FIELDS.filter(
    (field) =>
      !sameValue(form[field], baseline[field]) &&
      !sameValue(fresh[field], baseline[field]) &&
      !sameValue(fresh[field], form[field]),
  ).map((field) => ({ field, was: baseline[field], now: fresh[field], mine: form[field] }));
}

function copyField<K extends TaskField>(to: TaskForm, from: TaskForm, field: K) {
  to[field] = from[field];
}

// The user's answer to every current conflict. Either way `baseline` moves to the server value, so
// the conflict is gone: «Сохранить моё» ("mine") keeps the user's value in the form, which now
// diffs against the server's and is sent on save; «Взять новое» ("theirs") puts the server value
// in the form too, dropping the user's edit of that field. Other edits stay as they are.
export function resolveConflicts(
  form: TaskForm,
  baseline: TaskForm,
  fresh: TaskForm,
  keep: "mine" | "theirs",
): { form: TaskForm; baseline: TaskForm } {
  const nextForm = { ...form };
  const nextBaseline = { ...baseline };
  for (const { field } of findConflicts(form, baseline, fresh)) {
    copyField(nextBaseline, fresh, field);
    if (keep === "theirs") copyField(nextForm, fresh, field);
  }
  return { form: nextForm, baseline: nextBaseline };
}

const CONFLICT_SUBJECTS: Record<TaskField, string> = {
  name: "Название изменилось",
  description: "Описание изменилось",
  assignee: "Исполнитель изменился",
  duration: "Длительность изменилась",
  constraint: "Ограничение «Не раньше» изменилось",
};

const SHOWN_TEXT_LENGTH = 60;

function showValue(field: TaskField, value: FieldValue): string {
  if (field === "duration") return String(value);
  if (field === "constraint") return typeof value === "string" && value ? formatRu(value) : "не задано";
  const text = String(value ?? "").trim();
  if (field === "assignee") return text || "не назначен";
  if (!text) return "пусто";
  return `«${text.length > SHOWN_TEXT_LENGTH ? `${text.slice(0, SHOWN_TEXT_LENGTH)}…` : text}»`;
}

// «Длительность изменилась, пока вы редактировали: теперь 7 дн. (было 5). Ваше значение: 4.»
export function describeConflict({ field, was, now, mine }: FieldConflict): string {
  const unit = field === "duration" ? " дн." : "";
  return (
    `${CONFLICT_SUBJECTS[field]}, пока вы редактировали: теперь ${showValue(field, now)}${unit} ` +
    `(было ${showValue(field, was)}). Ваше значение: ${showValue(field, mine)}.`
  );
}

// Client-side validation shown inline in the task modal before a save is attempted.
export function validateTaskForm(form: TaskForm): string | null {
  const name = form.name.trim();
  if (name.length < 1 || name.length > 200) {
    return "Название должно быть от 1 до 200 символов";
  }
  if (form.description.trim().length > 2000) {
    return "Описание не должно превышать 2000 символов";
  }
  if (form.assignee.trim().length > 100) {
    return "Имя исполнителя не должно превышать 100 символов";
  }
  if (!Number.isInteger(form.duration) || form.duration < 1 || form.duration > 999) {
    return "Длительность должна быть целым числом от 1 до 999 дней";
  }
  return null;
}

import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, ApiError } from "@/api/client";
import type { PlanResponse, ScheduledPlan, ScheduledTask } from "@/api/types";
import { cachedPlanVersion, PLAN_KEY, refetchOnConflict } from "@/hooks/usePlan";
import { formatRu } from "@/lib/dates";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import {
  buildTaskOps,
  describeConflict,
  findConflicts,
  formFromTask,
  needsRebase,
  rebaseForm,
  resolveConflicts,
  validateTaskForm,
  type TaskForm,
} from "./taskOps";
import { TaskHistory } from "./TaskHistory";

const ASSIGNEE_DATALIST_ID = "task-modal-assignees";

function describeConstraint(task: ScheduledTask, plan: ScheduledPlan): string {
  if (task.constrained_by === "project_start") return "датой старта проекта";
  if (task.constrained_by === "constraint") return "ограничением «не раньше»";
  if (task.constrained_by.startsWith("predecessor:")) {
    const id = Number(task.constrained_by.slice("predecessor:".length));
    const predecessor = plan.tasks.find((t) => t.id === id);
    return `предшественником №${id} «${predecessor?.name ?? ""}»`;
  }
  return task.constrained_by;
}

export function TaskModal({
  task,
  plan,
  version,
  open,
  onOpenChange,
  onNavigate,
  onAddAfter,
  disabled,
}: {
  task: ScheduledTask | null;
  plan: ScheduledPlan;
  version: number;
  open: boolean;
  onOpenChange(open: boolean): void;
  onNavigate(id: number): void;
  // «Добавить после»: open the new-task form anchored on this task (see NewTaskModal).
  onAddAfter(id: number): void;
  disabled: boolean;
}) {
  const queryClient = useQueryClient();
  // `baseline` is the form as last known from the server (i.e. `formFromTask` of the task the
  // form was seeded/rebased from); `form` is what's shown in the inputs. Diffing `form` against
  // `baseline` (not the live `task` directly) in `buildTaskOps` means a server-side change to a
  // field the user hasn't touched (agent edit, another tab) never gets read as a local edit that
  // Save would silently revert. All of this resets/rebases during render, not in an effect —
  // deriving state from a changed prop belongs in the render body per React's own guidance, and
  // an effect doing the same thing would cost an extra render pass for no benefit. A server-side
  // change to a field the user IS editing isn't rebased: it's a conflict (`findConflicts`), and
  // the footer asks which value to keep instead of letting Save overwrite the other change.
  const [state, setState] = useState<{ taskId: number; form: TaskForm; baseline: TaskForm; error: string | null } | null>(
    null,
  );
  const [saving, setSaving] = useState(false);
  // «Удалить задачу» asks once more inline (the footer turns into a confirmation) before it sends
  // delete_task. Keyed by task id so it never carries over to another task's card.
  const [confirmDeleteId, setConfirmDeleteId] = useState<number | null>(null);
  // A click on the chart opens the card; the second click of a habitual double click lands on the
  // backdrop a moment later and closed it again right away. Presses outside are ignored briefly.
  const openedAt = useRef(0);
  useEffect(() => {
    if (open) openedAt.current = performance.now();
  }, [open]);

  if (task) {
    if (!state || state.taskId !== task.id) {
      const seeded = formFromTask(task);
      setState({ taskId: task.id, form: seeded, baseline: seeded, error: null });
    } else {
      const fresh = formFromTask(task);
      if (needsRebase(state.form, state.baseline, fresh)) {
        const rebased = rebaseForm(state.form, state.baseline, fresh);
        setState({ taskId: task.id, form: rebased.form, baseline: rebased.baseline, error: state.error });
      }
    }
  } else {
    // Closed (the modal stays mounted with no task): forget the form and a pending delete
    // confirmation, so reopening the same task shows its normal footer.
    if (state) setState(null);
    if (confirmDeleteId != null) setConfirmDeleteId(null);
  }

  if (!task || !state) return null;

  const { form, baseline, error } = state;
  const setForm = (form: TaskForm) => setState({ ...state, form, error: null });
  // Functional: it also runs after an awaited request and must not undo a rebase or a resolved
  // conflict that happened meanwhile.
  const setError = (error: string | null) => setState((s) => s && { ...s, error });

  const assigneeOptions = Array.from(
    new Set(plan.tasks.map((t) => t.assignee?.trim()).filter((a): a is string => Boolean(a))),
  ).sort((a, b) => a.localeCompare(b, "ru"));

  const predecessors = plan.dependencies
    .filter((d) => d.successor_id === task.id)
    .map((d) => plan.tasks.find((t) => t.id === d.predecessor_id))
    .filter((t): t is ScheduledTask => Boolean(t));
  const successors = plan.dependencies
    .filter((d) => d.predecessor_id === task.id)
    .map((d) => plan.tasks.find((t) => t.id === d.successor_id))
    .filter((t): t is ScheduledTask => Boolean(t));

  const ops = buildTaskOps(task, form, baseline);
  const canSave = !disabled && !saving && ops.length > 0;
  const confirmingDelete = confirmDeleteId === task.id;
  const fresh = formFromTask(task);
  const conflicts = findConflicts(form, baseline, fresh);

  // `base` is what the form is diffed against: «Сохранить моё» passes a baseline that has already
  // taken the server's value of the conflicting fields, so the user's value is sent over it.
  const handleSave = async (base: TaskForm = baseline) => {
    const validationError = validateTaskForm(form);
    if (validationError) {
      setError(validationError);
      return;
    }
    // `expected_version` comes from the cache, and the cache may already hold another change to
    // a field the user is editing (this render just hasn't shown it yet) — the backend would then
    // accept the save and the other change would be lost without a 409. So the fields are checked
    // against that same snapshot; on a conflict nothing is sent and the re-render asks the user.
    const cached = queryClient.getQueryData<PlanResponse>(PLAN_KEY);
    const current = cached?.plan.tasks.find((t) => t.id === task.id) ?? task;
    if (findConflicts(form, base, formFromTask(current)).length > 0) return;
    const saveOps = buildTaskOps(task, form, base);
    if (saveOps.length === 0) return;
    setSaving(true);
    setError(null);
    try {
      const res = await api.applyOps(saveOps, cached?.version);
      queryClient.setQueryData(PLAN_KEY, res);
      toast.success(res.summary);
      res.warnings.forEach((warning) => toast.warning(warning));
      onOpenChange(false);
    } catch (err) {
      // On a version conflict the refetch rebases the untouched fields; the user's own edits
      // stay in the form so they can review against the fresh values and save again (a field
      // the other change also touched shows up as a conflict).
      refetchOnConflict(queryClient, err);
      setError(err instanceof ApiError ? err.message : "Не удалось сохранить изменения");
    } finally {
      setSaving(false);
    }
  };

  // The two answers to a conflict (see `resolveConflicts`): «Сохранить моё» saves right away, now
  // knowingly over the other change; «Взять новое» only puts the new value in the form.
  const handleKeepMine = () => {
    const resolved = resolveConflicts(form, baseline, fresh, "mine");
    setState((s) => s && { ...s, baseline: resolved.baseline, error: null });
    void handleSave(resolved.baseline);
  };
  const handleTakeTheirs = () => setState({ ...state, ...resolveConflicts(form, baseline, fresh, "theirs"), error: null });

  // Its links are kept: the backend reconnects every predecessor to every successor (A→B→C
  // becomes A→C), so the rest of the chain keeps its order. Undo brings the task back.
  const handleDelete = async () => {
    setSaving(true);
    setError(null);
    try {
      const res = await api.applyOps([{ op: "delete_task", id: task.id }], cachedPlanVersion(queryClient));
      queryClient.setQueryData(PLAN_KEY, res);
      toast.success(res.summary);
      onOpenChange(false);
    } catch (err) {
      refetchOnConflict(queryClient, err);
      setError(err instanceof ApiError ? err.message : "Не удалось удалить задачу");
    } finally {
      setSaving(false);
      setConfirmDeleteId(null);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        onPointerDownOutside={(e) => {
          if (performance.now() - openedAt.current < 500) e.preventDefault();
        }}
      >
        <DialogTitle>
          №{task.id} «{task.name}»
        </DialogTitle>
        <DialogDescription className="sr-only">Редактирование задачи</DialogDescription>

        <div className="mt-2 flex flex-col gap-3">
          {task.is_critical && (
            <span className="inline-flex w-fit items-center rounded-full bg-destructive/10 px-2 py-0.5 text-xs font-medium text-destructive">
              Критическая задача
            </span>
          )}

          <label className="flex flex-col gap-1 text-sm">
            Название
            <input
              className="rounded-md border border-input bg-background px-2 py-1"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
          </label>

          <label className="flex flex-col gap-1 text-sm">
            Описание
            <textarea
              className="rounded-md border border-input bg-background px-2 py-1"
              rows={2}
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
            />
          </label>

          <div className="flex gap-3">
            <label className="flex flex-1 flex-col gap-1 text-sm">
              Исполнитель
              <input
                className="rounded-md border border-input bg-background px-2 py-1"
                value={form.assignee}
                list={ASSIGNEE_DATALIST_ID}
                onChange={(e) => setForm({ ...form, assignee: e.target.value })}
              />
              <datalist id={ASSIGNEE_DATALIST_ID}>
                {assigneeOptions.map((name) => (
                  <option key={name} value={name} />
                ))}
              </datalist>
            </label>
            <label className="flex w-28 flex-col gap-1 text-sm">
              Длительность, дн.
              <input
                type="number"
                min={1}
                max={999}
                className="rounded-md border border-input bg-background px-2 py-1"
                value={form.duration}
                onChange={(e) => setForm({ ...form, duration: Number(e.target.value) })}
              />
            </label>
          </div>

          <label className="flex flex-col gap-1 text-sm">
            Не раньше
            <div className="flex items-center gap-2">
              <input
                type="date"
                className="rounded-md border border-input bg-background px-2 py-1"
                value={form.constraint ?? ""}
                onChange={(e) => setForm({ ...form, constraint: e.target.value || null })}
              />
              {form.constraint && (
                <button
                  type="button"
                  className="text-xs text-muted-foreground hover:text-foreground"
                  onClick={() => setForm({ ...form, constraint: null })}
                >
                  Убрать
                </button>
              )}
            </div>
          </label>

          <div className="rounded-md bg-muted/50 p-2 text-sm text-muted-foreground">
            <p>Начало: {formatRu(task.start)}</p>
            <p>Окончание: {formatRu(task.end)}</p>
            <p>Резерв {task.slack} дн.</p>
            <p>Начало определяется: {describeConstraint(task, plan)}</p>
          </div>

          {(predecessors.length > 0 || successors.length > 0) && (
            <div className="flex flex-col gap-1.5 text-sm">
              {predecessors.length > 0 && (
                <div>
                  <span className="text-muted-foreground">Предшественники: </span>
                  {predecessors.map((p) => (
                    <button
                      key={p.id}
                      type="button"
                      className="mr-2 underline hover:text-foreground"
                      onClick={() => onNavigate(p.id)}
                    >
                      №{p.id} «{p.name}»
                    </button>
                  ))}
                </div>
              )}
              {successors.length > 0 && (
                <div>
                  <span className="text-muted-foreground">Последователи: </span>
                  {successors.map((s) => (
                    <button
                      key={s.id}
                      type="button"
                      className="mr-2 underline hover:text-foreground"
                      onClick={() => onNavigate(s.id)}
                    >
                      №{s.id} «{s.name}»
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}

          {task.overallocated_with.length > 0 && (
            <p className="text-sm text-amber-600">
              Пересекается по исполнителю с задачами: {task.overallocated_with.map((id) => `№${id}`).join(", ")}
            </p>
          )}

          <div className="border-t border-border pt-3">
            <TaskHistory taskId={task.id} version={version} />
          </div>

          {error && <p className="text-sm text-destructive">{error}</p>}

          {confirmingDelete ? (
            <div className="mt-2 flex flex-col gap-2 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm">
              <p>
                Удалить задачу №{task.id} «{task.name}»?
                {predecessors.length > 0 && successors.length > 0 &&
                  " Её предшественники станут предшественниками её последователей."}{" "}
                Вернуть можно кнопкой «Отменить».
              </p>
              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  className="rounded-md border border-input px-3 py-1.5 hover:bg-accent"
                  onClick={() => setConfirmDeleteId(null)}
                >
                  Не удалять
                </button>
                <button
                  type="button"
                  className="rounded-md bg-destructive px-3 py-1.5 font-medium text-primary-foreground disabled:opacity-50"
                  disabled={disabled || saving}
                  onClick={() => void handleDelete()}
                >
                  Удалить
                </button>
              </div>
            </div>
          ) : conflicts.length > 0 ? (
            // Someone else changed a field the user is editing: the footer asks, like «Удалить
            // задачу» does, and Save is out of reach until the user picks a value.
            <div
              role="alert"
              className="mt-2 flex flex-col gap-2 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-amber-200"
            >
              {conflicts.map((conflict) => (
                <p key={conflict.field}>{describeConflict(conflict)}</p>
              ))}
              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  className="rounded-md border border-input px-3 py-1.5 hover:bg-accent"
                  onClick={handleTakeTheirs}
                >
                  Взять новое
                </button>
                <button
                  type="button"
                  className="rounded-md bg-primary px-3 py-1.5 font-medium text-primary-foreground disabled:opacity-50"
                  disabled={disabled || saving}
                  onClick={handleKeepMine}
                >
                  Сохранить моё
                </button>
              </div>
            </div>
          ) : (
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <button
                type="button"
                className="rounded-md px-2 py-1.5 text-sm text-destructive hover:bg-destructive/10 disabled:opacity-50"
                disabled={disabled || saving}
                onClick={() => setConfirmDeleteId(task.id)}
              >
                Удалить задачу
              </button>
              <button
                type="button"
                className="rounded-md px-2 py-1.5 text-sm hover:bg-accent disabled:opacity-50"
                disabled={disabled}
                onClick={() => onAddAfter(task.id)}
              >
                Добавить после
              </button>
              <div className="ml-auto flex gap-2">
                <button
                  type="button"
                  className="rounded-md border border-input px-3 py-1.5 text-sm hover:bg-accent"
                  onClick={() => onOpenChange(false)}
                >
                  Отмена
                </button>
                <button
                  type="button"
                  className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50"
                  disabled={!canSave}
                  onClick={() => void handleSave()}
                >
                  Сохранить
                </button>
              </div>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

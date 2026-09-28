import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, ApiError } from "@/api/client";
import type { ScheduledPlan } from "@/api/types";
import { cachedPlanVersion, PLAN_KEY, refetchOnConflict } from "@/hooks/usePlan";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { buildNewTaskOps, emptyNewTaskForm, validateNewTaskForm, type NewTaskForm } from "./newTaskOps";

const ASSIGNEE_DATALIST_ID = "new-task-assignees";
const INPUT = "rounded-md border border-input bg-background px-2 py-1";

// Creates a task. Opened from the toolbar (`anchorId` null: end of the list, no links) or from a
// task's card via «Добавить после» (right after that task, starting when it ends). Mounted only
// while open, so every opening starts from a fresh form.
export function NewTaskModal({
  plan,
  anchorId,
  onClose,
  disabled,
}: {
  plan: ScheduledPlan;
  anchorId: number | null;
  onClose(): void;
  disabled: boolean;
}) {
  const queryClient = useQueryClient();
  const [form, setFormState] = useState<NewTaskForm>(() => emptyNewTaskForm(anchorId));
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const setForm = (next: NewTaskForm) => {
    setFormState(next);
    setError(null);
  };

  const assigneeOptions = Array.from(
    new Set(plan.tasks.map((t) => t.assignee?.trim()).filter((a): a is string => Boolean(a))),
  ).sort((a, b) => a.localeCompare(b, "ru"));
  const label = (id: number) => `№${id} «${plan.tasks.find((t) => t.id === id)?.name ?? ""}»`;
  const taskOptions = plan.tasks.map((t) => (
    <option key={t.id} value={t.id}>
      {label(t.id)}
    </option>
  ));
  const toId = (value: string) => (value === "" ? null : Number(value));
  const replacedLink =
    form.predecessorId != null &&
    form.successorId != null &&
    plan.dependencies.some((d) => d.predecessor_id === form.predecessorId && d.successor_id === form.successorId);

  const handleCreate = async () => {
    const validationError = validateNewTaskForm(plan, form);
    if (validationError) {
      setError(validationError);
      return;
    }
    setSaving(true);
    try {
      const res = await api.applyOps(buildNewTaskOps(plan, form), cachedPlanVersion(queryClient));
      queryClient.setQueryData(PLAN_KEY, res);
      toast.success(res.summary);
      res.warnings.forEach((warning) => toast.warning(warning));
      onClose();
    } catch (err) {
      refetchOnConflict(queryClient, err);
      setError(err instanceof ApiError ? err.message : "Не удалось добавить задачу");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogTitle>Новая задача</DialogTitle>
        <DialogDescription className="sr-only">Добавление задачи в план</DialogDescription>

        <div className="mt-2 flex flex-col gap-3">
          <label className="flex flex-col gap-1 text-sm">
            Название
            <input
              autoFocus
              className={INPUT}
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
          </label>

          <label className="flex flex-col gap-1 text-sm">
            Описание
            <textarea
              className={INPUT}
              rows={2}
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
            />
          </label>

          <div className="flex gap-3">
            <label className="flex flex-1 flex-col gap-1 text-sm">
              Исполнитель
              <input
                className={INPUT}
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
                className={INPUT}
                value={form.duration}
                onChange={(e) => setForm({ ...form, duration: Number(e.target.value) })}
              />
            </label>
          </div>

          <label className="flex flex-col gap-1 text-sm">
            Начать после задачи
            <select
              className={INPUT}
              value={form.predecessorId ?? ""}
              onChange={(e) => setForm({ ...form, predecessorId: toId(e.target.value) })}
            >
              <option value="">— не зависит от других задач —</option>
              {taskOptions}
            </select>
          </label>

          <label className="flex flex-col gap-1 text-sm">
            Перед задачей
            <select
              className={INPUT}
              value={form.successorId ?? ""}
              onChange={(e) => setForm({ ...form, successorId: toId(e.target.value) })}
            >
              <option value="">— ни одна задача её не ждёт —</option>
              {taskOptions}
            </select>
            <span className="text-xs text-muted-foreground">
              {replacedLink
                ? `Встанет между ними: связь ${label(form.predecessorId!)} → №${form.successorId} заменится цепочкой через новую задачу.`
                : "Выбранная задача начнётся только после окончания новой."}
            </span>
          </label>

          <div className="flex flex-wrap gap-3">
            <label className="flex min-w-48 flex-1 flex-col gap-1 text-sm">
              Место в списке
              <select
                className={INPUT}
                value={form.afterId ?? ""}
                onChange={(e) => setForm({ ...form, afterId: toId(e.target.value) })}
              >
                <option value="">В конец списка</option>
                {plan.tasks.map((t) => (
                  <option key={t.id} value={t.id}>
                    После {label(t.id)}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-sm">
              Не раньше
              <input
                type="date"
                className={INPUT}
                value={form.constraint ?? ""}
                onChange={(e) => setForm({ ...form, constraint: e.target.value || null })}
              />
            </label>
          </div>

          {error && <p className="text-sm text-destructive">{error}</p>}

          <div className="mt-2 flex justify-end gap-2">
            <button
              type="button"
              className="rounded-md border border-input px-3 py-1.5 text-sm hover:bg-accent"
              onClick={onClose}
            >
              Отмена
            </button>
            <button
              type="button"
              className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50"
              disabled={disabled || saving}
              onClick={() => void handleCreate()}
            >
              Добавить
            </button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

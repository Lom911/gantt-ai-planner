import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, ApiError } from "@/api/client";
import type { ImportIssue } from "@/api/types";
import { PLAN_KEY } from "@/hooks/usePlan";
import { CHAT_HISTORY_KEY } from "@/hooks/useChat";
import { nextMonday, toISODate } from "@/lib/dates";
import { uploadSizeError } from "@/lib/upload";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";

function issueText(issue: ImportIssue): string {
  return issue.row != null ? `Строка ${issue.row}: ${issue.message}` : issue.message;
}

export function ImportDialog({ open, onOpenChange }: { open: boolean; onOpenChange(open: boolean): void }) {
  const queryClient = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [projectStart, setProjectStart] = useState(() => toISODate(nextMonday(new Date())));
  const [errors, setErrors] = useState<ImportIssue[]>([]);
  const [warnings, setWarnings] = useState<ImportIssue[]>([]);
  const [generalError, setGeneralError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  // Re-derive the default date every time the dialog transitions closed -> open (not just once
  // at mount) — done during render (tracking the previous `open` value) rather than in an
  // effect, per the same "adjust state during render" pattern used elsewhere in this codebase.
  const [wasOpen, setWasOpen] = useState(open);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) setProjectStart(toISODate(nextMonday(new Date())));
  }

  const reset = () => {
    setFile(null);
    setErrors([]);
    setWarnings([]);
    setGeneralError(null);
  };

  const handleOpenChange = (next: boolean) => {
    if (!next) reset();
    onOpenChange(next);
  };

  const handleSubmit = async () => {
    if (!file || loading) return;
    setLoading(true);
    setGeneralError(null);
    try {
      const result = await api.importPlan(file, projectStart);
      if (!result.ok) {
        setErrors(result.errors);
        setWarnings(result.warnings);
        return;
      }
      queryClient.setQueryData(PLAN_KEY, result.plan);
      void queryClient.invalidateQueries({ queryKey: CHAT_HISTORY_KEY });
      const count = result.plan.plan.tasks.length;
      toast.success(
        result.warnings.length > 0
          ? `Загружено задач: ${count} (предупреждений: ${result.warnings.length})`
          : `Загружено задач: ${count}`,
      );
      reset();
      onOpenChange(false);
    } catch (err) {
      setGeneralError(err instanceof ApiError ? err.message : "Не удалось загрузить файл");
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent>
        <DialogTitle>Импорт плана из Excel</DialogTitle>
        <DialogDescription>Загрузите файл .xlsx и укажите дату старта проекта.</DialogDescription>

        <div className="mt-3 flex flex-col gap-3">
          <label className="flex flex-col gap-1 text-sm">
            Файл (.xlsx)
            <input
              type="file"
              accept=".xlsx"
              onChange={(e) => {
                const picked = e.target.files?.[0] ?? null;
                setFile(picked);
                setErrors([]);
                setWarnings([]);
                setGeneralError(picked ? uploadSizeError(picked.size) : null);
              }}
              className="rounded-md border border-input bg-background px-2 py-1 text-sm"
            />
          </label>

          <label className="flex flex-col gap-1 text-sm">
            Дата старта проекта
            <input
              type="date"
              value={projectStart}
              onChange={(e) => setProjectStart(e.target.value)}
              className="rounded-md border border-input bg-background px-2 py-1"
            />
          </label>

          {generalError && <p className="text-sm text-destructive">{generalError}</p>}

          {(errors.length > 0 || warnings.length > 0) && (
            <ul className="max-h-40 overflow-y-auto rounded-md border border-border p-2 text-sm">
              {errors.map((issue, i) => (
                <li key={`e-${i}`} className="text-destructive">
                  {issueText(issue)}
                </li>
              ))}
              {warnings.map((issue, i) => (
                <li key={`w-${i}`} className="text-amber-600">
                  {issueText(issue)}
                </li>
              ))}
            </ul>
          )}

          <div className="mt-1 flex justify-end gap-2">
            <button
              type="button"
              className="rounded-md border border-input px-3 py-1.5 text-sm hover:bg-accent"
              onClick={() => handleOpenChange(false)}
            >
              Отмена
            </button>
            <button
              type="button"
              className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50"
              disabled={!file || loading || (file != null && uploadSizeError(file.size) != null)}
              onClick={() => void handleSubmit()}
            >
              Загрузить
            </button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

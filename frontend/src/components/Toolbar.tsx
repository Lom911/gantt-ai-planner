import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Download, Link2, Monitor, Moon, MoreVertical, Plus, Redo2, RotateCcw, Sun, Undo2, Upload } from "lucide-react";
import { api, ApiError, exportUrl } from "@/api/client";
import type { PlanResponse } from "@/api/types";
import { cachedPlanVersion, PLAN_KEY, refetchOnConflict } from "@/hooks/usePlan";
import { useMeta } from "@/hooks/useMeta";
import type { ThemeMode } from "@/hooks/useTheme";
import { deleteMyData } from "@/lib/deleteMyData";
import { cn } from "@/lib/utils";
import { clearLayoutPrefs } from "@/lib/layoutPrefs";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { McpConnectDialog } from "@/components/McpConnectDialog";
import { taskLimitNotice } from "@/components/task/newTaskOps";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import type { Zoom } from "@/components/gantt/mapping";

const ZOOM_LABELS: Record<Zoom, string> = { day: "День", week: "Неделя", month: "Месяц" };
const ZOOM_ORDER: Zoom[] = ["day", "week", "month"];

const THEME_OPTIONS: { value: ThemeMode; label: string; icon: typeof Sun }[] = [
  { value: "light", label: "Светлая", icon: Sun },
  { value: "dark", label: "Тёмная", icon: Moon },
  { value: "system", label: "Как в системе", icon: Monitor },
];

export function Toolbar({
  plan,
  agentBusy,
  zoom,
  onZoom,
  onToday,
  onImport,
  onAddTask,
  theme,
  onTheme,
}: {
  plan: PlanResponse;
  agentBusy: boolean;
  zoom: Zoom;
  onZoom(zoom: Zoom): void;
  onToday(): void;
  onImport(): void;
  onAddTask(): void;
  theme: ThemeMode;
  onTheme(mode: ThemeMode): void;
}) {
  const queryClient = useQueryClient();
  const { data: meta } = useMeta();
  const [resetOpen, setResetOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [mcpOpen, setMcpOpen] = useState(false);
  const [pending, setPending] = useState(false);

  // Undo/redo carry the version this tab is showing: undoing on top of a plan another tab (or
  // the agent) has already moved would silently revert *their* change, so the backend refuses.
  const runPlanAction = async (
    action: (expectedVersion?: number) => Promise<PlanResponse>,
    errorMessage: string,
  ) => {
    setPending(true);
    try {
      const res = await action(cachedPlanVersion(queryClient));
      queryClient.setQueryData(PLAN_KEY, res);
    } catch (err) {
      refetchOnConflict(queryClient, err);
      toast.error(err instanceof ApiError ? err.message : errorMessage);
    } finally {
      setPending(false);
    }
  };

  const busy = agentBusy || pending;
  // At the plan's task limit the form would only end in the server's refusal.
  const limitNotice = taskLimitNotice(plan.plan, meta?.max_tasks);

  return (
    <div className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2">
      <button
        type="button"
        title={limitNotice ?? "Добавить задачу"}
        aria-label="Добавить задачу"
        disabled={busy || limitNotice != null}
        className="inline-flex items-center gap-1.5 rounded-md bg-primary px-2 py-1.5 text-sm font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50 sm:px-3"
        onClick={onAddTask}
      >
        <Plus className="h-4 w-4" /> <span className="hidden sm:inline">Задача</span>
      </button>

      <button
        type="button"
        title="Загрузить Excel"
        aria-label="Загрузить Excel"
        className="inline-flex items-center gap-1.5 rounded-md border border-input px-2 py-1.5 text-sm hover:bg-accent sm:px-3"
        onClick={onImport}
      >
        <Download className="h-4 w-4" /> <span className="hidden sm:inline">Загрузить Excel</span>
      </button>

      <a
        href={exportUrl()}
        // Recomputed on click too, in case the page has stayed open past midnight.
        onClick={(e) => {
          e.currentTarget.href = exportUrl();
        }}
        download
        title="Экспорт"
        aria-label="Экспорт"
        className="inline-flex items-center gap-1.5 rounded-md border border-input px-2 py-1.5 text-sm hover:bg-accent sm:px-3"
      >
        <Upload className="h-4 w-4" /> <span className="hidden sm:inline">Экспорт</span>
      </a>

      <div className="mx-1 h-5 w-px bg-border" />

      <button
        type="button"
        title="Отменить"
        aria-label="Отменить"
        disabled={!plan.can_undo || busy}
        className="rounded-md border border-input p-1.5 hover:bg-accent disabled:opacity-40"
        onClick={() => void runPlanAction(api.undo, "Не удалось отменить действие")}
      >
        <Undo2 className="h-4 w-4" />
      </button>
      <button
        type="button"
        title="Повторить"
        aria-label="Повторить"
        disabled={!plan.can_redo || busy}
        className="rounded-md border border-input p-1.5 hover:bg-accent disabled:opacity-40"
        onClick={() => void runPlanAction(api.redo, "Не удалось повторить действие")}
      >
        <Redo2 className="h-4 w-4" />
      </button>

      <div className="mx-1 h-5 w-px bg-border" />

      <div className="inline-flex rounded-md border border-input p-0.5">
        {ZOOM_ORDER.map((z) => (
          <button
            key={z}
            type="button"
            className={cn(
              "rounded-sm px-2.5 py-1 text-sm",
              zoom === z ? "bg-primary text-primary-foreground" : "hover:bg-accent",
            )}
            onClick={() => onZoom(z)}
          >
            {ZOOM_LABELS[z]}
          </button>
        ))}
      </div>
      {/* Colored like the chart's today column, which it scrolls to the middle of the chart. */}
      <button
        type="button"
        title="Показать сегодняшний день в середине диаграммы"
        className="rounded-md border border-[var(--gantt-today-accent)] bg-[var(--gantt-today)] px-2.5 py-1 text-sm font-medium text-[var(--gantt-today-accent)] hover:bg-[var(--gantt-changed-bg)]"
        onClick={onToday}
      >
        Сегодня
      </button>

      <div className="ml-auto flex items-center gap-2">
        {meta?.llm_mode === "fake" && (
          <span
            title="Ключ Anthropic не задан: чат понимает только примеры команд"
            className="rounded-full bg-amber-100 px-2.5 py-1 text-xs font-medium text-amber-800 dark:bg-amber-500/20 dark:text-amber-300"
          >
            Демо-режим без LLM
          </span>
        )}

        {agentBusy && (
          <span className="rounded-full bg-amber-100 px-2.5 py-1 text-xs font-medium text-amber-800 dark:bg-amber-500/20 dark:text-amber-300">
            Агент редактирует план…
          </span>
        )}

        <button
          type="button"
          title="Подключить MCP"
          aria-label="Подключить MCP"
          className="inline-flex items-center gap-1.5 rounded-md border border-input px-2 py-1.5 text-sm hover:bg-accent sm:px-3"
          onClick={() => setMcpOpen(true)}
        >
          <Link2 className="h-4 w-4" /> <span className="hidden sm:inline">Подключить MCP</span>
        </button>

        <button
          type="button"
          title="Сбросить к демо"
          aria-label="Сбросить к демо"
          className="inline-flex items-center gap-1.5 rounded-md border border-input px-2 py-1.5 text-sm hover:bg-accent disabled:opacity-40 sm:px-3"
          disabled={busy}
          onClick={() => setResetOpen(true)}
        >
          <RotateCcw className="h-4 w-4" /> <span className="hidden sm:inline">Сбросить к демо</span>
        </button>

        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button type="button" aria-label="Ещё" className="rounded-md border border-input p-1.5 hover:bg-accent">
              <MoreVertical className="h-4 w-4" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent>
            <DropdownMenuLabel>Тема</DropdownMenuLabel>
            <DropdownMenuRadioGroup value={theme} onValueChange={(value) => onTheme(value as ThemeMode)}>
              {THEME_OPTIONS.map(({ value, label, icon: Icon }) => (
                <DropdownMenuRadioItem key={value} value={value}>
                  <Icon className="h-4 w-4" /> {label}
                </DropdownMenuRadioItem>
              ))}
            </DropdownMenuRadioGroup>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              onSelect={() => {
                clearLayoutPrefs();
                location.reload();
              }}
            >
              Сбросить раскладку
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => setDeleteOpen(true)}>Удалить мои данные</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      <ConfirmDialog
        open={resetOpen}
        onOpenChange={setResetOpen}
        title="Сбросить план"
        description="Текущий план будет заменён демо-планом. Действие можно отменить."
        confirmLabel="Сбросить"
        onConfirm={() => void runPlanAction(api.reset, "Не удалось сбросить план")}
      />
      <McpConnectDialog open={mcpOpen} onOpenChange={setMcpOpen} />
      <ConfirmDialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title="Удалить мои данные"
        description="Все данные текущей сессии будут удалены без возможности восстановления."
        confirmLabel="Удалить"
        destructive
        onConfirm={() => {
          void deleteMyData(api.deleteSession, () => location.reload(), (message) => toast.error(message));
        }}
      />
    </div>
  );
}

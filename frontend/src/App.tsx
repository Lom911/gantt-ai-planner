import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, ApiError } from "@/api/client";
import type { Operation } from "@/api/types";
import { cachedPlanVersion, PLAN_KEY, refetchOnConflict, usePlan } from "@/hooks/usePlan";
import { useFlashHighlight } from "@/hooks/useFlashHighlight";
import { useSessionEvents } from "@/hooks/useSessionEvents";
import { useTheme } from "@/hooks/useTheme";
import { SplitLayout } from "@/components/SplitLayout";
import { GanttView } from "@/components/gantt/GanttView";
import { GanttLegend } from "@/components/gantt/GanttLegend";
import { ConfirmationBanner } from "@/components/ConfirmationBanner";
import { useConfirmation } from "@/hooks/useConfirmation";
import { loadLayoutPrefs, saveLayoutPrefs } from "@/lib/layoutPrefs";
import { ProjectDates } from "@/components/gantt/ProjectDates";
import { ChatPanel } from "@/components/chat/ChatPanel";
import { Toolbar } from "@/components/Toolbar";
import { TaskModal } from "@/components/task/TaskModal";
import { NewTaskModal } from "@/components/task/NewTaskModal";
import { ImportDialog } from "@/components/import/ImportDialog";
import { ResourcePanel } from "@/components/ResourcePanel";
import type { Zoom } from "@/components/gantt/mapping";

function App() {
  const queryClient = useQueryClient();
  const { data, isLoading, isError, error } = usePlan();
  const { theme, isDark, setTheme } = useTheme();
  const [zoom, setZoom] = useState<Zoom>(() => loadLayoutPrefs().zoom ?? "day");
  const changeZoom = (z: Zoom) => {
    setZoom(z);
    saveLayoutPrefs({ zoom: z });
  };
  const [focusedIds, flashFocused] = useFlashHighlight();
  const [openTaskId, setOpenTaskId] = useState<number | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  // The new-task form, when open: `anchorId` is the task it was opened after (its card's
  // «Добавить после»), null from the toolbar.
  const [newTask, setNewTask] = useState<{ anchorId: number | null } | null>(null);

  // Every plan change — from this tab, the agent, another tab, or an undo — arrives here via the
  // session-wide event bus, so this is the single source of highlighted ids (a click in the
  // chat's diff summary sets it too, via `onFocusTask` below). The highlight clears after
  // HIGHLIGHT_MS; imports/resets report no ids (see parsePlanChanged).
  const { data: pendingConfirmation } = useConfirmation();
  const { agentBusy } = useSessionEvents((ids) => {
    if (ids.length) flashFocused(ids);
  });

  const openTask = data?.plan.tasks.find((t) => t.id === openTaskId) ?? null;

  // Drag/resize/link edits from the Gantt chart itself (TaskModal has its own copy of this same
  // apply-and-refresh flow). The backend is the source of truth: a successful apply replaces the
  // cached plan outright (like TaskModal's `setQueryData`) so every consumer — the chart, the
  // toolbar's undo/redo, the task modal — re-renders from it; a failed apply is rethrown after
  // toasting so GanttView knows to snap the bar/link back to the last known-good plan.
  const onApplyPlanOps = async (ops: Operation[]) => {
    try {
      const res = await api.applyOps(ops, cachedPlanVersion(queryClient));
      queryClient.setQueryData(PLAN_KEY, res);
      res.warnings.forEach((warning) => toast.warning(warning));
    } catch (err) {
      refetchOnConflict(queryClient, err);
      toast.error(err instanceof ApiError ? err.message : "Не удалось применить изменение");
      throw err;
    }
  };

  // The open task can vanish server-side while the modal is up (agent/another tab deletes it).
  // A genuine side effect (closing the modal, toasting) belongs in an effect, unlike deriving
  // form state from a prop — that's why this isn't handled inline during render like `openTask`.
  useEffect(() => {
    if (!data || openTaskId == null) return;
    const stillExists = data.plan.tasks.some((t) => t.id === openTaskId);
    if (!stillExists) {
      setOpenTaskId(null);
      toast("Задача удалена");
    }
  }, [data, openTaskId]);

  return (
    <div className="flex h-screen min-h-0 flex-col">
      <header className="flex items-center border-b border-border px-4 py-3">
        <h1 className="text-lg font-semibold">Gantt AI Planner</h1>
      </header>
      {data && (
        <Toolbar
          plan={data}
          agentBusy={agentBusy}
          zoom={zoom}
          onZoom={changeZoom}
          onImport={() => setImportOpen(true)}
          onAddTask={() => setNewTask({ anchorId: null })}
          theme={theme}
          onTheme={setTheme}
        />
      )}
      <ConfirmationBanner pending={pendingConfirmation} />
      <main className="min-h-0 flex-1">
        {isLoading && <div className="p-4 text-muted-foreground">Загрузка плана…</div>}
        {isError && (
          <div className="p-4 text-destructive">
            Не удалось загрузить план: {error instanceof Error ? error.message : "неизвестная ошибка"}
          </div>
        )}
        {data && (
          <SplitLayout
            left={
              <div className="flex h-full min-h-0 flex-col">
                <ProjectDates start={data.plan.project_start} end={data.plan.project_end} />
                <div className="min-h-0 flex-1 overflow-hidden">
                  <GanttView
                    plan={data.plan}
                    zoom={zoom}
                    highlighted={focusedIds}
                    readOnly={agentBusy}
                    dark={isDark}
                    onOpenTask={(id) => setOpenTaskId(id)}
                    onApply={onApplyPlanOps}
                  />
                </div>
                <GanttLegend />
                <ResourcePanel plan={data.plan} onOpenTask={(id) => setOpenTaskId(id)} />
              </div>
            }
            right={
              <ChatPanel onFocusTask={(id) => flashFocused([id])} agentBusy={agentBusy} />
            }
            chatBusy={agentBusy}
          />
        )}
      </main>

      {data && (
        <TaskModal
          task={openTask}
          plan={data.plan}
          version={data.version}
          open={openTaskId != null}
          onOpenChange={(open) => {
            if (!open) setOpenTaskId(null);
          }}
          onNavigate={(id) => setOpenTaskId(id)}
          onAddAfter={(id) => {
            setOpenTaskId(null);
            setNewTask({ anchorId: id });
          }}
          disabled={agentBusy}
        />
      )}
      {data && newTask && (
        <NewTaskModal
          plan={data.plan}
          anchorId={newTask.anchorId}
          onClose={() => setNewTask(null)}
          disabled={agentBusy}
        />
      )}
      <ImportDialog open={importOpen} onOpenChange={setImportOpen} />
    </div>
  );
}

export default App;

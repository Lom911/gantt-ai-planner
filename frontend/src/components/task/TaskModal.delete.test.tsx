import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { toast } from "sonner";
import { api } from "@/api/client";
import type { ApplyResponse, ScheduledPlan, ScheduledTask } from "@/api/types";
import { TaskModal } from "./TaskModal";

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client");
  // The card reads the plan's task limit from GET /api/meta («Добавить после»).
  const meta = vi.fn().mockResolvedValue({ llm_mode: "fake", model: "fake", max_tasks: 500 });
  return { ...actual, api: { applyOps: vi.fn(), taskHistory: vi.fn(), meta } };
});
vi.mock("sonner", () => ({ toast: { success: vi.fn(), warning: vi.fn(), error: vi.fn() } }));

function task(id: number): ScheduledTask {
  return {
    id,
    name: `Задача ${id}`,
    description: "",
    assignee: null,
    duration: 1,
    constraint_start: null,
    start: "2026-09-21",
    end: "2026-09-21",
    slack: 0,
    is_critical: false,
    constrained_by: "project_start",
    overallocated_with: [],
  };
}

// №1 → №2 → №3, each link with the maximum lag: deleting №2 bridges №1 → №3 with a clamped lag.
const plan: ScheduledPlan = {
  project_start: "2026-09-21",
  project_end: "2029-02-01",
  last_id: 3,
  tasks: [task(1), task(2), task(3)],
  dependencies: [
    { predecessor_id: 1, successor_id: 2, lag: 365 },
    { predecessor_id: 2, successor_id: 3, lag: 365 },
  ],
  critical_path: [1, 2, 3],
};

const warning = "Задержка связи №1 → №3 после удаления №2 обрезана до 365 дней (было бы 730)";
const response: ApplyResponse = {
  version: 2,
  plan: { ...plan, tasks: [task(1), task(3)], dependencies: [{ predecessor_id: 1, successor_id: 3, lag: 365 }] },
  can_undo: true,
  can_redo: false,
  agent_busy: false,
  changes: [],
  warnings: [warning],
  summary: "Удалено задач: 1",
  created_task_ids: [],
};

test("deleting a task shows the backend's warnings, not just the summary", async () => {
  vi.mocked(api.taskHistory).mockResolvedValue([]);
  vi.mocked(api.applyOps).mockResolvedValue(response);
  const onOpenChange = vi.fn();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <TaskModal
        task={plan.tasks[1]}
        plan={plan}
        version={1}
        open
        onOpenChange={onOpenChange}
        onNavigate={vi.fn()}
        onAddAfter={vi.fn()}
        disabled={false}
      />
    </QueryClientProvider>,
  );

  fireEvent.click(screen.getByRole("button", { name: "Удалить задачу" }));
  fireEvent.click(screen.getByRole("button", { name: "Удалить" }));

  await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
  expect(api.applyOps).toHaveBeenCalledWith([{ op: "delete_task", id: 2 }], undefined);
  expect(toast.success).toHaveBeenCalledWith("Удалено задач: 1");
  expect(toast.warning).toHaveBeenCalledWith(warning);
});

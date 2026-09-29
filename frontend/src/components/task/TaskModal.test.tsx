import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { api } from "@/api/client";
import type { ScheduledPlan, ScheduledTask } from "@/api/types";
import { META_KEY } from "@/hooks/useMeta";
import { TaskModal } from "./TaskModal";

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return { ...actual, api: { meta: vi.fn(), taskHistory: vi.fn(), applyOps: vi.fn() } };
});

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

// GET /api/meta is seeded into the cache as already loaded (useMeta never refetches it).
function renderCard(taskCount: number, maxTasks: number) {
  vi.mocked(api.taskHistory).mockResolvedValue([]);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(META_KEY, { llm_mode: "anthropic", model: "claude", max_tasks: maxTasks });
  const plan: ScheduledPlan = {
    project_start: "2026-09-07",
    project_end: "2026-09-30",
    last_id: taskCount,
    tasks: Array.from({ length: taskCount }, (_, i) => task(i + 1)),
    dependencies: [],
    critical_path: [],
  };
  render(
    <QueryClientProvider client={client}>
      <TaskModal
        task={plan.tasks[0]}
        plan={plan}
        version={1}
        open
        onOpenChange={() => {}}
        onNavigate={() => {}}
        onAddAfter={() => {}}
        disabled={false}
      />
    </QueryClientProvider>,
  );
  return screen.getByRole("button", { name: "Добавить после" });
}

test("at the task limit «Добавить после» is disabled and its tooltip says why", async () => {
  const addAfter = renderCard(3, 3);
  expect(addAfter).toBeDisabled();
  expect(addAfter).toHaveAttribute("title", "В плане уже 3 задачи — это предел");
  await screen.findByText("Изменений пока не было"); // let the card's history settle
});

test("below the limit «Добавить после» stays available", async () => {
  const addAfter = renderCard(2, 3);
  expect(addAfter).toBeEnabled();
  expect(addAfter).not.toHaveAttribute("title");
  await screen.findByText("Изменений пока не было");
});

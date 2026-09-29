import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { PlanResponse, ScheduledTask } from "@/api/types";
import { META_KEY } from "@/hooks/useMeta";
import { Toolbar } from "./Toolbar";

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return { ...actual, api: { meta: vi.fn() } };
});

const task = (id: number) => ({ id, name: `Задача ${id}` }) as ScheduledTask;

// GET /api/meta is seeded into the cache as already loaded (useMeta never refetches it).
function renderToolbar(taskCount: number, maxTasks: number) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(META_KEY, { llm_mode: "anthropic", model: "claude", max_tasks: maxTasks });
  const plan: PlanResponse = {
    version: 1,
    plan: {
      project_start: "2026-09-07",
      project_end: "2026-09-30",
      last_id: taskCount,
      tasks: Array.from({ length: taskCount }, (_, i) => task(i + 1)),
      dependencies: [],
      critical_path: [],
    },
    can_undo: false,
    can_redo: false,
    agent_busy: false,
  };
  render(
    <QueryClientProvider client={client}>
      <Toolbar
        plan={plan}
        agentBusy={false}
        zoom="day"
        onZoom={() => {}}
        onToday={() => {}}
        onImport={() => {}}
        onAddTask={() => {}}
        theme="light"
        onTheme={() => {}}
      />
    </QueryClientProvider>,
  );
  return screen.getByRole("button", { name: "Добавить задачу" });
}

test("at the task limit «Добавить задачу» is disabled and its tooltip says why", () => {
  const add = renderToolbar(3, 3);
  expect(add).toBeDisabled();
  expect(add).toHaveAttribute("title", "В плане уже 3 задачи — это предел");
});

test("below the limit «Добавить задачу» stays available", () => {
  const add = renderToolbar(2, 3);
  expect(add).toBeEnabled();
  expect(add).toHaveAttribute("title", "Добавить задачу");
});

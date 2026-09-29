import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { api } from "@/api/client";
import type { ApplyResponse, ScheduledPlan, ScheduledTask } from "@/api/types";
import { META_KEY } from "@/hooks/useMeta";
import { NewTaskModal } from "./NewTaskModal";

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return { ...actual, api: { applyOps: vi.fn(), meta: vi.fn() } };
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

const planOf = (ids: number[]): ScheduledPlan => ({
  project_start: "2026-09-07",
  project_end: "2026-09-30",
  last_id: 9,
  tasks: ids.map(task),
  dependencies: ids.includes(6) && ids.includes(7) ? [{ predecessor_id: 6, successor_id: 7, lag: 0 }] : [],
  critical_path: [],
});

// The modal gets the plan as a prop from App, which re-renders it on every plan update (the
// agent, another tab); `rerenderWith` stands in for such an update.
// GET /api/meta is seeded into the cache as already loaded (useMeta never refetches it).
function renderModal(plan: ScheduledPlan, anchorId: number | null, maxTasks = 500) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(META_KEY, { llm_mode: "fake", model: null, max_tasks: maxTasks });
  const ui = (p: ScheduledPlan) => (
    <QueryClientProvider client={client}>
      <NewTaskModal plan={p} anchorId={anchorId} onClose={() => {}} disabled={false} />
    </QueryClientProvider>
  );
  const view = render(ui(plan));
  return { rerenderWith: (p: ScheduledPlan) => view.rerender(ui(p)) };
}

const field = (label: RegExp) => screen.getByLabelText(label) as HTMLSelectElement;
const deletedNotice = (id: number) => `Задачу №${id} удалили, пока форма была открыта — выберите другую`;

beforeEach(() => {
  vi.mocked(api.applyOps).mockReset();
});

test("a chosen task deleted elsewhere is unselected, with a notice next to each field that used it", () => {
  const { rerenderWith } = renderModal(planOf([6, 7, 9]), 6);
  fireEvent.change(field(/^Перед задачей/), { target: { value: "7" } });
  expect(field(/^Начать после задачи/).value).toBe("6");
  expect(field(/^Место в списке/).value).toBe("6");

  rerenderWith(planOf([9]));

  expect(field(/^Начать после задачи/).value).toBe("");
  expect(field(/^Перед задачей/).value).toBe("");
  expect(field(/^Место в списке/).value).toBe("");
  expect(screen.getAllByText(deletedNotice(6))).toHaveLength(2); // «Начать после задачи», «Место в списке»
  expect(screen.getByText(deletedNotice(7))).toBeInTheDocument();
});

test("picking another task in that field removes its notice, and the batch no longer names the deleted one", async () => {
  vi.mocked(api.applyOps).mockResolvedValue({ summary: "Добавлена задача", warnings: [] } as unknown as ApplyResponse);
  const { rerenderWith } = renderModal(planOf([6, 7, 9]), 6);
  rerenderWith(planOf([7, 9]));
  expect(screen.getAllByText(deletedNotice(6))).toHaveLength(2);

  fireEvent.change(field(/^Начать после задачи/), { target: { value: "9" } });
  expect(screen.getAllByText(deletedNotice(6))).toHaveLength(1); // still next to «Место в списке»

  fireEvent.change(screen.getByLabelText("Название"), { target: { value: "Ревью" } });
  fireEvent.click(screen.getByRole("button", { name: "Добавить" }));
  await waitFor(() => expect(api.applyOps).toHaveBeenCalled());
  expect(vi.mocked(api.applyOps).mock.calls[0][0]).toEqual([
    { op: "add_task", name: "Ревью", description: "", assignee: null, duration: 1, predecessors: [{ id: 9 }] },
  ]);
});

test("a plan update that keeps the chosen tasks changes nothing", () => {
  const { rerenderWith } = renderModal(planOf([6, 7, 9]), 6);
  rerenderWith(planOf([6, 7]));
  expect(field(/^Начать после задачи/).value).toBe("6");
  expect(screen.queryByText(/удалили, пока форма была открыта/)).not.toBeInTheDocument();
});

test("the plan reaching the task limit while the form is open disables «Добавить» and says why", () => {
  const { rerenderWith } = renderModal(planOf([6, 7]), null, 3);
  expect(screen.getByRole("button", { name: "Добавить" })).toBeEnabled();
  expect(screen.queryByText(/это предел/)).not.toBeInTheDocument();

  rerenderWith(planOf([6, 7, 9]));

  expect(screen.getByText("В плане уже 3 задачи — это предел")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Добавить" })).toBeDisabled();
});

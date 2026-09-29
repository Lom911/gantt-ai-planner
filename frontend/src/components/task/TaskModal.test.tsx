import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { api } from "@/api/client";
import type { ApplyResponse, PlanResponse, ScheduledTask } from "@/api/types";
import { PLAN_KEY } from "@/hooks/usePlan";
import { TaskModal } from "./TaskModal";

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return { ...actual, api: { applyOps: vi.fn(), taskHistory: vi.fn() } };
});

const task: ScheduledTask = {
  id: 7, name: "Дизайн", description: "", assignee: "Мария", duration: 5, constraint_start: null,
  start: "2026-09-21", end: "2026-09-25", slack: 0, is_critical: false, constrained_by: "project_start", overallocated_with: [],
};

function planAt(version: number, changes: Partial<ScheduledTask> = {}): PlanResponse {
  return {
    version,
    can_undo: false,
    can_redo: false,
    agent_busy: false,
    plan: {
      project_start: "2026-09-21",
      project_end: "2026-09-25",
      last_id: 7,
      tasks: [{ ...task, ...changes }],
      dependencies: [],
      critical_path: [],
    },
  };
}

function card(plan: PlanResponse) {
  return (
    <TaskModal
      task={plan.plan.tasks[0]}
      plan={plan.plan}
      version={plan.version}
      open
      onOpenChange={() => {}}
      onNavigate={() => {}}
      onAddAfter={() => {}}
      disabled={false}
    />
  );
}

// The card as App shows it: its task comes from the cached plan, so a change made elsewhere (the
// agent, another tab) lands in the cache and App re-renders the open card with the fresh task.
function renderCard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const show = (plan: PlanResponse) => {
    client.setQueryData(PLAN_KEY, plan);
    return <QueryClientProvider client={client}>{card(plan)}</QueryClientProvider>;
  };
  const view = render(show(planAt(1)));
  return {
    changedElsewhere: (version: number, changes: Partial<ScheduledTask>) => view.rerender(show(planAt(version, changes))),
  };
}

const edit = (label: string, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });
// Buttons are found by their text: a by-role query over the whole dialog is slow in jsdom.
const button = (name: string) => screen.queryByText(name, { selector: "button" });
// A click that may start a save: the request settles inside act, so do the state updates after it.
const click = (name: string) => act(async () => void fireEvent.click(screen.getByText(name, { selector: "button" })));
const conflictPrompt = () => screen.queryByText(/пока вы редактировали/);

beforeEach(() => {
  vi.mocked(api.taskHistory).mockResolvedValue([]);
  vi.mocked(api.applyOps).mockReset();
  vi.mocked(api.applyOps).mockResolvedValue({
    ...planAt(9),
    changes: [],
    warnings: [],
    summary: "Изменено задач: 1",
    created_task_ids: [],
  } satisfies ApplyResponse);
});

test("no conflict: Save sends the edit with the cached version, as before", async () => {
  renderCard();
  edit("Длительность, дн.", "6");
  await click("Сохранить");
  expect(api.applyOps).toHaveBeenCalledWith([{ op: "update_task", id: 7, duration: 6 }], 1);
});

test("a change elsewhere to a field the user hasn't touched is picked up, and Save sends only the user's edit", async () => {
  const { changedElsewhere } = renderCard();
  edit("Название", "Дизайн v2");
  changedElsewhere(2, { duration: 7 });

  expect(screen.getByLabelText("Длительность, дн.")).toHaveValue(7);
  expect(conflictPrompt()).not.toBeInTheDocument();
  await click("Сохранить");
  expect(api.applyOps).toHaveBeenCalledWith([{ op: "update_task", id: 7, name: "Дизайн v2" }], 2);
});

test("a change elsewhere to the field the user is editing shows a conflict and saves nothing", () => {
  const { changedElsewhere } = renderCard();
  edit("Длительность, дн.", "4");
  changedElsewhere(2, { duration: 7 });

  expect(
    screen.getByText("Длительность изменилась, пока вы редактировали: теперь 7 дн. (было 5). Ваше значение: 4."),
  ).toBeInTheDocument();
  expect(screen.getByLabelText("Длительность, дн.")).toHaveValue(4);
  expect(button("Сохранить")).not.toBeInTheDocument();
  expect(api.applyOps).not.toHaveBeenCalled();
});

test("«Сохранить моё» saves the user's value over the new one", async () => {
  const { changedElsewhere } = renderCard();
  edit("Длительность, дн.", "4");
  changedElsewhere(2, { duration: 7 });

  await click("Сохранить моё");
  expect(api.applyOps).toHaveBeenCalledWith([{ op: "update_task", id: 7, duration: 4 }], 2);
});

test("«Взять новое» drops the user's edit of that field, keeps their other edits", async () => {
  const { changedElsewhere } = renderCard();
  edit("Название", "Дизайн v2");
  edit("Длительность, дн.", "4");
  changedElsewhere(2, { duration: 7 });

  await click("Взять новое");
  expect(api.applyOps).not.toHaveBeenCalled();
  expect(conflictPrompt()).not.toBeInTheDocument();
  expect(screen.getByLabelText("Длительность, дн.")).toHaveValue(7);
  expect(screen.getByLabelText("Название")).toHaveValue("Дизайн v2");

  await click("Сохранить");
  expect(api.applyOps).toHaveBeenCalledWith([{ op: "update_task", id: 7, name: "Дизайн v2" }], 2);
});

test("Save checks the edited fields against the same cached plan it takes the version from", async () => {
  // The cache already holds the other change (version 2) but the card hasn't re-rendered with it
  // yet: sending the user's 4 with version 2 would silently overwrite the 7 without a 409.
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(PLAN_KEY, planAt(2, { duration: 7 }));
  render(<QueryClientProvider client={client}>{card(planAt(1))}</QueryClientProvider>);

  edit("Длительность, дн.", "4");
  await click("Сохранить");
  expect(api.applyOps).not.toHaveBeenCalled();
});

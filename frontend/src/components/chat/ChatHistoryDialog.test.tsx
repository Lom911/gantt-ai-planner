import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { api } from "@/api/client";
import { ChatHistoryDialog } from "./ChatHistoryDialog";

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return { ...actual, api: { conversations: vi.fn(), conversation: vi.fn() } };
});

function renderDialog(onFocusTask = vi.fn(), onOpenChange = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <ChatHistoryDialog open onOpenChange={onOpenChange} onFocusTask={onFocusTask} />
    </QueryClientProvider>,
  );
}

test("lists earlier conversations and opens one read-only", async () => {
  vi.mocked(api.conversations).mockResolvedValue([
    { id: "b", started_at: "2026-09-28T10:00:00Z", last_at: "2026-09-28T10:05:00Z", message_count: 4, title: "Сдвинь задачи Дмитрия" },
    { id: "a", started_at: "2026-09-27T09:00:00Z", last_at: "2026-09-27T09:01:00Z", message_count: 1, title: "Загружен план" },
  ]);
  vi.mocked(api.conversation).mockResolvedValue([
    { id: 1, role: "user", content: "Сдвинь задачи Дмитрия", created_at: "2026-09-28T10:00:00Z", meta: {} },
    { id: 2, role: "assistant", content: "Сдвинул 3 задачи.", created_at: "2026-09-28T10:00:30Z", meta: {} },
  ]);
  renderDialog();

  expect(await screen.findByText("Сдвинь задачи Дмитрия")).toBeInTheDocument();
  expect(screen.getByText(/4 сообщения/)).toBeInTheDocument();
  expect(screen.getByText(/1 сообщение$/)).toBeInTheDocument();

  fireEvent.click(screen.getByText("Сдвинь задачи Дмитрия"));
  expect(await screen.findByText("Сдвинул 3 задачи.")).toBeInTheDocument();
  expect(api.conversation).toHaveBeenCalledWith("b");
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument(); // nothing to reply with

  fireEvent.click(screen.getByRole("button", { name: /Все диалоги/ }));
  expect(await screen.findByText("Загружен план")).toBeInTheDocument();
});

test("says so when there is no earlier conversation", async () => {
  vi.mocked(api.conversations).mockResolvedValue([]);
  renderDialog();
  expect(await screen.findByText("Прошлых диалогов пока нет.")).toBeInTheDocument();
});

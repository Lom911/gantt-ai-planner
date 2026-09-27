import type { ReactElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render as rtlRender, screen, waitFor } from "@testing-library/react";
import { api } from "@/api/client";
import { ConfirmationBanner } from "./ConfirmationBanner";

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return { ...actual, api: { approveConfirmation: vi.fn(), rejectConfirmation: vi.fn() } };
});

const render = (ui: ReactElement) => rtlRender(<QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>);
const future = new Date(Date.now() + 5 * 60_000).toISOString();
const mcp = { id: "c1", origin: "mcp" as const, summary: "Удалить задачи: №1 «А», №2 «Б» (всего 6)", count: 6, expires_at: future, approved: false };

beforeEach(() => {
  vi.mocked(api.approveConfirmation).mockResolvedValue({});
  vi.mocked(api.rejectConfirmation).mockResolvedValue({});
});

test("an MCP mass deletion is approved or rejected in the app", async () => {
  render(<ConfirmationBanner pending={mcp} />);
  expect(screen.getByRole("alert")).toHaveTextContent("Внешний MCP-клиент просит удалить задачи (6)");
  fireEvent.click(screen.getByRole("button", { name: "Разрешить удаление" }));
  await waitFor(() => expect(api.approveConfirmation).toHaveBeenCalledWith("c1"));
  fireEvent.click(screen.getByRole("button", { name: "Отклонить" }));
  await waitFor(() => expect(api.rejectConfirmation).toHaveBeenCalledWith("c1"));
});

test("chat-originated, expired or already approved requests don't ask again", () => {
  const { rerender } = render(<ConfirmationBanner pending={{ ...mcp, origin: "agent" }} />);
  expect(screen.queryByRole("alert")).toBeNull();
  rerender(
    <QueryClientProvider client={new QueryClient()}>
      <ConfirmationBanner pending={{ ...mcp, expires_at: new Date(Date.now() - 1000).toISOString() }} />
    </QueryClientProvider>,
  );
  expect(screen.queryByRole("alert")).toBeNull();
  rerender(
    <QueryClientProvider client={new QueryClient()}>
      <ConfirmationBanner pending={{ ...mcp, approved: true }} />
    </QueryClientProvider>,
  );
  expect(screen.getByRole("alert")).toHaveTextContent("Разрешено");
  expect(screen.queryByRole("button", { name: "Разрешить удаление" })).toBeNull();
});

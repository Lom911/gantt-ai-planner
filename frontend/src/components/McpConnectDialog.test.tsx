import type { ReactElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render as rtlRender, screen, waitFor } from "@testing-library/react";
import { api } from "@/api/client";
import { historySafeCommand } from "@/lib/mcpCommands";
import { McpConnectDialog } from "./McpConnectDialog";

vi.mock("@/api/client", async () => {
  const actual = await vi.importActual<typeof import("@/api/client")>("@/api/client");
  return { ...actual, api: { createMcpToken: vi.fn(), revokeMcpToken: vi.fn(), mcpTokenStatus: vi.fn() } };
});

const sample = {
  token: "mcp_secret123",
  expires_at: "2026-10-02T00:00:00Z",
  url: "http://localhost:8000/mcp",
  claude_code_command:
    'claude mcp add --transport http planner http://localhost:8000/mcp --header "Authorization: Bearer mcp_secret123"',
  claude_desktop_config: {
    mcpServers: {
      planner: { url: "http://localhost:8000/mcp", headers: { Authorization: "Bearer mcp_secret123" } },
    },
  },
};

const render = (ui: ReactElement) =>
  rtlRender(<QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>);

const inactive = { active: false, prefix: null, created_at: null, expires_at: null, last_used_at: null };

beforeEach(() => {
  vi.mocked(api.mcpTokenStatus).mockResolvedValue(inactive);
  vi.mocked(api.createMcpToken).mockResolvedValue(sample);
  vi.mocked(api.revokeMcpToken).mockResolvedValue(undefined);
  Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
});

test("issues a token, shows it once with the warning, and copies it", async () => {
  render(<McpConnectDialog open onOpenChange={() => {}} />);

  fireEvent.click(screen.getByRole("button", { name: "Выпустить токен" }));
  await waitFor(() => expect(api.createMcpToken).toHaveBeenCalled());

  expect(await screen.findByText("Токен даёт доступ к вашему плану. Не публикуйте его.")).toBeInTheDocument();
  expect(screen.getByDisplayValue(sample.token)).toBeInTheDocument();
  expect(screen.getByDisplayValue(sample.claude_code_command)).toBeInTheDocument();

  const copyButtons = screen.getAllByRole("button", { name: "Копировать" });
  fireEvent.click(copyButtons[0]);
  await waitFor(() => expect(navigator.clipboard.writeText).toHaveBeenCalledWith(sample.token));
});

test("revoking clears the shown token", async () => {
  render(<McpConnectDialog open onOpenChange={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Выпустить токен" }));
  await screen.findByDisplayValue(sample.token);

  fireEvent.click(screen.getByRole("button", { name: "Отозвать" }));
  await waitFor(() => expect(api.revokeMcpToken).toHaveBeenCalled());
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Выпустить токен" })).toBeInTheDocument(),
  );
});

test("shows a live token's status and lets the user revoke it without having the token", async () => {
  vi.mocked(api.mcpTokenStatus).mockResolvedValue({
    active: true,
    prefix: "mcp_ab12",
    created_at: "2026-09-26T10:00:00Z",
    expires_at: "2026-10-03T10:00:00Z",
    last_used_at: null,
  });
  render(<McpConnectDialog open onOpenChange={() => {}} />);
  expect(await screen.findByText(/Активный токен/)).toHaveTextContent("mcp_ab12");
  expect(screen.getByText(/ещё не использовался/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Отозвать" }));
  await waitFor(() => expect(api.revokeMcpToken).toHaveBeenCalled());
  expect(screen.getByRole("button", { name: /Выпустить новый токен/ })).toBeInTheDocument();
});

test("offers a command that keeps the token out of the shell history", async () => {
  render(<McpConnectDialog open onOpenChange={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "Выпустить токен" }));
  const safe = historySafeCommand(sample.url);
  expect(await screen.findByDisplayValue(safe)).toBeInTheDocument();
  expect(safe).not.toContain(sample.token);
  expect(safe).toContain("read -rs GANTT_MCP_TOKEN");
});

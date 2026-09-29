import type { ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ChatEvent, ChatMessage } from "@/api/types";

const script: { events: ChatEvent[] } = { events: [] };
vi.mock("@/api/chatStream", () => ({
  async *streamChat() {
    for (const e of script.events) yield e;
  },
}));
const history: { rows: ChatMessage[] } = { rows: [] };
const calls: string[] = [];
vi.mock("@/api/client", () => ({
  api: {
    chatHistory: async () => {
      calls.push("history");
      return history.rows;
    },
    newConversation: async () => {
      calls.push("new");
      return { id: "c" };
    },
  },
}));

const { CHAT_HISTORY_KEY, clearChat, useChat } = await import("./useChat");

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const row = (id: number, role: ChatMessage["role"], content: string, meta = {}): ChatMessage => ({
  id,
  role,
  content,
  created_at: "2026-09-26T10:00:00Z",
  meta,
});

// First in the file: the page's first load of the chat (the conversation is started once per page).
test("opening the page starts a new conversation, once, before the chat is read", async () => {
  history.rows = [];
  const first = renderHook(() => useChat(), { wrapper });
  await waitFor(() => expect(calls).toEqual(["new", "history"]));
  const again = renderHook(() => useChat(), { wrapper }); // a remount, with its own query cache
  await waitFor(() => expect(calls).toEqual(["new", "history", "history"]));
  first.unmount();
  again.unmount();
});

test("clearing the chat starts a new conversation and empties it at once", async () => {
  const client = new QueryClient();
  client.setQueryData(CHAT_HISTORY_KEY, [row(1, "user", "привет")]);
  calls.length = 0;
  await clearChat(client);
  expect(calls).toEqual(["new"]);
  expect(client.getQueryData(CHAT_HISTORY_KEY)).toEqual([]);
});

test("a failed turn is shown once, as the assistant reply the server saved", async () => {
  script.events = [{ type: "error", code: "llm_unavailable", message: "LLM временно недоступна" }];
  history.rows = [];
  const { result } = renderHook(() => useChat(), { wrapper });
  await waitFor(() => expect(result.current.messages).toEqual([]));
  history.rows = [row(1, "user", "привет"), row(2, "assistant", "LLM временно недоступна", { error: "llm_unavailable" })];
  await act(() => result.current.send("привет"));
  expect(result.current.error).toBeNull();
  expect(result.current.messages.filter((m) => m.content === "LLM временно недоступна")).toHaveLength(1);
});

test("a busy rejection is not saved server-side, so it shows as the error line", async () => {
  script.events = [{ type: "error", code: "agent_busy", message: "Агент сейчас редактирует план, подождите" }];
  history.rows = [];
  const { result } = renderHook(() => useChat(), { wrapper });
  await waitFor(() => expect(result.current.messages).toEqual([]));
  await act(() => result.current.send("ещё"));
  expect(result.current.error).toBe("Агент сейчас редактирует план, подождите");
  expect(result.current.messages).toEqual([]);
});

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/api/client";
import { streamChat } from "@/api/chatStream";
import type { Change, ChatMessage } from "@/api/types";
import { PLAN_KEY } from "./usePlan";

export const CHAT_HISTORY_KEY = ["chat", "history"];
export const CHAT_CONVERSATIONS_KEY = ["chat", "conversations"];

// Opening or reloading the page starts a new conversation: the chat opens empty and the agent
// doesn't remember the old one, which stays under «История». Once per page load, shared by every
// mount of the chat and every refetch of its history. If it fails, the chat just shows the
// conversation it was on.
let pageConversation: Promise<unknown> | null = null;

export const chatHistoryQuery = {
  queryKey: CHAT_HISTORY_KEY,
  queryFn: async () => {
    pageConversation ??= api.newConversation().catch(() => undefined);
    await pageConversation;
    return api.chatHistory();
  },
};

// «Очистить чат»: the same as a reload, without reloading.
export async function clearChat(queryClient: QueryClient): Promise<void> {
  await api.newConversation();
  queryClient.setQueryData(CHAT_HISTORY_KEY, []);
  void queryClient.invalidateQueries({ queryKey: CHAT_CONVERSATIONS_KEY });
}

// Human labels for the tool-call status line shown while the agent is working.
const TOOL_LABELS: Record<string, string> = {
  apply_operations: "применяю изменения",
  get_plan: "смотрю план",
  find_tasks: "ищу задачи",
  get_task: "смотрю задачу",
  get_resource_load: "проверяю загрузку",
  undo: "отменяю",
};

export interface StreamingState {
  text: string;
  status: string | null;
}

let localIdSeq = 0;
function localMessage(role: ChatMessage["role"], content: string, meta: ChatMessage["meta"] = {}): ChatMessage {
  localIdSeq -= 1;
  return { id: localIdSeq, role, content, created_at: new Date().toISOString(), meta };
}

export function useChat(): {
  messages: ChatMessage[];
  streaming: StreamingState | null;
  send(text: string): Promise<void>;
  error: string | null;
} {
  const queryClient = useQueryClient();
  const historyQuery = useQuery(chatHistoryQuery);
  // Optimistic messages for the turn in flight (the user's text + the streamed reply). They sit
  // on top of the persisted history and are cleared once the invalidated history query has
  // re-fetched and (now) contains them — deriving `messages` this way needs no effect to keep a
  // separate copy of `historyQuery.data` in sync.
  const [turnMessages, setTurnMessages] = useState<ChatMessage[]>([]);
  const [streaming, setStreaming] = useState<StreamingState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const sendingRef = useRef(false);

  const messages = useMemo(
    () => [...(historyQuery.data ?? []), ...turnMessages],
    [historyQuery.data, turnMessages],
  );

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || sendingRef.current) return;
      sendingRef.current = true;
      setError(null);
      setTurnMessages((prev) => [...prev, localMessage("user", trimmed)]);
      let accumulated = "";
      let status: string | null = null;
      setStreaming({ text: "", status: null });
      const controller = new AbortController();
      abortRef.current = controller;
      try {
        for await (const event of streamChat(trimmed, controller.signal)) {
          switch (event.type) {
            case "text_delta":
              accumulated += event.text;
              setStreaming({ text: accumulated, status });
              break;
            case "tool_started":
              status = `Выполняю: ${TOOL_LABELS[event.name] ?? event.name}`;
              setStreaming({ text: accumulated, status });
              break;
            case "tool_finished":
              break;
            case "plan_changed":
              void queryClient.invalidateQueries({ queryKey: PLAN_KEY });
              break;
            case "done": {
              const changes: Change[] = event.changes ?? [];
              setTurnMessages((prev) => [
                ...prev,
                localMessage("assistant", accumulated, { summary: event.summary, changes }),
              ]);
              // The Gantt highlight for these ids comes from `useSessionEvents`' own
              // `plan_changed` bus event (published for every apply, including this one), not
              // from here.
              break;
            }
            case "error":
              // A failed turn is saved by the server as the assistant's reply, so it shows once,
              // as that bubble. `agent_busy` is the exception: that turn never started and
              // nothing was saved (its message is dropped), so it goes to the error line.
              if (event.code === "agent_busy") {
                setError(event.message);
              } else {
                setTurnMessages((prev) => [...prev, localMessage("assistant", event.message, { error: event.code })]);
              }
              break;
          }
        }
      } catch (err) {
        // ApiError messages are ours (Russian, user-facing); anything else is a browser error
        // such as the stream dying mid-answer ("network error") — don't show that text.
        setError(
          err instanceof ApiError
            ? err.message
            : "Связь с сервером прервалась. Проверьте подключение и отправьте сообщение ещё раз.",
        );
      } finally {
        setStreaming(null);
        abortRef.current = null;
        sendingRef.current = false;
        await queryClient.invalidateQueries({ queryKey: CHAT_HISTORY_KEY });
        void queryClient.invalidateQueries({ queryKey: PLAN_KEY });
        setTurnMessages([]);
      }
    },
    [queryClient],
  );

  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  return { messages, streaming, send, error };
}

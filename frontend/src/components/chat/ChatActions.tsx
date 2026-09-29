import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Eraser, History } from "lucide-react";
import { toast } from "sonner";
import { ApiError } from "@/api/client";
import { chatHistoryQuery, clearChat } from "@/hooks/useChat";
import { ChatHistoryDialog } from "./ChatHistoryDialog";

const buttonClass =
  "inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-xs text-muted-foreground hover:bg-accent hover:text-accent-foreground disabled:pointer-events-none disabled:opacity-40";

// The chat header's «История» and «Очистить» (SplitLayout places them).
export function ChatActions({ agentBusy, onFocusTask }: { agentBusy: boolean; onFocusTask(id: number): void }) {
  const queryClient = useQueryClient();
  const { data: messages } = useQuery(chatHistoryQuery);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [clearing, setClearing] = useState(false);

  const clear = async () => {
    setClearing(true);
    try {
      await clearChat(queryClient);
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Не удалось очистить чат");
    } finally {
      setClearing(false);
    }
  };

  return (
    <>
      <button
        type="button"
        title="Прошлые диалоги"
        className={buttonClass}
        onClick={() => setHistoryOpen(true)}
      >
        <History className="h-3.5 w-3.5" aria-hidden="true" /> История
      </button>
      <button
        type="button"
        title="Начать чат заново: этот диалог сохранится в «Истории»"
        className={buttonClass}
        // A turn in progress belongs to this conversation: clearing now would hide its answer.
        disabled={agentBusy || clearing || !messages?.length}
        onClick={() => void clear()}
      >
        <Eraser className="h-3.5 w-3.5" aria-hidden="true" /> Очистить
      </button>
      <ChatHistoryDialog open={historyOpen} onOpenChange={setHistoryOpen} onFocusTask={onFocusTask} />
    </>
  );
}

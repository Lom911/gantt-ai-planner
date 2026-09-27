import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, ApiError } from "@/api/client";
import type { PendingConfirmation } from "@/api/types";
import { CONFIRMATION_KEY } from "@/hooks/useConfirmation";

// An external MCP client (Claude Desktop, Claude Code…) asked for a mass deletion. Such a request
// can't confirm itself — text in a plan or an Excel file could have told the client to do it — so
// the user approves or rejects it here, in the app. Chat deletions are confirmed in the chat
// instead (the user's own «да»), so only origin "mcp" is shown.
export function ConfirmationBanner({ pending }: { pending: PendingConfirmation | null | undefined }) {
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState(false);
  if (!pending || pending.origin !== "mcp" || new Date(pending.expires_at) < new Date()) return null;

  const act = async (approve: boolean) => {
    setBusy(true);
    try {
      await (approve ? api.approveConfirmation(pending.id) : api.rejectConfirmation(pending.id));
      toast.success(approve ? "Удаление разрешено: MCP-клиент может повторить запрос" : "Удаление отклонено");
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Не удалось отправить ответ");
    } finally {
      setBusy(false);
      void queryClient.invalidateQueries({ queryKey: CONFIRMATION_KEY });
    }
  };

  const until = new Date(pending.expires_at).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
  return (
    <div
      role="alert"
      className="flex flex-wrap items-center gap-3 border-b border-amber-300 bg-amber-50 px-4 py-2 text-sm text-amber-900 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-amber-200"
    >
      <div className="min-w-0 flex-1">
        <span className="font-semibold">Внешний MCP-клиент просит удалить задачи ({pending.count}).</span>{" "}
        {pending.summary}
        <span className="text-amber-700 dark:text-amber-300"> · Запрос действует до {until}.</span>
      </div>
      {pending.approved ? (
        <span className="font-medium">Разрешено — ждём повторный запрос клиента</span>
      ) : (
        <div className="flex gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => void act(true)}
            className="rounded-md bg-destructive px-3 py-1.5 font-medium text-white disabled:opacity-50"
          >
            Разрешить удаление
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => void act(false)}
            className="rounded-md border border-amber-400 px-3 py-1.5 hover:bg-amber-100 disabled:opacity-50 dark:hover:bg-amber-500/20"
          >
            Отклонить
          </button>
        </div>
      )}
    </div>
  );
}

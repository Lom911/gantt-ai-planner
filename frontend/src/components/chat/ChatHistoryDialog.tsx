import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import { api } from "@/api/client";
import type { ChatConversation } from "@/api/types";
import { CHAT_CONVERSATIONS_KEY } from "@/hooks/useChat";
import { ruPlural } from "@/lib/resourceSummary";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { MessageItem } from "./MessageItem";

const WHEN = new Intl.DateTimeFormat("ru-RU", {
  day: "numeric",
  month: "long",
  hour: "2-digit",
  minute: "2-digit",
});
const when = (iso: string) => WHEN.format(new Date(iso));

// «История»: the earlier conversations of this session, read-only. The chat itself holds the
// current one; a page load or «Очистить» moves it here.
export function ChatHistoryDialog({
  open,
  onOpenChange,
  onFocusTask,
}: {
  open: boolean;
  onOpenChange(open: boolean): void;
  onFocusTask(id: number): void;
}) {
  const [selected, setSelected] = useState<ChatConversation | null>(null);
  const list = useQuery({ queryKey: CHAT_CONVERSATIONS_KEY, queryFn: api.conversations, enabled: open });
  const messages = useQuery({
    queryKey: ["chat", "conversation", selected?.id],
    queryFn: () => api.conversation(selected!.id),
    enabled: open && selected != null,
  });

  const close = (value: boolean) => {
    if (!value) setSelected(null);
    onOpenChange(value);
  };

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="flex max-w-xl flex-col gap-3">
        <DialogTitle>История чата</DialogTitle>
        {selected ? (
          <>
            <div className="flex items-center gap-2">
              <button
                type="button"
                className="inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-sm text-muted-foreground hover:bg-accent hover:text-accent-foreground"
                onClick={() => setSelected(null)}
              >
                <ArrowLeft className="h-4 w-4" /> Все диалоги
              </button>
              <DialogDescription className="ml-auto">{when(selected.started_at)}</DialogDescription>
            </div>
            <div className="max-h-[60vh] min-h-24 overflow-y-auto rounded-md border border-border px-3 py-2">
              {messages.isPending && <p className="text-sm text-muted-foreground">Загрузка…</p>}
              {messages.isError && <p className="text-sm text-destructive">{messages.error.message}</p>}
              {messages.data?.map((message) => (
                <MessageItem
                  key={message.id}
                  message={message}
                  onFocusTask={(id) => {
                    close(false);
                    onFocusTask(id);
                  }}
                />
              ))}
            </div>
          </>
        ) : (
          <>
            <DialogDescription>
              Чат начинается заново при каждом открытии страницы и по кнопке «Очистить», а прошлые
              диалоги сохраняются здесь.
            </DialogDescription>
            {list.isPending && <p className="text-sm text-muted-foreground">Загрузка…</p>}
            {list.isError && <p className="text-sm text-destructive">{list.error.message}</p>}
            {list.data?.length === 0 && <p className="text-sm text-muted-foreground">Прошлых диалогов пока нет.</p>}
            {list.data && list.data.length > 0 && (
              <ul className="-mx-1 max-h-[60vh] overflow-y-auto px-1">
                {list.data.map((c) => (
                  <li key={c.id}>
                    <button
                      type="button"
                      className="my-1 w-full rounded-md border border-border px-3 py-2 text-left hover:bg-accent hover:text-accent-foreground"
                      onClick={() => setSelected(c)}
                    >
                      <div className="truncate text-sm font-medium">{c.title}</div>
                      <div className="text-xs text-muted-foreground">
                        {when(c.started_at)} · {c.message_count}{" "}
                        {ruPlural(c.message_count, "сообщение", "сообщения", "сообщений")}
                      </div>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

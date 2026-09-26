import { useEffect, useRef, useState } from "react";
import { useChat } from "@/hooks/useChat";
import { useMeta } from "@/hooks/useMeta";
import { llmDisclaimer } from "./llmDisclaimer";
import { MessageItem } from "./MessageItem";

const EXAMPLE_PROMPTS = [
  "Сдвинь все задачи Дмитрия на 3 дня",
  "Перенеси задачу 6 на 2 дня",
  "Назначь задачу 19 на Игоря Петрова",
  "Добавь задачу «Ревью безопасности» на 2 дня после 10",
];

export function ChatPanel({
  onFocusTask,
  agentBusy,
}: {
  onFocusTask: (id: number) => void;
  agentBusy: boolean;
}) {
  const { messages, streaming, send, error } = useChat();
  const { data: meta } = useMeta();
  const [input, setInput] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  const disabled = streaming !== null || agentBusy;

  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, streaming]);

  const submit = () => {
    const text = input.trim();
    if (!text || disabled) return;
    setInput("");
    void send(text);
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div ref={listRef} className="min-h-0 flex-1 overflow-y-auto px-3 py-2">
        {messages.length === 0 && !streaming && (
          <div className="flex h-full flex-col items-center justify-center gap-3 p-4 text-center text-sm text-muted-foreground">
            <p>Напишите, что изменить в плане, например:</p>
            <div className="flex flex-wrap justify-center gap-2">
              {EXAMPLE_PROMPTS.map((prompt) => (
                <button
                  key={prompt}
                  type="button"
                  className="rounded-full border border-border px-3 py-1 text-xs hover:bg-accent hover:text-accent-foreground"
                  onClick={() => setInput(prompt)}
                >
                  {prompt}
                </button>
              ))}
            </div>
          </div>
        )}
        {messages.map((message) => (
          <MessageItem key={message.id} message={message} onFocusTask={onFocusTask} />
        ))}
        {streaming && (
          <div className="my-1.5 flex justify-start">
            <div className="max-w-[85%] whitespace-pre-wrap rounded-lg bg-secondary px-3 py-2 text-sm text-secondary-foreground">
              {streaming.text}
              {streaming.status && (
                <div className="mt-1 flex items-center gap-1.5 text-xs text-muted-foreground">
                  <span className="inline-block h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-current" />
                  {streaming.status}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
      {error && <div className="px-3 py-1 text-xs text-destructive">{error}</div>}
      <div className="border-t border-border p-2">
        <textarea
          aria-label="Сообщение агенту"
          className="w-full resize-none rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-ring disabled:opacity-50"
          rows={2}
          value={input}
          disabled={disabled}
          placeholder="Напишите сообщение агенту…"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
        />
        <div className="mt-1 flex items-center justify-between gap-2">
          <p className="text-[11px] text-muted-foreground">{llmDisclaimer(meta?.llm_mode)}</p>
          <button
            type="button"
            className="shrink-0 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50"
            disabled={disabled || !input.trim()}
            onClick={submit}
          >
            Отправить
          </button>
        </div>
      </div>
    </div>
  );
}

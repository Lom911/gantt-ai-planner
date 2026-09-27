import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, ApiError } from "@/api/client";
import { formatRuDateTime } from "@/lib/dates";
import { historySafeCommand } from "@/lib/mcpCommands";
import type { McpTokenResponse } from "@/api/types";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";

async function copyToClipboard(text: string, label: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
    toast.success(`${label} скопирован`);
  } catch {
    toast.error(`Не удалось скопировать: ${label.toLowerCase()}`);
  }
}

const MCP_STATUS_KEY = ["mcp-token-status"];

// spec §8: the token is issued once, shown once (server keeps only sha256 + prefix), lives up to
// 7 days, and issuing a new one revokes whatever was issued before.
export function McpConnectDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange(open: boolean): void;
}) {
  const [issued, setIssued] = useState<McpTokenResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Whether a token issued earlier (another tab, a previous visit) is still live — the token
  // itself is shown only once, but its status can be checked and it can be revoked any time.
  const queryClient = useQueryClient();
  const { data: status } = useQuery({
    queryKey: MCP_STATUS_KEY,
    queryFn: api.mcpTokenStatus,
    enabled: open,
    retry: false,
  });
  const refreshStatus = () => void queryClient.invalidateQueries({ queryKey: MCP_STATUS_KEY });

  const handleOpenChange = (next: boolean) => {
    if (!next) {
      setIssued(null);
      setError(null);
    }
    onOpenChange(next);
  };

  const issueToken = async () => {
    setLoading(true);
    setError(null);
    try {
      setIssued(await api.createMcpToken());
      refreshStatus();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Не удалось выпустить токен");
    } finally {
      setLoading(false);
    }
  };

  const revokeToken = async () => {
    setLoading(true);
    try {
      await api.revokeMcpToken();
      toast.success("Токен отозван");
      setIssued(null);
      refreshStatus();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Не удалось отозвать токен");
    } finally {
      setLoading(false);
    }
  };

  const desktopConfigJson = issued ? JSON.stringify(issued.claude_desktop_config, null, 2) : "";

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent>
        <DialogTitle>Подключить MCP</DialogTitle>
        <DialogDescription>
          Внешний MCP-клиент (Claude Code, Claude Desktop) сможет читать и редактировать текущий
          план по токену. Изменения сразу появятся в браузере.
        </DialogDescription>

        <div className="mt-3 flex flex-col gap-3">
          {!issued ? (
            <>
              {error && <p className="text-sm text-destructive">{error}</p>}
              {status?.active && (
                <div className="flex flex-col gap-2 rounded-md border border-border p-2 text-sm">
                  <p>
                    Активный токен <span className="font-mono">{status.prefix}…</span>
                    {status.last_used_at
                      ? ` · последнее использование ${formatRuDateTime(status.last_used_at)}`
                      : " · ещё не использовался"}
                    {status.expires_at && ` · действует до ${formatRuDateTime(status.expires_at)}`}
                  </p>
                  <button
                    type="button"
                    className="self-start rounded-md border border-input px-3 py-1.5 text-sm text-destructive hover:bg-accent disabled:opacity-50"
                    disabled={loading}
                    onClick={() => void revokeToken()}
                  >
                    Отозвать
                  </button>
                </div>
              )}
              <button
                type="button"
                className="self-start rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-50"
                disabled={loading}
                onClick={() => void issueToken()}
              >
                {status?.active ? "Выпустить новый токен (старый перестанет работать)" : "Выпустить токен"}
              </button>
            </>
          ) : (
            <>
              <p className="rounded-md bg-amber-100 px-3 py-2 text-sm font-medium text-amber-900">
                Токен даёт доступ к вашему плану. Не публикуйте его.
              </p>

              <label className="flex flex-col gap-1 text-sm">
                Токен (показан один раз)
                <div className="flex gap-2">
                  <input
                    readOnly
                    value={issued.token}
                    onFocus={(e) => e.currentTarget.select()}
                    className="flex-1 rounded-md border border-input bg-background px-2 py-1 font-mono text-xs"
                  />
                  <button
                    type="button"
                    className="shrink-0 rounded-md border border-input px-2 py-1 text-sm hover:bg-accent"
                    onClick={() => void copyToClipboard(issued.token, "Токен")}
                  >
                    Копировать
                  </button>
                </div>
              </label>

              <label className="flex flex-col gap-1 text-sm">
                Claude Code
                <div className="flex gap-2">
                  <textarea
                    readOnly
                    rows={2}
                    value={issued.claude_code_command}
                    onFocus={(e) => e.currentTarget.select()}
                    className="flex-1 resize-none rounded-md border border-input bg-background px-2 py-1 font-mono text-xs"
                  />
                  <button
                    type="button"
                    className="shrink-0 self-start rounded-md border border-input px-2 py-1 text-sm hover:bg-accent"
                    onClick={() => void copyToClipboard(issued.claude_code_command, "Команда")}
                  >
                    Копировать
                  </button>
                </div>
                <span className="text-xs text-muted-foreground">
                  Команда содержит токен и попадёт в историю оболочки. В bash/zsh без записи в историю:
                </span>
                <div className="flex gap-2">
                  <textarea
                    readOnly
                    rows={2}
                    value={historySafeCommand(issued.url)}
                    onFocus={(e) => e.currentTarget.select()}
                    className="flex-1 resize-none rounded-md border border-input bg-background px-2 py-1 font-mono text-xs"
                  />
                  <button
                    type="button"
                    className="shrink-0 self-start rounded-md border border-input px-2 py-1 text-sm hover:bg-accent"
                    onClick={() => void copyToClipboard(historySafeCommand(issued.url), "Команда")}
                  >
                    Копировать
                  </button>
                </div>
              </label>

              <label className="flex flex-col gap-1 text-sm">
                Claude Desktop (claude_desktop_config.json)
                <div className="flex gap-2">
                  <textarea
                    readOnly
                    rows={6}
                    value={desktopConfigJson}
                    onFocus={(e) => e.currentTarget.select()}
                    className="flex-1 resize-none rounded-md border border-input bg-background px-2 py-1 font-mono text-xs"
                  />
                  <button
                    type="button"
                    className="shrink-0 self-start rounded-md border border-input px-2 py-1 text-sm hover:bg-accent"
                    onClick={() => void copyToClipboard(desktopConfigJson, "JSON")}
                  >
                    Копировать
                  </button>
                </div>
              </label>

              <div className="mt-1 flex justify-end gap-2">
                <button
                  type="button"
                  className="rounded-md border border-input px-3 py-1.5 text-sm text-destructive hover:bg-accent disabled:opacity-50"
                  disabled={loading}
                  onClick={() => void revokeToken()}
                >
                  Отозвать
                </button>
              </div>
            </>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

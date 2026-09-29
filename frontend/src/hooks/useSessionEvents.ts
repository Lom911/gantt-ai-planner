import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ensureSession } from "@/api/client";
import { cachedPlanVersion, PLAN_KEY } from "./usePlan";
import { CONFIRMATION_KEY } from "./useConfirmation";
import { CHAT_CONVERSATIONS_KEY, CHAT_HISTORY_KEY } from "./useChat";

const RECONNECT_BASE_MS = 2000;
const RECONNECT_MAX_MS = 60_000;

// Delay before reconnect attempt number `attempt` (0-based): 2s, 4s, 8s … capped at a minute.
export function reconnectDelay(attempt: number): number {
  return Math.min(RECONNECT_BASE_MS * 2 ** attempt, RECONNECT_MAX_MS);
}

interface AgentStatusPayload {
  busy?: boolean;
}

// backend/app/services/plan_service.py `_publish`: {type, version, source, turn_id, changed_task_ids}.
interface PlanChangedPayload {
  version?: number;
  source?: string;
  changed_task_ids?: number[];
}

// The version a `plan_changed` event announces, or null when the payload doesn't carry one.
export function parsePlanVersion(data: string): number | null {
  try {
    const payload = JSON.parse(data) as PlanChangedPayload;
    return typeof payload.version === "number" ? payload.version : null;
  } catch {
    return null;
  }
}

// Whether a `plan_changed` event for `eventVersion` needs a refetch when the cache already holds
// `cachedVersion`. This tab's own apply/undo/redo writes the response straight into the cache,
// and the server's event for that same version arrives right after — refetching then would
// download the whole (up to 500-task) plan again for identical data. Only an exact match is
// skipped: an undo elsewhere announces a LOWER version that still has to be fetched, so `<=`
// would be wrong. Unknown on either side → refetch.
export function shouldRefetchPlan(cachedVersion: number | undefined, eventVersion: number | null): boolean {
  if (cachedVersion === undefined || eventVersion === null) return true;
  return cachedVersion !== eventVersion;
}

// Who changed the plan (`agent`, `user`, `mcp`, …), or null when the payload doesn't say.
export function parsePlanSource(data: string): string | null {
  try {
    const payload = JSON.parse(data) as PlanChangedPayload;
    return typeof payload.source === "string" ? payload.source : null;
  } catch {
    return null;
  }
}

// How to show a change whose tasks are all off screen (see GanttView's `reveal`): scroll the chart
// to it when the user asked for it — the agent's turn, or an edit this tab itself made (`local`:
// an undo, the task card, a new task) — and only offer to, with a notice, when it came from
// elsewhere (another tab, an external MCP client), so the chart doesn't jump under someone looking
// at it. Undo/redo carry the source of whoever pressed them, so "this tab" is told by `local`.
export type RevealMode = "scroll" | "notice";
export function revealMode(source: string | null, local: boolean): RevealMode {
  if (source === "agent") return "scroll";
  return local && source !== "mcp" ? "scroll" : "notice";
}

// What a `plan_changed` event tells the app besides the ids to highlight.
export interface PlanChange {
  source: string | null;
  version: number | null;
  // This tab already holds the announced version: it wrote it itself (or already fetched it).
  local: boolean;
}

// Sources that replace the whole plan: every task id is "changed", so highlighting them would
// just paint the entire chart in the highlight colour.
const REPLACING_SOURCES = new Set(["import", "reset", "seed"]);

// Pure so it's easy to unit test: parses the SSE `plan_changed` event's `data` string and
// returns the task ids to highlight — `[]` for a whole-plan replacement, and for
// malformed/missing data instead of throwing.
export function parsePlanChanged(data: string): number[] {
  try {
    const payload = JSON.parse(data) as PlanChangedPayload;
    if (payload.source && REPLACING_SOURCES.has(payload.source)) return [];
    return Array.isArray(payload.changed_task_ids) ? payload.changed_task_ids : [];
  } catch {
    return [];
  }
}

// Opens the session-wide live event stream (GET /api/events): `agent_status` toggles the busy
// flag surfaced here, `plan_changed` invalidates the plan query and reports the ids the change
// touched (so the Gantt can pulse them — this fires for every source: agent, user, mcp, undo,
// not just the tab that made the change; import/reset report none). On a stream error it closes
// and, after a growing delay, re-establishes the session, refetches the plan (events may have
// been missed) and reconnects. If the session can't be re-established (server down, per-IP
// session limit) it just waits longer: refetching then would restart the plan query and hide its
// error behind "loading", and a fixed short delay would hammer the server.
export function useSessionEvents(onPlanChanged: (ids: number[], change: PlanChange) => void): { agentBusy: boolean } {
  const queryClient = useQueryClient();
  const [agentBusy, setAgentBusy] = useState(false);
  const onPlanChangedRef = useRef(onPlanChanged);

  useEffect(() => {
    onPlanChangedRef.current = onPlanChanged;
  }, [onPlanChanged]);

  useEffect(() => {
    let source: EventSource | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let stopped = false;

    const handleAgentStatus = (event: Event) => {
      try {
        const payload = JSON.parse((event as MessageEvent<string>).data) as AgentStatusPayload;
        setAgentBusy(Boolean(payload.busy));
      } catch {
        /* malformed payload, ignore */
      }
    };

    const handlePlanChanged = (event: Event) => {
      const data = (event as MessageEvent<string>).data;
      const version = parsePlanVersion(data);
      const local = !shouldRefetchPlan(cachedPlanVersion(queryClient), version);
      if (!local) void queryClient.invalidateQueries({ queryKey: PLAN_KEY });
      onPlanChangedRef.current(parsePlanChanged(data), { source: parsePlanSource(data), version, local });
    };

    // A mass deletion requested by an external MCP client waits for approval in the app; the
    // banner re-reads GET /api/plan/confirmation whenever one appears or gets resolved.
    const handleConfirmation = () => {
      void queryClient.invalidateQueries({ queryKey: CONFIRMATION_KEY });
    };

    // Another tab (or this one) started a new conversation: the chat shows only the current one.
    const handleChatReset = () => {
      void queryClient.invalidateQueries({ queryKey: CHAT_HISTORY_KEY });
      void queryClient.invalidateQueries({ queryKey: CHAT_CONVERSATIONS_KEY });
    };

    const detach = (es: EventSource) => {
      es.removeEventListener("agent_status", handleAgentStatus);
      es.removeEventListener("plan_changed", handlePlanChanged);
      es.removeEventListener("confirmation_pending", handleConfirmation);
      es.removeEventListener("confirmation_resolved", handleConfirmation);
      es.removeEventListener("chat_reset", handleChatReset);
    };

    let attempt = 0;

    const scheduleReconnect = () => {
      reconnectTimer = setTimeout(() => {
        ensureSession().then(
          () => {
            if (stopped) return;
            void queryClient.invalidateQueries({ queryKey: PLAN_KEY });
            connect();
          },
          () => {
            if (!stopped) scheduleReconnect();
          },
        );
      }, reconnectDelay(attempt++));
    };

    const connect = () => {
      if (stopped) return;
      const es = new EventSource("/api/events");
      source = es;
      es.addEventListener("agent_status", handleAgentStatus);
      es.addEventListener("plan_changed", handlePlanChanged);
      es.addEventListener("confirmation_pending", handleConfirmation);
      es.addEventListener("confirmation_resolved", handleConfirmation);
      es.addEventListener("chat_reset", handleChatReset);
      es.onopen = () => {
        attempt = 0;
      };
      es.onerror = () => {
        detach(es);
        es.close();
        if (!stopped) scheduleReconnect();
      };
    };

    connect();

    return () => {
      stopped = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (source) {
        detach(source);
        source.close();
      }
    };
  }, [queryClient]);

  return { agentBusy };
}

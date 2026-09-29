import type {
  ApplyResponse,
  ChatConversation,
  ChatMessage,
  ImportFailure,
  ImportSuccess,
  McpTokenResponse,
  McpTokenStatus,
  PendingConfirmation,
  MetaResponse,
  Operation,
  PlanResponse,
  TaskHistoryEntry,
} from "./types";
import { toISODate } from "@/lib/dates";

export class ApiError extends Error {
  status: number;
  code: string;
  details?: unknown;

  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

// `fetch` rejects with a bare TypeError ("Failed to fetch" / "NetworkError when attempting to
// fetch resource") when the server is unreachable — that browser text used to reach the chat's
// error line and the "Не удалось загрузить план" screen as is. Turn it into our error envelope;
// an AbortError (the caller cancelled) passes through untouched.
export async function apiFetch(input: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(input, init);
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(0, "network_error", "Нет связи с сервером. Проверьте подключение и попробуйте ещё раз.");
  }
}

// Query retry policy (QueryClient default): a 4xx won't change on retry — and retrying a 429
// rate limit (e.g. the per-IP new-session limit) only hammers the server while the UI keeps
// showing "loading" instead of the server's message. Server/network errors: up to 3 retries.
export function retryUnlessClientError(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status >= 400 && error.status < 500) return false;
  return failureCount < 3;
}

// The backend names the file after this date (its own clock is UTC, a day off near midnight).
export const exportUrl = (now: Date = new Date()) => `/api/plan/export?today=${toISODate(now)}`;

// One POST /api/session at a time: on a fresh visit the plan query, the chat history query and
// the EventSource all hit 401 together, and each would otherwise create its own session (three
// Set-Cookie races, three orphan rows, the per-IP session limit burnt 3x faster). Concurrent
// callers share the in-flight request; a later call after it settles starts a new one.
let sessionInFlight: Promise<void> | null = null;

export function ensureSession(): Promise<void> {
  if (!sessionInFlight) {
    sessionInFlight = (async () => {
      const res = await apiFetch("/api/session", { method: "POST" });
      if (!res.ok) throw await toError(res);
    })().finally(() => {
      sessionInFlight = null;
    });
  }
  return sessionInFlight;
}

// Parses the backend's error envelope `{error: {code, message, details?}}` (spec §10); shared
// with chatStream.ts so both request paths report errors identically.
export async function toError(res: Response): Promise<ApiError> {
  const body = await res.json().catch(() => null);
  const err = body?.error;
  return new ApiError(res.status, err?.code ?? "http_error", err?.message ?? `Ошибка ${res.status}`, err?.details);
}

export async function request<T>(path: string, init?: RequestInit, retried = false): Promise<T> {
  const res = await apiFetch(path, init);
  if (res.status === 401 && !retried) {
    const err = await toError(res.clone());
    if (err.code === "no_session") {
      await ensureSession();
      return request<T>(path, init, true);
    }
  }
  if (!res.ok) throw await toError(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, {
    method: "POST",
    headers: body === undefined ? undefined : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

// `expectedVersion` is the plan version the edit was made against (optimistic concurrency):
// the backend refuses with 409 `version_conflict` if another tab / the agent / MCP has moved
// the plan on since, instead of silently overwriting their change. `undefined` skips the check.
export const api = {
  getPlan: () => request<PlanResponse>("/api/plan"),
  applyOps: (ops: Operation[], expectedVersion?: number) =>
    post<ApplyResponse>("/api/plan/operations", { ops, expected_version: expectedVersion ?? null }),
  undo: (expectedVersion?: number) =>
    post<PlanResponse>("/api/plan/undo", { expected_version: expectedVersion ?? null }),
  redo: (expectedVersion?: number) =>
    post<PlanResponse>("/api/plan/redo", { expected_version: expectedVersion ?? null }),
  reset: () => post<PlanResponse>("/api/plan/reset"),
  chatHistory: () => request<ChatMessage[]>("/api/chat/history"),
  newConversation: () => post<{ id: string }>("/api/chat/conversations"),
  conversations: () => request<ChatConversation[]>("/api/chat/conversations"),
  conversation: (id: string) => request<ChatMessage[]>(`/api/chat/conversations/${encodeURIComponent(id)}`),
  taskHistory: (id: number) => request<TaskHistoryEntry[]>(`/api/plan/tasks/${id}/history`),
  meta: () => request<MetaResponse>("/api/meta"),
  deleteSession: () => request<void>("/api/session", { method: "DELETE" }),
  mcpTokenStatus: () => request<McpTokenStatus>("/api/mcp-token"),
  createMcpToken: () => post<McpTokenResponse>("/api/mcp-token"),
  revokeMcpToken: () => request<void>("/api/mcp-token", { method: "DELETE" }),
  pendingConfirmation: () => request<PendingConfirmation | null>("/api/plan/confirmation"),
  approveConfirmation: (id: string) => post<unknown>(`/api/plan/confirmation/${encodeURIComponent(id)}/approve`),
  rejectConfirmation: (id: string) => post<unknown>(`/api/plan/confirmation/${encodeURIComponent(id)}/reject`),
  async importPlan(
    file: File,
    projectStart: string,
    expectedVersion?: number,
  ): Promise<ImportSuccess | ImportFailure> {
    const form = new FormData();
    form.append("file", file);
    form.append("project_start", projectStart);
    // The version the user was looking at: if the plan moved on meanwhile (another tab, the
    // agent, MCP), the server refuses with 409 instead of silently replacing the newer plan.
    if (expectedVersion != null) form.append("expected_version", String(expectedVersion));
    const send = () => apiFetch("/api/plan/import", { method: "POST", body: form });
    let res = await send();
    if (res.status === 401) {
      await ensureSession();
      res = await send();
    }
    if (res.ok || res.status === 422) {
      const body = await res.clone().json().catch(() => null);
      if (body && "ok" in body) return body;
    }
    throw await toError(res);
  },
};

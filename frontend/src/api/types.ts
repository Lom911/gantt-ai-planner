// Mirrors backend/app/domain/{models,scheduler,diff,operations}.py and spec §5, §7, §9, §10.
// Dates are ISO "YYYY-MM-DD" strings on the wire (pydantic `date` fields serialize this way).

export interface Task {
  id: number;
  name: string;
  description: string;
  assignee: string | null;
  duration: number;
  constraint_start: string | null;
}

export interface ScheduledTask extends Task {
  start: string;
  end: string;
  slack: number;
  is_critical: boolean;
  // "project_start" | "constraint" | "predecessor:<id>"
  constrained_by: string;
  overallocated_with: number[];
}

export interface Dependency {
  predecessor_id: number;
  successor_id: number;
  lag: number;
}

export interface ScheduledPlan {
  project_start: string;
  project_end: string;
  last_id: number;
  tasks: ScheduledTask[];
  dependencies: Dependency[];
  critical_path: number[];
}

// GET /api/plan (spec §10): "Вычисленный план: version, can_undo, can_redo, agent_busy".
export interface PlanResponse {
  version: number;
  plan: ScheduledPlan;
  can_undo: boolean;
  can_redo: boolean;
  agent_busy: boolean;
}

export type ChangeField =
  | "created"
  | "deleted"
  | "name"
  | "description"
  | "assignee"
  | "duration"
  | "constraint_start"
  | "predecessors"
  | "start"
  | "end";

export interface Change {
  task_id: number;
  task_name: string;
  field: ChangeField;
  before?: string | number | null;
  after?: string | number | null;
}

// POST /api/plan/operations (spec §10): "возвращает план и diff" — PlanResponse plus the
// per-batch result fields mirroring backend ApplyResponse (backend/app/api/schemas.py).
export interface ApplyResponse extends PlanResponse {
  changes: Change[];
  warnings: string[];
  summary: string;
  created_task_ids: number[];
}

// backend/app/excel/parse.py: ImportIssue / ImportResult.
export interface ImportIssue {
  row: number | null;
  message: string;
}

// backend/app/api/schemas.py: ImportSuccess — `plan` is the full nested PlanResponse (version
// lives on it, not at the top level).
export interface ImportSuccess {
  ok: true;
  plan: PlanResponse;
  warnings: ImportIssue[];
}

export interface ImportFailure {
  ok: false;
  errors: ImportIssue[];
  warnings: ImportIssue[];
}

// backend/app/domain/operations.py: Operation union (discriminated by `op`).
export interface PredRef {
  id: number;
  lag?: number;
}

export interface AddTaskOp {
  op: "add_task";
  name: string;
  description?: string;
  assignee?: string | null;
  duration: number;
  predecessors?: PredRef[];
  after_id?: number | null;
}

export interface UpdateTaskOp {
  op: "update_task";
  id: number;
  name?: string;
  description?: string;
  assignee?: string | null;
  duration?: number;
}

export interface MoveTaskOp {
  op: "move_task";
  id: number;
  start_date?: string | null;
  shift_days?: number | null;
}

export interface ClearConstraintOp {
  op: "clear_constraint";
  id: number;
}

export interface SetDependenciesOp {
  op: "set_dependencies";
  id: number;
  predecessors: PredRef[];
}

export interface AddDependencyOp {
  op: "add_dependency";
  predecessor_id: number;
  successor_id: number;
  lag?: number;
}

export interface RemoveDependencyOp {
  op: "remove_dependency";
  predecessor_id: number;
  successor_id: number;
}

export interface DeleteTaskOp {
  op: "delete_task";
  id: number;
}

export interface SetProjectStartOp {
  op: "set_project_start";
  date: string;
}

export type Operation =
  | AddTaskOp
  | UpdateTaskOp
  | MoveTaskOp
  | ClearConstraintOp
  | SetDependenciesOp
  | AddDependencyOp
  | RemoveDependencyOp
  | DeleteTaskOp
  | SetProjectStartOp;

// spec §10 GET /api/chat/history + §7 chat meta (diff summaries).
export type ChatRole = "user" | "assistant" | "system";

export interface ChatMessage {
  id: number;
  role: ChatRole;
  content: string;
  created_at: string;
  meta: {
    summary?: string;
    changes?: Change[];
    error?: string;
  };
}

// spec §7 / backend/app/agent/loop.py: chat SSE event stream.
export type ChatEvent =
  | { type: "text_delta"; text: string }
  | { type: "tool_started"; name: string }
  | { type: "tool_finished"; name: string; ok: boolean; summary: string | null }
  | { type: "plan_changed"; version: number | null }
  | { type: "done"; turn_id: string; summary: string; changes: Change[] }
  | { type: "error"; code: string; message: string };

// spec §8 / backend/app/api/schemas.py McpTokenResponse: POST /api/mcp-token.
// `token` is shown once; only its sha256 + prefix are stored server-side.
export interface McpTokenResponse {
  token: string;
  expires_at: string;
  url: string;
  claude_code_command: string;
  claude_desktop_config: {
    mcpServers: Record<string, { url: string; headers: Record<string, string> }>;
  };
}

// spec §6: version.source, also used as-is by task history entries below.
export type VersionSource = "seed" | "import" | "user" | "agent" | "mcp" | "reset";

// GET /api/plan/tasks/{id}/history (spec §10, §6: "вычисляется из diff сохранённых версий") —
// backend/app/services/plan_service.py `task_history`, newest version first.
export interface TaskHistoryEntry {
  version: number;
  source: VersionSource;
  created_at: string;
  summary: string;
  changes: Change[];
}

// GET /api/meta (added alongside this task by a parallel backend change): exposes whether the
// LLM is a real Anthropic key or the deterministic fake used when none is configured, so the UI
// can show a "demo mode" hint instead of silently behaving as if the agent understands anything.
export interface MetaResponse {
  llm_mode: "anthropic" | "openrouter" | "fake";
  model: string | null;
}

// spec §10: unified error envelope `{error: {code, message, details?}}`.
export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    details?: unknown;
  };
}

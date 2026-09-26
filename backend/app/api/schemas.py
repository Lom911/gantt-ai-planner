from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from app.domain.diff import Change
from app.domain.operations import OperationBatch
from app.domain.scheduler import ScheduledPlan
from app.excel.parse import ImportIssue
from app.services.plan_service import PlanState


class PlanResponse(BaseModel):
    version: int
    can_undo: bool
    can_redo: bool
    agent_busy: bool
    plan: ScheduledPlan


class VersionedRequest(BaseModel):
    # Optimistic concurrency: the plan version the client's edit is based on. When set and the
    # server has moved on (another tab, the agent, MCP), the request is refused with 409
    # `version_conflict` instead of silently overwriting the newer state. Omit to skip the check.
    expected_version: int | None = None


class ApplyRequest(VersionedRequest):
    ops: OperationBatch


class ApplyResponse(PlanResponse):
    changes: list[Change]
    warnings: list[str]
    summary: str
    created_task_ids: list[int]


class ImportSuccess(BaseModel):
    ok: Literal[True]
    plan: PlanResponse
    warnings: list[ImportIssue]


class ImportFailure(BaseModel):
    ok: Literal[False]
    errors: list[ImportIssue]
    warnings: list[ImportIssue]


class McpTokenResponse(BaseModel):
    token: str
    expires_at: datetime
    url: str
    claude_code_command: str
    claude_desktop_config: dict[str, Any]


class McpTokenStatus(BaseModel):
    # Never the token itself: only the short prefix stored next to its hash. An expired (but
    # not revoked) token is still described, with active=False, so the UI can say it expired.
    active: bool
    prefix: str | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None
    last_used_at: datetime | None = None


def to_plan_response(state: PlanState, busy: bool) -> PlanResponse:
    return PlanResponse(
        version=state.version,
        can_undo=state.can_undo,
        can_redo=state.can_redo,
        agent_busy=busy,
        plan=state.scheduled,
    )

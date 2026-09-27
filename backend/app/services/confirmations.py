"""Pending mass-delete confirmations (plan_confirmations): what PlanService.apply stores when a
batch needs the user's confirmation, and what a later `confirmed=true` must match.

A confirmation is bound to the exact batch through `digest`: the canonical JSON (sorted keys,
defaults filled in, operations in their given order — order matters, add_task ids follow it)
of the operations *and the plan version they were checked against*, so neither a different
batch nor the same batch on a plan that has changed since (other tasks may now carry those
ids) can ride on an earlier approval.
"""

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from app.db.models import PlanConfirmationRow
from app.domain.models import Plan
from app.domain.operations import DeleteTask, Operation

CONFIRMATION_TTL = timedelta(minutes=10)
_SUMMARY_NAMES = 10  # tasks named in the summary; the rest are counted
_NAME_CHARS = 60

Origin = Literal["agent", "mcp"]
Resolution = Literal["consumed", "rejected", "expired", "superseded"]


@dataclass(frozen=True)
class PendingConfirmation:
    id: uuid.UUID
    origin: Origin
    summary: str
    count: int
    expires_at: datetime
    approved: bool

    @classmethod
    def from_row(cls, row: PlanConfirmationRow) -> "PendingConfirmation":
        origin: Origin = "agent" if row.origin == "agent" else "mcp"
        return cls(
            id=row.id,
            origin=origin,
            summary=row.summary,
            count=row.task_count,
            expires_at=row.expires_at,
            approved=row.approved_at is not None,
        )

    def pending_event(self) -> dict[str, Any]:
        return {
            "type": "confirmation_pending",
            "id": str(self.id),
            "origin": self.origin,
            "summary": self.summary,
            "count": self.count,
            "expires_at": iso_utc(self.expires_at),
        }


def iso_utc(moment: datetime) -> str:
    """ISO 8601 in UTC with a «Z», exactly as the API responses (pydantic) render datetimes, so
    the same confirmation reads the same in an event and in GET /api/plan/confirmation."""
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def resolved_event(confirmation_id: uuid.UUID, result: str) -> dict[str, Any]:
    """`result`: approved (still pending, now usable) | consumed | rejected | expired |
    superseded."""
    return {"type": "confirmation_resolved", "id": str(confirmation_id), "result": result}


def batch_digest(version: int, ops: Sequence[Operation]) -> str:
    payload = {"version": version, "operations": [op.model_dump(mode="json") for op in ops]}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _clip(name: str) -> str:
    name = " ".join(name.split())
    return name if len(name) <= _NAME_CHARS else name[: _NAME_CHARS - 1] + "…"


def deletion_summary(plan: Plan, ops: Sequence[Operation]) -> tuple[str, int]:
    """What the user is asked to confirm, in Russian, and how many existing tasks it deletes:
    «Удалить задачи: №1 «…», №2 «…» (всего 6)»."""
    names = {t.id: t.name for t in plan.tasks}
    deleted: list[int] = []
    for op in ops:
        if isinstance(op, DeleteTask) and op.id in names and op.id not in deleted:
            deleted.append(op.id)
    listed = ", ".join(f"№{i} «{_clip(names[i])}»" for i in deleted[:_SUMMARY_NAMES])
    if len(deleted) > _SUMMARY_NAMES:
        listed += f" и ещё {len(deleted) - _SUMMARY_NAMES}"
    summary = f"Удалить задачи: {listed} (всего {len(deleted)})"
    others = len(ops) - len(deleted)
    if others:
        summary += f"; прочих операций в пакете: {others}"
    return summary, len(deleted)

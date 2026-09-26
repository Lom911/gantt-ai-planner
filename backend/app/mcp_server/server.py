import uuid
from collections.abc import Callable
from datetime import date
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import TokenVerifier
from fastmcp.server.dependencies import get_access_token

from app.domain.diff import format_predecessors
from app.domain.errors import DomainError
from app.domain.operations import OperationBatch
from app.domain.render import render_plan_table
from app.domain.scheduler import ScheduledPlan, ScheduledTask
from app.mcp_server.context import current_session, current_turn
from app.services.plan_service import PlanService, PlanState

INSTRUCTIONS = (
    "Инструменты редактирования плана-графика (диаграмма Ганта). Даты считаются автоматически "
    "по зависимостям «окончание→начало», рабочие дни пн–пт. Все правки одного запроса "
    "отправляйте одним вызовом apply_operations: пакет атомарный."
)


def resolve_session_id() -> uuid.UUID:
    token = get_access_token()
    if token is not None and token.claims.get("session_id"):
        return uuid.UUID(str(token.claims["session_id"]))
    sid = current_session.get()
    if sid is None:
        raise ToolError("Нет активной сессии")
    return sid


def _task_dict(sp: ScheduledPlan, t: ScheduledTask) -> dict[str, Any]:
    return {
        "id": t.id,
        "name": t.name,
        "assignee": t.assignee,
        "duration": t.duration,
        "start": t.start.isoformat(),
        "end": t.end.isoformat(),
        "slack": t.slack,
        "is_critical": t.is_critical,
        "predecessors": format_predecessors(sp.dependencies, t.id),
        "constraint_start": t.constraint_start.isoformat() if t.constraint_start else None,
        "overallocated_with": t.overallocated_with,
    }


def build_mcp(
    service: PlanService, *, today: Callable[[], date], auth: TokenVerifier | None = None
) -> FastMCP:
    # mask_error_details: an unexpected exception inside a tool reaches the MCP client (and the
    # LLM's context) as a generic error, not its internal text; ToolError messages still pass.
    mcp = FastMCP("planner", instructions=INSTRUCTIONS, auth=auth, mask_error_details=True)

    async def state() -> PlanState:
        try:
            return await service.get_state(resolve_session_id())
        except DomainError as exc:
            raise ToolError(f"{exc.code}: {exc.message}") from exc

    @mcp.tool
    async def get_plan() -> str:
        """Текущий план целиком: компактная таблица задач с датами, резервом и флагами."""
        return render_plan_table((await state()).scheduled, today())

    @mcp.tool
    async def find_tasks(
        query: str | None = None, assignee: str | None = None, critical_only: bool = False
    ) -> list[dict[str, Any]]:
        """Поиск задач: подстрока в названии/описании (query), подстрока имени исполнителя,
        только критические."""
        sp = (await state()).scheduled
        q, a = (query or "").casefold(), (assignee or "").casefold()
        return [
            _task_dict(sp, t)
            for t in sp.tasks
            if (not q or q in t.name.casefold() or q in t.description.casefold())
            and (not a or a in (t.assignee or "").casefold())
            and (not critical_only or t.is_critical)
        ]

    @mcp.tool
    async def get_task(id: int) -> dict[str, Any]:
        """Одна задача: поля, вычисленные даты, предшественники, последователи,
        чем ограничено начало."""
        sp = (await state()).scheduled
        try:
            t = sp.task(id)
        except KeyError as exc:
            raise ToolError(f"not_found: задачи {id} нет в плане") from exc
        data = _task_dict(sp, t)
        data["description"] = t.description
        data["successors"] = [d.successor_id for d in sp.dependencies if d.predecessor_id == id]
        data["constrained_by"] = t.constrained_by
        return data

    @mcp.tool
    async def get_resource_load(assignee: str | None = None) -> list[dict[str, Any]]:
        """Загрузка исполнителей: их задачи по датам и пары пересекающихся задач (перегрузка)."""
        sp = (await state()).scheduled
        people: dict[str, dict[str, Any]] = {}
        for t in sp.tasks:
            if not t.assignee or (assignee and assignee.casefold() not in t.assignee.casefold()):
                continue
            # Same grouping key as the scheduler's overallocation check (strip + casefold), so
            # «Иван Петров» and «иван петров» are one person whose conflicts point at tasks
            # in their own list, not at a phantom second person.
            key = t.assignee.strip().casefold()
            p = people.setdefault(key, {"assignee": t.assignee, "tasks": [], "conflicts": []})
            p["tasks"].append({"id": t.id, "start": t.start.isoformat(), "end": t.end.isoformat()})
            for other in t.overallocated_with:
                pair = sorted((t.id, other))
                if pair not in p["conflicts"]:
                    p["conflicts"].append(pair)
        return list(people.values())

    @mcp.tool
    async def apply_operations(
        operations: OperationBatch, confirmed: bool = False
    ) -> dict[str, Any]:
        """Атомарно применить пакет операций. Новые задачи получают id по порядку add_task,
        начиная со «следующего свободного id» из get_plan — на них можно ссылаться в этом же
        пакете. move_task задаёт ограничение «не раньше даты», последователи сдвигаются
        автоматически. Не больше 200 операций в пакете. При ошибке confirmation_required
        спросите пользователя и повторите с confirmed=true."""
        sid = resolve_session_id()
        turn = current_turn.get()
        try:
            out = await service.apply(
                sid,
                operations,
                source="agent" if turn else "mcp",
                turn_id=turn,
                confirmed=confirmed,
            )
        except DomainError as exc:
            raise ToolError(f"{exc.code}: {exc.message}") from exc
        return {
            "summary": out.summary,
            "version": out.state.version,
            "changes": [c.model_dump() for c in out.changes[:100]],
            "warnings": out.warnings,
            "created_task_ids": out.created_task_ids,
            "project_end": out.state.scheduled.project_end.isoformat(),
        }

    @mcp.tool
    async def undo() -> dict[str, Any]:
        """Отменить последнее изменение плана (ход агента отменяется целиком)."""
        sid = resolve_session_id()
        try:
            s = await service.undo(sid, source="agent" if current_turn.get() else "mcp")
        except DomainError as exc:
            raise ToolError(f"{exc.code}: {exc.message}") from exc
        return {"version": s.version, "project_end": s.scheduled.project_end.isoformat()}

    return mcp

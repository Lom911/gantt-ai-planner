"""Plan operations. A batch is applied to a copy and validated as a whole (atomic)."""

from collections.abc import Sequence
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError, model_validator

from app.domain.calendar import add_workdays, next_workday
from app.domain.diff import Change, diff_plans
from app.domain.errors import OperationError, PlanValidationError
from app.domain.models import MAX_DEPENDENCIES, MAX_LAG, MAX_TASKS, Dependency, Plan, Task
from app.domain.scheduler import ScheduledPlan, schedule


class PredRef(BaseModel):
    id: int
    lag: int = Field(default=0, ge=0, le=MAX_LAG)


class AddTask(BaseModel):
    op: Literal["add_task"]
    name: str
    description: str = ""
    assignee: str | None = None
    duration: int = Field(ge=1, le=999)
    predecessors: list[PredRef] = Field(
        default_factory=list, description="Задачи, после окончания которых начинается новая"
    )
    after_id: int | None = Field(
        default=None,
        description="id задачи, после которой поставить новую в списке; без него — в конец",
    )


class DeleteTask(BaseModel):
    """Удалить задачу. Связи через неё сохраняются: каждый её предшественник становится
    предшественником каждого её последователя, лаги двух связей складываются (не больше
    365 дней)."""  # also the op's description in the JSON schema; 365 = MAX_LAG

    op: Literal["delete_task"]
    id: int


class UpdateTask(BaseModel):
    op: Literal["update_task"]
    id: int
    name: str | None = None
    description: str | None = None
    assignee: str | None = None  # "" clears
    duration: int | None = Field(default=None, ge=1, le=999)


class MoveTask(BaseModel):
    op: Literal["move_task"]
    id: int
    start_date: date | None = None
    shift_days: int | None = Field(default=None, ge=-2000, le=2000)

    @model_validator(mode="after")
    def _exactly_one(self) -> "MoveTask":
        if (self.start_date is None) == (self.shift_days is None):
            raise ValueError("укажите ровно одно из полей start_date или shift_days")
        return self


class ClearConstraint(BaseModel):
    op: Literal["clear_constraint"]
    id: int


class SetDependencies(BaseModel):
    op: Literal["set_dependencies"]
    id: int
    predecessors: list[PredRef]


class AddDependency(BaseModel):
    op: Literal["add_dependency"]
    predecessor_id: int
    successor_id: int
    lag: int = Field(default=0, ge=0, le=MAX_LAG)


class RemoveDependency(BaseModel):
    op: Literal["remove_dependency"]
    predecessor_id: int
    successor_id: int


class SetProjectStart(BaseModel):
    op: Literal["set_project_start"]
    date: date


Operation = Annotated[
    AddTask
    | UpdateTask
    | MoveTask
    | ClearConstraint
    | SetDependencies
    | AddDependency
    | RemoveDependency
    | DeleteTask
    | SetProjectStart,
    Field(discriminator="op"),
]
operations_adapter: TypeAdapter[list[Operation]] = TypeAdapter(list[Operation])

# Upper bound on one batch. The whole batch runs synchronously on the single event loop, so an
# unbounded list would let any visitor stall every session; 200 is far above what a UI edit or an
# agent step needs (a bulk move of every task of one assignee is a few dozen ops). The limit is
# advertised as `maxItems` in the API/MCP schemas (OperationBatch) and enforced here, before any
# work, so the rejection carries a Russian message instead of a generic validation error.
MAX_BATCH_OPS = 200
OperationBatch = Annotated[list[Operation], Field(json_schema_extra={"maxItems": MAX_BATCH_OPS})]


def check_batch_size(ops: Sequence[Operation]) -> None:
    if len(ops) > MAX_BATCH_OPS:
        raise OperationError(
            f"В одном пакете не больше {MAX_BATCH_OPS} операций (получено {len(ops)}). "
            "Разбейте изменения на несколько пакетов."
        )


class ApplyResult(BaseModel):
    plan: Plan
    scheduled: ScheduledPlan
    changes: list[Change]
    warnings: list[str]
    created_task_ids: list[int]


def requires_confirmation(plan: Plan, ops: Sequence[Operation]) -> bool:
    existing = {t.id for t in plan.tasks}
    deleted = {op.id for op in ops if isinstance(op, DeleteTask) and op.id in existing}
    return len(deleted) > 5 or (bool(existing) and len(deleted) * 2 > len(existing))


def apply_operations(
    plan: Plan, ops: Sequence[Operation], before: ScheduledPlan | None = None
) -> ApplyResult:
    """`before` is `schedule(plan)`; a caller that already has it passes it in so a 500-task
    plan isn't scheduled twice per request."""
    check_batch_size(ops)
    if before is None:
        before = schedule(plan)
    work = plan.model_copy(deep=True)
    created: list[int] = []
    moved: list[int] = []
    for index, op in enumerate(ops):
        try:
            _apply_one(work, op, created, moved, before)
        except OperationError as exc:
            exc.index = index
            exc.message = f"Операция {index + 1} ({op.op}): {exc.message}"
            raise
        except (ValidationError, PlanValidationError, ValueError) as exc:
            msg = f"Операция {index + 1} ({op.op}): {_short(exc)}"
            raise OperationError(msg, index=index) from exc
    if len(work.tasks) > MAX_TASKS:
        raise OperationError(f"В плане не может быть больше {MAX_TASKS} задач")
    if len(work.dependencies) > MAX_DEPENDENCIES:
        raise OperationError(f"В плане не может быть больше {MAX_DEPENDENCIES} связей")
    try:
        work = Plan.model_validate(work.model_dump())
        after = schedule(work)
    except PlanValidationError as exc:
        raise OperationError(exc.message, details=exc.details) from exc
    return ApplyResult(
        plan=work,
        scheduled=after,
        changes=diff_plans(before, after),
        warnings=_constraint_warnings(after, moved),
        created_task_ids=created,
    )


def _short(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
    if isinstance(exc, PlanValidationError):
        return exc.message
    return str(exc)


def _get(plan: Plan, task_id: int) -> Task:
    for task in plan.tasks:
        if task.id == task_id:
            return task
    raise OperationError(f"задачи {task_id} нет в плане")


def _upsert_dep(plan: Plan, pred: int, succ: int, lag: int) -> None:
    if pred == succ:
        raise OperationError(f"задача {succ} не может зависеть от самой себя")
    for i, d in enumerate(plan.dependencies):
        if d.predecessor_id == pred and d.successor_id == succ:
            new_lag = max(lag, d.lag)
            plan.dependencies[i] = Dependency(predecessor_id=pred, successor_id=succ, lag=new_lag)
            return
    plan.dependencies.append(Dependency(predecessor_id=pred, successor_id=succ, lag=lag))


def _shift_base(plan: Plan, task: Task, moved: list[int], before: ScheduledPlan) -> date:
    """Start a `shift_days` move is measured from.

    A shift is relative to the plan as it was when the batch started, NOT to the partly edited
    working copy: otherwise shifting a predecessor and its successor in one batch would move the
    successor twice (once via the cascade, once by its own shift). A task already moved earlier
    in this batch accumulates from the start that move asked for. Only a task created in this
    batch has no "before" start, so its base comes from the working copy's schedule.
    """
    if task.id in moved and task.constraint_start is not None:
        return task.constraint_start
    if any(t.id == task.id for t in before.tasks):
        return before.task(task.id).start
    return schedule(plan).task(task.id).start


def _apply_one(
    plan: Plan, op: Operation, created: list[int], moved: list[int], before: ScheduledPlan
) -> None:
    match op:
        case AddTask():
            new_id = plan.last_id + 1
            task = Task(
                id=new_id,
                name=op.name,
                description=op.description,
                assignee=op.assignee,
                duration=op.duration,
            )
            if op.after_id is not None:
                anchor = plan.tasks.index(_get(plan, op.after_id))
                plan.tasks.insert(anchor + 1, task)
            else:
                plan.tasks.append(task)
            plan.last_id = new_id
            for p in op.predecessors:
                _get(plan, p.id)
                _upsert_dep(plan, p.id, new_id, p.lag)
            created.append(new_id)
        case UpdateTask():
            task = _get(plan, op.id)
            data = task.model_dump()
            for field in op.model_fields_set - {"op", "id"}:
                data[field] = getattr(op, field)
            plan.tasks[plan.tasks.index(task)] = Task.model_validate(data)
        case MoveTask():
            task = _get(plan, op.id)
            if op.start_date is not None:
                target = op.start_date
            else:
                assert op.shift_days is not None
                target = add_workdays(_shift_base(plan, task, moved, before), op.shift_days)
            task.constraint_start = next_workday(target)
            moved.append(op.id)
        case ClearConstraint():
            _get(plan, op.id).constraint_start = None
        case SetDependencies():
            _get(plan, op.id)
            plan.dependencies = [d for d in plan.dependencies if d.successor_id != op.id]
            for p in op.predecessors:
                _get(plan, p.id)
                _upsert_dep(plan, p.id, op.id, p.lag)
        case AddDependency():
            _get(plan, op.predecessor_id)
            _get(plan, op.successor_id)
            _upsert_dep(plan, op.predecessor_id, op.successor_id, op.lag)
        case RemoveDependency():
            deps_before = len(plan.dependencies)
            plan.dependencies = [
                d
                for d in plan.dependencies
                if not (d.predecessor_id == op.predecessor_id and d.successor_id == op.successor_id)
            ]
            if len(plan.dependencies) == deps_before:
                raise OperationError(f"связи {op.predecessor_id} → {op.successor_id} нет")
        case DeleteTask():
            task = _get(plan, op.id)
            incoming = [d for d in plan.dependencies if d.successor_id == op.id]
            outgoing = [d for d in plan.dependencies if d.predecessor_id == op.id]
            plan.dependencies = [
                d for d in plan.dependencies if op.id not in (d.predecessor_id, d.successor_id)
            ]
            plan.tasks.remove(task)
            # Bridging A→B→C into A→C sums the lags; clamp so two large lags can't push the
            # bridge past MAX_LAG and turn a plain delete into a validation error.
            for a in incoming:
                for b in outgoing:
                    lag = min(a.lag + b.lag, MAX_LAG)
                    _upsert_dep(plan, a.predecessor_id, b.successor_id, lag)
        case SetProjectStart():
            plan.project_start = next_workday(op.date)


def _constraint_warnings(after: ScheduledPlan, moved: list[int]) -> list[str]:
    warnings: list[str] = []
    ids = {t.id for t in after.tasks}
    for tid in dict.fromkeys(moved):
        if tid not in ids:
            continue
        task = after.task(tid)
        if task.constraint_start is None:
            continue
        wanted = next_workday(task.constraint_start)
        if task.start <= wanted:
            continue
        if task.constrained_by.startswith("predecessor:"):
            pid = int(task.constrained_by.split(":")[1])
            why = f"ограничено предшественником {pid} «{after.task(pid).name}»"
        else:
            why = "ограничено датой старта проекта"
        warnings.append(
            f"Задача {tid} «{task.name}» не может начаться {wanted:%d.%m.%Y}: "
            f"начало {task.start:%d.%m.%Y}, {why}"
        )
    return warnings

import asyncio
import uuid
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import repo
from app.db.repo import VersionDiff, VersionMeta
from app.domain.diff import Change, diff_plans, summarize_changes
from app.domain.errors import ConfirmationRequired, DomainError
from app.domain.models import Plan
from app.domain.operations import (
    Operation,
    apply_operations,
    check_batch_size,
    requires_confirmation,
)
from app.domain.scheduler import ScheduledPlan, schedule
from app.domain.seed import build_demo_plan
from app.services.errors import (
    AgentBusy,
    NoSession,
    NotFound,
    NothingToRedo,
    NothingToUndo,
    PlanTooLarge,
    VersionConflict,
)
from app.services.events import EventBus
from app.services.locks import SessionLocks
from app.services.sessions import hash_token, new_token

Source = Literal["seed", "import", "user", "agent", "mcp", "reset"]
# last_seen_at only drives the idle-session cleanup (days), so it is refreshed at most this
# often instead of an UPDATE (and a WAL write) on every request.
TOUCH_INTERVAL = timedelta(minutes=10)
# Scheduled plans kept in memory, bounded by the size of their JSON snapshots. As Python objects
# a plan takes roughly 10x its JSON size, and the container has 300 MB (the load test peaked at
# ~245 MB with 8 MB here), so this stays small: ~200 demo-sized plans.
PLAN_CACHE_MAX_BYTES = 2_000_000
MCP_WAIT_SECONDS = 10.0

# Sources that REPLACE the whole plan rather than editing it in place. A task's history must
# never cross this boundary: comparing across it would diff tasks that merely share an id
# (e.g. "UI-кит и дизайн-система" pre-boundary vs. "Заказ грузового транспорта" post-boundary).
_BOUNDARY_SOURCES = frozenset({"import", "reset", "seed"})
_BOUNDARY_SUMMARIES: dict[str, str] = {
    "import": "Задача появилась при импорте плана",
    "reset": "Задача появилась при сбросе к демо-плану",
    "seed": "Задача появилась в демо-плане",
}


@dataclass(frozen=True)
class PlanState:
    version: int
    plan: Plan
    scheduled: ScheduledPlan
    can_undo: bool
    can_redo: bool


@dataclass(frozen=True)
class ApplyOutcome:
    state: PlanState
    changes: list[Change]
    warnings: list[str]
    created_task_ids: list[int]
    summary: str


def apply_summary(changes: list[Change], before_start: date, after_start: date) -> str:
    """`summarize_changes` plus the project start, which `diff_plans` (per-task) can't see: a
    `set_project_start` that moves no task (every root task constrained later, or an empty plan)
    is still a real edit and must not read as «Без изменений»."""
    if before_start == after_start:
        return summarize_changes(changes)
    note = f"старт проекта перенесён на {after_start:%d.%m.%Y}"
    if not changes:
        return note[0].upper() + note[1:]
    return f"{summarize_changes(changes)}; {note}"


def plan_json_size(plan: Plan) -> int:
    """Bytes of the plan as compact UTF-8 JSON: what `max_plan_bytes` limits per snapshot."""
    return len(plan.model_dump_json().encode())


def _index(meta: list[VersionMeta], current: int) -> int:
    return next(i for i, m in enumerate(meta) if m.version_no == current)


def undo_target(meta: list[VersionMeta], current: int) -> int | None:
    i = _index(meta, current)
    turn = meta[i].turn_id
    if turn is not None:
        while i > 0 and meta[i - 1].turn_id == turn:
            i -= 1
    return meta[i - 1].version_no if i > 0 else None


def redo_target(meta: list[VersionMeta], current: int) -> int | None:
    i = _index(meta, current)
    if i + 1 >= len(meta):
        return None
    i += 1
    turn = meta[i].turn_id
    if turn is not None:
        while i + 1 < len(meta) and meta[i + 1].turn_id == turn:
            i += 1
    return meta[i].version_no


class PlanService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        bus: EventBus,
        locks: SessionLocks,
        *,
        max_versions: int = 50,
        max_plan_bytes: int = 1_500_000,
        today: Callable[[], date] = date.today,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.bus = bus
        self.locks = locks
        self._max_versions = max_versions
        self._max_plan_bytes = max_plan_bytes
        self._today = today
        # plan_versions.id -> (plan, its schedule, snapshot bytes). Stored versions never change
        # and their ids are never reused, so an entry can't go stale; it only saves re-reading,
        # re-parsing and re-scheduling the current snapshot on every read (the load test's main
        # per-request cost after DB round trips). Callers must not mutate the objects —
        # apply_operations works on a deep copy.
        self._plan_cache: OrderedDict[int, tuple[Plan, ScheduledPlan, int]] = OrderedDict()
        self._plan_cache_bytes = 0

    async def create_session(self) -> tuple[str, uuid.UUID]:
        token = new_token()
        plan = build_demo_plan(self._today())
        async with self.sessionmaker() as db, db.begin():
            row = await repo.create_session(db, hash_token(token))
            await repo.add_version(
                db,
                session_id=row.id,
                version_no=1,
                snapshot=plan.model_dump(mode="json"),
                source="seed",
                turn_id=None,
                summary="Демо-план",
                diff=[],
            )
            row.current_version = 1
        return token, row.id

    async def resolve_session(self, token: str) -> uuid.UUID | None:
        async with self.sessionmaker() as db, db.begin():
            row = await repo.get_session_by_token_hash(db, hash_token(token))
            if row is None:
                return None
            if row.last_seen_at < datetime.now(UTC) - TOUCH_INTERVAL:
                await repo.touch_session(db, row.id)
            return row.id

    async def delete_session(self, session_id: uuid.UUID) -> None:
        async with self.sessionmaker() as db, db.begin():
            await repo.delete_session(db, session_id)
        self.locks.forget(session_id)
        self.bus.forget(session_id)

    async def get_state(self, session_id: uuid.UUID) -> PlanState:
        async with self.sessionmaker() as db:
            return await self._state(db, session_id)

    async def _load(self, db: AsyncSession, session_id: uuid.UUID) -> tuple[int, Plan]:
        """Current version number and its (unscheduled) plan."""
        version, plan, _ = await self._load_scheduled(db, session_id)
        return version, plan

    async def _load_scheduled(
        self, db: AsyncSession, session_id: uuid.UUID
    ) -> tuple[int, Plan, ScheduledPlan]:
        """Current version number, its plan and schedule — from the cache when possible."""
        ref = await repo.get_current_version_ref(db, session_id)
        if ref is None:
            if await repo.get_session(db, session_id) is None:
                raise NoSession()
            raise DomainError("Текущая версия плана не найдена")
        version_no, version_id = ref
        cached = self._plan_cache.get(version_id)
        if cached is not None:
            self._plan_cache.move_to_end(version_id)
            return version_no, cached[0], cached[1]
        snapshot = await repo.get_version_snapshot(db, version_id)
        if snapshot is None:
            raise DomainError("Текущая версия плана не найдена")
        plan = Plan.model_validate(snapshot)
        scheduled = schedule(plan)
        self._cache_put(version_id, plan, scheduled)
        return version_no, plan, scheduled

    def _cache_put(self, version_id: int, plan: Plan, scheduled: ScheduledPlan) -> None:
        if version_id in self._plan_cache:
            # Concurrent cold reads of one version all miss, then all store it: keep the first
            # entry, or its bytes would be counted once per reader for a single entry.
            self._plan_cache.move_to_end(version_id)
            return
        size = plan_json_size(plan)
        if size > PLAN_CACHE_MAX_BYTES // 4:
            return  # one huge plan would evict everything else
        self._plan_cache[version_id] = (plan, scheduled, size)
        self._plan_cache_bytes += size
        while self._plan_cache_bytes > PLAN_CACHE_MAX_BYTES:
            _, (_, _, evicted) = self._plan_cache.popitem(last=False)
            self._plan_cache_bytes -= evicted

    async def _build_state(
        self,
        db: AsyncSession,
        session_id: uuid.UUID,
        version: int,
        plan: Plan,
        scheduled: ScheduledPlan,
    ) -> PlanState:
        """PlanState for a plan the caller already holds (and has scheduled): only the version
        list is read, so a write path doesn't re-parse the snapshot it just stored and
        re-schedule it a third time."""
        meta = await repo.list_version_meta(db, session_id)
        return PlanState(
            version=version,
            plan=plan,
            scheduled=scheduled,
            can_undo=undo_target(meta, version) is not None,
            can_redo=redo_target(meta, version) is not None,
        )

    async def _state(self, db: AsyncSession, session_id: uuid.UUID) -> PlanState:
        version, plan, scheduled = await self._load_scheduled(db, session_id)
        return await self._build_state(db, session_id, version, plan, scheduled)

    @staticmethod
    def _check_version(expected: int | None, current: int) -> None:
        """Optimistic concurrency (spec §6): a client that read version N and edits on top of it
        says so; if another tab / the agent / MCP has moved the plan on since, the edit is
        refused instead of silently overwriting the newer state. `None` skips the check (the
        agent and MCP clients act on what they just read inside the same lock)."""
        if expected is not None and expected != current:
            raise VersionConflict(expected, current)

    async def task_history(self, session_id: uuid.UUID, task_id: int) -> list[dict[str, Any]]:
        """History of a task (spec §6/§10): every stored version's diff, filtered to changes
        touching `task_id`, newest first. Only versions whose diff actually mentions the task are
        included — a task that exists but was never edited has an empty (not 404) history.

        Versions with a source in `_BOUNDARY_SOURCES` replace the whole plan, so the walk stops
        at the first (newest) such version at or before the current one: older versions are not
        inspected at all, since their diffs compare unrelated tasks that merely share an id. The
        boundary version itself is surfaced as a single synthetic entry (empty `changes`, a
        summary explaining where the task came from) when the task exists in its snapshot.
        """
        async with self.sessionmaker() as db:
            state = await self._state(db, session_id)
            versions = await repo.list_versions_upto(db, session_id, state.version)
        exists_now = any(t.id == task_id for t in state.plan.tasks)
        entries: list[dict[str, Any]] = []
        boundary: VersionDiff | None = None
        for v in versions:
            if v.source in _BOUNDARY_SOURCES:
                boundary = v
                break
            changes = [c for c in v.diff if c.get("task_id") == task_id]
            if not changes:
                continue
            entries.append(
                {
                    "version": v.version_no,
                    "source": v.source,
                    "created_at": v.created_at,
                    "summary": v.summary,
                    "changes": changes,
                }
            )
        found = exists_now or bool(entries)
        if boundary is not None:
            async with self.sessionmaker() as db:
                boundary_row = await repo.get_version(db, session_id, boundary.version_no)
            if boundary_row is not None:
                boundary_plan = Plan.model_validate(boundary_row.snapshot)
                if any(t.id == task_id for t in boundary_plan.tasks):
                    found = True
                    entries.append(
                        {
                            "version": boundary.version_no,
                            "source": boundary.source,
                            "created_at": boundary.created_at,
                            "summary": _BOUNDARY_SUMMARIES[boundary.source],
                            "changes": [],
                        }
                    )
        if not found:
            raise NotFound(f"Задача №{task_id} не найдена")
        return entries

    async def _guard_busy(self, session_id: uuid.UUID, source: Source) -> None:
        if source == "agent" or not self.locks.is_busy(session_id):
            return
        if source == "mcp" and await self.locks.wait_not_busy(session_id, MCP_WAIT_SECONDS):
            return
        raise AgentBusy()

    async def apply(
        self,
        session_id: uuid.UUID,
        ops: Sequence[Operation],
        *,
        source: Source,
        turn_id: uuid.UUID | None = None,
        confirmed: bool = False,
        expected_version: int | None = None,
    ) -> ApplyOutcome:
        check_batch_size(ops)  # before confirmation: an oversized batch fails however confirmed
        await self._guard_busy(session_id, source)
        async with self.locks.lock(session_id), self.sessionmaker() as db, db.begin():
            await repo.lock_session_plan(db, session_id)
            version, plan = await self._load(db, session_id)
            self._check_version(expected_version, version)
            if not confirmed and requires_confirmation(plan, ops):
                raise ConfirmationRequired(
                    "Пакет удаляет много задач. Спросите пользователя и повторите с confirmed=true"
                )
            # CPU-bound (up to MAX_BATCH_OPS ops on a 500-task plan): run it off the event loop
            # so SSE heartbeats, /healthz and other sessions stay responsive meanwhile.
            before = await asyncio.to_thread(schedule, plan)
            result = await asyncio.to_thread(apply_operations, plan, ops, before)
            summary = apply_summary(result.changes, plan.project_start, result.plan.project_start)
            changed = bool(result.changes) or result.plan.project_start != plan.project_start
            if changed:
                await self._commit(
                    db,
                    session_id,
                    version,
                    result.plan,
                    source,
                    turn_id,
                    summary,
                    [c.model_dump() for c in result.changes],
                )
                state = await self._build_state(
                    db, session_id, version + 1, result.plan, result.scheduled
                )
            else:
                state = await self._build_state(db, session_id, version, plan, before)
        if changed:
            changed_ids = sorted({c.task_id for c in result.changes})
            self._publish(session_id, state.version, source, turn_id, changed_ids)
        return ApplyOutcome(
            state, result.changes, result.warnings, result.created_task_ids, summary
        )

    async def replace(
        self,
        session_id: uuid.UUID,
        plan: Plan,
        *,
        source: Source,
        summary: str,
        chat_note: str | None = None,
    ) -> PlanState:
        await self._guard_busy(session_id, source)
        async with self.locks.lock(session_id), self.sessionmaker() as db, db.begin():
            await repo.lock_session_plan(db, session_id)
            version, current = await self._load(db, session_id)
            before, after = await asyncio.to_thread(lambda: (schedule(current), schedule(plan)))
            changes = diff_plans(before, after)
            await self._commit(
                db,
                session_id,
                version,
                plan,
                source,
                None,
                summary,
                [c.model_dump() for c in changes],
            )
            if chat_note:
                await repo.add_chat_message(
                    db, session_id=session_id, role="system", content=chat_note
                )
            state = await self._build_state(db, session_id, version + 1, plan, after)
        self._publish(session_id, state.version, source, None, sorted({c.task_id for c in changes}))
        return state

    async def reset(self, session_id: uuid.UUID) -> PlanState:
        return await self.replace(
            session_id, build_demo_plan(self._today()), source="reset", summary="Сброс к демо-плану"
        )

    async def undo(
        self,
        session_id: uuid.UUID,
        *,
        source: Source = "user",
        expected_version: int | None = None,
    ) -> PlanState:
        return await self._move_pointer(
            session_id, undo_target, NothingToUndo(), source, expected_version
        )

    async def redo(
        self, session_id: uuid.UUID, *, expected_version: int | None = None
    ) -> PlanState:
        return await self._move_pointer(
            session_id, redo_target, NothingToRedo(), "user", expected_version
        )

    async def _move_pointer(
        self,
        session_id: uuid.UUID,
        target_fn: Callable[[list[VersionMeta], int], int | None],
        empty_error: DomainError,
        source: Source,
        expected_version: int | None,
    ) -> PlanState:
        await self._guard_busy(session_id, source)
        async with self.locks.lock(session_id), self.sessionmaker() as db, db.begin():
            await repo.lock_session_plan(db, session_id)
            version, before_plan = await self._load(db, session_id)
            self._check_version(expected_version, version)
            target = target_fn(await repo.list_version_meta(db, session_id), version)
            if target is None:
                raise empty_error
            session = await repo.get_session(db, session_id)
            assert session is not None
            session.current_version = target
            await db.flush()
            state = await self._state(db, session_id)
        before = await asyncio.to_thread(schedule, before_plan)
        changed = sorted({c.task_id for c in diff_plans(before, state.scheduled)})
        self._publish(session_id, state.version, source, None, changed)
        return state

    async def _commit(
        self,
        db: AsyncSession,
        session_id: uuid.UUID,
        current_version: int,
        plan: Plan,
        source: Source,
        turn_id: uuid.UUID | None,
        summary: str,
        diff: list[dict[str, Any]],
    ) -> None:
        # Every stored version is a full snapshot (up to max_versions per session), and the
        # model's per-field caps alone still allow ~2.5 MB on a 500-task plan (security audit
        # M2). Checked here, the one place every mutation and import stores a version through.
        if plan_json_size(plan) > self._max_plan_bytes:
            raise PlanTooLarge(self._max_plan_bytes)
        await repo.delete_versions_after(db, session_id, current_version)
        new_version = current_version + 1
        await repo.add_version(
            db,
            session_id=session_id,
            version_no=new_version,
            snapshot=plan.model_dump(mode="json"),
            source=source,
            turn_id=turn_id,
            summary=summary,
            diff=diff,
        )
        session = await repo.get_session(db, session_id)
        assert session is not None
        session.current_version = new_version
        await db.flush()
        await repo.prune_versions(db, session_id, keep=self._max_versions)

    def _publish(
        self,
        session_id: uuid.UUID,
        version: int,
        source: Source,
        turn_id: uuid.UUID | None,
        changed: list[int],
    ) -> None:
        self.bus.publish(
            session_id,
            {
                "type": "plan_changed",
                "version": version,
                "source": source,
                "turn_id": str(turn_id) if turn_id else None,
                "changed_task_ids": changed,
            },
        )

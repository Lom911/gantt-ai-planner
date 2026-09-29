# Gantt AI Planner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Web app with an interactive Gantt chart (seeded demo plan), Excel import/export, and a chat agent (Claude via MCP) that bulk-edits the plan with instant chart updates; deployed with Docker Compose behind Caddy.

**Architecture:** FastAPI monolith (single uvicorn worker) with a pure-Python domain core (`app/domain`, `app/excel`) that owns scheduling (CPM, FS+lag, Mon–Fri), atomic operations and diffs. `PlanService` is the only mutation path (UI, agent, external MCP). Plans are stored as JSONB snapshots per version in PostgreSQL 17. The chat agent calls tools through an in-process MCP client bound to a FastMCP server that is also exposed at `/mcp`. React 19 SPA (SVAR Gantt) is served by the same container; live updates via SSE.

**Tech Stack:** Python 3.12, uv, FastAPI, Pydantic v2, pydantic-settings, SQLAlchemy 2 (async, asyncpg), Alembic, openpyxl + defusedxml, anthropic SDK, FastMCP, sse-starlette, pytest; Node 20, Vite, React 19, TypeScript strict, Tailwind v4, shadcn/ui, TanStack Query, `@svar-ui/react-gantt` 2.7.3, vitest, Playwright; Docker, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-25-gantt-ai-planner-design.md` — read it before starting any task; section numbers below (§N) refer to it.

## Global Constraints

- Python `>=3.12,<3.13` (pinned via `uv python pin 3.12`); Node 20; npm (no pnpm/yarn).
- Code, identifiers, comments, commit messages: English. All user-facing strings (UI, API error messages, agent-facing tool errors, Excel headers): Russian.
- Conventional Commits; every commit message ends with a blank line and the `Co-Authored-By:` trailer that your own harness specifies (it names the model that wrote the commit).
- Repo root: `gantt-ai-planner/`. Backend in `backend/` (package `app`), frontend in `frontend/`, deploy assets in `deploy/`, scripts in `scripts/`.
- `app/domain` and `app/excel` are pure: no imports of FastAPI, SQLAlchemy, anthropic, MCP, or I/O except bytes in/out. mypy `--strict` must pass for these two packages.
- Limits (spec §4.1, §9): name 1..200, description 0..2000, assignee 0..100, duration 1..999 workdays, lag 0..365 workdays, max 500 tasks, xlsx ≤ 2 MB, ≤ 50 versions per session.
- Workdays Mon–Fri, no holidays; dates are date-only (no time, no TZ).
- Dependency type: FS only with non-negative lag.
- Task ids are stable ints; `Plan.last_id` guarantees ids are never reused.
- Chat rate limits: 30 user messages/hour/session, 500/day global; agent max 15 tool iterations/turn.
- LLM: `LLM_PROVIDER=anthropic|fake` (default `fake` when no key), `LLM_MODEL` default `claude-sonnet-5`, fallback `claude-haiku-4-5-20251001`.
- Never log prompts, plan contents, people names or request bodies.
- Line endings LF (`.gitattributes`: `* text=auto eol=lf`).

## Review Focus

1. Excel whose header row is not row 1 (title rows above), with blank/formatted-but-empty rows in the middle and at the end, and over-long names → header detected, blanks skipped, long text truncated with a warning (never a 500). Test lives in Task 5.
2. Predecessor cells that Excel stores as floats (`3.0`) or strings `"3.0"`, `"3;4"`, `"3FS+2"` → parsed as ids 3/4 with lag 2. Test lives in Task 5.
3. A single agent batch that adds a task and immediately references its (predicted) id in `add_dependency` → works because ids are `last_id+1, +2…` and the render shows the next free id. Test lives in Task 3 (+ render line in Task 4).
4. Weekend dates coming from the LLM, the modal, or Excel (`move_task` to a Saturday, project start on Sunday) → normalized to the next Monday, no error. Tests live in Tasks 2 and 3.
5. Browser holding a cookie for a deleted/expired session → API answers `401 no_session`, the frontend transparently calls `POST /api/session` and retries once; the UI never ends up blank. Tests live in Task 9 (API) and Task 12 (client).

---

## File Structure

```
gantt-ai-planner/
  .gitattributes .gitignore .editorconfig .env.example docker-compose.yml Dockerfile .dockerignore
  backend/
    pyproject.toml uv.lock alembic.ini
    app/
      __init__.py main.py config.py
      domain/   __init__.py errors.py models.py calendar.py scheduler.py operations.py diff.py seed.py render.py
      excel/    __init__.py headers.py parse.py export.py
      db/       __init__.py base.py models.py engine.py repo.py
      migrations/ env.py script.py.mako versions/0001_initial.py
      services/ __init__.py events.py locks.py plan_service.py ratelimit.py sessions.py
      mcp_server/ __init__.py context.py server.py client.py
      agent/    __init__.py llm.py fake.py prompt.py loop.py
      api/      __init__.py deps.py errors.py routes_session.py routes_plan.py routes_chat.py routes_events.py
    tests/
      unit/ test_calendar.py test_scheduler.py test_operations.py test_diff.py test_seed_render.py test_excel_parse.py test_excel_export.py test_fake_llm.py
      integration/ conftest.py test_repo.py test_plan_service.py test_api_plan.py test_api_import_export.py test_mcp_tools.py test_agent_turn.py test_api_chat_events.py
  frontend/
    package.json vite.config.ts tsconfig.json index.html components.json
    src/ main.tsx App.tsx index.css
         api/ client.ts types.ts chatStream.ts
         hooks/ usePlan.ts useSessionEvents.ts useChat.ts
         components/ Toolbar.tsx SplitLayout.tsx
                     gantt/ GanttView.tsx mapping.ts mapping.test.ts locale.ts gantt.css
                     chat/ ChatPanel.tsx MessageItem.tsx DiffSummary.tsx
                     task/ TaskModal.tsx
                     import/ ImportDialog.tsx
                     ui/ (shadcn generated)
         lib/ dates.ts dates.test.ts
    e2e/ playwright.config.ts main.spec.ts
  deploy/ compose.prod.yml initdb/10-roles.sh caddy/ bootstrap.sh planner-deploy planner-deploy-wrapper backup.sh
  scripts/ make_sample_excel.py record_demo.ts deploy-manual.sh
  examples/sample-plan.xlsx
  docs/ roadmap-to-production.md runbook.md ai-usage.md demo.gif demo.mp4
  .github/ workflows/ci.yml workflows/deploy.yml dependabot.yml
```

---

# PHASE 1 — Core + deploy-ready

### Task 1: Repository scaffold + workday calendar

**Files:**
- Create: `.gitattributes`, `.gitignore`, `.editorconfig`, `backend/pyproject.toml`, `backend/app/__init__.py`, `backend/app/domain/__init__.py`, `backend/app/domain/calendar.py`, `backend/tests/__init__.py`, `backend/tests/unit/__init__.py`
- Test: `backend/tests/unit/test_calendar.py`

**Interfaces:**
- Produces: `app.domain.calendar` with `is_workday(d) -> bool`, `next_workday(d) -> date`, `prev_workday(d) -> date`, `add_workdays(d, n: int) -> date` (n may be negative), `workday_diff(a, b) -> int` (signed; `add_workdays(a, workday_diff(a, b)) == b` for workdays a, b).

- [ ] **Step 1: Root files**

`.gitattributes`:
```
* text=auto eol=lf
*.xlsx binary
*.png binary
*.gif binary
*.mp4 binary
```

`.gitignore`:
```
.env
secrets/
__pycache__/
*.pyc
.venv/
.pytest_cache/
.mypy_cache/
.ruff_cache/
node_modules/
frontend/dist/
frontend/test-results/
frontend/playwright-report/
*.log
.DS_Store
```

`.editorconfig`:
```
root = true
[*]
end_of_line = lf
insert_final_newline = true
charset = utf-8
indent_style = space
indent_size = 4
[*.{ts,tsx,js,json,yml,yaml,css,html,md}]
indent_size = 2
```

- [ ] **Step 2: Backend project**

Run (from `backend/`):
```bash
uv init --lib --name gantt-ai-planner --no-readme --vcs none .   # if it creates src/ layout, delete it; we use app/
uv python pin 3.12
uv add pydantic
uv add --dev pytest pytest-asyncio ruff mypy
```
Then make sure `pyproject.toml` contains (merge, keep uv-generated `[project]` deps):
```toml
[project]
name = "gantt-ai-planner"
version = "0.1.0"
requires-python = ">=3.12,<3.13"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["app"]

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "RUF"]
ignore = ["RUF001", "RUF002", "RUF003"]  # Cyrillic strings are intentional

[tool.mypy]
python_version = "3.12"
plugins = ["pydantic.mypy"]
warn_unused_ignores = true

[[tool.mypy.overrides]]
module = ["app.domain.*", "app.excel.*"]
strict = true

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "session"
asyncio_default_test_loop_scope = "session"
testpaths = ["tests"]
```
Create empty `app/__init__.py`, `app/domain/__init__.py`, `tests/__init__.py`, `tests/unit/__init__.py`.

- [ ] **Step 3: Write the failing test** `backend/tests/unit/test_calendar.py`

```python
from datetime import date

from app.domain.calendar import add_workdays, is_workday, next_workday, prev_workday, workday_diff

MON = date(2026, 9, 21)
FRI = date(2026, 9, 25)
SAT = date(2026, 9, 26)
SUN = date(2026, 9, 27)
NEXT_MON = date(2026, 9, 28)


def test_is_workday():
    assert is_workday(MON) and is_workday(FRI)
    assert not is_workday(SAT) and not is_workday(SUN)


def test_next_and_prev_workday():
    assert next_workday(MON) == MON
    assert next_workday(SAT) == NEXT_MON
    assert next_workday(SUN) == NEXT_MON
    assert prev_workday(SAT) == FRI
    assert prev_workday(FRI) == FRI


def test_add_workdays_forward():
    assert add_workdays(MON, 0) == MON
    assert add_workdays(MON, 4) == FRI
    assert add_workdays(FRI, 1) == NEXT_MON
    assert add_workdays(MON, 5) == NEXT_MON
    assert add_workdays(MON, 10) == date(2026, 10, 5)
    assert add_workdays(SAT, 0) == NEXT_MON  # weekend normalizes forward


def test_add_workdays_backward():
    assert add_workdays(NEXT_MON, -1) == FRI
    assert add_workdays(NEXT_MON, -5) == MON
    assert add_workdays(FRI, -4) == MON


def test_workday_diff_is_inverse_of_add():
    start = MON
    for n in range(-15, 16):
        target = add_workdays(start, n)
        assert workday_diff(start, target) == n
    assert workday_diff(FRI, NEXT_MON) == 1
    assert workday_diff(NEXT_MON, FRI) == -1
```

- [ ] **Step 4: Run to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_calendar.py -q`
Expected: FAIL (`ModuleNotFoundError: app.domain.calendar`).

- [ ] **Step 5: Implement** `backend/app/domain/calendar.py`

```python
"""Workday arithmetic: Monday–Friday are workdays, no holidays."""

from datetime import date, timedelta

_ONE_DAY = timedelta(days=1)


def is_workday(d: date) -> bool:
    return d.weekday() < 5


def next_workday(d: date) -> date:
    """Return d if it is a workday, otherwise the following Monday."""
    while not is_workday(d):
        d += _ONE_DAY
    return d


def prev_workday(d: date) -> date:
    """Return d if it is a workday, otherwise the preceding Friday."""
    while not is_workday(d):
        d -= _ONE_DAY
    return d


def add_workdays(d: date, n: int) -> date:
    """Move n workdays from d (backwards when n < 0). Weekend d is normalized first."""
    d = next_workday(d) if n >= 0 else prev_workday(d)
    step = _ONE_DAY if n >= 0 else -_ONE_DAY
    weeks, remaining = divmod(abs(n), 5)
    d += step * 7 * weeks
    while remaining:
        d += step
        if is_workday(d):
            remaining -= 1
    return d


def workday_diff(a: date, b: date) -> int:
    """Signed number of workdays needed to move from workday a to workday b."""
    if a == b:
        return 0
    sign = 1 if b > a else -1
    lo, hi = (a, b) if sign > 0 else (b, a)
    weeks, _ = divmod((hi - lo).days, 7)
    count = weeks * 5
    d = lo + timedelta(days=7 * weeks)
    while d < hi:
        d += _ONE_DAY
        if is_workday(d):
            count += 1
    return sign * count
```

- [ ] **Step 6: Verify**

Run: `cd backend && uv run pytest tests/unit/test_calendar.py -q && uv run ruff check . && uv run ruff format --check . && uv run mypy app`
Expected: all pass (run `uv run ruff format .` first if format-check complains).

- [ ] **Step 7: Commit**

```bash
git add .gitattributes .gitignore .editorconfig backend
git commit -m "chore: scaffold backend project and add workday calendar"
```

---

### Task 2: Domain models, validation, scheduler (CPM)

**Files:**
- Create: `backend/app/domain/errors.py`, `backend/app/domain/models.py`, `backend/app/domain/scheduler.py`
- Test: `backend/tests/unit/test_scheduler.py`

**Interfaces:**
- Consumes: `app.domain.calendar` (Task 1).
- Produces:
  - `errors.py`: `DomainError(message, *, details=None)` with `.code`, `.message`, `.details`; subclasses `PlanValidationError` (`code="invalid_plan"`), `CycleError(cycle: list[int])` (`code="cycle"`, subclass of PlanValidationError), `OperationError(message, *, index=None, details=None)` (`code="invalid_operation"`), `ConfirmationRequired` (`code="confirmation_required"`).
  - `models.py`: `MAX_TASKS = 500`; `Task(id, name, description="", assignee=None, duration, constraint_start=None)`; `Dependency(predecessor_id, successor_id, lag=0)` (frozen); `Plan(project_start, last_id=0, tasks=[], dependencies=[])` — `last_id` auto-raised to max task id.
  - `scheduler.py`: `ScheduledTask(Task)` + `start, end, slack, is_critical, constrained_by, overallocated_with`; `ScheduledPlan(project_start, project_end, last_id, tasks, dependencies, critical_path)` with `.task(id) -> ScheduledTask` and `.to_plan() -> Plan`; `validate_plan(plan) -> None`; `topological_order(plan) -> list[int]`; `schedule(plan) -> ScheduledPlan`.

- [ ] **Step 1: Write the failing test** `backend/tests/unit/test_scheduler.py`

```python
from datetime import date

import pytest

from app.domain.errors import CycleError, PlanValidationError
from app.domain.models import Dependency, Plan, Task
from app.domain.scheduler import schedule, topological_order

MON = date(2026, 9, 21)


def t(id: int, dur: int, assignee: str | None = None, **kw: object) -> Task:
    return Task(id=id, name=f"Задача {id}", duration=dur, assignee=assignee, **kw)


def dep(p: int, s: int, lag: int = 0) -> Dependency:
    return Dependency(predecessor_id=p, successor_id=s, lag=lag)


def test_single_task_starts_at_project_start():
    sp = schedule(Plan(project_start=MON, tasks=[t(1, 3)]))
    task = sp.task(1)
    assert (task.start, task.end) == (MON, date(2026, 9, 23))
    assert sp.project_end == date(2026, 9, 23)
    assert task.constrained_by == "project_start"


def test_project_start_on_weekend_is_normalized():
    sp = schedule(Plan(project_start=date(2026, 9, 27), tasks=[t(1, 1)]))  # Sunday
    assert sp.project_start == date(2026, 9, 28)
    assert sp.task(1).start == date(2026, 9, 28)


def test_fs_dependency_with_lag_skips_weekend():
    plan = Plan(project_start=MON, tasks=[t(1, 5), t(2, 2)], dependencies=[dep(1, 2, lag=1)])
    sp = schedule(plan)
    assert sp.task(1).end == date(2026, 9, 25)  # Fri
    assert sp.task(2).start == date(2026, 9, 29)  # Mon +1 lag -> Tue
    assert sp.task(2).constrained_by == "predecessor:1"


def test_constraint_delays_task_and_is_normalized():
    plan = Plan(project_start=MON, tasks=[t(1, 2, constraint_start=date(2026, 9, 26))])  # Saturday
    task = schedule(plan).task(1)
    assert task.start == date(2026, 9, 28)
    assert task.constrained_by == "constraint"


def test_constraint_earlier_than_predecessor_is_ignored():
    plan = Plan(
        project_start=MON,
        tasks=[t(1, 5), t(2, 1, constraint_start=MON)],
        dependencies=[dep(1, 2)],
    )
    assert schedule(plan).task(2).start == date(2026, 9, 28)


def test_critical_path_and_slack():
    # 1(3) -> 3(2); 2(1) -> 3 ; 2 has slack 2
    plan = Plan(
        project_start=MON,
        tasks=[t(1, 3), t(2, 1), t(3, 2)],
        dependencies=[dep(1, 3), dep(2, 3)],
    )
    sp = schedule(plan)
    assert sp.task(1).is_critical and sp.task(3).is_critical
    assert not sp.task(2).is_critical
    assert sp.task(2).slack == 2
    assert sp.critical_path == [1, 3]


def test_overallocation_detected_for_same_assignee_overlap():
    plan = Plan(
        project_start=MON,
        tasks=[t(1, 3, "Анна"), t(2, 2, "анна "), t(3, 2, "Игорь"), t(4, 1, "Анна")],
        dependencies=[dep(1, 4)],
    )
    sp = schedule(plan)
    assert sp.task(1).overallocated_with == [2]
    assert sp.task(2).overallocated_with == [1]
    assert sp.task(3).overallocated_with == []
    assert sp.task(4).overallocated_with == []  # starts after 1 ends, after 2 ends


def test_cycle_detected():
    plan = Plan(
        project_start=MON,
        tasks=[t(1, 1), t(2, 1), t(3, 1)],
        dependencies=[dep(1, 2), dep(2, 3), dep(3, 1)],
    )
    with pytest.raises(CycleError) as exc:
        topological_order(plan)
    assert sorted(exc.value.cycle) == [1, 2, 3]
    assert "Циклическая зависимость" in exc.value.message


@pytest.mark.parametrize(
    ("tasks", "deps", "fragment"),
    [
        ([t(1, 1), t(1, 2)], [], "повторяется"),
        ([t(1, 1)], [dep(1, 9)], "не существует"),
        ([t(1, 1)], [dep(1, 1)], "самой себя"),
        ([t(1, 1), t(2, 1)], [dep(1, 2), dep(1, 2, 3)], "дублируется"),
    ],
)
def test_validation_errors(tasks, deps, fragment):
    with pytest.raises(PlanValidationError) as exc:
        schedule(Plan(project_start=MON, tasks=tasks, dependencies=deps))
    assert fragment in exc.value.message


def test_last_id_never_below_max_id_and_empty_plan():
    assert Plan(project_start=MON, tasks=[t(7, 1)]).last_id == 7
    assert Plan(project_start=MON, last_id=10, tasks=[t(7, 1)]).last_id == 10
    sp = schedule(Plan(project_start=MON))
    assert sp.tasks == [] and sp.project_end == MON


def test_assignee_blank_becomes_none_and_to_plan_roundtrip():
    plan = Plan(project_start=MON, tasks=[t(1, 1, "  ")])
    assert plan.tasks[0].assignee is None
    assert schedule(plan).to_plan() == plan
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_scheduler.py -q`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement** `backend/app/domain/errors.py`

```python
from typing import Any


class DomainError(Exception):
    code = "domain_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}


class PlanValidationError(DomainError):
    code = "invalid_plan"


class CycleError(PlanValidationError):
    code = "cycle"

    def __init__(self, cycle: list[int]) -> None:
        path = " → ".join(str(i) for i in [*cycle, cycle[0]])
        super().__init__(f"Циклическая зависимость: {path}", details={"cycle": cycle})
        self.cycle = cycle


class OperationError(DomainError):
    code = "invalid_operation"

    def __init__(
        self, message: str, *, index: int | None = None, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message, details=details)
        self.index = index


class ConfirmationRequired(DomainError):
    code = "confirmation_required"
```

- [ ] **Step 4: Implement** `backend/app/domain/models.py`

```python
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_TASKS = 500


class Task(BaseModel):
    id: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    assignee: str | None = Field(default=None, max_length=100)
    duration: int = Field(ge=1, le=999)
    constraint_start: date | None = None

    @field_validator("name", "description", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v

    @field_validator("assignee", mode="before")
    @classmethod
    def _blank_assignee_is_none(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v


class Dependency(BaseModel):
    model_config = ConfigDict(frozen=True)

    predecessor_id: int
    successor_id: int
    lag: int = Field(default=0, ge=0, le=365)


class Plan(BaseModel):
    project_start: date
    last_id: int = Field(default=0, ge=0)
    tasks: list[Task] = Field(default_factory=list)
    dependencies: list[Dependency] = Field(default_factory=list)

    @model_validator(mode="after")
    def _sync_last_id(self) -> "Plan":
        max_id = max((t.id for t in self.tasks), default=0)
        if self.last_id < max_id:
            self.last_id = max_id
        return self
```

- [ ] **Step 5: Implement** `backend/app/domain/scheduler.py`

```python
"""CPM scheduling: forward pass (early dates), backward pass (slack), overallocation."""

from collections import defaultdict
from datetime import date

from pydantic import BaseModel, Field

from app.domain.calendar import add_workdays, next_workday, workday_diff
from app.domain.errors import CycleError, PlanValidationError
from app.domain.models import Dependency, Plan, Task


class ScheduledTask(Task):
    start: date
    end: date
    slack: int
    is_critical: bool
    constrained_by: str  # "project_start" | "constraint" | "predecessor:<id>"
    overallocated_with: list[int] = Field(default_factory=list)


class ScheduledPlan(BaseModel):
    project_start: date
    project_end: date
    last_id: int
    tasks: list[ScheduledTask]
    dependencies: list[Dependency]
    critical_path: list[int]

    def task(self, task_id: int) -> ScheduledTask:
        for t in self.tasks:
            if t.id == task_id:
                return t
        raise KeyError(task_id)

    def to_plan(self) -> Plan:
        fields = set(Task.model_fields)
        return Plan(
            project_start=self.project_start,
            last_id=self.last_id,
            tasks=[Task(**t.model_dump(include=fields)) for t in self.tasks],
            dependencies=list(self.dependencies),
        )


def validate_plan(plan: Plan) -> None:
    ids: set[int] = set()
    for task in plan.tasks:
        if task.id in ids:
            raise PlanValidationError(f"№ {task.id} повторяется")
        ids.add(task.id)
    seen: set[tuple[int, int]] = set()
    for d in plan.dependencies:
        for ref in (d.predecessor_id, d.successor_id):
            if ref not in ids:
                raise PlanValidationError(f"Задача {ref} не существует")
        if d.predecessor_id == d.successor_id:
            raise PlanValidationError(f"Задача {d.successor_id} не может зависеть от самой себя")
        key = (d.predecessor_id, d.successor_id)
        if key in seen:
            raise PlanValidationError(f"Связь {key[0]} → {key[1]} дублируется")
        seen.add(key)


def topological_order(plan: Plan) -> list[int]:
    """Kahn's algorithm; ties keep the task-list order. Raises CycleError."""
    succs: dict[int, list[int]] = defaultdict(list)
    indegree = {t.id: 0 for t in plan.tasks}
    for d in plan.dependencies:
        succs[d.predecessor_id].append(d.successor_id)
        indegree[d.successor_id] += 1
    position = {t.id: i for i, t in enumerate(plan.tasks)}
    ready = sorted((tid for tid, deg in indegree.items() if deg == 0), key=position.__getitem__)
    order: list[int] = []
    while ready:
        tid = ready.pop(0)
        order.append(tid)
        for s in succs[tid]:
            indegree[s] -= 1
            if indegree[s] == 0:
                ready.append(s)
                ready.sort(key=position.__getitem__)
    if len(order) != len(indegree):
        remaining = {tid for tid, deg in indegree.items() if deg > 0}
        raise CycleError(_find_cycle(remaining, succs))
    return order


def _find_cycle(nodes: set[int], succs: dict[int, list[int]]) -> list[int]:
    visited: set[int] = set()
    for root in sorted(nodes):
        stack: list[int] = []
        on_stack: set[int] = set()

        def dfs(node: int) -> list[int] | None:
            visited.add(node)
            stack.append(node)
            on_stack.add(node)
            for nxt in succs.get(node, []):
                if nxt not in nodes:
                    continue
                if nxt in on_stack:
                    return stack[stack.index(nxt) :]
                if nxt not in visited and (found := dfs(nxt)):
                    return found
            stack.pop()
            on_stack.discard(node)
            return None

        if root not in visited and (cycle := dfs(root)):
            return cycle
    return sorted(nodes)


def schedule(plan: Plan) -> ScheduledPlan:
    validate_plan(plan)
    order = topological_order(plan)
    by_id = {t.id: t for t in plan.tasks}
    preds: dict[int, list[Dependency]] = defaultdict(list)
    succs: dict[int, list[Dependency]] = defaultdict(list)
    for d in plan.dependencies:
        preds[d.successor_id].append(d)
        succs[d.predecessor_id].append(d)

    project_start = next_workday(plan.project_start)
    start: dict[int, date] = {}
    end: dict[int, date] = {}
    reason: dict[int, str] = {}
    for tid in order:
        task = by_id[tid]
        es, why = project_start, "project_start"
        if task.constraint_start is not None:
            c = next_workday(task.constraint_start)
            if c > es:
                es, why = c, "constraint"
        for d in preds[tid]:
            candidate = add_workdays(end[d.predecessor_id], 1 + d.lag)
            if candidate > es:
                es, why = candidate, f"predecessor:{d.predecessor_id}"
        start[tid], end[tid], reason[tid] = es, add_workdays(es, task.duration - 1), why

    project_end = max(end.values(), default=project_start)
    late_start: dict[int, date] = {}
    for tid in reversed(order):
        lf = project_end
        for d in succs[tid]:
            candidate = add_workdays(late_start[d.successor_id], -(1 + d.lag))
            if candidate < lf:
                lf = candidate
        late_start[tid] = add_workdays(lf, -(by_id[tid].duration - 1))

    overlaps = _overallocations(plan.tasks, start, end)
    tasks: list[ScheduledTask] = []
    for task in plan.tasks:
        slack = max(0, workday_diff(start[task.id], late_start[task.id]))
        tasks.append(
            ScheduledTask(
                **task.model_dump(),
                start=start[task.id],
                end=end[task.id],
                slack=slack,
                is_critical=slack == 0,
                constrained_by=reason[task.id],
                overallocated_with=sorted(overlaps[task.id]),
            )
        )
    critical = sorted((t for t in tasks if t.is_critical), key=lambda t: (t.start, t.id))
    return ScheduledPlan(
        project_start=project_start,
        project_end=project_end,
        last_id=plan.last_id,
        tasks=tasks,
        dependencies=list(plan.dependencies),
        critical_path=[t.id for t in critical],
    )


def _overallocations(
    tasks: list[Task], start: dict[int, date], end: dict[int, date]
) -> dict[int, set[int]]:
    groups: dict[str, list[int]] = defaultdict(list)
    for task in tasks:
        if task.assignee:
            groups[task.assignee.strip().casefold()].append(task.id)
    result: dict[int, set[int]] = defaultdict(set)
    for ids in groups.values():
        for i, a in enumerate(ids):
            for b in ids[i + 1 :]:
                if start[a] <= end[b] and start[b] <= end[a]:
                    result[a].add(b)
                    result[b].add(a)
    return result
```

- [ ] **Step 6: Verify**

Run: `cd backend && uv run pytest tests/unit -q && uv run ruff check . && uv run ruff format --check . && uv run mypy app`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add backend
git commit -m "feat(domain): add plan models and CPM scheduler"
```

---

### Task 3: Operations (atomic batches) + diff

**Files:**
- Create: `backend/app/domain/operations.py`, `backend/app/domain/diff.py`
- Test: `backend/tests/unit/test_operations.py`, `backend/tests/unit/test_diff.py`

**Interfaces:**
- Consumes: Task 2 models/scheduler/errors.
- Produces:
  - `diff.py`: `Change(task_id: int, task_name: str, field: ChangeField, before: str | int | None, after: str | int | None)`; `ChangeField = Literal["created","deleted","name","description","assignee","duration","constraint_start","predecessors","start","end"]`; `format_predecessors(deps: list[Dependency], task_id: int) -> str` (e.g. `"3, 5+2"`); `diff_plans(before: ScheduledPlan, after: ScheduledPlan) -> list[Change]`; `summarize_changes(changes: list[Change]) -> str`.
  - `operations.py`: models `PredRef`, `AddTask`, `UpdateTask`, `MoveTask`, `ClearConstraint`, `SetDependencies`, `AddDependency`, `RemoveDependency`, `DeleteTask`, `SetProjectStart`; `Operation` (discriminated union on `op`); `operations_adapter: TypeAdapter[list[Operation]]`; `ApplyResult(plan, scheduled, changes, warnings, created_task_ids)`; `apply_operations(plan: Plan, ops: Sequence[Operation]) -> ApplyResult`; `requires_confirmation(plan: Plan, ops: Sequence[Operation]) -> bool`.

- [ ] **Step 1: Write failing tests** `backend/tests/unit/test_operations.py`

```python
from datetime import date

import pytest

from app.domain.errors import OperationError
from app.domain.models import Dependency, Plan, Task
from app.domain.operations import apply_operations, operations_adapter, requires_confirmation

MON = date(2026, 9, 21)


def base_plan() -> Plan:
    # 1(3d) -> 2(2d) -> 3(1d); 4 independent
    return Plan(
        project_start=MON,
        tasks=[
            Task(id=1, name="Анализ", duration=3, assignee="Анна"),
            Task(id=2, name="Разработка", duration=2, assignee="Игорь"),
            Task(id=3, name="Тест", duration=1, assignee="Ольга"),
            Task(id=4, name="Маркетинг", duration=2),
        ],
        dependencies=[
            Dependency(predecessor_id=1, successor_id=2),
            Dependency(predecessor_id=2, successor_id=3),
        ],
    )


def ops(*raw: dict) -> list:
    return operations_adapter.validate_python(list(raw))


def test_move_task_by_shift_cascades_to_successors():
    res = apply_operations(base_plan(), ops({"op": "move_task", "id": 1, "shift_days": 2}))
    assert res.scheduled.task(1).start == date(2026, 9, 23)
    assert res.scheduled.task(3).start == date(2026, 9, 30)
    fields = {(c.task_id, c.field) for c in res.changes}
    assert {(1, "constraint_start"), (1, "start"), (2, "start"), (3, "start")} <= fields


def test_move_task_to_saturday_is_normalized_to_monday():
    res = apply_operations(base_plan(), ops({"op": "move_task", "id": 4, "start_date": "2026-09-26"}))
    assert res.plan.tasks[3].constraint_start == date(2026, 9, 28)
    assert res.scheduled.task(4).start == date(2026, 9, 28)


def test_move_before_predecessor_warns():
    res = apply_operations(base_plan(), ops({"op": "move_task", "id": 2, "start_date": "2026-09-21"}))
    assert res.scheduled.task(2).start == date(2026, 9, 24)
    assert any("ограничено предшественником 1" in w for w in res.warnings)


def test_add_task_then_reference_predicted_id_in_same_batch():
    res = apply_operations(
        base_plan(),
        ops(
            {"op": "add_task", "name": "Документация", "duration": 2, "predecessors": [{"id": 3}]},
            {"op": "add_dependency", "predecessor_id": 5, "successor_id": 4, "lag": 1},
        ),
    )
    assert res.created_task_ids == [5]
    assert res.scheduled.task(5).start == date(2026, 9, 29)
    assert res.scheduled.task(4).start == add_expected(res.scheduled.task(5).end, 2)


def add_expected(d: date, n: int) -> date:
    from app.domain.calendar import add_workdays

    return add_workdays(d, n)


def test_add_task_after_id_inserts_in_order():
    res = apply_operations(base_plan(), ops({"op": "add_task", "name": "X", "duration": 1, "after_id": 1}))
    assert [t.id for t in res.plan.tasks] == [1, 5, 2, 3, 4]


def test_update_task_reassign_and_clear_assignee():
    res = apply_operations(
        base_plan(),
        ops(
            {"op": "update_task", "id": 2, "assignee": "Мария", "duration": 4},
            {"op": "update_task", "id": 3, "assignee": ""},
        ),
    )
    assert res.plan.tasks[1].assignee == "Мария" and res.plan.tasks[1].duration == 4
    assert res.plan.tasks[2].assignee is None


def test_set_and_remove_dependencies():
    res = apply_operations(
        base_plan(),
        ops(
            {"op": "set_dependencies", "id": 3, "predecessors": [{"id": 4, "lag": 1}]},
            {"op": "remove_dependency", "predecessor_id": 1, "successor_id": 2},
        ),
    )
    pairs = {(d.predecessor_id, d.successor_id, d.lag) for d in res.plan.dependencies}
    assert pairs == {(4, 3, 1)}


def test_delete_task_bridges_dependencies():
    plan = base_plan()
    plan.dependencies = [
        Dependency(predecessor_id=1, successor_id=2, lag=1),
        Dependency(predecessor_id=2, successor_id=3, lag=2),
    ]
    res = apply_operations(plan, ops({"op": "delete_task", "id": 2}))
    assert [t.id for t in res.plan.tasks] == [1, 3, 4]
    assert [(d.predecessor_id, d.successor_id, d.lag) for d in res.plan.dependencies] == [(1, 3, 3)]
    assert any(c.field == "deleted" and c.task_id == 2 for c in res.changes)


def test_deleted_ids_are_never_reused():
    res = apply_operations(base_plan(), ops({"op": "delete_task", "id": 4}))
    res2 = apply_operations(res.plan, ops({"op": "add_task", "name": "Новая", "duration": 1}))
    assert res2.created_task_ids == [5]


def test_batch_is_atomic_on_error():
    plan = base_plan()
    with pytest.raises(OperationError) as exc:
        apply_operations(
            plan,
            ops(
                {"op": "update_task", "id": 1, "name": "Переименовано"},
                {"op": "update_task", "id": 99, "name": "Нет такой"},
            ),
        )
    assert exc.value.index == 1
    assert "99" in exc.value.message
    assert plan.tasks[0].name == "Анализ"  # input untouched


def test_cycle_is_rejected():
    with pytest.raises(OperationError) as exc:
        apply_operations(base_plan(), ops({"op": "add_dependency", "predecessor_id": 3, "successor_id": 1}))
    assert "Циклическая зависимость" in exc.value.message


def test_set_project_start_and_clear_constraint():
    plan = base_plan()
    plan.tasks[3].constraint_start = date(2026, 10, 5)
    res = apply_operations(
        plan,
        ops({"op": "set_project_start", "date": "2026-10-04"}, {"op": "clear_constraint", "id": 4}),
    )
    assert res.plan.project_start == date(2026, 10, 5)
    assert res.plan.tasks[3].constraint_start is None


def test_invalid_field_values_become_operation_error():
    with pytest.raises(OperationError):
        apply_operations(base_plan(), ops({"op": "update_task", "id": 1, "name": ""}))


def test_requires_confirmation_thresholds():
    plan = base_plan()  # 4 tasks
    assert not requires_confirmation(plan, ops({"op": "delete_task", "id": 1}))
    assert not requires_confirmation(plan, ops({"op": "delete_task", "id": 1}, {"op": "delete_task", "id": 2}))
    assert requires_confirmation(
        plan, ops({"op": "delete_task", "id": 1}, {"op": "delete_task", "id": 2}, {"op": "delete_task", "id": 3})
    )
    big = Plan(project_start=MON, tasks=[Task(id=i, name=str(i), duration=1) for i in range(1, 21)])
    six = ops(*({"op": "delete_task", "id": i} for i in range(1, 7)))
    assert requires_confirmation(big, six)


def test_move_task_requires_exactly_one_target():
    with pytest.raises(ValueError):
        ops({"op": "move_task", "id": 1})
    with pytest.raises(ValueError):
        ops({"op": "move_task", "id": 1, "shift_days": 1, "start_date": "2026-09-22"})
```

`backend/tests/unit/test_diff.py`:
```python
from datetime import date

from app.domain.diff import diff_plans, format_predecessors, summarize_changes
from app.domain.models import Dependency, Plan, Task
from app.domain.scheduler import schedule

MON = date(2026, 9, 21)


def test_format_predecessors():
    deps = [
        Dependency(predecessor_id=5, successor_id=9, lag=2),
        Dependency(predecessor_id=3, successor_id=9),
        Dependency(predecessor_id=1, successor_id=2),
    ]
    assert format_predecessors(deps, 9) == "3, 5+2"
    assert format_predecessors(deps, 1) == ""


def test_diff_detects_created_deleted_and_field_changes():
    before = schedule(
        Plan(project_start=MON, tasks=[Task(id=1, name="A", duration=1), Task(id=2, name="B", duration=1)])
    )
    after = schedule(
        Plan(
            project_start=MON,
            tasks=[Task(id=1, name="A2", duration=2, assignee="Анна"), Task(id=3, name="C", duration=1)],
            dependencies=[Dependency(predecessor_id=1, successor_id=3)],
        )
    )
    changes = diff_plans(before, after)
    as_tuples = {(c.task_id, c.field, c.before, c.after) for c in changes}
    assert (1, "name", "A", "A2") in as_tuples
    assert (1, "duration", 1, 2) in as_tuples
    assert (1, "assignee", None, "Анна") in as_tuples
    assert (1, "end", "2026-09-21", "2026-09-22") in as_tuples
    assert (3, "created", None, "C") in as_tuples
    assert (2, "deleted", "B", None) in as_tuples
    assert summarize_changes(changes) == "Изменено задач: 1, добавлено: 1, удалено: 1"


def test_summary_without_changes():
    assert summarize_changes([]) == "Без изменений"
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd backend && uv run pytest tests/unit/test_operations.py tests/unit/test_diff.py -q`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement** `backend/app/domain/diff.py`

```python
from datetime import date
from typing import Literal

from pydantic import BaseModel

from app.domain.models import Dependency
from app.domain.scheduler import ScheduledPlan, ScheduledTask

ChangeField = Literal[
    "created", "deleted", "name", "description", "assignee", "duration",
    "constraint_start", "predecessors", "start", "end",
]
_TRACKED: tuple[ChangeField, ...] = (
    "name", "description", "assignee", "duration", "constraint_start", "start", "end",
)


class Change(BaseModel):
    task_id: int
    task_name: str
    field: ChangeField
    before: str | int | None = None
    after: str | int | None = None


def format_predecessors(deps: list[Dependency], task_id: int) -> str:
    items = sorted((d for d in deps if d.successor_id == task_id), key=lambda d: d.predecessor_id)
    return ", ".join(f"{d.predecessor_id}+{d.lag}" if d.lag else str(d.predecessor_id) for d in items)


def _value(task: ScheduledTask, field: ChangeField) -> str | int | None:
    raw = getattr(task, field)
    if isinstance(raw, date):
        return raw.isoformat()
    if raw is None or isinstance(raw, (str, int)):
        return raw
    raise TypeError(field)


def diff_plans(before: ScheduledPlan, after: ScheduledPlan) -> list[Change]:
    old = {t.id: t for t in before.tasks}
    new = {t.id: t for t in after.tasks}
    changes: list[Change] = []
    for task in after.tasks:
        prev = old.get(task.id)
        if prev is None:
            changes.append(Change(task_id=task.id, task_name=task.name, field="created", after=task.name))
            continue
        for field in _TRACKED:
            b, a = _value(prev, field), _value(task, field)
            if b != a:
                changes.append(Change(task_id=task.id, task_name=task.name, field=field, before=b, after=a))
        pb = format_predecessors(before.dependencies, task.id)
        pa = format_predecessors(after.dependencies, task.id)
        if pb != pa:
            changes.append(
                Change(task_id=task.id, task_name=task.name, field="predecessors", before=pb, after=pa)
            )
    for task in before.tasks:
        if task.id not in new:
            changes.append(Change(task_id=task.id, task_name=task.name, field="deleted", before=task.name))
    return changes


def summarize_changes(changes: list[Change]) -> str:
    created = {c.task_id for c in changes if c.field == "created"}
    deleted = {c.task_id for c in changes if c.field == "deleted"}
    changed = {c.task_id for c in changes} - created - deleted
    if not changes:
        return "Без изменений"
    parts = [f"Изменено задач: {len(changed)}"]
    if created:
        parts.append(f"добавлено: {len(created)}")
    if deleted:
        parts.append(f"удалено: {len(deleted)}")
    return ", ".join(parts)
```

- [ ] **Step 4: Implement** `backend/app/domain/operations.py`

```python
"""Plan operations. A batch is applied to a copy and validated as a whole (atomic)."""

from collections.abc import Sequence
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError, model_validator

from app.domain.calendar import add_workdays, next_workday
from app.domain.diff import Change, diff_plans
from app.domain.errors import OperationError, PlanValidationError
from app.domain.models import MAX_TASKS, Dependency, Plan, Task
from app.domain.scheduler import ScheduledPlan, schedule


class PredRef(BaseModel):
    id: int
    lag: int = Field(default=0, ge=0, le=365)


class AddTask(BaseModel):
    op: Literal["add_task"]
    name: str
    description: str = ""
    assignee: str | None = None
    duration: int = Field(ge=1, le=999)
    predecessors: list[PredRef] = Field(default_factory=list)
    after_id: int | None = None


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
    lag: int = Field(default=0, ge=0, le=365)


class RemoveDependency(BaseModel):
    op: Literal["remove_dependency"]
    predecessor_id: int
    successor_id: int


class DeleteTask(BaseModel):
    op: Literal["delete_task"]
    id: int


class SetProjectStart(BaseModel):
    op: Literal["set_project_start"]
    date: date


Operation = Annotated[
    AddTask | UpdateTask | MoveTask | ClearConstraint | SetDependencies | AddDependency
    | RemoveDependency | DeleteTask | SetProjectStart,
    Field(discriminator="op"),
]
operations_adapter: TypeAdapter[list[Operation]] = TypeAdapter(list[Operation])


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


def apply_operations(plan: Plan, ops: Sequence[Operation]) -> ApplyResult:
    before = schedule(plan)
    work = plan.model_copy(deep=True)
    created: list[int] = []
    moved: list[int] = []
    for index, op in enumerate(ops):
        try:
            _apply_one(work, op, created, moved)
        except OperationError as exc:
            exc.index = index
            exc.message = f"Операция {index + 1} ({op.op}): {exc.message}"
            raise
        except (ValidationError, PlanValidationError, ValueError) as exc:
            raise OperationError(f"Операция {index + 1} ({op.op}): {_short(exc)}", index=index) from exc
    if len(work.tasks) > MAX_TASKS:
        raise OperationError(f"В плане не может быть больше {MAX_TASKS} задач")
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
            plan.dependencies[i] = Dependency(predecessor_id=pred, successor_id=succ, lag=max(lag, d.lag))
            return
    plan.dependencies.append(Dependency(predecessor_id=pred, successor_id=succ, lag=lag))


def _apply_one(plan: Plan, op: Operation, created: list[int], moved: list[int]) -> None:
    match op:
        case AddTask():
            new_id = plan.last_id + 1
            task = Task(
                id=new_id, name=op.name, description=op.description,
                assignee=op.assignee, duration=op.duration,
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
                target = add_workdays(schedule(plan).task(op.id).start, op.shift_days)
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
            before = len(plan.dependencies)
            plan.dependencies = [
                d for d in plan.dependencies
                if not (d.predecessor_id == op.predecessor_id and d.successor_id == op.successor_id)
            ]
            if len(plan.dependencies) == before:
                raise OperationError(f"связи {op.predecessor_id} → {op.successor_id} нет")
        case DeleteTask():
            task = _get(plan, op.id)
            incoming = [d for d in plan.dependencies if d.successor_id == op.id]
            outgoing = [d for d in plan.dependencies if d.predecessor_id == op.id]
            plan.dependencies = [
                d for d in plan.dependencies if op.id not in (d.predecessor_id, d.successor_id)
            ]
            plan.tasks.remove(task)
            for a in incoming:
                for b in outgoing:
                    _upsert_dep(plan, a.predecessor_id, b.successor_id, a.lag + b.lag)
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
```

Note for the implementer: `move_task` before the project start is also reported (the `else` branch). Keep the `assert` narrowing for mypy; the model validator guarantees it.

- [ ] **Step 5: Verify**

Run: `cd backend && uv run pytest tests/unit -q && uv run ruff check . && uv run ruff format --check . && uv run mypy app`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add backend
git commit -m "feat(domain): add atomic plan operations and change diff"
```

---

### Task 4: Demo seed + compact plan rendering for the LLM

**Files:**
- Create: `backend/app/domain/seed.py`, `backend/app/domain/render.py`
- Test: `backend/tests/unit/test_seed_render.py`

**Interfaces:**
- Consumes: Tasks 2–3.
- Produces: `seed.build_demo_plan(today: date) -> Plan` (project_start = Monday of the week containing `today - 14 days`); `render.render_plan_table(sp: ScheduledPlan, today: date) -> str`.

- [ ] **Step 1: Write failing test** `backend/tests/unit/test_seed_render.py`

```python
from datetime import date

from app.domain.render import render_plan_table
from app.domain.scheduler import schedule
from app.domain.seed import build_demo_plan

TODAY = date(2026, 9, 25)  # Friday


def test_demo_plan_shape():
    plan = build_demo_plan(TODAY)
    sp = schedule(plan)
    assert plan.project_start == date(2026, 9, 7)  # Monday of the week of 2026-09-11
    assert 22 <= len(plan.tasks) <= 28
    assert len({t.assignee for t in plan.tasks}) == 6
    assert sp.critical_path, "demo must have a critical path"
    assert any(t.overallocated_with for t in sp.tasks), "demo must contain an overallocation"
    assert any(d.lag > 0 for d in plan.dependencies)
    assert sp.project_start <= TODAY <= sp.project_end
    assert all(t.description for t in plan.tasks)


def test_render_contains_header_rows_and_next_id():
    sp = schedule(build_demo_plan(TODAY))
    text = render_plan_table(sp, TODAY)
    assert "Сегодня: 2026-09-25" in text
    assert "Следующий свободный id: " + str(sp.last_id + 1) in text
    assert "id | задача | исполнитель | длит | предш | не раньше | начало | конец | резерв | флаги" in text
    assert text.count("\n") >= len(sp.tasks) + 3
    assert "крит" in text and "перегруз" in text
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_seed_render.py -q` → FAIL (module missing).

- [ ] **Step 3: Implement** `backend/app/domain/seed.py`

```python
"""Demo plan: «Запуск MVP мобильного приложения». All people are fictional."""

from datetime import date, timedelta

from app.domain.models import Dependency, Plan, Task

ANNA, IGOR, MARIA = "Анна Смирнова", "Игорь Петров", "Мария Козлова"
DMITRY, OLGA, SERGEY = "Дмитрий Волков", "Ольга Никитина", "Сергей Орлов"

# (id, name, description, assignee, duration, predecessors[(id, lag)])
_TASKS: list[tuple[int, str, str, str, int, list[tuple[int, int]]]] = [
    (1, "Сбор требований и приоритизация", "Интервью со стейкхолдерами, бэклог MVP", ANNA, 3, []),
    (2, "Исследование пользователей", "10 интервью с целевой аудиторией, персоны", MARIA, 4, []),
    (3, "Архитектура бэкенда", "Схема сервисов, модель данных, выбор инфраструктуры", IGOR, 3, [(1, 0)]),
    (4, "Сценарии и wireframes", "Ключевые пользовательские потоки в низкой детализации", MARIA, 5, [(1, 0), (2, 0)]),
    (5, "UI-кит и дизайн-система", "Цвета, типографика, компоненты", MARIA, 4, [(4, 0)]),
    (6, "Макеты ключевых экранов", "Онбординг, каталог, корзина, профиль", MARIA, 6, [(5, 0)]),
    (7, "CI/CD и окружения", "Пайплайны сборки, stage и prod окружения", IGOR, 2, [(3, 0)]),
    (8, "API авторизации", "Регистрация, вход по телефону, токены", IGOR, 4, [(3, 0)]),
    (9, "API каталога и поиска", "Категории, карточки товаров, полнотекстовый поиск", IGOR, 6, [(8, 0)]),
    (10, "API заказов и оплаты", "Корзина, заказы, интеграция с эквайрингом", IGOR, 7, [(8, 0)]),
    (11, "Каркас мобильного приложения", "Навигация, сетевой слой, хранилище", DMITRY, 3, [(3, 0), (7, 0)]),
    (12, "Экраны онбординга и входа", "Верстка и интеграция с API авторизации", DMITRY, 4, [(6, 0), (8, 0)]),
    (13, "Экран каталога", "Список, фильтры, карточка товара", DMITRY, 5, [(6, 0), (9, 0)]),
    (14, "Корзина и оформление заказа", "Корзина, чекаут, оплата", DMITRY, 6, [(10, 0), (13, 0)]),
    (15, "Push-уведомления", "Подключение FCM/APNs, настройки уведомлений", DMITRY, 3, [(11, 0)]),
    (16, "Аналитика и трекинг событий", "SDK аналитики, схема событий воронки", DMITRY, 4, [(11, 0)]),
    (17, "Тест-план и тест-кейсы", "Покрытие ключевых сценариев", OLGA, 4, [(4, 0)]),
    (18, "Функциональное тестирование", "Регресс по тест-кейсам на iOS и Android", OLGA, 6, [(12, 0), (13, 0), (14, 0)]),
    (19, "Нагрузочное тестирование API", "Сценарии пиковой нагрузки, отчёт", OLGA, 3, [(9, 0), (10, 0)]),
    (20, "Исправление критичных багов", "Баги блокеры и критичные по итогам тестирования", IGOR, 4, [(18, 0), (19, 0)]),
    (21, "Лендинг и материалы для сторов", "Скриншоты, описание, лендинг", SERGEY, 5, [(6, 0)]),
    (22, "Маркетинговая кампания запуска", "Таргет, PR, рассылка по базе", SERGEY, 7, [(21, 0)]),
    (23, "Публикация в App Store и Google Play", "Сборки, заполнение карточек, отправка на ревью", DMITRY, 3, [(20, 0), (21, 0)]),
    (24, "Бета-тест с фокус-группой", "Закрытая бета на 50 пользователей", ANNA, 5, [(23, 2)]),
    (25, "Релиз и ретроспектива", "Публичный релиз, разбор итогов", ANNA, 2, [(22, 0), (24, 0)]),
]


def build_demo_plan(today: date) -> Plan:
    anchor = today - timedelta(days=14)
    project_start = anchor - timedelta(days=anchor.weekday())
    tasks = [
        Task(id=i, name=n, description=d, assignee=a, duration=dur)
        for i, n, d, a, dur, _ in _TASKS
    ]
    deps = [
        Dependency(predecessor_id=p, successor_id=i, lag=lag)
        for i, *_, preds in _TASKS
        for p, lag in preds
    ]
    return Plan(project_start=project_start, tasks=tasks, dependencies=deps)
```

- [ ] **Step 4: Implement** `backend/app/domain/render.py`

```python
from datetime import date

from app.domain.diff import format_predecessors
from app.domain.scheduler import ScheduledPlan


def render_plan_table(sp: ScheduledPlan, today: date) -> str:
    lines = [
        f"Сегодня: {today.isoformat()}. Старт проекта: {sp.project_start.isoformat()}. "
        f"Окончание: {sp.project_end.isoformat()}. Задач: {len(sp.tasks)}. "
        f"Следующий свободный id: {sp.last_id + 1}.",
        "Критический путь: " + (", ".join(map(str, sp.critical_path)) or "—"),
        "id | задача | исполнитель | длит | предш | не раньше | начало | конец | резерв | флаги",
    ]
    for t in sp.tasks:
        flags = []
        if t.is_critical:
            flags.append("крит")
        if t.overallocated_with:
            flags.append("перегруз(" + ",".join(map(str, t.overallocated_with)) + ")")
        lines.append(
            " | ".join(
                [
                    str(t.id),
                    t.name,
                    t.assignee or "—",
                    str(t.duration),
                    format_predecessors(sp.dependencies, t.id) or "—",
                    t.constraint_start.isoformat() if t.constraint_start else "—",
                    t.start.isoformat(),
                    t.end.isoformat(),
                    str(t.slack),
                    " ".join(flags) or "—",
                ]
            )
        )
    return "\n".join(lines)
```

- [ ] **Step 5: Verify**

Run: `cd backend && uv run pytest tests/unit -q && uv run ruff check . && uv run ruff format --check . && uv run mypy app`
Expected: all pass. If `test_demo_plan_shape` fails on the overallocation or "today inside project" asserts, adjust `_TASKS` durations (not the test) until it passes, keeping 6 people and ≥1 lag.

- [ ] **Step 6: Commit**

```bash
git add backend
git commit -m "feat(domain): add demo seed plan and LLM plan rendering"
```

---

### Task 5: Excel import parser

**Files:**
- Create: `backend/app/excel/__init__.py`, `backend/app/excel/headers.py`, `backend/app/excel/parse.py`
- Test: `backend/tests/unit/test_excel_parse.py`

**Interfaces:**
- Consumes: Tasks 1–2 (`Plan`, `Task`, `Dependency`, `topological_order`, `CycleError`, `next_workday`, `MAX_TASKS`).
- Produces: `headers.COLUMN_SYNONYMS: dict[str, tuple[str, ...]]` with keys `number, name, description, assignee, duration, predecessors, constraint`; `parse.ImportIssue(row: int | None, message: str)`; `parse.ImportResult(ok: bool, plan: Plan | None, errors: list[ImportIssue], warnings: list[ImportIssue])`; `parse.parse_plan_xlsx(data: bytes, project_start: date) -> ImportResult`; `parse.parse_duration(value: object) -> tuple[int, str | None]` (raises `ValueError` if unparseable; second item is a warning message).

- [ ] **Step 1: Add dependencies**

Run: `cd backend && uv add openpyxl defusedxml && uv add --dev types-openpyxl`

- [ ] **Step 2: Write failing test** `backend/tests/unit/test_excel_parse.py`

```python
from datetime import date, datetime
from io import BytesIO

import pytest
from openpyxl import Workbook

from app.excel.parse import parse_duration, parse_plan_xlsx

MON = date(2026, 9, 21)


def xlsx(rows: list[list[object]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


HEADER = ["Задача", "Описание", "Исполнитель", "Длительность", "Предшественники"]


@pytest.mark.parametrize(
    ("raw", "expected", "warns"),
    [
        (5, 5, False), (5.0, 5, False), ("5", 5, False), ("5д", 5, False), ("5 д.", 5, False),
        ("5 дн", 5, False), ("5 дней", 5, False), ("5d", 5, False), ("1 нед", 5, False),
        ("2w", 10, False), ("2,5", 3, True), (None, 1, True), (0, 1, True), ("", 1, True),
    ],
)
def test_parse_duration(raw, expected, warns):
    value, warning = parse_duration(raw)
    assert value == expected
    assert (warning is not None) == warns


def test_parse_duration_invalid():
    with pytest.raises(ValueError):
        parse_duration("долго")


def test_basic_import_by_row_order_and_names():
    data = xlsx(
        [
            HEADER,
            ["Анализ", "Сбор требований", "Анна", 3, None],
            ["Дизайн", "", "Мария", "5д", "1"],
            ["Разработка", None, "Игорь", "2 нед", "1; Дизайн+2"],
        ]
    )
    res = parse_plan_xlsx(data, MON)
    assert res.ok, res.errors
    plan = res.plan
    assert [t.id for t in plan.tasks] == [1, 2, 3]
    assert plan.tasks[2].duration == 10
    assert {(d.predecessor_id, d.successor_id, d.lag) for d in plan.dependencies} == {
        (1, 2, 0), (1, 3, 0), (2, 3, 2),
    }
    assert plan.project_start == MON


def test_header_not_on_first_row_blank_rows_and_long_text():
    data = xlsx(
        [
            ["План переезда офиса"],
            [],
            ["№", "Название", "Ответственный", "Дни", "Зависимости"],
            [10, "Упаковка", "Олег", 2, None],
            [None, None, None, None, None],
            [20, "Х" * 250, None, 1, 10.0],
            [],
        ]
    )
    res = parse_plan_xlsx(data, MON)
    assert res.ok, res.errors
    assert [t.id for t in res.plan.tasks] == [10, 20]
    assert len(res.plan.tasks[1].name) == 200
    assert any("обрезано" in w.message for w in res.warnings)
    assert res.plan.dependencies[0].predecessor_id == 10


@pytest.mark.parametrize("cell", [3.0, "3.0", "3FS+2", "3 + 2д"])
def test_numeric_predecessor_variants(cell):
    rows = [HEADER] + [[f"T{i}", "", None, 1, None] for i in range(1, 4)] + [["T4", "", None, 1, cell]]
    res = parse_plan_xlsx(xlsx(rows), MON)
    assert res.ok, res.errors
    dep = res.plan.dependencies[0]
    assert (dep.predecessor_id, dep.successor_id) == (3, 4)
    assert dep.lag == (2 if "+" in str(cell) else 0)


def test_constraint_column_and_weekend_project_start():
    data = xlsx(
        [
            HEADER + ["Не раньше"],
            ["A", "", None, 1, None, datetime(2026, 10, 5)],
            ["B", "", None, 1, None, "06.10.2026"],
        ]
    )
    res = parse_plan_xlsx(data, date(2026, 9, 27))  # Sunday
    assert res.ok, res.errors
    assert res.plan.project_start == date(2026, 9, 28)
    assert [t.constraint_start for t in res.plan.tasks] == [date(2026, 10, 5), date(2026, 10, 6)]


def test_errors_are_collected_with_rows():
    data = xlsx(
        [
            HEADER,
            ["A", "", None, 1, "5"],       # row 2: unknown ref
            ["", "", None, 1, None],        # row 3: empty name but other cells -> error
            ["C", "", None, "долго", None],  # row 4: bad duration
            ["D", "", None, 1, "D"],         # row 5: self reference
        ]
    )
    res = parse_plan_xlsx(data, MON)
    assert not res.ok and res.plan is None
    rows = {e.row for e in res.errors}
    assert {2, 3, 4, 5} <= rows


def test_ambiguous_name_and_cycle():
    ambiguous = xlsx([HEADER, ["A", "", None, 1, None], ["A", "", None, 1, None], ["B", "", None, 1, "A"]])
    res = parse_plan_xlsx(ambiguous, MON)
    assert not res.ok and any("укажите №" in e.message for e in res.errors)

    cycle = xlsx([HEADER, ["A", "", None, 1, "2"], ["B", "", None, 1, "1"]])
    res = parse_plan_xlsx(cycle, MON)
    assert not res.ok and any("Циклическая зависимость" in e.message for e in res.errors)


def test_missing_required_columns_and_empty_file():
    res = parse_plan_xlsx(xlsx([["Описание", "Исполнитель"], ["x", "y"]]), MON)
    assert not res.ok and "задача" in res.errors[0].message.lower()
    res = parse_plan_xlsx(xlsx([HEADER]), MON)
    assert not res.ok and "нет задач" in res.errors[0].message


def test_not_an_xlsx():
    res = parse_plan_xlsx(b"not a zip", MON)
    assert not res.ok and "xlsx" in res.errors[0].message


def test_duplicate_numbers_and_too_many_tasks():
    dup = xlsx([["№", *HEADER], [1, "A", "", None, 1, None], [1, "B", "", None, 1, None]])
    assert any("повторяется" in e.message for e in parse_plan_xlsx(dup, MON).errors)
    many = xlsx([HEADER] + [[f"T{i}", "", None, 1, None] for i in range(501)])
    assert any("500" in e.message for e in parse_plan_xlsx(many, MON).errors)
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd backend && uv run pytest tests/unit/test_excel_parse.py -q` → FAIL (module missing).

- [ ] **Step 4: Implement** `backend/app/excel/headers.py`

```python
COLUMN_SYNONYMS: dict[str, tuple[str, ...]] = {
    "number": ("№", "#", "id", "номер", "n"),
    "name": ("задача", "название", "наименование", "task", "name"),
    "description": ("описание", "description"),
    "assignee": ("исполнитель", "ответственный", "assignee", "owner", "resource"),
    "duration": ("длительность", "дни", "длительность (дн)", "duration", "days"),
    "predecessors": ("предшественники", "зависимости", "predecessors", "depends on"),
    "constraint": ("не раньше", "начать не раньше", "snet", "start no earlier than"),
}


def normalize_header(value: object) -> str:
    return " ".join(str(value).strip().casefold().split()) if value is not None else ""


def match_column(value: object) -> str | None:
    header = normalize_header(value)
    for key, names in COLUMN_SYNONYMS.items():
        if header in names:
            return key
    return None
```

- [ ] **Step 5: Implement** `backend/app/excel/parse.py`

```python
"""Tolerant .xlsx plan importer (spec §9.1). Collects all issues instead of failing fast."""

import math
import re
import zipfile
from datetime import date, datetime
from io import BytesIO
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException
from pydantic import BaseModel

from app.domain.calendar import next_workday
from app.domain.errors import CycleError
from app.domain.models import MAX_TASKS, Dependency, Plan, Task
from app.domain.scheduler import topological_order
from app.excel.headers import match_column

LIMITS = {"name": 200, "description": 2000, "assignee": 100}
_DURATION_RE = re.compile(
    r"^(\d+(?:[.,]\d+)?)\s*(д|дн|дня|дней|день|d|day|days|н|нед|недел[яьи]|w|wk|week|weeks)?\.?$"
)
_NUM_REF_RE = re.compile(r"^(\d+)(?:[.,]0+)?\s*(?:fs|он)?\s*(?:\+\s*(\d+)\s*(?:д|дн|d)?\.?)?$")
_NAME_LAG_RE = re.compile(r"^(.*?)\s*\+\s*(\d+)\s*(?:д|дн|d)?\.?$")
_OTHER_TYPES_RE = re.compile(r"^\d+\s*(ss|ff|sf|нн|оо|но)\b")


class ImportIssue(BaseModel):
    row: int | None
    message: str


class ImportResult(BaseModel):
    ok: bool
    plan: Plan | None
    errors: list[ImportIssue]
    warnings: list[ImportIssue]


def parse_duration(value: object) -> tuple[int, str | None]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return 1, "длительность не указана, принята 1 день"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number, unit = float(value), None
    else:
        m = _DURATION_RE.match(str(value).strip().casefold())
        if not m:
            raise ValueError(f"не удалось распознать длительность «{value}»")
        number, unit = float(m.group(1).replace(",", ".")), m.group(2)
    if unit and unit[0] in ("н", "w"):
        number *= 5
    if number <= 0:
        return 1, "длительность 0, принята 1 день"
    days = math.ceil(number)
    warning = None if days == number else f"длительность {number:g} округлена до {days}"
    if days > 999:
        raise ValueError("длительность больше 999 дней")
    return days, warning


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _fail(errors: list[ImportIssue], warnings: list[ImportIssue]) -> ImportResult:
    return ImportResult(ok=False, plan=None, errors=errors, warnings=warnings)


def parse_plan_xlsx(data: bytes, project_start: date) -> ImportResult:
    errors: list[ImportIssue] = []
    warnings: list[ImportIssue] = []
    try:
        wb = load_workbook(BytesIO(data), read_only=True, data_only=True)
    except (zipfile.BadZipFile, InvalidFileException, KeyError, OSError, ValueError):
        return _fail([ImportIssue(row=None, message="Файл не является корректным .xlsx")], [])
    ws = wb.worksheets[0]
    rows = [(i, list(r)) for i, r in enumerate(ws.iter_rows(values_only=True), start=1)]
    wb.close()

    header_row: int | None = None
    columns: dict[str, int] = {}
    for row_no, values in rows[:30]:
        mapped = {key: idx for idx, v in enumerate(values) if (key := match_column(v))}
        if "name" in mapped:
            header_row, columns = row_no, mapped
            break
    if header_row is None or "duration" not in columns:
        missing = "«Задача»" if header_row is None else "«Длительность»"
        return _fail([ImportIssue(row=None, message=f"Не найдена обязательная колонка {missing}")], [])

    def cell(values: list[Any], key: str) -> Any:
        idx = columns.get(key)
        return values[idx] if idx is not None and idx < len(values) else None

    raw_tasks: list[dict[str, Any]] = []
    for row_no, values in rows:
        if row_no <= header_row or all(_text(v) == "" for v in values):
            continue
        name = _text(cell(values, "name"))
        if not name:
            errors.append(ImportIssue(row=row_no, message="Пустое название задачи"))
            continue
        item: dict[str, Any] = {"row": row_no, "name": name}
        for key in ("name", "description", "assignee"):
            text = name if key == "name" else _text(cell(values, key))
            if len(text) > LIMITS[key]:
                text = text[: LIMITS[key]]
                warnings.append(ImportIssue(row=row_no, message=f"Поле «{key}» обрезано до {LIMITS[key]} символов"))
            item[key] = text
        try:
            item["duration"], warn = parse_duration(cell(values, "duration"))
            if warn:
                warnings.append(ImportIssue(row=row_no, message=warn.capitalize()))
        except ValueError as exc:
            errors.append(ImportIssue(row=row_no, message=str(exc).capitalize()))
            continue
        item["number"] = cell(values, "number")
        item["preds"] = _text(cell(values, "predecessors"))
        raw_constraint = cell(values, "constraint")
        item["constraint"] = _as_date(raw_constraint) if _text(raw_constraint) else None
        if _text(raw_constraint) and item["constraint"] is None:
            warnings.append(ImportIssue(row=row_no, message=f"Дата «{raw_constraint}» не распознана и пропущена"))
        raw_tasks.append(item)

    if not raw_tasks and not errors:
        return _fail([ImportIssue(row=None, message="В файле нет задач")], warnings)
    if len(raw_tasks) > MAX_TASKS:
        return _fail([ImportIssue(row=None, message=f"Больше {MAX_TASKS} задач в файле")], warnings)

    # ids: from «№» column if present, else 1..n
    use_numbers = "number" in columns
    ids: dict[int, int] = {}  # row -> id
    seen: dict[int, int] = {}
    for position, item in enumerate(raw_tasks, start=1):
        if not use_numbers:
            ids[item["row"]] = position
            continue
        text = _text(item["number"])
        if not text.isdigit() or int(text) < 1:
            errors.append(ImportIssue(row=item["row"], message="«№» должен быть положительным целым числом"))
            continue
        number = int(text)
        if number in seen:
            errors.append(ImportIssue(row=item["row"], message=f"№ {number} повторяется (строка {seen[number]})"))
            continue
        seen[number] = item["row"]
        ids[item["row"]] = number

    by_position = {pos: ids.get(it["row"]) for pos, it in enumerate(raw_tasks, start=1)}
    by_name: dict[str, list[int]] = {}
    for it in raw_tasks:
        if it["row"] in ids:
            by_name.setdefault(it["name"].casefold(), []).append(ids[it["row"]])
    known_ids = set(ids.values())

    deps: dict[tuple[int, int], int] = {}
    for it in raw_tasks:
        row, succ = it["row"], ids.get(it["row"])
        if succ is None or not it["preds"]:
            continue
        for token in (t.strip() for t in re.split(r"[;,\n]", it["preds"]) if t.strip()):
            ref, lag = _resolve(token, use_numbers, by_position, by_name, known_ids)
            if isinstance(ref, str):
                errors.append(ImportIssue(row=row, message=ref))
                continue
            if ref == succ:
                errors.append(ImportIssue(row=row, message="Задача не может зависеть от самой себя"))
                continue
            deps[(ref, succ)] = max(lag, deps.get((ref, succ), 0))

    if errors:
        return _fail(errors, warnings)

    plan = Plan(
        project_start=next_workday(project_start),
        tasks=[
            Task(
                id=ids[it["row"]], name=it["name"], description=it["description"],
                assignee=it["assignee"] or None, duration=it["duration"],
                constraint_start=it["constraint"],
            )
            for it in raw_tasks
        ],
        dependencies=[Dependency(predecessor_id=p, successor_id=s, lag=lag) for (p, s), lag in deps.items()],
    )
    try:
        topological_order(plan)
    except CycleError as exc:
        rows_by_id = {v: k for k, v in ids.items()}
        rows = ", ".join(str(rows_by_id[i]) for i in exc.cycle)
        return _fail([ImportIssue(row=None, message=f"{exc.message} (строки {rows})")], warnings)
    return ImportResult(ok=True, plan=plan, errors=[], warnings=warnings)


def _resolve(
    token: str,
    use_numbers: bool,
    by_position: dict[int, int | None],
    by_name: dict[str, list[int]],
    known_ids: set[int],
) -> tuple[int | str, int]:
    low = token.casefold()
    if _OTHER_TYPES_RE.match(low):
        return "Поддерживается только тип связи «окончание–начало»", 0
    m = _NUM_REF_RE.match(low)
    if m:
        number, lag = int(m.group(1)), int(m.group(2) or 0)
        ref = number if use_numbers else by_position.get(number)
        if ref is None or ref not in known_ids:
            return f"Предшественник «{token}» не найден", 0
        return ref, lag
    name, lag = token, 0
    m = _NAME_LAG_RE.match(token)
    if m:
        name, lag = m.group(1), int(m.group(2))
    matches = by_name.get(name.strip().casefold(), [])
    if not matches:
        return f"Предшественник «{token}» не найден", 0
    if len(matches) > 1:
        return f"Название «{name}» встречается несколько раз — укажите №", 0
    return matches[0], lag
```

- [ ] **Step 6: Verify**

Run: `cd backend && uv run pytest tests/unit -q && uv run ruff check . && uv run ruff format --check . && uv run mypy app`
Expected: all pass. (`ruff format` may rewrap long lines — run it.)

- [ ] **Step 7: Commit**

```bash
git add backend
git commit -m "feat(excel): add tolerant xlsx plan importer"
```

---

### Task 6: Excel export + round-trip

**Files:**
- Create: `backend/app/excel/export.py`
- Test: `backend/tests/unit/test_excel_export.py`

**Interfaces:**
- Consumes: Tasks 2, 4, 5.
- Produces: `export.EXPORT_HEADERS: list[str]`; `export.export_plan_xlsx(sp: ScheduledPlan) -> bytes`.

- [ ] **Step 1: Write failing test** `backend/tests/unit/test_excel_export.py`

```python
from datetime import date
from io import BytesIO

from openpyxl import load_workbook

from app.domain.operations import apply_operations, operations_adapter
from app.domain.scheduler import schedule
from app.domain.seed import build_demo_plan
from app.excel.export import EXPORT_HEADERS, export_plan_xlsx
from app.excel.parse import parse_plan_xlsx

TODAY = date(2026, 9, 25)


def test_export_layout():
    sp = schedule(build_demo_plan(TODAY))
    wb = load_workbook(BytesIO(export_plan_xlsx(sp)))
    ws = wb["План"]
    assert [c.value for c in ws[1]] == EXPORT_HEADERS
    assert ws.freeze_panes == "A2"
    assert ws.max_row == len(sp.tasks) + 1
    first = sp.tasks[0]
    row = [c.value for c in ws[2]]
    assert row[0] == first.id and row[1] == first.name
    assert row[7].date() == first.start and row[8].date() == first.end


def test_round_trip_preserves_plan_including_constraints_and_lags():
    plan = build_demo_plan(TODAY)
    res = apply_operations(
        plan,
        operations_adapter.validate_python(
            [
                {"op": "move_task", "id": 21, "start_date": "2026-10-12"},
                {"op": "delete_task", "id": 7},
            ]
        ),
    )
    sp = res.scheduled
    imported = parse_plan_xlsx(export_plan_xlsx(sp), sp.project_start)
    assert imported.ok, imported.errors
    p2 = imported.plan
    assert [t.model_dump() for t in p2.tasks] == [t.model_dump() for t in res.plan.tasks]
    key = lambda d: (d.predecessor_id, d.successor_id, d.lag)  # noqa: E731
    assert sorted(map(key, p2.dependencies)) == sorted(map(key, res.plan.dependencies))
    sp2 = schedule(p2)
    assert [(t.start, t.end) for t in sp2.tasks] == [(t.start, t.end) for t in sp.tasks]
```

- [ ] **Step 2: Run to verify it fails** → `uv run pytest tests/unit/test_excel_export.py -q` FAIL.

- [ ] **Step 3: Implement** `backend/app/excel/export.py`

```python
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from app.domain.diff import format_predecessors
from app.domain.scheduler import ScheduledPlan

EXPORT_HEADERS = [
    "№", "Задача", "Описание", "Исполнитель", "Длительность", "Предшественники",
    "Не раньше", "Начало", "Окончание", "Резерв", "Критическая",
]
_WIDTHS = [6, 40, 50, 22, 14, 18, 13, 13, 13, 9, 12]
_DATE_COLUMNS = (7, 8, 9)  # 1-based: «Не раньше», «Начало», «Окончание»


def export_plan_xlsx(sp: ScheduledPlan) -> bytes:
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "План"
    ws.append(EXPORT_HEADERS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for t in sp.tasks:
        ws.append(
            [
                t.id, t.name, t.description, t.assignee or "", t.duration,
                format_predecessors(sp.dependencies, t.id), t.constraint_start,
                t.start, t.end, t.slack, "да" if t.is_critical else "",
            ]
        )
    for row in ws.iter_rows(min_row=2):
        for col in _DATE_COLUMNS:
            row[col - 1].number_format = "DD.MM.YYYY"
    for i, width in enumerate(_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
```

- [ ] **Step 4: Verify**

Run: `cd backend && uv run pytest tests/unit -q && uv run ruff check . && uv run ruff format --check . && uv run mypy app`
Expected: all pass. If the round-trip fails because `description` `""` vs `None` or constraint datetime vs date, fix the parser/exporter (not the test).

- [ ] **Step 5: Commit**

```bash
git add backend
git commit -m "feat(excel): add plan export with lossless round-trip"
```

---

### Verified library facts (probed 2026-09-25 — use these, not memory)

- `fastmcp==4.0.9` (pulls `mcp` 2.2.0). **`mcp.server.fastmcp` does not exist in mcp 2.x** — import `from fastmcp import FastMCP, Client, Context`.
  - Tools: `@mcp.tool` on `async def`; pydantic-typed params incl. `list[Operation]` discriminated unions; a `ctx: Context` param is excluded from the schema; returning a dict/pydantic model gives `structured_content`; invalid args → `is_error=True`.
  - In-memory client: `async with Client(mcp) as client:`; `await client.list_tools()` → objects with `.name`, `.description`, `.input_schema`; `await client.call_tool(name, args, raise_on_error=False)` → `.is_error`, `.content[0].text`, `.structured_content`. One long-lived client (opened in the FastAPI lifespan) handles concurrent calls.
  - **ContextVars set by the caller propagate into the tool per call** (verified with 6 concurrent calls). Alternative: `call_tool(..., meta={...})` + `ctx.request_context.meta`.
  - HTTP: `class V(TokenVerifier): async def verify_token(self, token) -> AccessToken | None` (`from fastmcp.server.auth import TokenVerifier, AccessToken`); `FastMCP("planner", auth=V())`; in tools `from fastmcp.server.dependencies import get_access_token` → `.claims["session_id"]` (returns `None` on the in-memory transport). `mcp_app = mcp.http_app(path="/", stateless_http=True, json_response=True)`; FastAPI must use `lifespan=combine_lifespans(app_lifespan, mcp_app.lifespan)` (verify import path with `python -c`); `app.mount("/mcp", mcp_app)`. `POST /mcp` (no slash) 307-redirects to `/mcp/` → add a tiny ASGI path rewrite `/mcp` → `/mcp/`. Bad/missing token → 401 with `WWW-Authenticate: Bearer`.
- `anthropic==1.8.0`: `AsyncAnthropic(api_key=..., max_retries=2, timeout=anthropic.Timeout(60.0, connect=5.0))`; `async with client.messages.stream(model=..., max_tokens=..., system=[...], tools=[...], messages=[...]) as stream: async for ev in stream: if ev.type == "text": ev.text`; `final = await stream.get_final_message()`; `final.stop_reason`, `final.content` blocks (`.type in {"text","tool_use"}`; tool_use has `.id .name .input`). Tool results go back as ONE user message `[{"type":"tool_result","tool_use_id":id,"content":text,"is_error":bool}]`. Cache: `"cache_control":{"type":"ephemeral"}` on the static system block and on the LAST tool definition. Errors: `RateLimitError` (429), `OverloadedError` (529), `InternalServerError` (5xx) — all `APIStatusError` with `.type`; `APITimeoutError`/`APIConnectionError`. **Mid-stream overload raises plain `APIStatusError` with `.type == "overloaded_error"`.** SDK uses `httpx2` internally (never pass `httpx` clients).
- `sse-starlette==3.4.x`: `EventSourceResponse(gen(), ping=15, sep="\n")`; `gen` yields `{"event": name, "data": json_str}`. Use it (not `fastapi.sse`) so pre-stream checks can still return JSON errors.
- FastAPI 0.141 / Starlette 1.7; SQLAlchemy 2.1 + asyncpg 0.31; Alembic 1.20 (`alembic init -t async app/migrations`, then set `target_metadata` and URL); pydantic-settings 2.15 (`Settings(_secrets_dir=path)`; env vars win over secret files).

---

### Task 7: Settings + database layer (models, Alembic, repository)

**Files:**
- Create: `backend/app/config.py`, `backend/app/db/__init__.py`, `backend/app/db/base.py`, `backend/app/db/models.py`, `backend/app/db/engine.py`, `backend/app/db/repo.py`, `backend/alembic.ini`, `backend/app/migrations/env.py`, `backend/app/migrations/script.py.mako`, `backend/app/migrations/versions/0001_initial.py`, `docker-compose.yml` (dev Postgres), `.env.example`
- Test: `backend/tests/integration/__init__.py`, `backend/tests/integration/conftest.py`, `backend/tests/integration/test_repo.py`

**Interfaces:**
- Produces:
  - `config.Settings` (env names upper-case): `db_host="localhost"`, `db_port=55432`, `db_name="planner"`, `db_user="planner_app"`, `db_password: SecretStr = SecretStr("planner")`, `database_url: str | None = None` (full override for tests/CI), `public_origin="http://localhost:8000"`, `cookie_secure=False`, `llm_provider: Literal["anthropic","fake"]="fake"`, `llm_model="claude-sonnet-5"`, `anthropic_api_key: SecretStr | None = None`, `llm_max_tokens=4096`, `chat_limit_per_hour=30`, `chat_limit_per_day=500`, `max_upload_mb=2`, `session_ttl_days=14`, `max_versions=50`, `static_dir: str | None = None`, `log_level="INFO"`; property `sqlalchemy_url -> str`; `get_settings() -> Settings` (lru_cache; `_secrets_dir=os.environ.get("SECRETS_DIR") or None`).
  - `db.models`: `SessionRow`, `PlanVersionRow`, `ChatMessageRow`, `McpTokenRow` (spec §11); `db.base.Base`.
  - `db.engine`: `make_engine(settings) -> AsyncEngine`, `make_sessionmaker(engine) -> async_sessionmaker[AsyncSession]`.
  - `db.repo` (all async, first arg `db: AsyncSession`, never commit — caller owns the transaction): `create_session`, `get_session_by_token_hash`, `get_session`, `touch_session`, `delete_session`, `add_version(db, *, session_id, version_no, snapshot, source, turn_id, summary, diff)`, `get_version`, `list_version_meta -> list[VersionMeta]` (`VersionMeta(NamedTuple): version_no: int; turn_id: UUID | None`, ascending), `delete_versions_after`, `prune_versions(db, session_id, keep)`, `add_chat_message(db, *, session_id, role, content, turn_id=None, meta=None)`, `recent_chat_messages(db, session_id, limit)` (chronological), `count_user_messages_since(db, since, session_id=None)`, `delete_expired_sessions(db, older_than) -> int`.

- [ ] **Step 1: Dependencies + dev database**

Run: `cd backend && uv add "sqlalchemy[asyncio]" asyncpg alembic pydantic-settings`

Root `docker-compose.yml` (dev only; prod compose lives in `deploy/`):
```yaml
services:
  db:
    image: postgres:17-alpine
    environment:
      POSTGRES_USER: planner
      POSTGRES_PASSWORD: planner
      POSTGRES_DB: planner
    ports:
      - "127.0.0.1:55432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U planner"]
      interval: 3s
      retries: 20
volumes:
  pgdata:
```
`.env.example`:
```
DB_HOST=localhost
DB_PORT=55432
DB_NAME=planner
DB_USER=planner
DB_PASSWORD=planner
PUBLIC_ORIGIN=http://localhost:5173
COOKIE_SECURE=false
LLM_PROVIDER=fake
LLM_MODEL=claude-sonnet-5
# ANTHROPIC_API_KEY=   (prod: file secret /run/secrets/anthropic_api_key)
```
Run `docker compose up -d db`, wait for healthy, then `docker compose exec db createdb -U planner planner_test`. If Docker is unavailable, report BLOCKED.

- [ ] **Step 2: `config.py`**

```python
import os
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    db_host: str = "localhost"
    db_port: int = 55432
    db_name: str = "planner"
    db_user: str = "planner_app"
    db_password: SecretStr = SecretStr("planner")
    database_url: str | None = None

    public_origin: str = "http://localhost:8000"
    cookie_secure: bool = False

    llm_provider: Literal["anthropic", "fake"] = "fake"
    llm_model: str = "claude-sonnet-5"
    anthropic_api_key: SecretStr | None = None
    llm_max_tokens: int = 4096

    chat_limit_per_hour: int = 30
    chat_limit_per_day: int = 500
    max_upload_mb: int = 2
    session_ttl_days: int = 14
    max_versions: int = 50
    static_dir: str | None = None
    log_level: str = "INFO"

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        return URL.create(
            "postgresql+asyncpg",
            username=self.db_user,
            password=self.db_password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        ).render_as_string(hide_password=False)


@lru_cache
def get_settings() -> Settings:
    return Settings(_secrets_dir=os.environ.get("SECRETS_DIR") or None)  # type: ignore[call-arg]
```

- [ ] **Step 3: base / models / engine**

`db/base.py`:
```python
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
```
`db/models.py`:
```python
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger, DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SessionRow(Base):
    __tablename__ = "sessions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True, nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _created()
    last_seen_at: Mapped[datetime] = _created()


class PlanVersionRow(Base):
    __tablename__ = "plan_versions"
    __table_args__ = (UniqueConstraint("session_id", "version_no"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    diff: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = _created()


class ChatMessageRow(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("ix_chat_session_created", "session_id", "created_at"),
        Index("ix_chat_created", "created_at"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | assistant | system
    content: Mapped[str] = mapped_column(Text, nullable=False)
    turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created()


class McpTokenRow(Base):
    __tablename__ = "mcp_tokens"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), unique=True, nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created()
```
`created_at` ties are possible within one transaction (`now()` is per-transaction) — order chat by `(created_at, id)`.

`db/engine.py`:
```python
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings


def make_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.sqlalchemy_url, pool_size=5, max_overflow=5, pool_pre_ping=True,
        pool_recycle=1800, pool_timeout=10,
    )


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
```

- [ ] **Step 4: Failing repo tests**

`tests/integration/conftest.py`:
```python
import os
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

import app.db.models  # noqa: F401  (register tables)
from app.db.base import Base

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://planner:planner@localhost:55432/planner_test"
)


@pytest.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(TEST_DB_URL)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def sessionmaker(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE sessions, plan_versions, chat_messages, mcp_tokens CASCADE"))
    yield async_sessionmaker(engine, expire_on_commit=False)
```
`tests/integration/test_repo.py`:
```python
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from app.db import repo
from app.db.models import SessionRow


async def test_session_lifecycle_and_cascade(sessionmaker):
    async with sessionmaker() as db, db.begin():
        s = await repo.create_session(db, b"h" * 32)
        await repo.add_version(db, session_id=s.id, version_no=1, snapshot={"a": 1}, source="seed",
                               turn_id=None, summary="Демо", diff=[])
        await repo.add_chat_message(db, session_id=s.id, role="user", content="привет")
    async with sessionmaker() as db, db.begin():
        found = await repo.get_session_by_token_hash(db, b"h" * 32)
        assert found is not None and found.id == s.id
        await repo.delete_session(db, s.id)
    async with sessionmaker() as db:
        assert await repo.get_version(db, s.id, 1) is None
        assert await repo.recent_chat_messages(db, s.id, 10) == []


async def test_versions_meta_delete_after_and_prune(sessionmaker):
    turn = uuid.uuid4()
    async with sessionmaker() as db, db.begin():
        s = await repo.create_session(db, b"v" * 32)
        for n in range(1, 6):
            await repo.add_version(db, session_id=s.id, version_no=n, snapshot={"n": n}, source="agent",
                                   turn_id=turn if n in (3, 4) else None, summary="", diff=[])
    async with sessionmaker() as db, db.begin():
        meta = await repo.list_version_meta(db, s.id)
        assert [m.version_no for m in meta] == [1, 2, 3, 4, 5]
        assert meta[2].turn_id == turn and meta[0].turn_id is None
        await repo.delete_versions_after(db, s.id, 3)
        await repo.prune_versions(db, s.id, keep=2)
    async with sessionmaker() as db:
        assert [m.version_no for m in await repo.list_version_meta(db, s.id)] == [2, 3]


async def test_chat_order_and_rate_counts(sessionmaker):
    async with sessionmaker() as db, db.begin():
        a = await repo.create_session(db, b"a" * 32)
        b = await repo.create_session(db, b"b" * 32)
        for i in range(3):
            await repo.add_chat_message(db, session_id=a.id, role="user", content=f"m{i}")
        await repo.add_chat_message(db, session_id=a.id, role="assistant", content="ответ")
        await repo.add_chat_message(db, session_id=b.id, role="user", content="x")
    since = datetime.now(UTC) - timedelta(hours=1)
    async with sessionmaker() as db:
        assert [m.content for m in await repo.recent_chat_messages(db, a.id, 3)] == ["m1", "m2", "ответ"]
        assert await repo.count_user_messages_since(db, since, a.id) == 3
        assert await repo.count_user_messages_since(db, since) == 4


async def test_delete_expired_sessions(sessionmaker):
    async with sessionmaker() as db, db.begin():
        old = await repo.create_session(db, b"o" * 32)
        fresh = await repo.create_session(db, b"f" * 32)
        await db.execute(
            update(SessionRow).where(SessionRow.id == old.id)
            .values(last_seen_at=datetime.now(UTC) - timedelta(days=30))
        )
    async with sessionmaker() as db, db.begin():
        assert await repo.delete_expired_sessions(db, datetime.now(UTC) - timedelta(days=14)) == 1
        assert await repo.get_session(db, fresh.id) is not None
```

- [ ] **Step 5: Run → FAIL** (`uv run pytest tests/integration/test_repo.py -q`, `app.db.repo` missing).

- [ ] **Step 6: Implement `db/repo.py`**

```python
import uuid
from datetime import datetime
from typing import Any, NamedTuple

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChatMessageRow, PlanVersionRow, SessionRow


class VersionMeta(NamedTuple):
    version_no: int
    turn_id: uuid.UUID | None


async def create_session(db: AsyncSession, token_hash: bytes) -> SessionRow:
    row = SessionRow(token_hash=token_hash, current_version=0)
    db.add(row)
    await db.flush()
    return row


async def get_session_by_token_hash(db: AsyncSession, token_hash: bytes) -> SessionRow | None:
    return await db.scalar(select(SessionRow).where(SessionRow.token_hash == token_hash))


async def get_session(db: AsyncSession, session_id: uuid.UUID) -> SessionRow | None:
    return await db.get(SessionRow, session_id)


async def touch_session(db: AsyncSession, session_id: uuid.UUID) -> None:
    await db.execute(update(SessionRow).where(SessionRow.id == session_id).values(last_seen_at=func.now()))


async def delete_session(db: AsyncSession, session_id: uuid.UUID) -> None:
    await db.execute(delete(SessionRow).where(SessionRow.id == session_id))


async def add_version(
    db: AsyncSession, *, session_id: uuid.UUID, version_no: int, snapshot: dict[str, Any],
    source: str, turn_id: uuid.UUID | None, summary: str, diff: list[dict[str, Any]],
) -> None:
    db.add(PlanVersionRow(session_id=session_id, version_no=version_no, snapshot=snapshot,
                          source=source, turn_id=turn_id, summary=summary, diff=diff))
    await db.flush()


async def get_version(db: AsyncSession, session_id: uuid.UUID, version_no: int) -> PlanVersionRow | None:
    return await db.scalar(
        select(PlanVersionRow).where(PlanVersionRow.session_id == session_id,
                                     PlanVersionRow.version_no == version_no)
    )


async def list_version_meta(db: AsyncSession, session_id: uuid.UUID) -> list[VersionMeta]:
    rows = await db.execute(
        select(PlanVersionRow.version_no, PlanVersionRow.turn_id)
        .where(PlanVersionRow.session_id == session_id).order_by(PlanVersionRow.version_no)
    )
    return [VersionMeta(v, t) for v, t in rows.all()]


async def delete_versions_after(db: AsyncSession, session_id: uuid.UUID, version_no: int) -> None:
    await db.execute(delete(PlanVersionRow).where(PlanVersionRow.session_id == session_id,
                                                  PlanVersionRow.version_no > version_no))


async def prune_versions(db: AsyncSession, session_id: uuid.UUID, keep: int) -> None:
    keep_from = await db.scalar(
        select(PlanVersionRow.version_no).where(PlanVersionRow.session_id == session_id)
        .order_by(PlanVersionRow.version_no.desc()).offset(keep - 1).limit(1)
    )
    if keep_from is not None:
        await db.execute(delete(PlanVersionRow).where(PlanVersionRow.session_id == session_id,
                                                      PlanVersionRow.version_no < keep_from))


async def add_chat_message(
    db: AsyncSession, *, session_id: uuid.UUID, role: str, content: str,
    turn_id: uuid.UUID | None = None, meta: dict[str, Any] | None = None,
) -> ChatMessageRow:
    row = ChatMessageRow(session_id=session_id, role=role, content=content, turn_id=turn_id, meta=meta or {})
    db.add(row)
    await db.flush()
    return row


async def recent_chat_messages(db: AsyncSession, session_id: uuid.UUID, limit: int) -> list[ChatMessageRow]:
    rows = await db.scalars(
        select(ChatMessageRow).where(ChatMessageRow.session_id == session_id)
        .order_by(ChatMessageRow.created_at.desc(), ChatMessageRow.id.desc()).limit(limit)
    )
    return list(reversed(rows.all()))


async def count_user_messages_since(
    db: AsyncSession, since: datetime, session_id: uuid.UUID | None = None
) -> int:
    stmt = select(func.count()).select_from(ChatMessageRow).where(
        ChatMessageRow.role == "user", ChatMessageRow.created_at >= since
    )
    if session_id is not None:
        stmt = stmt.where(ChatMessageRow.session_id == session_id)
    return int(await db.scalar(stmt) or 0)


async def delete_expired_sessions(db: AsyncSession, older_than: datetime) -> int:
    result = await db.execute(delete(SessionRow).where(SessionRow.last_seen_at < older_than))
    return int(result.rowcount or 0)
```

- [ ] **Step 7: Alembic**

Run `cd backend && uv run alembic init -t async app/migrations`; keep `alembic.ini` in `backend/` with `script_location = app/migrations`. In `app/migrations/env.py`:
```python
import app.db.models  # noqa: F401
from app.config import get_settings
from app.db.base import Base

config.set_main_option("sqlalchemy.url", get_settings().sqlalchemy_url.replace("%", "%%"))
target_metadata = Base.metadata
```
Generate `versions/0001_initial.py`: `DATABASE_URL=postgresql+asyncpg://planner:planner@localhost:55432/planner uv run alembic revision --autogenerate -m initial --rev-id 0001`, review by eye (4 tables, FKs `ondelete="CASCADE"`, both chat indexes, unique constraints). Verify `upgrade head` → `downgrade base` → `upgrade head` all succeed against the dev DB.

- [ ] **Step 8: Verify + commit**

Run: `cd backend && uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy app`
```bash
git add docker-compose.yml .env.example backend
git commit -m "feat(db): add settings, models, migrations and repository"
```

---

### Task 8: PlanService (versions, undo/redo groups, locks, events)

**Files:**
- Create: `backend/app/services/__init__.py`, `backend/app/services/errors.py`, `backend/app/services/events.py`, `backend/app/services/locks.py`, `backend/app/services/sessions.py`, `backend/app/services/plan_service.py`
- Test: `backend/tests/unit/test_history.py`, `backend/tests/integration/test_plan_service.py`

**Interfaces:**
- Consumes: Tasks 2–4 (domain), Task 7 (repo, sessionmaker).
- Produces:
  - `services/errors.py`: `AgentBusy` (`agent_busy`, "Агент сейчас редактирует план, подождите"), `RateLimited` (`rate_limited`), `NoSession` (`no_session`, "Сессия не найдена"), `NothingToUndo` (`nothing_to_undo`, "Нечего отменять"), `NothingToRedo` (`nothing_to_redo`, "Нечего повторять"), `NotFound` (`not_found`), `BadOrigin` (`bad_origin`), `FileTooLarge` (`file_too_large`) — all subclasses of `DomainError`.
  - `services/sessions.py`: `new_token() -> str` (`secrets.token_urlsafe(32)`), `hash_token(token) -> bytes` (sha256 digest).
  - `services/events.py`: `EventBus.subscribe(session_id) -> asyncio.Queue[dict]`, `.unsubscribe(session_id, queue)`, `.publish(session_id, event)` (maxsize 100, drops on full).
  - `services/locks.py`: `SessionLocks.lock(sid) -> asyncio.Lock`, `.is_busy(sid) -> bool`, `.agent_turn(sid)` (async CM, raises `AgentBusy` if busy), `.wait_not_busy(sid, timeout) -> bool`.
  - `services/plan_service.py`: `Source = Literal["seed","import","user","agent","mcp","reset"]`; `PlanState(version, plan, scheduled, can_undo, can_redo)`; `ApplyOutcome(state, changes, warnings, created_task_ids, summary)`; pure `undo_target(meta, current) -> int | None`, `redo_target(meta, current) -> int | None`; `PlanService(sessionmaker, bus, locks, *, max_versions=50, today=date.today)` exposing attributes `.sessionmaker`, `.bus`, `.locks` and methods `create_session() -> tuple[str, UUID]`, `resolve_session(token) -> UUID | None` (touches `last_seen_at`), `get_state(sid) -> PlanState`, `apply(sid, ops, *, source, turn_id=None, confirmed=False) -> ApplyOutcome`, `replace(sid, plan, *, source, summary, chat_note=None) -> PlanState`, `reset(sid) -> PlanState`, `undo(sid, *, source="user") -> PlanState`, `redo(sid) -> PlanState`, `delete_session(sid) -> None`.
  - Every successful mutation publishes `{"type":"plan_changed","version":int,"source":str,"turn_id":str|None,"changed_task_ids":list[int]}`.
  - Busy rules: `source="user"` while the agent turn runs → `AgentBusy`; `source="mcp"` waits up to 10 s then `AgentBusy`; `source="agent"` passes.

- [ ] **Step 1: Failing unit test** `tests/unit/test_history.py`

```python
import uuid

from app.db.repo import VersionMeta
from app.services.plan_service import redo_target, undo_target

T = uuid.uuid4()
META = [VersionMeta(1, None), VersionMeta(2, None), VersionMeta(3, T), VersionMeta(4, T), VersionMeta(5, None)]


def test_undo_single_version():
    assert undo_target(META, 5) == 4
    assert undo_target(META, 2) == 1


def test_undo_whole_turn_group():
    assert undo_target(META, 4) == 2


def test_undo_at_first_version_is_none():
    assert undo_target(META, 1) is None


def test_redo_skips_to_end_of_group():
    assert redo_target(META, 2) == 4
    assert redo_target(META, 4) == 5
    assert redo_target(META, 5) is None
```

- [ ] **Step 2: Failing integration tests** `tests/integration/test_plan_service.py`

```python
import asyncio
import uuid
from datetime import date

import pytest

from app.db import repo
from app.domain.errors import ConfirmationRequired, OperationError
from app.domain.operations import operations_adapter
from app.domain.seed import build_demo_plan
from app.services.errors import AgentBusy, NothingToUndo
from app.services.events import EventBus
from app.services.locks import SessionLocks
from app.services.plan_service import PlanService

TODAY = date(2026, 9, 25)


@pytest.fixture
def service(sessionmaker):
    return PlanService(sessionmaker, EventBus(), SessionLocks(), max_versions=50, today=lambda: TODAY)


def ops(*raw):
    return operations_adapter.validate_python(list(raw))


async def test_create_session_seeds_demo_plan(service):
    token, sid = await service.create_session()
    assert await service.resolve_session(token) == sid
    assert await service.resolve_session("nope") is None
    state = await service.get_state(sid)
    assert state.version == 1 and not state.can_undo and not state.can_redo
    assert state.plan == build_demo_plan(TODAY)


async def test_apply_publishes_event_and_versions(service):
    _, sid = await service.create_session()
    queue = service.bus.subscribe(sid)
    out = await service.apply(sid, ops({"op": "move_task", "id": 1, "shift_days": 1}), source="user")
    assert out.state.version == 2 and out.state.can_undo
    event = queue.get_nowait()
    assert event["type"] == "plan_changed" and event["version"] == 2 and 1 in event["changed_task_ids"]


async def test_no_change_batch_creates_no_version(service):
    _, sid = await service.create_session()
    out = await service.apply(
        sid, ops({"op": "update_task", "id": 1, "name": "Сбор требований и приоритизация"}), source="user"
    )
    assert out.state.version == 1 and out.summary == "Без изменений"


async def test_undo_redo_turn_group_and_truncate(service):
    _, sid = await service.create_session()
    turn = uuid.uuid4()
    await service.apply(sid, ops({"op": "update_task", "id": 1, "duration": 5}), source="agent", turn_id=turn)
    await service.apply(sid, ops({"op": "update_task", "id": 2, "duration": 5}), source="agent", turn_id=turn)
    state = await service.undo(sid)
    assert state.version == 1 and state.can_redo
    state = await service.redo(sid)
    assert state.version == 3
    await service.undo(sid)
    await service.apply(sid, ops({"op": "update_task", "id": 3, "duration": 9}), source="user")
    state = await service.get_state(sid)
    assert state.version == 2 and not state.can_redo
    await service.undo(sid)
    with pytest.raises(NothingToUndo):
        await service.undo(sid)


async def test_invalid_batch_leaves_state(service):
    _, sid = await service.create_session()
    with pytest.raises(OperationError):
        await service.apply(sid, ops({"op": "delete_task", "id": 999}), source="user")
    assert (await service.get_state(sid)).version == 1


async def test_confirmation_required_for_mass_delete(service):
    _, sid = await service.create_session()
    batch = ops(*({"op": "delete_task", "id": i} for i in range(1, 8)))
    with pytest.raises(ConfirmationRequired):
        await service.apply(sid, batch, source="agent")
    out = await service.apply(sid, batch, source="agent", confirmed=True)
    assert len(out.state.plan.tasks) == 18


async def test_user_edit_rejected_while_agent_busy_and_mcp_waits(service):
    _, sid = await service.create_session()
    async with service.locks.agent_turn(sid):
        with pytest.raises(AgentBusy):
            await service.apply(sid, ops({"op": "update_task", "id": 1, "duration": 2}), source="user")
        with pytest.raises(AgentBusy):
            async with service.locks.agent_turn(sid):
                pass

    async def short_turn():
        async with service.locks.agent_turn(sid):
            await asyncio.sleep(0.2)

    task = asyncio.create_task(short_turn())
    await asyncio.sleep(0.05)
    out = await service.apply(sid, ops({"op": "update_task", "id": 1, "duration": 2}), source="mcp")
    await task
    assert out.state.version == 2


async def test_replace_and_reset_are_undoable_and_add_chat_note(service):
    _, sid = await service.create_session()
    plan = build_demo_plan(TODAY)
    plan.tasks = plan.tasks[:3]
    plan.dependencies = [d for d in plan.dependencies if d.predecessor_id <= 3 and d.successor_id <= 3]
    state = await service.replace(sid, plan, source="import", summary="Импорт",
                                  chat_note="Загружен план «x.xlsx», задач: 3")
    assert len(state.plan.tasks) == 3
    async with service.sessionmaker() as db:
        msgs = await repo.recent_chat_messages(db, sid, 5)
    assert msgs[-1].role == "system" and "x.xlsx" in msgs[-1].content
    assert len((await service.reset(sid)).plan.tasks) == 25
    assert len((await service.undo(sid)).plan.tasks) == 3


async def test_versions_are_pruned(sessionmaker):
    service = PlanService(sessionmaker, EventBus(), SessionLocks(), max_versions=3, today=lambda: TODAY)
    _, sid = await service.create_session()
    for d in range(2, 7):
        await service.apply(sid, ops({"op": "update_task", "id": 1, "duration": d}), source="user")
    assert (await service.get_state(sid)).version == 6
    await service.undo(sid)
    await service.undo(sid)
    with pytest.raises(NothingToUndo):
        await service.undo(sid)
```

- [ ] **Step 3: Run both → FAIL.**

- [ ] **Step 4: Implement errors / sessions / events / locks**

```python
# services/errors.py
from app.domain.errors import DomainError


class AgentBusy(DomainError):
    code = "agent_busy"

    def __init__(self) -> None:
        super().__init__("Агент сейчас редактирует план, подождите")


class RateLimited(DomainError):
    code = "rate_limited"


class NoSession(DomainError):
    code = "no_session"

    def __init__(self) -> None:
        super().__init__("Сессия не найдена")


class NothingToUndo(DomainError):
    code = "nothing_to_undo"

    def __init__(self) -> None:
        super().__init__("Нечего отменять")


class NothingToRedo(DomainError):
    code = "nothing_to_redo"

    def __init__(self) -> None:
        super().__init__("Нечего повторять")


class NotFound(DomainError):
    code = "not_found"


class BadOrigin(DomainError):
    code = "bad_origin"


class FileTooLarge(DomainError):
    code = "file_too_large"
```
```python
# services/sessions.py
import hashlib
import secrets


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()
```
```python
# services/events.py
import asyncio
import uuid
from collections import defaultdict
from typing import Any


class EventBus:
    """In-process per-session pub/sub. Valid for a single uvicorn worker only (see README)."""

    def __init__(self) -> None:
        self._subs: dict[uuid.UUID, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)

    def subscribe(self, session_id: uuid.UUID) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._subs[session_id].add(queue)
        return queue

    def unsubscribe(self, session_id: uuid.UUID, queue: asyncio.Queue[dict[str, Any]]) -> None:
        subs = self._subs.get(session_id)
        if subs is None:
            return
        subs.discard(queue)
        if not subs:
            del self._subs[session_id]

    def publish(self, session_id: uuid.UUID, event: dict[str, Any]) -> None:
        for queue in list(self._subs.get(session_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow client; it refetches the plan on reconnect
```
```python
# services/locks.py
import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.services.errors import AgentBusy


class SessionLocks:
    def __init__(self) -> None:
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}
        self._idle: dict[uuid.UUID, asyncio.Event] = {}

    def lock(self, session_id: uuid.UUID) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    def _event(self, session_id: uuid.UUID) -> asyncio.Event:
        if session_id not in self._idle:
            ev = asyncio.Event()
            ev.set()
            self._idle[session_id] = ev
        return self._idle[session_id]

    def is_busy(self, session_id: uuid.UUID) -> bool:
        return not self._event(session_id).is_set()

    @asynccontextmanager
    async def agent_turn(self, session_id: uuid.UUID) -> AsyncIterator[None]:
        ev = self._event(session_id)
        if not ev.is_set():
            raise AgentBusy()
        ev.clear()
        try:
            yield
        finally:
            ev.set()

    async def wait_not_busy(self, session_id: uuid.UUID, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._event(session_id).wait(), timeout)
            return True
        except TimeoutError:
            return False
```

- [ ] **Step 5: Implement `services/plan_service.py`**

```python
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import repo
from app.db.repo import VersionMeta
from app.domain.diff import Change, diff_plans, summarize_changes
from app.domain.errors import ConfirmationRequired, DomainError
from app.domain.models import Plan
from app.domain.operations import Operation, apply_operations, requires_confirmation
from app.domain.scheduler import ScheduledPlan, schedule
from app.domain.seed import build_demo_plan
from app.services.errors import AgentBusy, NoSession, NothingToRedo, NothingToUndo
from app.services.events import EventBus
from app.services.locks import SessionLocks
from app.services.sessions import hash_token, new_token

Source = Literal["seed", "import", "user", "agent", "mcp", "reset"]
MCP_WAIT_SECONDS = 10.0


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
        today: Callable[[], date] = date.today,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.bus = bus
        self.locks = locks
        self._max_versions = max_versions
        self._today = today

    async def create_session(self) -> tuple[str, uuid.UUID]:
        token = new_token()
        plan = build_demo_plan(self._today())
        async with self.sessionmaker() as db, db.begin():
            row = await repo.create_session(db, hash_token(token))
            await repo.add_version(db, session_id=row.id, version_no=1, snapshot=plan.model_dump(mode="json"),
                                   source="seed", turn_id=None, summary="Демо-план", diff=[])
            row.current_version = 1
        return token, row.id

    async def resolve_session(self, token: str) -> uuid.UUID | None:
        async with self.sessionmaker() as db, db.begin():
            row = await repo.get_session_by_token_hash(db, hash_token(token))
            if row is None:
                return None
            await repo.touch_session(db, row.id)
            return row.id

    async def delete_session(self, session_id: uuid.UUID) -> None:
        async with self.sessionmaker() as db, db.begin():
            await repo.delete_session(db, session_id)

    async def get_state(self, session_id: uuid.UUID) -> PlanState:
        async with self.sessionmaker() as db:
            return await self._state(db, session_id)

    async def _state(self, db: AsyncSession, session_id: uuid.UUID) -> PlanState:
        session = await repo.get_session(db, session_id)
        if session is None:
            raise NoSession()
        version = await repo.get_version(db, session_id, session.current_version)
        if version is None:
            raise DomainError("Текущая версия плана не найдена")
        meta = await repo.list_version_meta(db, session_id)
        plan = Plan.model_validate(version.snapshot)
        return PlanState(
            version=session.current_version,
            plan=plan,
            scheduled=schedule(plan),
            can_undo=undo_target(meta, session.current_version) is not None,
            can_redo=redo_target(meta, session.current_version) is not None,
        )

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
    ) -> ApplyOutcome:
        await self._guard_busy(session_id, source)
        async with self.locks.lock(session_id):
            async with self.sessionmaker() as db, db.begin():
                current = await self._state(db, session_id)
                if not confirmed and requires_confirmation(current.plan, ops):
                    raise ConfirmationRequired(
                        "Пакет удаляет много задач. Спросите пользователя и повторите с confirmed=true"
                    )
                result = apply_operations(current.plan, ops)
                summary = summarize_changes(result.changes)
                if result.changes:
                    await self._commit(db, session_id, current.version, result.plan, source, turn_id,
                                       summary, [c.model_dump() for c in result.changes])
                state = await self._state(db, session_id)
        if result.changes:
            self._publish(session_id, state.version, source, turn_id,
                          sorted({c.task_id for c in result.changes}))
        return ApplyOutcome(state, result.changes, result.warnings, result.created_task_ids, summary)

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
        async with self.locks.lock(session_id):
            async with self.sessionmaker() as db, db.begin():
                current = await self._state(db, session_id)
                changes = diff_plans(current.scheduled, schedule(plan))
                await self._commit(db, session_id, current.version, plan, source, None, summary,
                                   [c.model_dump() for c in changes])
                if chat_note:
                    await repo.add_chat_message(db, session_id=session_id, role="system", content=chat_note)
                state = await self._state(db, session_id)
        self._publish(session_id, state.version, source, None, sorted({c.task_id for c in changes}))
        return state

    async def reset(self, session_id: uuid.UUID) -> PlanState:
        return await self.replace(session_id, build_demo_plan(self._today()), source="reset",
                                  summary="Сброс к демо-плану")

    async def undo(self, session_id: uuid.UUID, *, source: Source = "user") -> PlanState:
        return await self._move_pointer(session_id, undo_target, NothingToUndo(), source)

    async def redo(self, session_id: uuid.UUID) -> PlanState:
        return await self._move_pointer(session_id, redo_target, NothingToRedo(), "user")

    async def _move_pointer(
        self,
        session_id: uuid.UUID,
        target_fn: Callable[[list[VersionMeta], int], int | None],
        empty_error: DomainError,
        source: Source,
    ) -> PlanState:
        await self._guard_busy(session_id, source)
        async with self.locks.lock(session_id):
            async with self.sessionmaker() as db, db.begin():
                before = await self._state(db, session_id)
                target = target_fn(await repo.list_version_meta(db, session_id), before.version)
                if target is None:
                    raise empty_error
                session = await repo.get_session(db, session_id)
                assert session is not None
                session.current_version = target
                await db.flush()
                state = await self._state(db, session_id)
        changed = sorted({c.task_id for c in diff_plans(before.scheduled, state.scheduled)})
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
        await repo.delete_versions_after(db, session_id, current_version)
        new_version = current_version + 1
        await repo.add_version(db, session_id=session_id, version_no=new_version,
                               snapshot=plan.model_dump(mode="json"), source=source,
                               turn_id=turn_id, summary=summary, diff=diff)
        session = await repo.get_session(db, session_id)
        assert session is not None
        session.current_version = new_version
        await db.flush()
        await repo.prune_versions(db, session_id, keep=self._max_versions)

    def _publish(self, session_id: uuid.UUID, version: int, source: Source,
                 turn_id: uuid.UUID | None, changed: list[int]) -> None:
        self.bus.publish(session_id, {
            "type": "plan_changed", "version": version, "source": source,
            "turn_id": str(turn_id) if turn_id else None, "changed_task_ids": changed,
        })
```

- [ ] **Step 6: Verify + commit**

Run: `cd backend && uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy app`
```bash
git add backend
git commit -m "feat(services): add plan service with versions, undo groups, locks and events"
```

---

### Task 9: HTTP API (session cookie, plan, import/export, errors, SPA)

**Files:**
- Create: `backend/app/main.py`, `backend/app/api/__init__.py`, `backend/app/api/deps.py`, `backend/app/api/errors.py`, `backend/app/api/schemas.py`, `backend/app/api/routes_session.py`, `backend/app/api/routes_plan.py`
- Modify: `backend/tests/integration/conftest.py` (app/client fixtures)
- Test: `backend/tests/integration/test_api_plan.py`, `backend/tests/integration/test_api_import_export.py`

**Interfaces:**
- Consumes: Tasks 5–8.
- Produces:
  - `main.create_app(settings=None, *, sessionmaker=None, today=None) -> FastAPI` — lifespan builds engine (unless a sessionmaker is injected), `EventBus`, `SessionLocks`, `PlanService`; sets `app.state.settings`, `app.state.sessionmaker`, `app.state.service`. Run with `uvicorn app.main:create_app --factory`.
  - `api/deps.py`: `cookie_name(settings)` (`"__Host-sid"` if `cookie_secure` else `"sid"`), `get_service(request)`, `require_session(request) -> UUID` (raises `NoSession`), `check_origin(request)` (unsafe methods with an `Origin` header ≠ `settings.public_origin` → `BadOrigin`; missing Origin allowed).
  - `api/schemas.py`: `PlanResponse(version, can_undo, can_redo, agent_busy, plan: ScheduledPlan)`, `ApplyRequest(ops: list[Operation])`, `ApplyResponse(PlanResponse + changes: list[Change], warnings: list[str], summary: str, created_task_ids: list[int])`, `ImportSuccess(ok: Literal[True], plan: PlanResponse, warnings: list[ImportIssue])`, `ImportFailure(ok: Literal[False], errors: list[ImportIssue], warnings: list[ImportIssue])`; helper `to_plan_response(state, busy) -> PlanResponse`.
  - Error envelope `{"error": {"code","message","details"}}`; status map: `invalid_plan|invalid_operation|cycle → 422`, `confirmation_required|agent_busy|nothing_to_undo|nothing_to_redo → 409`, `rate_limited → 429`, `no_session → 401`, `not_found → 404`, `bad_origin → 403`, `file_too_large → 413`, other DomainError → 400; `RequestValidationError → 422 validation_error`.
  - Endpoints: `POST /api/session` (idempotent; sets cookie `HttpOnly; SameSite=Lax; Path=/; Max-Age=ttl`, `Secure` when `cookie_secure`), `DELETE /api/session` (204, clears cookie), `GET /api/plan`, `POST /api/plan/operations`, `POST /api/plan/undo`, `POST /api/plan/redo`, `POST /api/plan/reset`, `POST /api/plan/import` (multipart `file` + form `project_start`), `GET /api/plan/export`, `GET /healthz` (`{"status":"ok"}` after `SELECT 1`). SPA catch-all `GET /{path:path}` registered LAST when `static_dir` exists.

- [ ] **Step 1: Deps** — `cd backend && uv add fastapi "uvicorn[standard]" python-multipart && uv add --dev httpx asgi-lifespan`

- [ ] **Step 2: Fixtures** — append to `tests/integration/conftest.py`:
```python
from datetime import date

import httpx
from asgi_lifespan import LifespanManager

from app.config import Settings
from app.main import create_app

TODAY = date(2026, 9, 25)


@pytest.fixture
def settings() -> Settings:
    return Settings(database_url=TEST_DB_URL, public_origin="http://testserver", llm_provider="fake")


@pytest.fixture
async def app(settings, sessionmaker):
    application = create_app(settings, sessionmaker=sessionmaker, today=lambda: TODAY)
    async with LifespanManager(application) as manager:
        yield manager.app


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


@pytest.fixture
async def session_client(client):
    r = await client.post("/api/session")
    assert r.status_code == 200
    return client
```

- [ ] **Step 3: Failing tests** `tests/integration/test_api_plan.py`

```python
async def test_session_cookie_and_plan(client):
    r = await client.get("/api/plan")
    assert r.status_code == 401 and r.json()["error"]["code"] == "no_session"
    r = await client.post("/api/session")
    assert r.status_code == 200 and "sid" in r.cookies
    body = (await client.get("/api/plan")).json()
    assert body["version"] == 1 and len(body["plan"]["tasks"]) == 25
    assert body["plan"]["tasks"][0]["start"] and body["can_undo"] is False and body["agent_busy"] is False


async def test_stale_cookie_gets_401_then_new_session(client):
    client.cookies.set("sid", "stale-token")
    assert (await client.get("/api/plan")).status_code == 401
    assert (await client.post("/api/session")).status_code == 200
    assert (await client.get("/api/plan")).status_code == 200


async def test_session_post_is_idempotent(session_client):
    first = session_client.cookies["sid"]
    await session_client.post("/api/session")
    assert session_client.cookies["sid"] == first


async def test_operations_undo_redo(session_client):
    r = await session_client.post("/api/plan/operations",
                                  json={"ops": [{"op": "update_task", "id": 1, "duration": 6}]})
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 2 and body["summary"].startswith("Изменено задач")
    assert any(c["field"] == "duration" for c in body["changes"])
    assert (await session_client.post("/api/plan/undo")).json()["version"] == 1
    assert (await session_client.post("/api/plan/redo")).json()["version"] == 2


async def test_errors_envelope(session_client):
    r = await session_client.post("/api/plan/operations", json={"ops": [{"op": "delete_task", "id": 999}]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_operation"
    r = await session_client.post("/api/plan/operations", json={"ops": [{"op": "nope"}]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
    r = await session_client.post("/api/plan/undo")
    assert r.status_code == 409 and r.json()["error"]["message"] == "Нечего отменять"


async def test_bad_origin_rejected(session_client):
    r = await session_client.post("/api/plan/reset", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "bad_origin"


async def test_reset_and_delete_session(session_client):
    await session_client.post("/api/plan/operations", json={"ops": [{"op": "delete_task", "id": 1}]})
    assert len((await session_client.post("/api/plan/reset")).json()["plan"]["tasks"]) == 25
    assert (await session_client.delete("/api/session")).status_code == 204
    assert (await session_client.get("/api/plan")).status_code == 401


async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
```
`tests/integration/test_api_import_export.py`:
```python
from io import BytesIO

from openpyxl import Workbook, load_workbook

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def xlsx(rows):
    wb = Workbook()
    for r in rows:
        wb.active.append(r)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


GOOD = xlsx([["Задача", "Описание", "Исполнитель", "Длительность", "Предшественники"],
             ["A", "", "Олег", 2, None], ["B", "", "Олег", 3, "1"]])


async def test_import_replaces_plan_and_is_undoable(session_client):
    r = await session_client.post("/api/plan/import", files={"file": ("office.xlsx", GOOD, XLSX_MIME)},
                                  data={"project_start": "2026-10-04"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and len(body["plan"]["plan"]["tasks"]) == 2
    assert body["plan"]["plan"]["project_start"] == "2026-10-05"
    history = (await session_client.post("/api/plan/undo")).json()
    assert len(history["plan"]["tasks"]) == 25


async def test_import_errors_and_size_limit(session_client):
    bad = xlsx([["Задача", "Длительность", "Предшественники"], ["A", 1, "7"]])
    r = await session_client.post("/api/plan/import", files={"file": ("b.xlsx", bad, XLSX_MIME)},
                                  data={"project_start": "2026-10-05"})
    assert r.status_code == 422 and r.json()["ok"] is False and r.json()["errors"][0]["row"] == 2
    huge = b"0" * (2 * 1024 * 1024 + 1)
    r = await session_client.post("/api/plan/import", files={"file": ("h.xlsx", huge, XLSX_MIME)},
                                  data={"project_start": "2026-10-05"})
    assert r.status_code == 413 and r.json()["error"]["code"] == "file_too_large"


async def test_export_downloads_xlsx(session_client):
    r = await session_client.get("/api/plan/export")
    assert r.status_code == 200 and r.headers["content-type"].startswith(XLSX_MIME)
    assert 'filename="plan-' in r.headers["content-disposition"]
    assert load_workbook(BytesIO(r.content))["План"].max_row == 26
```

- [ ] **Step 4: Run → FAIL.**

- [ ] **Step 5: Implement**

`api/errors.py`:
```python
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.domain.errors import DomainError

STATUS = {
    "invalid_plan": 422, "invalid_operation": 422, "cycle": 422,
    "confirmation_required": 409, "agent_busy": 409, "nothing_to_undo": 409, "nothing_to_redo": 409,
    "rate_limited": 429, "no_session": 401, "not_found": 404, "bad_origin": 403, "file_too_large": 413,
}


def error_response(code: str, message: str, details: object = None, status: int | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, "details": details}},
                        status_code=status or STATUS.get(code, 400))


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(_: Request, exc: DomainError) -> JSONResponse:
        return error_response(exc.code, exc.message, exc.details or None)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response("validation_error", "Некорректный запрос", jsonable_encoder(exc.errors()), 422)
```
`api/deps.py`:
```python
import uuid

from fastapi import Request

from app.config import Settings
from app.services.errors import BadOrigin, NoSession
from app.services.plan_service import PlanService

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def cookie_name(settings: Settings) -> str:
    return "__Host-sid" if settings.cookie_secure else "sid"


def get_service(request: Request) -> PlanService:
    service: PlanService = request.app.state.service
    return service


async def require_session(request: Request) -> uuid.UUID:
    token = request.cookies.get(cookie_name(request.app.state.settings))
    if not token:
        raise NoSession()
    session_id = await get_service(request).resolve_session(token)
    if session_id is None:
        raise NoSession()
    return session_id


def check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if request.method in UNSAFE and origin and origin != request.app.state.settings.public_origin:
        raise BadOrigin("Запрос с чужого источника отклонён")
```
`routes_session.py`: `POST /api/session` (dependency `check_origin`): if the cookie resolves → `{"ok": true}`; else `token, _ = await service.create_session()`, `response.set_cookie(cookie_name(s), token, max_age=s.session_ttl_days * 86400, httponly=True, secure=s.cookie_secure, samesite="lax", path="/")`, return `{"ok": true}`. `DELETE /api/session` (`require_session`, `check_origin`) → `service.delete_session`, `response.delete_cookie(cookie_name(s), path="/")`, status 204.

`routes_plan.py`: every route `Depends(require_session)`; unsafe ones also `dependencies=[Depends(check_origin)]`. Build `PlanResponse` via `to_plan_response(state, service.locks.is_busy(sid))`. Import:
```python
@router.post("/import", dependencies=[Depends(check_origin)])
async def import_plan(request: Request, file: UploadFile, project_start: date = Form(...),
                      session_id: uuid.UUID = Depends(require_session)) -> Response:
    settings = request.app.state.settings
    limit = settings.max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise FileTooLarge(f"Файл больше {settings.max_upload_mb} МБ")
    result = parse_plan_xlsx(data, project_start)
    if not result.ok or result.plan is None:
        body = ImportFailure(ok=False, errors=result.errors, warnings=result.warnings)
        return JSONResponse(body.model_dump(mode="json"), status_code=422)
    name = (file.filename or "plan.xlsx")[:100]
    service = get_service(request)
    state = await service.replace(session_id, result.plan, source="import", summary=f"Импорт {name}",
                                  chat_note=f"Загружен план «{name}», задач: {len(result.plan.tasks)}")
    ok = ImportSuccess(ok=True, plan=to_plan_response(state, service.locks.is_busy(session_id)),
                       warnings=result.warnings)
    return JSONResponse(ok.model_dump(mode="json"))
```
Export: `Response(export_plan_xlsx(state.scheduled), media_type=XLSX_MIME, headers={"Content-Disposition": f'attachment; filename="plan-{date.today():%Y-%m-%d}.xlsx"'})`.

`main.py`:
```python
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api import routes_plan, routes_session
from app.api.errors import install_error_handlers
from app.config import Settings, get_settings
from app.db.engine import make_engine, make_sessionmaker
from app.services.events import EventBus
from app.services.locks import SessionLocks
from app.services.plan_service import PlanService


def create_app(
    settings: Settings | None = None,
    *,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    today: Callable[[], date] | None = None,
) -> FastAPI:
    cfg = settings or get_settings()
    today_fn = today or date.today

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = None
        sm = sessionmaker
        if sm is None:
            engine = make_engine(cfg)
            sm = make_sessionmaker(engine)
        app.state.settings = cfg
        app.state.sessionmaker = sm
        app.state.service = PlanService(sm, EventBus(), SessionLocks(), max_versions=cfg.max_versions,
                                        today=today_fn)
        try:
            yield
        finally:
            if engine is not None:
                await engine.dispose()

    app = FastAPI(title="Gantt AI Planner", lifespan=lifespan, docs_url="/api/docs",
                  openapi_url="/api/openapi.json", redoc_url=None)
    install_error_handlers(app)
    app.include_router(routes_session.router)
    app.include_router(routes_plan.router)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        async with app.state.sessionmaker() as db:
            await db.execute(text("SELECT 1"))
        return JSONResponse({"status": "ok"})

    _mount_spa(app, cfg)  # must stay the LAST registration: catch-all route
    return app


def _mount_spa(app: FastAPI, settings: Settings) -> None:
    if not settings.static_dir or not Path(settings.static_dir).is_dir():
        return
    root = Path(settings.static_dir).resolve()

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(root / "index.html")
```

- [ ] **Step 6: Verify + commit**

Run: `cd backend && uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy app`
Smoke: `cd backend && DB_USER=planner uv run uvicorn app.main:create_app --factory --port 8000` (after `DB_USER=planner DB_PASSWORD=planner uv run alembic upgrade head`), then `curl -i -X POST localhost:8000/api/session` → 200 + `set-cookie: sid=`. Stop the server.
```bash
git add backend
git commit -m "feat(api): add session, plan, import/export endpoints and error envelope"
```

---

### Task 10: MCP server + in-process tool client

**Files:**
- Create: `backend/app/mcp_server/__init__.py`, `backend/app/mcp_server/context.py`, `backend/app/mcp_server/server.py`, `backend/app/mcp_server/client.py`
- Modify: `backend/app/main.py` (lifespan: `app.state.mcp = build_mcp(...)`; open long-lived `PlanToolClient` → `app.state.tool_client`)
- Test: `backend/tests/integration/test_mcp_tools.py`

**Interfaces:**
- Consumes: Tasks 4, 8, 9.
- Produces:
  - `context.py`: `current_session: ContextVar[uuid.UUID | None]`, `current_turn: ContextVar[uuid.UUID | None]` (defaults None).
  - `server.py`: `build_mcp(service, *, today: Callable[[], date], auth: TokenVerifier | None = None) -> FastMCP` with tools `get_plan() -> str`, `find_tasks(query=None, assignee=None, critical_only=False) -> list[dict]`, `get_task(id) -> dict`, `get_resource_load(assignee=None) -> list[dict]`, `apply_operations(operations: list[Operation], confirmed=False) -> dict`, `undo() -> dict`; `resolve_session_id() -> uuid.UUID` (access-token claim `session_id` first, then `current_session`, else `ToolError("Нет активной сессии")`).
  - `apply_operations` result: `{"summary","version","changes"(≤100 Change dicts),"warnings","created_task_ids","project_end"}`; source is `"agent"` when `current_turn` is set else `"mcp"`; every `DomainError` → `ToolError(f"{code}: {message}")`.
  - `client.py`: `ToolCallResult(text: str, is_error: bool, data: dict[str, Any] | None)`; `PlanToolClient(mcp)` — async context manager; `tool_definitions() -> list[dict]` (`{"name","description","input_schema"}`, cached); `call(name, args, *, session_id, turn_id) -> ToolCallResult` (sets/resets both context vars around the call).

- [ ] **Step 1: Dep + import check** — `cd backend && uv add "fastmcp==4.0.9"`; run `uv run python -c "from fastmcp import FastMCP, Client; from fastmcp.exceptions import ToolError; from fastmcp.server.auth import TokenVerifier, AccessToken; from fastmcp.server.dependencies import get_access_token; print('ok')"`. If a path differs, locate it in the installed package and note it in the report.

- [ ] **Step 2: Failing tests** `tests/integration/test_mcp_tools.py`

```python
import asyncio
import json
import uuid
from datetime import date

from app.db import repo
from app.mcp_server.client import PlanToolClient
from app.mcp_server.server import build_mcp


async def new_sid(app):
    _, sid = await app.state.service.create_session()
    return sid


async def test_tool_definitions_are_anthropic_shaped(app):
    defs = await app.state.tool_client.tool_definitions()
    assert {d["name"] for d in defs} == {"get_plan", "find_tasks", "get_task", "get_resource_load",
                                         "apply_operations", "undo"}
    apply = next(d for d in defs if d["name"] == "apply_operations")
    assert apply["input_schema"]["type"] == "object" and "operations" in apply["input_schema"]["properties"]
    assert "session_id" not in json.dumps(defs)


async def test_get_plan_and_find_tasks(app):
    sid = await new_sid(app)
    tools = app.state.tool_client
    r = await tools.call("get_plan", {}, session_id=sid, turn_id=None)
    assert not r.is_error and "Следующий свободный id: 26" in r.text
    r = await tools.call("find_tasks", {"assignee": "дмитрий"}, session_id=sid, turn_id=None)
    found = r.data["result"]
    assert not r.is_error and 11 in [t["id"] for t in found]


async def test_apply_operations_tags_agent_turn_and_undo(app):
    sid = await new_sid(app)
    tools = app.state.tool_client
    turn = uuid.uuid4()
    r = await tools.call("apply_operations", {"operations": [{"op": "move_task", "id": 1, "shift_days": 2}]},
                         session_id=sid, turn_id=turn)
    assert not r.is_error, r.text
    assert r.data["version"] == 2 and r.data["summary"].startswith("Изменено задач")
    async with app.state.service.sessionmaker() as db:
        meta = await repo.list_version_meta(db, sid)
    assert meta[-1].turn_id == turn
    r = await tools.call("undo", {}, session_id=sid, turn_id=turn)
    assert not r.is_error and r.data["version"] == 1


async def test_domain_errors_become_tool_errors(app):
    sid = await new_sid(app)
    tools = app.state.tool_client
    r = await tools.call("apply_operations", {"operations": [{"op": "delete_task", "id": 999}]},
                         session_id=sid, turn_id=None)
    assert r.is_error and "invalid_operation" in r.text
    batch = [{"op": "delete_task", "id": i} for i in range(1, 8)]
    r = await tools.call("apply_operations", {"operations": batch}, session_id=sid, turn_id=None)
    assert r.is_error and "confirmation_required" in r.text


async def test_sessions_are_isolated_under_concurrency(app):
    a, b = await new_sid(app), await new_sid(app)
    tools = app.state.tool_client
    await asyncio.gather(
        tools.call("apply_operations", {"operations": [{"op": "delete_task", "id": 25}]}, session_id=a, turn_id=None),
        tools.call("apply_operations", {"operations": [{"op": "delete_task", "id": 24}]}, session_id=b, turn_id=None),
    )
    ta = {t.id for t in (await app.state.service.get_state(a)).plan.tasks}
    tb = {t.id for t in (await app.state.service.get_state(b)).plan.tasks}
    assert 25 not in ta and 24 in ta and 24 not in tb and 25 in tb


async def test_missing_session_is_tool_error(app):
    mcp = build_mcp(app.state.service, today=lambda: date(2026, 9, 25))
    async with PlanToolClient(mcp) as client:
        r = await client.call("get_plan", {}, session_id=None, turn_id=None)
    assert r.is_error and "сесси" in r.text
```
(If fastmcp exposes a list return under a different structured key than `"result"`, adjust `found = ...` in the test once and note it.)

- [ ] **Step 3: Run → FAIL.**

- [ ] **Step 4: Implement**

`context.py`:
```python
import uuid
from contextvars import ContextVar

current_session: ContextVar[uuid.UUID | None] = ContextVar("current_session", default=None)
current_turn: ContextVar[uuid.UUID | None] = ContextVar("current_turn", default=None)
```
`server.py`:
```python
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
from app.domain.operations import Operation
from app.domain.render import render_plan_table
from app.domain.scheduler import ScheduledPlan, ScheduledTask
from app.mcp_server.context import current_session, current_turn
from app.services.plan_service import PlanService, PlanState

INSTRUCTIONS = (
    "Инструменты редактирования плана-графика (диаграмма Ганта). Даты считаются автоматически по "
    "зависимостям «окончание→начало», рабочие дни пн–пт. Все правки одного запроса отправляйте одним "
    "вызовом apply_operations: пакет атомарный."
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
        "id": t.id, "name": t.name, "assignee": t.assignee, "duration": t.duration,
        "start": t.start.isoformat(), "end": t.end.isoformat(), "slack": t.slack,
        "is_critical": t.is_critical, "predecessors": format_predecessors(sp.dependencies, t.id),
        "constraint_start": t.constraint_start.isoformat() if t.constraint_start else None,
        "overallocated_with": t.overallocated_with,
    }


def build_mcp(service: PlanService, *, today: Callable[[], date], auth: TokenVerifier | None = None) -> FastMCP:
    mcp = FastMCP("planner", instructions=INSTRUCTIONS, auth=auth)

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
    async def find_tasks(query: str | None = None, assignee: str | None = None,
                         critical_only: bool = False) -> list[dict[str, Any]]:
        """Поиск задач: подстрока в названии/описании (query), подстрока имени исполнителя, только критические."""
        sp = (await state()).scheduled
        q, a = (query or "").casefold(), (assignee or "").casefold()
        return [
            _task_dict(sp, t) for t in sp.tasks
            if (not q or q in t.name.casefold() or q in t.description.casefold())
            and (not a or a in (t.assignee or "").casefold())
            and (not critical_only or t.is_critical)
        ]

    @mcp.tool
    async def get_task(id: int) -> dict[str, Any]:
        """Одна задача: поля, вычисленные даты, предшественники, последователи, чем ограничено начало."""
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
            p = people.setdefault(t.assignee, {"assignee": t.assignee, "tasks": [], "conflicts": []})
            p["tasks"].append({"id": t.id, "start": t.start.isoformat(), "end": t.end.isoformat()})
            for other in t.overallocated_with:
                pair = sorted((t.id, other))
                if pair not in p["conflicts"]:
                    p["conflicts"].append(pair)
        return list(people.values())

    @mcp.tool
    async def apply_operations(operations: list[Operation], confirmed: bool = False) -> dict[str, Any]:
        """Атомарно применить пакет операций. Новые задачи получают id по порядку add_task, начиная со
        «следующего свободного id» из get_plan — на них можно ссылаться в этом же пакете. move_task задаёт
        ограничение «не раньше даты», последователи сдвигаются автоматически. При ошибке
        confirmation_required спросите пользователя и повторите с confirmed=true."""
        sid = resolve_session_id()
        turn = current_turn.get()
        try:
            out = await service.apply(sid, operations, source="agent" if turn else "mcp",
                                      turn_id=turn, confirmed=confirmed)
        except DomainError as exc:
            raise ToolError(f"{exc.code}: {exc.message}") from exc
        return {
            "summary": out.summary, "version": out.state.version,
            "changes": [c.model_dump() for c in out.changes[:100]], "warnings": out.warnings,
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
```
`client.py`:
```python
import json
import uuid
from types import TracebackType
from typing import Any

from fastmcp import Client, FastMCP

from app.mcp_server.context import current_session, current_turn


class ToolCallResult:
    def __init__(self, text: str, is_error: bool, data: dict[str, Any] | None) -> None:
        self.text = text
        self.is_error = is_error
        self.data = data


class PlanToolClient:
    def __init__(self, mcp: FastMCP) -> None:
        self._client = Client(mcp)
        self._defs: list[dict[str, Any]] | None = None

    async def __aenter__(self) -> "PlanToolClient":
        await self._client.__aenter__()
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                        tb: TracebackType | None) -> None:
        await self._client.__aexit__(exc_type, exc, tb)

    async def tool_definitions(self) -> list[dict[str, Any]]:
        if self._defs is None:
            tools = await self._client.list_tools()
            self._defs = [{"name": t.name, "description": t.description or "",
                           "input_schema": t.input_schema} for t in tools]
        return self._defs

    async def call(self, name: str, args: dict[str, Any], *, session_id: uuid.UUID | None,
                   turn_id: uuid.UUID | None) -> ToolCallResult:
        s_token = current_session.set(session_id)
        t_token = current_turn.set(turn_id)
        try:
            res = await self._client.call_tool(name, args, raise_on_error=False)
        finally:
            current_session.reset(s_token)
            current_turn.reset(t_token)
        text = "\n".join(getattr(c, "text", "") for c in res.content)
        if not text:
            text = json.dumps(res.structured_content or {}, ensure_ascii=False)
        return ToolCallResult(text=text, is_error=bool(res.is_error), data=res.structured_content)
```
`main.py` lifespan: after creating the service —
```python
        app.state.mcp = build_mcp(app.state.service, today=today_fn)
        async with PlanToolClient(app.state.mcp) as tool_client:
            app.state.tool_client = tool_client
            yield
```
(keep engine disposal in the outer `finally`).

- [ ] **Step 5: Verify + commit**

Run: `cd backend && uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy app`
```bash
git add backend
git commit -m "feat(mcp): add planner MCP server and in-process tool client"
```

---

### Task 11: Agent (LLM adapters, fake LLM, loop) + chat & events endpoints

**Files:**
- Create: `backend/app/agent/__init__.py`, `backend/app/agent/llm.py`, `backend/app/agent/fake.py`, `backend/app/agent/prompt.py`, `backend/app/agent/loop.py`, `backend/app/services/ratelimit.py`, `backend/app/api/routes_chat.py`, `backend/app/api/routes_events.py`
- Modify: `backend/app/main.py` (build LLM + `Agent` in lifespan → `app.state.agent`; include both routers before `_mount_spa`)
- Test: `backend/tests/unit/test_fake_llm.py`, `backend/tests/integration/test_agent_turn.py`, `backend/tests/integration/test_api_chat_events.py`

**Interfaces:**
- Consumes: Tasks 7–10.
- Produces:
  - `llm.py`: `LLMToolCall(id, name, input)`, `LLMTurnResult(text, tool_calls, stop_reason, content: list[dict])`, `TextDelta(text)`, `Completed(result)`, `LLMEvent = TextDelta | Completed`, `LLMError(code, message)`, `LLM` Protocol with `stream(*, system, tools, messages) -> AsyncIterator[LLMEvent]`, `AnthropicLLM(api_key, model, max_tokens)`, `make_llm(settings) -> LLM` (fake when provider is `fake` or no key).
  - `fake.py`: `FakeLLM` — deterministic Russian command rules (Step 2).
  - `prompt.py`: `STATIC_SYSTEM_PROMPT`, `build_system(plan_table) -> list[dict]`.
  - `loop.py`: `Agent(llm, tools, service, *, today, history_limit=20, max_iterations=15, turn_timeout=180.0)`; `run_turn(session_id, user_text) -> AsyncIterator[dict]` yielding `text_delta`, `tool_started`, `tool_finished`, `plan_changed`, and exactly one terminal `done {turn_id, summary, changes}` or `error {code, message}`.
  - `ratelimit.check_chat_limits(db, session_id, *, per_hour, per_day)` → raises `RateLimited`.
  - Endpoints: `POST /api/chat` (body `{"message": 1..4000 chars}`) → SSE (`event: <type>`, `data: <json>`), pre-stream JSON errors 401/403/409/429/422; `GET /api/chat/history` → `[{id, role, content, created_at, meta}]` (last 200); `GET /api/events` → SSE: first `agent_status {busy}`, then bus events, ping 20 s. Helper `routes_events.event_stream(bus, session_id, *, busy)` (async generator, unsubscribes on close).

- [ ] **Step 1: Deps** — `cd backend && uv add anthropic sse-starlette`

- [ ] **Step 2: FakeLLM spec + failing unit test**

FakeLLM (stateless, deterministic):
- Last message is `user` with a list of `tool_result` blocks:
  - If the previous assistant content contains a text block starting with `[fake] shift=` and the tool result is a JSON list (from `find_tasks`; accept either a bare list or `{"result": [...]}`) → reply with a `tool_use` `apply_operations` moving every found id by that shift.
  - Else → final text: `"Не получилось: <first error text>"` if any result `is_error`, otherwise `"Готово. " + <"summary" of the first JSON result, or its first 200 chars>`.
- Last message is `user` text. Rules on the lower-cased text (first match wins; regexes with `re.IGNORECASE`):
  1. `(?:перенеси|сдвинь) задачу (\d+) на (-?\d+) (?:раб\w* )?д` → `apply_operations({"operations":[{"op":"move_task","id":N,"shift_days":M}]})`
  2. `(?:сдвинь|перенеси) все задачи (\S+?) на (-?\d+) (?:раб\w* )?д` → text block `"[fake] shift=M"` + `find_tasks({"assignee": <stem>})` where stem = the word with a trailing Russian case ending stripped (`[аяиыуюе]$` removed, min 3 chars) so «Дмитрия» → «дмитри» matches «Дмитрий».
  3. `назначь задачу (\d+) на (.+)` → `apply_operations(... update_task id, assignee=<group 2 from the ORIGINAL text, stripped, trailing punctuation removed>)`
  4. `добавь задачу [«"](.+?)[»"] на (\d+) д\w*(?: после (\d+))?` → `apply_operations(... add_task name, duration, predecessors [{id}] if given)`
  5. `удали задачу (\d+)` → `apply_operations(... delete_task id)`
  6. `^отмени` → `undo({})`
  7. otherwise → text only: `"Я работаю в демо-режиме без LLM. Попробуйте: «Перенеси задачу 3 на 2 дня», «Сдвинь все задачи Дмитрия на 3 дня», «Назначь задачу 5 на Анну Смирнову», «Добавь задачу «Ревью» на 2 дня после 4», «Удали задачу 7», «Отмени»."`
- Text streams as `TextDelta` chunks ≤ 12 chars; tool ids `fake_<n>` (n = number of messages); `content` mirrors Anthropic blocks; `stop_reason` `"tool_use"` or `"end_turn"`.

`tests/unit/test_fake_llm.py`:
```python
import json

from app.agent.fake import FakeLLM
from app.agent.llm import Completed, TextDelta


async def run(messages):
    deltas, result = [], None
    async for ev in FakeLLM().stream(system=[], tools=[], messages=messages):
        if isinstance(ev, TextDelta):
            deltas.append(ev.text)
        elif isinstance(ev, Completed):
            result = ev.result
    return "".join(deltas), result


async def test_move_rule():
    _, r = await run([{"role": "user", "content": "Перенеси задачу 3 на 2 дня"}])
    assert r.stop_reason == "tool_use"
    assert r.tool_calls[0].name == "apply_operations"
    assert r.tool_calls[0].input == {"operations": [{"op": "move_task", "id": 3, "shift_days": 2}]}


async def test_bulk_move_two_steps():
    first = [{"role": "user", "content": "Сдвинь все задачи Дмитрия на 3 дня"}]
    _, r1 = await run(first)
    assert r1.tool_calls[0].name == "find_tasks"
    assert "дмитри" in r1.tool_calls[0].input["assignee"]
    found = json.dumps({"result": [{"id": 11}, {"id": 12}]})
    msgs = [*first, {"role": "assistant", "content": r1.content},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": r1.tool_calls[0].id, "content": found}]}]
    _, r2 = await run(msgs)
    ops = r2.tool_calls[0].input["operations"]
    assert [o["id"] for o in ops] == [11, 12] and all(o["shift_days"] == 3 for o in ops)


async def test_final_text_after_tool_result_and_help():
    msgs = [
        {"role": "user", "content": "Удали задачу 7"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "fake_1", "name": "apply_operations", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "fake_1",
                                      "content": json.dumps({"summary": "Изменено задач: 3, удалено: 1"})}]},
    ]
    text, r = await run(msgs)
    assert r.stop_reason == "end_turn" and "Изменено задач: 3" in text
    text, _ = await run([{"role": "user", "content": "привет"}])
    assert "демо-режиме" in text


async def test_assign_keeps_original_case():
    _, r = await run([{"role": "user", "content": "Назначь задачу 5 на Анну Смирнову."}])
    op = r.tool_calls[0].input["operations"][0]
    assert op == {"op": "update_task", "id": 5, "assignee": "Анну Смирнову"}
```

- [ ] **Step 3: Failing integration tests**

`tests/integration/test_agent_turn.py`:
```python
from datetime import date

from app.agent.llm import Completed, LLMError, LLMToolCall, LLMTurnResult
from app.agent.loop import Agent
from app.db import repo

TODAY = date(2026, 9, 25)


async def collect(agent, sid, text):
    return [ev async for ev in agent.run_turn(sid, text)]


async def new_sid(app):
    _, sid = await app.state.service.create_session()
    return sid


async def test_turn_moves_task_and_reports(app):
    sid = await new_sid(app)
    events = await collect(app.state.agent, sid, "Перенеси задачу 1 на 2 дня")
    types = [e["type"] for e in events]
    assert "tool_started" in types and "plan_changed" in types and types[-1] == "done"
    done = events[-1]
    assert done["summary"].startswith("Изменено задач") and any(c["task_id"] == 1 for c in done["changes"])
    assert (await app.state.service.get_state(sid)).version == 2
    assert (await app.state.service.undo(sid)).version == 1


async def test_bulk_move_is_one_undo_group(app):
    sid = await new_sid(app)
    events = await collect(app.state.agent, sid, "Сдвинь все задачи Дмитрия на 3 дня")
    assert events[-1]["type"] == "done" and events[-1]["changes"]
    assert (await app.state.service.undo(sid)).version == 1


async def test_turn_persists_chat_and_clears_busy(app):
    sid = await new_sid(app)
    await collect(app.state.agent, sid, "привет")
    async with app.state.service.sessionmaker() as db:
        msgs = await repo.recent_chat_messages(db, sid, 10)
    assert [m.role for m in msgs] == ["user", "assistant"] and "демо-режиме" in msgs[1].content
    assert not app.state.service.locks.is_busy(sid)


async def test_iteration_cap(app):
    class Looping:
        async def stream(self, *, system, tools, messages):
            call = LLMToolCall(id=f"x{len(messages)}", name="get_plan", input={})
            yield Completed(LLMTurnResult(text="", tool_calls=[call], stop_reason="tool_use",
                                          content=[{"type": "tool_use", "id": call.id, "name": "get_plan", "input": {}}]))

    sid = await new_sid(app)
    agent = Agent(Looping(), app.state.tool_client, app.state.service, today=lambda: TODAY, max_iterations=3)
    events = await collect(agent, sid, "зациклись")
    assert events[-1]["type"] == "error" and events[-1]["code"] == "too_many_steps"
    assert not app.state.service.locks.is_busy(sid)


async def test_llm_error_is_reported(app):
    class Broken:
        async def stream(self, *, system, tools, messages):
            raise LLMError("llm_unavailable", "LLM временно недоступна, попробуйте позже")
            yield  # pragma: no cover

    sid = await new_sid(app)
    agent = Agent(Broken(), app.state.tool_client, app.state.service, today=lambda: TODAY)
    events = await collect(agent, sid, "что-нибудь")
    assert events[-1] == {"type": "error", "code": "llm_unavailable",
                          "message": "LLM временно недоступна, попробуйте позже"}
    assert not app.state.service.locks.is_busy(sid)
```
`tests/integration/test_api_chat_events.py`:
```python
import asyncio
import json

from app.api.routes_events import event_stream


def parse_sse(body: str) -> list[dict]:
    events, current = [], {}
    for line in body.splitlines():
        if line.startswith("event:"):
            current["event"] = line[6:].strip()
        elif line.startswith("data:"):
            current["data"] = json.loads(line[5:].strip())
        elif not line.strip() and current:
            events.append(current)
            current = {}
    if current:
        events.append(current)
    return events


async def test_chat_streams_events(session_client):
    async with session_client.stream("POST", "/api/chat", json={"message": "Перенеси задачу 1 на 1 день"}) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        body = "".join([chunk async for chunk in r.aiter_text()])
    events = parse_sse(body)
    assert events[-1]["event"] == "done"
    history = (await session_client.get("/api/chat/history")).json()
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[1]["meta"]["summary"].startswith("Изменено задач")


async def test_chat_rate_limit(app, session_client):
    app.state.settings.chat_limit_per_hour = 1
    async with session_client.stream("POST", "/api/chat", json={"message": "привет"}) as r:
        [c async for c in r.aiter_text()]
    r = await session_client.post("/api/chat", json={"message": "ещё"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"


async def test_chat_validation(session_client):
    assert (await session_client.post("/api/chat", json={"message": ""})).status_code == 422


async def test_event_stream_generator(app):
    _, sid = await app.state.service.create_session()
    bus = app.state.service.bus
    gen = event_stream(bus, sid, busy=False)
    first = await asyncio.wait_for(gen.__anext__(), 1)
    assert first["event"] == "agent_status"
    bus.publish(sid, {"type": "plan_changed", "version": 2})
    second = await asyncio.wait_for(gen.__anext__(), 1)
    assert second["event"] == "plan_changed"
    await gen.aclose()
    assert sid not in bus._subs
```

- [ ] **Step 4: Run → FAIL.**

- [ ] **Step 5: `llm.py`**

```python
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic

from app.config import Settings

UNAVAILABLE = "LLM временно недоступна, попробуйте позже"


@dataclass(frozen=True)
class LLMToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class LLMTurnResult:
    text: str
    tool_calls: list[LLMToolCall]
    stop_reason: str
    content: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class Completed:
    result: LLMTurnResult


LLMEvent = TextDelta | Completed


class LLMError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class LLM(Protocol):
    def stream(self, *, system: list[dict[str, Any]], tools: list[dict[str, Any]],
               messages: list[dict[str, Any]]) -> AsyncIterator[LLMEvent]: ...


class AnthropicLLM:
    def __init__(self, api_key: str, model: str, max_tokens: int) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=2,
                                                timeout=anthropic.Timeout(60.0, connect=5.0))
        self._model = model
        self._max_tokens = max_tokens

    async def stream(self, *, system: list[dict[str, Any]], tools: list[dict[str, Any]],
                     messages: list[dict[str, Any]]) -> AsyncIterator[LLMEvent]:
        cached = [*tools[:-1], {**tools[-1], "cache_control": {"type": "ephemeral"}}] if tools else []
        try:
            async with self._client.messages.stream(
                model=self._model, max_tokens=self._max_tokens, system=system,  # type: ignore[arg-type]
                tools=cached, messages=messages,  # type: ignore[arg-type]
            ) as stream:
                async for event in stream:
                    if event.type == "text":
                        yield TextDelta(event.text)
                final = await stream.get_final_message()
        except anthropic.RateLimitError as exc:
            raise LLMError("llm_rate_limited", "Превышен лимит запросов к LLM, попробуйте через минуту") from exc
        except (anthropic.APITimeoutError, anthropic.APIConnectionError, anthropic.APIStatusError) as exc:
            raise LLMError("llm_unavailable", UNAVAILABLE) from exc
        content = [block.model_dump(exclude_none=True) for block in final.content]
        calls = [LLMToolCall(b.id, b.name, dict(b.input)) for b in final.content if b.type == "tool_use"]
        text = "".join(b.text for b in final.content if b.type == "text")
        yield Completed(LLMTurnResult(text=text, tool_calls=calls, stop_reason=final.stop_reason or "",
                                      content=content))


def make_llm(settings: Settings) -> LLM:
    if settings.llm_provider == "anthropic" and settings.anthropic_api_key is not None:
        return AnthropicLLM(settings.anthropic_api_key.get_secret_value(), settings.llm_model,
                            settings.llm_max_tokens)
    from app.agent.fake import FakeLLM

    return FakeLLM()
```

- [ ] **Step 6: `prompt.py`**

```python
from typing import Any

STATIC_SYSTEM_PROMPT = """Ты — ассистент планировщика проекта в веб-приложении с диаграммой Ганта.
Пользователь пишет на естественном языке, ты меняешь план через инструменты.

Правила:
- Все изменения делай через apply_operations. Собирай все правки одного запроса в ОДИН пакет:
  пакет атомарный, частично он не применяется.
- Ссылайся на задачи по id из таблицы плана. Новые задачи в пакете получают id по порядку,
  начиная со «следующего свободного id» — на них можно ссылаться в том же пакете.
- Даты считаются автоматически: задача начинается после окончания предшественников (+лаг),
  рабочие дни пн–пт. «Перенести задачу» = move_task (ограничение «не раньше»); последователи
  сдвинутся сами. Убрать ограничение — clear_constraint.
- Длительность и лаг — целые числа рабочих дней (длительность ≥ 1, лаг ≥ 0).
- Если под описание подходит несколько задач или запрос неоднозначен — задай один уточняющий
  вопрос и ничего не меняй.
- Если apply_operations вернул confirmation_required — спроси, подтверждает ли пользователь
  удаление, и только после явного «да» повтори вызов с confirmed=true.
- Не выдумывай задачи, людей и даты, которых нет в запросе или плане.
- Отвечай кратко (1–3 предложения) на языке пользователя: что сделано и заметные последствия
  (сдвиг окончания проекта, новые перегрузки). Не пересказывай весь план.
- Если инструмент вернул warnings — сообщи о них."""


def build_system(plan_table: str) -> list[dict[str, Any]]:
    return [
        {"type": "text", "text": STATIC_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "Текущий план (на начало хода):\n" + plan_table},
    ]
```

- [ ] **Step 7: `services/ratelimit.py`**

```python
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repo
from app.services.errors import RateLimited


async def check_chat_limits(db: AsyncSession, session_id: uuid.UUID, *, per_hour: int, per_day: int) -> None:
    now = datetime.now(UTC)
    if await repo.count_user_messages_since(db, now - timedelta(hours=1), session_id) >= per_hour:
        raise RateLimited(f"Лимит: {per_hour} сообщений в час. Попробуйте позже.")
    if await repo.count_user_messages_since(db, now - timedelta(days=1)) >= per_day:
        raise RateLimited("Дневной лимит демо исчерпан. Попробуйте завтра.")
```

- [ ] **Step 8: `loop.py`**

```python
import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import date
from typing import Any

from app.agent.llm import LLM, Completed, LLMError, LLMTurnResult, TextDelta
from app.agent.prompt import build_system
from app.db import repo
from app.db.models import ChatMessageRow
from app.domain.diff import diff_plans, summarize_changes
from app.domain.render import render_plan_table
from app.mcp_server.client import PlanToolClient
from app.services.plan_service import PlanService

MUTATING_TOOLS = {"apply_operations", "undo"}


class Agent:
    def __init__(self, llm: LLM, tools: PlanToolClient, service: PlanService, *,
                 today: Callable[[], date], history_limit: int = 20, max_iterations: int = 15,
                 turn_timeout: float = 180.0) -> None:
        self._llm = llm
        self._tools = tools
        self._service = service
        self._today = today
        self._history_limit = history_limit
        self._max_iterations = max_iterations
        self._timeout = turn_timeout

    async def run_turn(self, session_id: uuid.UUID, user_text: str) -> AsyncIterator[dict[str, Any]]:
        service = self._service
        turn_id = uuid.uuid4()
        async with service.locks.agent_turn(session_id):
            service.bus.publish(session_id, {"type": "agent_status", "busy": True})
            try:
                async with service.sessionmaker() as db, db.begin():
                    await repo.add_chat_message(db, session_id=session_id, role="user",
                                                content=user_text, turn_id=turn_id)
                    history = await repo.recent_chat_messages(db, session_id, self._history_limit)
                start = await service.get_state(session_id)
                text_parts: list[str] = []
                failure: dict[str, Any] | None = None
                try:
                    async with asyncio.timeout(self._timeout):
                        async for event in self._loop(session_id, turn_id, start, history, text_parts):
                            yield event
                except LLMError as exc:
                    failure = {"type": "error", "code": exc.code, "message": exc.message}
                except TooManySteps:
                    failure = {"type": "error", "code": "too_many_steps",
                               "message": "Слишком много шагов за один запрос, попробуйте разбить его на части"}
                except TimeoutError:
                    failure = {"type": "error", "code": "timeout",
                               "message": "Агент не уложился по времени, попробуйте ещё раз"}
                end = await service.get_state(session_id)
                changes = diff_plans(start.scheduled, end.scheduled) if end.version != start.version else []
                meta: dict[str, Any] = {"summary": summarize_changes(changes),
                                        "changes": [c.model_dump() for c in changes[:200]]}
                if failure:
                    meta["error"] = failure["code"]
                text = "".join(text_parts).strip() or (failure["message"] if failure else "Готово.")
                async with service.sessionmaker() as db, db.begin():
                    await repo.add_chat_message(db, session_id=session_id, role="assistant", content=text,
                                                turn_id=turn_id, meta=meta)
                yield failure or {"type": "done", "turn_id": str(turn_id), "summary": meta["summary"],
                                  "changes": meta["changes"]}
            finally:
                service.bus.publish(session_id, {"type": "agent_status", "busy": False})

    async def _loop(self, session_id: uuid.UUID, turn_id: uuid.UUID, start: Any,
                    history: list[ChatMessageRow], text_parts: list[str]) -> AsyncIterator[dict[str, Any]]:
        system = build_system(render_plan_table(start.scheduled, self._today()))
        messages = to_llm_messages(history)
        tools = await self._tools.tool_definitions()
        for _ in range(self._max_iterations):
            result: LLMTurnResult | None = None
            async for ev in self._llm.stream(system=system, tools=tools, messages=messages):
                if isinstance(ev, TextDelta):
                    text_parts.append(ev.text)
                    yield {"type": "text_delta", "text": ev.text}
                elif isinstance(ev, Completed):
                    result = ev.result
            if result is None:
                raise LLMError("llm_unavailable", "Пустой ответ LLM")
            messages.append({"role": "assistant", "content": result.content})
            if not result.tool_calls:
                return
            tool_results: list[dict[str, Any]] = []
            for call in result.tool_calls:
                yield {"type": "tool_started", "name": call.name}
                r = await self._tools.call(call.name, call.input, session_id=session_id, turn_id=turn_id)
                summary = r.text[:200] if r.is_error else (r.data or {}).get("summary")
                yield {"type": "tool_finished", "name": call.name, "ok": not r.is_error, "summary": summary}
                if call.name in MUTATING_TOOLS and not r.is_error and r.data:
                    yield {"type": "plan_changed", "version": r.data.get("version")}
                tool_results.append({"type": "tool_result", "tool_use_id": call.id, "content": r.text,
                                     "is_error": r.is_error})
            messages.append({"role": "user", "content": tool_results})
        raise TooManySteps()


class TooManySteps(Exception):
    pass


def to_llm_messages(history: list[ChatMessageRow]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for m in history:
        role = "assistant" if m.role == "assistant" else "user"
        content = f"[Событие приложения] {m.content}" if m.role == "system" else m.content
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"] += "\n\n" + content
        else:
            merged.append({"role": role, "content": content})
    while merged and merged[0]["role"] != "user":
        merged.pop(0)
    return merged
```
Note: a Python async generator cannot `yield` inside `asyncio.timeout` if the consumer stops iterating early — acceptable; the route always drains. If the client disconnects, sse-starlette cancels the generator; the `finally` still clears busy via `agent_turn` and publishes `agent_status false`.

- [ ] **Step 9: `fake.py`** — implement Step 2 rules (< 150 lines).

- [ ] **Step 10: Routes + wiring**

`routes_chat.py`:
```python
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sse_starlette import EventSourceResponse

from app.api.deps import check_origin, get_service, require_session
from app.db import repo
from app.services.errors import AgentBusy
from app.services.ratelimit import check_chat_limits

router = APIRouter(prefix="/api/chat")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


@router.post("", dependencies=[Depends(check_origin)])
async def chat(body: ChatRequest, request: Request,
               session_id: uuid.UUID = Depends(require_session)) -> EventSourceResponse:
    service = get_service(request)
    settings = request.app.state.settings
    if service.locks.is_busy(session_id):
        raise AgentBusy()
    async with service.sessionmaker() as db:
        await check_chat_limits(db, session_id, per_hour=settings.chat_limit_per_hour,
                                per_day=settings.chat_limit_per_day)
    agent = request.app.state.agent

    async def gen() -> AsyncIterator[dict[str, str]]:
        async for event in agent.run_turn(session_id, body.message.strip()):
            yield {"event": event["type"], "data": json.dumps(event, ensure_ascii=False)}

    return EventSourceResponse(gen(), ping=15, sep="\n")


@router.get("/history")
async def history(request: Request, session_id: uuid.UUID = Depends(require_session)) -> list[dict[str, Any]]:
    async with get_service(request).sessionmaker() as db:
        rows = await repo.recent_chat_messages(db, session_id, 200)
    return [{"id": r.id, "role": r.role, "content": r.content, "created_at": r.created_at.isoformat(),
             "meta": r.meta} for r in rows]
```
`routes_events.py`:
```python
import json
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from sse_starlette import EventSourceResponse

from app.api.deps import get_service, require_session
from app.services.events import EventBus

router = APIRouter(prefix="/api/events")


async def event_stream(bus: EventBus, session_id: uuid.UUID, *, busy: bool) -> AsyncIterator[dict[str, str]]:
    queue = bus.subscribe(session_id)
    try:
        yield {"event": "agent_status", "data": json.dumps({"type": "agent_status", "busy": busy})}
        while True:
            event = await queue.get()
            yield {"event": event["type"], "data": json.dumps(event, ensure_ascii=False)}
    finally:
        bus.unsubscribe(session_id, queue)


@router.get("")
async def events(request: Request, session_id: uuid.UUID = Depends(require_session)) -> EventSourceResponse:
    service = get_service(request)
    stream = event_stream(service.bus, session_id, busy=service.locks.is_busy(session_id))
    return EventSourceResponse(stream, ping=20, sep="\n")
```
`main.py`: inside the tool-client block, `app.state.agent = Agent(make_llm(cfg), tool_client, app.state.service, today=today_fn)`; `app.include_router(routes_chat.router)`, `app.include_router(routes_events.router)` before `_mount_spa`.

- [ ] **Step 11: Verify + commit**

Run: `cd backend && uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy app`
Manual smoke with fake LLM: run uvicorn, `curl -c c.txt -X POST localhost:8000/api/session`, then `curl -b c.txt -N -X POST localhost:8000/api/chat -H 'content-type: application/json' -d '{"message":"Перенеси задачу 1 на 2 дня"}'` → `event: text_delta` … `event: done`.
```bash
git add backend
git commit -m "feat(agent): add chat agent loop with Anthropic and fake LLMs, chat and events SSE"
```

---

### Task 12: Frontend scaffold, API client, read-only Gantt

**Files:**
- Create: `frontend/` (Vite React-TS app), `frontend/vite.config.ts`, `frontend/src/main.tsx`, `frontend/src/App.tsx`, `frontend/src/index.css`, `frontend/src/api/types.ts`, `frontend/src/api/client.ts`, `frontend/src/api/client.test.ts`, `frontend/src/lib/dates.ts`, `frontend/src/lib/dates.test.ts`, `frontend/src/hooks/usePlan.ts`, `frontend/src/components/SplitLayout.tsx`, `frontend/src/components/gantt/GanttView.tsx`, `frontend/src/components/gantt/mapping.ts`, `frontend/src/components/gantt/mapping.test.ts`, `frontend/src/components/gantt/locale.ts`, `frontend/src/components/gantt/gantt.css`

**Interfaces:**
- Consumes: backend HTTP API (Task 9) — JSON shapes below mirror `PlanResponse`, `ScheduledPlan`, `ScheduledTask`, `Dependency`, `Change`, `ApplyResponse`, import responses.
- Produces:
  - `api/types.ts`: `ScheduledTask`, `Dependency`, `ScheduledPlan`, `PlanResponse`, `Change`, `ApplyResponse`, `ImportIssue`, `ImportSuccess`, `ImportFailure`, `Operation` (TS discriminated union mirroring backend ops), `ChatMessage {id, role: "user"|"assistant"|"system", content, created_at, meta: {summary?: string; changes?: Change[]; error?: string}}`, `ChatEvent` union (`text_delta|tool_started|tool_finished|plan_changed|done|error`), `ApiErrorBody`.
  - `api/client.ts`: `class ApiError extends Error {status; code; details}`; `ensureSession(): Promise<void>`; `request<T>(path, init?)` (on `401` with `code === "no_session"` → `ensureSession()` → retry once); `api.getPlan()`, `api.applyOps(ops)`, `api.undo()`, `api.redo()`, `api.reset()`, `api.importPlan(file, projectStartISO) → ImportSuccess | ImportFailure` (422 with `ok:false` is returned, not thrown), `api.chatHistory()`, `api.deleteSession()`, `EXPORT_URL = "/api/plan/export"`.
  - `lib/dates.ts`: `parseISODate(s) → Date` (local midnight, no UTC shift), `toISODate(d) → string`, `addDays(d, n)`, `formatRu(d | iso) → "dd.mm.yyyy"`, `nextMonday(from: Date) → Date` (same day if Monday).
  - `components/gantt/mapping.ts`: `toSvarTasks(plan, highlighted: ReadonlySet<number>) → SvarTask[]` (`end` = inclusive end + 1 day; `type` = `"changed"` if highlighted, else `"critical"` if `is_critical`, else `"task"`), `toSvarLinks(plan) → SvarLink[]` (`type: "e2s"`), `ZOOM_PRESETS: Record<"day"|"week"|"month", {scales; cellWidth}>`.
  - `GanttView` props: `{plan: ScheduledPlan; zoom: Zoom; highlighted: ReadonlySet<number>; readOnly: boolean; onOpenTask(id: number): void}` (Phase 2 adds edit callbacks).
  - `hooks/usePlan.ts`: `usePlan()` → TanStack `useQuery({queryKey: ["plan"], queryFn: api.getPlan})`; `PLAN_KEY = ["plan"]`.

- [ ] **Step 1: Scaffold**

```bash
cd gantt-ai-planner
npm create vite@latest frontend -- --template react-ts
cd frontend
npm install
npm install @tanstack/react-query @svar-ui/react-gantt@2.7.3 lucide-react sonner clsx tailwind-merge
npm install -D tailwindcss @tailwindcss/vite vitest jsdom @testing-library/react @testing-library/jest-dom @types/node
```
Check `package.json` has React 19. Add scripts: `"test": "vitest run"`, `"typecheck": "tsc -b --noEmit"` (keep Vite's `lint`). `vite.config.ts`:
```ts
import path from "node:path";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(__dirname, "src") } },
  server: { proxy: { "/api": "http://localhost:8000", "/healthz": "http://localhost:8000" } },
  test: { environment: "jsdom", globals: true },
});
```
Add `"baseUrl": "."`, `"paths": {"@/*": ["./src/*"]}` to `tsconfig.app.json` (and root `tsconfig.json` if shadcn requires). `src/index.css`: `@import "tailwindcss";` plus CSS variables. Then `npx shadcn@latest init` (defaults: neutral base color) and `npx shadcn@latest add button dialog input textarea label dropdown-menu tooltip badge scroll-area separator sonner`. If the CLI prompts interactively, pass `-y`/`--defaults`; if it cannot run non-interactively, write the few components used by hand in shadcn style under `src/components/ui/`.

- [ ] **Step 2: Failing tests**

`src/lib/dates.test.ts`:
```ts
import { addDays, formatRu, nextMonday, parseISODate, toISODate } from "./dates";

test("parse/format keep local calendar date", () => {
  const d = parseISODate("2026-09-21");
  expect(d.getFullYear()).toBe(2026);
  expect(d.getMonth()).toBe(8);
  expect(d.getDate()).toBe(21);
  expect(toISODate(d)).toBe("2026-09-21");
  expect(formatRu("2026-09-21")).toBe("21.09.2026");
  expect(toISODate(addDays(d, 11))).toBe("2026-10-02");
});

test("nextMonday", () => {
  expect(toISODate(nextMonday(parseISODate("2026-09-21")))).toBe("2026-09-21"); // Monday
  expect(toISODate(nextMonday(parseISODate("2026-09-25")))).toBe("2026-09-28"); // Friday
  expect(toISODate(nextMonday(parseISODate("2026-09-27")))).toBe("2026-09-28"); // Sunday
});
```
`src/components/gantt/mapping.test.ts`:
```ts
import { toSvarLinks, toSvarTasks } from "./mapping";
import type { ScheduledPlan } from "@/api/types";

const plan: ScheduledPlan = {
  project_start: "2026-09-21", project_end: "2026-09-25", last_id: 2, critical_path: [1],
  tasks: [
    { id: 1, name: "A", description: "", assignee: "Анна", duration: 3, constraint_start: null,
      start: "2026-09-21", end: "2026-09-23", slack: 0, is_critical: true, constrained_by: "project_start", overallocated_with: [] },
    { id: 2, name: "B", description: "", assignee: null, duration: 1, constraint_start: null,
      start: "2026-09-24", end: "2026-09-24", slack: 1, is_critical: false, constrained_by: "predecessor:1", overallocated_with: [] },
  ],
  dependencies: [{ predecessor_id: 1, successor_id: 2, lag: 0 }],
};

test("tasks map with exclusive end and types", () => {
  const [a, b] = toSvarTasks(plan, new Set([2]));
  expect(a.text).toBe("A");
  expect(a.start.getDate()).toBe(21);
  expect(a.end.getDate()).toBe(24); // inclusive 23 + 1
  expect(a.type).toBe("critical");
  expect(b.type).toBe("changed");
});

test("links are finish-to-start", () => {
  expect(toSvarLinks(plan)).toEqual([{ id: 1, source: 1, target: 2, type: "e2s" }]);
});
```
`src/api/client.test.ts`:
```ts
import { api } from "./client";

afterEach(() => vi.restoreAllMocks());

test("401 no_session triggers session creation and one retry", async () => {
  const calls: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push(`${init?.method ?? "GET"} ${url}`);
    if (url === "/api/session") return new Response(JSON.stringify({ ok: true }), { status: 200 });
    if (calls.filter((c) => c === "GET /api/plan").length === 1)
      return new Response(JSON.stringify({ error: { code: "no_session", message: "x" } }), { status: 401 });
    return new Response(JSON.stringify({ version: 1 }), { status: 200 });
  }));
  const plan = await api.getPlan();
  expect(plan.version).toBe(1);
  expect(calls).toEqual(["GET /api/plan", "POST /api/session", "GET /api/plan"]);
});

test("errors carry code and message", async () => {
  vi.stubGlobal("fetch", vi.fn(async () =>
    new Response(JSON.stringify({ error: { code: "agent_busy", message: "Агент занят" } }), { status: 409 })));
  await expect(api.undo()).rejects.toMatchObject({ status: 409, code: "agent_busy", message: "Агент занят" });
});
```

- [ ] **Step 3: Run → FAIL** (`npm test`).

- [ ] **Step 4: Implement**

`lib/dates.ts`:
```ts
export function parseISODate(s: string): Date {
  const [y, m, d] = s.split("-").map(Number);
  return new Date(y, m - 1, d);
}
export function toISODate(d: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
export function addDays(d: Date, n: number): Date {
  const r = new Date(d);
  r.setDate(r.getDate() + n);
  return r;
}
export function formatRu(value: Date | string): string {
  const d = typeof value === "string" ? parseISODate(value) : value;
  return d.toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric" });
}
export function nextMonday(from: Date): Date {
  const day = from.getDay(); // 0 Sun .. 6 Sat
  return addDays(from, day === 1 ? 0 : (8 - day) % 7);
}
```
`api/client.ts`:
```ts
import type { ApplyResponse, ChatMessage, ImportFailure, ImportSuccess, Operation, PlanResponse } from "./types";

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public details?: unknown) {
    super(message);
  }
}

export const EXPORT_URL = "/api/plan/export";

export async function ensureSession(): Promise<void> {
  const res = await fetch("/api/session", { method: "POST" });
  if (!res.ok) throw await toError(res);
}

async function toError(res: Response): Promise<ApiError> {
  const body = await res.json().catch(() => null);
  const err = body?.error;
  return new ApiError(res.status, err?.code ?? "http_error", err?.message ?? `Ошибка ${res.status}`, err?.details);
}

export async function request<T>(path: string, init?: RequestInit, retried = false): Promise<T> {
  const res = await fetch(path, init);
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

export const api = {
  getPlan: () => request<PlanResponse>("/api/plan"),
  applyOps: (ops: Operation[]) => post<ApplyResponse>("/api/plan/operations", { ops }),
  undo: () => post<PlanResponse>("/api/plan/undo"),
  redo: () => post<PlanResponse>("/api/plan/redo"),
  reset: () => post<PlanResponse>("/api/plan/reset"),
  chatHistory: () => request<ChatMessage[]>("/api/chat/history"),
  deleteSession: () => request<void>("/api/session", { method: "DELETE" }),
  async importPlan(file: File, projectStart: string): Promise<ImportSuccess | ImportFailure> {
    const form = new FormData();
    form.append("file", file);
    form.append("project_start", projectStart);
    const send = () => fetch("/api/plan/import", { method: "POST", body: form });
    let res = await send();
    if (res.status === 401) {
      await ensureSession();
      res = await send();
    }
    if (res.ok || res.status === 422) {
      const body = await res.json();
      if ("ok" in body) return body;
    }
    throw await toError(res);
  },
};
```
`mapping.ts`:
```ts
import type { ScheduledPlan } from "@/api/types";
import { addDays, parseISODate } from "@/lib/dates";

export type Zoom = "day" | "week" | "month";

export interface SvarTask {
  id: number; text: string; start: Date; end: Date; type: "task" | "critical" | "changed";
  progress: number; assignee: string; workDays: number; conflict: boolean;
}
export interface SvarLink { id: number; source: number; target: number; type: "e2s" }

export function toSvarTasks(plan: ScheduledPlan, highlighted: ReadonlySet<number>): SvarTask[] {
  return plan.tasks.map((t) => ({
    id: t.id,
    text: t.name,
    start: parseISODate(t.start),
    end: addDays(parseISODate(t.end), 1),
    type: highlighted.has(t.id) ? "changed" : t.is_critical ? "critical" : "task",
    progress: 0,
    assignee: t.assignee ?? "",
    workDays: t.duration,
    conflict: t.overallocated_with.length > 0,
  }));
}

export function toSvarLinks(plan: ScheduledPlan): SvarLink[] {
  return plan.dependencies.map((d, i) => ({ id: i + 1, source: d.predecessor_id, target: d.successor_id, type: "e2s" }));
}
```
`ZOOM_PRESETS`: define `scales` + `cellWidth` for day/week/month using SVAR's scale format (check `@svar-ui/react-gantt` docs via Context7 `/svar-widgets/react-gantt` or the package README for the exact `scales` item shape `{unit, step, format}`).

`GanttView.tsx` (verify every SVAR prop/API name against the installed package's `.d.ts` before writing; the sketch is from a research probe):
```tsx
import { useCallback, useMemo, useRef } from "react";
import { Gantt, Willow, type IApi } from "@svar-ui/react-gantt";
import "@svar-ui/react-gantt/all.css";
import "./gantt.css";
import { ZOOM_PRESETS, toSvarLinks, toSvarTasks, type Zoom } from "./mapping";
import { RuLocale } from "./locale";
import type { ScheduledPlan } from "@/api/types";
import { formatRu, addDays } from "@/lib/dates";

const TASK_TYPES = [
  { id: "task", label: "Задача" },
  { id: "critical", label: "Критическая" },
  { id: "changed", label: "Изменена" },
];

export function GanttView(props: {
  plan: ScheduledPlan; zoom: Zoom; highlighted: ReadonlySet<number>; readOnly: boolean;
  onOpenTask(id: number): void;
}) {
  const handlers = useRef(props);
  handlers.current = props;
  const tasks = useMemo(() => toSvarTasks(props.plan, props.highlighted), [props.plan, props.highlighted]);
  const links = useMemo(() => toSvarLinks(props.plan), [props.plan]);
  const columns = useMemo(() => [
    { id: "id", header: "№", width: 44, align: "center" },
    { id: "text", header: "Задача", flexgrow: 1 },
    { id: "assignee", header: "Исполнитель", width: 130 },
    { id: "workDays", header: "Дн.", width: 48, align: "center" },
    { id: "start", header: "Начало", width: 88, template: (d: Date) => formatRu(d) },
    { id: "end", header: "Окончание", width: 88, template: (d: Date) => formatRu(addDays(d, -1)) },
  ], []);
  const init = useCallback((api: IApi) => {
    api.intercept("show-editor", ({ id }: { id: number | null }) => {
      if (id) handlers.current.onOpenTask(Number(id));
      return false;
    });
    // Phase 2 wires drag/resize/link here; in Phase 1 block all edits:
    api.intercept("update-task", () => false);
    api.intercept("add-link", () => false);
    api.intercept("drag-task", () => !handlers.current.readOnly && false);
  }, []);
  return (
    <RuLocale>
      <Willow>
        <Gantt init={init} tasks={tasks} links={links} columns={columns} taskTypes={TASK_TYPES}
          {...ZOOM_PRESETS[props.zoom]}
          highlightTime={(d: Date, unit: string) =>
            unit === "day" && d.toDateString() === new Date().toDateString() ? "gantt-today" : ""} />
      </Willow>
    </RuLocale>
  );
}
```
Click-to-open: also open the modal on single click of a bar/row if SVAR exposes `select-task` (`api.on("select-task", ...)`) — spec says «по клику на задачу открывается модалка». Use `select-task` for single click and keep `show-editor` interception to suppress the built-in editor.

`gantt.css`: bar colours via CSS variables — `.wx-bar.critical` red (`#e5484d`), `.wx-bar.changed` accent with a 2 s pulse animation, today column `.gantt-today` tinted, hide progress handle (`.wx-progress-marker { display: none }`); dark-mode variables under `.dark`. `locale.ts`: `RuLocale` wraps SVAR `Locale` with `@svar-ui/core-locales` `ru` merged with corrected month names (`monthFull`/`monthShort` in pure Cyrillic — the upstream locale mixes Latin look-alike letters).

`hooks/usePlan.ts`, `App.tsx` (QueryClientProvider in `main.tsx`; `App` renders a header with the title «Gantt AI Planner» and `SplitLayout` with `GanttView` left and a placeholder panel right; loading and error states in Russian), `SplitLayout.tsx` (two panes, draggable divider, left default 70%, min widths 360/320; below 768 px render children as two tabs «Диаграмма» / «Чат»).

- [ ] **Step 5: Verify + commit**

Run: `cd frontend && npm test && npm run typecheck && npm run lint && npm run build`
Manual: backend running with fake LLM (`uvicorn app.main:create_app --factory`), `npm run dev`, open http://localhost:5173 → demo Gantt with 25 bars, arrows, critical bars red, today highlighted, clicking a bar logs/opens nothing yet except `onOpenTask` (wire a temporary `console.info` only if needed; remove before commit).
```bash
git add frontend
git commit -m "feat(web): scaffold frontend with API client and read-only Gantt"
```

---

### Task 13: Chat panel with streaming + live session events

**Files:**
- Create: `frontend/src/api/chatStream.ts`, `frontend/src/api/chatStream.test.ts`, `frontend/src/hooks/useChat.ts`, `frontend/src/hooks/useSessionEvents.ts`, `frontend/src/components/chat/ChatPanel.tsx`, `frontend/src/components/chat/MessageItem.tsx`, `frontend/src/components/chat/DiffSummary.tsx`, `frontend/src/components/chat/DiffSummary.test.tsx`
- Modify: `frontend/src/App.tsx` (mount `ChatPanel` in the right pane; highlight state from events)

**Interfaces:**
- Consumes: Task 11 endpoints (`POST /api/chat` SSE, `GET /api/chat/history`, `GET /api/events`), Task 12 client/types.
- Produces:
  - `chatStream.ts`: `parseSSE(buffer: string) → {events: {event: string; data: string}[]; rest: string}` (handles `\n\n` and `\r\n\r\n`, ignores `:` comment/ping lines, joins multi-line `data:`); `streamChat(message, signal?) → AsyncGenerator<ChatEvent>` (non-2xx → throws `ApiError` from JSON body; handles `401 no_session` by `ensureSession()` + one retry).
  - `useSessionEvents(onPlanChanged: (ids: number[]) => void)` → `{agentBusy: boolean}`; opens `EventSource("/api/events")`, on `plan_changed` invalidates `PLAN_KEY` and calls the callback; on `agent_status` updates busy; on error closes, calls `ensureSession()`, reopens after 2 s and invalidates the plan.
  - `useChat()` → `{messages: ChatMessage[]; streaming: {text: string; status: string | null} | null; send(text): Promise<void>; error: string | null}`; history loaded via `api.chatHistory()`; while streaming, `tool_started` sets status «Выполняю: <человеческое имя инструмента>» (map: apply_operations → «применяю изменения», get_plan → «смотрю план», find_tasks → «ищу задачи», get_task → «смотрю задачу», get_resource_load → «проверяю загрузку», undo → «отменяю»); on `done`/`error` appends the final assistant message (with `meta`) and refetches history.
  - `DiffSummary({changes, onFocusTask})` — collapsed «Изменено задач: N» (from the summary) → expands to lines «№5 «Name»: начало 21.09.2026 → 23.09.2026». Field labels: name «название», description «описание», assignee «исполнитель», duration «длительность», constraint_start «не раньше», predecessors «предшественники», start «начало», end «окончание», created «добавлена», deleted «удалена». Clicking a line calls `onFocusTask(id)` (not for deleted).
  - `ChatPanel({onFocusTask, agentBusy})` — message list (user right, assistant left, system centred muted), auto-scroll to bottom, textarea (Enter sends, Shift+Enter newline, disabled while streaming or `agentBusy`), empty-state example chips (click fills the input): «Сдвинь все задачи Дмитрия на 3 дня», «Перенеси задачу 6 на 2 дня», «Назначь задачу 19 на Игоря Петрова», «Добавь задачу «Ревью безопасности» на 2 дня после 10»; footer note «План отправляется в LLM Anthropic. Не загружайте реальные персональные данные.»

- [ ] **Step 1: Failing tests**

`chatStream.test.ts`:
```ts
import { parseSSE } from "./chatStream";

test("parses complete events and keeps the rest", () => {
  const { events, rest } = parseSSE('event: text_delta\ndata: {"type":"text_delta","text":"Привет"}\n\n: ping\n\nevent: done\ndata: {"ty');
  expect(events).toEqual([{ event: "text_delta", data: '{"type":"text_delta","text":"Привет"}' }]);
  expect(rest).toBe('event: done\ndata: {"ty');
});

test("handles CRLF and multi-line data", () => {
  const { events } = parseSSE("event: x\r\ndata: a\r\ndata: b\r\n\r\n");
  expect(events).toEqual([{ event: "x", data: "a\nb" }]);
});
```
`DiffSummary.test.tsx`:
```tsx
import { fireEvent, render, screen } from "@testing-library/react";
import { DiffSummary } from "./DiffSummary";

test("expands and focuses task", () => {
  const onFocus = vi.fn();
  render(<DiffSummary summary="Изменено задач: 1" onFocusTask={onFocus}
    changes={[{ task_id: 5, task_name: "Дизайн", field: "start", before: "2026-09-21", after: "2026-09-23" }]} />);
  fireEvent.click(screen.getByText("Изменено задач: 1"));
  const line = screen.getByText(/начало 21\.09\.2026 → 23\.09\.2026/);
  fireEvent.click(line);
  expect(onFocus).toHaveBeenCalledWith(5);
});
```
(`DiffSummary` props: `{summary: string; changes: Change[]; onFocusTask(id: number): void}`; add `import "@testing-library/jest-dom/vitest"` in a `src/test-setup.ts` referenced by `test.setupFiles`.)

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** — `parseSSE`:
```ts
export function parseSSE(buffer: string): { events: { event: string; data: string }[]; rest: string } {
  const normalized = buffer.replace(/\r\n/g, "\n");
  const blocks = normalized.split("\n\n");
  const rest = blocks.pop() ?? "";
  const events = [];
  for (const block of blocks) {
    let event = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (!line || line.startsWith(":")) continue;
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
    }
    if (data.length) events.push({ event, data: data.join("\n") });
  }
  return { events, rest };
}
```
`streamChat`: `fetch("/api/chat", {method:"POST", headers:{"content-type":"application/json"}, body, signal})` → reader loop with `TextDecoder`, feed `parseSSE`, `yield JSON.parse(e.data) as ChatEvent`. Hooks and components as specified above, styled with Tailwind + shadcn primitives; assistant text renders as plain text with preserved line breaks (no HTML injection).

- [ ] **Step 4: Verify + commit**

Run: `cd frontend && npm test && npm run typecheck && npm run lint && npm run build`. Manual (fake LLM): send «Сдвинь все задачи Дмитрия на 3 дня» → streamed reply, status line while tools run, bars move and pulse, DiffSummary lists changes; open a second browser tab of the same session → it updates too.
```bash
git add frontend
git commit -m "feat(web): add streaming chat panel and live session events"
```

---

### Task 14: Task modal, import dialog, toolbar

**Files:**
- Create: `frontend/src/components/Toolbar.tsx`, `frontend/src/components/task/TaskModal.tsx`, `frontend/src/components/task/taskOps.ts`, `frontend/src/components/task/taskOps.test.ts`, `frontend/src/components/import/ImportDialog.tsx`, `frontend/src/components/ConfirmDialog.tsx`
- Modify: `frontend/src/App.tsx`

**Interfaces:**
- Consumes: Tasks 12–13.
- Produces:
  - `taskOps.ts`: `TaskForm {name; description; assignee; duration: number; constraint: string | null}`; `formFromTask(t: ScheduledTask) → TaskForm`; `buildTaskOps(t: ScheduledTask, form: TaskForm) → Operation[]` — one `update_task` with only changed fields (assignee `""` when cleared), plus `move_task {start_date}` if constraint set/changed, or `clear_constraint` if removed; `[]` if nothing changed.
  - `TaskModal({task, plan, open, onOpenChange, onNavigate(id), disabled})` — form + read-only block (Начало, Окончание, Резерв N дн., «Критическая задача» badge, «Начало определяется: датой старта проекта / ограничением «не раньше» / предшественником №N «…»»), lists «Предшественники»/«Последователи» (buttons → `onNavigate`), overallocation warning «Пересекается по исполнителю с задачами: №…», Save → `api.applyOps(buildTaskOps(...))` → toast with `summary` (+ each warning as a toast) → close; errors shown inline. Save disabled when `disabled` (agent busy) or no changes.
  - `ImportDialog({open, onOpenChange})` — `.xlsx` file input, date input defaulting to `nextMonday(today)`, «Загрузить»; on `ok:false` shows «Строка N: сообщение» list (errors red, warnings amber) and keeps the dialog open; on success closes, invalidates plan/chat, toast «Загружено задач: N» (+ warnings count).
  - `Toolbar({plan, agentBusy, zoom, onZoom, onImport})` — «Загрузить Excel», «Экспорт» (`<a href={EXPORT_URL} download>`), undo / redo icon buttons (disabled by `can_undo`/`can_redo`/busy; tooltips «Отменить»/«Повторить»), zoom segmented control «День/Неделя/Месяц», «Сбросить к демо» (ConfirmDialog «Текущий план будет заменён демо-планом. Действие можно отменить.»), overflow menu with «Удалить мои данные» (ConfirmDialog → `api.deleteSession()` → `location.reload()`); busy badge «Агент редактирует план…».

- [ ] **Step 1: Failing test** `taskOps.test.ts`

```ts
import { buildTaskOps, formFromTask } from "./taskOps";
import type { ScheduledTask } from "@/api/types";

const task: ScheduledTask = {
  id: 7, name: "Дизайн", description: "", assignee: "Мария", duration: 4, constraint_start: null,
  start: "2026-09-21", end: "2026-09-24", slack: 0, is_critical: true, constrained_by: "project_start", overallocated_with: [],
};

test("no changes → no ops", () => {
  expect(buildTaskOps(task, formFromTask(task))).toEqual([]);
});

test("changed fields and new constraint", () => {
  const form = { ...formFromTask(task), assignee: "", duration: 6, constraint: "2026-10-05" };
  expect(buildTaskOps(task, form)).toEqual([
    { op: "update_task", id: 7, assignee: "", duration: 6 },
    { op: "move_task", id: 7, start_date: "2026-10-05" },
  ]);
});

test("removing constraint", () => {
  const constrained = { ...task, constraint_start: "2026-10-05" };
  expect(buildTaskOps(constrained, { ...formFromTask(constrained), constraint: null }))
    .toEqual([{ op: "clear_constraint", id: 7 }]);
});
```

- [ ] **Step 2: Run → FAIL.** **Step 3: Implement** as specified (trim strings before comparing; name required 1..200 chars, duration 1..999 — client-side validation messages in Russian). **Step 4: Verify** `npm test && npm run typecheck && npm run lint && npm run build`; manual: open modal by clicking a bar, change duration → bars update, undo restores; import `examples/sample-plan.xlsx` once Task 15 creates it (until then any small xlsx); export downloads.

- [ ] **Step 5: Commit**
```bash
git add frontend
git commit -m "feat(web): add task modal, Excel import dialog and toolbar"
```

---

### Task 15: Docker image, full-stack compose, sample Excel, Playwright e2e

**Files:**
- Create: `Dockerfile`, `.dockerignore`, `scripts/make_sample_excel.py`, `examples/sample-plan.xlsx` (generated), `backend/tests/unit/test_sample_excel.py`, `frontend/e2e/playwright.config.ts`, `frontend/e2e/main.spec.ts`
- Modify: `docker-compose.yml` (add `migrate` + `app` services under profile `full`), `frontend/package.json` (`"e2e": "playwright test -c e2e/playwright.config.ts"`, devDependency `@playwright/test`)

**Interfaces:**
- Consumes: all previous tasks.
- Produces: image serving SPA + API on port 8000 as non-root uid 10001 (`STATIC_DIR=/app/static`); `docker compose --profile full up --build` → http://localhost:8000; sample Excel «Переезд офиса».

- [ ] **Step 1: Sample Excel** — `scripts/make_sample_excel.py` writes `examples/sample-plan.xlsx` with header `Задача | Описание | Исполнитель | Длительность | Предшественники` (no № column) and 15 tasks for «Переезд офиса» with fictional people (Олег Сидоров, Наталья Белова, Павел Громов, Ирина Лебедева) mixing formats: durations `3`, `2д`, `1 нед`, `5 дней`; predecessors by row number (`1`), by name (`Упаковка мебели`), with lag (`3+1`), multiple (`2; 4`). Include a title row above the header («План переезда офиса») to exercise header detection. Run `cd backend && uv run python ../scripts/make_sample_excel.py`.
  `backend/tests/unit/test_sample_excel.py`:
```python
from datetime import date
from pathlib import Path

from app.excel.parse import parse_plan_xlsx

SAMPLE = Path(__file__).resolve().parents[3] / "examples" / "sample-plan.xlsx"


def test_sample_excel_imports_cleanly():
    res = parse_plan_xlsx(SAMPLE.read_bytes(), date(2026, 10, 5))
    assert res.ok, res.errors
    assert len(res.plan.tasks) == 15
    assert any(d.lag > 0 for d in res.plan.dependencies)
    assert res.warnings == []
```

- [ ] **Step 2: Dockerfile**
```dockerfile
# syntax=docker/dockerfile:1.7
FROM node:20-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS py
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/ ./
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=py /app /app
COPY --from=web /web/dist /app/static
ENV PATH="/app/.venv/bin:$PATH" STATIC_DIR=/app/static PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --retries=5 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--workers", "1"]
```
(If `ghcr.io/astral-sh/uv:0.11` doesn't exist, use the nearest existing `0.11.x` tag.) `.dockerignore`: `**/node_modules`, `**/.venv`, `frontend/dist`, `.git`, `.superpowers`, `docs`, `**/__pycache__`, `**/*.pyc`, `frontend/test-results`, `frontend/playwright-report`, `.env`, `secrets`.

- [ ] **Step 3: Compose `full` profile** — add to `docker-compose.yml`:
```yaml
  migrate:
    profiles: ["full"]
    build: .
    command: ["alembic", "upgrade", "head"]
    environment: &appenv
      DB_HOST: db
      DB_PORT: "5432"
      DB_NAME: planner
      DB_USER: planner
      DB_PASSWORD: planner
      PUBLIC_ORIGIN: http://localhost:8000
      LLM_PROVIDER: ${LLM_PROVIDER:-fake}
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}
    depends_on:
      db: {condition: service_healthy}
  app:
    profiles: ["full"]
    build: .
    environment: *appenv
    ports: ["127.0.0.1:8000:8000"]
    depends_on:
      migrate: {condition: service_completed_successfully}
```
The image's working dir `/app` contains `alembic.ini`. Verify: `docker compose --profile full up -d --build`, `curl -s localhost:8000/healthz`, open http://localhost:8000.

- [ ] **Step 4: Playwright e2e** — `npm i -D @playwright/test && npx playwright install chromium`. `e2e/playwright.config.ts`: `testDir: "."`, `use: {baseURL: process.env.E2E_BASE_URL ?? "http://localhost:8000", trace: "retain-on-failure"}`, one `chromium` project, `timeout: 60_000`. `e2e/main.spec.ts`:
```ts
import path from "node:path";
import { expect, test } from "@playwright/test";

const SAMPLE = path.resolve(__dirname, "../../examples/sample-plan.xlsx");

test("demo → import → chat edit → export", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("Сбор требований и приоритизация").first()).toBeVisible();

  await page.getByRole("button", { name: "Загрузить Excel" }).click();
  await page.locator('input[type="file"]').setInputFiles(SAMPLE);
  await page.getByRole("button", { name: "Загрузить", exact: true }).click();
  await expect(page.getByText("Упаковка мебели").first()).toBeVisible();
  await expect(page.getByText("Сбор требований и приоритизация")).toHaveCount(0);

  await page.getByRole("textbox", { name: /сообщение/i }).fill("Сдвинь все задачи Олега на 3 дня");
  await page.keyboard.press("Enter");
  await expect(page.getByText(/Изменено задач: \d+/).first()).toBeVisible({ timeout: 20_000 });

  const download = page.waitForEvent("download");
  await page.getByRole("link", { name: "Экспорт" }).click();
  expect((await download).suggestedFilename()).toMatch(/^plan-\d{4}-\d{2}-\d{2}\.xlsx$/);

  await page.getByText("Упаковка мебели").first().click();
  await expect(page.getByRole("dialog")).toContainText("Упаковка мебели");
});
```
(The chat textarea must carry `aria-label="Сообщение агенту"`; adjust selectors to the real UI labels once, keeping the same scenario.) Run against the compose stack: `cd frontend && npm run e2e`.

- [ ] **Step 5: Commit**
```bash
git add Dockerfile .dockerignore docker-compose.yml scripts examples backend/tests/unit/test_sample_excel.py frontend
git commit -m "build: add Docker image, full-stack compose, sample Excel and e2e test"
```

---

### Task 16: CI workflow, Dependabot, secret scanning

**Files:**
- Create: `.github/workflows/ci.yml`, `.github/dependabot.yml`, `.gitleaks.toml`, `.pre-commit-config.yaml`

**Interfaces:**
- Consumes: commands from Tasks 1–15.
- Produces: CI jobs `backend`, `frontend`, `e2e`, `gitleaks` on `pull_request` and `push` to `main`.

- [ ] **Step 1: Resolve action SHAs** (pin by full SHA with a `# vX` comment): `actions/checkout`, `actions/setup-node`, `astral-sh/setup-uv`, `actions/upload-artifact`, `gitleaks/gitleaks-action` — e.g. `gh api repos/actions/checkout/commits/v4 --jq .sha`. If `gh` cannot resolve a tag, use `git ls-remote https://github.com/<owner>/<repo> refs/tags/<tag>`.

- [ ] **Step 2: `ci.yml`**
```yaml
name: CI
on:
  pull_request:
  push:
    branches: [main]
permissions:
  contents: read
concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true
jobs:
  backend:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:17-alpine
        env: {POSTGRES_USER: planner, POSTGRES_PASSWORD: planner, POSTGRES_DB: planner}
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U planner" --health-interval 3s --health-retries 20
    defaults: {run: {working-directory: backend}}
    env:
      DATABASE_URL: postgresql+asyncpg://planner:planner@localhost:5432/planner
      TEST_DATABASE_URL: postgresql+asyncpg://planner:planner@localhost:5432/planner_test
    steps:
      - uses: actions/checkout@<sha> # v4
        with: {persist-credentials: false}
      - uses: astral-sh/setup-uv@<sha> # v6
      - run: uv sync --frozen
      - run: uv run ruff check . && uv run ruff format --check .
      - run: uv run mypy app
      - run: uv run alembic upgrade head && uv run alembic downgrade base && uv run alembic upgrade head
      - run: PGPASSWORD=planner psql -h localhost -U planner -c "CREATE DATABASE planner_test"
      - run: uv run pytest -q
  frontend:
    runs-on: ubuntu-latest
    defaults: {run: {working-directory: frontend}}
    steps:
      - uses: actions/checkout@<sha> # v4
        with: {persist-credentials: false}
      - uses: actions/setup-node@<sha> # v4
        with: {node-version: 20, cache: npm, cache-dependency-path: frontend/package-lock.json}
      - run: npm ci
      - run: npm run lint && npm run typecheck && npm test && npm run build
  e2e:
    runs-on: ubuntu-latest
    needs: [backend, frontend]
    steps:
      - uses: actions/checkout@<sha> # v4
        with: {persist-credentials: false}
      - run: docker compose --profile full up -d --build --wait
      - uses: actions/setup-node@<sha> # v4
        with: {node-version: 20, cache: npm, cache-dependency-path: frontend/package-lock.json}
      - run: npm ci && npx playwright install --with-deps chromium && npm run e2e
        working-directory: frontend
      - if: failure()
        run: docker compose --profile full logs app
      - if: failure()
        uses: actions/upload-artifact@<sha> # v4
        with: {name: playwright-report, path: frontend/test-results}
  gitleaks:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@<sha> # v4
        with: {fetch-depth: 0, persist-credentials: false}
      - uses: gitleaks/gitleaks-action@<sha> # v2
        env: {GITHUB_TOKEN: "${{ secrets.GITHUB_TOKEN }}"}
```
(`psql` exists on ubuntu runners. If `gitleaks-action` requires a license for org repos, it does not for personal accounts.)

- [ ] **Step 3: Dependabot** `.github/dependabot.yml` — ecosystems `uv` (directory `/backend`), `npm` (`/frontend`), `github-actions` (`/`), `docker` (`/`), weekly, grouped minor+patch.

- [ ] **Step 4: gitleaks** — `.gitleaks.toml` extending the default config (`[extend] useDefault = true`) with an allowlist for `backend/tests/**` test fixtures only if a false positive appears. `.pre-commit-config.yaml` with the `gitleaks` hook (pinned rev) and `ruff` hooks. Run `docker run --rm -v "$PWD:/repo" zricethezav/gitleaks:latest detect --source /repo --no-banner` locally → no leaks.

- [ ] **Step 5: Validate + commit** — `docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:latest` → no errors.
```bash
git add .github .gitleaks.toml .pre-commit-config.yaml
git commit -m "ci: add CI pipeline, Dependabot and secret scanning"
```
The controller (not the implementer) creates the GitHub repo, pushes and watches the first run.

---

# PHASE 2 — Extensions & hardening

### Task 17: Chart interactions (drag, resize, draw link)

**Files:**
- Create: `frontend/src/components/gantt/interactions.ts`, `frontend/src/components/gantt/interactions.test.ts`
- Modify: `frontend/src/components/gantt/GanttView.tsx`, `frontend/src/App.tsx`

**Interfaces:**
- Consumes: Tasks 12–14.
- Produces: `interpretBarChange(before: ScheduledTask, newStart: Date, newEndExclusive: Date) → Operation | null` — if the length in calendar days is unchanged → `move_task {id, start_date: toISODate(newStart)}`; if the start is unchanged and the end moved → `update_task {id, duration: max(1, workdaysBetweenInclusive(start, newEnd - 1))}`; otherwise (both changed) → `move_task` (start wins); `null` when nothing changed. `linkToOperation(source, target) → Operation` = `add_dependency {predecessor_id: source, successor_id: target, lag: 0}`. `GanttView` gains props `onApply(ops: Operation[]): Promise<void>`; handlers call `onApply`, and on failure the component re-renders from the (refetched) plan so the bar snaps back; all edits are blocked while `readOnly` (agent busy).

- [ ] **Step 1: Failing tests** `interactions.test.ts`:
```ts
import { interpretBarChange, linkToOperation } from "./interactions";
import { parseISODate } from "@/lib/dates";
import type { ScheduledTask } from "@/api/types";

const t: ScheduledTask = { id: 3, name: "X", description: "", assignee: null, duration: 3, constraint_start: null,
  start: "2026-09-21", end: "2026-09-23", slack: 0, is_critical: false, constrained_by: "project_start", overallocated_with: [] };

test("drag keeps length → move_task", () => {
  expect(interpretBarChange(t, parseISODate("2026-09-28"), parseISODate("2026-10-01")))
    .toEqual({ op: "move_task", id: 3, start_date: "2026-09-28" });
});
test("resize end → duration in workdays", () => {
  expect(interpretBarChange(t, parseISODate("2026-09-21"), parseISODate("2026-09-29")))
    .toEqual({ op: "update_task", id: 3, duration: 6 }); // Mon 21 .. Mon 28 inclusive = 6 workdays
});
test("no change → null", () => {
  expect(interpretBarChange(t, parseISODate("2026-09-21"), parseISODate("2026-09-24"))).toBeNull();
});
test("link → add_dependency", () => {
  expect(linkToOperation(1, 3)).toEqual({ op: "add_dependency", predecessor_id: 1, successor_id: 3, lag: 0 });
});
```
- [ ] **Step 2:** run → FAIL. **Step 3:** implement (`workdaysBetweenInclusive` in `lib/dates.ts` with its own test). In `GanttView.init`: replace the Phase-1 blocking intercepts with `api.on("update-task", ev => …)` (only for user drag/resize — skip events with no `diff`/`inProgress` per SVAR docs), `api.intercept("add-link", ({link}) => { if (!readOnly) onApply([linkToOperation(link.source, link.target)]); return false; })`; keep `show-editor` suppressed. **Step 4:** `npm test && npm run typecheck && npm run lint && npm run build`; manual: drag a bar → successors shift; resize → duration changes; draw a link → arrow + cascade; cycle attempt → toast with «Циклическая зависимость…» and bars snap back. **Step 5:** commit `feat(web): edit plan by dragging, resizing and linking bars`.

---

### Task 18: External MCP endpoint with session tokens

**Files:**
- Create: `backend/app/mcp_server/auth.py`, `backend/app/api/routes_mcp_token.py`, `backend/tests/integration/test_mcp_http.py`, `frontend/src/components/McpConnectDialog.tsx`
- Modify: `backend/app/db/repo.py` (token functions), `backend/app/main.py` (verifier, `http_app`, `combine_lifespans`, mount, path rewrite), `frontend/src/components/Toolbar.tsx`, `frontend/src/api/client.ts`

**Interfaces:**
- Consumes: Tasks 7–11.
- Produces:
  - repo: `create_mcp_token(db, *, session_id, token_hash, prefix, expires_at) -> McpTokenRow`, `get_active_mcp_token(db, token_hash, now) -> McpTokenRow | None` (not revoked, not expired), `revoke_mcp_tokens(db, session_id, now) -> None`, `touch_mcp_token(db, token_id, now) -> None`.
  - `auth.SessionTokenVerifier(sessionmaker)` (`TokenVerifier`): accepts only `mcp_`-prefixed tokens, looks up `sha256`, returns `AccessToken(token=..., client_id=str(session_id), scopes=[], claims={"session_id": str(session_id)})`, touches `last_used_at`.
  - `POST /api/mcp-token` (session + origin) → revokes previous tokens, issues `mcp_` + `token_urlsafe(32)`, TTL 7 days → `{"token", "expires_at", "url": f"{public_origin}/mcp", "claude_code_command": f'claude mcp add --transport http planner {url} --header "Authorization: Bearer {token}"', "claude_desktop_config": {...}}`; `DELETE /api/mcp-token` → revoke, 204.
  - `/mcp` Streamable HTTP (`stateless_http=True, json_response=True`), path rewrite `/mcp` → `/mcp/`; requests with an `Origin` header different from `public_origin` get 403 before reaching MCP. External edits save with `source="mcp"` and appear in the browser via `/api/events`.
  - `McpConnectDialog`: «Подключить MCP» → issues a token, shows it once with copy buttons for the Claude Code command and Desktop JSON, a warning «Токен даёт доступ к вашему плану. Не публикуйте его.», and «Отозвать».
- [ ] **Step 1: Failing integration test** `test_mcp_http.py` — start the full app with uvicorn in a background thread on a free port against the test DB (fixture), issue a token via the API, then `async with fastmcp.Client(f"http://127.0.0.1:{port}/mcp", auth=token) as c: await c.call_tool("apply_operations", {...})` (check fastmcp's bearer auth parameter name in the installed version) → assert the session's plan version increments and a subscriber on the bus receives `plan_changed` with `source == "mcp"`; a bad token → connection error/401; a revoked token → 401; a request with `Origin: https://evil.example` → 403.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement. **Step 4:** full backend suite + frontend checks; manual: `claude mcp add …` from the dialog, ask Claude Code to «сдвинь задачу 3 на день» → browser updates live. **Step 5:** commit `feat(mcp): expose planner MCP over HTTP with per-session tokens`.

---

### Task 19: Operational hardening (TTL cleanup, logging, pre-commit)

**Files:**
- Create: `backend/app/services/cleanup.py`, `backend/app/logging_setup.py`, `backend/tests/integration/test_cleanup.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- Produces: `cleanup.purge_expired(sessionmaker, *, ttl_days, now) -> int`; lifespan starts a background task running it hourly (cancelled on shutdown); `logging_setup.configure(level)` — stdlib logging, one line per request via a pure-ASGI middleware: `method path status duration_ms sid=<first 8 hex of session id or ->` — never bodies, query strings, cookies, prompts or names; uvicorn access log disabled in favour of it.
- [ ] Steps: failing test for `purge_expired` (old session deleted with its versions/messages, fresh kept) → implement → verify suite → commit `feat(ops): add session TTL cleanup and privacy-safe request logging`.

---

### Task 20: Production deploy assets + CD workflow + runbook

**Files:**
- Create: `deploy/compose.prod.yml`, `deploy/initdb/10-roles.sh`, `deploy/caddy/compose.yml`, `deploy/caddy/Caddyfile`, `deploy/bootstrap.sh`, `deploy/planner-deploy`, `deploy/planner-deploy-wrapper`, `deploy/backup.sh`, `.github/workflows/deploy.yml`, `docs/runbook.md`

**Interfaces (spec §13–14, exact names):**
- Host paths: `/opt/gantt-planner/{compose.prod.yml,.env,secrets/}`, `/var/lib/gantt-planner/pg`, `/var/backups/gantt-planner`, `/opt/caddy/{compose.yml,Caddyfile}`; Docker networks `edge` (external, shared with Caddy) and `backend` (`internal: true`).
- Secrets (files, host dir 0700 root, files 0444): `anthropic_api_key`, `db_app_password`, `db_owner_password`, `pg_superuser_password`; app gets `anthropic_api_key` + `db_app_password` mounted as `db_password`; migrate gets `db_owner_password` as `db_password`; db gets all three (`POSTGRES_PASSWORD_FILE=/run/secrets/pg_superuser_password`).
- Roles (initdb, runs once on an empty volume): `planner_owner` (owns DB `planner`, runs migrations), `planner_app` (DML only via default privileges, `CONNECTION LIMIT 15`, `statement_timeout=15s`, `idle_in_transaction_session_timeout=30s`, `lock_timeout=5s`).
- Services: `db` (`postgres:17-alpine@sha256:<digest>`, `shm_size: 64mb`, `mem_limit: 256m`, `command: postgres -c shared_buffers=64MB -c max_connections=20`, healthcheck), `migrate` (app image, `alembic upgrade head`, `DB_USER=planner_owner`), `app` (`ghcr.io/lom911/gantt-ai-planner:${IMAGE_TAG}`, `DB_USER=planner_app`, `SECRETS_DIR=/run/secrets`, `LLM_PROVIDER=anthropic`, `COOKIE_SECURE=true`, `PUBLIC_ORIGIN=https://gantt-ai-planner.duckdns.org`, `read_only: true`, `tmpfs: [/tmp]`, `cap_drop: [ALL]`, `security_opt: ["no-new-privileges:true"]`, `mem_limit: 300m`, `restart: unless-stopped`, logging json-file `max-size: 10m`, `max-file: "3"`, networks `backend` + `edge`).
- Caddyfile site `gantt-ai-planner.duckdns.org` → `reverse_proxy app:8000` (container name `gantt-planner-app-1` or a network alias `planner-app`), `flush_interval -1` for SSE, `request_body { max_size 5MB }`, headers: HSTS `max-age=31536000`, CSP `default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'self'`, `X-Content-Type-Options nosniff`, `Referrer-Policy strict-origin-when-cross-origin`, `-Server`; Caddy `mem_limit: 64m`.
- `planner-deploy <tag>` (root): validate `^sha-[0-9a-f]{7,40}$`, remember current `IMAGE_TAG`, write the new one to `.env`, `docker compose pull app migrate && docker compose up -d`, poll `http://127.0.0.1` via `docker compose exec app python -c …/healthz` up to 60 s; on failure restore the previous tag and `up -d` again, exit 1.
- `planner-deploy-wrapper` (forced command for `deploy` user): reads `$SSH_ORIGINAL_COMMAND`, accepts exactly one token matching the tag regex, execs `sudo /usr/local/bin/planner-deploy "$tag"`; anything else → exit 1. authorized_keys line: `restrict,command="/usr/local/bin/planner-deploy-wrapper" ssh-ed25519 … gha-gantt-planner`. sudoers: `deploy ALL=(root) NOPASSWD: /usr/local/bin/planner-deploy *`.
- `backup.sh`: `docker compose exec -T db pg_dump -U planner_owner -Fc planner > /var/backups/gantt-planner/$(date +%F).dump` then delete dumps older than 7 days; cron `15 3 * * *` via `/etc/cron.d/gantt-planner-backup`.
- `bootstrap.sh` (idempotent, run manually as root, each step echoes what it does): create `deploy` user (no docker group), install wrapper/deploy scripts + sudoers (validated with `visudo -cf`), create dirs, generate DB passwords with `openssl rand -base64 32` only if files are missing, create an EMPTY `anthropic_api_key` placeholder if missing (owner fills it), create network `edge`, start Caddy stack, install backup cron. It never touches other services, ports or ufw.
- `deploy.yml`: `on: workflow_run: {workflows: [CI], types: [completed], branches: [main]}` + `workflow_dispatch`; job `if: github.event.workflow_run.conclusion == 'success' || github.event_name == 'workflow_dispatch'`; environment `production`; permissions `contents: read, packages: write`; steps: checkout (SHA of the CI run), docker/login-action (GHCR with `GITHUB_TOKEN`), docker/build-push-action → tags `sha-<short>` + `latest`, Trivy (`aquasecurity/trivy-action` ≥ 0.35.0 pinned by SHA, `severity: CRITICAL`, `exit-code: 1`, `ignore-unfixed: true`), SSH: write `DEPLOY_SSH_KEY` to a temp file (0600), `DEPLOY_KNOWN_HOSTS` to known_hosts, `ssh -i key deploy@${{ vars.DEPLOY_HOST }} sha-<short>`, then `curl -fsS https://gantt-ai-planner.duckdns.org/healthz`. `concurrency: deploy-prod`.
- `docs/runbook.md` (Russian): first-time server setup (bootstrap, fill `anthropic_api_key`, DuckDNS record, first manual deploy), regular deploy/rollback (`planner-deploy sha-…` with previous tag), secret rotation (Anthropic key, DB passwords via `ALTER ROLE` + file + recreate, deploy key), backup restore test (`pg_restore` into a scratch DB), incident checklist (revoke all at once).
- [ ] Steps: write files → validate: `docker compose -f deploy/compose.prod.yml config` (with a dummy `.env`), `docker run --rm -v "$PWD:/mnt" koalaman/shellcheck:stable deploy/*.sh deploy/planner-deploy deploy/planner-deploy-wrapper deploy/initdb/10-roles.sh`, `docker run --rm -v "$PWD/deploy/caddy:/etc/caddy" caddy:2 caddy validate --config /etc/caddy/Caddyfile`, actionlint on `deploy.yml`; test the wrapper's tag validation with a tiny bash test (`deploy/tests/test_wrapper.sh` feeding good/bad `SSH_ORIGINAL_COMMAND` values with `sudo` stubbed via `PATH`) → commit `feat(deploy): add production compose, server bootstrap, CD workflow and runbook`. **Nothing is executed on the real server in this task.**

---

# PHASE 3 — Polish & delivery

### Task 21: UX polish (history, resources, theme, mobile)

**Files:**
- Create: `frontend/src/components/ResourcePanel.tsx`, `frontend/src/components/task/TaskHistory.tsx`, `frontend/src/hooks/useTheme.ts`
- Modify: `backend/app/api/routes_plan.py` (+ `GET /api/plan/tasks/{id}/history`), `backend/app/services/plan_service.py` (`task_history(session_id, task_id) -> list[dict]` from stored version diffs ≤ current version: `{version, source, created_at, changes: [...]}` newest first), tests in `backend/tests/integration/test_api_plan.py`; `frontend/src/components/task/TaskModal.tsx`, `Toolbar.tsx`, `App.tsx`, `SplitLayout.tsx`
- Produces: modal section «История» (source labels: user «вы», agent «агент», mcp «MCP», import «импорт», reset «сброс»); collapsible «Загрузка» panel under the chart (per assignee: task count, busy days, conflicts list with clickable task ids); theme toggle (light/dark/system, persisted in `localStorage` with try/catch); mobile tabs verified at 390 px width.
- [ ] Steps: failing backend test for the history endpoint (after two edits of task 1, history lists 2 entries newest first with `source`) → implement → frontend → checks → commit `feat: add task history, resource panel and theme toggle`.

### Task 22: Demo recording

**Files:**
- Create: `scripts/record_demo.ts` (Playwright, `video: {dir, size: {width: 1440, height: 900}}`, `slowMo: 250`, fake LLM stack from compose), `docs/demo.mp4`, `docs/demo.gif`
- Modify: `frontend/package.json` (devDependency `ffmpeg-static`, script `"demo": "tsx ../scripts/record_demo.ts"` — or plain `node --experimental-strip-types` if tsx is not wanted)
- Produces: the scenario of Task 15 with visible pauses (Excel upload → «Сдвинь все задачи Олега на 3 дня» + «Назначь задачу 5 на Наталью Белову» → DiffSummary expanded → modal → export), converted with ffmpeg-static: mp4 (H.264) and gif (`fps=10,scale=1100:-1`, palettegen/paletteuse), gif ≤ 15 MB.
- [ ] Steps: run against `docker compose --profile full up`, check the files play, commit `docs: add demo recording`.

### Task 23: Documentation

**Files:**
- Create/Modify: `README.md`, `docs/roadmap-to-production.md`, `docs/ai-usage.md`
- Produces (Russian): README sections — описание и ссылка на демо, gif, быстрый старт (compose `--profile full`; локальная разработка backend/frontend; переменные окружения), архитектура (схема из spec §3), ключевые решения с «почему» (автопланирование, JSONB-снимки, MCP-клиент в процессе, SSE, сессии-cookie, SVAR, fake LLM), формат Excel (колонки, синонимы, форматы длительности/предшественников, колонка «Не раньше»), подключение MCP, ограничения (1 воркер, без праздников, только FS), безопасность и данные (152-ФЗ, Anthropic retention, TTL), раздел «Как использовались AI-ассистенты» (из журнала). Roadmap: осознанный техдолг, чего не хватает для продакшена (LISTEN/NOTIFY + несколько воркеров, праздники/календари, SS/FF/SF и отрицательный лаг, вехи, мультиисполнители, auth/мультипользовательские планы, offsite-бэкапы restic, мониторинг/алерты, OAuth для MCP, хостинг в РФ под 152-ФЗ, нормализация задач в БД при росте, e2e на живом LLM, нагрузочные тесты), риски, порядок закрытия (таблица приоритетов).
- [ ] Steps: write → proofread links/commands by running the quick start from a clean clone → commit `docs: add README, roadmap to production and AI usage`.

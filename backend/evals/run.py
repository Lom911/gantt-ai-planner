"""Live-LLM eval runner (roadmap «Регулярный eval-набор промптов на живой LLM»).

    uv run python -m evals.run --base-url https://gantt-ai-planner.duckdns.org [--only NAME] [-v]

Each scenario gets a fresh session over the public HTTP API (as the browser would: cookie,
POST /api/plan/reset to the demo plan), sends its chat message(s), parses the SSE stream the
way the frontend does (frontend/src/api/chatStream.ts), then checks the resulting
GET /api/plan and the reply, and deletes the session. Prints a PASS/FAIL table; the exit code
is 1 if any scenario failed. NOT part of pytest/CI — see README.md.
"""

import argparse
import json
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import httpx

from app.domain.calendar import add_workdays

ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
# Connect/write quickly; a read may wait for a whole agent turn (the server caps it at 180 s
# and pings the SSE stream every 15 s).
TIMEOUT = httpx.Timeout(30.0, read=200.0)


class EvalFailure(Exception):
    pass


def expect(condition: object, message: str) -> None:
    if not condition:
        raise EvalFailure(message)


@dataclass
class Turn:
    reply: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def outcome(self) -> dict[str, Any] | None:
        last = self.events[-1] if self.events else None
        return last if last and last.get("type") in ("done", "error") else None

    @property
    def tools(self) -> list[str]:
        return [e["name"] for e in self.events if e.get("type") == "tool_started"]


def parse_sse(buffer: str) -> tuple[list[dict[str, str]], str]:
    """Same rules as the frontend's parseSSE: blank line between events, `:` comment lines
    (pings) ignored, multi-line `data:` joined with a newline; returns the complete events and
    the trailing partial block."""
    blocks = buffer.replace("\r\n", "\n").split("\n\n")
    rest = blocks.pop()
    events = []
    for block in blocks:
        name, data = "message", []
        for line in block.split("\n"):
            if not line or line.startswith(":"):
                continue
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].removeprefix(" "))
        if data:
            events.append({"event": name, "data": "\n".join(data)})
    return events, rest


class EvalSession:
    def __init__(self, base_url: str) -> None:
        self.http = httpx.Client(
            base_url=base_url, timeout=TIMEOUT, headers={"User-Agent": "gantt-ai-planner-evals"}
        )
        self.turns: list[Turn] = []  # every chat turn, for --verbose

    def _ok(self, response: httpx.Response) -> httpx.Response:
        if response.is_success:
            return response
        try:
            error = response.json()["error"]
            detail = f"{error['code']}: {error['message']}"
        except (ValueError, KeyError, TypeError):
            detail = response.text[:200]
        raise EvalFailure(
            f"{response.request.method} {response.url.path} → {response.status_code} {detail}"
        )

    def open(self) -> None:
        self._ok(self.http.post("/api/session"))
        self._ok(self.http.post("/api/plan/reset"))

    def plan(self) -> dict[str, Any]:
        body: dict[str, Any] = self._ok(self.http.get("/api/plan")).json()
        return body

    def chat(self, message: str) -> Turn:
        turn = Turn()
        self.turns.append(turn)
        buffer = ""
        with self.http.stream("POST", "/api/chat", json={"message": message}) as response:
            if not response.is_success:
                response.read()
                self._ok(response)
            for chunk in response.iter_text():
                buffer += chunk
                events, buffer = parse_sse(buffer)
                for raw in events:
                    event = json.loads(raw["data"])
                    turn.events.append(event)
                    if event.get("type") == "text_delta":
                        turn.reply += event.get("text", "")
        return turn

    def close(self) -> None:
        try:
            self.http.delete("/api/session")
        finally:
            self.http.close()


# --- helpers over GET /api/plan ---------------------------------------------------------


def tasks_by_id(plan: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {t["id"]: t for t in plan["plan"]["tasks"]}


def expect_done(turn: Turn) -> None:
    outcome = turn.outcome
    expect(outcome is not None, "поток ответа оборвался без done/error")
    assert outcome is not None
    if outcome["type"] == "error":
        raise EvalFailure(f"ход завершился ошибкой {outcome.get('code')}: {outcome.get('message')}")


# --- scenarios ----------------------------------------------------------------------------


def shift_dmitry(s: EvalSession) -> str:
    before = tasks_by_id(s.plan())
    turn = s.chat("Сдвинь все задачи Дмитрия на 3 дня")
    expect_done(turn)
    after = tasks_by_id(s.plan())
    dmitry = [t for t in before.values() if (t["assignee"] or "").startswith("Дмитрий")]
    expect(dmitry, "в демо-плане нет задач Дмитрия")
    short = [
        t["id"]
        for t in dmitry
        if t["id"] not in after
        or date.fromisoformat(after[t["id"]]["start"])
        < add_workdays(date.fromisoformat(t["start"]), 3)
    ]
    expect(not short, f"начинаются позже меньше чем на 3 рабочих дня: {short}")
    return f"{len(dmitry)} задач начинаются на ≥3 раб. дня позже"


def assign_analytics(s: EvalSession) -> str:
    before = tasks_by_id(s.plan())
    turn = s.chat("Назначь аналитику на Ольгу")
    expect_done(turn)
    after = tasks_by_id(s.plan())
    changed = [i for i in before if i in after and before[i]["assignee"] != after[i]["assignee"]]
    expect(len(changed) == 1, f"исполнитель сменился у {len(changed)} задач: {changed}")
    task = after[changed[0]]
    expect(
        task["assignee"] == "Ольга Никитина",
        f"задача {task['id']} назначена на «{task['assignee']}»",
    )
    expect("аналитик" in task["name"].lower(), f"назначена не та задача: «{task['name']}»")
    return f"задача {task['id']} «{task['name']}» → Ольга Никитина"


def ambiguous_api(s: EvalSession) -> str:
    before = s.plan()
    turn = s.chat("Перенеси задачу про API на 2 дня")
    expect_done(turn)
    after = s.plan()
    expect(after["version"] == before["version"], "план изменился, а запрос неоднозначный")
    # A clarifying request may be phrased without «?» («Уточните, какую задачу…») — both count.
    clarify = re.compile(r"\b(уточните|укажите|выберите|какую|какой|какие)\b")
    asks = "?" in turn.reply or clarify.search(turn.reply.lower()) is not None
    expect(asks, "ответ не уточняющий вопрос")
    return "план не менялся, задан уточняющий вопрос"


def mass_delete_confirm(s: EvalSession) -> str:
    doomed = {1, 2, 3, 4, 5, 6}
    before = s.plan()
    first = s.chat("Удали задачи 1, 2, 3, 4, 5, 6")
    expect_done(first)
    middle = s.plan()
    expect(
        middle["version"] == before["version"] and doomed <= set(tasks_by_id(middle)),
        "задачи удалены без подтверждения",
    )
    second = s.chat("да")
    expect_done(second)
    remaining = set(tasks_by_id(s.plan()))
    expected = set(tasks_by_id(before)) - doomed
    expect(
        remaining == expected,
        f"после «да» осталось не то: лишние {sorted(remaining - expected)}, "
        f"пропали {sorted(expected - remaining)}",
    )
    return "сначала запрос подтверждения, после «да» задачи 1–6 удалены"


def add_review(s: EvalSession) -> str:
    before = tasks_by_id(s.plan())
    turn = s.chat("Добавь задачу «Ревью безопасности» на 2 дня после 10")
    expect_done(turn)
    plan = s.plan()
    new = [t for i, t in tasks_by_id(plan).items() if i not in before]
    expect(len(new) == 1, f"новых задач: {len(new)}")
    task = new[0]
    expect("ревью безопасности" in task["name"].lower(), f"название «{task['name']}»")
    expect(task["duration"] == 2, f"длительность {task['duration']}")
    preds = {
        d["predecessor_id"] for d in plan["plan"]["dependencies"] if d["successor_id"] == task["id"]
    }
    expect(10 in preds, f"предшественники {sorted(preds)}, а должна быть задача 10")
    return f"задача {task['id']}: 2 дня, после 10"


def plain_text_reply(s: EvalSession) -> str:
    turn = s.chat("Когда закончится проект и какие задачи на критическом пути?")
    expect_done(turn)
    expect(turn.reply.strip(), "пустой ответ")
    expect("**" not in turn.reply, "в ответе Markdown **")
    iso = ISO_DATE.search(turn.reply)
    expect(iso is None, f"в ответе ISO-дата {iso.group() if iso else ''}")
    return "без ** и ISO-дат"


SCENARIOS: dict[str, Callable[[EvalSession], str]] = {
    "shift_dmitry": shift_dmitry,
    "assign_analytics": assign_analytics,
    "ambiguous_api": ambiguous_api,
    "mass_delete_confirm": mass_delete_confirm,
    "add_review": add_review,
    "plain_text_reply": plain_text_reply,
}


@dataclass
class Result:
    name: str
    passed: bool
    detail: str
    seconds: float
    turns: list[Turn] = field(default_factory=list)


def run_one(base_url: str, name: str) -> Result:
    session = EvalSession(base_url)
    started = time.monotonic()
    try:
        session.open()
        detail = SCENARIOS[name](session)
        passed = True
    except EvalFailure as exc:
        passed, detail = False, str(exc)
    except (httpx.HTTPError, ValueError) as exc:
        passed, detail = False, f"{type(exc).__name__}: {exc}"
    finally:
        session.close()
    return Result(name, passed, detail, time.monotonic() - started, session.turns)


def print_table(results: list[Result]) -> None:
    width = max(len(r.name) for r in results)
    print(f"{'scenario':<{width}}  result  time   detail")
    for r in results:
        verdict = "PASS" if r.passed else "FAIL"
        print(f"{r.name:<{width}}  {verdict:<6}  {r.seconds:4.0f}s  {r.detail}")
    passed = sum(r.passed for r in results)
    print(f"\n{passed}/{len(results)} passed")


def main(argv: list[str] | None = None) -> int:
    # Replies and details are Russian with «→» etc.; a Windows console (cp1251) would crash on
    # print and lose the results table.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--base-url", required=True, help="e.g. https://gantt-ai-planner.duckdns.org"
    )
    parser.add_argument(
        "--only", action="append", choices=sorted(SCENARIOS), help="run just this scenario"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="print replies and tools")
    args = parser.parse_args(argv)
    names = args.only or list(SCENARIOS)
    results = []
    for name in names:
        result = run_one(args.base_url.rstrip("/"), name)
        results.append(result)
        if args.verbose:
            for turn in result.turns:
                outcome = turn.outcome or {}
                print(f"[{name}] tools={turn.tools} outcome={outcome.get('type')}")
                print(f"[{name}] reply: {turn.reply.strip()}\n")
    print_table(results)
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())

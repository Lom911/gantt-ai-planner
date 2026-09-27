"""Deterministic, stateless fake LLM for demo/dev use without an Anthropic key.

Recognizes a small set of Russian imperative commands via regex and translates them to
MCP tool calls (see the task brief for the exact rule table). Anything else gets a canned
help message. Used when `settings.llm_provider == "fake"` or no API key is configured.
"""

import json
import re
from collections.abc import AsyncIterator
from typing import Any

from app.agent.llm import Completed, LLMEvent, LLMToolCall, LLMTurnResult, TextDelta

HELP = (
    "Я работаю в демо-режиме без LLM. Попробуйте: «Перенеси задачу 3 на 2 дня», "
    "«Сдвинь все задачи Дмитрия на 3 дня», «Назначь задачу 5 на Анну Смирнову», "
    "«Добавь задачу «Ревью» на 2 дня после 4», «Удали задачу 7», «Отмени»."
)
_CASE_ENDINGS = "аяиыуюе"
_CHUNK = 12


def _stem(word: str) -> str:
    lower = word.lower()
    if len(lower) >= 4 and lower[-1] in _CASE_ENDINGS:
        return lower[:-1]
    return lower


# Russian-declension-tolerant stem used to match a typed assignee name (any case: "Наталью
# Белову") against an existing assignee from the plan table (nominative: "Наталья Белова").
_RU_DECLENSION_ENDINGS = "аяуюеиыйоь"
_TABLE_ROW_RE = re.compile(r"^\d+ \| ")
_TABLE_COLUMNS = 10  # see render_plan_table's header: id|задача|исполнитель|...|флаги


def _declension_stem(word: str) -> str:
    lower = word.lower().replace("ё", "е")
    for _ in range(2):
        if lower and lower[-1] in _RU_DECLENSION_ENDINGS:
            lower = lower[:-1]
        else:
            break
    return lower


def _stems_match(a: str, b: str) -> bool:
    sa, sb = _declension_stem(a), _declension_stem(b)
    shorter = min(len(sa), len(sb))
    return shorter >= 3 and (sa.startswith(sb) or sb.startswith(sa))


def _known_assignees(system: list[dict[str, Any]]) -> list[str]:
    """Existing assignees, parsed from the rendered plan table in a `system` block (see
    `render_plan_table`: header "id | задача | исполнитель | ...", one row per task)."""
    names: list[str] = []
    seen: set[str] = set()
    for block in system:
        text = block.get("text") if isinstance(block, dict) else None
        if not isinstance(text, str):
            continue
        for line in text.splitlines():
            if not _TABLE_ROW_RE.match(line):
                continue
            fields = line.split(" | ")
            # render_plan_table sanitizes cells so a row always has exactly this many columns;
            # a row that doesn't match is malformed (or from a differently-shaped source) and
            # must be skipped whole rather than parsed with shifted columns.
            if len(fields) != _TABLE_COLUMNS:
                continue
            assignee = fields[2].strip()
            if assignee and assignee != "—" and assignee not in seen:
                seen.add(assignee)
                names.append(assignee)
    return names


def _resolve_assignee(typed: str, known: list[str]) -> str:
    """Maps a typed assignee name to an existing one when every typed word matches the
    corresponding word of an existing assignee by declension-tolerant stem (see
    `_stems_match`). Falls back to the typed text (kept exactly as typed) when there is no
    match, or more than one candidate matches ambiguously."""
    typed_words = typed.split()
    matches = [
        name
        for name in known
        if len(name.split()) == len(typed_words)
        and all(_stems_match(tw, nw) for tw, nw in zip(typed_words, name.split(), strict=True))
    ]
    if len(matches) == 1:
        return matches[0]
    return typed


_SHIFT_ID_PREFIX = "fake_shift_"


def _shift_tool_id(shift: int, fake_id: str) -> str:
    """Carries a pending bulk-move's day shift out-of-band, in the tool_use id itself,
    instead of a synthetic text block (which would otherwise be streamed to the client
    and persisted as chat content — see the find_tasks step in `_respond_to_text`)."""
    return f"{_SHIFT_ID_PREFIX}{shift}_{fake_id}"


def _shift_from_id(tool_use_id: str) -> int | None:
    if not tool_use_id.startswith(_SHIFT_ID_PREFIX):
        return None
    rest = tool_use_id.removeprefix(_SHIFT_ID_PREFIX)
    shift_str = rest.split("_", 1)[0]
    try:
        return int(shift_str)
    except ValueError:
        return None


def _tool_use(fake_id: str, name: str, input_: dict[str, Any], *, text: str = "") -> LLMTurnResult:
    content: list[dict[str, Any]] = []
    if text:
        content.append({"type": "text", "text": text})
    content.append({"type": "tool_use", "id": fake_id, "name": name, "input": input_})
    call = LLMToolCall(id=fake_id, name=name, input=input_)
    return LLMTurnResult(text=text, tool_calls=[call], stop_reason="tool_use", content=content)


def _final_text(text: str) -> LLMTurnResult:
    return LLMTurnResult(
        text=text, tool_calls=[], stop_reason="end_turn", content=[{"type": "text", "text": text}]
    )


def _parse_found_list(raw: str) -> list[dict[str, Any]] | None:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict) and isinstance(parsed.get("result"), list):
        result: list[dict[str, Any]] = parsed["result"]
        return result
    return None


def _summary_or_head(raw: str) -> str:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("summary"), str):
        summary: str = parsed["summary"]
        return summary
    return raw[:200]


class FakeLLM:
    async def stream(
        self,
        *,
        system: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        max_tokens: int | None = None,  # canned replies are short; nothing to cap
    ) -> AsyncIterator[LLMEvent]:
        result = self._respond(system, messages)
        for i in range(0, len(result.text), _CHUNK):
            yield TextDelta(result.text[i : i + _CHUNK])
        yield Completed(result)

    def _respond(
        self, system: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> LLMTurnResult:
        fake_id = f"fake_{len(messages)}"
        last = messages[-1]
        content = last.get("content")
        if (
            last.get("role") == "user"
            and isinstance(content, list)
            and any(b.get("type") == "tool_result" for b in content)
        ):
            return self._respond_to_tool_result(messages, content, fake_id)
        return self._respond_to_text(content if isinstance(content, str) else "", system, fake_id)

    def _respond_to_tool_result(
        self, messages: list[dict[str, Any]], content: list[dict[str, Any]], fake_id: str
    ) -> LLMTurnResult:
        prev = messages[-2] if len(messages) >= 2 else {}
        prev_content = prev.get("content") if prev.get("role") == "assistant" else None
        shift = None
        if isinstance(prev_content, list):
            shift = next(
                (
                    _shift_from_id(b["id"])
                    for b in prev_content
                    if b.get("type") == "tool_use" and _shift_from_id(b.get("id", "")) is not None
                ),
                None,
            )
        tool_results = [b for b in content if b.get("type") == "tool_result"]
        first = tool_results[0] if tool_results else {}
        if shift is not None:
            items = _parse_found_list(first.get("content", ""))
            if items is not None:
                ops = [{"op": "move_task", "id": item["id"], "shift_days": shift} for item in items]
                return _tool_use(fake_id, "apply_operations", {"operations": ops})
        error_block = next((b for b in tool_results if b.get("is_error")), None)
        if error_block is not None:
            return _final_text(f"Не получилось: {error_block.get('content', '')}")
        return _final_text("Готово. " + _summary_or_head(first.get("content", "")))

    def _respond_to_text(
        self, text: str, system: list[dict[str, Any]], fake_id: str
    ) -> LLMTurnResult:
        lowered = text.lower()

        m = re.search(
            r"(?:перенеси|сдвинь) задачу (\d+) на (-?\d+) (?:раб\w* )?д", lowered, re.IGNORECASE
        )
        if m:
            op = {"op": "move_task", "id": int(m.group(1)), "shift_days": int(m.group(2))}
            return _tool_use(fake_id, "apply_operations", {"operations": [op]})

        m = re.search(
            r"(?:сдвинь|перенеси) все задачи (\S+?) на (-?\d+) (?:раб\w* )?д",
            lowered,
            re.IGNORECASE,
        )
        if m:
            stem = _stem(text[m.start(1) : m.end(1)])
            shift_id = _shift_tool_id(int(m.group(2)), fake_id)
            return _tool_use(shift_id, "find_tasks", {"assignee": stem})

        m = re.search(r"назначь задачу (\d+) на (.+)", lowered, re.IGNORECASE)
        if m:
            typed = text[m.start(2) : m.end(2)].strip().rstrip(".,;:!?")
            assignee = _resolve_assignee(typed, _known_assignees(system))
            op = {"op": "update_task", "id": int(m.group(1)), "assignee": assignee}
            return _tool_use(fake_id, "apply_operations", {"operations": [op]})

        m = re.search(
            r'добавь задачу [«"](.+?)[»"] на (\d+) д\w*(?: после (\d+))?', lowered, re.IGNORECASE
        )
        if m:
            name = text[m.start(1) : m.end(1)]
            add_op: dict[str, Any] = {"op": "add_task", "name": name, "duration": int(m.group(2))}
            if m.group(3):
                add_op["predecessors"] = [{"id": int(m.group(3))}]
            return _tool_use(fake_id, "apply_operations", {"operations": [add_op]})

        m = re.search(r"удали задачу (\d+)", lowered, re.IGNORECASE)
        if m:
            op = {"op": "delete_task", "id": int(m.group(1))}
            return _tool_use(fake_id, "apply_operations", {"operations": [op]})

        if re.search(r"^отмени", lowered, re.IGNORECASE):
            return _tool_use(fake_id, "undo", {})

        return _final_text(HELP)

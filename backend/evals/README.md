# Live-LLM evals

A small set of chat scenarios run against a **deployed** app with a **real LLM**, to catch
regressions in the agent's behaviour (prompt, tools, model changes) that the deterministic
test suite with the fake LLM can't see.

**Not part of pytest.** pytest only collects `tests/`. It runs on a schedule instead: `.github/workflows/evals.yml` (weekly + manual dispatch) runs it against production and opens/closes an issue labelled `evals`. Each run costs ~6 sessions and ~7 chat messages on the real LLM.
Every run costs LLM tokens and uses the target's per-IP limits.

## Run

From `backend/`:

```sh
uv run python -m evals.run --base-url https://gantt-ai-planner.duckdns.org
uv run python -m evals.run --base-url http://localhost:8000 --only mass_delete_confirm -v
```

`--only NAME` (repeatable) runs selected scenarios, `-v` prints each reply and the tools the
agent called. The table at the end shows PASS/FAIL per scenario; the exit code is 1 if any
failed.

## What a scenario does

1. A fresh session over the public HTTP API (`POST /api/session`, cookie), then
   `POST /api/plan/reset` to the demo plan.
2. Chat message(s) via `POST /api/chat`, parsing the SSE stream like the frontend
   (`text_delta`, `tool_started`, `tool_finished`, `plan_changed`, `done`, `error`).
3. Checks on `GET /api/plan` and the reply text.
4. `DELETE /api/session`, pass or fail.

| scenario | message(s) | passes when |
|---|---|---|
| `shift_dmitry` | «Сдвинь все задачи Дмитрия на 3 дня» | every task assigned to «Дмитрий…» starts ≥ 3 working days later |
| `assign_analytics` | «Назначь аналитику на Ольгу» | exactly one task changes assignee: the analytics task, to «Ольга Никитина» |
| `ambiguous_api` | «Перенеси задачу про API на 2 дня» | several tasks match: the plan doesn't change, the reply is a question |
| `mass_delete_confirm` | «Удали задачи 1, 2, 3, 4, 5, 6», then «да» | no change after the first turn (confirmation asked); after «да» exactly tasks 1–6 are gone |
| `add_review` | «Добавь задачу «Ревью безопасности» на 2 дня после 10» | one new task with that name, duration 2, predecessor 10 |
| `plain_text_reply` | «Когда закончится проект и какие задачи на критическом пути?» | the reply has no Markdown `**` and no ISO dates |

## Budget

One full run: 6 new sessions and 7 chat messages. The server allows 20 new sessions per IP
per hour (`SESSION_LIMIT_PER_IP_HOUR`), so don't run it more than about three times an hour
from one address; the chat messages also count toward the app-wide daily quota
(`CHAT_LIMIT_PER_DAY`). The model's wording varies between runs: a failure is worth a look at
`-v` output before blaming the app.

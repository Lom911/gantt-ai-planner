from datetime import date

from app.domain.operations import operations_adapter
from app.domain.seed import build_demo_plan
from app.services.confirmations import batch_digest, deletion_summary


def ops(*raw):
    return operations_adapter.validate_python(list(raw))


def test_digest_is_canonical_over_defaults_and_key_order():
    a = ops({"op": "add_task", "name": "X", "duration": 2}, {"op": "delete_task", "id": 3})
    b = ops(
        {"duration": 2, "predecessors": [], "name": "X", "op": "add_task", "description": ""},
        {"id": 3, "op": "delete_task"},
    )
    assert batch_digest(5, a) == batch_digest(5, b)
    assert len(batch_digest(5, a)) == 64  # sha256 hex


def test_digest_pins_the_batch_its_order_and_the_plan_version():
    batch = ops(*({"op": "delete_task", "id": i} for i in range(1, 7)))
    assert batch_digest(1, batch) != batch_digest(1, batch[:5])
    assert batch_digest(1, batch) != batch_digest(1, list(reversed(batch)))
    assert batch_digest(1, batch) != batch_digest(2, batch)


def test_summary_names_the_deleted_tasks():
    plan = build_demo_plan(date(2026, 9, 25))
    names = {t.id: t.name for t in plan.tasks}
    batch = ops(*({"op": "delete_task", "id": i} for i in range(1, 7)))
    summary, count = deletion_summary(plan, batch)
    assert count == 6
    assert summary.startswith(f"Удалить задачи: №1 «{names[1]}», №2 «{names[2]}»")
    assert summary.endswith("(всего 6)")


def test_summary_of_a_long_list_is_cut_and_mentions_other_operations():
    plan = build_demo_plan(date(2026, 9, 25))
    batch = ops(
        *({"op": "delete_task", "id": i} for i in range(1, 16)),
        {"op": "delete_task", "id": 999},  # not in the plan: not counted
        {"op": "update_task", "id": 20, "duration": 3},
    )
    summary, count = deletion_summary(plan, batch)
    assert count == 15
    assert "№10" in summary and "№11" not in summary and "и ещё 5" in summary
    assert "(всего 15)" in summary and "прочих операций в пакете: 2" in summary

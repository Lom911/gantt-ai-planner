from app.agent.prompt import STATIC_SYSTEM_PROMPT, build_system


def test_both_system_blocks_are_cache_breakpoints():
    # The plan table stays the same for every LLM call of a turn (up to 15 iterations), so it
    # must be cached too, not only the static rules; tools add the third breakpoint (max 4).
    blocks = build_system("id | задача\n1 | Анализ")
    assert blocks[0]["text"] == STATIC_SYSTEM_PROMPT
    assert blocks[1]["text"].endswith("1 | Анализ")
    assert all(b.get("cache_control") == {"type": "ephemeral"} for b in blocks)


def test_replies_are_plain_text_with_russian_dates():
    # The chat renders text as-is: Markdown (**bold**) showed up as literal asterisks, and the
    # model echoed ISO dates (2026-11-20) instead of the app's DD.MM.YYYY.
    assert "без Markdown" in STATIC_SYSTEM_PROMPT
    assert "ДД.ММ.ГГГГ" in STATIC_SYSTEM_PROMPT


def test_mass_delete_is_confirmed_by_an_exact_yes():
    # app.agent.confirmation accepts only «да»/«подтверждаю»: the prompt must ask for exactly that.
    assert "ответить «да»" in STATIC_SYSTEM_PROMPT
    assert "confirmed=true" in STATIC_SYSTEM_PROMPT

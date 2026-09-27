import pytest

from app.agent.confirmation import is_explicit_confirmation


@pytest.mark.parametrize(
    "text",
    [
        "да",
        "Да",
        "ДА",
        "Да.",
        "да!",
        "Да!!!",
        "  да  ",
        "\nда\n",
        "Подтверждаю",
        "подтверждаю.",
        "ПОДТВЕРЖДАЮ!",
        "да 👍",
        "Да!👍",
        "да👍🏻",
        "подтверждаю ✅",
        "да ❤️",
    ],
)
def test_exact_confirmations(text):
    assert is_explicit_confirmation(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "👍",
        "ок",
        "OK",
        "окей",
        "можно",
        "удали",
        "удаляй",
        "Удалить",
        "согласен",
        "yes",
        "конечно",
        "да, удаляй",
        "Да, удаляй все",
        "да, но сначала покажи план",
        "да, но…",
        "подтверждаю удаление",
        "да да",
        "да?",
        "Да?!",
        "да нет, не надо",
        "нет",
        "не удаляй",
        "Удали задачи 1, 2, 3, 4, 5, 6",
        "задача",  # «да» inside another word
        "дата",
        "«да»",
        "да,",
        "дa",  # Latin «a»
        "👍 да",
    ],
)
def test_anything_but_an_exact_reply_does_not_confirm(text):
    assert not is_explicit_confirmation(text)

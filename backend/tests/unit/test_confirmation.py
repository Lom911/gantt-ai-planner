import pytest

from app.agent.confirmation import is_explicit_confirmation


@pytest.mark.parametrize(
    "text",
    [
        "да",
        "Да.",
        "ДА!",
        "да, удаляй",
        "Да, удаляй все",
        "Подтверждаю",
        "подтверждаю удаление",
        "удаляй",
        "Удалить",
        "согласен",
        "Согласна!",
        "ок",
        "Окей, давай",
        "ага",
        "конечно, удаляйте их",
        "yes",
        "Yes, delete them",
        "OK",
        "confirm",
        "да, всё верно",
        "  да  ",
    ],
)
def test_short_affirmative_replies_confirm(text):
    assert is_explicit_confirmation(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Удали задачи 1, 2, 3, 4, 5, 6",
        "Удали 1, 2, 3, 4, 5, 6",
        "удали все задачи",
        "Сдвинь задачу 3 на 2 дня",
        "задача",  # «да» inside another word
        "дата",
        "нет",
        "да нет, не надо",
        "не удаляй",
        "да?",
        "точно?",
        "давай подумаем",
        "да, но сначала покажи план",
        "👍",
        "да " * 10,  # not a short reply any more
    ],
)
def test_requests_questions_and_negations_do_not_confirm(text):
    assert not is_explicit_confirmation(text)

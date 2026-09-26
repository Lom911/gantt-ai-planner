"""Is the user's chat message an explicit confirmation? (security audit L3)

The server refuses a mass deletion until `apply_operations` is repeated with
`confirmed=true`, but the flag is set by the model — and text it reads (a task description,
an imported file) could talk it into setting the flag on its own. The agent loop therefore
lets the flag through only when the user's message of the current turn is itself a short
affirmative reply: «да», «подтверждаю», «удаляй», «ок», «yes»... A request («удали задачи
1–6»), a question («да?») or a negation («да нет, не надо») never counts.
"""

import re

_AFFIRMATIVE = (
    "да",
    "ага",
    "угу",
    "ок",
    "окей",
    "ладно",
    "хорошо",
    "давай",
    "давайте",
    "конечно",
    "верно",
    "точно",
    "согласен",
    "согласна",
    "согласны",
    "подтверждаю",
    "подтверждаем",
    "подтверждено",
    "удаляй",
    "удаляйте",
    "удали",
    "удалите",
    "удалить",
    "можно",
    "yes",
    "yep",
    "yeah",
    "ok",
    "okay",
    "sure",
    "confirm",
    "confirmed",
    "delete",
)
# Words that may accompany an affirmative without changing its meaning («да, удаляй их все»).
_FILLER = (
    "все",
    "их",
    "это",
    "эти",
    "пожалуйста",
    "так",
    "уверен",
    "уверена",
    "удаление",
    "please",
    "all",
    "them",
    "it",
    "go",
    "ahead",
    "do",
)
_MAX_WORDS = 6

_A = "|".join(_AFFIRMATIVE)
_ANY = "|".join(_AFFIRMATIVE + _FILLER)
# Whole message: only affirmatives and fillers, at least one affirmative.
_CONFIRMATION = re.compile(rf"(?:(?:{_ANY}) )*(?:{_A})(?: (?:{_ANY}))*")


def _normalize(text: str) -> str:
    """Lower case, ё → е, words (letters and digits) separated by single spaces."""
    return " ".join(re.findall(r"\w+", text.casefold().replace("ё", "е")))


def is_explicit_confirmation(text: str) -> bool:
    if "?" in text:
        return False
    normalized = _normalize(text)
    if len(normalized.split()) > _MAX_WORDS:
        return False
    return _CONFIRMATION.fullmatch(normalized) is not None

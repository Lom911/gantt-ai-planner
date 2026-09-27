"""Is the user's chat message an explicit confirmation? (security audit L3)

The server refuses a mass deletion until `apply_operations` is repeated with
`confirmed=true`, but the flag is set by the model — and text it reads (a task description,
an imported file) could talk it into setting the flag on its own. The agent loop therefore
lets the flag through only when the user's message of the current turn is exactly «да» or
«подтверждаю» — any case, surrounding whitespace, trailing «.»/«!» and emoji allowed —
which is the reply the assistant asks for. Anything else («ок», «можно», «удали», «да, но…»,
a question) is not a confirmation: loose affirmatives were too easy to produce by accident,
or to read into a reply meant for a different question.
"""

import unicodedata

_CONFIRMATIONS = frozenset({"да", "подтверждаю"})
# Emoji building blocks that may trail a reply: zero-width joiner, variation selectors,
# skin-tone modifiers (category Sk, but so are ^ and `, hence the explicit range).
_EMOJI_JOINERS = frozenset({"‍", "︎", "️"})
_SKIN_TONES = range(0x1F3FB, 0x1F400)


def _is_trailer(ch: str) -> bool:
    return (
        ch in ".!"
        or ch.isspace()
        or ch in _EMOJI_JOINERS
        or ord(ch) in _SKIN_TONES
        or unicodedata.category(ch) == "So"  # emoji and other pictographs
    )


def is_explicit_confirmation(text: str) -> bool:
    body = text.strip()
    end = len(body)
    while end > 0 and _is_trailer(body[end - 1]):
        end -= 1
    return body[:end].casefold() in _CONFIRMATIONS

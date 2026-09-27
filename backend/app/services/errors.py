from app.domain.errors import DomainError


class AgentBusy(DomainError):
    code = "agent_busy"

    def __init__(self) -> None:
        super().__init__("Агент сейчас редактирует план, подождите")


class RateLimited(DomainError):
    code = "rate_limited"


class Unauthorized(DomainError):
    code = "unauthorized"


class NoSession(DomainError):
    code = "no_session"

    def __init__(self) -> None:
        super().__init__("Сессия не найдена")


class VersionConflict(DomainError):
    code = "version_conflict"

    def __init__(self, expected: int, current: int) -> None:
        super().__init__(
            "План изменился с момента загрузки: обновите его и повторите изменение",
            details={"expected_version": expected, "current_version": current},
        )


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


class PlanTooLarge(DomainError):
    code = "plan_too_large"

    def __init__(self, limit_bytes: int) -> None:
        super().__init__(
            "План получился слишком большим: сократите описания задач или их число",
            details={"limit_bytes": limit_bytes},
        )

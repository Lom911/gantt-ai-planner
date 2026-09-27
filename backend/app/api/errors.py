from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.domain.errors import DomainError

STATUS = {
    "invalid_plan": 422,
    "invalid_operation": 422,
    "cycle": 422,
    "confirmation_required": 409,
    "agent_busy": 409,
    "version_conflict": 409,
    "nothing_to_undo": 409,
    "nothing_to_redo": 409,
    "rate_limited": 429,
    "no_session": 401,
    "unauthorized": 401,
    "not_found": 404,
    "bad_origin": 403,
    "file_too_large": 413,
    "plan_too_large": 413,
}


def error_response(
    code: str, message: str, details: object = None, status: int | None = None
) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, "details": details}},
        status_code=status or STATUS.get(code, 400),
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain(_: Request, exc: DomainError) -> JSONResponse:
        return error_response(exc.code, exc.message, exc.details or None)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = jsonable_encoder(exc.errors())
        return error_response("validation_error", "Некорректный запрос", details, 422)

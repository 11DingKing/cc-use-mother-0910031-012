"""业务错误类型，API 层据此映射 HTTP 状态码。"""
from __future__ import annotations


class AppError(Exception):
    """所有可预期业务错误的基类。"""

    status = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message}


class NotFoundError(AppError):
    status = 404
    code = "not_found"


class ValidationError(AppError):
    status = 422
    code = "validation_error"


class StateConflictError(AppError):
    status = 409
    code = "state_conflict"


class RecusalError(AppError):
    """复核人员存在回避关系。"""

    status = 403
    code = "recusal_conflict"


class ScoringError(AppError):
    status = 422
    code = "scoring_error"

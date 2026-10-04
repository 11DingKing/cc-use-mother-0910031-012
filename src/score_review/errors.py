"""领域错误类型。"""
from __future__ import annotations


class DomainError(Exception):
    """业务规则错误，携带 HTTP 状态码与稳定错误码。"""

    status = 400
    code = "domain_error"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


class NotFoundError(DomainError):
    status = 404
    code = "not_found"


class ConflictError(DomainError):
    status = 409
    code = "conflict"


class StateError(ConflictError):
    """实体当前状态不允许该操作。"""

    code = "invalid_state"


class RecusalError(ConflictError):
    """复核人存在回避情形。"""

    code = "recusal_conflict"


class ValidationError(DomainError):
    status = 422
    code = "validation"

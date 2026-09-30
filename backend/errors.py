"""Structured application errors with safe, user-facing messages."""

from typing import Optional


class AppError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        provider: Optional[str] = None,
        http_status: int = 400,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.provider = provider
        self.http_status = http_status

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "provider": self.provider,
        }


def not_found(what: str) -> AppError:
    return AppError("not_found", f"{what} not found.", http_status=404)

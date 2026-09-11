"""网关统一错误模型与错误码（对应 SPEC §5.5）。"""

from __future__ import annotations

from typing import Any, Optional


class GatewayError(Exception):
    """网关统一业务异常。"""

    code: str = "internal_error"
    http_status: int = 500
    error_type: str = "internal_error"

    def __init__(
        self,
        message: str,
        code: Optional[str] = None,
        http_status: Optional[int] = None,
        error_type: Optional[str] = None,
        retry_after: Optional[float] = None,
        details: Optional[Any] = None,
        trace_id: Optional[str] = None,
    ):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        if error_type:
            self.error_type = error_type
        self.retry_after = retry_after
        self.details = details
        self.trace_id = trace_id

    def to_response(self) -> dict:
        body: dict = {
            "error": {
                "code": self.code,
                "message": self.message,
                "type": self.error_type,
                "trace_id": self.trace_id or "",
            }
        }
        if self.retry_after is not None:
            body["error"]["retry_after"] = self.retry_after
        return body


class InvalidAuthError(GatewayError):
    code = "invalid_auth"
    http_status = 401
    error_type = "auth_error"


class ForbiddenError(GatewayError):
    code = "forbidden"
    http_status = 403
    error_type = "auth_error"


class NotFoundError(GatewayError):
    code = "not_found"
    http_status = 404
    error_type = "not_found"


class InvalidRequestError(GatewayError):
    code = "invalid_request"
    http_status = 400
    error_type = "invalid_request"


class RateLimitExceededError(GatewayError):
    code = "rate_limit_exceeded"
    http_status = 429
    error_type = "rate_limit"

    def __init__(self, message: str, retry_after: float = 1.0, **kw):
        super().__init__(message, retry_after=retry_after, **kw)


class BudgetExhaustedError(GatewayError):
    code = "budget_exhausted"
    http_status = 402
    error_type = "budget"


class PromptMissingVarsError(GatewayError):
    code = "prompt_missing_vars"
    http_status = 400
    error_type = "invalid_request"


class UpstreamError(GatewayError):
    code = "upstream_error"
    http_status = 502
    error_type = "upstream"


class UpstreamTimeoutError(GatewayError):
    code = "upstream_timeout"
    http_status = 504
    error_type = "upstream"


class ModelUnavailableError(GatewayError):
    code = "model_unavailable"
    http_status = 503
    error_type = "unavailable"


class PromptInjectionDetectedError(GatewayError):
    code = "prompt_injection_detected"
    http_status = 400
    error_type = "security"


class StructuredOutputError(GatewayError):
    code = "structured_output_failed"
    http_status = 502
    error_type = "upstream"

"""Closed local-code presentation boundary for operation failures."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .messages import OperationError, OperationErrorCode


_MESSAGE_ID_RE = re.compile(r"op\.[a-z_]{1,63}\Z")


_CATALOGUE: dict[OperationErrorCode, tuple[str, str]] = {
    OperationErrorCode.UNAVAILABLE: (
        "op.unavailable",
        "This operation is unavailable in the current installation.",
    ),
    OperationErrorCode.INVALID_REQUEST: (
        "op.invalid_request",
        "The operation request was invalid.",
    ),
    OperationErrorCode.DEADLINE: (
        "op.deadline",
        "The operation timed out. Try again.",
    ),
    OperationErrorCode.CANCELLED: (
        "op.cancelled",
        "The operation was cancelled.",
    ),
    OperationErrorCode.PROVIDER_FAILURE: (
        "op.provider_failure",
        "The installed operation provider could not complete the request.",
    ),
    OperationErrorCode.PROTOCOL_ERROR: (
        "op.protocol_error",
        "The operation provider returned an unsupported response.",
    ),
    OperationErrorCode.OUTPUT_INVALID: (
        "op.output_invalid",
        "The operation output failed verification.",
    ),
    OperationErrorCode.INTERNAL: (
        "op.internal",
        "The operation failed internally.",
    ),
}
UNKNOWN_MESSAGE = (
    "op.protocol_error",
    "The operation provider returned an unsupported response.",
)


@dataclass(frozen=True, slots=True)
class LocalDiagnostic:
    message_id: str
    text: str
    retryable: bool
    diagnostic_ref: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.message_id, str) or _MESSAGE_ID_RE.fullmatch(
            self.message_id
        ) is None:
            raise ValueError("local operation message ID is not canonical")
        if (
            not isinstance(self.text, str)
            or not self.text
            or len(self.text) > 256
            or any(ord(item) < 0x20 or ord(item) == 0x7F for item in self.text)
        ):
            raise ValueError("local operation diagnostic text is unsafe")
        if not isinstance(self.retryable, bool):
            raise ValueError("local operation retryability must be boolean")
        if self.diagnostic_ref is not None and not isinstance(
            self.diagnostic_ref, str
        ):
            raise ValueError("local operation diagnostic reference is malformed")


def render_operation_error(value: object) -> LocalDiagnostic:
    """Render only fixed local text; unknown/provider prose has no output channel."""

    if not isinstance(value, OperationError):
        return LocalDiagnostic(UNKNOWN_MESSAGE[0], UNKNOWN_MESSAGE[1], False, None)
    message = _CATALOGUE.get(value.code, UNKNOWN_MESSAGE)
    return LocalDiagnostic(
        message[0],
        message[1],
        value.retryable,
        value.diagnostic_ref,
    )


def diagnostic_catalogue() -> tuple[tuple[OperationErrorCode, str, str], ...]:
    return tuple(
        (code, message_id, text)
        for code, (message_id, text) in sorted(
            _CATALOGUE.items(),
            key=lambda item: item[0].value,
        )
    )


__all__ = (
    "LocalDiagnostic",
    "diagnostic_catalogue",
    "render_operation_error",
)

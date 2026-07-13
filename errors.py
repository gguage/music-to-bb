from __future__ import annotations

from enum import IntEnum


class ErrorCategory(IntEnum):
    INVALID_INPUT = 1
    AUTHENTICATION = 2
    EXTRACTION = 3
    NO_MATCHES = 4
    PARTIAL_WRITE = 5
    WRITE_FAILED = 6
    BROWSER_REQUIRED = 7
    NETWORK = 8
    CANCELLED = 9
    INTERNAL = 10


class ExitCode(IntEnum):
    SUCCESS = 0
    INTERNAL = 1
    INVALID_INPUT = 2
    AUTHENTICATION = 3
    EXTRACTION = 4
    NO_MATCHES = 5
    PARTIAL_WRITE = 6
    WRITE_FAILURE = 7
    CANCELLED = 130


class Music2bbError(Exception):
    def __init__(self, category: ErrorCategory, operation: str, message: str):
        super().__init__(message)
        self.category = category
        self.operation = operation
        self.message = message


class BatchError(Music2bbError):
    def __init__(self, operation: str, message: str, failures: list[ItemFailure] | None = None):
        super().__init__(ErrorCategory.PARTIAL_WRITE, operation, message)
        self.failures = failures or []


class ItemFailure:
    def __init__(self, bvid: str, reason: str):
        self.bvid = bvid
        self.reason = reason


def exit_for(error: Exception | None) -> int:
    if error is None:
        return ExitCode.SUCCESS
    if isinstance(error, Music2bbError):
        category = error.category
    else:
        category = ErrorCategory.INTERNAL
    mapping = {
        ErrorCategory.INVALID_INPUT: ExitCode.INVALID_INPUT,
        ErrorCategory.AUTHENTICATION: ExitCode.AUTHENTICATION,
        ErrorCategory.EXTRACTION: ExitCode.EXTRACTION,
        ErrorCategory.BROWSER_REQUIRED: ExitCode.EXTRACTION,
        ErrorCategory.NETWORK: ExitCode.EXTRACTION,
        ErrorCategory.NO_MATCHES: ExitCode.NO_MATCHES,
        ErrorCategory.PARTIAL_WRITE: ExitCode.PARTIAL_WRITE,
        ErrorCategory.WRITE_FAILED: ExitCode.WRITE_FAILURE,
        ErrorCategory.CANCELLED: ExitCode.CANCELLED,
    }
    return mapping.get(category, ExitCode.INTERNAL)

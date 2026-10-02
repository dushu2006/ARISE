"""Structured, redaction-aware error taxonomy for API, tasks, and telemetry."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ErrorCategory(StrEnum):
    CONFIGURATION = "configuration"
    VALIDATION = "validation"
    MODEL = "model"
    PROVIDER = "provider"
    TIMEOUT = "timeout"
    CANCELLATION = "cancellation"
    EXECUTION = "execution"
    VERIFICATION = "verification"
    POLICY = "policy"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    CAPABILITY = "capability"
    DATABASE = "database"
    PROTOCOL = "protocol"
    ENVIRONMENT = "environment"
    RESOURCE = "resource"
    INTERNAL = "internal"


class ErrorSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    error_code: str
    category: ErrorCategory
    message: str
    retryable: bool
    severity: ErrorSeverity
    component: str
    operation: str | None = None
    correlation_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_code": self.error_code,
            "category": self.category.value,
            "message": self.message,
            "retryable": self.retryable,
            "severity": self.severity.value,
            "component": self.component,
            "operation": self.operation,
            "correlation_id": self.correlation_id,
            "metadata": dict(self.metadata),
        }


class AriseError(Exception):
    """Base exception carrying safe, structured public diagnostics."""

    default_category = ErrorCategory.INTERNAL
    default_retryable = False
    default_severity = ErrorSeverity.ERROR
    default_code = "ARISE_INTERNAL_ERROR"

    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        component: str = "core",
        operation: str | None = None,
        retryable: bool | None = None,
        severity: ErrorSeverity | None = None,
        correlation_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.info = ErrorInfo(
            error_code=error_code or self.default_code,
            category=self.default_category,
            message=message,
            retryable=self.default_retryable if retryable is None else retryable,
            severity=severity or self.default_severity,
            component=component,
            operation=operation,
            correlation_id=correlation_id,
            metadata=metadata or {},
        )


class ConfigurationError(AriseError):
    default_category = ErrorCategory.CONFIGURATION
    default_code = "ARISE_CONFIGURATION_ERROR"


class ValidationError(AriseError):
    default_category = ErrorCategory.VALIDATION
    default_code = "ARISE_VALIDATION_ERROR"


class ModelError(AriseError):
    default_category = ErrorCategory.MODEL
    default_code = "ARISE_MODEL_ERROR"


class ProviderUnavailableError(AriseError):
    default_category = ErrorCategory.PROVIDER
    default_retryable = True
    default_code = "ARISE_PROVIDER_UNAVAILABLE"


class ExecutionError(AriseError):
    default_category = ErrorCategory.EXECUTION
    default_code = "ARISE_EXECUTION_ERROR"


class VerificationError(AriseError):
    default_category = ErrorCategory.VERIFICATION
    default_code = "ARISE_VERIFICATION_ERROR"


class PolicyDeniedError(AriseError):
    default_category = ErrorCategory.POLICY
    default_code = "ARISE_POLICY_DENIED"


class AuthenticationError(AriseError):
    default_category = ErrorCategory.AUTHENTICATION
    default_code = "ARISE_AUTHENTICATION_REQUIRED"
    default_severity = ErrorSeverity.WARNING


class CapabilityUnavailableError(AriseError):
    default_category = ErrorCategory.CAPABILITY
    default_code = "ARISE_CAPABILITY_UNAVAILABLE"
    default_severity = ErrorSeverity.WARNING


class DatabaseError(AriseError):
    default_category = ErrorCategory.DATABASE
    default_code = "ARISE_DATABASE_ERROR"


class ProtocolError(AriseError):
    default_category = ErrorCategory.PROTOCOL
    default_code = "ARISE_PROTOCOL_ERROR"
    default_severity = ErrorSeverity.WARNING


class EnvironmentError(AriseError):
    default_category = ErrorCategory.ENVIRONMENT
    default_code = "ARISE_ENVIRONMENT_ERROR"


class ResourceError(AriseError):
    default_category = ErrorCategory.RESOURCE
    default_code = "ARISE_RESOURCE_ERROR"
    default_retryable = True


class RetryableOperationError(AriseError):
    """An error deliberately classified as safe to retry by the caller."""

    default_retryable = True


def classify_exception(
    exc: BaseException,
    *,
    component: str = "runtime",
    operation: str | None = None,
    correlation_id: str | None = None,
) -> ErrorInfo:
    """Map failures to safe metadata without copying arbitrary exception text."""

    if isinstance(exc, AriseError):
        info = exc.info
        return ErrorInfo(
            error_code=info.error_code,
            category=info.category,
            message=info.message,
            retryable=info.retryable,
            severity=info.severity,
            component=info.component or component,
            operation=info.operation or operation,
            correlation_id=info.correlation_id or correlation_id,
            metadata=info.metadata,
        )
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return ErrorInfo(
            "ARISE_TIMEOUT",
            ErrorCategory.TIMEOUT,
            "The operation exceeded its deadline.",
            True,
            ErrorSeverity.WARNING,
            component,
            operation,
            correlation_id,
        )
    if isinstance(exc, asyncio.CancelledError):
        return ErrorInfo(
            "ARISE_CANCELLED",
            ErrorCategory.CANCELLATION,
            "The operation was cancelled.",
            False,
            ErrorSeverity.INFO,
            component,
            operation,
            correlation_id,
        )
    if isinstance(exc, (ValueError, TypeError)):
        return ErrorInfo(
            "ARISE_VALIDATION_ERROR",
            ErrorCategory.VALIDATION,
            "The operation received invalid input.",
            False,
            ErrorSeverity.WARNING,
            component,
            operation,
            correlation_id,
        )
    return ErrorInfo(
        "ARISE_INTERNAL_ERROR",
        ErrorCategory.INTERNAL,
        "An unexpected internal error occurred.",
        False,
        ErrorSeverity.ERROR,
        component,
        operation,
        correlation_id,
        {"exception_type": type(exc).__name__},
    )

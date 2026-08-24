"""Stable user-facing error types for RouteCraft Memory Local."""

from __future__ import annotations


class RouteCraftLocalError(RuntimeError):
    """Expected error that should be shown without a traceback."""

    exit_code = 2


class NotFoundError(RouteCraftLocalError):
    """A requested project, memory, or artifact was not found."""

    exit_code = 3


class ConfirmationRequiredError(RouteCraftLocalError):
    """A destructive or replacement action lacked exact confirmation."""

    exit_code = 4


class ConflictError(RouteCraftLocalError):
    """An import or update conflicts with existing durable state."""

    exit_code = 5


class IntegrityError(RouteCraftLocalError):
    """A database, backup, or package failed structural validation."""

    exit_code = 6


class SecurityError(RouteCraftLocalError):
    """Unsafe input cannot be handled without widening the security boundary."""

    exit_code = 7


__all__ = [
    "ConflictError",
    "ConfirmationRequiredError",
    "IntegrityError",
    "NotFoundError",
    "RouteCraftLocalError",
    "SecurityError",
]

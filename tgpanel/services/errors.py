"""Service-layer errors (messages are Russian and secret-free)."""

from __future__ import annotations

from tgpanel.apply.errors import OperationRejected


class UserServiceError(OperationRejected):
    """Validation / lookup failure in a DB-only call (raised to the caller)."""

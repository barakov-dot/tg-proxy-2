"""Exceptions of the apply layer. Messages never contain secrets."""

from __future__ import annotations


class OperationRejected(Exception):
    """Business-level refusal raised by a mutation. The message is shown to the user (Russian)."""


class ApplyError(Exception):
    """A pipeline step failed. ``stage`` is a short machine name, ``detail`` is secret-free."""

    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(f"{stage}: {detail}")
        self.stage = stage
        self.detail = detail


class ExternalChangeDetected(Exception):
    """profiles.json was changed behind our back (or has foreign profiles and no baseline)."""

    def __init__(self, description: str, *, no_baseline: bool = False) -> None:
        super().__init__(description)
        self.description = description
        self.no_baseline = no_baseline


class SettingsError(ValueError):
    """Invalid setting value (message in Russian, secret-free)."""

"""Errors raised by the pure render layer."""

from __future__ import annotations


class RenderError(ValueError):
    """Invalid input for rendering or an unparsable existing file."""

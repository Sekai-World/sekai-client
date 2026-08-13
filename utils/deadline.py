"""Monotonic request deadlines shared by RPC, queue, and HTTP work."""

from __future__ import annotations

import math
from contextvars import ContextVar, Token
from dataclasses import dataclass
from time import monotonic


class DeadlineExceeded(RuntimeError):
    """Raised when an operation has exhausted its request budget."""


@dataclass(frozen=True)
class Deadline:
    expires_at: float

    @classmethod
    def after(cls, seconds: float) -> Deadline:
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
            raise ValueError("deadline seconds must be a number")
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("deadline seconds must be finite and positive")
        return cls(monotonic() + float(seconds))

    def remaining(self) -> float:
        return max(0.0, self.expires_at - monotonic())

    def require_remaining(self) -> float:
        remaining = self.remaining()
        if remaining <= 0:
            raise DeadlineExceeded("Request deadline exceeded")
        return remaining


_current_deadline: ContextVar[Deadline | None] = ContextVar(
    "current_request_deadline", default=None
)


def current_deadline() -> Deadline | None:
    return _current_deadline.get()


def set_current_deadline(deadline: Deadline | None) -> Token[Deadline | None]:
    return _current_deadline.set(deadline)


def reset_current_deadline(token: Token[Deadline | None]) -> None:
    _current_deadline.reset(token)


def bounded_timeout(configured_timeout: float) -> float:
    """Return the smaller of the configured timeout and remaining budget."""
    deadline = current_deadline()
    if deadline is None:
        return configured_timeout
    return min(configured_timeout, deadline.require_remaining())

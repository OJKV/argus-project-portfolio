"""Span helper that works identically with and without Logfire configured.

Call sites read the same in both cases, so the offline test suite exercises the real code path
instead of a branch that only exists in production.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from argus.observability import setup


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[None]:
    if not setup.enabled():
        yield
        return
    import logfire

    with logfire.span(name, **attributes):
        yield

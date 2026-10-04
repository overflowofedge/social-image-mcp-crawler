from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any


ProgressCallback = Callable[[dict[str, Any]], None]
_callback: ContextVar[ProgressCallback | None] = ContextVar("social_image_progress", default=None)


@contextmanager
def progress_context(callback: ProgressCallback | None) -> Iterator[None]:
    """Attach progress reporting to one async request without sharing task state."""
    token = _callback.set(callback)
    try:
        yield
    finally:
        _callback.reset(token)


def report_progress(stage: str, message: str, **values: Any) -> None:
    callback = _callback.get()
    if callback is None:
        return
    payload = {"stage": stage, "message": message, **values}
    try:
        callback(payload)
    except Exception:
        # Progress is observational and must never abort collection.
        return

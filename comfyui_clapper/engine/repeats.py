"""Refuse accidental repeats (double click, phone double tap, a retried script) for a few seconds. Pure Python."""
from __future__ import annotations

import json
import time
from typing import Callable, Hashable

from .project import ProjectError

WINDOW = 5.0  # seconds


class Repeated(ProjectError):
    """The same action was done a moment ago."""


class RepeatGuard:
    """Remembers actions for `window` seconds after they were done.

    Callers check a key before acting and record it only once the action succeeded, both under the
    same lock: a second identical request waiting on that lock is then refused, one after a failure is not.
    """

    def __init__(self, window: float = WINDOW, clock: Callable[[], float] = time.monotonic):
        self.window, self.clock = window, clock
        self._done: dict[Hashable, float] = {}

    def recent(self, key: Hashable) -> bool:
        now = self.clock()
        self._done = {k: t for k, t in self._done.items() if now - t < self.window}
        return key in self._done

    def check(self, key: Hashable, message: str) -> None:
        if self.recent(key):
            raise Repeated(message)

    def done(self, key: Hashable) -> None:
        self._done[key] = self.clock()


def render_key(project_root, scene_id: str, *, seed=None, overrides=None, parent_take=None) -> tuple:
    """Same scene with the same seed (or none), overrides and parent take = the same render."""
    params = json.dumps({"seed": seed, "overrides": overrides or None, "parent": parent_take or None}, sort_keys=True)
    return ("render", str(project_root), str(scene_id), params)


def requeue_key(project_root, take_id: str) -> tuple:
    return ("requeue", str(project_root), str(take_id))


__all__ = ["WINDOW", "Repeated", "RepeatGuard", "render_key", "requeue_key"]

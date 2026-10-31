import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from comfyui_clapper.engine.project import ProjectError  # noqa: E402
from comfyui_clapper.engine.repeats import Repeated, RepeatGuard, render_key, requeue_key  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def guard():
    return RepeatGuard(window=5.0, clock=Clock())


def test_repeat_inside_window_is_refused(guard):
    key = render_key("/p", "s1")
    guard.check(key, "again")  # nothing recorded yet
    guard.done(key)
    guard.clock.t += 1.5
    with pytest.raises(Repeated, match="again"):
        guard.check(key, "again")


def test_repeat_after_window_is_allowed(guard):
    key = render_key("/p", "s1", seed=7)
    guard.done(key)
    guard.clock.t += 5.0
    guard.check(key, "again")
    assert not guard.recent(key)


def test_failed_action_is_not_recorded(guard):
    # callers record only after success, so an immediate retry after an error goes through
    key = requeue_key("/p", "t1")
    guard.check(key, "again")
    guard.check(key, "again")


def test_render_key_separates_parameters():
    base = render_key("/p", "s1")
    assert base == render_key("/p", "s1", seed=None, overrides={}, parent_take="")
    assert base != render_key("/p", "s1", seed=42)
    assert render_key("/p", "s1", seed=42) != render_key("/p", "s1", seed=43)
    assert base != render_key("/p", "s1", overrides={"sampling": {"steps": 8}})
    assert base != render_key("/p", "s1", parent_take="t9")
    assert base != render_key("/p", "s2")
    assert base != render_key("/other", "s1")
    assert render_key("/p", "s1", overrides={"a": 1, "b": 2}) == render_key("/p", "s1", overrides={"b": 2, "a": 1})


def test_other_keys_are_independent(guard):
    guard.done(render_key("/p", "s1"))
    guard.check(render_key("/p", "s2"), "again")
    guard.check(requeue_key("/p", "t1"), "again")


def test_repeated_is_a_project_error():
    assert issubclass(Repeated, ProjectError)

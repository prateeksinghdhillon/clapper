import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from comfyui_clapper.engine import profiles  # noqa: E402
from comfyui_clapper.engine.profiles import InUse, ProfileError, Store  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    s = Store(tmp_path / "clapper", clock=clock)
    s.add("alpha", "alpha123", name="Alpha")
    s.add("beta", "beta123", name="Beta")
    return s


def test_passwords_are_hashed_and_checked(store, tmp_path):
    text = (tmp_path / "clapper" / "profiles.json").read_text()
    assert "alpha123" not in text and "scrypt$" in text
    with pytest.raises(ProfileError, match="wrong profile id or password"):
        store.login("alpha", "nope")
    with pytest.raises(ProfileError, match="wrong profile id or password"):
        store.login("nobody", "alpha123")
    assert store.session(store.login("alpha", "alpha123")) == "alpha"


def test_profile_rules(store):
    with pytest.raises(ProfileError, match="already exists"):
        store.add("alpha", "whatever")
    with pytest.raises(ProfileError, match="lowercase"):
        store.add("Bad Id", "whatever")
    with pytest.raises(ProfileError, match="at least"):
        store.add("gamma", "abc")
    with pytest.raises(ProfileError, match="already exist"):
        store.add("gamma", "gamma123", only_if_empty=True)


def test_only_one_profile_at_a_time(store, clock):
    laptop = store.login("alpha", "alpha123")
    phone = store.login("alpha", "alpha123")  # same profile, another device: allowed
    assert store.session(laptop) == store.session(phone) == "alpha"
    with pytest.raises(InUse) as err:
        store.login("beta", "beta123")
    assert err.value.holder == "alpha" and "Alpha" in str(err.value)
    # still active while its pages keep polling
    clock.t += profiles.IDLE_MINUTES * 60 - 30
    assert store.session(phone) == "alpha"
    clock.t += profiles.IDLE_MINUTES * 60 - 30
    with pytest.raises(InUse):
        store.login("beta", "beta123")


def test_idle_profile_releases_and_is_signed_out(store, clock):
    token = store.login("alpha", "alpha123")
    clock.t += profiles.IDLE_MINUTES * 60 + 1
    assert store.active()["holds"] is False
    other = store.login("beta", "beta123")
    assert store.session(token) is None  # alpha's idle sessions ended
    assert store.session(other) == "beta"


def test_idle_profile_resumes_when_nobody_else_logged_in(store, clock):
    token = store.login("alpha", "alpha123")
    clock.t += profiles.IDLE_MINUTES * 60 + 1
    assert store.session(token) == "alpha"
    assert store.active()["holds"] is True


def test_logout_frees_clapper_on_every_device(store):
    laptop = store.login("alpha", "alpha123")
    phone = store.login("alpha", "alpha123")
    assert store.logout(laptop) == 2
    assert store.session(phone) is None
    assert store.session(store.login("beta", "beta123")) == "beta"


def test_switching_profile_on_the_only_device(store):
    token = store.login("alpha", "alpha123")
    assert store.session(store.login("beta", "beta123", replace=token)) == "beta"
    assert store.session(token) is None


def test_changing_a_password_logs_out_other_devices(store):
    laptop = store.login("alpha", "alpha123")
    phone = store.login("alpha", "alpha123")
    with pytest.raises(ProfileError, match="current password is wrong"):
        store.change_password(laptop, "nope", "newpass1")
    assert store.change_password(laptop, "alpha123", "newpass1") == 1
    assert store.session(laptop) == "alpha" and store.session(phone) is None
    with pytest.raises(ProfileError):
        store.login("alpha", "alpha123")
    assert store.set_password("alpha", "another1") == 1  # machine owner (CLI): every session ends
    assert store.session(laptop) is None


def test_release_and_remove(store):
    token = store.login("alpha", "alpha123")
    assert store.release("beta") == 0
    assert store.release() == 1
    assert store.session(token) is None
    token = store.login("alpha", "alpha123")
    store.remove("alpha")
    assert store.session(token) is None


def test_project_ownership(store, tmp_path):
    a, b = tmp_path / "one", tmp_path / "two"
    store.assign(a, "alpha")
    store.assign(b, "alpha")
    assert store.owner(a) == "alpha" and store.projects("alpha") == [str(a), str(b)]
    store.assign(b, "beta")  # a folder has one owner: assigning moves it
    assert store.projects("alpha") == [str(a)] and store.owner(b) == "beta"
    store.assign(a, None)
    assert store.owner(a) is None
    with pytest.raises(ProfileError, match="unknown profile"):
        store.assign(a, "nobody")
    assert store.remove("beta") == [str(b)] and store.owner(b) is None


def test_first_profile_gets_existing_projects(tmp_path, clock):
    s = Store(tmp_path / "fresh", clock=clock)
    s.add("studio", "studio1", projects=[tmp_path / "old1", tmp_path / "old2"], only_if_empty=True)
    assert s.projects("studio") == [str(tmp_path / "old1"), str(tmp_path / "old2")]

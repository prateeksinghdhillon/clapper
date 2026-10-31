"""Profiles: who may open which projects, and which profile is using Clapper right now.

    ~/.clapper/profiles.json   profiles: name, scrypt password hash, owned project folders
    ~/.clapper/sessions.json   signed-in devices: sha256 of the cookie token, profile, last seen

One profile at a time: logging in is refused while another profile has a session that was
active within `idle_minutes`. The same profile may be logged in on any number of devices.
A successful login ends the (idle) sessions of every other profile, so sessions.json only
ever holds sessions of one profile. Pure Python (no torch); shared by the web API and the CLI.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: the in-process lock still serialises the web server
    fcntl = None

CLAPPER_DIR = Path.home() / ".clapper"
IDLE_MINUTES = 10          # a session holds Clapper this long after its last request
SESSION_DAYS = 30          # a device stays logged in this long without any request
TOUCH_SECONDS = 20         # "last seen" is written at most this often per session
MIN_PASSWORD = 4
_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
_thread_lock = threading.RLock()
_dummy_hash: str | None = None


class ProfileError(ValueError):
    pass


class InUse(ProfileError):
    """Another profile is logged in and active."""

    def __init__(self, holder: str, name: str, idle_seconds: float, idle_minutes: float):
        self.holder, self.name, self.idle_seconds = holder, name, idle_seconds
        ago = "just now" if idle_seconds < 60 else f"{int(idle_seconds // 60)} min ago"
        super().__init__(f"Clapper is in use by {name} (last active {ago}). Try again after they log out, "
                         f"or {int(idle_minutes)} minutes after their last activity.")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _norm(path: str | Path) -> str:
    return str(Path(os.path.expanduser(str(path))).resolve())


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(digest) // 2)
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(got.hex(), digest)


def _check_new_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise ProfileError(f"passwords need at least {MIN_PASSWORD} characters")


class Store:
    def __init__(self, root: str | Path | None = None, *, clock=time.time):
        self.root = Path(root) if root else CLAPPER_DIR
        self.clock = clock

    # ------------------------------------------------------------ files

    @contextlib.contextmanager
    def _locked(self):
        with _thread_lock:
            self.root.mkdir(parents=True, exist_ok=True)
            if fcntl is None:
                yield
                return
            with open(self.root / ".profiles.lock", "a") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)

    def _read(self, name: str, default: dict) -> dict:
        try:
            return json.loads((self.root / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _write(self, name: str, data: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / name
        tmp = path.with_name(f"{name}.{secrets.token_hex(3)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # holds password hashes
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)

    def _profiles_data(self) -> dict:
        data = self._read("profiles.json", {})
        data.setdefault("idle_minutes", IDLE_MINUTES)
        data.setdefault("profiles", {})
        return data

    def _sessions_data(self) -> dict:
        data = self._read("sessions.json", {})
        data.setdefault("sessions", {})
        return data

    def idle_seconds(self, data: dict | None = None) -> float:
        return float((data or self._profiles_data()).get("idle_minutes", IDLE_MINUTES)) * 60

    # ------------------------------------------------------------ profiles

    def profiles(self) -> dict:
        return self._profiles_data()["profiles"]

    def get(self, pid: str) -> dict | None:
        return self.profiles().get(pid)

    def add(self, pid: str, password: str, *, name: str = "", projects=(), only_if_empty: bool = False) -> dict:
        pid = (pid or "").strip().lower()
        if not _ID_RE.match(pid):
            raise ProfileError("profile ids are lowercase letters, digits, - and _, starting with a letter (max 32)")
        _check_new_password(password)
        hashed = hash_password(password)
        with self._locked():
            data = self._profiles_data()
            if only_if_empty and data["profiles"]:
                raise ProfileError("profiles already exist — log in instead")
            if pid in data["profiles"]:
                raise ProfileError(f"profile {pid} already exists")
            data["profiles"][pid] = {"name": (name or "").strip() or pid, "password": hashed,
                                     "created": _now_iso(), "projects": []}
            for path in projects:
                self._assign(data, path, pid)
            self._write("profiles.json", data)
            return data["profiles"][pid]

    def set_password(self, pid: str, password: str, *, keep: str | None = None) -> int:
        """Change a password and end the profile's sessions, except the one with token `keep`
        (the device that made the change). Returns how many sessions were ended."""
        _check_new_password(password)
        hashed = hash_password(password)
        with self._locked():
            data = self._profiles_data()
            if pid not in data["profiles"]:
                raise ProfileError(f"unknown profile {pid}")
            data["profiles"][pid]["password"] = hashed
            self._write("profiles.json", data)
            s = self._sessions_data()
            kept = _token_hash(keep) if keep else None
            remaining = {h: v for h, v in s["sessions"].items() if v["profile"] != pid or h == kept}
            ended = len(s["sessions"]) - len(remaining)
            self._write("sessions.json", {"sessions": remaining})
            return ended

    def change_password(self, token: str | None, current: str, new: str) -> int:
        """A logged-in profile changes its own password (the current one is required)."""
        pid = self.session(token)
        if not pid:
            raise ProfileError("log in first")
        if not check_password(current or "", self.profiles()[pid]["password"]):
            raise ProfileError("the current password is wrong")
        return self.set_password(pid, new, keep=token)

    def remove(self, pid: str) -> list[str]:
        """Delete a profile; returns the project folders it owned (they become unassigned)."""
        with self._locked():
            data = self._profiles_data()
            prof = data["profiles"].pop(pid, None)
            if prof is None:
                raise ProfileError(f"unknown profile {pid}")
            self._write("profiles.json", data)
            s = self._sessions_data()
            s["sessions"] = {h: v for h, v in s["sessions"].items() if v["profile"] != pid}
            self._write("sessions.json", s)
            return prof["projects"]

    # ------------------------------------------------------------ project ownership

    @staticmethod
    def _assign(data: dict, path: str | Path, pid: str | None) -> None:
        path = _norm(path)
        if pid is not None and pid not in data["profiles"]:
            raise ProfileError(f"unknown profile {pid}")
        for prof in data["profiles"].values():
            prof["projects"] = [p for p in prof["projects"] if p != path]
        if pid is not None:
            data["profiles"][pid]["projects"].append(path)

    def assign(self, path: str | Path, pid: str | None) -> None:
        """Give a project folder to a profile (None = to nobody). A folder has at most one owner."""
        with self._locked():
            data = self._profiles_data()
            self._assign(data, path, pid)
            self._write("profiles.json", data)

    def owner(self, path: str | Path) -> str | None:
        path = _norm(path)
        return next((pid for pid, prof in self.profiles().items() if path in prof["projects"]), None)

    def projects(self, pid: str) -> list[str]:
        return list((self.get(pid) or {}).get("projects", []))

    # ------------------------------------------------------------ sessions

    def _prune(self, sessions: dict, profiles: dict, now: float) -> dict:
        return {h: v for h, v in sessions.items()
                if v.get("profile") in profiles and now - v.get("seen", 0) < SESSION_DAYS * 86400}

    def login(self, pid: str, password: str, *, agent: str = "", replace: str | None = None) -> str:
        """Returns a new session token. `replace` is this device's current token (switching profiles)."""
        global _dummy_hash
        pid = (pid or "").strip().lower()
        prof = self.get(pid)
        if prof is None:
            _dummy_hash = _dummy_hash or hash_password(secrets.token_hex(8))
            check_password(password or "", _dummy_hash)  # same time as a real check
            raise ProfileError("wrong profile id or password")
        if not check_password(password or "", prof["password"]):
            raise ProfileError("wrong profile id or password")
        with self._locked():
            now = self.clock()
            data = self._profiles_data()
            if pid not in data["profiles"]:
                raise ProfileError("wrong profile id or password")
            sessions = self._prune(self._sessions_data()["sessions"], data["profiles"], now)
            if replace:
                sessions.pop(_token_hash(replace), None)
            others = [v for v in sessions.values() if v["profile"] != pid and now - v["seen"] < self.idle_seconds(data)]
            if others:
                latest = max(others, key=lambda v: v["seen"])
                holder = latest["profile"]
                raise InUse(holder, data["profiles"][holder]["name"], now - latest["seen"], self.idle_seconds(data) / 60)
            sessions = {h: v for h, v in sessions.items() if v["profile"] == pid}
            token = secrets.token_urlsafe(32)
            sessions[_token_hash(token)] = {"profile": pid, "created": now, "seen": now, "agent": (agent or "")[:200]}
            self._write("sessions.json", {"sessions": sessions})
        return token

    def session(self, token: str | None) -> str | None:
        """The profile a session token belongs to (and mark it active), or None."""
        if not token:
            return None
        key = _token_hash(token)
        now = self.clock()
        sess = self._sessions_data()["sessions"].get(key)
        if not sess or now - sess.get("seen", 0) >= SESSION_DAYS * 86400 or sess.get("profile") not in self.profiles():
            return None
        if now - sess["seen"] > TOUCH_SECONDS:
            with self._locked():
                data = self._sessions_data()
                sess = data["sessions"].get(key)
                if sess is None:  # ended meanwhile (logout, or another profile logged in)
                    return None
                sess["seen"] = now
                self._write("sessions.json", data)
        return sess["profile"]

    def logout(self, token: str | None, *, everywhere: bool = True) -> int:
        """End this device's session, or (default) every session of its profile. Returns sessions ended."""
        if not token:
            return 0
        key = _token_hash(token)
        with self._locked():
            data = self._sessions_data()
            sess = data["sessions"].get(key)
            if sess is None:
                return 0
            keep = {h: v for h, v in data["sessions"].items()
                    if h != key and not (everywhere and v["profile"] == sess["profile"])}
            ended = len(data["sessions"]) - len(keep)
            self._write("sessions.json", {"sessions": keep})
            return ended

    def release(self, pid: str | None = None) -> int:
        """End every session of a profile (or of everyone). For the machine owner (CLI)."""
        with self._locked():
            data = self._sessions_data()
            keep = {h: v for h, v in data["sessions"].items() if pid is not None and v["profile"] != pid}
            ended = len(data["sessions"]) - len(keep)
            self._write("sessions.json", {"sessions": keep})
            return ended

    def active(self) -> dict | None:
        """Who is logged in: profile, name, devices, seconds since last activity, whether it holds Clapper."""
        now = self.clock()
        data = self._profiles_data()
        sessions = list(self._prune(self._sessions_data()["sessions"], data["profiles"], now).values())
        if not sessions:
            return None
        latest = max(sessions, key=lambda v: v["seen"])
        pid = latest["profile"]
        idle = now - latest["seen"]
        return {"profile": pid, "name": data["profiles"][pid]["name"], "devices": sum(v["profile"] == pid for v in sessions),
                "idle_seconds": idle, "holds": idle < self.idle_seconds(data)}

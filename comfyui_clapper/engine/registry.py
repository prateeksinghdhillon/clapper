"""List of known project folders, shared by the web page and the CLI (~/.clapper/projects.json)."""
from __future__ import annotations

import json
import os
from pathlib import Path

REGISTRY = Path.home() / ".clapper" / "projects.json"
CONFIG = Path.home() / ".clapper" / "config.json"
DEFAULT_LOCATION = Path.home() / "Documents" / "Clapper-Projects"
RECENT_KEEP = 8


def known() -> list[str]:
    try:
        return json.loads(REGISTRY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def register(path: str | Path) -> None:
    path = str(Path(path).resolve())
    items = [p for p in known() if p != path]
    items.insert(0, path)
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
    os.replace(tmp, REGISTRY)


def unregister(path: str | Path) -> None:
    path = str(Path(path).resolve())
    items = [p for p in known() if p != path]
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
    os.replace(tmp, REGISTRY)


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def config() -> dict:
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("default_location", str(DEFAULT_LOCATION))
    data.setdefault("recent_locations", [])
    return data


def set_default_location(path: str | Path) -> None:
    data = config()
    data["default_location"] = str(Path(path).resolve())
    _write_json(CONFIG, data)


def remember_location(path: str | Path) -> None:
    data = config()
    path = str(Path(path).resolve())
    data["recent_locations"] = [path] + [p for p in data["recent_locations"] if p != path][: RECENT_KEEP - 1]
    _write_json(CONFIG, data)


def locations(projects: list[str] | None = None) -> dict:
    """Where new projects can go: the default, recently used parents, and parents of known projects
    (or of `projects` only, e.g. the projects of one profile)."""
    data = config()
    seen, out = set(), []
    for p in [data["default_location"], *data["recent_locations"], *(str(Path(x).parent) for x in (known() if projects is None else projects))]:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return {"default": data["default_location"], "choices": out}

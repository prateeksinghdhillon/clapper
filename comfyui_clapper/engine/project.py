"""Project store: scenes, takes, references, lineage. Pure Python (no torch).

A project is a folder:
    project.json            the single source of truth (atomic writes)
    history/                previous project.json versions (undo)
    refs/                   copies of reference files added as references
    assets/<kind>s/         library files (images, audio, video) with their origin in `assets`
    trash/                  removed library files, until emptied
    takes/<scene>/<take>/   one folder per take (latent, mp4, wav, meta.json)
    exports/
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import shutil
import time
from pathlib import Path

SCHEMA = 1
HISTORY_KEEP = 50
REF_KINDS = ("image", "audio", "video")
JOIN_MODES = ("continue", "cut", "bridge")
ASSET_KINDS = ("image", "audio", "video")
ENTITY_KINDS = ("character", "place", "prop", "style")
ASSET_ROLES = ("character", "place", "prop", "style", "start", "frame", "voice", "speech", "sound", "music",
               "clip", "other")
_EXT_KIND = {**{e: "image" for e in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")},
             **{e: "audio" for e in (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac")},
             **{e: "video" for e in (".mp4", ".mov", ".webm", ".mkv", ".m4v")}}
TAKE_STATUSES = ("queued", "rendering", "done", "failed", "interrupted")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_TAG_RE = re.compile(r"@([a-z][a-z0-9_]{0,31})\b")


class ProjectError(ValueError):
    pass


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def asset_kind_for(path: str | Path) -> str:
    kind = _EXT_KIND.get(Path(path).suffix.lower())
    if kind is None:
        raise ProjectError(f"unsupported file type {Path(path).suffix or '(none)'}: images, audio (mp3, wav, m4a, ogg, "
                           "flac, …) and video (mp4, mov, webm) can be added")
    return kind


def folder_name(name: str) -> str:
    """A safe folder name from a project name: 'Café promo 2!' -> 'cafe-promo-2'."""
    import unicodedata

    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60].strip("-") or "project"


def disk_usage(root: str | Path) -> dict:
    """Bytes used by a project folder, by part. Symlinks are not followed."""
    root = Path(root)
    parts = {"takes": 0, "exports": 0, "refs": 0, "history": 0, "other": 0}
    files = 0

    def walk(path: Path) -> int:
        nonlocal files
        size = 0
        try:
            with os.scandir(path) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        size += walk(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        size += entry.stat(follow_symlinks=False).st_size
                        files += 1
        except OSError:
            pass
        return size

    try:
        entries = list(os.scandir(root))
    except OSError:
        entries = []
    for entry in entries:
        # The library (reference copies, assets/ and its trash/) is reported as one part, "refs".
        key = "refs" if entry.name in ("assets", "trash") else entry.name if entry.name in parts else "other"
        if entry.is_dir(follow_symlinks=False):
            parts[key] += walk(Path(entry.path))
        elif entry.is_file(follow_symlinks=False):
            parts[key] += entry.stat(follow_symlinks=False).st_size
            files += 1
    return {"total": sum(parts.values()), "parts": parts, "files": files}


def delete_project_folder(root: str | Path, *, home: str | Path | None = None) -> None:
    """Permanently delete a project folder after safety checks."""
    root = Path(root).resolve()
    home = Path(home or Path.home()).resolve()
    if not (root / "project.json").is_file():
        raise ProjectError("not a project folder (no project.json)")
    if home not in root.parents or root == home or len(root.relative_to(home).parts) < 2:
        raise ProjectError("refusing to delete a folder this close to the home folder")
    shutil.rmtree(root)


class Project:
    def __init__(self, root: str | Path, data: dict):
        self.root = Path(root)
        self.data = data

    # ------------------------------------------------------------ create / open / save

    @classmethod
    def create(cls, root: str | Path, name: str, settings: dict | None = None) -> "Project":
        root = Path(root)
        if (root / "project.json").exists():
            raise ProjectError(f"a project already exists at {root}")
        for sub in ("history", "refs", "takes", "exports"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        data = {
            "schema": SCHEMA, "name": name, "created": _now(), "updated": _now(),
            "settings": settings or {},       # overrides on top of DEFAULT_SETTINGS
            "style": "",                      # shared text sent with every scene
            "start_image": None,              # library name pinned as frame 0 of the first scene
            "library": {"items": {}, "defaults": []},
            "assets": {},                     # every library file with its origin; @names link to these
            "entities": {},                   # characters, places, props, styles: identity + looks (assets with `entity`)
            "scenes": [],
            "takes": {},
        }
        project = cls(root, data)
        project.save(snapshot=False)
        return project

    @classmethod
    def open(cls, root: str | Path) -> "Project":
        root = Path(root)
        data = json.loads((root / "project.json").read_text(encoding="utf-8"))
        if data.get("schema") != SCHEMA:
            raise ProjectError(f"unsupported project schema {data.get('schema')}")
        project = cls(root, data)
        project._link_library_assets()  # projects from before the asset library (kept in memory until saved)
        return project

    def save(self, *, snapshot: bool = True) -> None:
        path = self.root / "project.json"
        if snapshot and path.exists():
            hist = self.root / "history"
            hist.mkdir(exist_ok=True)
            shutil.copy2(path, hist / f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}.json")
            for old in sorted(hist.glob("*.json"))[:-HISTORY_KEEP]:
                old.unlink()
        self.data["updated"] = _now()
        _atomic_write_json(path, self.data)

    # ------------------------------------------------------------ library

    def add_reference(self, name: str, source: str | Path, kind: str, *, description: str = "",
                      definition: str = "", default: bool = False) -> dict:
        if not _NAME_RE.match(name):
            raise ProjectError("reference names are lowercase letters, digits and _, starting with a letter")
        if kind not in REF_KINDS:
            raise ProjectError(f"kind must be one of {REF_KINDS}")
        source = Path(source)
        digest = _sha256(source)
        dest = self.root / "refs" / f"{name}{source.suffix.lower()}"
        shutil.copy2(source, dest)
        item = {"kind": kind, "file": str(dest.relative_to(self.root)), "sha256": digest,
                "description": description, "definition": definition, "source": str(source)}
        old = self.data["library"]["items"].get(name)
        self.data["library"]["items"][name] = item
        if old and old.get("asset") and old["file"] == item["file"]:
            self.data.get("assets", {}).pop(old["asset"], None)  # the file was overwritten
        self._link_library_assets()
        defaults = self.data["library"]["defaults"]
        if default and name not in defaults:
            defaults.append(name)
        self.save()
        return item

    def update_reference(self, name: str, **fields) -> dict:
        """description (for people), definition (sent to H3 in every scene that uses the reference),
        card + card_status (what it looks like, for the assistant; "model" until the user confirms it)."""
        item = self.data["library"]["items"].get(name)
        if item is None:
            raise ProjectError(f"unknown reference @{name}")
        unknown = set(fields) - {"description", "definition", "card", "card_status"}
        if unknown:
            raise ProjectError(f"cannot update {sorted(unknown)}")
        item.update(fields)
        self.save()
        return item

    def remove_reference(self, name: str) -> None:
        """Remove an @name (its asset and file stay in the library). Refused while text still mentions it."""
        if name not in self.data["library"]["items"]:
            raise ProjectError(f"unknown reference @{name}")
        mention = re.compile(r"@" + re.escape(name) + r"\b")
        users = [str(i + 1) for i, s in enumerate(self.scenes) if mention.search(s.get("text", ""))]
        if mention.search(self.data.get("style", "")):
            users.insert(0, "the style text")
        if users:
            raise ProjectError(f"@{name} is still mentioned in " + ", ".join(
                u if u == "the style text" else f"scene {u}" for u in users) + "; remove the mentions first")
        del self.data["library"]["items"][name]
        lib = self.data["library"]
        lib["defaults"] = [n for n in lib["defaults"] if n != name]
        if self.data.get("start_image") == name:
            self.data["start_image"] = None
        for scene in self.scenes:
            if scene.get("refs") is not None and name in scene["refs"]:
                scene["refs"] = [n for n in scene["refs"] if n != name]
            if scene.get("start_image") == name:
                scene["start_image"] = None
        self.save()

    # ------------------------------------------------------------ assets

    @property
    def assets(self) -> dict:
        return self.data.setdefault("assets", {})

    def asset(self, asset_id: str) -> dict:
        try:
            return self.assets[asset_id]
        except KeyError:
            raise ProjectError(f"unknown library item {asset_id}") from None

    def asset_path(self, asset_id: str) -> Path:
        return self.root / self.asset(asset_id)["file"]

    def asset_names(self, asset_id: str) -> list[str]:
        return [n for n, item in self.data["library"]["items"].items() if item.get("asset") == asset_id]

    def _link_library_assets(self) -> None:
        """Every @name points at an asset; references made before the asset library get one (same file)."""
        assets = self.assets
        by_file = {a["file"]: a["id"] for a in assets.values()}
        for name, item in self.data["library"]["items"].items():
            if item.get("asset") in assets:
                continue
            aid = by_file.get(item["file"])
            if aid is None:
                aid = _new_id("a")
                role = "start" if self.data.get("start_image") == name else ("voice" if item["kind"] == "audio" else "other")
                assets[aid] = {"id": aid, "kind": item["kind"], "role": role, "title": item.get("description") or name,
                               "file": item["file"], "sha256": item.get("sha256"), "tags": [],
                               "origin": {"type": "upload", "source": item.get("source", "")},
                               "parent": None, "created": item.get("created") or self.data.get("created")}
                by_file[item["file"]] = aid
            item["asset"] = aid

    def add_asset(self, source: str | Path, *, kind: str | None = None, role: str = "other", title: str = "",
                  origin: dict | None = None, parent: str | None = None, tags: list[str] | None = None,
                  save: bool = True) -> dict:
        """Copy a file into the library. `origin` says what made it (upload, frame of a take, a tool run)."""
        source = Path(source)
        if not source.is_file():
            raise ProjectError(f"no such file {source}")
        kind = kind or asset_kind_for(source)
        if kind not in ASSET_KINDS:
            raise ProjectError(f"kind must be one of {ASSET_KINDS}")
        if role not in ASSET_ROLES:
            raise ProjectError(f"role must be one of {ASSET_ROLES}")
        if parent is not None:
            self.asset(parent)
        aid = _new_id("a")
        dest = self.root / "assets" / f"{kind}s" / f"{aid}{source.suffix.lower()}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        asset = {"id": aid, "kind": kind, "role": role, "title": title.strip() or source.stem, "file": str(dest.relative_to(self.root)),
                 "sha256": _sha256(dest), "tags": list(tags or []), "origin": origin or {"type": "upload", "source": source.name},
                 "parent": parent, "created": _now()}
        self.assets[aid] = asset
        if save:
            self.save()
        return asset

    def update_asset(self, asset_id: str, **fields) -> dict:
        asset = self.asset(asset_id)
        unknown = set(fields) - {"title", "role", "tags", "notes", "transcript", "look"}
        if unknown:
            raise ProjectError(f"cannot update {sorted(unknown)}")
        if "role" in fields and fields["role"] not in ASSET_ROLES:
            raise ProjectError(f"role must be one of {ASSET_ROLES}")
        asset.update(fields)
        self.save()
        return asset

    def name_asset(self, asset_id: str, name: str, *, definition: str = "", description: str = "",
                   default: bool = False) -> dict:
        """Give a library item an @name so scenes can use it as a reference."""
        asset = self.asset(asset_id)
        if not _NAME_RE.match(name):
            raise ProjectError("reference names are lowercase letters, digits and _, starting with a letter")
        existing = self.data["library"]["items"].get(name)
        if existing and existing.get("asset") != asset_id:
            raise ProjectError(f"@{name} is already used by another item")
        if asset["kind"] == "video":
            raise ProjectError("video references are not supported yet")
        item = {"kind": asset["kind"], "file": asset["file"], "sha256": asset.get("sha256"),
                "description": description or (existing or {}).get("description") or asset.get("title", ""),
                "definition": definition or (existing or {}).get("definition", ""), "source": f"library:{asset_id}",
                "asset": asset_id}
        for key in ("card", "card_status"):
            if existing and key in existing:
                item[key] = existing[key]
        self.data["library"]["items"][name] = item
        if default and name not in self.data["library"]["defaults"]:
            self.data["library"]["defaults"].append(name)
        self.save()
        return item

    # ------------------------------------------------------------ entities (characters, places, props, styles)

    @property
    def entities(self) -> dict:
        return self.data.setdefault("entities", {})

    def entity(self, entity_id: str) -> dict:
        try:
            return self.entities[entity_id]
        except KeyError:
            raise ProjectError(f"unknown character / place {entity_id}") from None

    def entity_looks(self, entity_id: str) -> list[dict]:
        """The entity's images (and other files), oldest first."""
        return sorted((a for a in self.assets.values() if a.get("entity") == entity_id), key=lambda a: a.get("created") or "")

    def _free_name(self, name: str) -> str:
        base = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")
        base = (base if base[:1].isalpha() else "x_" + base)[:28] or "item"
        candidate, n = base, 2
        while candidate in self.data["library"]["items"]:
            candidate, n = f"{base}_{n}", n + 1
        return candidate

    def create_entity(self, kind: str, name: str, *, description: str = "", from_asset: str | None = None,
                      default: bool = True) -> dict:
        """A character, place, prop or style. With `from_asset`, that image becomes its first look and main image,
        and the entity gets an @name (its description is the definition H3 receives)."""
        if kind not in ENTITY_KINDS:
            raise ProjectError(f"kind must be one of {ENTITY_KINDS}")
        if not name.strip():
            raise ProjectError("give it a name")
        eid = _new_id("e")
        # An image that already has an @name keeps it: scenes that use it go on working.
        adopted = [n for n in self.asset_names(from_asset) if not self.data["library"]["items"][n].get("entity")] if from_asset else []
        entity = {"id": eid, "kind": kind, "name": name.strip(), "description": description.strip(), "main": None,
                  "ref": adopted[0] if adopted else self._free_name(name), "voice": None, "default": bool(default), "created": _now()}
        self.entities[eid] = entity
        if from_asset:
            self.add_to_entity(from_asset, eid, look="default", save=False)
            self.set_entity_main(eid, from_asset, save=False)
        self.save()
        return entity

    def update_entity(self, entity_id: str, **fields) -> dict:
        entity = self.entity(entity_id)
        unknown = set(fields) - {"name", "description", "voice", "notes", "default"}
        if unknown:
            raise ProjectError(f"cannot update {sorted(unknown)}")
        if fields.get("voice"):
            if self.asset(fields["voice"])["kind"] != "audio":
                raise ProjectError("a voice must be a sound from the library")
        entity.update(fields)
        self._sync_entity_ref(entity)
        self.save()
        return entity

    def add_to_entity(self, asset_id: str, entity_id: str | None, *, look: str = "", save: bool = True) -> dict:
        """Put a library item into an entity (or take it out with None) with a look label."""
        asset = self.asset(asset_id)
        if entity_id is None:
            old = asset.pop("entity", None)
            asset.pop("look", None)
            if old and self.entities.get(old, {}).get("main") == asset_id:
                rest = [a for a in self.entity_looks(old) if a["kind"] == "image"]
                self.entities[old]["main"] = rest[0]["id"] if rest else None
                self._sync_entity_ref(self.entities[old])
        else:
            entity = self.entity(entity_id)
            asset["entity"], asset["look"] = entity_id, look.strip() or asset.get("look") or "look"
            if not entity.get("main") and asset["kind"] == "image":
                entity["main"] = asset_id
                self._sync_entity_ref(entity)
        if save:
            self.save()
        return asset

    def set_entity_main(self, entity_id: str, asset_id: str, *, save: bool = True) -> dict:
        """The image scenes use for this entity: its @name points here."""
        entity = self.entity(entity_id)
        asset = self.asset(asset_id)
        if asset.get("entity") != entity_id or asset["kind"] != "image":
            raise ProjectError("the main image must be one of its own images")
        entity["main"] = asset_id
        self._sync_entity_ref(entity)
        if save:
            self.save()
        return entity

    def _sync_entity_ref(self, entity: dict) -> None:
        """Keep the entity's @name on its main image, with its description as the definition."""
        name = entity.get("ref")
        if not name or not entity.get("main"):
            return
        asset = self.assets[entity["main"]]
        items = self.data["library"]["items"]
        existing = items.get(name)
        if existing and existing.get("entity") not in (None, entity["id"]):
            entity["ref"] = name = self._free_name(entity["name"])
            existing = None
        definition = f"@{name} is {entity['name']}" + (f": {entity['description']}" if entity.get("description") else ".")
        item = {"kind": "image", "file": asset["file"], "sha256": asset.get("sha256"), "description": entity["name"],
                "definition": definition if not (existing or {}).get("definition_custom") else existing["definition"],
                "source": f"library:{asset['id']}", "asset": asset["id"], "entity": entity["id"]}
        for key in ("card", "card_status", "definition_custom"):
            if existing and key in existing:
                item[key] = existing[key]
        items[name] = item
        defaults = self.data["library"]["defaults"]
        if entity.get("default", True) and entity["kind"] in ("character", "style") and name not in defaults:
            defaults.append(name)

    def remove_entity(self, entity_id: str) -> None:
        """Remove the character / place; its images stay in the library, its @name is removed when unused."""
        entity = self.entity(entity_id)
        for a in self.entity_looks(entity_id):
            a.pop("entity", None)
            a.pop("look", None)
        del self.entities[entity_id]
        if entity.get("ref") in self.data["library"]["items"]:
            try:
                self.remove_reference(entity["ref"])
                return
            except ProjectError:
                self.data["library"]["items"][entity["ref"]].pop("entity", None)
        self.save()

    def remove_asset(self, asset_id: str) -> None:
        """Move a library file to trash/. Refused while an @name or a scene's audio clip uses it."""
        asset = self.asset(asset_id)
        names = self.asset_names(asset_id)
        if names:
            raise ProjectError("remove its @name first: " + ", ".join("@" + n for n in names))
        users = [str(i + 1) for i, s in enumerate(self.scenes) if any(c.get("asset") == asset_id for c in s.get("audio_clips") or [])]
        if users:
            raise ProjectError("used as an audio clip in scene " + ", ".join(users))
        del self.assets[asset_id]
        path = self.root / asset["file"]
        if path.exists() and not any(a["file"] == asset["file"] for a in self.assets.values()) \
                and not any(i["file"] == asset["file"] for i in self.data["library"]["items"].values()):
            trash = self.root / "trash"
            trash.mkdir(exist_ok=True)
            shutil.move(str(path), trash / path.name)
        self.save()

    def ref_path(self, name: str) -> Path:
        try:
            return self.root / self.data["library"]["items"][name]["file"]
        except KeyError:
            raise ProjectError(f"unknown reference @{name}") from None

    # ------------------------------------------------------------ scenes

    @property
    def scenes(self) -> list[dict]:
        return self.data["scenes"]

    def scene(self, scene_id: str) -> dict:
        for scene in self.scenes:
            if scene["id"] == scene_id:
                return scene
        raise ProjectError(f"unknown scene {scene_id}")

    def scene_index(self, scene_id: str) -> int:
        return next(i for i, s in enumerate(self.scenes) if s["id"] == scene_id)

    def add_scene(self, *, text: str = "", seconds: float | None = None, after: str | None = None,
                  join: str = "continue", refs: list[str] | None = None, title: str = "",
                  start_image: str | None = None, audio_clips: list[dict] | None = None) -> dict:
        if join not in JOIN_MODES:
            raise ProjectError(f"join must be one of {JOIN_MODES}")
        self._check_scene_fields({"start_image": start_image, "audio_clips": audio_clips or []})
        scene = {"id": _new_id("s"), "title": title, "text": text, "seconds": seconds, "join": join,
                 "refs": refs, "selected_take": None, "created": _now()}
        if start_image is not None:
            scene["start_image"] = start_image
        if audio_clips:
            scene["audio_clips"] = [dict(c) for c in audio_clips]
        position = len(self.scenes) if after is None else self.scene_index(after) + 1
        self.scenes.insert(position, scene)
        self.save()
        return scene

    def update_scene(self, scene_id: str, **fields) -> dict:
        allowed = {"title", "text", "seconds", "join", "refs", "start_image", "audio_clips"}
        unknown = set(fields) - allowed
        if unknown:
            raise ProjectError(f"cannot update {sorted(unknown)}")
        if "join" in fields and fields["join"] not in JOIN_MODES:
            raise ProjectError(f"join must be one of {JOIN_MODES}")
        self._check_scene_fields(fields)
        if fields.get("audio_clips") is not None:
            fields["audio_clips"] = [{"asset": c["asset"], "at": float(c.get("at", 0))} for c in fields["audio_clips"]]
        self.scene(scene_id).update(fields)
        self.save()
        return self.scene(scene_id)

    def _check_scene_fields(self, fields: dict) -> None:
        start = fields.get("start_image")
        if start:  # None = inherit the project's start image (first scene), "" = no start image
            item = self.data["library"]["items"].get(start)
            if item is None or item["kind"] != "image":
                raise ProjectError(f"start image must be an image reference; @{start} is not one")
        for clip in fields.get("audio_clips") or []:
            asset = self.assets.get(clip.get("asset", ""))
            if asset is None or asset["kind"] != "audio":
                raise ProjectError("an audio clip must be an audio item from the library")
            if float(clip.get("at", 0)) < 0:
                raise ProjectError("an audio clip cannot start before the scene")

    def scene_start(self, scene: dict) -> str | None:
        """The start image's @name for this scene: its own, none (""), or the project's (None = inherit)."""
        own = scene.get("start_image")
        if own is None:
            return self.data.get("start_image")
        return own or None

    def scene_mode(self, scene: dict) -> str:
        """continue | bridge | fresh (from references) | image (from its start image)."""
        index = self.scene_index(scene["id"]) if any(s["id"] == scene["id"] for s in self.scenes) else 0
        if index > 0 and scene.get("join") in ("continue", "bridge"):
            return scene["join"]
        start = self.scene_start(scene)
        pinned = start and (index == 0 or scene.get("start_image"))
        return "image" if pinned else "fresh"

    def move_scene(self, scene_id: str, new_index: int) -> None:
        scene = self.scenes.pop(self.scene_index(scene_id))
        self.scenes.insert(max(0, min(new_index, len(self.scenes))), scene)
        self.save()

    def remove_scene(self, scene_id: str) -> None:
        """Removes the scene from the timeline; its takes stay on disk and in `takes`."""
        self.scenes.pop(self.scene_index(scene_id))
        self.save()

    # ------------------------------------------------------------ references + prompt compile

    def scene_refs(self, scene: dict) -> list[str]:
        """The scene's own reference list, or the project defaults when it has none."""
        refs = scene.get("refs")
        return list(self.data["library"]["defaults"] if refs is None else refs)

    def compile_prompt(self, scene: dict) -> dict:
        """Resolve @names into <Picture N>/<Audio N> for this scene and join the shared style text.

        Numbering follows what the conditioning node presents: the start image (if presented)
        is Picture 1, then this scene's image references in list order; audio references are
        numbered separately. Names used in the text but not active in the scene are errors.
        """
        refs = self.scene_refs(scene)
        items = self.data["library"]["items"]
        missing = [r for r in refs if r not in items]
        if missing:
            raise ProjectError(f"scene uses unknown references: {', '.join('@' + m for m in missing)}")
        start = self.scene_start(scene)
        labels: dict[str, str] = {}
        picture = 0
        if start:
            picture += 1
            labels[start] = f"<Picture {picture}>"
        audio = 0
        images, audios = [], []
        for name in refs:
            kind = items[name]["kind"]
            if kind == "image" and name != start:
                picture += 1
                labels[name] = f"<Picture {picture}>"
                images.append(name)
            elif kind == "audio":
                audio += 1
                labels[name] = f"<Audio {audio}>"
                audios.append(name)
            elif kind == "video":
                raise ProjectError("video references are not supported yet")
        if audio > 3:
            raise ProjectError("at most 3 audio references per scene")
        if len(images) > 9:
            raise ProjectError("at most 9 image references per scene")

        errors: list[str] = []

        def sub(match: re.Match) -> str:
            name = match.group(1)
            if name in labels:
                return labels[name]
            errors.append(name)
            return match.group(0)

        scene_text = scene.get("text", "").strip()
        index = self.scene_index(scene["id"]) if any(s["id"] == scene["id"] for s in self.scenes) else 0
        if index > 0 and scene.get("join") in ("continue", "bridge") and self.data.get("continuation_sentence", True):
            # The Continuum author's recommended boundary opener, numbered from the current timeline order.
            scene_text = f"Continuation of Chunk {index}. {scene_text}".strip()
        # Definitions of the references active in this scene, in <Picture N>/<Audio N> order.
        definitions = [items[n].get("definition", "").strip() for n in labels if items[n].get("definition", "").strip()]
        style = self.data.get("style", "").strip()
        if definitions:
            lines = style.split("\n")
            anchor = next((i for i, line in enumerate(lines) if line.strip().lower() == "subject_definitions:"), None)
            if anchor is None:
                style = "\n".join(["subject_definitions:", *definitions]) + ("\n\n" + style if style else "")
            else:
                style = "\n".join(lines[: anchor + 1] + definitions + lines[anchor + 1:])
        body = "\n\n".join(part for part in (style, scene_text) if part)
        text = _TAG_RE.sub(sub, body)
        if errors:
            raise ProjectError("not active in this scene: " + ", ".join(sorted({"@" + e for e in errors})))
        clips = [{"asset": c["asset"], "at": float(c.get("at", 0)), "file": self.assets[c["asset"]]["file"]}
                 for c in scene.get("audio_clips") or [] if c.get("asset") in self.assets]
        if len(clips) != len(scene.get("audio_clips") or []):
            raise ProjectError("an audio clip of this scene is no longer in the library")
        fingerprint = text
        if clips or scene.get("start_image") is not None:
            # Only scenes that use the newer fields hash them, so older takes keep matching their scenes.
            fingerprint += "\n" + json.dumps({"start": scene.get("start_image"), "start_sha": items.get(start, {}).get("sha256") if start else None,
                                               "clips": [(c["asset"], c["at"]) for c in clips]}, sort_keys=True)
        return {"prompt": text, "labels": labels, "images": images, "audios": audios, "start": start, "clips": clips,
                "sha256": hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()}

    # ------------------------------------------------------------ takes

    def new_take(self, scene_id: str, **fields) -> dict:
        take_id = _new_id("t")
        number = 1 + max((t.get("number", 0) for t in self.data["takes"].values() if t["scene"] == scene_id), default=0)
        take = {"id": take_id, "scene": scene_id, "number": number, "status": "queued", "created": _now(),
                "dir": f"takes/{scene_id}/{take_id}", **fields}
        self.data["takes"][take_id] = take
        self.save()
        return take

    def update_take(self, take_id: str, **fields) -> dict:
        take = self.data["takes"][take_id]
        take.update(fields)
        self.save()
        return take

    def select_take(self, scene_id: str, take_id: str) -> None:
        if self.data["takes"].get(take_id, {}).get("scene") != scene_id:
            raise ProjectError(f"take {take_id} does not belong to scene {scene_id}")
        self.scene(scene_id)["selected_take"] = take_id
        self.save()

    def restore_take_prompt(self, take_id: str) -> dict:
        """Put a take's own scene text/refs/length/join back into its scene (to redo or vary that version)."""
        take = self.data["takes"][take_id]
        if "scene_text" not in take:
            raise ProjectError("this take does not have its scene text saved (made before this feature)")
        fields = {"text": take["scene_text"], "refs": take.get("scene_refs"), "seconds": take.get("scene_seconds")}
        if take.get("scene_join"):
            fields["join"] = take["scene_join"]
        for key in ("start_image", "audio_clips"):
            if f"scene_{key}" in take:
                fields[key] = take[f"scene_{key}"]
        return self.update_scene(take["scene"], **fields)

    def takes_for(self, scene_id: str) -> list[dict]:
        return sorted((t for t in self.data["takes"].values() if t["scene"] == scene_id),
                      key=lambda t: (t.get("number", 0), t["created"]))

    def previous_selected_take(self, scene_id: str) -> dict | None:
        index = self.scene_index(scene_id)
        if index == 0:
            return None
        prev = self.scenes[index - 1]
        take_id = prev.get("selected_take")
        return self.data["takes"].get(take_id) if take_id else None

    # ------------------------------------------------------------ status badges

    def scene_status(self, scene_id: str) -> list[str]:
        """Badges: no_take, rendering, prompt_changed, refs_changed, continuity_changed, parent_missing."""
        scene = self.scene(scene_id)
        badges: list[str] = []
        if any(t["status"] in ("queued", "rendering") for t in self.takes_for(scene_id)):
            badges.append("rendering")
        take = self.data["takes"].get(scene.get("selected_take") or "")
        if not take or take.get("status") != "done":
            badges.append("no_take")
            return badges
        try:
            compiled = self.compile_prompt(scene)
            if compiled["sha256"] != take.get("prompt_sha256"):
                badges.append("prompt_changed")
            if self.scene_refs(scene) != take.get("refs"):
                badges.append("refs_changed")
        except ProjectError:
            badges.append("prompt_error")
        if scene["join"] in ("continue", "bridge") and self.scene_index(scene_id) > 0:
            prev = self.previous_selected_take(scene_id)
            if prev is None:
                badges.append("parent_missing")
            elif take.get("parent_take") != prev["id"]:
                badges.append("continuity_changed")
        return badges

    def snapshot(self) -> dict:
        return copy.deepcopy(self.data)

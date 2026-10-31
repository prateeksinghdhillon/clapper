"""The director's assistant: a chat that knows Clapper's workflow and the H3 prompt rules, and edits
projects through proposals the user applies. Pure Python (no torch); Pillow is used for images.

Chats live in <project>/chat/<id>.json. What the model receives for every request:

    system    fixed instructions + the prompt guide (+ summary of older turns, after compaction)
    history   the conversation since the last compaction
    newest    the newest user message, with a fresh <project> snapshot attached

The snapshot rides on the newest message only, so everything before it stays byte-identical
between requests and the model's prompt cache is reused. Lasting facts belong in the project
(story notes, reference cards), not in the chat, so compaction never loses them.
"""
from __future__ import annotations

import base64
import copy
import difflib
import io
import json
import re
import secrets
import subprocess
import time
from pathlib import Path

from .export import FFMPEG
from .llm import Client, LLMError
from .project import JOIN_MODES, Project, ProjectError

GUIDE = Path(__file__).resolve().parents[2] / "docs" / "PROMPT_GUIDE.md"
MAX_ROUNDS = 10
KEEP_RECENT = 8          # messages kept word for word when older ones are summarised
COMPACT_AT = 0.6         # share of the context window that triggers compaction
IMAGE_EDGE = 1024        # long edge of images sent to the model (about 1000 vision tokens)
REPLY_TOKENS = 6000

INSTRUCTIONS = """You are the director's assistant inside Clapper, a tool for making long AI videos with the MiniMax H3 model, scene by scene. You help the user plan the story, split it into scenes, write each scene's H3 prompt, choose references and settings, and review finished takes.

# How Clapper works
- A project is one video: an ordered list of scenes. A scene is usually 5 seconds (1 to 15). Each render of a scene is a take; the user picks one take per scene, and the export joins the picked takes into one video.
- Joins: "continue" (default) starts the scene from the last 22 frames and sound of the previous scene's selected take, so there is no cut. "cut" is a fresh start. "bridge" is experimental; don't suggest it. Scene 1 always starts fresh, from the start image if there is one.
- Because of "continue", a scene's text must start from where the previous scene ends, and end in a state the next scene can pick up.
- References are images (characters, props, style sheets, logos) or voice samples, written as @name. Clapper turns @name into <Picture N>/<Audio N> for each scene. Never write <Picture N> yourself; always write @name.
- A scene uses the project's default references (marked ★) unless it has its own list. A scene's text may only mention @names active in that scene (its references, plus the start image). Fewer references per scene is better.
- The start image (marked ▶) is the first frame of scene 1 and is shown to H3 as <Picture 1> in every scene.
- Each reference has a definition, sent to H3 in the scenes that use it ("@hero is the mascot: …"), and may have a card: notes on what it looks like, for you only.
- The style text is sent with every scene: format, look, voices, rules. Nothing scene-specific goes there.
- Rendering one scene takes the GPU for several minutes. Suggest rendering one scene and checking it before rendering many.

# How you work
- Each user message ends with the current project between <project> tags. Scenes are numbered from 1.
- You never change the project directly. You propose changes with tools (add_scenes, edit_scene, edit_project, …); each becomes a card the user applies or rejects. Then tell the user in one or two sentences what you proposed. Don't repeat the prompt text in your reply; the card shows it.
- To plan a video, make ONE add_scenes call with all its scenes.
- Scene numbers always mean the scenes listed in <project>. New scenes in a proposal waiting for the user get numbers only once it is applied; never propose them again. To correct an add_scenes proposal from the same answer, call add_scenes again with the same `after`: the new card replaces the old one.
- Change only what the user asked for: a change to one scene is one edit_scene call for that scene.
- When a reference's look matters and it has no card, call view_reference, then save what you saw with save_reference_card: precise features only, and say when unsure. The user's definition is more reliable than your eyes when they disagree.
- To check a finished take, call view_take (unless its frames are already attached to the message). Never describe or judge an image or take you have not been shown in this conversation.
- To change an existing scene use edit_scene; add_scenes is only for new scenes. When a take does not match its scene, say what differs, then usually propose an edit_scene that makes the text clearer.
- Keep lasting facts in the story notes with update_notes: the story, characters, tone, the user's preferences and decisions. Old messages get summarised; the notes stay. Always write the complete notes.
- Be brief. Ask a short question when something important is unclear (length, format, language, look) rather than guessing.
- Write scene text exactly as the prompt guide below says.

# Planning a new video
1. If the style text is empty, propose one with edit_project: format and total length, the look (in the style of @style if such a reference exists), who speaks and how, rules such as "no readable writing". Include a line "subject_definitions:".
2. For every reference without a definition, propose one with set_reference_definition.
3. Propose all scenes with ONE add_scenes call. Scenes are only the parts of the video people will watch: never make a scene for a definition, the style text or the start image. You cannot create images; if there is no start image, scene 1 describes the opening setting in words.
4. Each scene: a short title; 5 seconds unless there is a reason; join "continue" (scene 1 always starts fresh); refs null to use the default references, or a list containing every @name its text mentions; text with the three H3 fields, each on its own line.
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


# ------------------------------------------------------------------ chat storage

def chat_dir(project: Project) -> Path:
    return project.root / "chat"


def list_chats(project: Project, profile: str | None = None) -> list[dict]:
    out = []
    for path in sorted(chat_dir(project).glob("c_*.json")):
        try:
            chat = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if profile and chat.get("profile") not in (None, profile):
            continue
        out.append({"id": chat["id"], "title": chat.get("title") or "New chat", "updated": chat.get("updated"),
                    "messages": sum(m["role"] in ("user", "assistant") for m in chat["messages"])})
    return sorted(out, key=lambda c: c["updated"] or "", reverse=True)


def new_chat(project: Project, profile: str | None = None) -> dict:
    chat = {"id": _new_id("c"), "profile": profile, "title": "", "created": _now(), "updated": _now(),
            "messages": [], "proposals": {}, "summary": "", "summarized_upto": 0, "context_tokens": 0}
    save_chat(project, chat)
    return chat


def load_chat(project: Project, chat_id: str) -> dict:
    if not re.fullmatch(r"c_[0-9a-f]{8}", chat_id or ""):
        raise ProjectError("unknown chat")
    try:
        return json.loads((chat_dir(project) / f"{chat_id}.json").read_text(encoding="utf-8"))
    except OSError:
        raise ProjectError("unknown chat") from None


def save_chat(project: Project, chat: dict) -> None:
    folder = chat_dir(project)
    folder.mkdir(exist_ok=True)
    chat["updated"] = _now()
    tmp = folder / f"{chat['id']}.json.tmp"
    tmp.write_text(json.dumps(chat, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(folder / f"{chat['id']}.json")


def delete_chat(project: Project, chat_id: str) -> None:
    load_chat(project, chat_id)
    (chat_dir(project) / f"{chat_id}.json").unlink()


# ------------------------------------------------------------------ what the model sees

def system_prompt(chat: dict) -> str:
    try:
        guide = GUIDE.read_text(encoding="utf-8")
    except OSError:
        guide = ""
    text = INSTRUCTIONS + "\n# Prompt guide\n\n" + guide
    if chat.get("summary"):
        text += "\n\n# Summary of the earlier conversation\n\n" + chat["summary"]
    return text


def _scene_refs_label(project: Project, scene: dict) -> str:
    refs = project.scene_refs(scene)
    names = ", ".join("@" + r for r in refs) or "none"
    return f"defaults ({names})" if scene.get("refs") is None else names


def snapshot(project: Project, chat: dict | None = None) -> str:
    """The project as the model sees it in the newest message."""
    from .settings import DEFAULT_SETTINGS, merge

    d = project.data
    s = merge(DEFAULT_SETTINGS, d.get("settings") or {})
    items = d["library"]["items"]
    defaults, start = d["library"]["defaults"], d.get("start_image")
    lines = ["<project>", f"Name: {d['name']}",
             f"Settings: default scene length {s['scene_seconds']}s · {s['sampling']['steps']} steps · "
             f"{'size from the start image' if s['resolution']['mode'] == 'start_image' and start else str(s['resolution']['width']) + 'x' + str(s['resolution']['height'])}"
             f" · continuity {s['continuity']['frames']} frames"
             + (f" · video model {s['video_model']['mode']}" if (s.get("video_model") or {}).get("mode", "fl2va") != "fl2va" else ""),
             f"Start image: {'@' + start if start else 'none (scene 1 describes the opening setting in words)'}",
             "Style text:", '"""' + (d.get("style") or "").strip() + '"""',
             "Story notes:", '"""' + (d.get("notes") or "").strip() + '"""',
             "References (★ default, ▶ start image):"]
    if not items:
        lines.append("- none yet (the user adds them in the Library)")
    for name, item in items.items():
        marks = (" ▶" if name == start else "") + (" ★" if name in defaults else "")
        lines.append(f"- @{name} ({item['kind']}){marks}")
        for key, label in (("description", "description"), ("definition", "definition"), ("card", "card")):
            if (item.get(key) or "").strip():
                extra = " (written by you, not yet confirmed by the user)" if key == "card" and item.get("card_status") != "confirmed" else ""
                lines.append(f"    {label}{extra}: {item[key].strip()}")
    named = {item.get("asset") for item in items.values()}
    unnamed = [a for a in project.assets.values() if a["id"] not in named]
    if unnamed:
        lines.append("Library items without an @name (not usable as references until the user names them; audio can be a scene's sound clip):")
        for a in unnamed[:40]:
            extra = f", {a['duration']:.1f}s" if a.get("duration") else ""
            lines.append(f"- {a['id']} ({a['kind']}, {a['role']}{extra}): \"{a.get('title', '')}\"")
        if len(unnamed) > 40:
            lines.append(f"- … and {len(unnamed) - 40} more")
    scenes = project.scenes
    total = sum(sc.get("seconds") or s["scene_seconds"] for sc in scenes)
    lines.append(f"Scenes: {len(scenes)}, {total:g}s planned")
    for i, sc in enumerate(scenes, 1):
        take = d["takes"].get(sc.get("selected_take") or "")
        takes = project.takes_for(sc["id"])
        badges = ", ".join(project.scene_status(sc["id"])) or "ok"
        take_label = f"Take {take.get('number')} selected ({take['status']})" if take else "no take selected"
        lines.append(f"{i}. \"{sc.get('title') or ''}\" · {sc.get('seconds') or s['scene_seconds']}s · join {sc['join'] if i > 1 else 'start'}"
                     f" · refs: {_scene_refs_label(project, sc)} · {len(takes)} take(s), {take_label} · status: {badges}")
        if sc.get("start_image") is not None:
            lines.append("   starts: " + (f"from image @{sc['start_image']}" if sc["start_image"] else "fresh from the references (no start image)"))
        clips = sc.get("audio_clips") or []
        if clips:
            lines.append("   sound clips (the soundtrack becomes the clip from that time): " + ", ".join(
                f"\"{(project.assets.get(c['asset']) or {}).get('title', c['asset'])}\" at {c['at']:g}s" for c in clips))
        lines.append('   text: """' + (sc.get("text") or "").strip() + '"""')
    if not scenes:
        lines.append("(no scenes yet)")
    pending = [p for p in (chat or {}).get("proposals", {}).values() if p["status"] == "pending"]
    if pending:
        lines.append("Your proposals waiting for the user (not applied yet; their new scenes get numbers only when applied):")
        for p in pending:
            titles = ", ".join(f"\"{s.get('title') or ''}\"" for s in p["args"].get("scenes") or []) if p["tool"] == "add_scenes" else ""
            lines.append(f"- {p['summary']}" + (f": {titles}" if titles else ""))
    lines.append("</project>")
    return "\n".join(lines)


def _image_part(project: Project, rel: str) -> dict | None:
    from PIL import Image

    path = (project.root / rel).resolve()
    if project.root.resolve() not in path.parents or not path.is_file():
        return None
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((IMAGE_EDGE, IMAGE_EDGE))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}}


def build_messages(project: Project, chat: dict, provider: dict, *, with_snapshot: bool = True) -> list[dict]:
    """The request messages. with_snapshot=False gives the stable part only (used to prime the cache)."""
    vision = bool(provider.get("vision"))
    history = chat["messages"][chat.get("summarized_upto", 0):]
    last_user = max((i for i, m in enumerate(history) if m["role"] == "user"), default=-1)
    out = [{"role": "system", "content": system_prompt(chat)}]
    events: list[str] = []
    for i, m in enumerate(history):
        role = m["role"]
        if role == "event":
            events.append(m["text"])
        elif role == "user":
            text = m["text"] + (f"\n\n{m['note']}" if m.get("note") else "")
            if events:
                text = "[Since your last message: " + " ".join(events) + "]\n\n" + text
                events = []
            if with_snapshot and i == last_user:
                text += "\n\n" + (m.get("snapshot") or "")
            parts = [p for p in (_image_part(project, r) for r in m.get("images", [])) if p] if vision else []
            out.append({"role": "user", "content": [*parts, {"type": "text", "text": text}] if parts else text})
        elif role == "assistant":
            msg = {"role": "assistant", "content": m.get("text") or ""}
            if m.get("tool_calls"):
                msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                      "function": {"name": c["name"], "arguments": c["arguments"]}} for c in m["tool_calls"]]
            out.append(msg)
        elif role == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["text"]})
        elif role == "attachment":  # images a tool showed to the model
            parts = [p for p in (_image_part(project, r) for r in m.get("images", [])) if p] if vision else []
            out.append({"role": "user", "content": [*parts, {"type": "text", "text": m["text"]}] if parts else m["text"]})
    return out


# ------------------------------------------------------------------ tools

def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": {"type": "object", "properties": properties, "required": required}}}


_SCENE = {"type": "integer", "description": "scene number (1 = first)"}
_REFS = {"type": ["array", "null"], "items": {"type": "string"},
         "description": "reference names without @ used by this scene; null = the project defaults"}
_SCENE_FIELDS = {
    "title": {"type": "string", "description": "short title"},
    "text": {"type": "string", "description": "the H3 scene text, written as the prompt guide says"},
    "seconds": {"type": "number", "description": "length in seconds, 1-15 (usually 5)"},
    "join": {"type": "string", "enum": ["continue", "cut"], "description": "how it joins the previous scene"},
    "refs": _REFS,
}

TOOLS = [
    _fn("get_scene", "Read one scene in full: text, references, the exact prompt sent to H3, and its takes.",
        {"scene": _SCENE}, ["scene"]),
    _fn("view_reference", "Look at a reference image (you will see the picture).",
        {"name": {"type": "string", "description": "reference name without @"}}, ["name"]),
    _fn("view_take", "Look at frames from a finished take of a scene (you will see 4 frames from start to end).",
        {"scene": _SCENE, "take": {"type": "integer", "description": "take number; omit for the selected take"}}, ["scene"]),
    _fn("update_notes", "Replace the project's story notes (your lasting memory: story, characters, tone, decisions). Applied at once.",
        {"notes": {"type": "string", "description": "the complete new notes"}}, ["notes"]),
    _fn("save_reference_card", "Save what a reference looks like, for your own later use (not sent to H3). Applied at once.",
        {"name": {"type": "string"}, "card": {"type": "string", "description": "precise visual features"}}, ["name", "card"]),
    _fn("add_scenes", "Propose new scenes (inserted in order after `after`, or at the end).",
        {"scenes": {"type": "array", "items": {"type": "object", "properties": _SCENE_FIELDS, "required": ["title", "text"]}},
         "after": {"type": "integer", "description": "insert after this scene number; omit to add at the end"}}, ["scenes"]),
    _fn("edit_scene", "Propose changes to one scene. Give only the fields that change.",
        {"scene": _SCENE, **_SCENE_FIELDS}, ["scene"]),
    _fn("remove_scene", "Propose removing a scene from the timeline (its takes stay on disk).", {"scene": _SCENE}, ["scene"]),
    _fn("move_scene", "Propose moving a scene to another position.",
        {"scene": _SCENE, "position": {"type": "integer", "description": "new scene number"}}, ["scene", "position"]),
    _fn("edit_project", "Propose project-wide changes: style text, default references, start image.",
        {"style": {"type": "string", "description": "the complete new style text"},
         "default_refs": {"type": "array", "items": {"type": "string"}, "description": "reference names used by scenes without their own list"},
         "start_image": {"type": ["string", "null"], "description": "image reference to use as the start image, or null for none"}}, []),
    _fn("set_reference_definition", "Propose a new definition for a reference (sent to H3 in scenes that use it).",
        {"name": {"type": "string"}, "definition": {"type": "string"}}, ["name", "definition"]),
    _fn("change_settings", "Propose generation settings changes.",
        {"steps": {"type": "integer", "description": "sampling steps (20 = proven quality, 8 = about 2.5x faster)"},
         "scene_seconds": {"type": "number", "description": "default scene length"},
         "continuity_frames": {"type": "integer", "enum": [5, 22, 39], "description": "frames carried between scenes"}}, []),
    _fn("render_scenes", "Propose rendering takes. The user confirms; rendering switches the GPU to video generation.",
        {"scenes": {"type": "array", "items": {"type": "integer"}, "description": "scene numbers to render"},
         "rest": {"type": "boolean", "description": "render every scene from the first one without a finished take, in order"}}, []),
]
PROPOSAL_TOOLS = {"add_scenes", "edit_scene", "remove_scene", "move_scene", "edit_project", "set_reference_definition",
                  "change_settings", "render_scenes"}


class _Dry(Project):
    """An in-memory copy for checking a proposal; never written to disk."""

    def save(self, *, snapshot: bool = True) -> None:
        pass


def _dry(project: Project) -> _Dry:
    return _Dry(project.root, copy.deepcopy(project.data))


def _scene_id(project: Project, number) -> str:
    try:
        n = int(number)
    except (TypeError, ValueError):
        raise ProjectError(f"scene must be a number, got {number!r}") from None
    if not 1 <= n <= len(project.scenes):
        raise ProjectError(f"there is no scene {n} (the project has {len(project.scenes)})")
    return project.scenes[n - 1]["id"]


def _clean_refs(project: Project, refs):
    if refs is None:
        return None
    if not isinstance(refs, list):
        raise ProjectError("refs must be a list of names or null")
    names = [str(r).lstrip("@") for r in refs]
    unknown = [n for n in names if n not in project.data["library"]["items"]]
    if unknown:
        raise ProjectError("unknown references: " + ", ".join("@" + n for n in unknown))
    return names


H3_FIELDS = ("integrated_multimodal_description:", "overall_soundscape:", "non_diegetic_music:")
SCRIPTS = {  # languages that must be written in their own script (prompt guide §5)
    "hindi": "\u0900-\u097F", "marathi": "\u0900-\u097F", "nepali": "\u0900-\u097F", "bengali": "\u0980-\u09FF",
    "gujarati": "\u0A80-\u0AFF", "punjabi": "\u0A00-\u0A7F", "tamil": "\u0B80-\u0BFF", "telugu": "\u0C00-\u0C7F",
    "kannada": "\u0C80-\u0CFF", "malayalam": "\u0D00-\u0D7F", "arabic": "\u0600-\u06FF", "urdu": "\u0600-\u06FF",
    "russian": "\u0400-\u04FF", "ukrainian": "\u0400-\u04FF", "greek": "\u0370-\u03FF", "hebrew": "\u0590-\u05FF",
    "thai": "\u0E00-\u0E7F", "japanese": "\u3040-\u30FF\u4E00-\u9FFF", "chinese": "\u4E00-\u9FFF", "korean": "\uAC00-\uD7AF",
}


_DTAG = re.compile(r"</?d>")
NEAR_DUPLICATE = 0.9  # word-sequence similarity from which a new scene counts as a copy of another


def _words(text: str) -> list[str]:
    return (text or "").lower().split()


def similarity(a: str, b: str) -> float:
    """How alike two scene texts are (0-1), ignoring case and whitespace."""
    wa, wb = _words(a), _words(b)
    if wa == wb:
        return 1.0
    if not wa or not wb:
        return 0.0
    return difflib.SequenceMatcher(None, wa, wb, autojunk=False).ratio()


def check_scene_text(text: str) -> None:
    """What every scene text needs before H3 sees it (prompt guide §4 and §5): the three fields once each,
    in order, at most one free line before them, [Shot 1], and every spoken line as <d>[Language] words</d>."""
    missing = [f for f in H3_FIELDS if f not in text]
    if missing:
        raise ProjectError("scene text must contain all three H3 fields, each on its own line — "
                           "'integrated_multimodal_description: [Shot 1] …', 'overall_soundscape: …', 'non_diegetic_music: … (or N/A)'; "
                           f"missing: {', '.join(missing)}. Write the whole scene in the text field")
    repeated = [f for f in H3_FIELDS if text.count(f) > 1]
    if repeated:
        raise ProjectError(f"each H3 field must appear exactly once; repeated: {', '.join(repeated)}")
    starts = [text.find(f) for f in H3_FIELDS]
    if starts != sorted(starts):
        raise ProjectError("the H3 fields must come in this order: integrated_multimodal_description:, overall_soundscape:, non_diegetic_music:")
    if "\n" in text[:starts[0]].strip():
        raise ProjectError("only one short line (the state the scene starts from) may come before integrated_multimodal_description:; "
                           "put everything else inside the fields")
    for field, start, end in zip(H3_FIELDS, starts, [*starts[1:], len(text)]):
        if not text[start + len(field):end].strip():
            raise ProjectError(f"{field} is empty" + (" (write N/A when there is no music)" if field == "non_diegetic_music:" else ""))
    if "[Shot 1]" not in text[starts[0]:starts[1]]:
        raise ProjectError("integrated_multimodal_description must start with [Shot 1]")
    opened = None
    for m in _DTAG.finditer(text):
        if m.group() == "<d>":
            if opened is not None:
                raise ProjectError(f"a spoken line is not closed: <d>{text[opened:m.start()][:60]} — end every spoken line with </d> "
                                   "(not <d>) before the next one starts")
            opened = m.end()
            continue
        if opened is None:
            raise ProjectError(f"</d> without an opening <d>: …{text[max(0, m.start() - 60):m.end()]}")
        line, opened = text[opened:m.start()], None
        tag = re.match(r"\s*\[([^\]]+)\]", line)
        if not tag:
            raise ProjectError(f"spoken line without a [Language] tag: <d>{line[:60]}</d>")
        script = SCRIPTS.get(tag.group(1).strip().lower())
        if script and not re.search(f"[{script}]", line):
            raise ProjectError(f"the {tag.group(1)} line must be written in {tag.group(1)}'s own script, not transliterated: <d>{line[:60]}</d>")
    if opened is not None:
        raise ProjectError(f"a spoken line is not closed: <d>{text[opened:][:60]} — end it with </d>")


JOIN_ALIASES = {"start": "cut", "fresh": "cut", "new": "cut", "none": "cut", "continuation": "continue", "continuous": "continue"}


def _style_names(project: Project) -> list[str]:
    """@names in the style text: it goes with every scene, so they must be active in every scene."""
    from .project import _TAG_RE

    items = project.data["library"]["items"]
    return list(dict.fromkeys(n for n in _TAG_RE.findall(project.data.get("style") or "") if n in items))


def _scene_fields(project: Project, args: dict, notes: list[str] | None = None) -> dict:
    """notes collects what was completed on the model's behalf (e.g. a style reference added to refs)."""
    fields = {}
    for key in ("title", "text"):
        if key in args and args[key] is not None:
            fields[key] = str(args[key])
    if "text" in fields:
        fields["text"] = normalize_scene_text(fields["text"])
        check_scene_text(fields["text"])
    if args.get("seconds") is not None:
        seconds = float(args["seconds"])
        if not 1 <= seconds <= 15:
            raise ProjectError("seconds must be between 1 and 15")
        fields["seconds"] = seconds
    if args.get("join") is not None:
        join = str(args["join"]).strip().lower()
        join = JOIN_ALIASES.get(join, join)
        if join not in JOIN_MODES:
            raise ProjectError(f"join must be one of {JOIN_MODES}, got {args['join']!r}")
        fields["join"] = join
    if "refs" in args:
        fields["refs"] = _clean_refs(project, args["refs"])
        missing = [n for n in _style_names(project) if fields["refs"] is not None and n not in fields["refs"]]
        if missing:
            fields["refs"] += missing
            if notes is not None:
                notes.append(", ".join("@" + n for n in missing))
    return fields


def _check_prompts(project: Project, scene_ids: list[str]) -> None:
    errors = []
    for sid in scene_ids:
        scene = project.scene(sid)
        try:
            project.compile_prompt(scene)
        except ProjectError as exc:
            msg = f"scene {project.scene_index(sid) + 1} \"{scene.get('title') or ''}\": {exc}"
            if "not active" in str(exc):
                active = ", ".join("@" + r for r in project.scene_refs(scene)) or "none"
                msg += (f" (its active references are: {active}; its refs must list every @name its text uses, "
                        f"or be null to use the defaults: {', '.join('@' + d for d in project.data['library']['defaults']) or 'none'})")
                from_style = [n for n in _style_names(project) if n not in project.scene_refs(scene) and "@" + n not in (scene.get("text") or "")]
                if from_style:
                    msg += (f". {', '.join('@' + n for n in from_style)} comes from the style text, which is sent with every scene, "
                            "so every scene needs it: give such scenes refs null, or add it to their refs, "
                            "or describe the look in the style text without the @name")
            errors.append(msg)
    if errors:
        raise ProjectError("; ".join(errors))


_ARTIFACT = re.compile(r"\s*</?(?:parameter|function|tool_call)(?:=[^>]*)?>\s*,?")
_FIELDS = re.compile(r"[ \t]*\n?[ \t]*\b(integrated_multimodal_description:|overall_soundscape:|non_diegetic_music:)")
_FIELD_SPELLING = re.compile(r"^[ \t]*(?:[-•][ \t]*)?\*{0,2}(integrated[ _]multimodal[ _]description|overall[ _]soundscape|non[ _-]diegetic[ _]music)"
                            r"\*{0,2}[ \t]*:\*{0,2}", re.I | re.M)
_SHOT = re.compile(r"\[\s*shot\s*(\d+)\s*\]", re.I)
_DTAG_SPELLING = re.compile(r"<\s*([/\\]?)\s*[dD]\s*(/?)\s*>")
_LANG_AHEAD = re.compile(r"\s*\[(?!shot\b)[^\]\n]{1,40}\]", re.I)


def clean_args(value):
    """Remove tool-call markup some models leak into argument strings."""
    if isinstance(value, str):
        return _ARTIFACT.sub("", value).strip()
    if isinstance(value, list):
        return [clean_args(v) for v in value]
    if isinstance(value, dict):
        return {k: clean_args(v) for k, v in value.items()}
    return value


def _close_dialogue(line: str) -> str:
    """Pair spoken-line tags written as <d>…<d>: a <d> while a line is open closes it, unless a [Language] tag
    follows (then it is a new line and the slip is ambiguous). Lines that still don't pair up are left alone."""
    out, last, opened = [], 0, False
    for m in _DTAG.finditer(line):
        tag = m.group()
        if tag == "<d>" and opened and not _LANG_AHEAD.match(line, m.end()):
            tag = "</d>"
        if (tag == "<d>") == opened:
            return line
        opened = tag == "<d>"
        out.append(line[last:m.start()] + tag)
        last = m.end()
    return line if opened else "".join(out) + line[last:]


def normalize_scene_text(text: str) -> str:
    """Repair trivial slips without changing a word: field names spelled with spaces, capitals or bold,
    [shot 1], dialogue tags such as </ d> or a closing <d>, a missing music line (N/A). Each H3 field goes on
    its own line; a field the model repeated is kept once (the first time)."""
    text = _FIELD_SPELLING.sub(lambda m: re.sub(r"[ -]", "_", m.group(1).lower()) + ":", text)
    text = _SHOT.sub(lambda m: f"[Shot {m.group(1)}]", text)
    text = _DTAG_SPELLING.sub(lambda m: "</d>" if m.group(1) or m.group(2) else "<d>", text)
    text = "\n".join(_close_dialogue(line) for line in text.split("\n"))
    text = _FIELDS.sub(lambda m: "\n" + m.group(1), text.strip()).strip()
    out, seen, skipping = [], set(), False
    for line in text.split("\n"):
        field = next((f for f in ("overall_soundscape:", "non_diegetic_music:", "integrated_multimodal_description:") if line.startswith(f)), None)
        if field:
            skipping = field in seen
            seen.add(field)
        if not skipping:
            out.append(line)
    text = "\n".join(out).strip()
    if "integrated_multimodal_description:" in text and "overall_soundscape:" in text and "non_diegetic_music:" not in text:
        text += "\nnon_diegetic_music: N/A"  # the guide's value for "no music"; the user sees it on the card
    return text


def _none_like(value) -> bool:
    return value is None or str(value).strip().lstrip("@").lower() in ("", "null", "none")


def _check_tags(project: Project, text: str, what: str) -> None:
    from .project import _TAG_RE

    unknown = sorted({t for t in _TAG_RE.findall(text) if t not in project.data["library"]["items"]})
    if unknown:
        raise ProjectError(f"{what} mentions references that do not exist: {', '.join('@' + u for u in unknown)} "
                           f"(existing: {', '.join('@' + n for n in project.data['library']['items']) or 'none'})")


def plan_proposal(project: Project, tool: str, args: dict, pending: list[dict] | None = None) -> dict:
    """Validate a proposal and resolve scene numbers to ids.
    Returns {tool, args (resolved), summary, details}; raises ProjectError for the model to fix.

    Scene numbers refer to the project as it is (what the model sees in the snapshot); scenes in pending
    proposals have no number until applied. The checks run on a dry copy with the chat's pending proposals
    applied first, in order, so proposals that only break together are caught."""
    dry = _dry(project)
    for earlier in pending or []:
        if earlier["tool"] != "render_scenes":
            try:
                _apply(dry, earlier)
            except (ProjectError, KeyError, ValueError, StopIteration):
                pass
    resolved: dict = {}
    details: list[dict] = []
    numbers = {s["id"]: i + 1 for i, s in enumerate(project.scenes)}
    if tool == "add_scenes":
        scenes = args.get("scenes") or []
        if not scenes:
            raise ProjectError("add_scenes needs at least one scene")
        after = _scene_id(project, args["after"]) if args.get("after") else None
        known = [(s["id"], s.get("title") or "", s.get("text") or "") for s in dry.scenes]
        resolved = {"after": after, "scenes": []}
        prev, added, completed = after, [], []
        for i, sc in enumerate(scenes):
            notes: list[str] = []
            fields = _scene_fields(dry, sc, notes)
            completed += [f"{n} to the refs of new scene {i + 1}" for n in notes]
            if not fields.get("text"):
                raise ProjectError("every scene needs text")
            new = dry.add_scene(after=prev, text=fields.get("text", ""), seconds=fields.get("seconds"),
                                join=fields.get("join", "continue"), refs=fields.get("refs"), title=fields.get("title", ""))
            resolved["scenes"].append(fields)
            added.append(new["id"])
            prev = new["id"]
            details.append({"label": f"New scene {dry.scene_index(new['id']) + 1}: {fields.get('title', '')}",
                            "meta": f"{fields.get('seconds') or 'default'}s · {fields.get('join', 'continue')} · refs: {_scene_refs_label(dry, new)}",
                            "after": fields["text"]})
        texts = [" ".join(_words(f["text"])) for f in resolved["scenes"]]
        twins = [f"{i + 1} and {texts.index(t) + 1}" for i, t in enumerate(texts) if texts.index(t) != i]
        if twins:
            raise ProjectError(f"new scenes {', '.join(twins)} have the same text; every scene must show something new "
                               "(to hold the last moment longer, make the last scene longer instead)")
        copies = []
        for i, f in enumerate(resolved["scenes"]):
            for sid, title, old in known:
                ratio = similarity(f["text"], old)
                if ratio >= NEAR_DUPLICATE:
                    where = (f"scene {numbers[sid]} \"{title}\"" if sid in numbers
                             else f"the new scene \"{title}\" in a proposal waiting for the user")
                    copies.append(f"new scene {i + 1} {'repeats' if ratio == 1 else 'is nearly the same as'} {where}")
                    break
        if copies:
            raise ProjectError("; ".join(copies) + ". Those scenes already exist or are already proposed: "
                               "only add the new scenes the user asked for, each showing something new")
        _check_prompts(dry, added)
        warnings = [f"added {'; '.join(completed)}, because the style text names it"] if completed else []
        short = [str(i + 1) for i, f in enumerate(resolved["scenes"]) if f.get("seconds") is not None and f["seconds"] < 3]
        if short:
            warnings.append(f"new scene(s) {', '.join(short)} are shorter than 3 seconds; H3 needs time for an action, "
                            "so only keep them if that is intended")
        if not (dry.data.get("style") or "").strip():
            warnings.append("the project has no style text yet (and none is waiting for the user); propose one with edit_project: "
                            "format and length, the look, who speaks and how, rules such as no readable writing")
        if re.search(r"\(S\d\)", dry.data.get("style") or "") and not any("<d>" in f["text"] for f in resolved["scenes"]):
            warnings.append("the style text defines a speaker (S1) but none of the new scenes has a spoken line <d>[Language] …</d>; "
                            "if the video has narration, give the scenes their lines")
        if warnings:
            resolved["warning"] = "; ".join(warnings)
        resolved["total"] = len(dry.scenes)
        where = f"after scene {project.scene_index(after) + 1}" if after else "at the end"
        summary = f"Add {len(added)} scene{'s' if len(added) > 1 else ''} {where}"
    elif tool == "edit_scene":
        sid = _scene_id(project, args.get("scene"))
        notes = []
        fields = _scene_fields(dry, {k: v for k, v in args.items() if k != "scene"}, notes)
        if not fields:
            raise ProjectError("edit_scene needs at least one field to change")
        if not any(s["id"] == sid for s in dry.scenes):
            raise ProjectError(f"scene {numbers[sid]} is removed by a proposal waiting for the user")
        before = copy.deepcopy(dry.scene(sid))
        dry.update_scene(sid, **fields)
        _check_prompts(dry, [sid])
        resolved = {"scene": sid, "fields": fields}
        if notes:
            resolved["warning"] = f"added {notes[0]} to the scene's refs, because the style text names it"
        n = project.scene_index(sid) + 1
        for key, value in fields.items():
            old = before.get(key)
            if key == "refs":
                old = _scene_refs_label(project, before)
                value = _scene_refs_label(dry, dry.scene(sid))
            details.append({"label": key, "before": "" if old is None else str(old), "after": "" if value is None else str(value)})
        summary = f"Edit scene {n} ({', '.join(fields)})"
    elif tool == "remove_scene":
        sid = _scene_id(project, args.get("scene"))
        resolved = {"scene": sid}
        summary = f"Remove scene {project.scene_index(sid) + 1} \"{project.scene(sid).get('title') or ''}\" from the timeline"
    elif tool == "move_scene":
        sid = _scene_id(project, args.get("scene"))
        position = int(args.get("position", 0))
        if not 1 <= position <= len(project.scenes):
            raise ProjectError(f"position must be 1-{len(project.scenes)}")
        resolved = {"scene": sid, "index": position - 1}
        summary = f"Move scene {project.scene_index(sid) + 1} to position {position}"
    elif tool == "edit_project":
        fields = {}
        if args.get("style") is not None:
            fields["style"] = str(args["style"])
            _check_tags(dry, fields["style"], "the style text")
            details.append({"label": "style text", "before": project.data.get("style", ""), "after": fields["style"]})
        if args.get("default_refs") is not None:
            fields["defaults"] = _clean_refs(dry, args["default_refs"])
            if fields["defaults"] == project.data["library"]["defaults"]:
                del fields["defaults"]
            else:
                details.append({"label": "default references", "before": ", ".join(project.data["library"]["defaults"]),
                                "after": ", ".join(fields["defaults"])})
        if "start_image" in args:
            start = None if _none_like(args["start_image"]) else str(args["start_image"]).lstrip("@")
            if start is not None and dry.data["library"]["items"].get(start, {}).get("kind") != "image":
                raise ProjectError(f"@{start} is not an image reference; start_image must be an existing image reference or null. "
                                   "You cannot create images.")
            if start != project.data.get("start_image"):
                fields["start_image"] = start
                details.append({"label": "start image", "before": project.data.get("start_image") or "none", "after": start or "none"})
        if not fields:
            raise ProjectError("edit_project would change nothing: give style, default_refs or start_image that differ from the current ones")
        ok_before = [s["id"] for s in dry.scenes if _compiles(dry, s)]  # a scene broken already does not block this
        for key, value in fields.items():
            if key == "defaults":
                dry.data["library"]["defaults"] = value
            else:
                dry.data[key] = value
        _check_prompts(dry, ok_before)
        resolved = fields
        summary = "Change " + ", ".join({"style": "the style text", "defaults": "the default references", "start_image": "the start image"}[k] for k in fields)
    elif tool == "set_reference_definition":
        name = str(args.get("name", "")).lstrip("@")
        item = dry.data["library"]["items"].get(name)
        if item is None:
            raise ProjectError(f"unknown reference @{name}")
        resolved = {"name": name, "definition": str(args.get("definition", ""))}
        _check_tags(dry, resolved["definition"], "the definition")
        details.append({"label": f"@{name} definition", "before": item.get("definition", ""), "after": resolved["definition"]})
        summary = f"New definition for @{name}"
    elif tool == "change_settings":
        overrides: dict = {}
        if args.get("steps") is not None:
            steps = int(args["steps"])
            if not 1 <= steps <= 60:
                raise ProjectError("steps must be 1-60")
            overrides.setdefault("sampling", {})["steps"] = steps
        if args.get("scene_seconds") is not None:
            overrides["scene_seconds"] = float(args["scene_seconds"])
        if args.get("continuity_frames") is not None:
            if int(args["continuity_frames"]) not in (5, 22, 39):
                raise ProjectError("continuity_frames must be 5, 22 or 39")
            overrides.setdefault("continuity", {})["frames"] = int(args["continuity_frames"])
        if not overrides:
            raise ProjectError("change_settings needs steps, scene_seconds or continuity_frames")
        resolved = {"settings": overrides}
        summary = "Change settings: " + json.dumps(overrides)
    elif tool == "render_scenes":
        if args.get("rest"):
            resolved = {"rest": True}
            summary = "Render the remaining scenes (in order, each continuing from the one before)"
        else:
            ids = [_scene_id(project, n) for n in args.get("scenes") or []]
            if not ids:
                raise ProjectError("render_scenes needs scene numbers or rest=true")
            _check_prompts(project, ids)  # renders run on the project as it is, not on pending proposals
            resolved = {"scenes": ids}
            summary = "Render a take of scene " + ", ".join(str(project.scene_index(i) + 1) for i in ids)
    else:
        raise ProjectError(f"unknown tool {tool}")
    return {"tool": tool, "args": resolved, "summary": summary, "details": details}


def apply_proposal(project: Project, proposal: dict, *, render=None) -> str:
    """Carry out an applied proposal on the real project. render(project, scene_ids | None) queues takes.

    The project may have changed since the proposal was checked (other proposals applied or rejected, the
    user's own edits), so it is tried on a dry copy first: it is refused if a scene it adds or rewrites, or
    one that rendered before, would no longer compile."""
    if proposal["tool"] not in ("render_scenes", "change_settings"):
        dry = _dry(project)
        ok_before = {s["id"] for s in dry.scenes if _compiles(dry, s)}
        ids_before = {s["id"] for s in dry.scenes}
        _apply(dry, proposal)
        a = proposal["args"]
        edited = a["scene"] if proposal["tool"] == "edit_scene" and {"text", "refs"} & set(a["fields"]) else None
        check = [s["id"] for s in dry.scenes if s["id"] in ok_before or s["id"] not in ids_before or s["id"] == edited]
        try:
            _check_prompts(dry, check)
        except ProjectError as exc:
            raise ProjectError(f"not applied, because afterwards {exc}. Apply the proposals this one depends on first "
                               "(for example the style text or a reference's definition), or ask for a corrected version") from None
    return _apply(project, proposal, render=render)


def _compiles(project: Project, scene: dict) -> bool:
    try:
        project.compile_prompt(scene)
        return True
    except ProjectError:
        return False


def _apply(project: Project, proposal: dict, *, render=None) -> str:
    tool, a = proposal["tool"], proposal["args"]
    if tool == "add_scenes":
        prev = a.get("after")
        if prev and not any(s["id"] == prev for s in project.scenes):
            raise ProjectError("the scene these were to follow no longer exists")
        for fields in a["scenes"]:
            prev = project.add_scene(after=prev, text=fields.get("text", ""), seconds=fields.get("seconds"),
                                     join=fields.get("join", "continue"), refs=fields.get("refs"), title=fields.get("title", ""))["id"]
    elif tool == "edit_scene":
        project.update_scene(a["scene"], **a["fields"])
    elif tool == "remove_scene":
        project.remove_scene(a["scene"])
    elif tool == "move_scene":
        project.move_scene(a["scene"], a["index"])
    elif tool == "edit_project":
        for key, value in a.items():
            if key == "defaults":
                project.data["library"]["defaults"] = value
            else:
                project.data[key] = value
        project.save()
    elif tool == "set_reference_definition":
        project.update_reference(a["name"], definition=a["definition"])
    elif tool == "change_settings":
        from .settings import merge

        project.data["settings"] = merge(project.data.get("settings") or {}, a["settings"])
        project.save()
    elif tool == "render_scenes":
        if render is None:
            raise ProjectError("rendering is not available here")
        return render(project, None if a.get("rest") else a["scenes"])
    return "applied"


def extract_frames(project: Project, take: dict, count: int = 4) -> list[str]:
    video = project.root / take["dir"] / "take.mp4"
    if not video.is_file():
        raise ProjectError("this take has no video yet")
    out_dir = chat_dir(project) / "frames" / take["id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = int(take.get("frames") or 120)
    skip = int(take.get("context_frames") or 0)  # carried-over frames belong to the previous scene
    rels = []
    for i in range(count):
        index = skip + round((frames - skip - 1) * (0.08 + 0.84 * i / (count - 1)))
        target = out_dir / f"f{i + 1}.jpg"
        if not target.is_file():
            proc = subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(video), "-vf", f"select=eq(n\\,{index})",
                                   "-frames:v", "1", "-q:v", "3", str(target)], capture_output=True)
            if proc.returncode != 0 or not target.is_file():
                raise ProjectError("could not extract frames: " + proc.stderr.decode(errors="replace")[-300:])
        rels.append(str(target.relative_to(project.root)))
    return rels


def run_read_tool(project: Project, tool: str, args: dict, provider: dict) -> tuple[str, list[str]]:
    """Read tools: (text result, images to show)."""
    if tool == "get_scene":
        sid = _scene_id(project, args.get("scene"))
        sc = project.scene(sid)
        try:
            compiled = project.compile_prompt(sc)["prompt"]
        except ProjectError as exc:
            compiled = f"ERROR: {exc}"
        takes = [f"Take {t.get('number')}: {t['status']}{' (selected)' if t['id'] == sc.get('selected_take') else ''}, seed {t.get('seed')}"
                 for t in project.takes_for(sid)]
        return (f"Scene {project.scene_index(sid) + 1} \"{sc.get('title') or ''}\" · {sc.get('seconds') or 'default'}s · join {sc['join']}"
                f" · refs: {_scene_refs_label(project, sc)}\nText:\n{sc.get('text') or ''}\n\nPrompt sent to H3:\n{compiled}\n\n"
                f"Takes: {'; '.join(takes) or 'none'}"), []
    if tool == "view_reference":
        name = str(args.get("name", "")).lstrip("@")
        item = project.data["library"]["items"].get(name)
        if item is None:
            raise ProjectError(f"unknown reference @{name}")
        if item["kind"] != "image":
            return f"@{name} is a {item['kind']} reference; it cannot be viewed.", []
        if not provider.get("vision"):
            return "This model cannot see images. Use the reference's definition and card.", []
        return f"Showing @{name}. The user's definition: {item.get('definition') or '(none)'}", [item["file"]]
    if tool == "view_take":
        sid = _scene_id(project, args.get("scene"))
        takes = project.takes_for(sid)
        if args.get("take"):
            take = next((t for t in takes if t.get("number") == int(args["take"])), None)
        else:
            take = project.data["takes"].get(project.scene(sid).get("selected_take") or "")
        if not take or take.get("status") != "done":
            raise ProjectError("that take does not exist or is not finished")
        if not provider.get("vision"):
            return "This model cannot see images.", []
        frames = extract_frames(project, take)
        return (f"Showing 4 frames, start to end, of scene {project.scene_index(sid) + 1} Take {take.get('number')} "
                f"({take.get('frames')} frames at 24 fps). The scene text it was made from:\n{take.get('scene_text', '')}"), frames
    raise ProjectError(f"unknown tool {tool}")


# ------------------------------------------------------------------ the agent

REPEAT = "You already proposed exactly this"
BIG_TASK = re.compile(r"\b(plan|storyboard|whole (video|story|ad)|all (the )?scenes|every scene|rewrite|restructure|"
                      r"from scratch|start over|\d+ (new )?scenes)\b", re.I)


def resolve_thinking(thinking, project: Project, text: str, provider: dict):
    """The thinking setting for one turn: False, a token budget, or "auto" — think (the provider's
    thinking_budget) when planning a video (no scenes yet, or the message asks for a plan or many scenes),
    answer directly for everything else."""
    if thinking == "auto":
        big = not project.scenes or bool(BIG_TASK.search(text or ""))
        return int(provider.get("thinking_budget") or 2048) if big else False
    return thinking


class Assistant:
    """Runs one user turn: request → tool calls → request … until the model answers in text.

    hooks: before_request() (e.g. load the local model), write(fn) (apply fn(project) safely and save),
    open() → fresh Project, update(state dict) for live progress, stopped() → bool.
    """

    def __init__(self, provider: dict, *, open, write, before_request=None, update=None, stopped=None):
        self.provider = provider
        self.client = Client(provider)
        self.open, self.write = open, write
        self.before_request = before_request or (lambda: None)
        self.update = update or (lambda state: None)
        self.stopped = stopped or (lambda: False)
        self.turn = None  # index of the user message being answered; proposals remember it

    def _complete(self, project: Project, chat: dict, state: dict, *, thinking=False, tools=TOOLS) -> dict:
        self.before_request()
        messages = build_messages(project, chat, self.provider)
        state.update(phase="thinking", text="", reasoning=0)
        self.update(state)

        def on_delta(kind, text):
            if kind == "content":
                state["text"] += text
                state["phase"] = "writing"
            else:
                state["reasoning"] += len(text)
            self.update(state)

        result = self.client.chat(messages, tools=tools, max_tokens=REPLY_TOKENS, thinking=thinking,
                                  on_delta=on_delta, should_stop=self.stopped)
        usage = result.get("usage") or {}
        if usage.get("prompt_tokens"):
            chat["context_tokens"] = int(usage["prompt_tokens"]) + int(usage.get("completion_tokens") or 0)
        timings = result.get("timings") or {}
        if timings:  # llama.cpp: how much of the prompt came from its cache
            chat["last_request"] = {k: timings.get(k) for k in ("prompt_n", "cache_n", "prompt_ms", "predicted_n", "predicted_per_second")}
        return result

    def run_turn(self, chat: dict, text: str, *, images: list[str] | None = None, thinking=False, note: str = "") -> dict:
        """note: extra context for the model that the chat panel does not show (e.g. what attached frames are)."""
        project = self.open()
        state = {"phase": "starting", "text": "", "reasoning": 0, "tool": None}
        if self.needs_compaction(chat):
            state["phase"] = "compacting"
            self.update(state)
            self.compact(project, chat)
        chat["messages"].append({"role": "user", "text": text, "note": note, "images": images or [], "t": _now(),
                                 "snapshot": snapshot(project, chat)})
        self.turn = len(chat["messages"]) - 1
        if not chat.get("title"):
            chat["title"] = text.strip().splitlines()[0][:60] if text.strip() else "New chat"
        self.write_chat(chat)
        thinking = resolve_thinking(thinking, project, text, self.provider)
        tools = TOOLS
        for _ in range(MAX_ROUNDS):
            project = self.open()
            result = self._complete(project, chat, state, thinking=thinking, tools=tools)
            if thinking and result["finish_reason"] == "length" and not result["tool_calls"]:
                # the budget ran out and the model went on reasoning in its reply until the limit: ask once more without thinking
                result = self._complete(project, chat, state, thinking=False, tools=tools)
            chat["messages"].append({"role": "assistant", "text": result["content"].strip(), "reasoning": result["reasoning"],
                                     "tool_calls": result["tool_calls"], "t": _now()})
            self.write_chat(chat)
            if result["finish_reason"] == "stopped" or not result["tool_calls"] or tools is None:
                break
            start = len(chat["messages"])
            for call in result["tool_calls"]:
                state.update(phase="tool", tool=call["name"])
                self.update(state)
                self.handle_tool(chat, call)
            self.write_chat(chat)
            if all(REPEAT in m["text"] for m in chat["messages"][start:] if m["role"] == "tool"):
                tools = None  # it only repeated waiting cards: one last request without tools, so it answers the user
        return chat

    def write_chat(self, chat: dict) -> None:
        save_chat(self.open(), chat)

    def handle_tool(self, chat: dict, call: dict) -> None:
        name = call["name"]
        images: list[str] = []
        proposal_id = None
        try:
            args = json.loads(call["arguments"] or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
            args = clean_args(args)
        except ValueError as exc:
            result = f"ERROR: the arguments are not valid JSON ({exc}). Call the tool again with valid JSON."
        else:
            try:
                project = self.open()
                if name in PROPOSAL_TOOLS:
                    pending = [p for p in chat.get("proposals", {}).values() if p["status"] == "pending"]
                    replaced = []
                    if name == "add_scenes":  # a corrected version of a proposal from this same answer replaces it
                        try:
                            after = _scene_id(project, args["after"]) if args.get("after") else None
                        except ProjectError:
                            after = "?"
                        replaced = [p for p in pending if p["tool"] == "add_scenes" and p.get("turn") is not None
                                    and p.get("turn") == self.turn and p["args"].get("after") == after]
                    elif name == "edit_scene":  # a later edit of the same scene in this answer is merged into the earlier card
                        try:
                            sid = _scene_id(project, args.get("scene"))
                        except ProjectError:
                            sid = None
                        replaced = [p for p in pending if p["tool"] == "edit_scene" and p.get("turn") is not None
                                    and p.get("turn") == self.turn and p["args"]["scene"] == sid]
                        for p in replaced:
                            args = {**p["args"]["fields"], **args}
                    plan = plan_proposal(project, name, args, [p for p in pending if p not in replaced])
                    same = next((p for p in pending if p["tool"] == name and p["args"] == plan["args"]), None)
                    if same:
                        result = f"{REPEAT} ({same['summary']}); it is waiting for the user. Don't propose it again."
                    else:
                        proposal_id = _new_id("p")
                        chat.setdefault("proposals", {})[proposal_id] = {"id": proposal_id, **plan, "status": "pending",
                                                                         "created": _now(), "turn": self.turn}
                        result = f"Proposed to the user as a card ({plan['summary']}). It is applied only when they click Apply."
                        for p in replaced:
                            p.update(status="replaced", replaced_by=proposal_id)
                        if replaced:
                            result += (f" It replaces your earlier proposal ({'; '.join(p['summary'] for p in replaced)})"
                                       + (" and includes its changes." if name == "edit_scene" else "."))
                        pending = [p for p in pending if p not in replaced]
                        if plan["args"].get("total") is not None:
                            result += f" If applied (with your other waiting proposals), the project has {plan['args']['total']} scenes."
                        if plan["args"].get("warning"):
                            result += f" Warning: {plan['args']['warning']}. If that was a mistake, propose a corrected version."
                        if name == "add_scenes" and any(p["tool"] == "add_scenes" for p in pending):
                            result += (" Note: another add_scenes proposal is also waiting; if both are applied, both sets of scenes are added."
                                       " Only propose more scenes if the user asked for them.")
                elif name == "update_notes":
                    notes = str(args.get("notes", ""))
                    self.write(lambda p: (p.data.__setitem__("notes", notes), p.save()))
                    result = "Story notes saved."
                elif name == "save_reference_card":
                    ref = str(args.get("name", "")).lstrip("@")
                    card = str(args.get("card", ""))
                    self.write(lambda p: p.update_reference(ref, card=card, card_status="model"))
                    result = f"Card for @{ref} saved (marked as written by you until the user confirms it)."
                else:
                    result, images = run_read_tool(project, name, args, self.provider)
            except (ProjectError, KeyError, ValueError, TypeError) as exc:
                result = f"ERROR: {exc}. Fix it and call the tool again if needed."
                if str(exc).startswith("there is no scene") and any(
                        p["tool"] == "add_scenes" and p["status"] == "pending" for p in chat.get("proposals", {}).values()):
                    result += (" Scenes in your add_scenes proposal that waits for the user have no number yet, so edit_scene cannot reach them;"
                               " to change them, call add_scenes again with the same `after` and the corrected scenes (it replaces the card)"
                               " — but only in the same answer, and only if something is wrong.")
        chat["messages"].append({"role": "tool", "tool_call_id": call["id"], "name": name, "text": result,
                                 "proposal": proposal_id, "t": _now()})
        if images:
            chat["messages"].append({"role": "attachment", "images": images, "text": f"(images from {name})", "t": _now()})

    # ------------------------------------------------------------ compaction

    def needs_compaction(self, chat: dict) -> bool:
        context = int(self.provider.get("context") or 32768)
        return chat.get("context_tokens", 0) > COMPACT_AT * context and len(chat["messages"]) - chat.get("summarized_upto", 0) > KEEP_RECENT

    def compact(self, project: Project, chat: dict) -> bool:
        """Summarise older turns into chat['summary']; the messages stay on disk and in the panel."""
        msgs = chat["messages"]
        start = chat.get("summarized_upto", 0)
        cut = len(msgs) - KEEP_RECENT
        while cut > start and msgs[cut]["role"] != "user":  # never split a tool call from its result
            cut -= 1
        if cut <= start:
            return False
        lines = []
        for m in msgs[start:cut]:
            if m["role"] == "user":
                lines.append("USER: " + m["text"])
            elif m["role"] == "assistant":
                calls = "; ".join(f"{c['name']}({c['arguments'][:300]})" for c in m.get("tool_calls") or [])
                lines.append("ASSISTANT: " + (m.get("text") or "") + (f" [tools: {calls}]" if calls else ""))
            elif m["role"] == "tool":
                lines.append(f"TOOL {m.get('name')}: {m['text'][:400]}")
            elif m["role"] == "event":
                lines.append("EVENT: " + m["text"])
        request = [
            {"role": "system", "content": "You summarise a conversation between a user and the assistant that helps them direct an AI video in Clapper. "
                                          "Keep: what the user wants (story, style, length, language), decisions made, what was proposed and whether it was applied, "
                                          "the user's likes and dislikes, open questions and next steps. Drop pleasantries and anything already visible in the project itself. "
                                          "Write compact bullet points."},
            {"role": "user", "content": (f"Earlier summary:\n{chat['summary']}\n\n" if chat.get("summary") else "")
                                        + "Conversation to add:\n" + "\n".join(lines)}]
        self.before_request()
        result = self.client.chat(request, max_tokens=1500, thinking=False)
        chat["summary"] = result["content"].strip()
        chat["summarized_upto"] = cut
        chat["context_tokens"] = 0
        chat.setdefault("compactions", []).append({"at": _now(), "upto": cut})
        save_chat(project, chat)
        return True


def describe_reference(provider: dict, project: Project, name: str) -> str:
    """One look at a reference image; returns a card the user reviews before saving."""
    item = project.data["library"]["items"].get(name)
    if item is None or item["kind"] != "image":
        raise ProjectError(f"@{name} is not an image reference")
    if not provider.get("vision"):
        raise LLMError("the active model cannot see images")
    known = "\n".join(f"{k}: {item[k]}" for k in ("description", "definition") if (item.get(k) or "").strip())
    question = ("This image is a reference for an AI video. Write a precise visual card for it: shape and build, every colour "
                "(eyes and irises, hair, skin, clothes, beak, feet …), markings, objects or symbols on it, outline and rendering style. "
                "Short lines, facts only. Mark anything you are not sure about with (?)."
                + (f"\n\nThe user already wrote this about it; it is reliable, but check it against the image and add what it misses:\n{known}" if known else ""))
    part = _image_part(project, item["file"])
    result = Client(provider).chat([{"role": "user", "content": [part, {"type": "text", "text": question}]}], max_tokens=1500, thinking=False)
    return result["content"].strip()

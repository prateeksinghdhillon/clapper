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
import os
import json
import re
import secrets
import subprocess
import time
from pathlib import Path

from .export import FFMPEG
from .llm import CONTEXT_ERROR, Client, HTTPError, LLMError, learn_request_limit, request_limit
from .project import JOIN_MODES, Project, ProjectError

GUIDE = Path(__file__).resolve().parents[2] / "docs" / "PROMPT_GUIDE.md"
MAX_ROUNDS = 10
KEEP_RECENT = 8          # messages kept word for word when older ones are summarised
COMPACT_AT = 0.6         # share of the context window that triggers compaction
IMAGE_EDGE = 1024        # long edge of images sent to the model (about 1000 vision tokens)
REPLY_TOKENS = 6000

INSTRUCTIONS = """You are the director's assistant inside Clapper, a studio for making AI videos with the MiniMax H3 video model, scene by scene. You plan the story, create the characters and places, write each scene's H3 prompt and dialogue, choose references and settings, and review finished takes.

# What you receive with every message
1. Sometimes a line "Changed in the project since your last message": edits the user made themselves or cards they applied. Trust the project block over your memory of the chat.
2. The whole project between <project> tags, read fresh for this message: settings, style text, story notes, the cast (characters and places with their @names, descriptions, looks and voices), voice samples, references, every scene in full with how it starts, its references, sound clips and takes, and the proposals still waiting. Scenes are numbered from 1.
3. Sometimes attached frames, described in a line "[Attached, in this order: …]": the real pictures of the video — the last frame of the scene a scene continues from, and frames of a scene's selected take.
4. Last, under "# The user's message", what the user wants now. Answer that.

# How Clapper works
- A project is one video: an ordered list of scenes (usually 5 seconds, 1 to 15). Each render of a scene is a take; the user picks one take per scene; the export joins them. Workspaces: Studio (the material), Scenes (the storyboard), Finish (upscale, music, overlays, export).
- Characters, places, props and styles live in the Studio. Each has a name; a description of what stays the same in every look (age, build, face, hair, colours; for places the layout, materials, light) — added to every image of it and sent to H3 as its definition; looks (outfits, angles, variations) with one main look, which is what its @name means in scenes; and for characters a voice. A new outfit, pose or angle is a new look (make_images), never a new description.
- A scene starts in one of three ways: "continue" (default) carries on from the last 22 frames and sound of the previous scene's take — the new scene begins exactly at that picture; "fresh" is a cut to a new shot; "image" opens exactly on an image such as a start frame made in the Studio.
- References are @names; Clapper turns them into <Picture N>/<Audio N> per scene. Write @name, never <Picture N>. A scene's text may only use @names in its references (plus its start image). Give a scene only the references that should be visible in it.
- Sound: a voice sample in a scene's references makes H3 speak new words in that voice. A sound clip (an audio item, e.g. a line made with speak_line) becomes the scene's soundtrack word for word from a given second; write the same words in the scene's <d> line, same speaker, same time.
- The style text goes with every scene: format, look, voices, rules. Nothing scene-specific goes there.
- An image takes seconds, a spoken line a few seconds, a scene minutes of GPU time.

# How you act
- Every change is a proposal: a tool call that becomes a card the user applies or rejects. You never change the project yourself. After proposing, say in one or two sentences what you proposed; the card shows the text.
- Images and speech are made only after the user applies their card and appear in the project a little later. Say "once you apply the card". Use a new character's @name only once the project shows it.
- Change only what was asked. A change to one scene is one edit_scene call for that scene. A new video is ONE add_scenes call with all its scenes.
- Scene numbers mean the scenes in <project>. Scenes in a waiting add_scenes card have no number yet; to correct that card in the same answer, call add_scenes again with the same `after`.
- Describe or judge only images and takes you have been shown (attached, or by view_reference / view_take). The user's descriptions and definitions beat your eyes.
- Keep lasting facts (story, characters, tone, the user's decisions) in the story notes with update_notes, always as the complete notes.
- Be brief. Ask one short question when something important is unclear instead of guessing.

# Steps for each kind of request
- A question: answer from the project block and the attached frames.
- Write a scene after a particular take ("continue from Take 5 of scene 3"): the next scene always continues from the SELECTED take. If that take is not selected, propose select_take first, then write the next scene from that take's last frame (attached, or view_take with take and part=end). get_scene shows the text each take was made from.
- Go back to an earlier take's version ("Take 2's prompt was better"): to use that take's video, select_take; to change or re-render that version, edit_scene with from_take (it puts back the take's text, references and length exactly — don't retype it).
- Fix or rewrite a scene: (1) read it and the scenes before and after it in <project>; (2) look at how the previous take really ends — the attached last frame, or view_take with part=end; (3) decide the four things in the scene checklist at the end; (4) one edit_scene with the new text (and refs, start, seconds if they change). If its selected take was rendered from an earlier text, say so.
- A new scene: the same steps, then add_scenes with `after`.
- A take that looks wrong: when the user means a newer take than the selected one (the project lists it as "newest"), look at that take (view_take with its number). Compare its frames with its text, say first what already matches, then what differs, then propose an edit_scene that makes the text clearer — usually less action, a clearer camera position, or fewer references.
- New material: create_character for a new character or place (a precise description, without repeating its name); make_images for new looks, angles or start frames; speak_line for exact dialogue in a character's voice; edit_character to change a description or voice.
- A new video: agree on length, shape (portrait 9:16 or landscape 16:9), language and characters if unclear; style text (edit_project, with a line "subject_definitions:"); the cast with create_character; once they have main looks, start frames and lines; then all scenes in one add_scenes call.
"""

# The prompt guide in brief, for providers whose request limit is too small for the whole guide (lean mode).
GUIDE_SHORT = """- Clapper builds each take's prompt: the style text, then the definitions of the scene's references, then "Continuation of Chunk N." for continued scenes, then the scene text; every @name becomes <Picture N>/<Audio N>.
- Style text: only what is always true (format and flow, look, voices, rules such as "no readable writing"). No one-time actions, dialogue or endings. Avoid changing it once takes exist.
- References: a scene uses only the references that should be visible; anyone named in the text must be in its refs. Definitions say what the reference is ("@aoi is Aoi: …").
- Scene text has three fields: integrated_multimodal_description ([Shot 1], the action, the camera, spoken lines), overall_soundscape (ambience, action sounds, non-verbal sounds), non_diegetic_music (or N/A).
- One main action per scene, plus at most a small reaction; state where the camera is and which way people move.
- A continued scene starts from the picture the previous take really ends on: its first sentence describes that state.
- Spoken words: <d>[Language] exact words</d> with a named speaker; about one or two short sentences per 5 seconds. An off-screen narrator: visible characters keep their lips closed.
- Sound: one clear sound early in the scene works well; keep music continuous across scenes ("the same score continues").
- Signs, papers and screens show pictures and plain lines, no readable writing."""

SCENE_CHECKLIST = """# Before you write any scene text
Decide these four things first; then write the three H3 fields.
1. Where it starts: for a continued scene, the exact picture the previous take ends on (look at it). The first sentence names that state when it matters ("The bus is pulling in beside the shelter, its side close to the camera.").
2. The ONE main action of these seconds, plus at most a small reaction. Five beats ("walks to the door, climbs in, stands in the aisle, looks back, sees him smile") are too many for 5 seconds: split them over scenes.
3. Where the camera is and which way people move — above all when someone enters or leaves something: to get on a bus, the camera stays outside behind her and she moves away from us into it.
4. Who is visible: only those characters in refs; anyone named in the text must be in refs as @name.

Good continued scene (5 s):
integrated_multimodal_description: [Shot 1] The bus is pulling in beside the shelter, its side close to the camera. It stops and its folding door hisses open beside @aoi. The camera stays on the wet platform, behind her. She lowers her umbrella and climbs the two steps into the bus, her back to the camera, moving away from us. Medium shot from behind, the camera follows her to the doorway and stops, one continuous shot.
overall_soundscape: In the first second, the loud hiss of the bus door. Then her sneakers on the metal steps, the idling engine and steady rain on the roof.
non_diegetic_music: The same soft piano continues.
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
    tmp = folder / f"{chat['id']}.json.{os.getpid()}.{secrets.token_hex(3)}.tmp"   # never shared by two writers
    tmp.write_text(json.dumps(chat, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(folder / f"{chat['id']}.json")


def delete_chat(project: Project, chat_id: str) -> None:
    load_chat(project, chat_id)
    (chat_dir(project) / f"{chat_id}.json").unlink()


# ------------------------------------------------------------------ what the model sees

def system_prompt(chat: dict, *, lean: bool = False) -> str:
    """lean: the prompt guide in brief, for providers with a small request limit."""
    if lean:
        guide = GUIDE_SHORT
    else:
        try:
            guide = GUIDE.read_text(encoding="utf-8")
        except OSError:
            guide = GUIDE_SHORT
    # the checklist comes last: models follow what they read last most closely
    text = INSTRUCTIONS + ("\n# Prompt guide (in brief)\n\n" if lean else "\n# Prompt guide (reference)\n\n") + guide + "\n\n" + SCENE_CHECKLIST
    if chat.get("summary"):
        text += "\n\n# Summary of the earlier conversation\n\n" + chat["summary"]
    return text


BADGE_WORDS = {
    "no_take": "no take yet", "rendering": "a take is rendering now",
    "prompt_changed": "its selected take was rendered from an EARLIER version of this text (the video shows the old version; get_scene shows that text)",
    "refs_changed": "its selected take was rendered with other references",
    "pictures_changed": "a reference picture changed after its selected take (e.g. a new main look); the video shows the old one",
    "continuity_changed": "the scene before it now has another selected take, so the join may jump",
    "parent_missing": "the scene before it has no take", "prompt_error": "its text does not compile",
}


def scene_fingerprints(project: Project) -> dict:
    """What the assistant last saw of each scene, to tell it what changed outside the chat."""
    out = {}
    for i, sc in enumerate(project.scenes, 1):
        take = project.data["takes"].get(sc.get("selected_take") or "") or {}
        out[sc["id"]] = {"n": i, "title": sc.get("title") or "", "text": hashlib_sha(sc.get("text") or ""),
                         "setup": hashlib_sha(json.dumps([sc.get("refs"), sc.get("join"), sc.get("start_image"), sc.get("audio_clips"), sc.get("seconds")])),
                         "take": take.get("id"), "take_number": take.get("number"), "take_status": take.get("status"),
                         "takes": {t["id"]: [t.get("number"), t.get("status"), bool(t.get("upscaled"))] for t in project.takes_for(sc["id"])}}
    d = project.data
    h = lambda v: hashlib_sha(json.dumps(v, sort_keys=True, default=str))   # noqa: E731
    out[PROJECT_KEY] = {
        "style": h(d.get("style") or ""), "notes": h(d.get("notes") or ""), "defaults": h(d["library"]["defaults"]),
        "start": h(d.get("start_image")), "settings": h(d.get("settings") or {}), "finish": h(d.get("finish") or {}),
        "refs": {n: h([it.get("definition"), it.get("asset") or it.get("file"), it.get("card")]) for n, it in d["library"]["items"].items()},
        "cast": {e["id"]: [e["name"], h([e.get("description"), e.get("main"), e.get("voice")])] for e in project.entities.values()}}
    return out


PROJECT_KEY = "_project"
_PROJECT_WORDS = {"style": "the style text was edited", "notes": "the story notes were edited", "defaults": "the default references changed",
                  "start": "the project start image changed", "settings": "the generation settings changed",
                  "finish": "the Finish (music, overlays, title cards) changed"}


def hashlib_sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def changes_since(before: dict | None, after: dict) -> list[str]:
    """Plain sentences: what changed in the scenes since the assistant's last turn (by the user or by applied cards)."""
    if before is None:
        return []
    out = []
    pa, pb = after.get(PROJECT_KEY), before.get(PROJECT_KEY)
    if pa and pb:
        out += [words for key, words in _PROJECT_WORDS.items() if pa[key] != pb[key]]
        for name in pa["refs"]:
            if name not in pb["refs"]:
                out.append(f"reference @{name} was added")
            elif pa["refs"][name] != pb["refs"][name]:
                out.append(f"reference @{name} changed (its picture, definition or card)")
        out += [f"reference @{name} was removed" for name in pb["refs"] if name not in pa["refs"]]
        for eid, (name, fp) in pa["cast"].items():
            if eid not in pb["cast"]:
                out.append(f"{name} was added to the cast")
            elif pb["cast"][eid] != [name, fp]:
                out.append(f"{name} changed (name, description, main look or voice)")
        out += [f"{name} was removed from the cast" for eid, (name, _) in pb["cast"].items() if eid not in pa["cast"]]
    for sid, a in after.items():
        if sid == PROJECT_KEY:
            continue
        b = before.get(sid)
        label = f"scene {a['n']}" + (f" \"{a['title']}\"" if a["title"] else "")
        if b is None:
            out.append(f"{label} was added")
            continue
        if b["text"] != a["text"]:
            out.append(f"{label}: its text was edited (read it again below)")
        if b["setup"] != a["setup"]:
            out.append(f"{label}: its references, start, length or sound clips changed")
        if a["take"] and (b["take"] != a["take"] or b["take_status"] != a["take_status"]) and a["take_status"] == "done":
            out.append(f"{label}: Take {a['take_number']} is now its finished, selected take")
        if b["n"] != a["n"] and b["text"] == a["text"]:
            out.append(f"{label} moved (it was scene {b['n']})")
        if b.get("takes") is not None:
            for tid, (number, status, upscaled) in a["takes"].items():
                old = b["takes"].get(tid)
                if tid == a["take"] and status == "done" and (old is None or old[1] != "done"):
                    continue   # reported above as its finished, selected take
                if (old is None or old[1] != status) and status in ("done", "failed", "interrupted"):
                    out.append(f"{label}: Take {number} " + {"done": "finished (not selected)", "failed": "FAILED",
                                                            "interrupted": "was interrupted"}[status])
                elif old is None and status in ("queued", "rendering"):
                    out.append(f"{label}: Take {number} is rendering")
                if upscaled and old is not None and not old[2]:
                    out.append(f"{label}: Take {number} was upscaled")
    for sid, b in before.items():
        if sid == PROJECT_KEY:
            continue
        if sid not in after:
            out.append(f"scene {b['n']}" + (f" \"{b['title']}\"" if b["title"] else "") + " was removed")
    return out


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
             + (f" · video model {s['video_model']['mode']}" if (s.get("video_model") or {}).get("mode", "fl2va") != "fl2va" else "")
             + (f" · video shape {s['resolution'].get('aspect')}" if s["resolution"].get("aspect", "start_image") != "start_image" else ""),
             *([f"Project start image (scene 1 opens on it unless it has its own): @{start}"] if start else
               [] if any(sc.get("start_image") for sc in project.scenes) else ["Start image: none — scene 1 describes its opening in words"]),
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
    ents = list(project.entities.values())
    if ents:
        lines.append("Cast & world (characters, places, props, styles; their main look is their @name):")
        for e in ents:
            looks = project.entity_looks(e["id"])
            main = project.assets.get(e.get("main") or "")
            voice = project.assets.get(e.get("voice") or "")
            lines.append(f"- {e['name']} ({e['kind']}) · " + (f"@{e['ref']}" if main else "no main look yet (no @name until it has one)")
                         + f" · {len(looks)} look(s)" + (f": " + ", ".join(f"{'★' if x['id'] == e.get('main') else ''}{x.get('look') or x['title']}" for x in looks[:8]) if looks else "")
                         + (f" · voice {voice['id']} \"{voice['title']}\"" if voice else (" · no voice" if e["kind"] == "character" else ""))
                         + ((" · has a character sheet" + (" (scenes use it as its reference)" if e.get("ref_use") == "sheet" else ""))
                            if e.get("sheet") in project.assets else ""))
            if e.get("description"):
                lines.append(f"    description: {e['description']}")
    voices = [a for a in project.assets.values() if a["kind"] == "audio" and a.get("role") == "voice"]
    if voices:
        lines.append("Voice samples (use the id for a character's voice or speak_line): " + "; ".join(
            f"{a['id']} \"{a['title']}\"" + (f" {a['duration']:.1f}s" if a.get("duration") else "") for a in voices[:20]))
    busy = [j for j in (d.get("jobs") or {}).values() if j["status"] in ("queued", "running") and j.get("kind") != "upscale"]
    if busy:
        lines.append(f"Being generated now: {len(busy)} job(s) — " + "; ".join((j.get("title") or j["tool"]) for j in busy[:6]))
    named = {item.get("asset") for item in items.values()}
    unnamed = [a for a in project.assets.values() if a["id"] not in named]
    if unnamed:
        lines.append("Library items without an @name (not usable as references until the user names them; audio can be a scene's sound clip):")
        for a in unnamed[:40]:
            extra = f", {a['duration']:.1f}s" if a.get("duration") else ""
            lines.append(f"- {a['id']} ({a['kind']}, {a['role']}{extra}): \"{a.get('title', '')}\""
                         + (f" — {a['description'][:160]}" if a.get("description") else ""))
        if len(unnamed) > 40:
            lines.append(f"- … and {len(unnamed) - 40} more")
    scenes = project.scenes
    total = sum(sc.get("seconds") or s["scene_seconds"] for sc in scenes)
    lines.append(f"Scenes: {len(scenes)}, {total:g}s planned")
    for i, sc in enumerate(scenes, 1):
        take = d["takes"].get(sc.get("selected_take") or "")
        takes = project.takes_for(sc["id"])
        badges = "; ".join(BADGE_WORDS.get(b, b) for b in project.scene_status(sc["id"])) or "its selected take matches its text"
        take_label = f"Take {take.get('number')} selected ({take['status']})" if take else "no take selected"
        mode = project.scene_mode(sc)
        prev_take = project.data["takes"].get(project.scenes[i - 2].get("selected_take") or "") if i > 1 else None
        cont = f"continues from scene {i - 1} Take {prev_take.get('number')}" if prev_take else "continues the scene before (which has no take yet)"
        start_label = {"continue": cont, "bridge": "bridges to the next scene", "fresh": "a fresh shot",
                       "image": f"opens on the image @{project.scene_start(sc)}"}.get(mode, mode)
        if take and take.get("upscaled"):
            take_label += ", upscaled"
        lines.append(f"{i}. \"{sc.get('title') or ''}\" · {sc.get('seconds') or s['scene_seconds']}s · {start_label}"
                     f" · refs: {_scene_refs_label(project, sc)} · {len(takes)} take(s), {take_label} · {badges}")
        newest = takes[-1] if takes else None
        if newest and take and newest["id"] != take["id"] and newest.get("number", 0) > take.get("number", 0):
            lines.append(f"   newest: Take {newest.get('number')} {take_status_words(newest)}, NOT selected (the video still uses Take {take.get('number')})")
        elif newest and not take and newest.get("status") in ("failed", "interrupted"):
            lines.append(f"   newest: Take {newest.get('number')} {take_status_words(newest)}")
        clips = sc.get("audio_clips") or []
        if clips:
            lines.append("   sound clips (the soundtrack becomes the clip from that time): " + ", ".join(
                f"\"{(project.assets.get(c['asset']) or {}).get('title', c['asset'])}\" at {c['at']:g}s" for c in clips))
        lines.append('   text: """' + (sc.get("text") or "").strip() + '"""')
    if not scenes:
        lines.append("(no scenes yet)")
    fin = d.get("finish") or {}
    if fin.get("tracks") or fin.get("overlays"):
        def _t(x):
            return (project.assets.get(x["asset"]) or {}).get("title") or x["asset"]
        lines.append("Finish (added on top when exporting; the user edits it in Finish): "
                     + "; ".join([f"track \"{_t(t)}\" from {t.get('start', 0):g}s at volume {t.get('volume', 0.6):g}" for t in fin.get("tracks") or []]
                                 + [f"overlay \"{_t(o)}\" {o.get('start', 0):g}–{o['end'] if o.get('end') is not None else 'end'}s, {o.get('position')}"
                                    for o in fin.get("overlays") or []])
                     + (f"; scene sound at {fin.get('scene_volume', 1):g}" if fin.get("scene_volume", 1) != 1 else ""))
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


def build_messages(project: Project, chat: dict, provider: dict, *, with_snapshot: bool = True, lean: bool = False) -> list[dict]:
    """The request messages. with_snapshot=False gives the stable part only (used to prime the cache)."""
    vision = bool(provider.get("vision"))
    history = chat["messages"][chat.get("summarized_upto", 0):]
    last_user = max((i for i, m in enumerate(history) if m["role"] == "user"), default=-1)
    out = [{"role": "system", "content": system_prompt(chat, lean=lean)}]
    events: list[str] = []
    local = provider.get("kind") == "local"
    held: list[dict] = []      # images a tool showed: sent after the last tool reply of that round
    open_calls: list[str] = []  # tool calls of the last assistant message still waiting for a reply

    def close_round():
        # every tool call needs a reply before anything else is sent (strict APIs answer 400 otherwise)
        for cid in open_calls:
            out.append({"role": "tool", "tool_call_id": cid, "content": "(no result: the answer was stopped)"})
        open_calls.clear()
        out.extend(held)
        held.clear()

    for i, m in enumerate(history):
        role = m["role"]
        if role not in ("tool", "attachment"):
            close_round()
        if role == "event":
            events.append(m["text"])
        elif role == "user":
            text = m["text"] + (f"\n\n{m['note']}" if m.get("note") else "")
            since = ("[Since your last message: " + " ".join(events) + "]\n\n") if events else ""
            events = []
            if with_snapshot and i == last_user:
                # small models follow the end of a long message best: what changed and the project first, the request last
                changed = m.get("changes") or []
                head = since + (("Changed in the project since your last message (by the user or by applied cards): "
                                 + "; ".join(changed) + ".\n\n") if changed else "")
                if not vision:
                    head += ("You cannot see pictures with this model: where these instructions say to look at frames, read the "
                             "text the take was made from instead (get_scene, view_take), and tell the user what you could not check.\n\n")
                text = head + (m.get("snapshot") or "") + "\n\n# The user's message\n" + text
            else:
                text = since + text
            parts = [p for p in (_image_part(project, r) for r in m.get("images", [])) if p] if vision else []
            out.append({"role": "user", "content": [*parts, {"type": "text", "text": text}] if parts else text})
        elif role == "assistant":
            calls = m.get("tool_calls") or []
            text = m.get("text") or ""
            if not calls and not text.strip():
                continue   # an empty answer (e.g. stopped at once) is not sent: some APIs reject empty messages
            msg = {"role": "assistant", "content": text if (text or local) else None}
            if calls:
                msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                      "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}} for c in calls]
                open_calls.extend(c["id"] for c in calls)
            out.append(msg)
        elif role == "tool":
            if m["tool_call_id"] in open_calls:
                open_calls.remove(m["tool_call_id"])
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["text"]})
        elif role == "attachment":  # images a tool showed to the model
            parts = [p for p in (_image_part(project, r) for r in m.get("images", [])) if p] if vision else []
            held.append({"role": "user", "content": [*parts, {"type": "text", "text": m["text"]}] if parts else m["text"]})
    close_round()
    return out


def fit_messages(messages: list, budget: int) -> list:
    """For a provider with a small request limit: shorten the longest tool results (oldest first among equals)
    until the request fits, each with a note on how to get the part that is needed. The rest stays as it is."""
    if estimate_tokens(messages) <= budget:
        return messages
    out = [dict(m) for m in messages]
    tools = sorted((i for i, m in enumerate(out) if m.get("role") == "tool" and isinstance(m.get("content"), str)),
                   key=lambda i: -len(out[i]["content"]))
    for keep in (1500, 800, 400):
        for i in tools:
            text = out[i]["content"]
            if len(text) > keep:
                out[i]["content"] = (text[:keep] + "\n[… shortened to fit this provider's request limit. Ask for one thing at a "
                                     "time: view_take scene=N take=M gives one take's text.]")
            if estimate_tokens(out) <= budget:
                return out
    return out


def estimate_tokens(messages: list) -> int:
    """A rough count for providers that send no usage (about 3.5 characters per token; images ~800 tokens)."""
    chars, images = 0, 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):
            for part in c:
                if part.get("type") == "text":
                    chars += len(part.get("text", ""))
                else:
                    images += 1
        for call in m.get("tool_calls") or []:
            chars += len(call["function"]["arguments"]) + 30
    return int(chars / 3.5) + 800 * images


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
    "start": {"type": "string", "enum": ["continue", "fresh", "image"],
              "description": "continue = carry on from the previous scene (default); fresh = a new shot from the references; image = open exactly on start_image"},
    "start_image": {"type": "string", "description": "with start=image: the @name of an image (e.g. a start frame) the scene opens on"},
    "sound_clips": {"type": "array", "items": {"type": "object", "properties": {
        "clip": {"type": "string", "description": "id of an audio item (a_…) from the library, e.g. a speech line"},
        "at": {"type": "number", "description": "seconds into the scene"}}, "required": ["clip"]},
        "description": "audio that becomes the scene's soundtrack from that time, word for word (e.g. a spoken line)"},
    "refs": _REFS,
}
_KINDS = ["character", "place", "prop", "style"]

TOOLS = [
    _fn("get_scene", "Read one scene in full: text, references, the exact prompt sent to H3, and its takes.",
        {"scene": _SCENE}, ["scene"]),
    _fn("view_reference", "Look at a reference image (you will see the picture).",
        {"name": {"type": "string", "description": "reference name without @"}}, ["name"]),
    _fn("view_take", "Look at frames from a finished take of a scene: 4 frames from start to end, or with part=end its last "
        "frames — how it really ends, which is where a scene that continues it begins.",
        {"scene": _SCENE, "take": {"type": "integer", "description": "take number; omit for the selected take"},
         "part": {"type": "string", "enum": ["all", "end"], "description": "all (default) or end"}}, ["scene"]),
    _fn("update_notes", "Replace the project's story notes (your lasting memory: story, characters, tone, decisions). Applied at once.",
        {"notes": {"type": "string", "description": "the complete new notes"}}, ["notes"]),
    _fn("save_reference_card", "Save what a reference looks like, for your own later use (not sent to H3). Applied at once.",
        {"name": {"type": "string"}, "card": {"type": "string", "description": "precise visual features"}}, ["name", "card"]),
    _fn("add_scenes", "Propose new scenes (inserted in order after `after`, or at the end).",
        {"scenes": {"type": "array", "items": {"type": "object", "properties": _SCENE_FIELDS, "required": ["title", "text"]}},
         "after": {"type": "integer", "description": "insert after this scene number; omit to add at the end"}}, ["scenes"]),
    _fn("edit_scene", "Propose changes to one scene. Give only the fields that change.",
        {"scene": _SCENE, **_SCENE_FIELDS,
         "from_take": {"type": "integer", "description": "a take of THIS scene: put back the text, references, length and start it "
                       "was made from (other fields you give change on top). Not for continuing from another scene's take: that is select_take"}}, ["scene"]),
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
         "continuity_frames": {"type": "integer", "enum": [5, 22, 39], "description": "frames carried between scenes"},
         "video_shape": {"type": "string", "enum": ["9:16", "16:9", "1:1", "4:5", "start_image"], "description": "portrait, landscape, square, 4:5, or the start image's shape"},
         "video_model": {"type": "string", "enum": ["fl2va", "ref2va", "ref2va_fresh"], "description": "fl2va = proven default; ref2va = one model, 4 steps, about 2 min per scene"}}, []),
    _fn("create_character", "Propose a new character (or place, prop, style) in the Studio, designed from its description. "
        "Its name becomes its @name once it has a main look.",
        {"kind": {"type": "string", "enum": _KINDS}, "name": {"type": "string"},
         "description": {"type": "string", "description": "what makes it look like itself: age, build, face, hair, clothes, colours (places: layout, materials, time of day). Added to every image of it and sent to H3"},
         "designs": {"type": "integer", "description": "how many first designs to generate (1-4, default 2); 0 with from_image"},
         "from_image": {"type": "string", "description": "optional: an existing image (@name or a_… id) that becomes its first look instead of designing"}},
        ["kind", "name", "description"]),
    _fn("edit_character", "Propose changing a character's / place's description, name or voice.",
        {"character": {"type": "string", "description": "its name or @name"}, "description": {"type": "string", "description": "the complete new description"},
         "new_name": {"type": "string"}, "voice": {"type": "string", "description": "a voice sample (a_… id or @name) for its spoken lines"}}, ["character"]),
    _fn("make_images", "Propose generating images of a character or place: a new outfit/look or variation, a new angle/pose, "
        "start frames of a character in a place, more designs, or a character sheet (front, side and back in one picture, which "
        "scenes can use as its reference). Uses its description and main look automatically — describe only the change.",
        {"character": {"type": "string", "description": "its name or @name"},
         "action": {"type": "string", "enum": ["look", "angle", "place", "more", "sheet"]},
         "change": {"type": "string", "description": "look: 'wearing a winter coat'; angle: 'from the side, walking'; place: what happens there"},
         "place": {"type": "string", "description": "action place: the place's name or @name"},
         "references": {"type": "array", "description": "optional: up to 3 more images to take something from, e.g. another character's outfit",
                        "items": {"type": "object", "properties": {
                            "image": {"type": "string", "description": "a library id (a_…), an @name, or a character/place name (its main look)"},
                            "use": {"type": "string", "description": "outfit, pose, hair, accessory, colours, lighting, background, material, style — or a few words"}},
                            "required": ["image"]}},
         "count": {"type": "integer", "description": "1-4 (default 2)"}}, ["character", "action"]),
    _fn("speak_line", "Propose generating a spoken line in a voice (Qwen3-TTS). The result is an audio item you can then add to a scene as a sound clip.",
        {"text": {"type": "string", "description": "exactly what is said"},
         "voice": {"type": "string", "description": "a character's name (uses its voice), or a voice sample's a_… id / @name"},
         "language": {"type": "string", "description": "Auto, English, Chinese, Japanese, Korean, French, German, Spanish, Portuguese, Russian or Italian"},
         "count": {"type": "integer", "description": "takes to generate, 1-3 (default 2)"}}, ["text", "voice"]),
    _fn("select_take", "Propose choosing another take of a scene for the video. The next scene continues from the selected take, "
        "so select the take you want before writing or rendering the scene after it.",
        {"scene": _SCENE, "take": {"type": "integer", "description": "take number"}}, ["scene", "take"]),
    _fn("render_scenes", "Propose rendering takes. The user confirms; rendering switches the GPU to video generation.",
        {"scenes": {"type": "array", "items": {"type": "integer"}, "description": "scene numbers to render"},
         "rest": {"type": "boolean", "description": "render every scene from the first one without a finished take, in order"}}, []),
]
GENERATE_TOOLS = {"create_character", "make_images", "speak_line"}   # they queue GPU jobs when applied
PROPOSAL_TOOLS = {"add_scenes", "edit_scene", "remove_scene", "move_scene", "edit_project", "set_reference_definition",
                  "change_settings", "render_scenes", "edit_character", "select_take"} | GENERATE_TOOLS


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
    start = (str(args.get("start") or "").strip().lower()) or None
    if start or args.get("start_image"):
        if start in (None, "image") and args.get("start_image"):
            name = str(args["start_image"]).lstrip("@")
            item = project.data["library"]["items"].get(name)
            if not item or item["kind"] != "image":
                raise ProjectError(f"start_image must be the @name of an image; @{name} is not one")
            fields.update(join="cut", start_image=name)
        elif start == "image":
            raise ProjectError("start=image needs start_image (the @name of the image to open on)")
        elif start == "fresh":
            fields.update(join="cut", start_image="")
        elif start == "continue":
            fields.update(join="continue", start_image=None)
        else:
            raise ProjectError("start must be continue, fresh or image")
    if args.get("sound_clips") is not None:
        clips = []
        for c in args["sound_clips"] or []:
            asset = _asset_ref(project, (c or {}).get("clip") or "", "audio")
            clips.append({"asset": asset["id"], "at": max(0.0, float(c.get("at") or 0.5))})
        fields["audio_clips"] = clips
    if "refs" in args:
        fields["refs"] = _clean_refs(project, args["refs"])
        missing = [n for n in _style_names(project) if fields["refs"] is not None and n not in fields["refs"]]
        if missing:
            fields["refs"] += missing
            if notes is not None:
                notes.append(", ".join("@" + n for n in missing))
    return fields


_CAMERA = re.compile(r"\b(shot|camera|close-up|close up|wide|dolly|pan|push-in|pull-back|tracking|one continuous)\b", re.I)


def continuity_echo(project: Project, scene_id: str, text: str) -> str:
    """For a scene that continues a finished take: quote its first sentence back, so the model compares it with
    the picture the scene really starts from (small models act on such feedback far more than on a rule)."""
    idx = project.scene_index(scene_id)
    if idx == 0 or project.scene_mode(project.scene(scene_id)) not in ("continue", "bridge"):
        return ""
    prev = project.scenes[idx - 1]
    take = project.data["takes"].get(prev.get("selected_take") or "")
    if not take or take.get("status") != "done":
        return ""
    m = re.search(r"\[Shot \d+\]\s*(.*?[.!?])(?:\s|$)", text or "", re.S)
    first = (m.group(1) if m else (text or "")[:160]).strip()
    return (f"Continuity check: scene {idx + 1} starts from the LAST frame of scene {idx} Take {take.get('number')} "
            f"(view_take scene={idx} part=end shows it, if it is not attached). Your text begins: \"{first}\" Does that describe "
            "what that frame shows — where each person and thing is, and what is still moving? If not, propose the edit again "
            "with a first sentence that does, then the one action that follows from it. If it does, say nothing about this.")


def density_warning(text: str, seconds: float) -> str:
    """Too many actions for the length: the model compresses them and skips to the last one."""
    m = re.search(r"integrated_multimodal_description:(.*?)(?:\n\s*overall_soundscape:|$)", text or "", re.S)
    body = re.sub(r"<d>.*?</d>", "", m.group(1) if m else "", flags=re.S)
    body = re.sub(r"\[Shot \d+\]", "", body)
    sentences = [x for x in re.split(r"(?<=[.!?])\s+", body.strip()) if x.strip() and not _CAMERA.search(x)]
    verbs = len(re.findall(r",\s*(?:and\s+)?(?:then\s+)?[a-z]+s\b|\band\s+(?:then\s+)?[a-z]+s\b|\bthen\s+[a-z]+s\b", body))
    beats = len(sentences) + verbs
    allowed = max(3, round(4 * float(seconds or 5) / 5))
    if beats > allowed:
        return (f"it describes about {beats} actions in {float(seconds or 5):g} seconds; H3 compresses that and jumps to the last "
                "one. Keep one main action (plus a small reaction), say where the camera is, and move the rest to the next scene")
    return ""


def _check_prompts(project: Project, scene_ids: list[str]) -> None:
    errors = []
    for sid in scene_ids:
        scene = project.scene(sid)
        try:
            compiled = project.compile_prompt(scene)
            # a character named in the text but not active in the scene: H3 would draw a stranger
            for e in project.entities.values():
                if e["kind"] != "character" or e.get("ref") in compiled["labels"]:
                    continue
                if re.search(r"(?<![@\w])" + re.escape(e["name"]) + r"(?!\w)", scene.get("text") or "", re.I):
                    active = e.get("ref") in project.data["library"]["items"]
                    raise ProjectError(f"the text names {e['name']}, but " + (
                        f"@{e['ref']} is not in this scene's references — H3 would draw a stranger. Write @{e['ref']} and add it "
                        "to refs, or leave them out of this scene" if active else
                        f"{e['name']} has no main look yet, so H3 cannot know what they look like; leave them out for now"))
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
        if earlier["tool"] != "render_scenes" and earlier["tool"] not in GENERATE_TOOLS:
            try:
                _apply(dry, earlier)
            except (ProjectError, KeyError, ValueError, StopIteration):
                pass
    pre = copy.deepcopy(dry.data)   # with the waiting cards applied: what this card was written against
    resolved: dict = {}
    details: list[dict] = []
    numbers = {s["id"]: i + 1 for i, s in enumerate(project.scenes)}
    if tool == "add_scenes":
        scenes = args.get("scenes") or []
        if isinstance(scenes, str):
            try:
                scenes = json.loads(scenes)
            except ValueError:
                scenes = []
        if (not isinstance(scenes, list) or not scenes or not all(isinstance(x, dict) for x in scenes)) and args.get("text"):
            scenes = [{k: v for k, v in args.items() if k not in ("scenes", "after")}]   # one scene written flat
        if not isinstance(scenes, list) or not all(isinstance(x, dict) for x in scenes):
            raise ProjectError("`scenes` must be a list of scene objects, each with title and text")
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
        dense = [f"new scene {i + 1}: {msg}" for i, f in enumerate(resolved["scenes"])
                 if (msg := density_warning(f["text"], f.get("seconds") or 5))]
        if added:
            echo = continuity_echo(dry, added[0], resolved["scenes"][0]["text"])
            if echo:
                resolved["echo"] = echo
        warnings += dense
        if warnings:
            resolved["warning"] = "; ".join(warnings)
        resolved["total"] = len(dry.scenes)
        where = f"after scene {project.scene_index(after) + 1}" if after else "at the end"
        summary = f"Add {len(added)} scene{'s' if len(added) > 1 else ''} {where}"
    elif tool == "edit_scene":
        sid = _scene_id(project, args.get("scene"))
        notes = []
        fields = {}
        if args.get("from_take") not in (None, "", 0):
            take = next((t for t in project.takes_for(sid) if t.get("number") == int(args["from_take"])), None)
            if not take:
                n = numbers[sid]
                raise ProjectError(f"scene {n} has no Take {args['from_take']} (from_take puts back a take of the same scene). "
                                   + (f"To continue scene {n} from a take of scene {n - 1}, propose select_take for scene {n - 1}, "
                                      "and edit_scene with only the new text" if n > 1 else ""))
            text = take_text(project, take)
            if text is None:
                raise ProjectError(f"Take {take['number']} has no recorded prompt to put back")
            fields["text"] = text
            if "scene_text" in take:   # recorded setup (takes made outside Clapper's scenes have only their prompt)
                fields.update(refs=take.get("scene_refs"), seconds=take.get("scene_seconds"))
                if take.get("scene_join"):
                    fields["join"] = take["scene_join"]
                for key in ("start_image", "audio_clips"):
                    if f"scene_{key}" in take:
                        fields[key] = take[f"scene_{key}"]
        fields.update(_scene_fields(dry, {k: v for k, v in args.items() if k not in ("scene", "from_take")}, notes))
        current = dry.scene(sid) if any(s["id"] == sid for s in dry.scenes) else {}
        if args.get("from_take") not in (None, "", 0):   # only what really differs from the scene now
            fields = {k: v for k, v in fields.items() if current.get(k) != v or k in args}
        if not fields:
            raise ProjectError("edit_scene needs at least one field to change")
        if not any(s["id"] == sid for s in dry.scenes):
            raise ProjectError(f"scene {numbers[sid]} is removed by a proposal waiting for the user")
        before = copy.deepcopy(dry.scene(sid))
        dry.update_scene(sid, **fields)
        _check_prompts(dry, [sid])
        resolved = {"scene": sid, "fields": fields}
        warn = [f"added {notes[0]} to the scene's refs, because the style text names it"] if notes else []
        restoring = args.get("from_take") not in (None, "", 0) and "text" not in args
        if "text" in fields and not restoring:   # a take's own text was already rendered: no advice on it
            d = density_warning(fields["text"], dry.scene(sid).get("seconds") or 5)
            if d:
                warn.append(d)
            echo = continuity_echo(dry, sid, fields["text"])
            if echo:
                resolved["echo"] = echo
        if warn:
            resolved["warning"] = "; ".join(warn)
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
        if args.get("video_shape") is not None:
            from .settings import ASPECTS
            if args["video_shape"] not in ASPECTS or args["video_shape"] == "custom":
                raise ProjectError("video_shape must be 9:16, 16:9, 1:1, 4:5 or start_image")
            overrides.setdefault("resolution", {})["aspect"] = args["video_shape"]
        if args.get("video_model") is not None:
            if args["video_model"] not in ("fl2va", "ref2va", "ref2va_fresh"):
                raise ProjectError("video_model must be fl2va, ref2va or ref2va_fresh")
            overrides.setdefault("video_model", {})["mode"] = args["video_model"]
        if not overrides:
            raise ProjectError("change_settings needs steps, scene_seconds, continuity_frames, video_shape or video_model")
        resolved = {"settings": overrides}
        summary = "Change settings: " + json.dumps(overrides)
    elif tool == "select_take":
        sid = _scene_id(project, args.get("scene"))
        take = next((t for t in project.takes_for(sid) if t.get("number") == int(args.get("take") or 0)), None)
        if not take or take.get("status") != "done":
            raise ProjectError(f"scene {project.scene_index(sid) + 1} has no finished Take {args.get('take')}")
        if take["id"] == project.scene(sid).get("selected_take"):
            raise ProjectError(f"Take {take['number']} is already the selected take of scene {project.scene_index(sid) + 1}")
        resolved = {"scene": sid, "take": take["id"]}
        nxt = project.scenes[project.scene_index(sid) + 1] if project.scene_index(sid) + 1 < len(project.scenes) else None
        summary = (f"Use Take {take['number']} for scene {project.scene_index(sid) + 1}"
                   + (" (the next scene then continues from it)" if nxt and nxt.get("join") in ("continue", "bridge") else ""))
    elif tool in ("create_character", "edit_character", "make_images", "speak_line"):
        resolved, summary = _plan_studio(project, tool, args)
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
    plan = {"tool": tool, "args": resolved, "summary": summary, "details": details}
    base_now, base_pending = _card_fields(project.data, tool, resolved), _card_fields(pre, tool, resolved)
    if base_now is not None:
        plan["base"] = {"now": base_now, "pending": base_pending}
    return plan


def _card_fields(data: dict, tool: str, args: dict) -> dict | None:
    """The current values of what a card changes (to notice when the user changed them after it was made)."""
    try:
        if tool == "edit_scene":
            scene = next((s for s in data["scenes"] if s["id"] == args["scene"]), None)
            return {k: (scene or {}).get(k) for k in args["fields"]}
        if tool == "edit_project":
            return {k: data["library"]["defaults"] if k == "defaults" else data.get(k) for k in args}
        if tool == "set_reference_definition":
            return {"definition": (data["library"]["items"].get(args["name"]) or {}).get("definition")}
        if tool == "edit_character":
            e = (data.get("entities") or {}).get(args["entity"]) or {}
            return {k: e.get(k) for k in args["fields"]}
    except (KeyError, TypeError):
        return None
    return None


def stale_card(project: Project, proposal: dict) -> str:
    """What the user changed after this card was made ('' if nothing): applying it would overwrite that."""
    base = proposal.get("base")
    if not base:
        return ""
    now = _card_fields(project.data, proposal["tool"], proposal["args"]) or {}
    changed = [k for k, v in now.items() if v != base["now"].get(k) and v != (base.get("pending") or {}).get(k)]
    if not changed:
        return ""
    what = {"edit_scene": "the scene", "edit_project": "the project", "set_reference_definition": "the reference",
            "edit_character": "the character"}[proposal["tool"]]
    return f"{what} was changed after this card was made ({', '.join(changed)})"


def find_entity(project: Project, name: str) -> dict:
    """A character / place by id, @name or name (case-insensitive)."""
    key = str(name or "").strip().lstrip("@")
    for e in project.entities.values():
        if key in (e["id"], e.get("ref")) or key.lower() == e["name"].lower():
            return e
    names = ", ".join(f"{e['name']} (@{e['ref']})" for e in project.entities.values()) or "none yet"
    raise ProjectError(f"there is no character or place called {name!r} (there are: {names})")


def _asset_ref(project: Project, value: str, kind: str) -> dict:
    """An asset by a_… id or by @name."""
    v = str(value or "").strip().lstrip("@")
    a = project.assets.get(v)
    if a is None and v in project.data["library"]["items"]:
        a = project.assets.get(project.data["library"]["items"][v].get("asset"))
    if a is None or a["kind"] != kind:
        raise ProjectError(f"{value!r} is not an {kind} in the library (use its a_… id or @name from <project>)")
    return a


def _plan_studio(project: Project, tool: str, args: dict) -> tuple[dict, str]:
    if tool == "create_character":
        kind = str(args.get("kind") or "character")
        if kind not in _KINDS:
            raise ProjectError(f"kind must be one of {_KINDS}")
        name = str(args.get("name") or "").strip()
        if not name:
            raise ProjectError("give it a name")
        if any(e["name"].lower() == name.lower() for e in project.entities.values()):
            raise ProjectError(f"{name} already exists; use edit_character or make_images")
        desc = str(args.get("description") or "").strip()
        if len(desc) < 15:
            raise ProjectError("the description is too short: describe what makes it look like itself")
        image = _asset_ref(project, args["from_image"], "image")["id"] if args.get("from_image") else None
        designs = 0 if image else max(1, min(4, int(args.get("designs") or 2)))
        return ({"kind": kind, "name": name, "description": desc, "from_image": image, "designs": designs},
                f"Create {kind} {name}" + (f" and design {designs} image(s)" if designs else " from an existing image"))
    if tool == "edit_character":
        e = find_entity(project, args.get("character"))
        fields = {}
        if args.get("description"):
            fields["description"] = str(args["description"]).strip()
        if args.get("new_name"):
            fields["name"] = str(args["new_name"]).strip()
        if args.get("voice"):
            fields["voice"] = _asset_ref(project, args["voice"], "audio")["id"]
        if not fields:
            raise ProjectError("edit_character needs description, new_name or voice")
        return ({"entity": e["id"], "fields": fields}, f"Change {e['name']}: " + ", ".join(fields))
    if tool == "make_images":
        e = find_entity(project, args.get("character"))
        action = {"more": "first"}.get(str(args.get("action")), str(args.get("action")))
        if action not in ("look", "angle", "place", "first", "sheet"):
            raise ProjectError("action must be look, angle, place, more or sheet")
        if action == "sheet" and e["kind"] not in ("character", "prop"):
            raise ProjectError("a character sheet is for characters and props")
        change = str(args.get("change") or "").strip()
        if action in ("look", "angle") and not change and not args.get("references"):
            raise ProjectError("describe the change (e.g. 'wearing a winter coat'), or give references to take it from")
        if action != "first" and not e.get("main"):
            raise ProjectError(f"{e['name']} has no main look yet; propose make_images action=more first")
        place = None
        if action == "place":
            if e["kind"] != "character":
                raise ProjectError("only a character can be put in a place")
            p = find_entity(project, args.get("place"))
            if p["kind"] != "place" or not p.get("main"):
                raise ProjectError(f"{p['name']} is not a place with a main image")
            place = p["id"]
        count = max(1, min(4, int(args.get("count") or (1 if action == "sheet" else 2))))
        label = {"look": f"new look of {e['name']}: {change}", "angle": f"{e['name']} {change}", "sheet": f"a character sheet of {e['name']}",
                 "place": f"{e['name']} in {project.entities[place]['name'] if place else ''}: {change}", "first": f"more designs of {e['name']}"}[action]
        refs = []
        for r in (args.get("references") or [])[:3]:
            ref = str((r or {}).get("image") or "").strip()
            try:
                aid = find_entity(project, ref)["main"]
            except ProjectError:
                aid = _asset_ref(project, ref, "image")["id"]
            if not aid:
                raise ProjectError(f"{ref} has no image to use as a reference")
            refs.append({"asset": aid, "use": str((r or {}).get("use") or "").strip()})
        if refs:
            label += " (with " + ", ".join((r["use"] or "reference") + " from " + (project.assets[r["asset"]].get("title") or r["asset"]) for r in refs) + ")"
        return ({"entity": e["id"], "action": action, "text": change, "place": place, "count": count, "refs": refs}, f"Make {count} image(s) — {label}")
    # speak_line
    text = str(args.get("text") or "").strip()
    if not text:
        raise ProjectError("give the words to speak")
    voice_arg = str(args.get("voice") or "")
    try:
        e = find_entity(project, voice_arg)
        if not e.get("voice"):
            raise ProjectError(f"{e['name']} has no voice yet; give a voice sample id, or propose edit_character with a voice")
        voice, who = e["voice"], e["name"]
    except ProjectError as exc:
        if "has no voice" in str(exc):
            raise
        a = _asset_ref(project, voice_arg, "audio")
        voice, who = a["id"], a["title"]
    language = str(args.get("language") or "Auto")
    count = max(1, min(3, int(args.get("count") or 2)))
    return ({"text": text, "voice": voice, "language": language, "count": count},
            f"Speak {count} take(s) in {who}'s voice: “{text[:60]}”")


def apply_proposal(project: Project, proposal: dict, *, render=None, client=None, force: bool = False) -> str:
    """Carry out an applied proposal on the real project. render(project, scene_ids | None) queues takes.

    The project may have changed since the proposal was checked (other proposals applied or rejected, the
    user's own edits), so it is tried on a dry copy first: it is refused if a scene it adds or rewrites, or
    one that rendered before, would no longer compile. A card whose fields the user changed after it was made is
    refused too (it would silently undo their edit), unless force=True."""
    stale = "" if force else stale_card(project, proposal)
    if stale:
        raise ProjectError(f"not applied: {stale}, and applying it would undo that change. Ask the assistant for a new version "
                           "(it sees your change), or apply it anyway")
    if proposal["tool"] not in ("render_scenes", "change_settings", "edit_character", "select_take") and proposal["tool"] not in GENERATE_TOOLS:
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
    return _apply(project, proposal, render=render, client=client)


def _compiles(project: Project, scene: dict) -> bool:
    try:
        project.compile_prompt(scene)
        return True
    except ProjectError:
        return False


def _apply(project: Project, proposal: dict, *, render=None, client=None) -> str:
    tool, a = proposal["tool"], proposal["args"]
    needs_gpu = tool in GENERATE_TOOLS and not (tool == "create_character" and not a.get("designs"))
    if needs_gpu and client is None:
        if isinstance(project, _Dry):   # checking only: create the entity, generate nothing
            if tool == "create_character":
                project.create_entity(a["kind"], a["name"], description=a["description"], from_asset=a.get("from_image"))
            return "checked"
        raise ProjectError("generating is not available here")
    if tool == "add_scenes":
        prev = a.get("after")
        if prev and not any(s["id"] == prev for s in project.scenes):
            raise ProjectError("the scene these were to follow no longer exists")
        for fields in a["scenes"]:
            prev = project.add_scene(after=prev, text=fields.get("text", ""), seconds=fields.get("seconds"),
                                     join=fields.get("join", "continue"), refs=fields.get("refs"), title=fields.get("title", ""),
                                     start_image=fields.get("start_image"), audio_clips=fields.get("audio_clips"))["id"]
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
    elif tool == "edit_character":
        project.update_entity(a["entity"], **a["fields"])
    elif tool == "select_take":
        project.select_take(a["scene"], a["take"])
    elif tool == "create_character":
        from . import tools as studio_tools

        e = project.create_entity(a["kind"], a["name"], description=a["description"], from_asset=a.get("from_image"))
        if a.get("designs"):
            studio_tools.queue_entity(project, client, e["id"], "first", "", count=a["designs"])
            return f"created {e['name']}; {a['designs']} design(s) queued"
        return f"created {e['name']} (@{e['ref']})"
    elif tool == "make_images":
        from . import tools as studio_tools

        jobs = studio_tools.queue_entity(project, client, a["entity"], a["action"], a.get("text", ""), place_id=a.get("place"), count=a["count"],
                                         refs=a.get("refs") or None)
        return f"{len(jobs)} image(s) queued"
    elif tool == "speak_line":
        from . import tools as studio_tools

        voice = project.assets.get(a["voice"]) or {}
        jobs = studio_tools.queue_tool(project, client, "speech", {"text": a["text"], "voice": a["voice"], "voice_text": voice.get("transcript", ""),
                                                                   "language": a["language"]}, count=a["count"], role="speech", title=a["text"][:40])
        return f"{len(jobs)} take(s) of the line queued"
    return "applied"


def take_text(project: Project, take: dict) -> str | None:
    """The scene text a take was made from: recorded on the take, or recovered from its meta.json prompt."""
    if take.get("scene_text") is not None:
        return take["scene_text"]
    try:
        prompt = json.loads((project.root / take["dir"] / "meta.json").read_text(encoding="utf-8")).get("prompt") or ""
    except (OSError, ValueError):
        return None
    m = re.search(r"(?:Continuation of Chunk \d+\.\s*)?(integrated_multimodal_description:.*)$", prompt, re.S)
    body = m.group(1) if m else prompt
    # a take made outside a scene keeps H3's <Picture N> numbers: give them back their @names where Clapper knows them
    labels = {}
    try:
        labels = {v: k for k, v in project.compile_prompt(project.scene(take["scene"]))["labels"].items()}
    except ProjectError:
        pass
    return re.sub(r"<(Picture|Audio) \d+>", lambda mm: "@" + labels[mm.group(0)] if mm.group(0) in labels else mm.group(0), body).strip()


TAKE_STATUS_WORDS = {"done": "finished", "queued": "waiting to render", "rendering": "rendering now",
                     "interrupted": "interrupted (lost to a restart; no video)"}


def take_status_words(take: dict) -> str:
    if take.get("status") == "failed":
        return "FAILED" + (f" ({str(take.get('error') or '').strip().splitlines()[-1][:200]})" if (take.get("error") or "").strip() else "")
    return TAKE_STATUS_WORDS.get(take.get("status"), take.get("status") or "?")


def describe_take(project: Project, scene: dict, take: dict, shown: dict | None = None) -> str:
    """One take as the assistant reads it: status, what it continued from, how its setup differs from the scene
    now, and the text it was made from (texts already shown for an earlier take are referred to, not repeated)."""
    shown = {} if shown is None else shown
    bits = [take_status_words(take)]
    if take["id"] == scene.get("selected_take"):
        bits.append("SELECTED: the video and the next scene use this one")
    if take.get("seed") is not None:
        bits.append(f"seed {take['seed']}")
    parent = project.data["takes"].get(take.get("parent_take") or "")
    if parent:
        where = next((i for i, s in enumerate(project.scenes, 1) if s["id"] == parent["scene"]), None)
        label = f"scene {where} Take {parent.get('number')}" if where else f"Take {parent.get('number')} of a removed scene"
        prev = project.previous_selected_take(scene["id"]) if any(s["id"] == scene["id"] for s in project.scenes) else None
        bits.append(f"continued from {label}" + (f" (that scene now uses Take {prev.get('number')}, so this take's start no longer matches)"
                                                 if prev and prev["id"] != parent["id"] else ""))
    elif take.get("join") == "cut" and take.get("status") == "done":
        bits.append("a fresh shot")
    diffs = []
    if take.get("refs") is not None and take["refs"] != project.scene_refs(scene):
        diffs.append("references " + (", ".join("@" + r for r in take["refs"]) or "none"))
    if "scene_seconds" in take and take["scene_seconds"] != scene.get("seconds"):
        diffs.append(f"length {take.get('seconds')}s")
    if "scene_start_image" in take and (take["scene_start_image"] or None) != (scene.get("start_image") or None):
        diffs.append("start image " + (f"@{take['scene_start_image']}" if take["scene_start_image"] else "none (from the references)"))
    if "scene_audio_clips" in take and (take["scene_audio_clips"] or []) != (scene.get("audio_clips") or []):
        diffs.append("other sound clips")
    if diffs:
        bits.append("made with " + "; ".join(diffs) + " (differs from the scene now)")
    if take.get("upscaled"):
        bits.append(f"upscaled ({take['upscaled']}; the export uses that version)")
    head = f"Take {take.get('number')}: " + ", ".join(bits)
    text = take_text(project, take)
    if text is None:
        return head + " — its prompt is not recorded"
    if text == (scene.get("text") or ""):
        return head + " — made from the scene's current text"
    if text in shown:
        return head + f" — made from the same text as Take {shown[text]}"
    shown[text] = take.get("number")
    return head + " — made from this text:\n" + text


def extract_frames(project: Project, take: dict, count: int = 4, *, end: bool = False) -> list[str]:
    video = project.root / take["dir"] / "take.mp4"
    if not video.is_file():
        raise ProjectError("this take has no video yet")
    out_dir = chat_dir(project) / "frames" / take["id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = int(take.get("frames") or 120)
    skip = int(take.get("context_frames") or 0)  # carried-over frames belong to the previous scene
    rels = []
    for i in range(count):
        if end:   # the last second: what a continuing scene starts from
            index = frames - 1 - round(i * 8)
            target = out_dir / f"end{i + 1}.jpg"
        else:
            index = skip + round((frames - skip - 1) * (0.08 + 0.84 * i / (count - 1)))
            target = out_dir / f"f{i + 1}.jpg"
        if not target.is_file():
            proc = subprocess.run([FFMPEG, "-v", "error", "-y", "-i", str(video), "-vf", f"select=eq(n\\,{index})",
                                   "-frames:v", "1", "-q:v", "3", str(target)], capture_output=True)
            if proc.returncode != 0 or not target.is_file():
                raise ProjectError("could not extract frames: " + proc.stderr.decode(errors="replace")[-300:])
        rels.append(str(target.relative_to(project.root)))
    return list(reversed(rels)) if end else rels


def run_read_tool(project: Project, tool: str, args: dict, provider: dict) -> tuple[str, list[str]]:
    """Read tools: (text result, images to show)."""
    if tool == "get_scene":
        sid = _scene_id(project, args.get("scene"))
        sc = project.scene(sid)
        try:
            compiled = project.compile_prompt(sc)["prompt"]
        except ProjectError as exc:
            compiled = f"ERROR: {exc}"
        lines, shown = [], {}
        for t in project.takes_for(sid):
            lines.append(describe_take(project, sc, t, shown))
        return (f"Scene {project.scene_index(sid) + 1} \"{sc.get('title') or ''}\" · {sc.get('seconds') or 'default'}s · join {sc['join']}"
                f" · refs: {_scene_refs_label(project, sc)}\nText:\n{sc.get('text') or ''}\n\nPrompt sent to H3:\n{compiled}\n\n"
                "Takes (each with the text it was made from):\n" + ("\n".join(lines) or "none")), []
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
        if not take:
            numbers = ", ".join(str(t.get("number")) for t in takes) or "none"
            raise ProjectError(f"scene {project.scene_index(sid) + 1} has no " + (f"Take {args['take']}" if args.get("take") else "selected take")
                               + f" (its takes: {numbers})")
        if take.get("status") != "done":
            raise ProjectError(f"Take {take.get('number')} of scene {project.scene_index(sid) + 1} has no video: it is {take_status_words(take)}")
        if not provider.get("vision"):   # a text-only model: the take's own text says how it ends
            return (f"You cannot see pictures. Scene {project.scene_index(sid) + 1} Take {take.get('number')} was made from this text; "
                    "its last action is the state the take ends in (treat it as approximate, and say so if it matters):\n"
                    + (take_text(project, take) or "(not recorded)")), []
        if args.get("part") == "end":
            frames = extract_frames(project, take, 3, end=True)
            return (f"Showing the last {len(frames)} frames (the final second) of scene {project.scene_index(sid) + 1} Take "
                    f"{take.get('number')}, oldest first. A scene that continues this take starts from exactly this picture: "
                    "describe that state first, then the one action that follows."), frames
        frames = extract_frames(project, take)
        return (f"Showing 4 frames, start to end, of scene {project.scene_index(sid) + 1} Take {take.get('number')} "
                f"({take.get('frames')} frames at 24 fps). The scene text it was made from:\n{take_text(project, take) or '(not recorded)'}"), frames
    raise ProjectError(f"unknown tool {tool}")


# ------------------------------------------------------------------ the agent

_LEAK = re.compile(r"</?(?:tool_call|function)\b")
_XML_CALL = re.compile(r"<function=([\w-]+)>\s*(.*?)\s*(?:</function>|$)", re.S)
_XML_PARAM = re.compile(r"<parameter=([\w-]+)>\s*(.*?)\s*(?:</parameter>|(?=<parameter=)|$)", re.S)


def _value(v: str):
    v = v.strip().strip('"').strip()
    try:
        return json.loads(v)
    except ValueError:
        return v


def _xml_args(body: str) -> dict:
    args = {k: _value(re.split(r'"\s*[,}]', v, maxsplit=1)[0]) for k, v in _XML_PARAM.findall(body)}
    # some servers mix the two formats: '<parameter=scene>\n3","part":"all"}'
    for k, v in re.findall(r'"(\w+)"\s*:\s*("(?:[^"\\]|\\.)*"|-?[\d.]+|true|false|null)', body):
        args.setdefault(k, json.loads(v))
    return args


def repair_tool_calls(calls: list[dict], content: str) -> tuple[list[dict], str]:
    """Qwen-style models sometimes leak their XML tool-call format: several calls glued into one call's JSON
    arguments, or calls written into the text. Split them into proper calls."""
    out = []
    for c in calls:
        raw = c.get("arguments") or ""
        if not _LEAK.search(raw):
            out.append(c)
            continue
        first, _, rest = raw.partition("</function>")
        first = re.sub(r"\s*$", "", first)
        args0 = None
        for tail in ('"}', "}", '"]}', ""):
            try:
                args0 = json.loads(first + tail)
                break
            except ValueError:
                continue
        if args0 is None:
            args0 = _xml_args(first)
        out.append({**c, "arguments": json.dumps({k: (v.strip() if isinstance(v, str) else v) for k, v in args0.items()})})
        for n, (name, body) in enumerate(_XML_CALL.findall(rest)):
            out.append({"id": f"{c['id']}_{n + 2}", "name": name, "arguments": json.dumps(_xml_args(body))})
    if not out and "<tool_call>" in (content or ""):
        for n, (name, body) in enumerate(_XML_CALL.findall(content)):
            out.append({"id": f"call_text_{n + 1}", "name": name, "arguments": json.dumps(_xml_args(body))})
        if out:
            content = re.sub(r"<tool_call>.*?(?:</tool_call>|$)", "", content, flags=re.S).strip()
    return out, content


REPEAT = "You already proposed exactly this"
BIG_TASK = re.compile(r"\b(plan|storyboard|whole (video|story|ad)|all (the )?scenes|every scene|rewrite|restructure|"
                      r"from scratch|start over|\d+ (new )?scenes)\b", re.I)


TAKE_REF = re.compile(r"\btake\s*(?:no\.?|number|#)?\s*(\d+)", re.I)
SCENE_REF = re.compile(r"\bscenes?\s*(?:no\.?|number|#)?\s*(\d+)", re.I)
_NUM = r"\s*(?:no\.?|number|#)?\s*(\d+)"
TAKE_OF_SCENE = re.compile(r"\btake" + _NUM + r"\s+(?:of|from|in|on)\s+(?:the\s+)?scene" + _NUM
                           + r"|\bscene" + _NUM + r"(?:'s)?\s*[,:]?\s*(?:its\s+)?take" + _NUM, re.I)


def named_take(project: Project, text: str) -> tuple[int, dict] | None:
    """(scene number, take) a message names: "take 5 of scene 3", "scene 3, take 5", or a take number when only
    one scene is named. "write scene 4 from take 5 of scene 3" is Take 5 of scene 3, not of scene 4."""
    m = TAKE_OF_SCENE.search(text or "")
    if m:
        number, n = (int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(4)), int(m.group(3)))
    else:
        tk = TAKE_REF.search(text or "")
        scenes = {int(x.group(1)) for x in SCENE_REF.finditer(text or "")}
        if not tk or len(scenes) != 1 or re.search(r"\bscenes\b", text, re.I):
            return None
        number, n = int(tk.group(1)), scenes.pop()
    if not 1 <= n <= len(project.scenes):
        return None
    take = next((t for t in project.takes_for(project.scenes[n - 1]["id"]) if t.get("number") == number), None)
    return (n, take) if take else None
WRITES_SCENE = re.compile(r"\b(fix|rewrite|re-write|write|redo|change|edit|improve|add|insert|new|make|extend|continue|shorten|longer|"
                          r"doesn'?t work|wrong|instead)\b", re.I)


def scene_frames_for(project: Project, text: str) -> tuple[list[str], str]:
    """When a message names a scene, show what the video really looks like there: the end of the take it continues
    from, and the middle of its own selected take (deterministic, so the model need not ask)."""
    numbers = []
    for m in SCENE_REF.finditer(text or ""):
        n = int(m.group(1))
        if 1 <= n <= len(project.scenes) and n not in numbers:
            numbers.append(n)
    images, notes = [], []
    named = named_take(project, text)
    if named:   # "take 5 of scene 3" / "scene 3 take 5": that take's middle and its last frame
        n, take = named
        scene = project.scenes[n - 1]
        if take.get("status") == "done":
            images = extract_frames(project, take, 4)[1:3] + extract_frames(project, take, 1, end=True)
            sel = "" if take["id"] == scene.get("selected_take") else f" (NOT the selected take: the selected one is Take {(project.data['takes'].get(scene.get('selected_take') or '') or {}).get('number', '—')})"
            return images, (f"[Attached, in this order: two frames from the middle of scene {n} Take {take['number']}{sel}, "
                            f"and its LAST frame — a scene that continues Take {take['number']} starts from exactly this picture.]")
    for n in numbers[:2]:
        scene = project.scenes[n - 1]
        prev = project.scenes[n - 2] if n > 1 else None
        prev_take = project.data["takes"].get((prev or {}).get("selected_take") or "")
        if prev and project.scene_mode(scene) in ("continue", "bridge") and prev_take and prev_take.get("status") == "done":
            images += extract_frames(project, prev_take, 1, end=True)
            notes.append(f"the LAST frame of scene {n - 1} (Take {prev_take.get('number')}) — scene {n} starts from exactly this picture")
        take = project.data["takes"].get(scene.get("selected_take") or "")
        if take and take.get("status") == "done":
            images += extract_frames(project, take, 4)[1:3]
            stale = " (rendered from an earlier version of its text)" if "prompt_changed" in project.scene_status(scene["id"]) else ""
            notes.append(f"two frames from the middle of scene {n}'s selected Take {take.get('number')}{stale}")
    if not images:
        return [], ""
    return images, "[Attached, in this order: " + "; ".join(notes) + ".]"



def asks_for_one_scene(text: str) -> bool:
    """'write scene 4' / 'a new scene' — not 'scenes 4 to 6', 'two scenes' or 'the rest of the story'."""
    text = text or ""
    if re.search(r"\bscenes\b|\b(?:two|three|four|five|several|few|some)\b", text, re.I):
        return False
    return bool(re.search(r"\b(?:a|one|the next|new|another)\s+(?:new\s+)?scene\b|\bscene\s*(?:no\.?|number|#)?\s*\d+", text, re.I))


def named_take_to_select(project: Project, text: str, tool: str, args: dict) -> dict | None:
    """The take a new or edited scene should continue from, when the user's message named a take of the scene
    just before it ("take 5 of scene 3 … write scene 4") and that take is not selected: the next scene always
    continues from the selected take, so the card alone would not do what was asked. Returns the take or None."""
    named = named_take(project, text)
    if not named or tool not in ("add_scenes", "edit_scene"):
        return None
    n, take = named
    before = project.scenes[n - 1]
    if tool == "add_scenes":
        first = (args.get("scenes") or [{}])[0]
        prev = args.get("after") or (project.scenes[-1]["id"] if project.scenes else None)
        join = first.get("join", "continue")
    else:
        sid = args.get("scene")
        i = project.scene_index(sid) if any(s["id"] == sid for s in project.scenes) else -1
        prev = project.scenes[i - 1]["id"] if i > 0 else None
        join = (args.get("fields") or {}).get("join") or (project.scene(sid).get("join") if i >= 0 else None)
    if prev != before["id"] or join not in ("continue", "bridge"):
        return None
    if take.get("status") != "done" or take["id"] == before.get("selected_take"):
        return None
    return take

def resolve_thinking(thinking, project: Project, text: str, provider: dict):
    """The thinking setting for one turn: False, a token budget, or "auto" — think (the provider's
    thinking_budget) when planning a video (no scenes yet, or the message asks for a plan or many scenes),
    answer directly for everything else."""
    if thinking == "auto":
        big = (not project.scenes or bool(BIG_TASK.search(text or ""))
               or (bool(SCENE_REF.search(text or "") or re.search(r"\bscenes?\b", text or "", re.I)) and bool(WRITES_SCENE.search(text or ""))))
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
        self.lean = False   # the prompt guide in brief (a provider with a small request limit)
        self.open, self.write = open, write
        self.before_request = before_request or (lambda: None)
        self.update = update or (lambda state: None)
        self.stopped = stopped or (lambda: False)
        self.turn = None  # index of the user message being answered; proposals remember it
        self._echoed: set = set()   # (turn, scene) pairs that already got the continuity check

    def _complete(self, project: Project, chat: dict, state: dict, *, thinking=False, tools=TOOLS) -> dict:
        if self.stopped():   # stopped before the request went out (e.g. a render took the GPU): don't load the model again
            return {"content": "", "reasoning": "", "finish_reason": "stopped", "tool_calls": [], "usage": {}}
        self.before_request()
        if self.stopped():
            return {"content": "", "reasoning": "", "finish_reason": "stopped", "tool_calls": [], "usage": {}}
        limit = request_limit(self.provider)
        messages = build_messages(project, chat, self.provider)
        if limit and estimate_tokens(messages) + estimate_tokens([{"content": json.dumps(tools or [])}]) > 0.9 * limit:
            self.lean = True
        if self.lean:
            messages = build_messages(project, chat, self.provider, lean=True)
            if limit:
                messages = fit_messages(messages, int(0.85 * limit) - estimate_tokens([{"content": json.dumps(tools or [])}]))
        try:
            return self._send(chat, state, messages, thinking, tools)
        except HTTPError as err:
            learned = learn_request_limit(self.provider, err)
            if learned:
                # the provider's per-request (or per-minute) token limit: the guide in brief, then older messages summarised
                tools_cost = estimate_tokens([{"content": json.dumps(tools or [])}])
                budget = int(0.85 * learned) - tools_cost
                if not self.lean:
                    self.lean = True
                    chat["messages"].append({"role": "notice", "text": f"This provider accepts at most {learned} tokens per request, so the "
                                             "assistant uses the prompt guide in brief (and summarises older messages when needed).", "t": _now()})
                    try:
                        return self._send(chat, state, fit_messages(build_messages(project, chat, self.provider, lean=True), budget), thinking, tools)
                    except HTTPError as again:
                        if not learn_request_limit(self.provider, again):
                            raise
                if self.compact(project, chat):
                    state["phase"] = "compacting"
                    self.update(state)
                    try:
                        return self._send(chat, state, fit_messages(build_messages(project, chat, self.provider, lean=True), budget), thinking, tools)
                    except HTTPError as again:
                        if not learn_request_limit(self.provider, again):
                            raise
                raise LLMError(f"this provider accepts at most {learned} tokens per request (its plan's limit for this model), and the "
                               f"assistant's smallest request is larger (about {estimate_tokens(build_messages(project, chat, self.provider, lean=True)) + estimate_tokens([{'content': json.dumps(tools or [])}])} "
                               "tokens: its instructions, the tools and your project). Use a model or plan with a higher limit "
                               "— for example a paid tier, another model of this provider, or the local model.") from None
            # too long for the model: summarise older messages and try once more
            if not CONTEXT_ERROR.search(err.detail or "") or not self.compact(project, chat):
                raise
            state["phase"] = "compacting"
            self.update(state)
            return self._send(chat, state, build_messages(project, chat, self.provider, lean=self.lean), thinking, tools)

    def _send(self, chat: dict, state: dict, messages: list, thinking, tools) -> dict:
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
            # the reply is resent next time, its hidden reasoning is not
            reasoning = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
            chat["context_tokens"] = int(usage["prompt_tokens"]) + max(0, int(usage.get("completion_tokens") or 0) - reasoning)
        else:   # no usage from this provider: estimate, so compaction still happens in time
            chat["context_tokens"] = estimate_tokens(messages) + len(result.get("content") or "") // 3
        timings = result.get("timings") or {}
        if timings:  # llama.cpp: how much of the prompt came from its cache
            chat["last_request"] = {k: timings.get(k) for k in ("prompt_n", "cache_n", "prompt_ms", "predicted_n", "predicted_per_second")}
        return result

    def run_turn(self, chat: dict, text: str, *, images: list[str] | None = None, thinking=False, note: str = "") -> dict:
        """note: extra context for the model that the chat panel does not show (e.g. what attached frames are)."""
        project = self.open()
        state = {"phase": "starting", "text": "", "reasoning": 0, "tool": None}
        if self.provider.get("vision") and not getattr(self.client, "sees_images", True):
            self.provider = {**self.provider, "vision": False}   # learned: this model refuses pictures
        if self.needs_compaction(chat):
            state["phase"] = "compacting"
            self.update(state)
            self.compact(project, chat)
        seen = scene_fingerprints(project)
        changes = changes_since(chat.get("seen"), seen)
        chat["seen"] = seen
        auto_images, auto_note = ([], "")
        if self.provider.get("vision") and not images:
            try:
                auto_images, auto_note = scene_frames_for(project, text)
            except ProjectError:
                auto_images, auto_note = [], ""
        if auto_note:
            note = (note + "\n" if note else "") + auto_note
        chat["messages"].append({"role": "user", "text": text, "note": note, "images": (images or []) + auto_images, "t": _now(),
                                 "snapshot": snapshot(project, chat), "changes": changes})
        self.turn = len(chat["messages"]) - 1
        self.turn_text = text
        if not chat.get("title"):
            chat["title"] = text.strip().splitlines()[0][:60] if text.strip() else "New chat"
        self.write_chat(chat)
        thinking = resolve_thinking(thinking, project, text, self.provider)
        tools, repeats = TOOLS, 0
        for _ in range(MAX_ROUNDS):
            project = self.open()
            result = self._complete(project, chat, state, thinking=thinking, tools=tools)
            if thinking and result["finish_reason"] == "length" and not result["tool_calls"]:
                # the budget ran out and the model went on reasoning in its reply until the limit: ask once more without thinking
                result = self._complete(project, chat, state, thinking=False, tools=tools)
            result["tool_calls"], result["content"] = repair_tool_calls(result["tool_calls"], result["content"])
            chat["messages"].append({"role": "assistant", "text": result["content"].strip(), "reasoning": result["reasoning"],
                                     "tool_calls": result["tool_calls"], "t": _now()})
            self.write_chat(chat)
            if result["finish_reason"] == "stopped" or not result["tool_calls"]:
                self._notices(chat)
                break
            last_round = tools is None   # the model was asked to answer without tools but still called one: run it, then stop
            start = len(chat["messages"])
            for call in result["tool_calls"]:
                state.update(phase="tool", tool=call["name"])
                self.update(state)
                self.handle_tool(chat, call)
            self.write_chat(chat)
            if last_round:
                break
            self._notices(chat)
            if all(REPEAT in m["text"] for m in chat["messages"][start:] if m["role"] == "tool"):
                repeats += 1
                if repeats >= 2:
                    tools = None  # it keeps repeating waiting cards: one last request without tools, so it answers the user
            else:
                repeats = 0
        return chat

    def _notices(self, chat: dict) -> None:
        """What the client changed on its own (e.g. pictures left out for a text-only model): shown in the chat once."""
        for text in getattr(self.client, "notices", []) or []:
            chat["messages"].append({"role": "notice", "text": text, "t": _now()})
        if getattr(self.client, "notices", None):
            self.client.notices.clear()
            if not self.client.sees_images:
                self.provider = {**self.provider, "vision": False}

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
                    auto_note = ""
                    follow = named_take_to_select(project, getattr(self, "turn_text", ""), name, plan["args"])
                    if follow and not any(p["tool"] == "select_take" and p["status"] == "pending" and p["args"].get("take") == follow["id"]
                                          for p in chat.get("proposals", {}).values()):
                        n_before = project.scene_index(follow["scene"]) + 1
                        sel = plan_proposal(project, "select_take", {"scene": n_before, "take": follow["number"]})
                        auto_id = _new_id("p")
                        auto = {"id": auto_id, **sel, "status": "pending", "created": _now(), "turn": self.turn, "auto": True}
                        chat.setdefault("proposals", {})[auto_id] = auto
                        pending = pending + [auto]
                        plan = plan_proposal(project, name, args, [p for p in pending if p not in replaced])
                        auto_note = (f" Clapper also proposed a card \"{sel['summary']}\": the scene after scene {n_before} continues from"
                                     f" the SELECTED take, and that was not Take {follow['number']}. Tell the user to apply both cards.")
                    bare = {k: v for k, v in plan["args"].items() if k != "echo"}   # the check text is not part of the card
                    same = next((p for p in pending if p["tool"] == name and p["args"] == bare), None)
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
                        echo = plan["args"].pop("echo", None)
                        key = (self.turn, plan["args"].get("scene"))
                        if echo and key not in self._echoed:
                            self._echoed.add(key)
                            if not self.provider.get("vision"):   # it cannot look: point it at the take's text instead
                                echo = re.sub(r"\(view_take [^)]*\)", "(you cannot see it: view_take gives the text that take was made from; its last action is where it ends)", echo)
                            result += " " + echo
                        result += auto_note
                        if name == "add_scenes" and len(plan["args"]["scenes"]) > 1 and asks_for_one_scene(getattr(self, "turn_text", "")):
                            result += (f" Note: the user asked for one scene, and you proposed {len(plan['args']['scenes'])}. If that was not"
                                       " intended, call add_scenes again in this answer with only the scene they asked for (it replaces this card).")
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


_DESCRIBE_FOR = {
    "character": ("a character", "what makes them look like themselves in every picture: age and build, face, hair (style and colour), "
                  "eyes, skin, the outfit piece by piece with colours, shoes, accessories, distinctive marks; then the art style in a few words"),
    "place": ("a place", "what makes it recognisable: the kind of place, its main structures and objects and where they are, materials "
              "and colours, the time of day, weather and light, what is in the background; then the art style in a few words"),
    "prop": ("an object", "its shape, size compared with a hand, materials, colours, markings or symbols on it; then the art style in a few words"),
    "style": ("an art style", "the medium and technique, line work, shading, colour palette, level of detail, lighting and mood"),
}


def describe_image(provider: dict, project: Project, asset_id: str, kind: str | None = None) -> dict:
    """The model looks at a library image once and writes what a person would type for it:
    {"name", "description"} for a new character / place / prop / style (kind), or {"name", "description", "kind"}
    for an uploaded image (kind None: it also says what the image is). The user reviews it before it is used."""
    asset = project.asset(asset_id)
    if asset["kind"] != "image":
        raise ProjectError("only an image can be described")
    if not provider.get("vision"):
        raise LLMError("the active assistant model cannot see images — choose one that can in Assistant settings")
    if kind and kind not in _DESCRIBE_FOR:
        raise ProjectError(f"kind must be one of {sorted(_DESCRIBE_FOR)}")
    if kind:
        what, how = _DESCRIBE_FOR[kind]
        task = (f"This image shows {what} for an AI video. Give it a short name (one or two words; a person's first name if it is a "
                f"person and none is visible, made up to fit) and a description of {how}. The description is added to every image "
                "made of it and sent to the video model, so write it as one dense paragraph of visual facts, 40-90 words, no story, "
                "no feelings, no 'the image shows'.")
        shape = '{"name": "…", "description": "…"}'
    else:
        task = ("This image was added to the library of an AI video project. Say what it is: kind is one of character, place, prop, "
                "style or other (a frame, a logo, a texture …). Give a short title (2-5 words) and a description of 30-70 words of "
                "visual facts (who or what, colours, setting, art style), no story, no 'the image shows'.")
        shape = '{"kind": "…", "name": "…", "description": "…"}'
    known = (asset.get("title") or "").strip()
    question = (task + (f"\n\nIts file is called \"{known}\" (a hint only)." if known else "")
                + f"\n\nAnswer with only this JSON, nothing else: {shape}")
    part = _image_part(project, asset["file"])
    client = Client(provider)
    result = client.chat([{"role": "user", "content": [part, {"type": "text", "text": question}]}], max_tokens=1200, thinking=False)
    if not client.sees_images:
        raise LLMError("the active assistant model cannot see images — choose one that can in Assistant settings")
    text = (result["content"] or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict) or not str(data.get("description") or "").strip():
        data = {"name": "", "description": re.sub(r"^```\w*|```$", "", text).strip()}   # it answered in prose: keep it
    out = {"name": str(data.get("name") or "").strip()[:60], "description": " ".join(str(data.get("description") or "").split())}
    if not kind:
        guess = str(data.get("kind") or "other").strip().lower()
        out["kind"] = guess if guess in (*_DESCRIBE_FOR, "other") else "other"
    return out


# How to write prompts for the image models (FLUX.2 Klein / Qwen-Image-2.1 with reference images), from what was tested in Clapper.
# The Qwen-Image-2.1 graph turns "image 1" into its own <image1> tags, so one set of rules serves every model.
IMAGE_PROMPT_RULES = """Rules for the image model's prompt (it edits and combines reference images):
- The images are numbered in the order given: image 1 is the character / place / object itself; references follow. Refer to them as "image 1", "image 2" ….
- Say what changes and what stays: "The same character as image 1, now wearing …. Keep the face, hair and body."
- When the look changes, do NOT repeat the old description (old clothes, weather, time of day): the model then keeps the old look (a new outfit came out with the old raincoat on top; a "sunny morning" stayed rainy). Name the character, then describe only the new state.
- Name concretely what each reference lends, in a few words: "wearing the clothes from image 2: a red plaid flannel shirt, blue denim overalls and brown work boots", "holding the red umbrella from image 2". Tested: the image models follow named things; "the clothes from image 2" alone was often ignored, and the reference's whole person was pasted in.
- To take clothes (or a pose, the lighting …) from a reference: "wearing the clothes from image 2 instead of their own", "in the pose of image 2", "with the light of image 2". Say what NOT to take from it ("not the face or hair").
- When the weather, light or time of day changes, also say what is gone, because image 1 still shows it: "no rain, dry ground, no puddles", "no snow", "daylight, the street lamp off".
- Concrete, visible words: garments with colours and materials, pose, camera angle, background. No feelings, no story, no "beautiful".
- Characters and props: full body (props: whole object), plain light grey background, no text. Places: the same layout and buildings, no people unless asked, no text.
- 40-110 words. One image description, not a list of options."""


def write_image_prompt(provider: dict, project: Project, entity_id: str, request: str, *, action: str = "look",
                       refs: list | None = None, source: str | None = None, place: str | None = None) -> dict:
    """From the user's plain words, the assistant writes the generation for an entity action: the short change, a
    name for the new look, and the final prompt — looking at the images that will be sent (image 1 and the
    references), when the model can see. Returns {change, name, prompt, explain, images_seen}."""
    from . import tools as studio_tools

    e = project.entity(entity_id)
    request = (request or "").strip()
    if not request:
        raise ProjectError("say in your own words what you want")
    try:
        draft, _ = studio_tools.entity_inputs(project, entity_id, action, request, place_id=place, source=source, refs=refs)
    except ProjectError:
        draft = {"prompt": "", "images": []}
    images = draft.get("images") or []
    parts, seen = [], False
    if provider.get("vision"):
        for n, aid in enumerate(images, 1):
            a = project.assets.get(aid)
            part = _image_part(project, a["file"]) if a else None
            if part:
                owner = project.entities.get(a.get("entity") or "", {}).get("name")
                parts += [{"type": "text", "text": f"Image {n}" + (f" ({owner}" + (f", {a['look']}" if a.get("look") else "") + ")" if owner else "") + ":"}, part]
                seen = True
    what = {"look": "a new look (outfit, hair, accessories) or a variation", "angle": "a new angle or pose",
            "place": f"the character in a place (image 2 is the place)", "sheet": "a turnaround sheet (front, three-quarter, side, back)",
            "first": "first designs from the description alone (no image)"}.get(action, action)
    refs_text = "\n".join((f"- image {images.index(r['asset']) + 1}: take {r.get('use') or 'what fits'}" if r.get('asset') in images
                           else f"- a pose reference (followed as a pose skeleton, not sent as an image): {r.get('use')}")
                          + (f" — {r['what']}" if r.get("what") else "") for r in (refs or []))
    # Which look image 1 is: when it is not the main look, the description describes other clothes (tested: the
    # assistant then dressed a character who started in another outfit in the description's clothes).
    start = project.assets.get(source or e.get("main") or "") or {}
    other_look = bool(source) and source != e.get("main") and action not in ("first", "place")
    start_text = ((f"Image 1 is NOT its main look: it is its look \"{start.get('look') or start.get('title') or 'another look'}\". Keep the "
                   "outfit image 1 shows (unless the user asks to change it) and do NOT write the clothes from the description "
                   "below — they belong to the main look.\n") if other_look else "")
    has_pose = any(str((r or {}).get("use")) == "pose" for r in refs or [])
    pose_text = (("A pose reference is given: Clapper reads its pose skeleton and makes the model follow it (the pose image itself "
                  "is not sent). Do not describe the pose picture's clothes or person. End the prompt with exactly this sentence: "
                  f"\"{studio_tools.POSE_LINE}\"\n") if has_pose else "")
    task = (f"{IMAGE_PROMPT_RULES}\n\nThe user is making {what} for the {e['kind']} \"{e['name']}\".\n" + start_text + pose_text +
            f"Its description (what its MAIN look shows): {e.get('description') or '(none)'}\n"
            + (f"References the user added:\n{refs_text}\n" if refs_text else "")
            + f"Clapper's automatic prompt for this (a starting point; improve it):\n{draft.get('prompt') or '(none)'}\n\n"
            f"The user's words: \"{request}\"\n\n"
            + ("Look at the images above before writing.\n" if seen else "You cannot see the images; rely on the descriptions.\n")
            + 'Answer with only this JSON: {"change": "the change in a few words, as it would complete \\"now …\\"", '
              '"name": "a short name for the new look, 2-4 words, without the name of the character or place (e.g. red winter coat, foggy dawn)", "prompt": "the complete prompt for the image model", '
              '"explain": "one short sentence for the user: what you did and anything to check"}')
    client = Client(provider)
    result = client.chat([{"role": "user", "content": [*parts, {"type": "text", "text": task}] if parts else task}], max_tokens=1500, thinking=False)
    data = _json_answer(result["content"])
    if not data.get("prompt"):
        raise LLMError("the assistant did not write a prompt — try again, or say it differently")
    change = re.sub(r"^(?:the same \w+ as image 1 \([^)]*\),?\s*)?(?:now\s+)?", "", " ".join(str(data.get("change") or "").split()), flags=re.I)
    prompt = _clean_prompt(str(data["prompt"]), e["kind"])
    if has_pose and studio_tools.POSE_LINE not in prompt:   # the graph replaces this line with each model's pose instruction
        prompt = prompt.rstrip() + " " + studio_tools.POSE_LINE
    return {"change": change[:200], "name": _look_name(str(data.get("name") or ""), e["name"]),
            "prompt": prompt, "explain": " ".join(str(data.get("explain") or "").split())[:300],
            "images_seen": seen and client.sees_images}


# What each image model needs in a prompt (keys: engine/tools.py IMAGE_MODELS).
IMAGE_MODEL_HINTS = {
    "flux2_klein_9b": "FLUX.2 Klein: concrete, compact visual description; it follows reference images closely. It takes no "
                      "negative prompt, so say what must NOT appear inside the prompt itself (\"no text\", \"no rain\").",
    "qwen_image_21_realism": "Qwen-Image-2.1, photoreal: write it as the caption of a finished photograph in full sentences "
                             "(the kind of shot, the person with an adult age, what they do, where, the light, the framing), "
                             "not a tag list. Start with \"A photo of …\" unless the user wants another style. It takes a "
                             "negative prompt.",
    "qwen_image_21_anime": "Qwen-Image-2.1, anime: start with \"An anime illustration of …\" and describe the scene in full "
                           "sentences (characters, poses, camera angle, background, light, colours); it follows long, "
                           "detailed prompts closely. It takes a negative prompt.",
}


# The Image tool's prompts: any picture, not only a character's look (IMAGE_PROMPT_RULES are for those).
TOOL_IMAGE_RULES = """Rules for the image model's prompt:
- Describe one finished picture: the subject, what they do, where, the light, the camera angle and framing. Concrete, visible words: garments with colours and materials, objects, surfaces, weather, time of day. No feelings, no story, no "beautiful", no quality buzzwords.
- The setting is what the user asks for (a café, a street, a room). Use a plain background only when the user asks for one or for a design sheet.
- Never contradict yourself: what the picture shows must not also be ruled out ("rain on the window" and "no rain" cannot both be in it).
- With images: they are numbered in the order given; refer to them as "image 1", "image 2" …. Name concretely what each one lends ("the red plaid shirt and denim overalls from image 2"); the models follow named things. Say what to keep from each ("the same woman as image 1, keep her face and hair") and what changes. When a person keeps their identity but the clothes, light or weather change, describe only the new state.
- No readable text in the picture unless the user asks for it; then quote it exactly.
- 40-120 words. One description, not a list of options."""


def _negative_without(negative: str, prompt: str) -> str:
    """A negative prompt without anything the prompt asks for ("rain" goes when the prompt has rain on the window)."""
    low = prompt.lower()
    items = [re.sub(r"^(?:no|without)\s+", "", t.strip(), flags=re.I) for t in negative.split(",")]
    keep = [t for t in items if t and t.lower() not in low]
    return ", ".join(dict.fromkeys(keep))


def write_tool_image_prompt(provider: dict, project: Project, request: str, *, prompt: str = "", model: str | None = None,
                            images: list | None = None, cast: list | None = None, negative: str = "") -> dict:
    """The Image tool's ✨: the user's plain words (and the prompt so far, the chosen library images and characters) →
    {prompt, negative, explain, images_seen} written for the chosen image model."""
    from . import tools as studio_tools
    from .director import project_settings

    request, prompt = (request or "").strip(), (prompt or "").strip()
    if not request and not prompt:
        raise ProjectError("say in your own words what you want, or write a prompt to improve")
    model_id = studio_tools.resolve_image_model({"model": model}, project_settings(project))
    m = studio_tools.IMAGE_MODELS[model_id]
    run = studio_tools.with_cast(project, {"images": list(images or []), "cast": list(cast or []), "prompt": ""})
    ids = run.get("images") or []
    parts, seen = [], False
    if provider.get("vision"):
        for n, aid in enumerate(ids, 1):
            a = project.assets.get(aid)
            part = _image_part(project, a["file"]) if a and a.get("kind") == "image" else None
            if part:
                owner = project.entities.get(a.get("entity") or "", {}).get("name")
                parts += [{"type": "text", "text": f"Image {n}" + (f" ({owner})" if owner else "") + ":"}, part]
                seen = True
    who = run.get("prompt", "").strip()
    takes_negative = m.get("negative") is not None
    task = (f"{TOOL_IMAGE_RULES}\n\nThe image model: {IMAGE_MODEL_HINTS.get(model_id, m['title'])}\n"
            + (f"The images sent with the prompt, in order: {len(ids)} (image 1 … image {len(ids)}). {who}\n" if ids
               else "No image is sent: the prompt alone makes a new picture. Then the 40-110 word limit and \"image N\" rules do not apply to references; describe everything.\n")
            + (f"The prompt so far (improve, complete or rewrite it as the user asks):\n{prompt}\n" if prompt else "")
            + (f"The negative prompt so far: {negative}\n" if takes_negative and negative.strip() else "")
            + f"\nThe user's words: \"{request or 'make this prompt better'}\"\n\n"
            + ("Look at the images above before writing.\n" if seen else ("You cannot see the images; rely on the names and descriptions.\n" if ids else ""))
            + 'Answer with only this JSON: {"prompt": "the complete prompt for the image model"'
            + (', "negative": "a short comma-separated list of plain words for what to avoid (quality defects such as blurry, extra fingers, distorted face; unwanted styles; things that would spoil THIS picture) — never anything the prompt asks for nor the picture style itself (an anime picture never lists cartoon or flat style), and no \\"no\\" in front"' if takes_negative else "")
            + ', "explain": "one short sentence for the user: what you did and anything to check"}')
    client = Client(provider)
    result = client.chat([{"role": "user", "content": [*parts, {"type": "text", "text": task}] if parts else task}], max_tokens=1500, thinking=False)
    data = _json_answer(result["content"])
    if not data.get("prompt"):
        raise LLMError("the assistant did not write a prompt — try again, or say it differently")
    out = {"prompt": _clean_prompt(str(data["prompt"]), "image"), "explain": " ".join(str(data.get("explain") or "").split())[:300],
           "images_seen": seen and client.sees_images, "model": model_id}
    if takes_negative and data.get("negative"):
        out["negative"] = _negative_without(" ".join(str(data["negative"]).split()), out["prompt"])[:400]
    return out


# How to put feeling into a line for Qwen3-TTS voice clone (the Speech tool). Its voice-clone model has no emotion or
# style input: the delivery comes from the words and punctuation, the voice sample's own delivery, and the temperature.
SPEECH_TEXT_RULES = """Rules for a line spoken by a voice-clone text-to-speech model (Qwen3-TTS). It reads EVERY character aloud and has no emotion setting: the feeling must be carried by the words themselves.
- Write the exact words the speaker says, in their own voice: first person ("I", "you"), spoken to the listener. The user usually describes the moment in the third person ("she admits she missed him"); turn that into what she says ("I missed you."), never a narration of it.
- Write only what is spoken. No stage directions, no (brackets), no [tags], no *asterisks*, no emojis, no speaker names, no quotation marks around the whole line: the model would read them aloud or stumble.
- Show the emotion through the words: interjections and sounds people really make ("Oh…", "Ah—", "Hey!", "Hmm.", "Ugh,", "Haha,"), word choice, repetition ("No. No, no, no."), unfinished sentences, a question where someone pleads or doubts.
- Use punctuation as the delivery: "…" for a hesitant or trailing pause, "—" for a break or interruption, "," for a short breath, "." for a firm stop, "!" for force or joy, "?" for a rising question. Short sentences sound tense or angry; long flowing ones sound calm or warm.
- Hesitation and nerves: stammer the first word ("I— I didn't mean it"), fillers ("um", "well…"). Excitement: quick short phrases with "!". Sadness: slower, with "…" and softer words. Anger: short, hard sentences, no fillers. Whispering or tenderness: gentle words, commas, "…".
- Write numbers, units and abbreviations as they are spoken ("twenty-five", "doctor"). Keep names consistent.
- Write the line in the language it should be spoken in. A few seconds of speech is about 2-3 words per second; keep it to what fits.
- One speaker per line: the Speech tool speaks in one voice. For a dialogue, write one line per speaker and say so.
- Do not overdo it: one or two emotional cues per sentence are enough; every word in capitals or "!!!" sounds unnatural."""


def write_speech_text(provider: dict, project: Project, request: str, *, text: str = "", language: str = "Auto",
                      voice: str | None = None, voice_text: str = "") -> dict:
    """The Speech tool's ✨: the user's words (an idea, a draft line or a dialogue to finish) → {text, explain,
    temperature, lines} — the spoken text written so the voice-clone model carries the emotion through words and
    punctuation (see SPEECH_TEXT_RULES)."""
    request, text = (request or "").strip(), (text or "").strip()
    if not request and not text:
        raise ProjectError("say what should be said and how it should feel, or write a line to improve")
    speaker = ""
    if voice:
        a = project.assets.get(voice) or {}
        owner = next((e for e in project.entities.values() if e.get("voice") == voice), None)
        if owner:
            speaker = f"The speaker is {owner['name']}" + (f": {owner['description']}" if owner.get("description") else "") + "."
        sample = voice_text or a.get("transcript") or ""
        if sample:
            speaker += f" The voice sample says: \"{sample}\" (its delivery carries over into the new line)."
    task = (f"{SPEECH_TEXT_RULES}\n\n" + (f"{speaker}\n" if speaker else "")
            + "Language: " + (language if language and language != "Auto" else "the language of the user's line (or English)") + ".\n"
            + (f"The line so far (finish, improve or rewrite it as the user asks; keep what they wrote unless asked to change it):\n{text}\n" if text else "")
            + f"\nThe user's words: \"{request or 'make this line sound natural and give it the right feeling'}\"\n\n"
            'Answer with only this JSON: {"text": "only the words to be spoken", "emotion": "the feeling in 1-3 words", '
            '"temperature": a number 0.7-1.2 (higher = more expressive; about 1.0-1.1 for strong emotion, 0.8 for calm narration), '
            '"lines": [only for a dialogue: {"speaker": "name", "text": "…"} per line, else []], '
            '"explain": "one short sentence for the user: how the feeling is carried. The user does not read the line aloud (the voice model does): any tip is about the tool — a voice sample spoken in that mood, or the Expressiveness setting"}')
    client = Client(provider)
    result = client.chat([{"role": "user", "content": task}], max_tokens=1500, thinking=False)
    data = _json_answer(result["content"])
    lines = [{"speaker": " ".join(str(l.get("speaker") or "").split())[:40], "text": _spoken(str(l.get("text") or ""))}
             for l in (data.get("lines") or []) if isinstance(l, dict) and l.get("text")]
    # a dialogue: the form gets the first line (one voice per run); the lines come back separately
    spoken = lines[0]["text"] if len(lines) > 1 else (_spoken(str(data.get("text") or "")) or (lines[0]["text"] if lines else ""))
    if not spoken:
        raise LLMError("the assistant did not write a line — try again, or say it differently")
    try:
        temp = min(1.3, max(0.5, float(data.get("temperature"))))
    except (TypeError, ValueError):
        temp = None
    return {"text": spoken, "emotion": " ".join(str(data.get("emotion") or "").split())[:40], "temperature": temp,
            "lines": lines if len(lines) > 1 else [], "explain": " ".join(str(data.get("explain") or "").split())[:300]}


def _spoken(text: str) -> str:
    """Only what is said: stage directions, tags, emojis and markup that the speech model would read aloud go."""
    text = re.sub(r"\([^)]*\)|\[[^\]]*\]|\*[^*]*\*|<[^>]*>", " ", text)
    text = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", text)
    text = re.sub(r"^\s*[A-Z][\w .'-]{0,30}:\s+", "", text)   # a leading "Name:" speaker label
    text = " ".join(text.split()).strip()
    if len(text) > 1 and text[0] == text[-1] and text[0] in "\"“”'":
        text = text[1:-1].strip()
    return re.sub(r"\s+([,.!?…])", r"\1", text)


# What to name for each kind of reference (assistant.describe_ref_use): only that, never the person.
_REF_WHAT = {
    "outfit": "only the clothing and shoes: each garment with its colour, material and pattern (for example: cream cable-knit sweater, olive cargo trousers, black ankle boots)",
    "hair": "only the hairstyle: length, colour, cut and styling (for example: short curly auburn bob with a side fringe)",
    "accessory": ("only the accessories: objects that are NOT clothing or shoes — glasses, bags, umbrellas, hats, jewellery, watches, "
                  "scarves, anything held — each with colour and material and how it is worn or held, as -ing phrases (for example: "
                  "wearing a wide straw sun hat, holding a green canvas tote bag in the left hand, silver hoop earrings)"),
    "colours": "only the main colours, as a palette (for example: dusty pink, navy, mustard)",
    "lighting": "only the light: its colours, direction, contrast, time of day and weather (for example: warm low sunlight from the left, long shadows, golden hour)",
    "background": "only the place and setting, without people (for example: a snowy pine forest at dawn with mist between the trees)",
    "material": "only the material and surface: texture, sheen, colour (for example: rough dark brown leather with visible stitching)",
    "style": "only the art style: medium, line work, shading and colouring (for example: flat cel-shaded anime, bold black outlines, two-tone shadows)",
}


def describe_ref_use(provider: dict, project: Project, asset_id: str, use: str) -> dict:
    """Name what a reference image lends for one use (its outfit, hairstyle, accessories, lighting …) in a short phrase
    that goes into the image prompt — tested: the image models follow named things, not "the clothing from image 2".
    Returns {what}."""
    a = project.asset(asset_id)
    if a["kind"] != "image":
        raise ProjectError(f"{a['title']} is not an image")
    if not provider.get("vision"):
        raise LLMError("the assistant's model cannot see images: type what to take yourself")
    part = _image_part(project, a["file"])
    if not part:
        raise ProjectError("cannot read this image")
    ask = _REF_WHAT.get(use, f"only {use}")
    task = (f"Look at the image and name {ask}. Never mention the person, their face, body, age or gender. "
            'A short comma-separated phrase, at most 25 words. Answer with only this JSON: {"what": "…"}')
    client = Client(provider)
    result = client.chat([{"role": "user", "content": [part, {"type": "text", "text": task}]}], max_tokens=400, thinking=False)
    data = _json_answer(result["content"])
    what = " ".join(str(data.get("what") or result["content"] or "").split()).strip(" .\"")
    if not what or what.startswith("{"):
        raise LLMError("the assistant could not name it — type what to take yourself")
    return {"what": what[:200]}


# Small models copy pieces of their instructions into the answer: take those out again.
_ECHO = re.compile(r"\s*\(?\b\d+\s*[-–]\s*\d+ words\b\.?\)?\.?|\s*\bno (?:visible )?distinctive marks\b[^.]*\.?", re.I)


def _clean_prompt(text: str, kind: str) -> str:
    text = _ECHO.sub("", " ".join(text.split()))
    if kind == "place":   # a place keeps its own surroundings
        text = re.sub(r"\s*,?\s*plain (?:light )?grey background,?", "", text, flags=re.I)
    text = re.sub(r"\s+([.,])", r"\1", text)
    return re.sub(r"([.,])[.,]+", r"\1", text).strip()


def _look_name(name: str, owner: str) -> str:
    """A look's name without the character's / place's own name ("foggy dawn", not "foggy dawn bus stop")."""
    name = re.sub(re.escape(owner), "", " ".join(name.split()), flags=re.I) if owner else name
    return " ".join(name.strip(" -–:,").split())[:48]


def write_description(provider: dict, project: Project, kind: str, words: str, *, name: str = "", asset_id: str | None = None) -> dict:
    """The user's own words about a character / place / prop / style → {name, description} written for the image
    and video models (and checked against its image, when there is one and the model can see)."""
    if kind not in _DESCRIBE_FOR:
        raise ProjectError(f"kind must be one of {sorted(_DESCRIBE_FOR)}")
    words = (words or "").strip()
    if not words and not asset_id:
        raise ProjectError("write a few words about it first")
    what, how = _DESCRIBE_FOR[kind]
    part = None
    if asset_id and provider.get("vision"):
        a = project.asset(asset_id)
        part = _image_part(project, a["file"]) if a["kind"] == "image" else None
    task = (f"Write the description of {what} for an AI video project: {how}. It is added to every image made of it and sent "
            "to the video model, so: one dense paragraph of visual facts, 40-90 words, no story, no feelings, no names of "
            "other characters. Keep everything the user said (it is right); add the visual details it needs (build, face, "
            "hair, colours, clothes piece by piece); do not invent distinctive marks the user did not mention (scars, "
            "tattoos, jewellery, accessories), and nothing that contradicts them." + (" Check it against the image and use what you see for what the user left out." if part else "")
            + f"\n\nIts name: {name or '(none yet: suggest one)'}\nThe user's words: \"{words or '(none: describe the image)'}\"\n\n"
            'Answer with only this JSON: {"name": "…", "description": "…"}')
    client = Client(provider)
    result = client.chat([{"role": "user", "content": [part, {"type": "text", "text": task}] if part else task}], max_tokens=1200, thinking=False)
    data = _json_answer(result["content"])
    if not data.get("description"):
        raise LLMError("the assistant did not write a description — try again")
    return {"name": " ".join(str(data.get("name") or name).split())[:60], "description": _ECHO.sub("", " ".join(str(data["description"]).split())).strip()}


def _json_answer(text: str) -> dict:
    """The JSON object in a model's answer (models add prose or code fences around it)."""
    text = (text or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


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

# Changelog

## Unreleased

- Audio clips anchored in a scene: the scene's soundtrack becomes the clip from a chosen time and the picture is generated around it (`spec.audio_guides`; CLI `take --anchor-audio FILE@SECONDS`).
- CLI `take`: `--unet` and `--lora` to render with another model, e.g. the Ref2VA model for a scene made from references only.
- Change password from the profile menu (current password required; your other devices are logged out). Changing a password from the command line now logs that profile out everywhere.
- Header redesigned for phones, tablets and laptops: always one row. A status pill replaces the machine stats, queue indicator and GPU pill (rendering progress, who holds the GPU, waiting jobs; GPU load and VRAM on wide screens) and opens the machine / queue / GPU panel. ✨ Assistant and Export stay in the bar (icons on phones); References and Settings move into the profile menu on narrower screens, together with Storage, the ComfyUI log, Assistant settings and Log out. The chat and the scene editor open below the header.
- Assistant proposals are checked more strictly: scene text must have the three H3 fields once each, in order, not empty, with at most one line before them, and `[Shot 1]`; every spoken line must be `<d>[Language] words</d>` (closed with `</d>`) in its language's own script; proposals are checked together with the ones still waiting (a new style text that would break a proposed scene is caught); scenes that repeat or nearly repeat (≥ 90%) each other, a scene in the project or one in a waiting card are refused, naming that scene; a missing narration or style text is flagged.
- Safe repairs before the checks, never changing words: a spoken line closed with `<d>` instead of `</d>`, `</ d>`-style tag slips, `[shot 1]`, field names with spaces, capitals or bold, a missing `non_diegetic_music: N/A`, a common `join` slip ("start", "continuous"); the style text's `@names` are added to a proposed scene's own reference list.
- **Apply** re-checks a card against the project as it is now and refuses it, writing nothing, if a scene it adds or rewrites, or one that rendered before, would no longer compile.
- A corrected `add_scenes` card in the same answer replaces the earlier one (instead of adding the scenes twice), and several edits of one scene in one answer become one card; an answer that only repeats waiting cards ends instead of looping.
- Assistant thinking: a thinking mode per provider in Assistant settings (Off / Auto: only when planning a video / Always) and a thinking budget, also used by **think harder**; the default is now Auto with 2048 tokens (measured best). A reply cut off by the budget is retried without thinking. Optional sampling parameters per provider (`llm.json`).
- `tools/assistant_eval.py`: score a model's prompts and tool use against the prompt guide, next to a project's own scenes; checks dialogue tags, scene count against the brief and duplicates; `--thinking off|auto|N`, `--sampling`, `--repeat N`.
- Assistant: a chat panel (✨ Assistant) with a language model that knows Clapper's workflow and the prompt guide. It plans videos (style text, reference definitions, all scenes), rewrites scenes, changes settings and proposes renders — every change is a card you apply or reject, checked before you see it. It sees the whole project with every message, can look at reference images, reviews takes from their frames (✨ Review), and keeps story notes and reference cards as lasting memory. Chats are saved per project; older messages are summarised automatically at 60% of the context (context meter in the panel).
- Local or online model: a local model through llama-swap (with vision via its projector), or any OpenAI-compatible API. Assistant settings in the status panel and the profile menu; API keys stay on the server.
- GPU controls (status panel): the status pill shows whether H3 or the assistant holds the graphics card (*GPU · Video* / *GPU · AI* / *GPU · Free*). Auto mode loads the assistant when you ask it something and ejects it when any render is queued (also from ComfyUI's own page), saving and restoring its conversation cache; manual mode uses Load / Eject / Free buttons only.
- Story notes (References dialog), reference cards with ✨ Describe, ✨ Plan the video with the assistant on an empty storyboard, ✨ Ask the assistant in the scene editor.
- Profiles: Clapper opens with a login (profile id + password); each profile sees only its own projects. Only one profile can be logged in and active at a time; the same profile may be logged in on several devices. Log out frees Clapper on every device; an unused session stops blocking others after 10 minutes. CLI `profile list/add/password/assign/unassign/remove/logout`. The first profile can be created on the login screen and gets the existing projects.
- New project needs only a name: choose where to save it from a list (default folder, recent places, project folders) or a tap-to-browse folder picker with New folder and Make default; the folder is named after the project; a preview shows the exact location and whether it would open an existing project.
- Disk space per project on the project list and project screen (takes, exports, references, history).
- Delete a project: everything on disk (typed-name confirmation, refused while rendering) or only remove it from the list. CLI `usage` and `delete`.
- Double-click protection: *Render take* is disabled from the moment you click it until the take is queued, so a double click no longer queues two renders. The same applies to *Render remaining*, *Re-queue*, *Stop* / *Cancel* / *Stop all*, *Export*, and in the assistant panel *Apply* / *Render* / *Reject*, *Apply all* and *Send* (also with Enter). The server also refuses the same render of a scene (same seed, overrides and parent take) and the same re-queue for 5 seconds (HTTP 409, "a take of this scene was queued a moment ago"), so a phone double tap or a retried script cannot queue duplicates; a different seed renders at once.
- The ComfyUI sidebar tab's button reads *Open Clapper*.
- Documentation for new users and contributors: README front page, `CONTRIBUTING.md`, `SECURITY.md`, issue and pull request templates; user guide and reference checked against the code (Continuum import and command-line workflows, tools, error codes).

## 0.1.0 — 2026-09-24

First version.

- Projects of scenes and takes; takes numbered per scene with created/finished times; one selected take per scene.
- Masked audio-video continuation between scenes (22 carried frames, adapted from ComfyUI-H3-Continuum); keyframe and bridge options (untested).
- Per-scene references with project defaults, `@name` numbering per scene, and per-reference definitions.
- Lineage badges (*previous scene changed*, *prompt edited since take*, …); nothing re-renders silently.
- Render one take, render the remaining scenes as a chained queue, re-queue interrupted takes, stop/cancel with dependent takes.
- Restore a take's own prompt into its scene.
- Export: trims carried frames, fades audio at joins, warns about continuity, asks before a partial export; manifest of takes used.
- Web interface served by ComfyUI (desktop and phone): storyboard, scene editor, references, settings, export, live progress, machine stats, ComfyUI log viewer; ComfyUI sidebar tab.
- Autosave with history; recovery after restarts; render time and seconds per step recorded per take.
- CLI and HTTP API for everything; Continuum Timeline import; adopting existing takes.
- Defaults equal a proven Continuum draft setup (FL2VA int8, Turbo 8-step LoRA at 20 steps, res_multistep/simple, 0.3 MP).

# Clapper design

Why Clapper is built the way it is, what has been verified, and what is next. Status labels: **done** (built and verified), **untested** (built, not yet verified on a GPU), **planned**.

## Goals

Long-form MiniMax H3 video with the working style of a film set:

1. Regenerate any scene at any time, including after the full video exists.
2. Keep every take; choose any earlier take for a scene.
3. Change the prompt and the references per scene.
4. Replace a scene with a different one that continues from the scene before it.
5. Extend at the end; insert between scenes.
6. Per-scene references (images, voices, videos): a scene without its own list uses the project defaults, and unused references cost nothing.
7. Consistent voices across scenes and across projects.
8. An easy interface on desktop and phone; everything also drivable from a command line or by an assistant.
9. Everything saved as a project: close, come back, continue where you left off without regenerating anything.
10. Speed first for drafts (correct action, characters and sound matter more than pixels); upscale later as a separate step.

## Why not only ComfyUI-H3-Continuum

Continuum generates long H3 video as linked chunks and is the proven base (its masked audio-video continuation and prompt conventions are reused here). Its model is a linear chain inside one run:

| Need | Continuum | Clapper |
|---|---|---|
| Regenerate a chunk | yes; every later chunk regenerates too | the scene only; later scenes are flagged, never re-rendered silently |
| Keep and choose takes | per branch (a take plus everything after it) | per scene, any combination |
| References per chunk | no — one set for the whole run; changing it regenerates everything | yes, per scene with project defaults |
| Insert between chunks | no | yes (bridge join untested) |
| Extend at the end | yes | yes |

These limits come from Continuum's run identity (the reference set is part of the run-wide contract) and its single-run structure, not from H3. Clapper therefore renders every scene as its own ComfyUI job and keeps the chain in its own project file.

## Architecture

```
Browser (desktop / phone)          CLI (cli/clapper.py)          Assistant
        │  /clapper/api/*                   │ HTTP                       │
        ▼                                   ▼                            ▼
ComfyUI server ── comfyui_clapper ──────────────────────────────────────────────
   api.py        routes; ComfyUI calls in a worker thread (never block the event loop)
   assistant_api.py  assistant + GPU routes; one assistant turn at a time in a background thread
   gpu.py        who holds the GPU (H3 or the local assistant model); auto-eject on every queued job
   live.py       observes ComfyUI progress events in-process
   sysstats.py   CPU / RAM / GPU / VRAM / temperatures
   engine/       pure Python, no torch — usable from the CLI and tests
      project.py   project store, @name compile, definitions, badges, take numbering
      director.py  plan / queue / chain / refresh / requeue
      graph.py     ComfyUI API graph for one take
      export.py    export plan, checks, ffmpeg join
      importer.py  Continuum Timeline import, adopting existing takes
      settings.py  defaults and presets
      profiles.py  profiles, project ownership, the one-profile-at-a-time session lock
      repeats.py   short-window refusal of repeated render / re-queue requests
      llm.py       OpenAI-compatible chat client, provider settings, llama-swap load/eject/cache
      assistant.py instructions, project snapshot, tools, proposals, chats, compaction
   nodes.py      Clapper · Scene Conditioning, Clapper · Save Take (torch side)
   web/          the interface (plain HTML/CSS/JS, no build step)
   js/           ComfyUI sidebar tab
Project folders on disk (project.json, refs/, takes/, exports/, history/, chat/)
tools/            join_check.py (measure a join), assistant_eval.py (score the assistant's scenes)
```

Key decisions:

- **A full page served by ComfyUI**, not a node on the canvas: a storyboard with video takes needs room and must work on a phone.
- **One ComfyUI job per take**, built from core H3 nodes plus two small Clapper nodes. Chained renders are queued at once: ComfyUI runs its queue in order, so a take can point at its parent's folder before the parent has finished.
- **Takes save their raw latents.** Continuing from a take copies its last latent slots; no re-encoding, so the join is exact.
- **Lineage instead of cascades.** Each take records its parent take; badges report when a scene's parent is no longer the previous scene's selected take.
- **Flag and keep.** Nothing re-renders without an explicit action.
- **Prompt parity.** For a project imported from a Continuum Timeline prompt with all references active, Clapper's compiled prompt is character-for-character identical to Continuum's per-chunk prompt. Re-check this when changing compile or import code.
- **References carry their own definitions**, inserted only into scenes that use them, so per-scene reference lists never leave dangling mentions.
- **Defaults equal a proven Continuum draft setup**; faster presets are opt-in.
- **Plain files, atomic writes, history.** Anyone can read, back up or repair a project.
- **A click queues one job.** GPU work is expensive, so buttons that queue it (or change something for good) ignore clicks while their request is out, and the server refuses an identical render or re-queue for 5 seconds. The server check is keyed on the parameters, so a deliberate second take with another seed is never blocked.
- **The assistant proposes; the user applies.** A small local model makes mistakes, so every project change is a card, checked on an in-memory copy of the project before the user sees it (errors go back to the model). Only the assistant's own memory (story notes, reference cards) is written directly.
- **Lasting facts live in the project, not in the chat.** The model gets a fresh project snapshot with every message, plus story notes and reference cards; the chat can then be summarised freely without losing anything that matters.
- **Cache-friendly prompts.** Fixed instructions first, the snapshot only on the newest message, so each request repeats the previous one byte for byte and llama.cpp reuses its cache. Before an eject the cache is filled to the exact point the next message extends and saved; after the reload it is restored.
- **Deterministic context for common actions.** Review attaches the take's frames itself instead of trusting the model to ask for them.
- **One provider interface.** Everything speaks the OpenAI chat API; the local kind adds only load / eject / cache. Online providers need no GPU switching.
- **One profile at a time.** Several people may share one GPU, so logging in is refused while another profile is active; the same profile may be on several devices. "Active" means a request within 10 minutes (open pages poll), so a forgotten browser does not lock the machine for long. Ownership is kept in `~/.clapper/profiles.json`, not in the project folder, so projects stay plain and movable. This is privacy inside Clapper, not a security boundary: ComfyUI itself has no login.

## Verified results (RTX 5060 Ti 16 GB, 31 GB RAM, ComfyUI 0.34)

| Check | Result |
|---|---|
| First scene at default settings | 124 frames at 416×736, ≈13.6 s/step, ≈5.5 min including model load; same look and action as the equivalent Continuum chunk |
| Continued scenes (masked, 22 frames + audio) | ≈15.8 s/step, ≈6 min per 141-frame take; joins show no jump (change across the join below normal in-take motion) and no audio clicks |
| Prompt parity with Continuum | identical for all scenes of a 10-scene Timeline import |
| Per-scene references | a scene rendered without the logo reference showed no logo artwork (the model still wrote the brand name mentioned in the text) |
| Alternative take of a middle scene | rendered, selectable, exported; next scene flagged as expected |
| Stop | interrupts a running take at once; queue cleared; take marked interrupted |
| Export | 5 scenes → exactly 25.000 s / 600 frames |
| Local assistant (Ternary Bonsai 27B abliterated, Q2, + Q8 projector, 128k context, q8 KV) | 12.7 GB VRAM; load 3–6 s; eject 1 s; 45 tokens/s written, 930 tokens/s read |
| GPU switching in auto mode | render queued → conversation saved (8,104 tokens) and model ejected within 0.9 s, H3 loads; next chat message → H3 unloaded, model loaded, cache restored in ≈5 s (9,119 tokens reused, 358 read) |
| Assistant planning a 15 s ad | style text, a reference definition and 3 scenes with correct fields and references in ≈25 s; invalid proposals rejected by the checks and fixed by the model |
| Assistant prompt quality (`tools/assistant_eval.py`, 50 s / 10-scene Hindi-narrated ad; hard rules of the prompt guide) | hand-written scenes 100%; local model without checks 68% (scenes with one line only, missing fields, style text breaking scenes' references); with thinking 87% but no narration at all and ignored task scope (added 9 scenes when asked for 1), 2–3× slower; with the stricter proposal checks 89% — every applied scene structurally valid, but many retries (missing music field), little narration (2/11), and the style text never got past the checks |
| Assistant settings sweep (same eval, after the `<d>`/structure/near-duplicate/apply checks, the style-reference completion and the budget fallback; right tool of 4 tasks; time per run of all 4) | thinking off 98% (2 runs), 3.0/4, 241 s · **auto, budget 2048 (default)** 96% (2 runs), 4/4, 327 s · always 1024: 100% but planning ran out of budget and made no tool call (1/4, fixed since by retrying without thinking) · always 2048 93%, 4/4, 412 s · always 4096 100%, 3/4, 633 s · demo sampling (0.7 / 0.95 / top-k 20 / min-p 0) 94–99%, 2.5–3.5/4, not reliably better · Qwen-style sampling 90%. Structure, `@names` and dialogue tags now pass in every configuration; what remains is scenes without narration and task scope. `thinking_budget_tokens` is honoured per request by this llama-server build |
| Assistant vision | overall look right; small details wrong or invented at 2 bits (eye and beak colour); take reviews unreliable (proposed changes to a take that matched its scene) |
| Reference following on the FL2VA model | characters and logo reproduced closely (the hybrid FL2VA+Ref2VA model is not required for images) |
| Ref2VA int8 + Ref2V Turbo 4-step LoRA at 4 steps, fresh scene from references only (no start image) | 124 frames at 416×736 in ≈114 s including model load; the character reproduced closely, action followed; look a little softer than the style sheet |
| Ref2VA, start image pinned at frame 0 (as for FL2VA) | opens exactly on the image, keeps its flat style, character and action right; ≈114 s — no FL2VA needed for an image start |
| Ref2VA, masked continuation (22 frames + audio) | ≈144 s per 141-frame take; join clean (frame change across the join 7.8 vs 6.8–6.9 inside the takes; carried overlap diff 2.2); later prompt beats followed only partly. One model can make a whole video |
| Anchored audio clip (`audio_guides`, at 0.5 s) | the soundtrack is the clip: envelope correlation 0.995 at zero lag, waveform correlation 0.95 (an unanchored take: 0.28 / 0.03); mouths stayed closed as prompted |
| Voice reference (`<Audio 1>`) with new words | new words spoken (envelope correlation with the sample 0.13); pitch median 115 Hz vs the sample's 113 Hz (no voice reference: 131 Hz), spectral similarity 0.96 vs 0.84 — likely the same voice; not yet confirmed by listening |
| Qwen3-TTS 1.7B Base, voice clone from a saved voice, English | a 4.4 s line in 12 s including model load |
| Ref2VA: Ref2V Turbo LoRA at 4 vs 20 steps (same seed, start image) | 114 s vs 396 s; frames nearly identical, no visible gain at 20 — 4 steps is the Ref2VA default |

## Roadmap

The next version's plan (library, image and speech tools, start modes, finishing) is in [PLAN_0.2.md](PLAN_0.2.md).

| Item | Status |
|---|---|
| Scenes, takes, selection, lineage badges, autosave, history | done |
| Masked continuation, chained render-rest, export with checks | done |
| Web interface, live progress, stop, logs, machine stats, phone layout | done |
| Double-click protection: one request per click for renders, re-queue, stop, export and assistant actions; server refuses identical renders / re-queues within 5 s | done |
| Continuum Timeline import, adopting takes | done (command line) |
| Profiles: login, private project lists, one active profile at a time, CLI management | done |
| Profile management in the web interface (add profiles, change password, hand over projects) | planned |
| Assistant: chat, proposals, story notes, reference cards, take review, compaction, local + OpenAI-compatible providers, GPU manager (auto/manual) | done |
| Assistant evaluation script: scores the model's scenes against the prompt guide, with the project's own scenes as baseline (`tools/assistant_eval.py`) | done |
| Assistant: thinking per task (auto: only for planning), thinking budget and sampling per provider, measured with `tools/assistant_eval.py` | done |
| Assistant: stronger reviews (an online model for reviews only) | planned |
| Assistant: per-task model choice (e.g. local for small edits, a stronger model for planning); compare local candidates with `tools/assistant_eval.py` | planned |
| Assistant: native Anthropic / Gemini adapters | planned |
| Assistant: show a proposed scene's compiled H3 prompt on its card | planned |
| Keyframe continuation method | untested |
| Bridge join (insert/replace keeping later scenes) | untested |
| Audio (voice) references | built; measured close (see results), listening check pending |
| Anchored audio clips in a scene | built (CLI); verified |
| Video references | planned |
| Overlays in export (exact logos, product photos, text cards with position, timing, simple animation) | planned |
| Seam correction at joins (port Continuum's audio/video seam logic) | planned if joins show flicker |
| Speed comparison: 8 vs 20 steps (FL2VA); SageAttention via ComfyUI's `--use-sage-attention` (needs `sageattention` 1.0.6) | planned |
| Upscaling pass for selected takes (an H3 latent upscaler exists) | planned |
| Scene reordering in the interface (the API supports `move`) | planned |
| Undo in the interface (history files exist) | planned |
| Shared voice/character library across projects | planned |
| Import any mp4 as a starting scene | planned |

## Notes on models and speed

- MiniMax H3 ships as **FL2VA** (first/last frame; best quality) and **Ref2VA** (references; lower quality). A community **hybrid** merges them. In practice FL2VA followed image references closely, so Clapper defaults to FL2VA; the hybrid (same size, same speed) is an option if a reference type proves weak.
- The **Turbo 8-step LoRA at 20 steps** is the proven setting; 8 steps is the LoRA's design point and ≈2.5× faster sampling — to be compared.
- **SageAttention**: the KJNodes H3 patch needs compiled SageAttention 2.x kernels (no Linux wheels; building needs the CUDA toolkit). ComfyUI's own `--use-sage-attention` with the Triton-based `sageattention` 1.0.6 is the easier path to test.
- **Chunk FeedForward** lowers peak VRAM; keep it on at 16 GB.

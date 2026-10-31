# Clapper 0.2 plan — from director to studio

Status: **agreed; M0 done**. When an item is built, move it to the roadmap in [DESIGN.md](DESIGN.md) with its real status.

## The idea

Clapper 0.1 directs a video: scenes, takes, joins, export. 0.2 adds the tools that make what a video is built from, so a whole AI video can be made in one place:

```
 PREPARE  (tools, any order, any time)        SHOOT  (video takes)              FINISH  (once you are happy)
 ───────────────────────────────────          ─────────────────────             ──────────────────────────
 generate / vary images ─┐                    scene 1: fresh from references    upscale selected takes
 character / place sheets├─► Library ───────► scene 2…: continue / first frame  overlays (logo, photos, cards)
 import audio, video     │   (@names,         attach @refs, voices, clips       music bed, sound effects
 generate speech ────────┘    definitions)    takes, select                     export presets
          ▲                                          │
          └──── assistant: script, dialogue, image prompts, scenes — every change is a card ────┘
```

Rules that follow from this:

1. **The stages are separate.** A tool job makes an asset and stops. Nothing chains from Prepare into Shoot or from Shoot into Finish on its own. You generate characters today, write scenes tomorrow, upscale next week.
2. **Everything lands in the project.** Every image, audio clip and video clip is an *asset* in the project library, kept with what made it (upload, or tool + prompt + seed + inputs), exactly like takes today. Any asset can become an `@name` reference.
3. **Video generation only uses what is in the library.** While shooting, the GPU runs H3 only, and one model (Ref2VA) can make every scene (tests T0–T2).
4. **Defaults stay proven.** New modes are opt-in; a new scene still defaults to today's setup.
5. **Simple first, complete underneath.** Each screen shows the common action; the rest is one tap away (an *Advanced* fold, an asset's menu), never removed.

## Tests that decide the design

Run with `clapper take` on an RTX 5060 Ti 16 GB at 416×736, Ref2VA int8 + Ref2V Turbo 4-step LoRA at 4 steps unless noted. Numbers are in the verified-results table in [DESIGN.md](DESIGN.md).

| # | Question | Result |
|---|---|---|
| T0 | Ref2VA, fresh scene from references only | **pass** — character close, action followed, ≈114 s |
| T1 | Ref2VA + start image pinned at frame 0 | **pass** — opens exactly on the image, style kept |
| T2 | Ref2VA with masked continuation | **pass** — clean join; later prompt beats followed only partly |
| T3 | Audio clip anchored at a time | **pass** — the soundtrack is the clip (envelope 0.995, waveform 0.95) |
| T4 | Voice reference `<Audio N>` | **likely pass** — new words, pitch and spectrum match the sample; listening check pending |
| T5 | 4 vs 20 steps (Ref2VA, start image) | **4 steps** — 114 s vs 396 s, no visible gain at 20 |

Decisions from these:

- **One model can make the whole video.** Ref2VA handles a fresh start, an image start and continuation, so no switching is needed. Settings offer a **video model** choice: *Ref2VA for every scene* or *FL2VA for every scene* (0.1's proven setup and still the default), plus *Ref2VA for fresh scenes, FL2VA for the rest* for people who prefer FL2VA's continuation.
- **Dialogue clips become part of the scene.** A generated or imported line anchored in a scene comes out word for word, with the picture made around it, so speech made with a TTS tool can be used directly. Laying audio over the video in Finish stays available (music, effects, re-timing).

## Features

IDs are for discussion and commits. **M** = milestone (build order below).

### A. Library (M1)

- **A1 Assets.** One library per project: images, audio, video. Each asset has a preview, a kind (*character, place, prop, style, voice, sound, music, clip, frame*), an optional `@name` and definition, tags, and its origin. The 0.1 reference list becomes the image/audio assets that have an `@name` (automatic migration).
- **A2 Import.** Drag and drop or pick files: PNG/JPG/WEBP; MP3/M4A/WAV/OGG/FLAC (converted with ffmpeg, original kept); MP4/MOV/WEBM. Audio: trim, normalise, preview waveform. Video: trim, keep its soundtrack.
- **A3 Grab a frame** from any take into the library (for a new start image, a sheet, or a variation).
- **A4 Versions.** A variation of an asset is linked to it; the asset page shows the family. Replacing an `@name`'s image keeps the old one.
- **A5 Shared library.** Mark an asset (a character, a voice) as shared; other projects of the same profile can use it.

### B. Tools (M2 images, M3 audio)

Every tool is a **workflow adapter**: a ComfyUI API-format workflow plus a small manifest naming its inputs (prompt, images, audio, seed, size…) and its outputs. Clapper ships adapters for models people are likely to have, and anyone can add their own by dropping a workflow + manifest into a folder. Adapters show up in the *New* menu with a form built from the manifest.

Built-in adapters (to verify on a GPU as each is added):

| Tool | Model (default adapter) | What you give it |
|---|---|---|
| B1 Generate image | FLUX.2 Klein | prompt, optional style/character references, size |
| B2 Vary / edit image | FLUX.2 Klein (multi-reference edit) | an asset + an instruction ("same character, waving", "this street at night") |
| B3 Combine | FLUX.2 Klein | character(s) + place → a start frame |
| B4 Character / place sheet | a multi-output Klein workflow | one asset → many angles/expressions; pick which to keep |
| B5 Generate speech | Qwen3-TTS (voice clone) | text, a voice asset (sample + its transcript), language |
| B6 Design a voice | Qwen3-TTS Voice Design (needs that model) | a description of the voice |
| B7 Upscale take | H3 latent upscaler workflow | a selected take (Finish stage) |

- **B8 Batches.** Any tool can run N seeds at once; results appear as a grid; keep some, discard the rest (discarded files go to a trash folder until emptied).
- **B9 Every job is kept** like a take: inputs, seed, settings; *Run again*, *Run again with changes*.

### C. Shooting (M1)

- **C1 Start mode per scene:** *Continue from previous scene* (0.1 default) · *Fresh from references* (Ref2VA) · *Start from image* (an image asset or a take's frame pinned as frame 0; works on both models, T1). Optional *end on image* (last frame).
- **C2 Video model** in Settings: *FL2VA for every scene* (default), *Ref2VA for every scene*, or *Ref2VA for fresh scenes, FL2VA for the rest*; each model with its own LoRA and steps (Ref2V Turbo: 4 steps).
- **C3 Audio in a scene:** voice references (`@narrator` → `<Audio N>`, T4) and **soundtrack clips** anchored at a time in the scene (T3), e.g. a generated line at 0.5 s. The scene editor shows the clip's length against the scene's.
- **C4 Video references** (`<Video N>`) from video assets.
- **C5 Queue by model.** *Render remaining* orders jobs so the model loads once per run.
- **C6 Bridge join and keyframe continuation** (built in 0.1, untested): test, then finish or remove.

### D. Finishing (M4)

- **D1 Upscale** selected takes, kept as versions of the take; export uses the chosen one (or the original) and resizes any scene that is not upscaled to the same size. Two methods: **Sharpen** (an image upscale model on each frame; the picture stays the same) and **Re-detail** (the H3 latent upscaler, then a short low-noise H3 pass in tiles, MMH3 Split Upscale, that draws new detail; strength light or strong). Both keep the take's own sound.
- **D2 Overlays:** exact images (logo, product photo, end card) with position, timing and a simple fade/slide.
- **D3 Audio tracks:** a music bed under the whole video with automatic ducking under speech, sound effects at chosen times, and dialogue clips laid over (the post-production alternative to C3).
- **D4 Export presets:** 9:16, 1:1, 16:9; size and quality.
- **D5 Timeline view** of the finished video: scenes, overlays and audio tracks on one strip; drag to reorder scenes (the API has `move`).
- **D6 Undo** from the history files.

### E. Assistant (M5, alongside the others)

Stays as in 0.1 — local model through llama-swap or any OpenAI-compatible API — and gains the new tools as cards:

- **E1 Script first:** brief → script → per-scene dialogue lines (speaker, language, rough length) → scenes.
- **E2 Asset plan:** from the script it proposes the characters, places and voices to make, each as a *generate image* / *generate speech* card with the prompt written for that model. Nothing runs until you apply it.
- **E3 Prompts for tools:** writes FLUX image prompts and edit instructions; describes library assets (✨ Describe, already in 0.1) to write definitions.
- **E4 Timing:** uses generated clip lengths to choose 5 s vs 10 s scenes.
- **E5 Compiled prompt on scene cards** (0.1 roadmap).
- **E6 Model per task** (local for small edits, online for planning/reviews), native Anthropic/Gemini adapters (0.1 roadmap).
- GPU sharing stays as in 0.1: tool jobs and renders eject the local model; the next chat message loads it back.

### F. Simple settings (M4)

Settings must make sense to someone who has never heard of steps or megapixels, and still give experts everything.

- **F1 Simple view (default)** with plain choices only, each showing what it changes and the time to expect:
  - *Quality and speed* — **Draft** (Ref2VA, 4 steps) or **Standard** (FL2VA, 20 steps, the proven setup); *Custom* appears when Advanced values match neither.
  - *Steps* per model in use, every value with its time per scene.
  - *Picture size while rendering* (with the resulting width × height), *default scene length*, *joins between scenes* (Quick / Smooth / Strong) and *carry sound across joins*.
  - A running total: one scene and the whole video.
- **F2 Honest numbers.** Every time is marked **measured** (on the reference GPU, or from this project's own finished takes) or **estimate** (scaled from measurements). Real timings from the user's takes replace the defaults automatically.
- **F3 Advanced view** — the full settings as before (sampler, scheduler, LoRAs, model files, patches, continuity method, reference size, the Ref2VA model and steps). The choice of view is remembered.
- **F4 The same idea elsewhere** — the image tool's steps and the upscale dialog show expected times the same way.

## Interface

Three workspaces, switched by a rail (laptop), tabs (tablet) or a bottom bar (phone); the ✨ Assistant opens beside any of them.

| Workspace | What it holds |
|---|---|
| **Studio** | The project's material, like a media studio rather than a step of video generation. Collections: *Cast & world* (characters, places, props, styles), *Files* (images, start images & frames, voices, speech lines, music & effects, clips, @ references), *From scenes* (takes, exports). A detail panel for any item: preview, role, belongs-to, versions, editable **recipe** (make a new version), *Change it*, *Speak*, @name and definition, where it is used. **＋ New**: a character/place/prop/style, the tools, uploads. |
| **Scenes** | The storyboard and scene editor; *Style & notes*. |
| **Finish** | Per-scene upscales and versions, export (size, partial), previous exports; next: music, effects, overlays. |

### G. Characters, places, props, styles (done)

A character (or place, prop, style) couples everything that makes it itself: a **name** (its @name), a **description** (added to every image made from it and sent to H3 as its definition), its **looks** (outfits, angles, variations, each labelled) with one **main look** that scenes use, and for characters a **voice**. Its page makes more of it with only the change typed: **new outfit / look**, **new angle / pose**, **put in a place** (start frames with the character and the place as references), **design it / more designs**; the built prompt can be seen and edited. Generating again from any recipe makes a **version** in the item's family.

## Data

- `project.json` gains `assets` (with origin) and `jobs`; files under `assets/<kind>/`. Old projects migrate on open (their `refs/` become assets; nothing is moved until the project is saved).
- Adapters live in `comfyui_clapper/adapters/<name>/` (built-in) and `~/.clapper/adapters/<name>/` (yours): `workflow.json` (API format) + `adapter.json` (inputs → node/field, outputs, model files it needs). Clapper checks the model files are present and greys out an adapter that cannot run.
- Shared assets in `~/.clapper/library/`.

## Milestones

| M | Contents | Done when |
|---|---|---|
| M0 | Tests T0–T5 on a GPU (CLI, no interface) | **done** — results in DESIGN.md |
| M1 | Library (A1–A3), start modes (C1), video model (C2), audio import + voice refs + sound clips (C3) | **done** — the job tray for tool jobs moves to M2, where the first tool jobs appear |
| M2 | Adapter system, image tools B1–B4, batches B8, versions A4 | **done** except B4 (sheets) — an original anime character made, varied, combined with a location into a start frame and used in scenes |
| M3 | Speech B5 (B6 if the model is present) | **B5 done** (built with M2) — a generated line used as a scene's sound clip; B6 needs the Voice Design model |
| M4 | Upscale D1 (Sharpen, Re-detail) with export using upscaled takes and one export size; simple settings F1–F4 — **done**; overlays D2 and audio tracks D3 — **done**; then presets D4, timeline D5, undo D6 | an upscaled export; a new user picks Draft/Standard and knows how long a video will take |
| M5 | Assistant E1–E5 across the above; E6 | **E2–E3 done** (Studio tools, rewritten instructions), connection hardening done; E1 script-first and E4 timing next |
| later | Shared library (A5), video references (C4), more video models through adapters, lip-sync | — |

# Clapper 0.2 plan — from director to studio

Status: **draft for discussion**. When an item is built, move it to the roadmap in [DESIGN.md](DESIGN.md) with its real status.

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
3. **Video generation only uses what is in the library.** While shooting, the GPU runs H3 only; the one possible model switch is Ref2VA for a fresh scene vs FL2VA for continued scenes, and test T1/T2 below may remove even that.
4. **Defaults stay proven.** New modes are opt-in; a new scene still defaults to today's setup.
5. **Simple first, complete underneath.** Each screen shows the common action; the rest is one tap away (an *Advanced* fold, an asset's menu), never removed.

## Tests that decide the design (one GPU run each)

| # | Question | Why it matters | Grounding |
|---|---|---|---|
| T1 | Ref2VA + a first-frame guide (`MiniMaxH3AddGuide` at frame 0): does the scene start on the image and still follow references? | If yes, a "fresh" scene can also start from an image without FL2VA | `MiniMaxH3AddGuide` works on any H3 conditioning (core node) |
| T2 | Ref2VA with Clapper's masked continuation (22 frames + audio) | If the join is clean, one model for the whole video — no switching at all | untested |
| T3 | Audio anchored with `MiniMaxH3AddGuide(audio=…)`: does the video carry exactly that clip (same words, same timing) with picture generated around it? | "Speak this exact line" from a generated or imported clip | the core node documents audio anchoring; quality unknown |
| T4 | Standalone reference audio (`<Audio N>`) | Voice likeness without fixing the words | built in 0.1, untested |
| T5 | Ref2V Turbo 4-step LoRA vs 20 steps for fresh scenes | speed | — |

Results go into the verified-results table in DESIGN.md.

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

- **C1 Start mode per scene:** *Continue from previous scene* (0.1 default) · *Fresh from references* (Ref2VA) · *Start from image* (an image asset as first frame; FL2VA, or Ref2VA + guide if T1 passes). Optional *end on image* (last frame).
- **C2 Model per mode** in Settings (model, LoRA, steps), with the proven setup as default for *continue* and a Ref2VA default for *fresh*.
- **C3 Audio in a scene:** voice references (`@narrator` → `<Audio N>`, T4) and **soundtrack clips** anchored at a time in the scene (T3), e.g. a generated line at 0.5 s. The scene editor shows the clip's length against the scene's.
- **C4 Video references** (`<Video N>`) from video assets.
- **C5 Queue by model.** *Render remaining* orders jobs so the model loads once per run.
- **C6 Bridge join and keyframe continuation** (built in 0.1, untested): test, then finish or remove.

### D. Finishing (M4)

- **D1 Upscale** selected takes (B7), kept as a second version of the take; export uses it when present.
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

## Interface

Four places, same header as today:

| Screen | What it holds |
|---|---|
| **Library** (new) | Grid of assets with filter chips (Characters · Places · Props · Styles · Voices · Audio · Clips · Frames). **+ New** menu: *Upload* · *Generate image* · *Vary…* · *Sheet…* · *Generate speech* · *Grab frame*. Tap an asset: preview, `@name`, definition, origin, versions, *Vary*, *Use in scene…*. |
| **Storyboard** | As today. The scene editor gains a **Start** switch (Continue · Fresh · From image), reference chips with a library picker instead of a list, and an **Audio** row (voices, soundtrack clips with their length). |
| **Finish** (replaces the Export dialog) | Selected takes in order, upscale per take, overlays, music and effects, preset, *Export*. |
| **✨ Assistant** | The existing panel; new card types for tool jobs. |

One **job tray** (the existing status pill and live bar) shows every kind of job — takes, images, speech, upscales — with the same progress, stop and re-queue.

## Data

- `project.json` gains `assets` (with origin) and `jobs`; files under `assets/<kind>/`. Old projects migrate on open (their `refs/` become assets; nothing is moved until the project is saved).
- Adapters live in `comfyui_clapper/adapters/<name>/` (built-in) and `~/.clapper/adapters/<name>/` (yours): `workflow.json` (API format) + `adapter.json` (inputs → node/field, outputs, model files it needs). Clapper checks the model files are present and greys out an adapter that cannot run.
- Shared assets in `~/.clapper/library/`.

## Milestones

| M | Contents | Done when |
|---|---|---|
| M0 | Tests T1–T4 on a GPU (scripts, no interface) | results in DESIGN.md |
| M1 | Library (A1–A3), job model, start modes (C1, C2), audio import + voice refs + soundtrack clips (C3) as far as T3/T4 allow | a video with a fresh first scene and a scene that uses an imported clip, rendered from the interface |
| M2 | Adapter system, image tools B1–B4, batches B8, versions A4 | a character made, varied and used as `@name` in a scene |
| M3 | Speech B5 (B6 if the model is present) | a generated line used in a scene (C3) |
| M4 | Finish screen D1–D4, then D5–D6 | an upscaled export with a logo overlay and music |
| M5 | Assistant E1–E5 across the above; E6 | the assistant plans a video including its assets |
| later | Shared library (A5), video references (C4), more video models through adapters, lip-sync | — |

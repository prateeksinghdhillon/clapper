# Clapper reference

Settings, command line, HTTP API, project format, internals and troubleshooting.

## Settings

Project settings are stored as `settings` in `project.json` and merged over these defaults (`comfyui_clapper/engine/settings.py`). The web **Settings** dialog edits them; `--overrides` (CLI) and the editor's *Options* override single takes. Every take stores the full settings it used in its `meta.json`.

| Key | Default | Meaning |
|---|---|---|
| `models.unet` | `minimax_h3_fl2va_pruned_int8_convrot.safetensors` | Diffusion model (`diffusion_models/`) |
| `models.weight_dtype` | `default` | `UNETLoader` weight dtype |
| `models.clip` | `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` | H3 text encoder, loaded as type `minimax` |
| `models.video_vae` / `models.audio_vae` | `minimax_h3_video_vae_fp16` / `minimax_h3_audio_vae_fp32` | VAEs |
| `loras[]` | Turbo 8-step v1.0, strength 1.0, enabled | Model-only LoRAs, applied in order |
| `patches.chunk_feed_forward` | enabled, 2 chunks, threshold 4096 | KJNodes `MiniMaxChunkFeedForward`: lowers peak VRAM, not a speed-up |
| `patches.spectrum` | disabled | `SpectrumApplyMiniMaxH3` (other keys are passed through as its inputs) |
| `sampling.sampler` | `res_multistep` | Any ComfyUI sampler name |
| `sampling.scheduler` | `simple` | Any ComfyUI scheduler name |
| `sampling.steps` | 20 | Steps. The Turbo LoRA is distilled for 8; 20 is the proven quality setting, 8 is ≈2.5× faster sampling |
| `sampling.denoise` | 1.0 | |
| `resolution.mode` | `start_image` | Size from the start image's aspect at `megapixels`; otherwise `width`×`height` |
| `resolution.megapixels` | 0.30 | e.g. 1536×2752 → 416×736 |
| `resolution.multiple` | 32 | Size rounding |
| `resolution.width` / `height` | 416 / 736 | Used when there is no start image |
| `continuity.frames` | 22 | Carried frames: 5 (fast), 22 (balanced), 39 (strong) |
| `continuity.method` | `masked` | `masked` (lock the carried latents in place) or `keyframe` (pin them as a guide) |
| `continuity.audio` | true | Carry sound across the join as well |
| `references.image_size` | `match` | `match` (scale down to the output area) or `max` (up to 2048 px short edge) |
| `scene_seconds` | 5.0 | Default scene length |

## Command line

```bash
python cli/clapper.py [--url http://127.0.0.1:8188] <command> …
```

Any Python ≥ 3.10 with Pillow works; the CLI talks to ComfyUI over HTTP. Scenes can be given as a number (1-based) or an id.

| Command | Does |
|---|---|
| `new PROJECT --name NAME` | Create a project folder and add it to the project list |
| `ref PROJECT NAME FILE [--kind image\|audio] [--description D] [--default] [--start]` | Add a reference (copied into the project) |
| `scene PROJECT [--text T \| --text-file F] [--seconds S] [--join continue\|cut\|bridge] [--refs a,b \| --refs ""] [--title T] [--after N]` | Add a scene (`--refs ""` = no references, omitted = project defaults) |
| `scene PROJECT --update N [fields…]` | Change a scene |
| `render PROJECT N [N…] [--seed S] [--overrides JSON] [--wait]` | Queue a take for each scene |
| `render-rest PROJECT [--from-scene N] [--overrides JSON]` | Queue every remaining scene, chained in order |
| `refresh PROJECT` | Collect finished takes, detect interrupted ones |
| `usage PROJECT` | Disk space used, split into takes, exports, references, history |
| `delete PROJECT --confirm "NAME" [--keep-files]` | Delete the project folder for good (refused while takes are rendering); `--keep-files` only removes it from the project list |
| `status PROJECT` | Scenes, takes (number, status, created, finished, parent) and badges |
| `select PROJECT N TAKE_ID` | Select a take |
| `export PROJECT [--partial]` | Join the selected takes; `--partial` exports the ready scenes if later ones have no take |
| `import-timeline PROJECT PROMPT_FILE --picture 1=name … [--audio 1=name …]` | Turn a Continuum Timeline prompt into scenes: the preamble becomes the style text, `<Picture N> is …` lines become reference definitions, each section becomes a scene |
| `adopt PROJECT N TAKE_DIR [--parent TAKE_ID]` | Add an already-rendered take folder to a scene |
| `take --prompt-file P [--start-image I] [--ref I …] [--continue-from TAKE_DIR] --out DIR …` | Render one take outside any project (testing) |
| `take … [--unet FILE] [--lora NAME[:STRENGTH] …] [--anchor-audio FILE[@SECONDS] …] [--ref-audio FILE …]` | Same, with another diffusion model and LoRA list (`--lora none` for none), a soundtrack clip anchored at a time, or a voice reference |
| `profile list` | Profiles, their projects, projects with no profile, and who is logged in |
| `profile add ID [--name N] [--password P]` | Create a profile (asks for the password if omitted) |
| `profile password ID [--password P]` | Change a password (asks for it if omitted, so it never shows in the shell history); every logged-in session of that profile ends |
| `profile assign ID PROJECT…` | Give project folders to a profile (moves them from any other profile) |
| `profile unassign PROJECT…` | Take project folders away from their profile |
| `profile remove ID` | Delete a profile; its projects stay on disk, owned by nobody |
| `profile logout [ID]` | End the sessions of a profile (or everyone), freeing Clapper at once |

The CLI works on project folders directly and does not ask for a profile: it is meant for whoever runs the computer. A project made with `new` belongs to no profile, so it is not on anyone's list in the web interface until you run `profile assign ID FOLDER` (or open the folder once in the web interface with *Type a folder path instead*, which gives it to the logged-in profile).

Example — a project from scratch:

```bash
C="python cli/clapper.py"; P=~/Documents/Clapper-Projects/cafe
$C new $P --name "Café promo"
$C ref $P opening start.png --start
$C ref $P hero fox.png --default --description "Mascot"
$C scene $P --text-file scene1.txt --join cut
$C scene $P --text-file scene2.txt
$C render $P 1 --wait
$C render-rest $P
$C export $P
```

## HTTP API

Served by ComfyUI under `/clapper/`. JSON in, JSON out; errors return `{"error": "..."}` with HTTP 400, or 409 for a refused repeat (below) and a login while another profile is active. Every call except the page, its static files (`/clapper/static/*`) and `/clapper/api/auth/*` needs a logged-in profile (the `clapper_session` cookie set by login) and otherwise returns HTTP 401 `{"error": "log in first", "login": true}`. Every project call takes the project folder as `path` and only accepts projects of the logged-in profile; a project must be created or opened once (`POST /clapper/api/projects`) before other calls accept it. Paths must be inside the home folder.

| Method & path | Body / query | Does |
|---|---|---|
| `GET /clapper/` | | The web interface |
| `GET /clapper/api/auth/me` | | The logged-in profile, or 401 with `no_profiles` when none exist yet |
| `POST /clapper/api/auth/login` | `profile`, `password` | Log in and set the session cookie; 401 on a wrong id/password, 409 `{"in_use": true}` while another profile is active |
| `POST /clapper/api/auth/setup` | `profile`, `name`, `password` | Create the first profile (only while none exist) and log in; it gets every known project |
| `POST /clapper/api/auth/logout` | | End the profile's sessions on every device |
| `POST /clapper/api/auth/password` | `current`, `new` | Change the logged-in profile's password; its other devices are logged out, this one stays |
| `GET /clapper/api/projects` | | Known projects, settings defaults |
| `POST /clapper/api/projects` | `name` + `location` (folder named after the project), or `path` | Create or open a project (it becomes the profile's); refused if the folder exists and is not a project, or is another profile's project |
| `GET /clapper/api/projects/target` | `?location=&name=` | Where a new project would go and whether that folder is `free`, a `project`, `busy`, or `taken` (another profile's project) |
| `GET /clapper/api/locations` | | Default location and remembered locations |
| `POST /clapper/api/locations/default` | `path` | Set the default location |
| `GET /clapper/api/folders` | `?path=` | Subfolders (inside the home folder, hidden ones skipped) for the folder picker |
| `POST /clapper/api/folders/new` | `parent`, `name` | Create a folder |
| `GET /clapper/api/project` | `?path=` | Project view: data, badges, effective settings, compiled prompts |
| `GET /clapper/api/project/usage` | `?path=` | Disk space: `total`, `files`, `parts` (takes, exports, refs, history, other) |
| `POST /clapper/api/project/delete` | `path`, `confirm_name` (exact project name), `keep_files` | Delete the folder for good (or only unlist it); refused while takes are rendering or waiting |
| `POST /clapper/api/project/update` | `path`, any of `name`, `style`, `start_image`, `settings`, `defaults`, `notes` | Project-level changes |
| `POST /clapper/api/ref` | multipart: `path`, `name`, `kind`, `description`, `default`, `start`, `file` | Add a reference |
| `POST /clapper/api/ref/update` | `path`, `name`, `description` / `definition` / `card` / `card_status` | Edit a reference |
| `POST /clapper/api/scene` | `path`, `action`: `add` (`text`, `seconds`, `join`, `refs`, `title`, `after`) · `update` (`id`, fields) · `move` (`id`, `index`) · `remove` (`id`) | Scene changes |
| `POST /clapper/api/render` | `path`, `scene_id`, optional `seed`, `overrides`, `parent_take` | Queue a take. The same scene with the same `seed` (or none), `overrides` and `parent_take` is refused with 409 for 5 seconds after it was queued |
| `POST /clapper/api/render_rest` | `path`, optional `from_scene`, `overrides` | Queue the remaining scenes, chained; refused while any of those scenes has a take rendering or waiting |
| `POST /clapper/api/requeue` | `path`, `take_id` | New take with the same seed and parent; the same take is refused with 409 for 5 seconds after it was re-queued |
| `POST /clapper/api/stop` | `path`, `take_id` or `all: true` | Interrupt/cancel takes (and the queued takes that continue from them) |
| `POST /clapper/api/select` | `path`, `scene_id`, `take_id` | Select a take |
| `POST /clapper/api/take/restore_prompt` | `path`, `take_id` | Copy a take's text/refs/length/join back into its scene |
| `POST /clapper/api/take/note` | `path`, `take_id`, `note` / `rating` | Annotate a take |
| `POST /clapper/api/refresh` | `path` | Collect finished takes; returns queue counts |
| `GET /clapper/api/export/check` | `?path=` | Ready scenes, missing scenes, length, warnings |
| `POST /clapper/api/export` | `path`, `partial` | Export; returns the file and its manifest |
| `GET /clapper/api/live` | | Current render (phase, step, s/step, ETA) and recent renders |
| `GET /clapper/api/system` | | CPU, RAM, GPU, VRAM, temperatures, power |
| `GET /clapper/media` | `?path=&file=` | A file inside the project (videos, references, exports) |
| `GET /clapper/static/{name}` | | The interface's own files (`web/`); no login needed |
| `GET /clapper/api/gpu` | | Who holds the GPU, GPU mode, local model and ComfyUI state |
| `POST /clapper/api/gpu` | `mode` (`auto`/`manual`), `action` (`load` / `eject` / `free_comfy`) | Change the GPU mode, load or eject the local model, unload ComfyUI's models |
| `GET/POST /clapper/api/assistant/settings` | providers, `active`, `gpu_mode` | Read (keys masked) / change the assistant settings |
| `GET /clapper/api/assistant/chats` | `?path=` | The project's chats |
| `POST /clapper/api/assistant/chats` | `path`, `action`: `new` · `rename` (`id`, `title`) · `delete` (`id`) | Manage chats |
| `GET /clapper/api/assistant/chat` | `?path=&id=` | Messages, proposals, context use, and the live state of a running turn |
| `POST /clapper/api/assistant/send` | `path`, `id`, `text`, optional `images` (reference names), `take` (attach its frames), `thinking` | Start a turn (runs in the background; poll the chat) |
| `POST /clapper/api/assistant/stop` | `path`, `id` | Stop the running turn |
| `POST /clapper/api/assistant/compact` | `path`, `id` | Summarise older messages now |
| `POST /clapper/api/assistant/proposal` | `path`, `id`, `proposal`, `action` (`apply`/`reject`) | Apply or reject a proposal; a proposal that is no longer pending is refused, so it cannot be applied twice |
| `POST /clapper/api/assistant/describe_ref` | `path`, `name` | The model describes a reference image; returns the card text (not saved) |

## Project folder

```
<project>/
  project.json            everything below; atomic writes
  history/                the last 50 versions of project.json (manual undo: copy one back)
  refs/<name>.<ext>       copies of the reference files
  takes/<scene-id>/<take-id>/
      take.mp4            H.264 + AAC, 24 fps
      audio.wav           the take's sound
      latent.safetensors  raw video + audio latents (needed to continue from this take)
      meta.json           exact prompt, spec, seed, settings, parent, frames, size, timing
  exports/<name>-<date>.mp4 + .json   exported videos and the list of takes used
  chat/c_<id>.json        assistant chats; chat/frames/<take>/ frames used for take reviews
```

`project.json` top level: `schema`, `name`, `created`, `updated`, `settings`, `style`, `notes`, `start_image`, `library` (`items` by name: `kind`, `file`, `sha256`, `description`, `definition`, `card`, `card_status`; `defaults`), `scenes` (ordered: `id`, `title`, `text`, `seconds`, `join`, `refs` — `null` = defaults, `selected_take`), `takes` (by id: `scene`, `number`, `status`, `created`, `finished`, `seed`, `parent_take`, `join`, `context_frames`, `frames`, `width`, `height`, `prompt_sha256`, `refs`, `scene_text`, `scene_refs`, `scene_join`, `scene_seconds`, `prompt_id`, `render_seconds`, `s_per_it`, `steps`, `error`).

Take statuses: `queued`, `rendering`, `done`, `failed`, `interrupted`.

The list of known projects is `~/.clapper/projects.json`; the default location and remembered locations are in `~/.clapper/config.json`.

## Assistant

### Providers

`~/.clapper/llm.json` (readable only by its owner) holds the providers, the active one, and the GPU mode. The web **Assistant settings** dialog edits it; API keys are never sent back to the browser.

| Key | Meaning |
|---|---|
| `active` | Provider id used by the assistant |
| `gpu_mode` | `auto` (load / eject the local model as needed) or `manual` |
| `providers.<id>.kind` | `local` (llama-swap on this computer, shares the GPU) or `openai` (any OpenAI-compatible API) |
| `base_url`, `model`, `api_key` | The chat-completions endpoint (`…/v1`), model name, key (`openai` kind) |
| `swap_url` | llama-swap's address (`local` kind), default `http://127.0.0.1:8080` |
| `context`, `vision` | Context window in tokens (drives compaction and the meter); whether the model accepts images |
| `slot_file` | File name the local model's conversation cache is saved to before an eject |
| `thinking` | `"auto"` (default, measured best — see DESIGN.md): think only when planning (the project has no scenes yet, or the message asks for a plan, a storyboard, all scenes, a rewrite or *N scenes*); `false`: answer directly; a number: always think. Local providers only; set in Assistant settings |
| `thinking_budget` | Reasoning tokens per request when thinking (default 2048), sent as `thinking_budget_tokens` (llama-server honours it per request). **think harder** in the chat uses this budget for one message |
| `sampling`, `sampling_thinking` | Optional sampling parameters sent with every request (`temperature`, `top_p`, `top_k`, `min_p`, `presence_penalty`, `repeat_penalty`); `sampling_thinking` is added on top when thinking. Empty = the server's defaults. Edit `llm.json` to change them |

### Local model with llama-swap

The local provider expects [llama-swap](https://github.com/mostlygeek/llama-swap) in front of llama.cpp's `llama-server`, with a model entry named like the provider's `model` (default `clapper-128k`):

```yaml
models:
  clapper-128k:
    cmd: >
      /path/to/llama-server --host 127.0.0.1 --port ${PORT}
      -m /path/to/model.gguf --mmproj /path/to/mmproj.gguf
      -ngl 99 -np 1 -fa on --jinja --reasoning-format deepseek
      -c 131072 -ctk q8_0 -ctv q8_0
      --slot-save-path /home/<you>/.clapper/llm-slots
    ttl: 1800
```

- `--mmproj` gives the model vision (the projector file that belongs to the model); without it set `vision` to false.
- `-np 1` gives the whole context to one conversation; `-ctk/-ctv q8_0` halves the KV cache.
- `--slot-save-path` lets Clapper save the conversation cache before an eject and restore it after the next load.
- Measured with a 27B ternary model (7.2 GB) + Q8 projector at 128k context on an RTX 5060 Ti 16 GB: 12.7 GB VRAM, load 3–6 s, eject 1 s, about 45 tokens/s written and 930 tokens/s read.

### How the GPU is shared

- Auto mode: before a request to the local model, ComfyUI's models are unloaded (only when its queue is empty) and the model is loaded. Every job queued in ComfyUI (from Clapper or from ComfyUI's own page) first ejects the local model.
- Manual mode: nothing loads automatically; Clapper's render routes refuse while the local model is loaded; jobs from ComfyUI's own page still eject it.
- Before an eject, the conversation of the chat that used the model last is re-read up to the exact point the next message will add to, and saved (`slot_file`). When that chat loads the model again, the cache is restored and the next message is answered without re-reading the conversation (this hybrid-attention model family cannot rewind a single token, which is why the exact point matters).

### What the model receives

System: fixed instructions + `docs/PROMPT_GUIDE.md` (+ a summary of older messages after compaction). Then the conversation. The newest user message carries a `<project>` snapshot: settings, start image, style text, story notes, references (description, definition, card), every scene in full with its references, takes and badges, and the proposals still waiting. Only the newest message has it, so everything before stays identical and the model's prompt cache is reused.

Tools: `get_scene`, `view_reference`, `view_take` (read); `update_notes`, `save_reference_card` (applied at once, they are the assistant's own memory); `add_scenes`, `edit_scene`, `remove_scene`, `move_scene`, `edit_project`, `set_reference_definition`, `change_settings`, `render_scenes` (proposals). Proposals are checked on an in-memory copy of the project with the chat's proposals still waiting for the user applied first, in order, so two proposals that only break together are caught (a style text using `@style` against proposed scenes whose references lack it).

Before checking, scene text is repaired where that is safe and changes no words: field names written with spaces, capitals or bold become `overall_soundscape:` etc., `[shot 1]` becomes `[Shot 1]`, `</ d>` / `<\d>` / `<D>` become `</d>` / `<d>`, a spoken line closed with `<d>` instead of `</d>` is closed properly (only when no `[Language]` tag follows, so the pairing is unambiguous), each field goes on its own line, a repeated field is kept once, and a missing music line becomes `non_diegetic_music: N/A`.

A proposal is refused (and the reason goes back to the model) when:
- scene text lacks one of the three H3 fields, repeats one, has them out of order, has an empty field, or has more than one line before `integrated_multimodal_description:` (one free line for the starting state is allowed);
- the description lacks `[Shot 1]`;
- a spoken line is malformed: a `<d>` not closed by `</d>` before the next one or the end, a `</d>` without its `<d>`, no `[Language]` tag, or a language with its own script (Hindi, Tamil, Arabic, Russian, Japanese, …) transliterated;
- an affected scene would not compile (a `@name` not active or unknown). For `edit_project`, scenes that were broken already don't block the change;
- new scenes repeat each other, or repeat or nearly repeat (≥ 90% of the words in the same order, ignoring case and spacing) a scene in the project or in a waiting proposal; the error names that scene.

It gets a warning when a new scene is shorter than 3 s, the project has no style text yet, or the style text defines a speaker `(S1)` but no new scene speaks, and is told how many scenes the project would have.

Scene numbers always mean the project as it is (the snapshot the model sees); new scenes in a waiting proposal get numbers only once applied, and the snapshot lists the waiting proposals with their new scenes' titles. Numbers are resolved to ids when the card is made, so applying it later still targets the right scenes. An `add_scenes` call with the same `after` as an earlier `add_scenes` card from the same answer replaces that card (status `replaced`), so a corrected version doesn't add the scenes twice; across answers the copy check applies instead.

**Apply** re-checks on a copy first: the card is refused, and nothing is written, if a scene it adds or rewrites, or a scene that compiled before, would no longer compile (for example a style text naming `@style` while a scene applied in the meantime has its own reference list without it). The error says which scene and why.

### Measuring a model

`tools/assistant_eval.py PROJECT --brief "…" [--language L] [--edit-language L] [--thinking off|auto|N] [--budget N] [--sampling JSON] [--sampling-thinking JSON] [--repeat N] [--label TEXT] --out report.md` runs fixed tasks on a throwaway copy of a project (plan the video from an empty storyboard → add a spoken line to scene 2 → insert a bridging scene → change the narration language of scene 1), applies the proposals between tasks (through the same apply check as the web page), and scores every resulting scene against the prompt guide next to the project's own scenes as a baseline. Hard rules: the three fields once each, `[Shot 1]`, active `@names`, well-formed dialogue tags (`<d>…</d>`, so a line closed with `<d>` fails instead of counting as no dialogue), `[Language]` tag and speaker, speech density, no readable text, no leaked markup, the narration language in its script. Soft rules: camera, timing cues, an early clear sound, narration present, starting where the previous scene ends. It also reports time, requests, reasoning length, tool errors, repeats, replaced cards, whether the right tool was used, the scene count after planning and at the end against the count the brief asks for (`--scenes N`, or read from "*N* scenes" in the brief; one more is expected after the insert), and near-duplicate scenes. `--thinking` and `--sampling` override the provider's settings for the run; `--repeat N` runs everything N times and adds a mean row, since results vary from run to run. Use it to compare models or settings.

Compaction: when a request used more than 60% of the context, the messages before the last 8 are summarised into the chat's `summary`; they stay in the chat file.

Files: `<project>/chat/c_<id>.json` (messages, proposals, summary, and the profile that started the chat; other profiles do not see it), `<project>/chat/frames/<take>/` (frames used for reviews). Project fields: `notes`; per reference `card` and `card_status` (`model` / `confirmed`).

## Profiles

`~/.clapper/profiles.json` (readable only by its owner) holds each profile's display name, its password as a salted scrypt hash, and the project folders it owns; a folder belongs to at most one profile. `~/.clapper/sessions.json` holds the logged-in devices (a SHA-256 of each session token, never the token itself) and when each was last active.

- Logging in is refused while another profile has a session active within `idle_minutes` (10, set in `profiles.json`). The same profile may log in on any number of devices.
- An open Clapper page keeps its session active (it polls every few seconds). A successful login ends every other profile's sessions, so a forgotten session is ended the moment someone else logs in after it went idle.
- Log out ends the profile's sessions on every device. A device stays logged in for up to 30 days without use.
- Profiles protect Clapper's pages and API only. ComfyUI's own page, queue and history have no login.
- Projects created before profiles existed belong to nobody and are hidden until assigned (`profile assign`), or handed to the first profile created in the web interface.

## How a take is made

For each take Clapper builds a ComfyUI graph from core nodes plus two Clapper nodes and queues it in ComfyUI's normal queue:

```
UNETLoader → [MiniMaxChunkFeedForward] → [LoRAs] → [Spectrum] ─┬─► BasicScheduler ─┐
CLIPLoader, VAELoader ×2 ──► Clapper · Scene Conditioning ──┬──┴─► BasicGuider ────┤
                                                            └── latent ──────────► SamplerCustomAdvanced ─► Clapper · Save Take
RandomNoise, KSamplerSelect ─────────────────────────────────────────────────────┘
```

- **Clapper · Scene Conditioning** loads the start image and references, builds what the text encoder sees (start image, then images, then audio — which is why `<Picture 1>` is the start image), attaches reference latents, and prepares the target latent with the continuation.
- **Clapper · Save Take** re-applies the locked prefix exactly, saves the latents, decodes picture and sound, and writes `take.mp4`, `audio.wav` and `meta.json`.

### Frame counts

H3 only accepts clip lengths of 17k+5 frames at 24 fps (5, 22, 39 … 124, 141 …). A first scene of 5 s is **124** frames (5.17 s). A continued scene of 5 s is **141** frames: 22 carried + 119 new. On export the carried frames are trimmed, so five 5-second scenes make exactly 25.0 s.

### Continuation methods

- **masked** (default): the last 7 latent slots (22 frames) and the last 37 audio latent steps of the parent take are copied into the start of the new latent and protected by a noise mask, so the sampler generates only what comes after. This is ComfyUI-H3-Continuum's default "Masked AV" route.
- **keyframe**: the same latents are attached as a keyframe guide at frame 0 (ComfyUI's `minimax_keyframes`, as used by `MiniMaxH3AddGuide`). Softer; untested in Clapper renders.
- **bridge**: additionally pins the first 22 frames of the next scene's selected take at the end of the new take. Untested.

The continuation needs the parent's `latent.safetensors` and the same width and height.

### Anchored audio

A scene can carry audio clips anchored at a time (`spec.audio_guides`: `[{"path": …, "frame": N}]`; CLI `--anchor-audio FILE@SECONDS`). Each clip is encoded with the audio VAE and attached as an audio keyframe at that frame, the same mechanism as the core `MiniMaxH3AddGuide` audio input. The scene's soundtrack then *is* that clip from that point on (measured: envelope correlation 0.995, waveform correlation 0.95 against the input), and the picture is generated around it. A clip longer than the rest of the scene is cut at the scene's end. In a continued scene the clip must start after the carried frames.

This differs from a **voice reference** (`<Audio N>`, `--ref-audio`): a voice reference gives the speaker's voice, and the words come from the prompt.

### Export

`ffmpeg` trims each continued take's first `context_frames` (and a bridge take's pinned tail), fades the audio 30 ms at each join, concatenates, and writes H.264/AAC. The export stops before the first scene without a finished selected take; `partial` must be requested explicitly.

### Live progress

ComfyUI sends step progress only to the browser session that queued a job. Clapper queues from the server, so it observes ComfyUI's internal progress events in-process and serves them at `/clapper/api/live`.

### Protection against double clicks

A button that queues GPU work or changes something for good (Render take, Render remaining, Re-queue, Stop / Cancel / Stop all, Export, and in the assistant panel Apply / Render / Reject, Apply all and Send) ignores further clicks until the server has answered, and stays disabled meanwhile. The server also refuses the same render (same scene, seed, overrides and parent take) and the same re-queue for 5 seconds (`engine/repeats.py`), so a double tap on a phone or a retried script cannot queue it twice. A different seed, or waiting a few seconds, queues another take as usual. *Render remaining* is refused while any of its scenes is still rendering or waiting, and a proposal can only be applied while it is pending.

## Tools

Developer and evaluation scripts in `tools/`; run them with a Python that has the listed packages.

| Script | Does |
|---|---|
| `tools/join_check.py TAKE_A TAKE_B OUT_DIR` | Joins two take folders the way the export does and measures the join: frame-to-frame change across it versus inside each take, and how identical the carried frames are. Writes `joined.mp4` and `join_strip.png` (last 3 frames of A next to the first 3 kept frames of B). Needs numpy, Pillow and ffmpeg. |
| `tools/assistant_eval.py PROJECT --brief "…" [--language L] [--edit-language L] [--thinking off\|auto\|N] [--sampling JSON] [--repeat N] [--out report.md]` | Measures how well the active assistant model follows the workflow and the [prompt guide](PROMPT_GUIDE.md). On a throwaway copy of the project (the original is only read) it runs four tasks — plan the video from the brief, edit scene 2, insert a scene, change the narration language of scene 1 — applies the proposals, and scores every resulting scene against hard rules (three fields in order, `[Shot 1]`, active `@names`, well-formed and tagged dialogue, speech that fits the length, no readable text) and soft ones (camera, timing cues, starting where the previous scene ends), plus scene count and duplicates. The project's own scenes are scored the same way as a baseline. `--repeat N` aggregates several runs. Writes a Markdown report and a JSON file. A local model is loaded through llama-swap; ComfyUI's queue must be empty. See *Measuring a model* above. |

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "the local model server is not running" | Start llama-swap (for a user service: `systemctl --user start llama-swap`) and check `swap_url` in Assistant settings. |
| "ComfyUI is rendering — the local assistant can load when the queue is empty" | Wait for the render, or add an online provider in Assistant settings and switch to it. |
| The assistant answers slowly after a render | Its cache was restored only if the same chat used it last; otherwise it re-reads the conversation (about 1 s per 1,000 tokens). Compact long chats. |
| The assistant sees no images | Set `vision` for the provider, and give the local model its projector (`--mmproj`). |
| "Clapper is in use by …" at login | Another profile is logged in and active. Ask them to log out, wait 10 minutes after their last activity, or on the computer run `python cli/clapper.py profile logout`. |
| A project is missing from the list after updating | Projects made before profiles belong to nobody. `python cli/clapper.py profile list` shows them; `profile assign ID FOLDER` hands them over. |
| Forgot a password | `python cli/clapper.py profile password ID` on the computer running ComfyUI. |
| `/clapper/` returns 404 | ComfyUI has not loaded the pack. Check the symlink in `custom_nodes/` and restart ComfyUI; look for import errors in the startup log. |
| Code changes have no effect | Python changes need a ComfyUI restart; web files (`web/*`) only need a page reload. If ComfyUI runs as a service without sudo, ComfyUI-Manager can restart it in place: `curl -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:8188/v2/manager/reboot` (only when the queue is empty). |
| "a take of this scene was queued a moment ago" | The same render was sent twice within 5 seconds (a double click or a retried request) and only the first was queued. To render another take right away, give it a different seed; otherwise wait a few seconds. |
| "the previous scene has no finished selected take" | Render or select a take for the previous scene, or set this scene's join to Cut. |
| "not active in this scene: @name" | The text or style text uses a reference the scene does not use. Add it to the scene's list or remove the mention. |
| A take stays *queued* after ComfyUI restarted | Open the project (or `refresh`): it becomes *interrupted*; use Re-queue. |
| Out of GPU memory | Lower megapixels, use fewer or smaller references, keep Chunk FeedForward on, shorten scenes. |
| A visible jump at a join | Check the export warnings (a take made from a different parent); otherwise the scene text probably starts in a different state from where the previous take ends — describe the transition (prompt guide §4). |
| `ffmpeg failed` | Install ffmpeg or put it on the PATH of the ComfyUI process (`~/.local/bin` is also searched). |
| Seconds per step higher than usual at the start | The first steps include warm-up; the figure settles after 2–3 steps. |

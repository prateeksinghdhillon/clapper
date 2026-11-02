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
| `resolution.aspect` | `start_image` | The video shape of fresh scenes: `start_image` (the start image's shape; without one `width`×`height`), `9:16`, `16:9`, `1:1`, `4:5` (at `megapixels`), or `custom` (exactly `width`×`height`). Continued scenes keep their parent's size; a start image of another shape is centre-cropped |
| `resolution.mode` | `start_image` | Older setting, read when `aspect` is missing (`start_image`, anything else = custom) |
| `resolution.megapixels` | 0.30 | e.g. 1536×2752 → 416×736 |
| `resolution.multiple` | 32 | Size rounding |
| `resolution.width` / `height` | 416 / 736 | `custom` size, and the size for `start_image` without a start image |
| `continuity.frames` | 22 | Carried frames: 5 (fast), 22 (balanced), 39 (strong) |
| `continuity.method` | `masked` | `masked` (lock the carried latents in place) or `keyframe` (pin them as a guide) |
| `continuity.audio` | true | Carry sound across the join as well |
| `references.image_size` | `match` | `match` (scale down to the output area) or `max` (up to 2048 px short edge) |
| `scene_seconds` | 5.0 | Default scene length |
| `video_model.mode` | `fl2va` | `fl2va` = the settings above for every scene; `ref2va` = Ref2VA for every scene; `ref2va_fresh` = Ref2VA for scenes without a parent take (fresh or from an image), FL2VA for continued scenes |
| `video_model.ref2va.unet` / `.loras` / `.steps` | `minimax_h3_ref2va_pruned_int8_convrot`, Ref2V Turbo 4-step v0.1 LoRA, 4 | Replace `models.unet`, `loras` and `sampling.steps` when Ref2VA renders a take. A take's own `--overrides` still apply on top |
| `image.model` | `flux2_klein_9b` | The image model for images made in the Studio (looks, sheets, places, start frames, the assistant's `make_images`) and for the Image tool when its form leaves *Image model* on the project's choice: `flux2_klein_9b`, `qwen_image_21_realism` or `qwen_image_21_anime` (see [Image models](#image-models)) |
| `image.negative` | empty | A negative prompt for the models that take one (the Qwen-Image-2.1 models); empty = the model's recommended negative prompt. FLUX.2 Klein ignores it |

### Image models

Defined in `IMAGE_MODELS` in `comfyui_clapper/engine/tools.py`. Each one makes a new picture from a description and edits or combines up to 4 library images (image 1 is the one being changed). Steps and cfg of 0 in the form mean the model's own values below.

| Id | Model | Files | Steps · cfg | Negative prompt |
|---|---|---|---|---|
| `flux2_klein_9b` | FLUX.2 Klein 9B fp8 (Black Forest Labs) | `flux-2-klein-9b-fp8`, `qwen_3_8b_fp8mixed`, `flux2-vae`, with images the `Flux2-Klein-9B-consistency-V2` LoRA | 4 · — (distilled; guidance-free `BasicGuider`) | no |
| `qwen_image_21_realism` | Qwen-Image-2.1 · Noct Q V4, a photoreal fine-tune (Noctaluna), int8 | `NoctQ_V4_int8_convrot`, `qwen3vl_8b_int8_convrot`, `qwen_image_2.1_vae_bf16` | 25 · 3 | yes; default from the model's own workflow |
| `qwen_image_21_anime` | Qwen-Image-2.1 · Noct Q Anime V1, an anime fine-tune (Noctaluna), int8 | `NoctQA_V1_int8_convrot`, `qwen3vl_8b_int8_convrot`, `qwen_image_2.1_vae_bf16` | 25 · 3 | yes; default from the model's own workflow |

The Qwen-Image-2.1 graph is `UNETLoader` → `CLIPLoader` (type `qwen_image`) → `TextEncodeQwenImage21` (prompt, negative prompt, resolution 1024, the library images as `images.image_1…4` with the VAE) → `KSampler` (euler, simple) on an `EmptyLatentImage` of the chosen shape → `VAEDecode`. Clapper's prompts say "image 1", "image 2"; for Qwen-Image-2.1 they are rewritten to its `<image1>`, `<image2>` tags. The anime model draws a photo unless the prompt says *anime*, so a prompt without the word gets "An anime illustration of the following." in front; the photoreal model drew Clapper's "Original character design …" prompts as cartoons, so a prompt that names no style (photo, anime, illustration, cartoon, painting, drawing, sketch, 3D render, art style) gets "A realistic photograph of the following." in front. cfg 1 is about twice as fast and ignores the negative prompt. These models need ComfyUI 0.37 or newer. Licences: FLUX.2 Klein 9B and the Qwen-Image-2.1 models are non-commercial.

### How the assistant writes image prompts

`engine/assistant.py` (`write_image_prompt` for character / place / prop actions, `write_tool_image_prompt` for the Image tool):

- It gets the numbered legend (`tools.image_legend` / `tools.tool_legend`) as text and, when its model can see, each picture with that label; the pose picture is labelled as pose only.
- Rules: `IMAGE_PROMPT_RULES` / `TOOL_IMAGE_RULES` (shared), `ACTION_RULES` per action (a design sends no picture of the subject, so references start at image 1 and are never "the subject"; a scene in a place uses the place's surroundings and "no other people"; a sheet keeps four fixed views), `IMAGE_MODEL_HINTS` per model. The description is marked as possibly older than the pictures; with *Start from* another look it is told not to use the description's clothes.
- **Prompt checker** (`assistant.lint_image_prompt`, in code — not trusting the assistant): every prompt it writes is checked against what Clapper knows, and each problem goes back to it as an exact instruction for one correction round: images mentioned that are not sent; a design that says "keep the same face" (there is no picture yet) or leaves out the description's hair; clothes from the description when image 1 is another look; a Qwen-Image-2.1 edit that does not start with the operation ("Replace … with …", "Add …", "Turn the scene into …" — it edits image 1 conservatively); a stance or arm / leg position with a pose reference; a plain background in a scene in a place; a repeated sentence; over 170 words. The result carries `checks` {`corrected`, `remaining`, `tips`}, shown under ✨ ("✓ Checked: 2 things corrected"). Tips: with Qwen-Image-2.1, a night / weather / time-of-day change is better done with FLUX.2 Klein (tested: Qwen kept the daylight).
- Safeguards on its answer: forgiving JSON reading (line breaks, cut-off answers) and one retry when no real prompt comes back (also when it echoes the template); every spelling of a picture ("the first image", "Figure 2", "<image1>") becomes "image N" (`tools.normalize_image_refs`, also applied by every graph); one correction round when it names a picture that is not sent, then sentences that still do are dropped; with a pose reference, a stance or arm / leg position the user did not ask for is removed (the skeleton gives the pose; tested: "arms down" over an arms-up skeleton lost the pose entirely) and the pose line is appended; negative prompts never name what the prompt asks for or the model's own style.

### References and pose

How each reference reaches the model (`engine/tools.py`, tested in a reference matrix: every kind × every model, see `docs/DESIGN.md`):

- **Wording** (`REF_LINES`): one instruction per kind, phrased like the models' own guides (BFL's FLUX.2 multi-reference guide; Qwen-Image-2.1's prompt-enhancer rules: name each image's role and what is taken from it): "Dress the person from image 1 in exactly the clothing from image 2 (…)", "Give … exactly the hairstyle from image 2", "Place … in the setting of image 2", "Relight the picture with the lighting of image 2", "Redraw the whole picture in the art style of image 2" ….
- **What exactly** (`refs[].what`): the named thing ("red plaid flannel shirt, denim overalls") goes into the instruction. The interface fills it with `POST /clapper/api/assistant/ref_what` (the assistant looks at the image and names only that, never the person). Without names, outfit / hair / accessory / style references were mostly ignored by Qwen-Image-2.1, and FLUX.2 Klein pasted the reference's whole person in.
- **Who stays**: lines name the character ("Lena from image 1"); with an outfit, hairstyle or accessory reference the prompt adds "Image 2 shows a different person: do not draw that person … Keep Lena's own face, hair and clothes" (what is not being replaced). With accessories, a new background, light, style or pose and no other change, the character's description stays in the prompt too.
- **Hidden faces (FLUX.2 Klein)**: an outfit or accessory reference reaches Klein with its person's face and hair greyed out (`SDPoseKeypointExtractor` → `SDPoseFaceBBoxes` scale 2.2 → `ClapperHideFaces`). Tested: whatever the wording, Klein drew the reference's own face onto the character as long as it could see it.
- **Accessories** are named the way they are worn or held ("holding a red umbrella in the right hand, carrying a brown leather bag across the shoulder, wearing black sunglasses"): Qwen-Image-2.1 added only the sunglasses from a plain list.
- **Template conflicts**: the action templates ask for a plain grey background and to keep the art style; a background or lighting reference removes the background phrase, a style reference the art-style phrase.
- **Pose** is its own input (`inputs.pose`, the Image tool's *Pose from*), not an image slot. The prompt carries `POSE_LINE`, which the graph builders replace:
  - SDPose draws the skeleton in the same job: `CheckpointLoaderSimple(sdpose_wholebody_fp16)` → `SDPoseKeypointExtractor` → `SDPoseDrawKeypoints` (body, hands, feet).
  - **FLUX.2 Klein**: the skeleton is the first `ReferenceLatent` (image 1) with the RefControl LoRA (`refcontrol_v2_poses`, strength 1.0; `inputs.pose_strength` changes it); the prompt starts with its trigger "apply pose from image 1 with reference from image 2" and every other image number moves up one. The consistency LoRA is left out (it fights the pose LoRA). Works on the distilled model at 4 steps.
  - **Qwen-Image-2.1, two stages in one job** (needs Klein's files and the RefControl LoRA): Klein poses the character as above; its picture becomes `<image1>` and the original images follow; the sampler uses `TextEncodeQwenImage21`'s latent (image 1's size). Tested: Qwen keeps image 1's composition, so with the character's standing picture as image 1 it ignored both prompt and ControlNet; with an image 1 that already has the pose it redraws it faithfully in its own look.
  - **Qwen-Image-2.1 without Klein**: `ModelPatchLoader(qwen_image_2.1_fun_controlnet_union_int8_convrot)` → `ZImageFunControlnet` (strength 1.4: at 1.0 and cfg 3 it kept image 1's pose and added ghost hands; cfg 1 followed the pose but dropped the outfit).
  - **Without pose files**: the pose image is the last reference with BFL's wording "Match the exact pose from image N: the same arm positions, leg positions, body angle and head direction".

To add another image model, add an entry to `IMAGE_MODELS` (and a graph builder if it is a new family); the form, Settings and availability check pick it up.

## Command line

```bash
python cli/clapper.py [--url http://127.0.0.1:8188] <command> …
```

Any Python ≥ 3.10 with Pillow works; the CLI talks to ComfyUI over HTTP. Scenes can be given as a number (1-based) or an id.

| Command | Does |
|---|---|
| `new PROJECT --name NAME` | Create a project folder and add it to the project list |
| `ref PROJECT NAME FILE [--kind image\|audio] [--description D] [--default] [--start]` | Add a reference (copied into the project) |
| `scene PROJECT [--text T \| --text-file F] [--seconds S] [--join continue\|cut\|bridge] [--refs a,b \| --refs ""] [--title T] [--after N] [--start-image NAME\|none] [--audio-clip ID@SECONDS …]` | Add a scene (`--refs ""` = no references, omitted = project defaults). `--start-image` with `--join cut`: open on that image, or `none` = fresh from the references. `--audio-clip`: a library sound becomes the soundtrack from that time (`none` clears) |
| `scene PROJECT --update N [fields…]` | Change a scene |
| `asset PROJECT add FILE… [--role R] [--title T]` | Add files to the library (images, audio, video); prints their ids, durations and sizes |
| `asset PROJECT list` | Library items: id, kind, role, title, length, `@names`, origin |
| `asset PROJECT name ID NAME [--definition D] [--default]` | Give a library item an `@name` (makes it a reference) |
| `asset PROJECT remove ID` | Move an item without `@name` to `trash/` |
| `frame PROJECT TAKE_ID SECONDS` | Save a frame of a finished take to the library |
| `entity PROJECT list` | Characters, places, props, styles with their looks (★ = main) |
| `entity PROJECT create KIND NAME [--description D] [--from ASSET_ID]` | A character / place / prop / style, optionally from a library image (which becomes its main look) |
| `entity PROJECT main ENTITY_ID ASSET_ID` | Make one of its looks the main look (its @name follows) |
| `entity PROJECT generate ENTITY_ID first\|look\|angle\|place [TEXT…] [--place ID] [--count N]` | Make designs, a new look or angle, or start frames of it in a place |
| `video-model PROJECT fl2va\|ref2va\|ref2va_fresh` | Which H3 model renders the scenes |
| `render PROJECT N [N…] [--seed S] [--overrides JSON] [--wait]` | Queue a take for each scene |
| `render-rest PROJECT [--from-scene N] [--overrides JSON]` | Queue every remaining scene, chained in order |
| `refresh PROJECT` | Collect finished takes, detect interrupted ones |
| `usage PROJECT` | Disk space used, split into takes, exports, references, history |
| `delete PROJECT --confirm "NAME" [--keep-files]` | Delete the project folder for good (refused while takes are rendering); `--keep-files` only removes it from the project list |
| `status PROJECT` | Scenes, takes (number, status, created, finished, parent) and badges |
| `select PROJECT N TAKE_ID` | Select a take |
| `export PROJECT [--partial] [--size auto\|original\|720p\|1080p] [--no-upscaled]` | Join the selected takes; `--partial` exports the ready scenes if later ones have no take. `auto` = the largest scene (upscaled takes set it), the others resized to match |
| `upscale PROJECT [N…] [--method sharpen\|redetail] [--size 720p\|1080p] [--strength light\|strong]` | Upscale the selected takes of the given scenes (all if none); `refresh` records them when done |
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
| `POST /clapper/api/scene` | `path`, `action`: `add` (`text`, `seconds`, `join`, `refs`, `title`, `after`, `start_image`, `audio_clips`) · `update` (`id`, fields) · `move` (`id`, `index`) · `remove` (`id`) | Scene changes |
| `POST /clapper/api/render` | `path`, `scene_id`, optional `seed`, `overrides`, `parent_take` | Queue a take. The same scene with the same `seed` (or none), `overrides` and `parent_take` is refused with 409 for 5 seconds after it was queued |
| `POST /clapper/api/render_rest` | `path`, optional `from_scene`, `overrides` | Queue the remaining scenes, chained; refused while any of those scenes has a take rendering or waiting |
| `POST /clapper/api/requeue` | `path`, `take_id` | New take with the same seed and parent; the same take is refused with 409 for 5 seconds after it was re-queued |
| `POST /clapper/api/stop` | `path`, `take_id` or `all: true` | Interrupt/cancel takes (and the queued takes that continue from them) |
| `POST /clapper/api/select` | `path`, `scene_id`, `take_id` | Select a take |
| `POST /clapper/api/take/restore_prompt` | `path`, `take_id` | Copy a take's text/refs/length/join/start image/sound clips back into its scene |
| `POST /clapper/api/asset` | multipart: `path`, `role`, `title` (single file), one or more `file` | Add files to the library; returns `{project, added: [ids]}` |
| `POST /clapper/api/asset/update` | `path`, `id`, any of `title`, `role`, `tags`, `notes` | Change a library item |
| `POST /clapper/api/asset/name` | `path`, `id`, `name`, `definition`, `default`, `start` | Give a library item an `@name` |
| `POST /clapper/api/asset/remove` | `path`, `id` | Move an item without `@name` to `trash/` (refused while a scene uses it as a sound clip) |
| `POST /clapper/api/ref/remove` | `path`, `name` | Remove an `@name` (refused while the style text or a scene's text mentions it); the item stays |
| `POST /clapper/api/entity` | `path`, `action`: `create` (`kind`, `name`, `description`, `from_asset`) · `update` (`id`, `name`, `description`, `voice`, `default`, `ref_use` `main`/`sheet`) · `main` (`id`, `asset`) · `sheet` (`id`, `asset` or `null`: its character sheet) · `add` (`asset`, `id` or `null`, `look`) · `remove` (`id`, `force`) | Characters, places, props, styles. `create` returns `{entity, project}` |
| `POST /clapper/api/entity/generate` | `path`, `id`, `action` (`first`/`look`/`angle`/`place`/`sheet`), `text`, `place`, `count`, `seed`, `aspect`, `prompt` (optional full prompt), `source` (one of its images to start from instead of the main look), `look` (the new look's name), `refs` [{`asset`, `use`, `what`}] (up to 3 more images; `use`: outfit, pose, hair, accessory, colours, lighting, background, material, style, or free words; `what`: the named thing to take; a `pose` reference is followed as a skeleton and takes no image slot), `model` (an image model id; empty = `image.model`), `negative` (empty = `image.negative`, else the model's own) | Image jobs built from the description and the main look; results are filed as looks (or as start frames for `place`); `sheet` makes a 16:9 turnaround (front, three-quarter, side, back) — the first one becomes its sheet |
| `POST /clapper/api/assistant/image_prompt` | `path`, `id`, `request` (plain words), `action`, `refs`, `source`, `place`, `model` (empty = the project's) | The assistant writes `{change, name, prompt, negative?, explain, images_seen, model, unknown_images}` for an entity action, for that image model, looking at the pictures with the same numbered labels as the legend (and the pose picture). See *How the assistant writes image prompts* |
| `POST /clapper/api/assistant/ref_what` | `path`, `asset`, `use` (`outfit`, `hair`, `accessory`, `colours`, `lighting`, `background`, `material`, `style`) | The assistant names what the image lends for that use, never the person: `{what}` ("red plaid flannel shirt, blue denim overalls, brown leather work boots"); needs a model that can see images |
| `POST /clapper/api/assistant/tool_prompt` | `path`, `tool` (`image`/`speech`), `request` (plain words), `inputs` (the form so far) | The assistant fills in a tool's form. `image` → `{prompt, negative?, explain, images_seen, model}`, written for the chosen (or the project's) image model, looking at `inputs.images` and `inputs.cast`; the negative prompt only for models that take one, never naming what the prompt asks for. `speech` → `{text, emotion, temperature, lines, explain}`: only the words to be spoken, with the feeling carried by words and punctuation (Qwen3-TTS voice clone has no emotion input); stage directions, tags and emojis are removed; a dialogue comes back as `lines` [{`speaker`, `text`}] with the first line as `text` |
| `POST /clapper/api/assistant/write_description` | `path`, `kind`, `words`, `name`, `asset` | Plain words (and the image) → `{name, description}` |
| `POST /clapper/api/assistant/describe_image` | `path`, `asset`, `kind` (`character`/`place`/`prop`/`style`, or none for "what is it"), `auto`, `save` | The assistant looks at the image: `{name, description}` (and `kind` without one). `auto` skips quietly (`skipped`) when the model cannot see images or the local model would have to load during a render; `save` stores the description (and a better title than a file name) on the item |
| `POST /clapper/api/entity/preview` | same as generate | The prompt and images that would be used, plus `legend` [{`n`, `asset`, `label`}] (which picture is image 1, 2 …; the pose reference with `n: null`) and `pose` |
| `POST /clapper/api/tool/legend` | `path`, `inputs` {`images`, `cast`, `pose`} | The Image tool's legend: its own images first, then each *Who and where* main look, then the pose |
| `GET /clapper/api/exports` | `path` | Exported videos with their size, length and upscaled scenes |
| `POST /clapper/api/exports/delete` | `path`, `file` | Delete an export and its JSON |
| `GET /clapper/thumb` | `path`, `file`, `t` | A cached 360 px JPEG of a project video frame (`.cache/thumbs/`) |
| `GET /clapper/api/tools` | — | Tools with their form fields, `ok` and `missing` (node packs / model files), plus problems with your own tool folders, and `image_models` [{`id`, `title`, `note`, `steps`, `cfg`, `negative`, `takes_negative`, `ok`, `missing`}]. The image tool is `ok` when at least one image model can run; `options.model` lists what each one lacks |
| `POST /clapper/api/tool/run` | `path`, `tool`, `inputs` {…}, `count` (1–8), `role`, `seed`, `title`, `entity` + `look` (file the results under that character / place), `version_of` | Queue one job per result; returns `{jobs, project}`. Results land in the library on the next `/refresh`. The image tool's `inputs.model` (empty = the project's `image.model`) and `inputs.negative` (empty = `image.negative`) are stored in the recipe as used. Its `inputs.cast` (entity ids) adds each one's main look after `images` and a line "Image N is Name: description" before the prompt; the recipe keeps `cast` |
| `POST /clapper/api/upscale` | `path`, `take_ids` or `selected: true`, `method` (`sharpen`/`redetail`), `size` (`720p`/`1080p`), `strength` (`light`/`strong`), `seed` | Queue upscales (jobs of kind `upscale`); with `selected`, every scene's selected take without that version yet |
| `POST /clapper/api/upscale/use` | `path`, `take_id`, `version` (a key of the take's `upscales`, or `null`) | Which version the export uses |
| `POST /clapper/api/tool/cancel` | `path`, `job_id` or `all` | Stop a running job, take waiting ones out of the queue |
| `POST /clapper/api/tool/dismiss` | `path`, `job_id` or `finished: true` | Remove jobs from the list (their library items stay) |
| `POST /clapper/api/asset/frame` | `path`, `take_id`, `seconds` | Save a frame of a finished take to the library; returns `{project, asset}` |
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
  refs/<name>.<ext>       copies of files added as references (older projects, CLI `ref`)
  assets/images|audios|videos/<id>.<ext>   library files added with + Add files, CLI `asset add` or Save frame
  trash/                  library files removed from the library
  jobs/<job-id>/          a tool job's files until they move into the library
  .cache/thumbs/          small poster images of takes and exports (safe to delete)
  takes/<scene-id>/<take-id>/
      take.mp4            H.264 + AAC, 24 fps
      audio.wav           the take's sound
      latent.safetensors  raw video + audio latents (needed to continue from this take)
      meta.json           exact prompt, spec, seed, settings, parent, frames, size, timing
  exports/<name>-<date>.mp4 + .json   exported videos and the list of takes used
  chat/c_<id>.json        assistant chats; chat/frames/<take>/ frames used for take reviews
```

`project.json` top level: `schema`, `name`, `created`, `updated`, `settings`, `style`, `notes`, `start_image`, `library` (`items` by name: `kind`, `file`, `sha256`, `description`, `definition`, `card`, `card_status`, `asset`; `defaults`), `entities` (by id: `kind` — character/place/prop/style, `name`, `description`, `main` (asset id), `ref` (its @name), `voice`, `default`, `created`), `assets` (by id: `kind`, `role`, `title`, `file`, `sha256`, `tags`, `transcript` (voice samples), `entity` and `look` (a look of a character/place), `uses` (entities a start frame was made from), `origin` — `{type: upload, source}`, `{type: frame, take, seconds}` or `{type: tool, tool, tool_title, job, seed, inputs}`, `parent`, `created`, `duration` / `width`, `height`), `scenes` (ordered: `id`, `title`, `text`, `seconds`, `join`, `refs` — `null` = defaults, `start_image` — absent/`null` = the project's start image for the first scene, `""` = none, or an image `@name`; `audio_clips` — `[{asset, at}]` with `at` in seconds of scene time; `selected_take`), `takes` (by id: `scene`, `number`, `status`, `created`, `finished`, `seed`, `parent_take`, `join`, `context_frames`, `frames`, `width`, `height`, `prompt_sha256`, `refs`, `scene_text`, `scene_refs`, `scene_join`, `scene_seconds`, `scene_start_image`, `scene_audio_clips`, `model` (`fl2va`/`ref2va`), `upscales` (by version: `file`, `method`, `size`, `strength`, `width`, `height`, `created`, `job`), `upscaled` (the version export uses), `prompt_id`, `render_seconds`, `s_per_it`, `steps`, `error`).

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
| `base_url`, `model`, `api_key` | The OpenAI-compatible base URL (`…/v1`; a pasted `…/v1/chat/completions` is shortened), model name, key (`openai` kind). Changing the base URL to another host clears the saved key, so a key is never sent to a different service |
| `max_tokens_field` | Optional: `max_tokens` or `max_completion_tokens`. Default: `max_completion_tokens` for api.openai.com, `max_tokens` elsewhere; Clapper switches by itself (once) when a provider rejects one |
| `reasoning_effort` | Optional (`low`/`medium`/`high`): sent with every request to an online reasoning model (gpt-oss, o-series, gpt-5 …); dropped by itself if the model rejects it. Set in Assistant settings |
| `request_limit` | Optional: the most tokens one request may contain on this plan (e.g. 8000 for Groq's free tier with gpt-oss-120b). Learned from the provider's first "request too large" answer and saved; then the assistant sends the prompt guide in brief, shortens long tool results and summarises older messages to fit |
| `swap_url` | llama-swap's address (`local` kind), default `http://127.0.0.1:8080` |
| `context`, `vision` | Context window in tokens (drives compaction and the meter); whether the model accepts images |
| `slot_file` | File name the local model's conversation cache is saved to before an eject |
| `start_command` | Local kind: run when llama-swap is not answering and the assistant needs the model, e.g. `systemctl --user start llama-swap`; Clapper then waits for llama-swap (up to 45 s) and loads the model as usual. It works from a ComfyUI system service too (the user's session bus is set for `systemctl --user`). Empty = start llama-swap yourself |
| `thinking` | `"auto"` (default, measured best — see DESIGN.md): think only when planning (the project has no scenes yet, or the message asks for a plan, a storyboard, all scenes, a rewrite or *N scenes*); `false`: answer directly; a number: always think. Local providers only; set in Assistant settings |
| `thinking_budget` | Reasoning tokens per request when thinking (default 2048), sent as `thinking_budget_tokens` (llama-server honours it per request). **think harder** in the chat uses this budget for one message |
| `sampling`, `sampling_thinking` | Optional sampling parameters (`temperature`, `top_p`, `top_k`, `min_p`, `presence_penalty`, `repeat_penalty`); `sampling_thinking` is added on top when thinking. Online providers get only `temperature`, `top_p`, `presence_penalty` (the others are llama.cpp's). Empty = the server's defaults. Edit `llm.json` to change them |

**Online providers.** Assistant settings › *Add provider* has presets (OpenAI, OpenRouter, Groq, Mistral, DeepSeek, Together, Google Gemini and Anthropic through their OpenAI-compatible endpoints, LM Studio and Ollama on another computer) that fill in the base URL and suggest a model; **Load list** next to the model shows the provider's current models (providers retire models; a retired one gives a clear error saying so). New providers start with *can see images* off — turn it on only for vision models. **Test connection** checks what a real turn needs, one by one: the model answers (reply, time, token counts, reasoning), it is in the provider's model list, it calls a tool (proposals need this), and — with *can see images* on — it accepts a picture (if not, the switch is turned off). Errors say what to check (401 key, 402 credit, 403 access, 404 base URL or model, 413 too large, 429 rate limit, context too long).

How requests behave online: 429 and 5xx answers and dropped connections are retried up to 3 times, waiting what the provider asks (`Retry-After`, also with decimals, or "try again in 7.5s" in its message; at most a minute) or with backoff; a model that refuses pictures in a message (e.g. *content must be a string*) gets the same request as text with a note on what was left out, and Clapper remembers it (the chat says so once and *can see images* is turned off); a request larger than the plan allows switches to lean mode (see `request_limit`); Groq's *tool_use_failed* (a malformed tool call) is asked again up to twice; a field the provider rejects (`max_tokens`, `stream_options`, `temperature`, …) is dropped or renamed and the request sent again; reasoning is read from `reasoning_content` and `reasoning`, and list-shaped content (Mistral) is understood; a stream that ends before it is finished is reported, not saved; when a provider sends no token counts, the context is estimated (about 3.5 characters per token) so compaction still happens; a context-length error compacts the chat and retries once. Stop never leaves a half-written tool call in the chat, and tool calls without a reply are answered with a placeholder, which strict APIs require.

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

To have the model ready without starting anything by hand, run llama-swap as a user service that starts at boot (`systemctl --user enable --now llama-swap`, with `loginctl enable-linger` on a machine without a desktop login); idle, it holds no GPU memory — the model is loaded on the assistant's first message and ejected when a render is queued. As a fallback, set the local provider's **start command** (Assistant settings) so Clapper starts llama-swap itself when it is not running.

### How the GPU is shared

- Auto mode: before a request to the local model, ComfyUI's models are unloaded (only when its queue is empty) and the model is loaded. Every job queued in ComfyUI (from Clapper or from ComfyUI's own page) first ejects the local model.
- Manual mode: nothing loads automatically; Clapper's render routes refuse while the local model is loaded; jobs from ComfyUI's own page still eject it.
- Before an eject, the conversation of the chat that used the model last is re-read up to the exact point the next message will add to, and saved (`slot_file`). When that chat loads the model again, the cache is restored and the next message is answered without re-reading the conversation (this hybrid-attention model family cannot rewind a single token, which is why the exact point matters).

### What the model receives

System: fixed instructions + `docs/PROMPT_GUIDE.md` (+ a summary of older messages after compaction). Then the conversation. The newest user message carries a `<project>` snapshot: settings (with video shape and model), start image, style text, story notes, the cast and world (each character / place with its @name, description, looks with the main one, voice), references (description, definition, card), library items without an @name (with their ids), what is being generated, every scene in full with how it starts, its sound clips, references, takes and badges, and the proposals still waiting. Only the newest message has it, so everything before stays identical and the model's prompt cache is reused.

Tools: `get_scene`, `view_reference`, `view_take` (read); `update_notes`, `save_reference_card` (applied at once, they are the assistant's own memory); `add_scenes`, `edit_scene` (also `start` continue/fresh/image, `start_image`, `sound_clips`, and `from_take` to put back the text, references and length a take of that scene was made from), `select_take`, `remove_scene`, `move_scene`, `edit_project`, `set_reference_definition`, `change_settings` (also `video_shape`, `video_model`), `render_scenes`, and in the Studio `create_character` (designs it, or starts from an image), `edit_character` (description, name, voice), `make_images` (look / angle / in a place / more designs) and `speak_line` (in a character's voice) (proposals; the last three and a designed character queue GPU jobs when applied). Proposals are checked on an in-memory copy of the project with the chat's proposals still waiting for the user applied first, in order, so two proposals that only break together are caught (a style text using `@style` against proposed scenes whose references lack it). A card also remembers the values it changes: if the user edits those (the scene text, a definition, a character's description…) after the card was made, *Apply* is refused with a note, and the page asks before applying it anyway (`force`).

What the assistant is told about takes: in the project overview, each scene's selected take, whether it is upscaled, and a newer take that is not selected (or one that failed, with its error); in `get_scene`, every take with its status, what it continued from (and whether the scene before now uses another take), how its references, length, start image or sound clips differ from the scene now, and the text it was made from (recovered from the take's prompt for takes made outside a scene). Changes since its last message cover the scenes, new, failed and upscaled takes, the style text, notes, default references, start image, settings, references, the cast and the Finish.

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

### Upscaling

A job of kind `upscale` per take; the result is `takes/<scene>/<take>/upscaled/<version>.mp4`, where the version is `sharpen-720p`, `sharpen-1080p`, `redetail-light-720p`, … Both methods write the frames with the take's own `audio.wav` (Clapper · Save Video).

- **sharpen**: Clapper · Load Video Frames → `UpscaleModelLoader` `RealESRGAN_x2plus.pth` → `ImageUpscaleWithModel` → `ImageScale` lanczos to the target (short side 720 or 1080, aspect kept, even sizes). 4x-UltraSharp at 1080p took 4.4× longer with no visible gain, so ×2 is used for both sizes.
- **redetail**: Clapper · Load Take Latent → `LTXVSeparateAVLatent` → `MinimaxH3LatentUpscaler3D` (*target dimensions*, multiples of 32 — 720p becomes 704 on the short side) → `LTXVConcatAVLatent` → `MMH3SplitUpscale` (512 px tiles, 25% overlap, 73-frame chunks with 22 overlapping, seam polish auto, colour match) with Ref2VA + its Turbo LoRA + Chunk FeedForward, the take's own prompt and references (no continuation, nothing pinned), euler, sigmas `0.6316, 0.3158, 0` (light) or `0.9035, 0.6316, 0.3158, 0` (strong) → `VAEDecode`. Needs the [Minimax H3 latent upscaler](https://github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler) node pack and `latent_upscale_models/minimax_h3_latent_upscaler_3d_fp16.safetensors`, `LTXVSeparateAVLatent`/`LTXVConcatAVLatent` (ComfyUI core) and the take's `latent.safetensors`.

Export uses each take's chosen version and resizes the others (`scale=…:flags=lanczos`) to the export size.

### Export

`ffmpeg` trims each continued take's first `context_frames` (and a bridge take's pinned tail), fades the audio 30 ms at each join, concatenates, and writes H.264/AAC. The export stops before the first scene without a finished selected take; `partial` must be requested explicitly.

### Live progress

ComfyUI sends step progress only to the browser session that queued a job. Clapper queues from the server, so it observes ComfyUI's internal progress events in-process and serves them at `/clapper/api/live`.

### Protection against double clicks

A button that queues GPU work or changes something for good (Render take, Render remaining, Re-queue, Stop / Cancel / Stop all, Export, and in the assistant panel Apply / Render / Reject, Apply all and Send) ignores further clicks until the server has answered, and stays disabled meanwhile. The server also refuses the same render (same scene, seed, overrides and parent take) and the same re-queue for 5 seconds (`engine/repeats.py`), so a double tap on a phone or a retried script cannot queue it twice. A different seed, or waiting a few seconds, queues another take as usual. *Render remaining* is refused while any of its scenes is still rendering or waiting, and a proposal can only be applied while it is pending.

## Adding your own tool

Any ComfyUI workflow that makes images or audio can become a **Create** tool without writing code:

1. Build the workflow in ComfyUI. Load library files with **Clapper · Load Image (path)** / **Clapper · Load Audio (path)** and end it with **Clapper · Save Images (job)** / **Clapper · Save Audio (job)**.
2. Export it with *Export (API)* and save it as `~/.clapper/tools/<id>/workflow.json`.
3. Next to it write `tool.json`:

```json
{
  "title": "Upscale ×2",
  "kind": "image",
  "description": "Doubles the size of a library image.",
  "default_role": "frame",
  "output_node": "9",
  "seed_node": "5", "seed_input": "seed",
  "fields": [
    {"key": "image", "label": "Image", "type": "image", "node": "1", "input": "path", "required": true},
    {"key": "prompt", "label": "Describe the details", "type": "longtext", "node": "3", "input": "text"},
    {"key": "scale", "label": "Scale", "type": "select", "options": [2, 4], "default": 2, "node": "6", "input": "scale_by"}
  ]
}
```

| Key | Meaning |
|---|---|
| `kind` | `image` or `audio` — what the tool makes |
| `output_node` | id of the Clapper save node; Clapper sets its `out_dir` to the job folder |
| `seed_node`, `seed_input` | optional: where the seed goes (each result gets its own) |
| `fields[]` | the form: `key`, `label`, `type` (`text`, `longtext`, `int`, `float`, `select` + `options`, `image`, `audio`, `images` + `nodes` [one load node per image] + `max`), `default`, `required`, `advanced`, `hint`, and where the value goes: `node` and `input` |

Reload the Clapper page; the tool appears in **Create** (marked ⚠ with the reason if a node pack or model is missing, and listed with the problem if `tool.json` is wrong). The built-in tools are in `comfyui_clapper/engine/tools.py`.

A job writes into `<project>/jobs/<job-id>/`; when it finishes its files move into `assets/` with `origin: {type: "tool", tool, tool_title, job, seed, inputs}`, and an image made from library images records the first one as its `parent`.

## Tools

Developer and evaluation scripts in `tools/`; run them with a Python that has the listed packages.

| Script | Does |
|---|---|
| `HOME=/tmp/clapper-dev python tools/dev_server.py [--port 8190] [--comfy URL]` | Runs the web interface and HTTP API outside ComfyUI's process, sending jobs to a running ComfyUI — for interface work and browser tests without logging anyone out (a scratch `HOME` gives it its own profiles and project list; projects must be inside it). Needs aiohttp, Pillow and psutil (ComfyUI's Python has them). |
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

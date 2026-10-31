# Clapper user guide

This guide explains the ideas behind Clapper and walks through every screen. For how to *write* scenes, see the [prompt guide](PROMPT_GUIDE.md).

## 1. Concepts

| Term | Meaning |
|---|---|
| **Profile** | A login (id + password) with its own projects. Only one profile can use Clapper at a time. |
| **Assistant** | A chat with a language model that plans scenes and writes prompts; its changes are proposals you apply. |
| **Project** | One video. A folder on disk containing everything: scenes, takes, references, exports. |
| **Scene** | One part of the video (usually 5 seconds): its text, its references, its length, and how it joins the scene before it. |
| **Take** | One generation of one scene. Takes are numbered per scene (Take 1, Take 2, …), dated, and never overwritten. |
| **Selected take** | The take of a scene that the video uses. Exactly one per scene. |
| **Studio** | Every image, sound and video clip of the project, each with a role (character, place, voice sample, speech line, music, …) and where it came from (uploaded, a tool, or a frame of a take). |
| **Character / place** (also prop, style) | A thing that must look the same in every scene: a name (its @name), a description, its looks (outfits, angles, variations) with one main look, and for characters a voice. |
| **Reference** | A library item with an `@name` — an image (character, product, style sheet, logo) or an audio sample (a voice). Used in text as `@name`. |
| **Sound clip** | A library sound placed at a time in a scene. The scene's soundtrack becomes that clip from that moment — for dialogue recorded or generated elsewhere. |
| **Start image** | The image a scene opens on. The project's start image (▶) is used by the first scene and shown to the model in every scene as `<Picture 1>`; any later scene can start from its own image instead. |
| **Style text** | Text sent with every scene: format, look, rules. |
| **Definition** | A reference's own description ("@hero is a …"). Added to a scene's prompt only when that scene uses the reference. |
| **Start** | How a scene begins: *Continue* the previous scene, *From image*, *Fresh* from the references only, or *Bridge* (below). |
| **Parent take** | The take of the previous scene that a take continued from. Recorded on every take. |

### How scenes connect

H3 generates at most about 15 seconds at a time, so Clapper makes a long video scene by scene. With **Continue**, a new take starts with the last 22 frames and the matching sound of its parent take locked in place; the model generates everything after that. The locked part is the same material, so the join is seamless, and motion, lighting and sound carry across. On export those 22 carried frames are trimmed, so nothing plays twice.

| Start | What it does | Use when |
|---|---|---|
| **Continue** (default) | Starts from the end of the previous scene's selected take | The normal, flowing case |
| **From image** | A cut: the scene opens exactly on an image from the library (its `@name` becomes `<Picture 1>` in that scene) | A new place or angle you prepared as an image, or a frame saved from another take |
| **Fresh** | A cut: a new shot made from the references and the text only, no start image | A new shot where the model should compose the picture itself |
| **Bridge** | Continues from the previous scene *and* lands on the first 22 frames of the next scene's selected take | Inserting a scene between two finished scenes, or replacing a middle scene while keeping the ones after it. **Experimental — not yet tested on a GPU.** |

The first scene is *From image* (the project's start image, ▶) or *Fresh*.

### Which model renders a scene

MiniMax H3 comes as **FL2VA** (first/last frame) and **Ref2VA** (references). Settings › *Video model* chooses:

| Choice | Fresh / from-image scenes | Continued scenes | Notes |
|---|---|---|---|
| FL2VA for every scene (default) | FL2VA | FL2VA | the proven setup, 20 steps with the Turbo LoRA |
| Ref2VA for every scene | Ref2VA | Ref2VA | one model, no switching; with its Turbo LoRA at 4 steps a 5 s scene takes about 2 minutes on a 16 GB card |
| Ref2VA for fresh scenes, FL2VA when continuing | Ref2VA | FL2VA | the GPU switches models between scenes, which adds load time |

Both models follow image references, start from an image and continue a previous scene.

### Changing things never re-renders silently

If you select a different take for scene 3, scene 4's selected take was still made from the old one. Clapper does **not** re-render scene 4 by itself. It marks scene 4 **"previous scene changed"**, and the export dialog warns that a jump may be visible there. You decide: keep it, retake scene 4 from the new version, or switch scene 3 back.

### Status badges

| Badge | Meaning |
|---|---|
| take ready | The selected take is finished and matches the scene |
| no take yet | The scene has no finished selected take |
| rendering… | A take of this scene is queued or rendering |
| prompt edited since take | The scene's text (or style text / definitions) changed after the selected take was made; the video still shows the older version |
| references changed since take | The scene's reference list changed after the selected take was made |
| previous scene changed | The previous scene's selected take is not the one this take continued from |
| previous scene has no take | This scene continues from a scene that has nothing to continue from |
| prompt error | The text uses a `@name` that is not active in this scene, or another prompt problem (shown in the editor) |

## 2. Screens

### Log in

Clapper opens with a login screen: type your **profile id** and **password**. After logging in you see only your profile's projects; other profiles' projects are hidden from the list, the folder picker and every screen.

- **One profile at a time.** The GPU can only do one thing at a time, so while one profile is logged in and active, other profiles cannot log in. They see *"Clapper is in use by … — try again after they log out"*.
- **The same profile on several devices is fine.** Logged in on the computer, you can also log in with the same profile on your phone.
- **Log out** (profile menu, top right) ends your profile's session on *every* device and frees Clapper for someone else.
- **Change password…** (profile menu): type the current password and the new one twice. Your other devices are logged out; the one you are using stays logged in. Forgot it? On the computer running ComfyUI: `python cli/clapper.py profile password ID` (it asks for the new password and logs that profile out everywhere).
- Profiles and passwords belong to the computer, not to a project: they are stored in `~/.clapper/profiles.json` (passwords only as salted hashes). A project belongs to one profile.
- **Forgot to log out?** A profile counts as active while any of its Clapper pages is open (the page checks in every few seconds). After all its pages are closed or asleep for **10 minutes**, another profile can log in; the forgotten session is then ended. If nobody else logged in meanwhile, you simply continue where you were.
- **First start.** When no profile exists yet, the login screen offers *Create the first profile*; that profile gets every project already on the list. More profiles are made on the computer running ComfyUI (see the [reference](REFERENCE.md#profiles)).

Profiles keep projects private *inside Clapper*. ComfyUI's own page has no login, so someone who opens ComfyUI directly can still see its queue and history.

### Home

- **Projects list** — click a project to open it. Each card shows the disk space the project uses and a **Storage / delete…** button.
- **New project** — type a **name** and choose where to **save** it; no typing of paths needed (made for phones).
  - The project folder is named after the project automatically ("Café Promo 2026" → `cafe-promo-2026`).
  - **Save in** lists the default projects folder (`~/Documents/Clapper-Projects` unless you change it), folders you used before, and the folders that already hold your projects.
  - **Other folder…** opens a folder picker: tap a folder to open it, **↑ Up**, **+ New folder**, **Use this folder**, and *make this the default*. It only shows folders inside your home folder.
  - The line under the fields shows exactly where the project will be saved. If a project with that name is already there, the button changes to **Open project**; if a non-project folder with that name exists, choose another name.
  - *Type a folder path instead* is still available for full control.

### Top bar

One row at the top of every screen:

- **▶ Clapper** — back to the project list. Next to it, the name of the open project.
- **Status pill** — the machine at a glance. While a take renders it shows the step (*Rendering 13/20*); otherwise who holds the graphics card: *GPU · Video* (H3), *GPU · AI* (the assistant) or *GPU · Free*. `· +N` means N more jobs are waiting in ComfyUI's queue. On wide screens it also shows GPU load and VRAM. Tap it for the **status menu**:
  - **Machine** (every 2 s): CPU load and temperature, RAM, GPU load / temperature / power, VRAM. Bars turn amber and red at high load or temperature. VRAM stays high between renders because ComfyUI keeps the model loaded.
  - **ComfyUI queue**: how many jobs are running and waiting.
  - **GPU**: the state of the assistant model and of ComfyUI, the **Auto / Manual** switch, and **Load assistant**, **Eject assistant**, **Free ComfyUI memory** (see *Assistant*).
  - **ComfyUI log** and **Assistant settings…**.
- In a project: **✨ Assistant** (opens the chat, see *Assistant*) and **Settings** (generation settings). On narrower screens Settings moves into the profile menu; on phones the Assistant is a tab at the bottom.
- **Profile menu** (the round initial ▾): your name and profile id; in a project also **Settings** (on narrower screens) and **Storage**; then **ComfyUI log** (see *Logs panel*), **Assistant settings…**, **Change password…**, and **Log out** (ends the profile's session on every device).

The assistant chat and the scene editor open as panels below the top bar, so the top bar and the live bar stay visible.

### Workspaces

A project has three workspaces; switch between them at any time — nothing stops or is lost:

| Workspace | For |
|---|---|
| **Studio** | Everything a video is made from: characters, places, props and styles, and every image, sound and video clip — make them, change them, keep their versions |
| **Scenes** | The storyboard: write scenes, render and choose takes |
| **Finish** | Upscale each scene, export the video, previous exports |

On a laptop they are a rail on the left, on a tablet tabs at the top, on a phone a bar at the bottom (with the ✨ Assistant as a fourth tab). Clapper reopens the workspace you used last.

### Live bar

Appears while anything renders: which scene, the phase (*Loading models → Encoding prompt & references → Sampling → Decoding & saving*), the step (e.g. *step 13/20*), seconds per step, time elapsed and time left in the phase. The first steps include warm-up, so the speed settles after step 2–3. Decoding and saving add roughly 20 seconds after sampling.

- **■ Stop** — stops the take that is rendering. Takes queued after it that continue from it are cancelled too (they could not work without it). You are asked to confirm.
- **Stop all** — stops every rendering or waiting take of the project.

Stopped takes stay in the list as *interrupted — stopped by you* with a **Re-queue** button.

### Scenes: storyboard

One card per scene, in order: a thumbnail of the selected take (hover or tap to play), the scene number, the join mode, a one-line summary, the length, which take is selected (*Take 2 of 3 selected*), and the badges.

- The line above the cards shows the number of scenes, the planned length, how many scenes have a take, and the **disk space** the project uses (hover for the split; click to open *Storage*).
- **Style & notes** — the shared style text sent with every scene (changing it changes every scene's prompt, so all scenes show *prompt edited since take*) and the story notes, the assistant's memory (not sent to H3).
- **⤢ Upscale scenes…** — see *Finish*.
- **Storage** — the project's disk space split into takes, exports, references and history, and **Delete this project** (below).
- **+** between two cards inserts a new scene there.
- **+ Add scene at the end** extends the video.
- **Render remaining N scenes (≈ M min)** queues every scene from the first one without a take to the end, in order, each continuing from the one before. The time estimate comes from your own previous render times. Finished takes are selected automatically; you can still retake any of them.

### Scene editor (click a scene)

- **Title** (optional) and **What happens** — the scene text. Click an `@name` chip under the text to insert a reference at the cursor.
- **Length (seconds)** — empty means the project default (Settings).
- **Starts** — Continue / From image (then choose the image) / Fresh / Bridge.
- **References in this scene** — *use project defaults*, or click chips to choose this scene's own list. Below it, the exact mapping for this scene (e.g. `@hero → <Picture 2>`).
- **Sound clips** — add a sound from the library and set when it starts (seconds into the scene). The scene's soundtrack becomes that clip from that moment: the same words, timing and voice. If the clip runs past the end of the scene it is cut, and the editor says so. In a continued scene the time counts from the join. Describe what you hear in the text too (for example *"the narrator says …"* and the words) so the picture fits it.
- **Prompt sent to H3** — the complete text the model receives: style text, the definitions of this scene's references, an automatic *"Continuation of Chunk N."* opener for continued scenes, and the scene text, with every `@name` replaced by its number. Errors are shown here and block rendering.
- **Render take** — queue a new take. **Options**: a fixed seed, a different step count for this take only, or which take of the previous scene to continue from (default: its selected take). The button stays disabled until the take is queued, so an accidental double click queues only one take; Clapper also refuses the same render again within a few seconds (*"a take of this scene was queued a moment ago"*). To queue a second take at once, give it a different seed.
- **Takes** — oldest first. Each shows its number, status, *Created* and *Finished* date and time (in your device's time zone), how long it took, seed, and which take it continued from. Buttons:
  - **Use this take** — select it for the video.
  - **Play** — the player also has **Save this frame to Library**: pause where you want and save that frame as an image (to start a later scene from it, or as a reference).
  - **Restore its prompt** — put this take's text, references, length and join back into the scene editor (to redo or vary that version).
  - **Stop / Cancel** while rendering or waiting; **Re-queue** after an interruption or failure.
- **Remove scene from timeline** — its takes stay saved on disk.

Everything you type is saved automatically.

### Studio

The Studio is where a video's material lives and grows. On the left (on tablets and phones: a row of chips) are its **collections**:

- **Cast & world** — **Characters**, **Places**, **Props**, **Styles**.
- **Files** — all files, images, start images & frames, voices, speech lines, music & effects, video clips, and the items with an @name.
- **From scenes** — every finished **scene take** and every **export**.

**Search** finds names, titles and the prompts that made things. **⬆ Upload** (or dropping files anywhere in the Studio) adds images, sounds (mp3, wav, m4a, ogg, flac, …) and video clips; files are copied into the project, and durations and sizes are read automatically. **＋ New** opens the *New* page (below).

Click anything to open it in the **detail panel** (on the right on a laptop, sliding over the page on a tablet, full screen with ← on a phone):

- a large preview, the **title** and **role**, its size or length and where it came from (*made with Image · seed 4242*, *frame of scene 2 Take 1 at 3.4 s*);
- **Belongs to** — file an image under a character, place, prop or style, with a look label; **★ Make it … 's main look**; or **Make a character / place from it…**;
- **Versions** — the item's family: what it was made from and everything made from it; tap one to open it;
- **Recipe** (things made with a tool) — the prompt, images and settings that made it, editable; **Make new version** keeps the original and adds the result to the family. Images without a recipe have **Change it** ("at night with the street lamp on");
- for voices and speech lines: **🗣 Speak in this voice** (with the sample's transcript, which improves the likeness);
- **In scenes** — its @name (or **Give it an @name**), the definition sent to H3, the assistant's card, ★ default / ▶ start image, and which scenes use it;
- **⤓ Download** and **Remove** (to the project's `trash/` folder; refused while an @name or a scene's sound clip uses it).

### Characters, places, props and styles

A character (likewise a place, prop or style) has its own page:

- its **name**, which is also its `@name` in scenes (an image that already had an @name keeps it);
- a **description** of what makes it look like itself ("a 17-year-old girl, short indigo bob with a white streak, amber eyes…"). It is added to every image made from it, and it is the definition H3 receives in every scene that uses it — one text keeps the pictures and the video consistent;
- a **voice** (characters) — a voice sample from the library, used by 🗣 Speak;
- its **looks** — every image of it with a label (*default*, *winter outfit*, *side view*, *at night*). **★** marks the **main look**: the image scenes use as its @name. **Make main** on another look switches every scene that uses it. **＋ Add from library** files images you already have under it.

The action box makes more of it, using the description and the main look as image 1, so you type only what changes:

| Action | You type | You get |
|---|---|---|
| **New outfit / look** (places: **Variation**) | "wearing a winter coat and a red scarf" · "at night in the rain" | new looks, filed under it |
| **New angle / pose** | "from the side, walking" · "from above" | new looks |
| **Put in a place** (characters) | a place, and "sitting on the bench, looking down the road" | start frames for scenes, with both as references (Files › Start images & frames) |
| **Design it** / **More designs** | optional extra details | first designs from the description alone |

**See / edit the prompt** shows the exact prompt that will be used; edit it to take full control. Each image takes about 4–10 s on a 16 GB card; running jobs show as tiles you can cancel.

### New

**＋ New** offers:

- **Character / Place / Prop / Style** — a name, a description, and either **Design it now** (1–4 first designs; pick the main look afterwards) or **Start from an image I have**;
- the tools: **Image (FLUX.2 Klein)**, **Speech (Qwen3-TTS voice clone)** and your own tools (below). Results appear under **Made here**; open one to name it, file it under a character or make versions;
- **Upload files**.

| Tool | Makes | Needs |
|---|---|---|
| **Image (FLUX.2 Klein)** | A new image from a description, or up to 4 library images changed or combined ("the girl from image 1 sits in the shelter from image 2"). About 4 s per image at the default 4 steps. The shape starts at the video's shape | FLUX.2 Klein 9B (`flux-2-klein-9b-fp8`), its text encoder `qwen_3_8b_fp8mixed` and `flux2-vae`; with images also the Klein consistency LoRA (strength under *More options*) |
| **Speech (Qwen3-TTS voice clone)** | A spoken line in the voice of a library sample, in 10 languages. Use it as a scene's sound clip. A few seconds per line | The `qwen3-tts-comfyui` node pack and its 1.7B Base model; a clean voice sample of a few seconds |
| **Your own tools** | Anything a ComfyUI workflow makes — see the [Reference](REFERENCE.md#adding-your-own-tool) | — |

Tools that cannot run on this ComfyUI say what is missing. Tool jobs share ComfyUI's queue with takes in the order queued; making the images and voices first, then rendering scenes, keeps model switching to a minimum.

A **voice sample** with an `@name` used in a scene is a *voice reference*: the scene's speaker gets that voice and says new words written in the text. A **sound clip** (scene editor) is played as-is. Use a voice reference for new lines in a known voice, a sound clip for lines you already have as audio.

Limits per scene: up to 9 images and 3 audio references. Video references are not supported yet.

### Settings dialog

Two views; Clapper remembers which one you use.

**Simple** — plain choices, each with what it changes and how long to expect:

| Choice | Options | What it changes |
|---|---|---|
| Video shape | **Portrait 9:16** · **Landscape 16:9** · **Square 1:1** · **Vertical 4:5** · **Match the start image**, each with its size while rendering | the shape of new scenes that start fresh or from an image (a continued scene keeps the size of the scene before it; a start image of another shape is centre-cropped); places and start frames made in the Studio follow it |
| Quality and speed | **⚡ Draft** (Ref2VA, 4 steps, ≈2 min per 5 s scene) · **★ Standard** (FL2VA, 20 steps, ≈5.5 min — the most tested setup) | the model and its steps; *Custom* shows when Advanced values match neither |
| Steps | per model in use, each with its time per scene | time; with these Turbo LoRAs more steps made no visible difference in testing |
| Picture size while rendering | 416×736 (tested on 16 GB) and two larger sizes | time and memory; larger sizes are untested on 16 GB cards and may run out — upscale the finished takes instead |
| Default scene length | 3 · 5 · 7 · 10 s | time per scene (each scene can still set its own) |
| Joins between scenes | Quick (5 frames) · Smooth (22, default) · Strong (39); carry sound across joins | how much of the previous scene each continued scene starts from |

Each time is marked **measured** (on the reference card, an RTX 5060 Ti 16 GB, or from this project's own finished takes) or **estimate** (scaled from measurements). Once you have rendered takes, their real timings replace the defaults. A line at the bottom gives the time for one scene and for the whole video.

**Advanced** — everything: FL2VA steps presets, sampler and scheduler, default scene length, video shape (also **custom**: an exact width × height in multiples of 32) and megapixels, continuity (carried frames, method, carry sound), reference image size, LoRAs, model names, the memory patch, and the video model with the Ref2VA model, LoRA and steps.

**Reset to defaults** restores the proven setup. Settings apply to takes rendered afterwards; every take records the settings it used. See [Reference § Settings](REFERENCE.md#settings).

### Finish

Three steps on one page:

**1 · Scenes** — every scene with its selected take. Finish each scene on its own: **⤢ Upscale…** a scene, or tick several (or none, for every scene not upscaled yet) and upscale them — they run one after another, so memory use stays the same however many you choose.

| How | What it does | Time (7 s take, 16 GB card) |
|---|---|---|
| **Sharpen** | Real-ESRGAN ×2 on every frame, then an exact resize. Crisp edges; the picture stays exactly the same, including its flaws | 720p ≈ 1.2 min · 1080p ≈ 1.4 min |
| **Re-detail** | The H3 latent upscaler, then H3 (Ref2VA) redraws fine detail at the new size, in tiles so it fits in 16 GB. Faces, eyes and hair become clean; small details can change. *Light* stays closest to the take, *Strong* redraws more | light 720p ≈ 7.5 min · light 1080p ≈ 19 min |

Both keep the take's sound exactly. Each method and size is kept as its own version of the take (a new run never replaces another); each scene lists its versions with **▶** play, **⤓** download and **✕** delete, and a choice of which one goes into the video (or the *Original*). Scene cards on the storyboard show *⤢ 1080p* when upscaled.

**2 · Sound & overlays** — added at export, exact, nothing re-rendered. A timeline strip shows the scenes and, one lane each, every track and overlay.

- **Scenes' own sound** — the volume of the rendered sound.
- **＋ Add music or sound** — any sound from the Studio, placed at a time, with volume, fade in and out, **Loop to the end** (music) and **Dip under the scenes' sound** (ducking: the track gets quieter while the scenes' own sound is loud — voices, effects, but also loud ambience such as heavy rain).
- **＋ Add image** — a logo or product photo over the picture: position (corners, edges, centre or custom x/y), size, opacity, from / until (empty = to the end) and fade. Images stay exact because they are placed, not generated.
- **＋ Title card…** — type the text (two lines allowed), choose a style and colour, with or without a dark box; Clapper renders it as a sharp image with a font that has your text's script (Latin, Devanagari, Chinese, Japanese, Korean, Arabic, …) and places it at the top for the first 3 seconds. Title cards are kept in the Studio (role *overlay*).

**3 · Export** — what will be exported (*Scenes 1–5 of 10 · 25.0 s*), which scenes are upscaled, and the **video size**: the largest scene (scenes that are not upscaled are resized to match, which looks softer), 720p, 1080p, or the original takes. It says clearly when the full video is not ready and offers a **partial video** of the ready scenes (it stops before the first scene without a take — skipping a middle one would leave a jump), and lists **Check before exporting** warnings with the time where they happen. The result plays right there with a download button and is saved in the project's `exports/` folder with a JSON list of exactly which takes were used.

**With sound & overlays** can be switched off for a clean version.

**4 · Previous exports** — every export with play, download and delete (also in Studio › Exports).

### Storage and deleting a project

The Storage dialog (project screen, or *Storage / delete…* on a project card) shows how much disk space the project uses: **takes** (video, sound and the raw data needed to continue from them — the largest part), **exports**, **references** (copies of your files) and **history** (earlier versions of the project file).

**Delete this project** offers two choices:

- **Delete the project and all its files** — the whole project folder: every take, export, reference copy and its history. This frees the space shown and **cannot be undone**. Your original reference files elsewhere on disk are not touched.
- **Only remove it from the project list** — nothing is deleted; the folder can be opened again later with *New project → Create / open*.

To confirm you type the project's exact name. Deleting is refused while any take of the project is rendering or waiting — stop them first.

### Logs panel

ComfyUI's terminal output, refreshed every 2 seconds. Filter *Everything / Warnings & errors / Progress only*, search text, **follow** (stay at the newest line), **Copy**.

### Assistant

A chat with a language model that knows how Clapper works and how H3 prompts are written (it is given the [prompt guide](PROMPT_GUIDE.md)). Describe what you want in plain words; it writes the exact scene text, picks references, lengths and joins.

- **It proposes, you decide.** Every change (new scenes, a rewritten scene, the style text, a reference definition, settings, rendering — and in the Studio a new character or place, a new look or angle, start frames of a character in a place, a spoken line in a character's voice) appears as a **card** with the new text (and the old one, for edits). **Apply** or **Reject** each card, or **Apply all**. Nothing in the project changes until you apply. Each card is checked before you see it: a scene that would not render (for example a `@name` that is not active in it) is sent back to the assistant to fix, never shown to you.
- **It always works from the video as it is:** every message tells it what changed since its last answer (your own edits included), then gives it the whole project, with your request last. When you name a scene ("fix scene 3"), it also sees the last frame of the scene before and frames of that scene's take, so it writes from what the video really shows. It checks its own scenes before you see them (too many actions for the length, a character named but not in the references, whether a continued scene starts from the right picture) and corrects them.
- **What it always knows:** the whole project as it is at that moment (scenes with their full text, how they start and their sound clips, the cast with descriptions, looks and voices, references with their definitions, library items, style text, settings including video shape, which takes exist, what is being generated) and the *story notes*.
- **How it makes a video:** agrees on length, shape and language; writes the style text; proposes the characters and places with precise descriptions (and designs them); once they have main looks, start frames and other looks; spoken lines in the characters' voices; then all scenes, each opening the right way and with its lines as sound clips. Images and lines are made only after you apply their cards, and it waits for them before writing scenes that use them.
- **What it can look at:** a reference image (it asks to), and frames of a take (**✨ Review** on a take sends four frames from start to end with the scene text).
- **Story notes** (Scenes › Style & notes) are the assistant's lasting memory for the project: story, characters, tone, your decisions. The assistant keeps them up to date itself; you can edit them too. They are never sent to H3.
- **Reference cards** (Studio, in an image's panel): what the reference looks like, for the assistant. **✨ Describe** lets the assistant look at the image and write one; check and correct it, then **Save card**. Cards the assistant wrote itself are marked as unchecked until you save them. The assistant trusts your definition over its own eyes.
- **Where to start:** in an empty project, **✨ Plan the video with the assistant** on the storyboard; in the scene editor, **✨ Ask the assistant**; on a finished take, **✨ Review**.
- **Chats:** each project keeps its chats (choose them at the top of the panel, **＋** for a new one). The bar next to the model name shows how full the model's memory (context) is; at 60% the older messages are summarised automatically so the conversation can go on. Summarised messages stay visible in the panel; **Compact** does it now.
- **Connections:** the local model (llama-swap, sharing the GPU) or any OpenAI-compatible service. In Assistant settings, **Add provider** offers presets (OpenAI, OpenRouter, Groq, Mistral, DeepSeek, Together, Google Gemini, Anthropic, LM Studio or Ollama on another computer), and **Test connection** checks the address, key and model with one short request and says exactly what is wrong if it fails (key, credit, base URL, model, rate limit). Busy services (429) and passing network errors are retried automatically.
- **Thinking:** by default (*Auto*, in Assistant settings) the assistant reasons before answering only when you plan a video (an empty storyboard, or a message asking for a plan, all scenes or a rewrite) and answers small edits directly. **think harder** makes it reason for one message, up to the thinking budget (2048 tokens by default): slower, and not reliably better for small edits.

**GPU controls** (tap the status pill; the *GPU* part of the status menu). A local assistant model and H3 do not fit on the graphics card together, so only one is loaded at a time.

- **Auto** (default): the assistant loads when you send it a message (ComfyUI's models are unloaded first, only while nothing is rendering), and it is ejected the moment a render is queued, also renders queued from ComfyUI's own page. Before it is ejected its conversation is saved, so the next message continues without re-reading everything.
- **Manual**: only the buttons change anything: **Load assistant**, **Eject assistant**, **Free ComfyUI memory**. Clapper refuses to start a render while the assistant is loaded.
- Switching costs time: loading the assistant takes a few seconds; the first render after it takes about a minute longer while H3 loads again. Plan several scenes with the assistant, then render them together.
- **Assistant settings…** (status menu or profile menu): the local model (served by llama-swap on this computer) or any online service with an OpenAI-compatible API (OpenAI, OpenRouter, Groq, …; or LM Studio / Ollama on another computer). Online services don't use this graphics card, so they work while H3 renders. API keys stay on the computer running ComfyUI.

The local model is small and runs at about 2 bits: it follows the workflow well but makes mistakes. Read the cards before applying them, and don't take its reviews of takes as final.

## 3. Workflows

### Start a new video

1. New project → **Studio**: make the **characters** and **places** (*＋ New › Character*: describe it and design it, or start from an image), make their looks, and **Put in a place** for start frames. Upload a voice sample and **🗣 Speak** the lines you need.
2. **Settings › Video shape** (portrait, landscape, …) and **Scenes › Style & notes**: format, art style, rules that always apply.
3. Scene 1 → **Render take**. Judge the style, the characters and the sound before going further.
4. Write the remaining scenes (you can write them all first), then **Render remaining**.

### Retake a scene

Open the scene → **Render take** again. A new numbered take appears; the old ones stay. Compare with **Play**, then **Use this take** on the best one. A different seed gives a different result from the same text.

### Try a different version of a scene

Edit the scene's text (or its references) → **Render take**. The earlier takes keep their own text; **Restore its prompt** on any of them brings its version back. Selecting the new version flags the next scene *previous scene changed*: retake the next scene (and adjust its opening so it follows from the new version), or switch back.

### Extend the video

**+ Add scene at the end** → write it → **Render take**. Nothing before it is re-rendered.

### Insert a scene

Use **+** between two scenes. The new scene continues from the one before it. The scene after it now continues from a take that is no longer directly before it, so it shows *previous scene changed*: either retake it (it will continue from the new scene), or set the new scene's join to **Bridge** so the new scene lands on the start of the existing next scene (experimental).

### Stop and resume

Stop anything with **■ Stop**. Close the browser, restart ComfyUI or shut down the computer at any time: reopening the project shows exactly where you were. Takes that were rendering when ComfyUI stopped show as *interrupted* with **Re-queue** (same seed and settings, so the same result).

### Plan a video with the assistant

1. Create the project and add the references (a character, a style sheet, a logo) with short names and descriptions.
2. On the empty storyboard press **✨ Plan the video with the assistant**, and describe the idea, the length, the format and the look.
3. The assistant proposes the style text, definitions for the references, and all scenes. Read the cards, ask for changes in plain words ("make scene 2 slower", "the narrator should be a woman"), then **Apply**.
4. Render scene 1, check it (**✨ Review** gives a second opinion), then render the rest.

### Use it from a phone

Open `http://<computer>:8188/clapper/` on a phone that can reach the computer (for example over Tailscale) and log in with your profile — the same profile can be logged in on the computer at the same time. The workspaces move to a bar at the bottom (Studio · Scenes · Finish · Assistant), the storyboard becomes a vertical list, the Studio's detail panel and the scene editor open full screen (← goes back), and the top bar (with the status pill) and the live bar stay at the top. Thumbnails of takes and exports are small cached images, so browsing stays light on a phone connection.

### Bring in a Continuum Timeline prompt

If you already have a long prompt written for ComfyUI-H3-Continuum's Timeline, turn it into a project on the computer running ComfyUI:

```bash
python cli/clapper.py new ~/Documents/Clapper-Projects/cafe --name "Café promo"
python cli/clapper.py ref ~/Documents/Clapper-Projects/cafe opening start.png --start
python cli/clapper.py ref ~/Documents/Clapper-Projects/cafe hero hero.png --default
python cli/clapper.py import-timeline ~/Documents/Clapper-Projects/cafe timeline.txt --picture 1=opening --picture 2=hero
python cli/clapper.py profile assign YOUR_ID ~/Documents/Clapper-Projects/cafe
```

The preamble becomes the style text, `<Picture N> is …` lines become the references' definitions, and each section (`[Chunk 2]`, `[5-10s]`, …) becomes a scene; the first is a cut, the others continue. `--picture N=name` says which reference was `<Picture N>` in the Timeline prompt; add the references first so their definitions can be filled in. With all references active, every scene's *Prompt sent to H3* is exactly Continuum's prompt for that chunk. Take folders rendered with `clapper.py take` can be added to a scene with `adopt` (see the [reference](REFERENCE.md#command-line)).

### Work from the command line or a script

Everything the interface does is also available as `python cli/clapper.py …` commands and as an HTTP API under `/clapper/api/` — create projects, add references and scenes, render, check status, export. See the [reference](REFERENCE.md#command-line).

### Share the computer with other people

Give each person a profile (`python cli/clapper.py profile add ID --name "Name"`) and hand their projects over with `profile assign`. When you are done, press **Log out** so the next person can log in right away instead of waiting 10 minutes. Takes you queued keep rendering after you log out; the next person's takes wait behind them in ComfyUI's queue.

## 4. Tips

- **One scene first.** Render scene 1 alone and check it before rendering the rest.
- **Write transitions.** If a scene starts in a different state from where the previous one ends, describe the change ("the catalogue slides away and the chat appears") — otherwise the model may jump straight to the new state. See the prompt guide.
- **Keep the style text stable** once scenes are rendered; every change there changes every scene.
- **Readable text and logos warp.** Keep text out of scenes where possible.
- **Speed.** The default 20 steps with the 8-step Turbo LoRA is the proven setup; 8 steps is about 2.5× faster sampling — try it on one take and compare.

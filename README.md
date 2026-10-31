# Clapper

**Scene-and-take video directing for MiniMax H3 in ComfyUI.**

Clapper lets you make a long MiniMax H3 video the way a film set works. You write the video as a list of **scenes** and shoot **takes** of each scene. Every take is kept, you pick the best one per scene, and the export joins the picked takes into one continuous video. You can change, retake, extend or insert any scene without re-rendering the parts you already like.

It is a ComfyUI custom node pack with its own full-page web interface (desktop and phone), a command-line tool and an HTTP API. An optional assistant (a local model or any OpenAI-compatible service) can plan the scenes and write the prompts for you.

```
Storyboard ──► Scene editor ──► Render take ──► Takes (numbered, dated) ──► Select ──► Export
   ▲                                                                                   │
   └──────────── retake · vary · extend · insert · switch takes · resume later ◄────────┘
```

## Features

**Directing**
- **Scenes and takes.** Any number of takes per scene (Take 1, Take 2, …), each kept on disk with the exact prompt, references, seed and settings that made it. One take per scene is *selected*, and the export uses the selected takes.
- **Seamless joins.** A scene continues from the last 22 frames of the previous scene's selected take, picture **and** sound. Scenes rendered separately join without a visible cut.
- **Nothing re-renders silently.** Change a scene and the later scenes get a badge such as *previous scene changed*. You decide what to redo.
- **Per-scene references.** Images and voice samples live in a named library (`@hero`, `@logo`, `@narrator`). Each scene uses the project defaults or its own list, and Clapper numbers them correctly (`<Picture N>`, `<Audio N>`) for every scene.

**Making what a video needs**
- **Library** of every image, sound and clip in the project, with where it came from. Any item becomes a reference with an `@name`.
- **✨ Create** images with FLUX.2 Klein (new, or the same character in a new pose or place, several references combined) and speech with Qwen3-TTS voice cloning. Add your own ComfyUI workflows as tools with a small `tool.json`.
- **Sound clips**: a recorded or generated line placed in a scene becomes its soundtrack, word for word, and the picture is made around it.
- **Start any scene** by continuing the previous one, from a library image, or fresh from the references; render with MiniMax H3 FL2VA or Ref2VA.
- **Upscale** the chosen takes to 720p or 1080p: fast *Sharpen*, or *Re-detail* where H3 redraws fine detail (fits in 16 GB). Export picks the upscaled versions.
- **Simple settings** with the time to expect for every choice (measured or estimated, learning from your own takes), and every option under *Advanced*.

**Working with it**
- **Web interface** served by ComfyUI: storyboard, scene editor, references, settings, export. It works on a phone too.
- **Live progress.** Each render shows its phase, step, seconds per step and time left. You get Stop / Cancel buttons, machine stats (CPU, RAM, GPU, VRAM, temperatures) and ComfyUI's log.
- **Saved as a project folder** with autosave and history. Close the browser or restart the computer and carry on where you left off. Interrupted renders can be re-queued with the same seed.
- **Export** trims the carried-over frames, fades the audio at each join, warns about continuity problems, and asks before a partial export.
- **Profiles.** Each person logs in with a profile id and password and sees only their own projects. One profile uses Clapper at a time, because the GPU is shared.

**Assistant (optional)**
- **A chat that knows the workflow and the H3 prompt rules.** Describe a video in plain words. It proposes the style text, reference definitions and every scene as cards, and nothing changes until you apply one.
- **Sees and remembers.** It can look at reference images and review takes from their frames, and it keeps story notes and reference cards as lasting memory for the project.
- **A local model or an online one.** Run a local model through llama-swap, which takes turns with H3 on the GPU (switched automatically), or use any OpenAI-compatible API.

**Automation and compatibility**
- **Command line and HTTP API** for everything the interface does.
- **Continuum compatible.** The defaults match a proven ComfyUI-H3-Continuum draft setup, and Continuum Timeline prompts can be imported as projects.

## Requirements

- **ComfyUI** with the MiniMax H3 core nodes (`MiniMaxH3ReferenceToVideo`, `MiniMaxH3AddGuide`, …). Verified on ComfyUI 0.34.0.
- **MiniMax H3 model files** in the usual ComfyUI folders (names can be changed in Settings):
  - `diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors`
  - `text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors`
  - `vae/minimax_h3_video_vae_fp16.safetensors`, `vae/minimax_h3_audio_vae_fp32.safetensors`
  - `loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors` (used by the default setup; can be switched off)
- **[comfyui-kjnodes](https://github.com/kijai/ComfyUI-KJNodes)** for the `MiniMaxChunkFeedForward` memory patch. It is on by default and can be switched off in Settings.
- **ffmpeg** on the PATH of the ComfyUI process (`~/.local/bin` is also searched).
- **A GPU that runs H3 at your chosen size.** Tested on an RTX 5060 Ti 16 GB with 31 GB RAM on Linux: a 5-second scene at 416×736 takes about 5–6 minutes with the default settings. Other GPUs are untested. If you run out of memory, lower the megapixels, use fewer references and keep the memory patch on.
- **Optional, for the Create tools** (each tool says what it is missing):
  - images: FLUX.2 Klein 9B (`diffusion_models/flux-2-klein-9b-fp8.safetensors`, `text_encoders/qwen_3_8b_fp8mixed.safetensors`, `vae/flux2-vae.safetensors`; for changing images also `loras/Flux2-Klein-9B-consistency-V2.safetensors`);
  - speech: the [qwen3-tts-comfyui](https://github.com/flybirdxx/ComfyUI-Qwen-TTS) node pack with its 1.7B Base model.
- **Optional, for upscaling:** `upscale_models/RealESRGAN_x2plus.pth` (Sharpen); the [Minimax H3 latent upscaler](https://github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler) node pack with `latent_upscale_models/minimax_h3_latent_upscaler_3d_fp16.safetensors` and the Ref2VA model (Re-detail).
- **Optional, for the Ref2VA video model:** `diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors` and `loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors`.
- **Optional, for the assistant:**
  - a local model: [llama-swap](https://github.com/mostlygeek/llama-swap) in front of llama.cpp, with a model that fits your GPU (plus its vision projector if it should see images). It takes turns with H3 on the GPU. See [Reference › Local model](docs/REFERENCE.md#local-model-with-llama-swap).
  - or an API key for any OpenAI-compatible service (OpenAI, OpenRouter, Groq, …, or LM Studio / Ollama on another computer). This needs no GPU.

## Install

```bash
git clone https://github.com/prateeksinghdhillon/clapper.git ~/Documents/Clapper
ln -s ~/Documents/Clapper/comfyui_clapper ~/ComfyUI/custom_nodes/comfyui_clapper
```

Restart ComfyUI. The startup log lists `comfyui_clapper`, and after one reload of the ComfyUI browser tab a **Clapper** tab appears in its left sidebar.

## First run

1. Open **`http://<comfyui-host>:8188/clapper/`**. The page is served by ComfyUI itself, so any device that can reach ComfyUI can open it.
2. The login screen offers **Create the first profile**: choose an id, a name and a password. This first profile gets every project already known to Clapper.
3. More profiles are added on the computer running ComfyUI: `python cli/clapper.py profile add ID --name "Name"`.

Profiles keep projects private inside Clapper only; ComfyUI's own page has no login. Don't expose ComfyUI to the internet (see [SECURITY](SECURITY.md)).

## Quick start (5 minutes, plus render time)

1. **Create a project** on the home page. Give it a name and choose where to save it (default `~/Documents/Clapper-Projects/<name>`).
2. **Add references** (*References*):
   - upload the start image and tick *start image*;
   - upload characters, props or a style sheet with a short `@name` each, and write each one's **definition** ("@hero is a …");
   - optionally add a voice sample.
3. **Write the style text** in the same dialog: the look, format and rules that apply to every scene.
4. **Add scene 1** and write what happens (see the [prompt guide](docs/PROMPT_GUIDE.md)). Or, on the empty storyboard, press **✨ Plan the video with the assistant**.
5. **Render take** and watch its progress in the live bar. Check the style, characters and sound before going further.
6. Add the next scenes, then **Render remaining N scenes**, or render them one by one.
7. Retake anything you don't like, pick the best takes, and **Export**.

## Documentation

| Document | For |
|---|---|
| [User guide](docs/USER_GUIDE.md) | Concepts, every screen and button, common workflows |
| [Prompt guide](docs/PROMPT_GUIDE.md) | How to write scenes, references, dialogue and sound for H3 |
| [Reference](docs/REFERENCE.md) | Settings, command line, HTTP API, project format, assistant setup, internals, troubleshooting |
| [Design](docs/DESIGN.md) | Architecture, decisions, verified results, roadmap |
| [Plan 0.2](docs/PLAN_0.2.md) | Draft plan for the next version |
| [Contributing](CONTRIBUTING.md) | Dev setup, architecture map, project rules, tests, issues and pull requests |
| [Security](SECURITY.md) | What profiles protect, where keys and passwords are stored, reporting a vulnerability |
| [Changelog](CHANGELOG.md) | What changed |

## Help and contributing

- Something not working? Check the [troubleshooting table](docs/REFERENCE.md#troubleshooting) first.
- Still stuck, or found a bug? Open an issue with your ComfyUI version, GPU and the relevant log lines (status pill or profile menu → *ComfyUI log* → *Copy*).
- Security problems: report them privately, as described in [SECURITY](SECURITY.md).
- Contributions are welcome. [CONTRIBUTING](CONTRIBUTING.md) explains the setup, the rules and how to send a pull request.

## Credits and license

Clapper adapts the frame-grid helpers and the masked audio-video continuation from [ComfyUI-H3-Continuum](https://github.com/ukr8b3g-cmyk/ComfyUI-H3-Continuum) (MIT); see [NOTICE](NOTICE). The prompt-writing conventions follow the Continuum author's prompt skill and MiniMax's published H3 prompt guides.

Clapper is released under the [MIT License](LICENSE).

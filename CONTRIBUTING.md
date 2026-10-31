# Contributing to Clapper

Thanks for helping. This page covers the dev setup, how the code is laid out, the rules every change follows, and how to send issues and pull requests. The user-facing docs are in [`docs/`](docs/); [`docs/DESIGN.md`](docs/DESIGN.md) explains why things are built the way they are.

## Dev setup

You need a working ComfyUI with the MiniMax H3 models to render (see [Requirements](README.md#requirements)). The unit tests don't need ComfyUI, a GPU or any models.

```bash
git clone https://github.com/prateeksinghdhillon/clapper.git ~/Documents/Clapper
cd ~/Documents/Clapper
ln -s "$PWD/comfyui_clapper" ~/ComfyUI/custom_nodes/comfyui_clapper   # ComfyUI loads the pack from here

python3 -m venv .venv                     # dev venv for the tests, separate from ComfyUI's
.venv/bin/pip install pytest Pillow
.venv/bin/python -m pytest -q tests
```

While you work:

- **Python changes need a ComfyUI restart**, and only restart when ComfyUI's queue is empty. If ComfyUI runs as a service and you have no sudo, ComfyUI-Manager can restart it in place: `curl -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:8188/v2/manager/reboot`.
- **Files in `comfyui_clapper/web/` only need a page reload.** There is no build step.
- Run `node --check comfyui_clapper/web/app.js` (and the other JS files you touched) to catch syntax errors.
- The startup log should list `comfyui_clapper` without `IMPORT FAILED`. Clapper's own messages are logged under `clapper`.

## Architecture map

| Path | What it does |
|---|---|
| `comfyui_clapper/__init__.py` | Registers the nodes and the routes when ComfyUI loads the pack; outside ComfyUI only `engine/` is used |
| `comfyui_clapper/nodes.py` | The two graph nodes, *Clapper · Scene Conditioning* and *Clapper · Save Take* (the torch side) |
| `comfyui_clapper/h3_temporal.py` | H3 frame-count and latent-grid helpers |
| `comfyui_clapper/api.py` | HTTP routes under `/clapper/` (projects, scenes, takes, export, live, login middleware) |
| `comfyui_clapper/assistant_api.py` | Assistant and GPU routes; runs one assistant turn at a time in a background thread |
| `comfyui_clapper/gpu.py` | Who holds the GPU: H3 or the local assistant model; auto mode ejects the model on every queued ComfyUI job |
| `comfyui_clapper/live.py`, `sysstats.py` | Render progress captured in-process; CPU / RAM / GPU stats |
| `comfyui_clapper/engine/` | **Pure Python, no torch**, shared by the server, the CLI and the tests |
| `…/engine/project.py` | Project store, `@name` → `<Picture N>` compile, definitions, badges, take numbering |
| `…/engine/director.py` | Plan, queue, chain, refresh and re-queue takes |
| `…/engine/graph.py` | The ComfyUI API graph for one take |
| `…/engine/export.py` | Export checks and the ffmpeg join |
| `…/engine/importer.py` | Continuum Timeline import, adopting existing take folders |
| `…/engine/settings.py` | Default settings and presets |
| `…/engine/registry.py` | Known projects and save locations (`~/.clapper/`) |
| `…/engine/profiles.py` | Profiles, project ownership, one active profile at a time |
| `…/engine/repeats.py` | Short-window refusal of repeated render / re-queue requests |
| `…/engine/llm.py` | OpenAI-compatible chat client, provider settings, llama-swap load / eject / cache |
| `…/engine/assistant.py` | Assistant instructions, project snapshot, tools, proposals, chats, compaction |
| `…/engine/comfy_client.py` | Minimal client for ComfyUI's `/prompt`, `/queue` and `/history` |
| `comfyui_clapper/web/` | The interface: `index.html`, `style.css`, `app.js`, `assistant.js` (plain JS, no framework) |
| `comfyui_clapper/js/` | The ComfyUI sidebar tab only |
| `cli/clapper.py` | Command line; works on project folders directly and queues in ComfyUI over HTTP |
| `tools/` | `join_check.py` (measure a join), `assistant_eval.py` (score the assistant model) |
| `tests/` | Unit tests for the engine |

Two things to know before touching the server code:

- **Never call ComfyUI's own HTTP API synchronously from an aiohttp handler.** The handler runs on ComfyUI's event loop, so a blocking call to the same server deadlocks it. Wrap such work in `loop.run_in_executor(...)`, as the existing routes do.
- Writes to a project go through the module-level `_lock` in `api.py`, one writer at a time.

## Project rules

- **Defaults stay equal to the proven Continuum draft setup** (`engine/settings.py`: FL2VA int8, Turbo 8-step LoRA at 20 steps, `res_multistep` / `simple`, 0.3 MP, 22-frame masked continuation). Faster or different options are opt-in presets.
- **Prompt parity with Continuum.** With all references active, a project imported from a Continuum Timeline prompt must compile to exactly Continuum's per-chunk prompts. Check this whenever you touch `project.compile_prompt` or `importer`.
- **General-purpose names only.** No project-, brand- or client-specific names, prompts or assets in code, tests or docs. Use neutral examples: `@hero`, `@logo`, a café promo.
- **Credit adapted code.** Code adapted from ComfyUI-H3-Continuum (MIT) or anywhere else is credited in [`NOTICE`](NOTICE) and in the file's docstring.
- **Nothing re-renders silently.** A change that affects other scenes flags them with a badge; the user decides.
- **CHANGELOG for every user-visible change**, under *Unreleased* in [`CHANGELOG.md`](CHANGELOG.md).
- **Keep the docs current.** A new setting, route or command goes into [`docs/REFERENCE.md`](docs/REFERENCE.md); a new screen or button goes into [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md); roadmap statuses in [`docs/DESIGN.md`](docs/DESIGN.md) are updated when an item is built or verified.

## Testing

```bash
.venv/bin/python -m pytest -q tests
```

- Put logic that can be tested without ComfyUI in `engine/`, and test it there. Tests make throwaway projects in `tmp_path`; time-dependent code takes a `clock` argument so tests can move time forward (see `tests/test_profiles.py`, `tests/test_repeats.py`).
- **Assistant unit tests** never call a real model: `tests/test_assistant.py` swaps in a `FakeClient` that plays back scripted replies (text and tool calls) and records the requests. Use it to test new tools, proposal checks, compaction and the prompt the model receives.
- **Real models** are checked with `tools/assistant_eval.py`. It runs fixed tasks (plan, edit, insert, change language) against a throwaway copy of a project with the active provider from `~/.clapper/llm.json`, and scores every scene the model wrote against the prompt guide's rules, with the project's own scenes as the baseline:

  ```bash
  .venv/bin/python tools/assistant_eval.py ~/Documents/Clapper-Projects/cafe \
      --brief "A 15-second vertical café promo with @hero serving coffee" --out eval.md
  ```

  A local model is loaded through llama-swap, so ComfyUI's queue must be empty. Include the report's summary in a pull request that changes the assistant's instructions or tools.
- **Renders** can't be unit-tested. If you change the graph, the nodes, continuation or export, render at least two joined scenes and check the join (`tools/join_check.py TAKE_A TAKE_B OUT_DIR`, run with a Python that has numpy and Pillow, for example ComfyUI's). Say in the pull request what you rendered and on which GPU.

## Coding style

- Match the surrounding code: its naming, comment density and idioms. Keep functions short and plain.
- Python: type hints where they help, standard library first; `engine/` must not import torch or ComfyUI.
- Web: plain JavaScript, no framework and no build step. `app.js` has small helpers (`h()` builds elements, `call()` talks to the API, `guard()` shows errors, `once()` makes a button run one request at a time); use them.
- Comments explain constraints the code cannot show (why a lock is needed, why a number is what it is), not what the next line does.
- User-facing text is plain and short, and says what to do next.

## Issues

Before opening an issue, check the [troubleshooting table](docs/REFERENCE.md#troubleshooting). A good bug report includes:

- What you did, what you expected, and what happened.
- Clapper version (the CHANGELOG entry or the commit), ComfyUI version, OS, GPU and VRAM.
- The relevant log lines: ComfyUI's terminal output, or **ComfyUI log** in Clapper's status panel or profile menu (it has a *Copy* button). For browser problems, the browser's console.
- For render problems: the take's `meta.json` (it holds the exact prompt, settings and seed). Remove anything private first.
- For assistant problems: which provider and model, and whether it was the local model or an online service.

## Pull requests

- Keep a pull request to one change, and explain the why in its description.
- Run the tests and `node --check` on the JS files you changed; say how you tested anything the tests don't cover.
- Update the CHANGELOG and the docs in the same pull request.
- Commit messages: a short summary line in plain words, in the imperative ("Refuse repeated renders for 5 seconds"), then details if needed. When a change was written together with someone else — including an AI coding assistant — end the message with a `Co-Authored-By:` line.

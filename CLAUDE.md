# Clapper — notes for development

Scene-and-take directing for MiniMax H3 in ComfyUI. User docs: `README.md`, `docs/USER_GUIDE.md`, `docs/PROMPT_GUIDE.md`, `docs/REFERENCE.md`; architecture, verified results and roadmap: `docs/DESIGN.md` (keep its roadmap statuses current); `CHANGELOG.md` for every user-visible change. Contributor guide: `CONTRIBUTING.md` (keep it in step with these notes); `SECURITY.md`.

This repository is general-purpose: no project-, brand- or client-specific names, prompts or assets in code, tests or docs. Examples use neutral names (`@hero`, `@logo`, a café promo).

## Layout

| Path | What |
|---|---|
| `comfyui_clapper/` | ComfyUI node pack; symlink it into `ComfyUI/custom_nodes/comfyui_clapper` |
| `…/nodes.py` | `ClapperSceneConditioning`, `ClapperSaveTake` (torch/ComfyUI side) |
| `…/engine/` | Pure Python, no torch: settings, graph, project (incl. the asset library), director, export, media (ffmpeg/ffprobe helpers), importer, registry, profiles, repeats, llm, assistant, comfy_client |
| `…/api.py`, `assistant_api.py`, `gpu.py`, `live.py`, `sysstats.py` | Routes under `/clapper/`, assistant + GPU routes, GPU sharing (H3 vs local LLM), progress capture, machine stats |
| `…/web/` | Interface (no build step): `app.js` (shell, Scenes, settings, upscale), `studio.js` (Studio and Finish workspaces), `assistant.js`; `…/js/` = ComfyUI sidebar tab only |
| `cli/clapper.py` | CLI |
| `tools/dev_server.py`, `tools/join_check.py`, `tools/assistant_eval.py` | Run the interface/API outside ComfyUI for UI work and browser tests (scratch `HOME`); measure a join; score the assistant |
| `tests/` | `.venv/bin/python -m pytest -q tests` (`.venv` is a dev venv with pytest + Pillow, not ComfyUI's) |

## Rules

- Python changes need a ComfyUI restart; `web/*` needs only a page reload. Restart only when ComfyUI's queue is empty. Without sudo, restart via ComfyUI-Manager: `curl -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:8188/v2/manager/reboot` (re-execs in place, stays under a systemd service).
- Never call ComfyUI's own HTTP API synchronously from an aiohttp handler; use `run_in_executor` (deadlock otherwise).
- Defaults must stay equal to the proven Continuum draft setup (see `engine/settings.py`); faster options are opt-in presets.
- Prompt parity: with all references active, a project imported from a Continuum Timeline prompt must compile to exactly Continuum's per-chunk prompts. Check after touching `project.compile_prompt` or `importer`.
- Code adapted from ComfyUI-H3-Continuum (MIT) is credited in `NOTICE`.
- Commit messages end with the Co-Authored-By line.

- Interface changes: test them in a real browser at laptop, tablet (≈820 px) and phone (≈390 px) widths. While someone is logged in to Clapper, use `tools/dev_server.py` with a scratch `HOME` instead of a second login (one profile at a time).

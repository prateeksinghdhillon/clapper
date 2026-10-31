#!/usr/bin/env python3
"""Measure how well the assistant's model follows Clapper's workflow and the prompt guide.

Runs fixed tasks on a throwaway copy of a project (the original is only read) and scores every
scene text the model proposes against rules taken from docs/PROMPT_GUIDE.md. The project's own
scenes are scored the same way as a baseline.

  .venv/bin/python tools/assistant_eval.py PROJECT --brief "…" [--language L] [--edit-language L]
      [--thinking off|auto|N] [--budget N] [--sampling JSON] [--repeat N] [--out report.md]

Tasks: plan (style text, definitions, all scenes, from an empty storyboard) → edit scene 2 →
insert a scene after scene 1 → change the narration language of scene 1. Proposals are applied
between tasks. Uses the active provider in ~/.clapper/llm.json (thinking and sampling can be
overridden per run); a local model is loaded through llama-swap (ComfyUI must be idle: its
models are unloaded first).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from comfyui_clapper.engine import assistant, llm  # noqa: E402
from comfyui_clapper.engine.project import Project, ProjectError  # noqa: E402

FIELDS = ("integrated_multimodal_description:", "overall_soundscape:", "non_diegetic_music:")
CAMERA = re.compile(r"\b(close-up|close up|medium shot|wide shot|push-in|push in|pull-back|pull back|pans?|panning|tilts?|dolly|"
                    r"orbits?|tracking|follows?|zoom|eye-level|low angle|overhead|static|holds?)\b", re.I)
SPEAKER = re.compile(r"(says|asks|whispers|shouts|exclaims|narrator|\(S\d\)|replies|calls)", re.I)
DEVANAGARI = re.compile(r"[\u0900-\u097F]")
DTAG = re.compile(r"<\s*/?\s*[dD]\s*/?\s*>")
STOP = set("the a an and of to in on at with from is are as its it his her their then while into onto over this that "
           "camera scene video shot one".split())


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in STOP}


def dialogue(text: str) -> tuple[list[tuple[int, str]], bool]:
    """Spoken lines as (position, words) and whether every <d> is closed by </d>. Tolerant: a <d> while a
    line is open ends it, and an unclosed line runs to the end of its text line, so slips still count as speech."""
    lines, well_formed, opened = [], True, None
    for m in DTAG.finditer(text):
        tag, closer = m.group(), "/" in m.group()
        well_formed &= tag in ("<d>", "</d>")
        if opened is None:
            if closer:  # a closing tag with nothing open
                well_formed = False
            else:
                opened = m.end()
            continue
        well_formed &= closer  # <d> used to close a line
        lines.append((opened, text[opened:m.start()]))
        opened = None
    if opened is not None:
        well_formed = False
        end = text.find("\n", opened)
        lines.append((opened, text[opened:end if end >= 0 else len(text)]))
    return lines, well_formed


def score_scene(project: Project, scene: dict, prev: dict | None, *, language: str | None = None) -> dict:
    """Hard rules (the guide says so; breaking them hurts the render) and soft ones (good practice)."""
    t = scene.get("text") or ""
    seconds = scene.get("seconds") or 5
    hard, soft, notes = {}, {}, []
    positions = [t.find(f) for f in FIELDS]
    hard["three fields, once each, in order"] = all(t.count(f) == 1 for f in FIELDS) and positions == sorted(positions) and positions[0] >= 0
    imd = t[positions[0]:positions[1]] if positions[0] >= 0 and positions[1] > positions[0] else t
    sound = t[positions[1]:positions[2]] if positions[1] >= 0 and positions[2] > positions[1] else ""
    hard["[Shot 1] in the description"] = "[Shot 1]" in imd
    try:
        project.compile_prompt(scene)
        hard["@names exist and are active"] = "<Picture" not in t and "<Audio" not in t
    except ProjectError as exc:
        hard["@names exist and are active"] = False
        notes.append(str(exc))
    spoken_lines, well_formed = dialogue(t)
    lines = [d for _, d in spoken_lines]
    hard["dialogue tags well-formed (<d>…</d>)"] = well_formed
    hard["dialogue has [Language] tag and a speaker"] = all(
        re.match(r"\s*\[[A-Za-z ]+\]", d) and SPEAKER.search(t[max(0, pos - 170):pos]) for pos, d in spoken_lines)
    spoken = sum(len(re.sub(r"\[[^\]]*\]", "", d).split()) for d in lines)
    hard["speech fits the length (≤ 4.5 words/s)"] = spoken <= 4.5 * seconds
    visible = DTAG.sub(" ", imd)
    for d in lines:  # sounds may be quoted; spoken words are fine
        visible = visible.replace(d, " ")
    defined = " ".join(project.data["library"]["items"].get(n, {}).get("definition", "") for n in project.scene_refs(scene))
    quoted = [q for q in re.findall(r"[“\"]([^”\"]{2,})[”\"]", visible) if q not in defined]  # an exact logo word its reference spells is allowed
    asks = re.search(r"\b(sign|screen|label|banner|paper|board|menu|card|caption|title)s?\s+(reads|says|shows the (word|text))|\blettering\b|"
                     r"\bwritten\b|\btext that says\b", visible, re.I)
    hard["no readable text requested"] = not quoted and not asks
    hard["no leaked markup"] = "</parameter" not in t and "<tool_call" not in t
    if language:
        tagged = [d for d in lines if d.strip().lower().startswith(f"[{language.lower()}]")]
        hard[f"narration in {language}, native script"] = bool(tagged) and all(
            DEVANAGARI.search(d) for d in tagged) if language.lower() == "hindi" else bool(tagged)
    soft["camera: shot size / movement"] = bool(CAMERA.search(imd))
    soft["timing cues (at about… / final second)"] = bool(re.search(r"at about|in the (very )?(first|final|last) (half )?second", imd, re.I))
    soft["clear sound early"] = bool(re.search(r"(very )?first (half )?second|at the start|opens with|immediately", sound, re.I))
    soft["has narration"] = bool(lines)
    if prev is not None and scene.get("join") != "cut":
        prev_text = prev.get("text") or ""
        prev_imd = prev_text.split("overall_soundscape:")[0]
        last = re.split(r"(?<=[.!?])\s+", prev_imd.strip())[-2:]
        first = re.split(r"(?<=[.!?])\s+", imd.replace("integrated_multimodal_description:", "").replace("[Shot 1]", "").strip())[:2]
        before_fields = t[:positions[0]] if positions[0] > 0 else ""
        overlap = _words(" ".join(last)) & _words(" ".join(first) + " " + before_fields)
        soft["starts where the previous scene ends"] = len(overlap) >= 2 or bool(re.match(r"\s*(the camera is on|continuing|still)", before_fields + " ".join(first), re.I))
        if not soft["starts where the previous scene ends"]:
            notes.append("no shared state with the previous scene's ending")
    return {"hard": hard, "soft": soft, "notes": notes, "spoken_words": spoken}


def score_style(style: str) -> dict:
    return {"states the format (vertical/9:16…)": bool(re.search(r"9:16|16:9|vertical|horizontal|square", style, re.I)),
            "has subject_definitions:": "subject_definitions:" in style,
            "no dialogue in the style text": "<d>" not in style,
            "defines the voice": bool(re.search(r"\(S\d\)|narrator|voice", style, re.I)),
            "no-readable-writing rule": bool(re.search(r"no readable|without (readable )?(writing|text)|no (writing|text)", style, re.I))}


def duplicates(scenes: list[dict]) -> list[str]:
    """Scenes whose text (nearly) repeats an earlier scene's, as 'j≈i'."""
    out = []
    for j, b in enumerate(scenes):
        i = next((i for i, a in enumerate(scenes[:j]) if assistant.similarity(a.get("text") or "", b.get("text") or "") >= assistant.NEAR_DUPLICATE), None)
        if i is not None:
            out.append(f"{j + 1}≈{i + 1}")
    return out


def _pct(results: list[dict], kind: str) -> tuple[int, int]:
    ok = sum(v for r in results for v in r[kind].values())
    return ok, sum(len(r[kind]) for r in results)


class Runner:
    def __init__(self, root: Path, provider: dict, thinking):
        self.root, self.provider, self.thinking = root, provider, thinking
        self.chat = None

    def _before(self):
        if self.provider.get("kind") != "local":
            return
        runner = llm.LocalRunner(self.provider)
        if runner.loaded():
            return
        try:  # free ComfyUI's models first (only if its queue is empty)
            base = "http://127.0.0.1:8188"
            queue = json.load(urllib.request.urlopen(base + "/queue", timeout=5))
            if queue["queue_running"] or queue["queue_pending"]:
                raise SystemExit("ComfyUI is rendering; run the evaluation when its queue is empty")
            urllib.request.urlopen(urllib.request.Request(base + "/free", json.dumps({"unload_models": True, "free_memory": True}).encode(),
                                                          {"Content-Type": "application/json"}), timeout=10)
            time.sleep(3)
        except OSError:
            pass
        runner.load()

    def turn(self, text: str) -> dict:
        project = Project.open(self.root)
        if self.chat is None:
            self.chat = assistant.new_chat(project)

        def write(fn):
            fn(Project.open(self.root))

        agent = assistant.Assistant(self.provider, open=lambda: Project.open(self.root), write=write, before_request=self._before)
        start = len(self.chat["messages"])
        before = set(self.chat["proposals"])
        t0 = time.time()
        agent.run_turn(self.chat, text, thinking=self.thinking)
        new_msgs = self.chat["messages"][start:]
        calls = [c["name"] for m in new_msgs if m["role"] == "assistant" for c in m.get("tool_calls") or []]
        errors = [m["text"][:300] for m in new_msgs if m["role"] == "tool" and m["text"].startswith("ERROR")]
        repeats = [m for m in new_msgs if m["role"] == "tool" and "already proposed exactly this" in m["text"]]
        props = [self.chat["proposals"][p] for p in self.chat["proposals"] if p not in before and self.chat["proposals"][p]["status"] == "pending"]
        replaced = sum(self.chat["proposals"][p]["status"] == "replaced" for p in self.chat["proposals"] if p not in before)
        reply = next((m.get("text") for m in reversed(new_msgs) if m["role"] == "assistant" and m.get("text")), "")
        return {"seconds": round(time.time() - t0, 1), "requests": sum(m["role"] == "assistant" for m in new_msgs),
                "reasoning": sum(len(m.get("reasoning") or "") for m in new_msgs if m["role"] == "assistant"),
                "calls": calls, "errors": errors, "repeats": len(repeats), "replaced": replaced, "proposals": props, "reply": reply}

    def apply(self, props: list[dict]) -> None:
        for p in props:
            if p["tool"] == "render_scenes":
                continue
            project = Project.open(self.root)
            try:
                assistant.apply_proposal(project, p)
                p["status"] = "applied"
            except ProjectError as exc:
                p["status"] = f"failed: {exc}"


def run_once(source: Project, provider: dict, thinking, args) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="clapper-eval-"))
    root = tmp / "project"
    shutil.copytree(source.root, root, ignore=shutil.ignore_patterns("takes", "exports", "history", "chat"))
    for sub in ("takes", "exports", "history"):
        (root / sub).mkdir(exist_ok=True)
    blank = Project.open(root)
    blank.data.update(scenes=[], takes={}, style="", notes="")
    blank.save()
    run = Runner(root, provider, thinking)
    tasks = []

    def task(name, text, expect):
        result = run.turn(text)
        ok = expect(result)
        run.apply(result["proposals"])
        tasks.append({"name": name, "prompt": text, "expected_tools": bool(ok), **{k: v for k, v in result.items() if k != "proposals"},
                      "proposals": [{"summary": p["summary"], "tool": p["tool"], "status": p.get("status")} for p in result["proposals"]]})
        print(f"  {name}: {result['seconds']}s, {result['requests']} requests, calls {result['calls']}, errors {len(result['errors'])}", flush=True)

    t0 = time.time()
    task("plan", args.brief, lambda r: any(p["tool"] == "add_scenes" for p in r["proposals"]))
    project = Project.open(root)
    n_plan = len(project.scenes)
    task("edit", "In scene 2, add one short spoken line by the narrator and end the scene with a slow push-in.",
         lambda r: len(project.scenes) > 1 and [p["tool"] for p in r["proposals"]] == ["edit_scene"]
         and r["proposals"][0]["args"]["scene"] == project.scenes[1]["id"])
    task("insert", "Insert one new 5-second scene between scene 1 and scene 2 that bridges them smoothly.",
         lambda r: project.scenes and [p["tool"] for p in r["proposals"]] == ["add_scenes"] and r["proposals"][0]["args"]["after"] == project.scenes[0]["id"]
         and len(r["proposals"][0]["args"]["scenes"]) == 1)
    project = Project.open(root)
    done = bool(project.scenes) and score_scene(project, project.scenes[0], None, language=args.edit_language)["hard"].get(
        f"narration in {args.edit_language}, native script", False)  # then "nothing to change" is the right answer too
    task("language", f"Change the narration in scene 1 to {args.edit_language}, written in its own script.",
         lambda r: project.scenes and ((done and not r["proposals"]) or
                                       ([p["tool"] for p in r["proposals"]] == ["edit_scene"] and r["proposals"][0]["args"]["scene"] == project.scenes[0]["id"])))
    seconds = round(time.time() - t0, 1)
    final = Project.open(root)
    scores = [score_scene(final, s, final.scenes[i - 1] if i else None, language=args.edit_language if i == 0 else args.language)
              for i, s in enumerate(final.scenes)]
    out = {"tasks": tasks, "scores": scores, "n_plan": n_plan, "n_final": len(final.scenes), "duplicates": duplicates(final.scenes),
           "seconds": seconds, "style": final.data.get("style", ""), "style_score": score_style(final.data.get("style", "")),
           "scenes": [{k: s.get(k) for k in ("title", "seconds", "join", "refs", "text")} for s in final.scenes]}
    shutil.rmtree(tmp, ignore_errors=True)
    return out


def _rule_rows(runs: list[dict], base_scores: list[dict]) -> list[str]:
    gen = [r for run in runs for r in run["scores"]]
    rows = ["| Rule | Generated | Baseline |", "|---|---|---|"]
    rules = list(dict.fromkeys(k for r in gen + base_scores for kind in ("hard", "soft") for k in r[kind]))
    for rule in rules:
        def cell(scores):
            vals = [r[kind][rule] for r in scores for kind in ("hard", "soft") if rule in r[kind]]
            return f"{sum(vals)}/{len(vals)}" if vals else "—"
        hard = any(rule in r["hard"] for r in gen + base_scores)
        rows.append(f"| {'**' + rule + '**' if hard else rule} | {cell(gen)} | {cell(base_scores)} |")
    gh, gt = _pct(gen, "hard")
    bh, bt = _pct(base_scores, "hard")
    rows.append(f"| **all hard rules** | {gh}/{gt} ({100 * gh / max(1, gt):.0f}%) | {bh}/{bt} ({100 * bh / max(1, bt):.0f}%) |")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--brief", required=True, help="the video idea for the planning task")
    ap.add_argument("--language", help="narration language the brief asks for (checked in every scene)")
    ap.add_argument("--edit-language", default="Spanish", help="language for the 'change the narration language' task")
    ap.add_argument("--scenes", type=int, help="scene count the brief asks for (default: read '<N> scenes' from the brief)")
    ap.add_argument("--thinking", nargs="?", const="2048", default="off",
                    help="off, auto (think only for planning, budget --budget) or a token budget for every turn; bare flag = 2048")
    ap.add_argument("--budget", type=int, help="thinking budget used by --thinking auto (default: the provider's)")
    ap.add_argument("--sampling", help='JSON sampling parameters for every request, e.g. \'{"temperature": 0.7, "top_k": 20}\'')
    ap.add_argument("--sampling-thinking", help="JSON sampling parameters added when thinking")
    ap.add_argument("--repeat", type=int, default=1, help="run the tasks N times and aggregate")
    ap.add_argument("--label", help="name of this configuration in the report")
    ap.add_argument("--out", default="assistant-eval.md")
    args = ap.parse_args()

    settings = llm.load_settings()
    provider = dict(settings["providers"][settings["active"]])
    if args.sampling is not None:
        provider["sampling"] = json.loads(args.sampling)
    if args.sampling_thinking is not None:
        provider["sampling_thinking"] = json.loads(args.sampling_thinking)
    if args.budget:
        provider["thinking_budget"] = args.budget
    thinking = llm._thinking_mode(args.thinking)
    expected = args.scenes or (int(m.group(1)) if (m := re.search(r"\b(\d+) scenes\b", args.brief)) else None)
    label = args.label or f"thinking {args.thinking}" + (f" (budget {provider.get('thinking_budget')})" if thinking == "auto" else "") + \
        (f", sampling {json.dumps(provider.get('sampling'))}" if provider.get("sampling") else "")

    source = Project.open(args.project)
    base_scores = [score_scene(source, s, source.scenes[i - 1] if i else None, language=args.language) for i, s in enumerate(source.scenes)]
    base_style = score_style(source.data.get("style", ""))
    runs = []
    for k in range(args.repeat):
        print(f"run {k + 1}/{args.repeat} — {label}", flush=True)
        runs.append(run_once(source, provider, thinking, args))

    lines = [f"# Assistant evaluation — {provider.get('name') or provider.get('model')} — {label}", "",
             f"Planning brief: {args.brief}", "", f"Runs: {len(runs)}", "", "## Summary", "",
             "| Run | Hard rules | Soft rules | Right tool | Scenes plan / final (asked) | Duplicates | Tool errors | Reasoning chars | Time |",
             "|---|---|---|---|---|---|---|---|---|"]
    for k, r in enumerate(runs, 1):
        h, ht = _pct(r["scores"], "hard")
        s, st = _pct(r["scores"], "soft")
        r["hard_pct"] = 100 * h / max(1, ht)
        r["soft_pct"] = 100 * s / max(1, st)
        r["right"] = sum(t["expected_tools"] for t in r["tasks"])
        asked = f" ({expected} / {expected + 1})" if expected else ""
        lines.append(f"| {k} | {h}/{ht} ({r['hard_pct']:.0f}%) | {s}/{st} ({r['soft_pct']:.0f}%) | {r['right']}/{len(r['tasks'])} | "
                     f"{r['n_plan']} / {r['n_final']}{asked} | {len(r['duplicates'])}{' (' + ', '.join(r['duplicates']) + ')' if r['duplicates'] else ''} | "
                     f"{sum(len(t['errors']) for t in r['tasks'])} | {sum(t['reasoning'] for t in r['tasks'])} | {r['seconds']:.0f} s |")
    if len(runs) > 1:
        n = len(runs)
        lines.append(f"| **mean** | {sum(r['hard_pct'] for r in runs) / n:.0f}% | {sum(r['soft_pct'] for r in runs) / n:.0f}% | "
                     f"{sum(r['right'] for r in runs) / n:.1f}/4 | {sum(r['n_plan'] for r in runs) / n:.1f} / {sum(r['n_final'] for r in runs) / n:.1f} | "
                     f"{sum(len(r['duplicates']) for r in runs) / n:.1f} | {sum(len(t['errors']) for r in runs for t in r['tasks']) / n:.1f} | "
                     f"{sum(t['reasoning'] for r in runs for t in r['tasks']) / n:.0f} | {sum(r['seconds'] for r in runs) / n:.0f} s |")
    lines += ["", "## Rules — all runs vs. the project's own scenes", "", *_rule_rows(runs, base_scores), "",
              "Bold rules are hard (breaking them hurts the render); the others are good practice from the guide.", ""]
    for k, r in enumerate(runs, 1):
        lines += [f"## Run {k}", "", "| Task | Time | Requests | Reasoning chars | Tool calls | Tool errors | Repeats | Replaced | Right tool | Proposals |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for t in r["tasks"]:
            lines.append(f"| {t['name']} | {t['seconds']} s | {t['requests']} | {t['reasoning']} | {', '.join(t['calls']) or '—'} | {len(t['errors'])} | "
                         f"{t['repeats']} | {t['replaced']} | {'yes' if t['expected_tools'] else 'NO'} | "
                         f"{'; '.join(p['summary'] + ' (' + str(p['status'])[:160] + ')' for p in t['proposals']) or '—'} |")
        lines += ["", f"Scenes after planning: {r['n_plan']}{f' (asked for {expected})' if expected else ''}; after all tasks: {r['n_final']}"
                      f"{f' (expected {expected + 1})' if expected else ''}; duplicates: {', '.join(r['duplicates']) or 'none'}.", ""]
        lines += ["### Style text", "", "| Check | Generated | Baseline |", "|---|---|---|"]
        lines += [f"| {c} | {'yes' if r['style_score'][c] else 'no'} | {'yes' if base_style[c] else 'no'} |" for c in r["style_score"]]
        lines += ["", "```", r["style"], "```", "", "### Scenes", ""]
        for i, (s, sc) in enumerate(zip(r["scenes"], r["scores"]), 1):
            broken = [key for key, v in sc["hard"].items() if not v]
            lines += [f"#### Scene {i}: {s.get('title') or ''} ({s.get('seconds') or 'default'} s, {s['join']}, refs {s.get('refs')})", "",
                      "Breaks: " + (", ".join(broken) if broken else "none") + (f" — {'; '.join(sc['notes'])}" if sc["notes"] else ""), "",
                      "```", s.get("text") or "", "```", ""]
        lines += ["### Replies", ""]
        lines += [f"- **{t['name']}**: {t['reply'][:600]}" + (f"\n  - tool errors: {' | '.join(e[:200] for e in t['errors'])}" if t["errors"] else "")
                  for t in r["tasks"]]
        lines.append("")
    Path(args.out).write_text("\n".join(lines), encoding="utf-8")
    Path(args.out).with_suffix(".json").write_text(json.dumps({"label": label, "runs": runs, "baseline": base_scores},
                                                              indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"report: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

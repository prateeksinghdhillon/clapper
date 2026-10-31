import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from comfyui_clapper.engine import tools  # noqa: E402
from comfyui_clapper.engine.project import Project, ProjectError  # noqa: E402

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x10\x00\x00\x00\x10\x08\x02\x00\x00\x00\x90\x91h6"
       b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82")


class FakeComfy:
    def __init__(self):
        self.graphs, self.history_map, self.running, self.pending = [], {}, set(), set()

    def submit(self, graph):
        self.graphs.append(graph)
        pid = f"p{len(self.graphs)}"
        self.pending.add(pid)
        return pid

    def queue_state(self):
        return {"queue_running": [[0, p] for p in self.running], "queue_pending": [[1, p] for p in self.pending]}

    def history(self, pid):
        return self.history_map.get(pid)

    def finish(self, pid, ok=True):
        self.pending.discard(pid); self.running.discard(pid)
        self.history_map[pid] = {"status": {"status_str": "success" if ok else "error", "completed": ok, "messages":
                                 [["execution_start", {"timestamp": 1000}], ["execution_success", {"timestamp": 13500}]] if ok
                                 else [["execution_error", {"exception_message": "boom"}]]}}


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "anime", "Anime short")
    (tmp_path / "girl.png").write_bytes(PNG)
    (tmp_path / "voice.wav").write_bytes(b"RIFF0000WAVE")
    p.girl = p.add_asset(tmp_path / "girl.png", role="character", title="Girl")
    p.voice = p.add_asset(tmp_path / "voice.wav", role="voice", title="Voice")
    return p


def nodes_of(graph, cls):
    return [n for n in graph.values() if n["class_type"] == cls]


def test_image_tool_text_only(project):
    c = FakeComfy()
    jobs = tools.queue_tool(project, c, "image", {"prompt": "an anime girl", "aspect": "9:16"}, count=2, seed=10)
    assert [j["seed"] for j in jobs] == [10, 11] and len(c.graphs) == 2
    g = c.graphs[0]
    assert nodes_of(g, "EmptyFlux2LatentImage")[0]["inputs"]["width"] == 768
    assert not nodes_of(g, "ReferenceLatent") and not nodes_of(g, "LoraLoaderModelOnly")
    assert nodes_of(g, "ClapperSaveImages")[0]["inputs"]["out_dir"].endswith(f"jobs/{jobs[0]['id']}")
    assert jobs[0]["role"] == "character"


def test_image_tool_with_reference_uses_lora_and_latents(project):
    c = FakeComfy()
    tools.queue_tool(project, c, "image", {"prompt": "the same girl, sitting", "images": [project.girl["id"]]}, seed=1)
    g = c.graphs[0]
    assert len(nodes_of(g, "ReferenceLatent")) == 1 and nodes_of(g, "LoraLoaderModelOnly")[0]["inputs"]["strength_model"] == 0.35
    assert nodes_of(g, "ClapperLoadImage")[0]["inputs"]["path"] == str(project.root.resolve() / project.girl["file"])


def test_inputs_are_checked(project):
    c = FakeComfy()
    with pytest.raises(ProjectError, match="required"):
        tools.queue_tool(project, c, "image", {"prompt": " "})
    with pytest.raises(ProjectError, match="not an image"):
        tools.queue_tool(project, c, "image", {"prompt": "x", "images": [project.voice["id"]]})
    with pytest.raises(ProjectError, match="choose one of"):
        tools.queue_tool(project, c, "image", {"prompt": "x", "aspect": "5:1"})
    with pytest.raises(ProjectError, match="required"):
        tools.queue_tool(project, c, "speech", {"text": "hi"})
    assert not c.graphs


def test_speech_tool(project):
    c = FakeComfy()
    tools.queue_tool(project, c, "speech", {"text": "You came back.", "voice": project.voice["id"], "language": "English"}, seed=5)
    tts = nodes_of(c.graphs[0], "FB_Qwen3TTSVoiceClone")[0]["inputs"]
    assert tts["target_text"] == "You came back." and tts["x_vector_only"] is True and tts["language"] == "English"
    tools.queue_tool(project, c, "speech", {"text": "Again.", "voice": project.voice["id"], "voice_text": "hello there"}, seed=5)
    assert nodes_of(c.graphs[1], "FB_Qwen3TTSVoiceClone")[0]["inputs"]["x_vector_only"] is False


def test_collect_moves_results_into_library(project):
    c = FakeComfy()
    job = tools.queue_tool(project, c, "image", {"prompt": "the same girl at night", "images": [project.girl["id"]]}, seed=3)[0]
    assert tools.collect(project, c) == []          # still waiting: nothing changes
    out = project.root / job["dir"]
    out.mkdir(parents=True)
    (out / "image_01.png").write_bytes(PNG)
    c.finish(job["prompt_id"])
    assert tools.collect(project, c) == [job["id"]]
    done = project.data["jobs"][job["id"]]
    assert done["status"] == "done" and done["seconds"] == 12.5 and len(done["outputs"]) == 1
    asset = project.assets[done["outputs"][0]]
    assert asset["role"] == "character" and asset["parent"] == project.girl["id"]
    assert asset["origin"]["tool"] == "image" and asset["origin"]["seed"] == 3 and asset["title"].startswith("the same girl")
    assert not out.exists()
    tools.dismiss(project, job["id"])
    assert job["id"] not in project.data["jobs"] and asset["id"] in project.assets


def test_collect_failed_and_lost(project):
    c = FakeComfy()
    a, b = tools.queue_tool(project, c, "image", {"prompt": "x"}, count=2, seed=1)
    c.finish(a["prompt_id"], ok=False)
    c.pending.discard(b["prompt_id"])                # vanished from the queue without a result
    tools.collect(project, c)
    assert project.data["jobs"][a["id"]]["status"] == "failed" and "boom" in project.data["jobs"][a["id"]]["error"]
    assert project.data["jobs"][b["id"]]["status"] == "interrupted"
    with pytest.raises(ProjectError, match="cancel"):
        c2 = FakeComfy(); j = tools.queue_tool(project, c2, "image", {"prompt": "y"})[0]; tools.dismiss(project, j["id"])


def test_user_tool_from_folder(project, tmp_path):
    folder = tmp_path / "tools" / "upscale"
    folder.mkdir(parents=True)
    (folder / "workflow.json").write_text(json.dumps({
        "1": {"class_type": "ClapperLoadImage", "inputs": {"path": ""}},
        "2": {"class_type": "SomeUpscaler", "inputs": {"image": ["1", 0], "scale": 2, "seed": 0}},
        "3": {"class_type": "ClapperSaveImages", "inputs": {"images": ["2", 0], "out_dir": "", "prefix": "image"}}}))
    (folder / "tool.json").write_text(json.dumps({
        "title": "Upscale ×2", "kind": "image", "output_node": "3", "seed_node": "2", "default_role": "frame",
        "fields": [{"key": "image", "label": "Image", "type": "image", "node": "1", "input": "path", "required": True},
                   {"key": "scale", "label": "Scale", "type": "select", "options": [2, 4], "default": 2, "node": "2", "input": "scale"}]}))
    (tmp_path / "tools" / "broken").mkdir()
    found, problems = tools.all_tools(tmp_path / "tools")
    assert [t.id for t in found] == ["image", "speech", "user:upscale"] and problems and "broken" in problems[0]
    assert "node" not in found[2].describe()["fields"][0]
    c = FakeComfy()
    job = tools.queue_tool(project, c, "user:upscale", {"image": project.girl["id"], "scale": 4}, seed=9,
                           user_dir=tmp_path / "tools")[0]
    g = c.graphs[0]
    assert g["1"]["inputs"]["path"].endswith(project.girl["file"]) and g["2"]["inputs"] == {"image": ["1", 0], "scale": 4, "seed": 9}
    assert g["3"]["inputs"]["out_dir"].endswith(job["dir"]) and job["role"] == "frame"


def test_availability():
    info = {"UNETLoader": {"input": {"required": {"unet_name": [["flux-2-klein-9b-fp8.safetensors"]]}}},
            "CLIPLoader": {"input": {"required": {"clip_name": [["other.safetensors"]]}}},
            "VAELoader": {"input": {"required": {"vae_name": [["flux2-vae.safetensors"]]}}}}
    have = set(info) | {"EmptyFlux2LatentImage", "Flux2Scheduler", "ReferenceLatent", "ClapperLoadImage", "ClapperSaveImages"}
    a = tools.availability([tools.IMAGE_TOOL, tools.SPEECH_TOOL], lambda n: info.get(n) or ({"input": {}} if n in have else {}))
    assert a["image"] == {"ok": False, "missing": ["model qwen_3_8b_fp8mixed.safetensors"]}
    assert not a["speech"]["ok"] and "node FB_Qwen3TTSVoiceClone" in a["speech"]["missing"]


def test_batch_titles_are_numbered(project):
    c = FakeComfy()
    jobs = tools.queue_tool(project, c, "image", {"prompt": "x"}, count=3, title="Hero waving", seed=1)
    assert [j["title"] for j in jobs] == ["Hero waving 1", "Hero waving 2", "Hero waving 3"]
    assert tools.queue_tool(project, c, "image", {"prompt": "x"}, title="Solo", seed=1)[0]["title"] == "Solo"


def test_timing_from_takes(project):
    from comfyui_clapper.engine import estimates
    t = estimates.timing(project)
    assert t["ref2va"]["measured_here"] is False and t["fl2va"]["s_per_step"] == 13.6
    s = project.add_scene(text="x")
    for steps, secs in ((4, 120.0), (4, 110.0)):
        take = project.new_take(s["id"], seed=1)
        project.update_take(take["id"], status="done", steps=steps, render_seconds=secs, s_per_it=18.0, model="ref2va",
                            width=416, height=736, frames=124)
    t = estimates.timing(project)
    assert t["ref2va"]["takes"] == 2 and t["ref2va"]["s_per_step"] == 18.0 and t["ref2va"]["overhead"] == 43.0
    assert round(estimates.estimate_seconds(t["ref2va"], steps=4, width=416, height=736, frames=124)) == 115

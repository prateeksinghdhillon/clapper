import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from comfyui_clapper.engine import export, tools, upscale  # noqa: E402
from comfyui_clapper.engine.project import Project, ProjectError  # noqa: E402
from test_tools import FakeComfy  # noqa: E402


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "short", "Short")
    s1 = p.add_scene(text="one")
    s2 = p.add_scene(text="two")
    for scene, ctx in ((s1, 0), (s2, 22)):
        t = p.new_take(scene["id"], seed=1, context_frames=ctx, join="continue" if ctx else "cut")
        d = p.root / t["dir"]
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({"frames": 124 + ctx, "width": 416, "height": 736, "prompt": "p",
                                                 "spec": {"ref_images": [], "continue_from": {"take_dir": "x"} if ctx else None},
                                                 "settings": {}}))
        (d / "latent.safetensors").write_bytes(b"x")
        p.update_take(t["id"], status="done", width=416, height=736, frames=124 + ctx)
        p.select_take(scene["id"], t["id"])
    return p


def takes(p):
    return [p.data["takes"][s["selected_take"]] for s in p.scenes]


def test_target_size():
    assert upscale.target_size(416, 736, "1080p") == (1080, 1910)
    assert upscale.target_size(736, 416, "720p") == (1274, 720)
    with pytest.raises(ProjectError, match="already larger"):
        upscale.target_size(1920, 1080, "720p")


def test_sharpen_graph_keeps_sound(project):
    c = FakeComfy()
    t = takes(project)[0]
    job = upscale.queue_upscale(project, c, t["id"], method="sharpen", size="1080p")
    g = c.graphs[0]
    assert g["2"]["inputs"]["model_name"] == "RealESRGAN_x2plus.pth"
    assert (g["4"]["inputs"]["width"], g["4"]["inputs"]["height"]) == (1080, 1910)
    assert g["5"]["inputs"]["audio_path"].endswith(f"{t['dir']}/audio.wav") and job["out"].endswith("upscaled/sharpen-1080p.mp4")
    with pytest.raises(ProjectError, match="already being upscaled"):
        upscale.queue_upscale(project, c, t["id"])


def test_redetail_graph(project):
    c = FakeComfy()
    t = takes(project)[1]
    upscale.queue_upscale(project, c, t["id"], method="redetail", size="720p", strength="strong", seed=3)
    g = c.graphs[0]
    by = {n["class_type"]: n["inputs"] for n in g.values()}
    assert by["MinimaxH3LatentUpscaler3D"]["mode"] == "target dimensions" and by["MinimaxH3LatentUpscaler3D"]["mode.width"] == 704
    assert by["ManualSigmas"]["sigmas"].startswith("0.9035") and by["RandomNoise"]["noise_seed"] == 3
    spec = json.loads(by["ClapperSceneConditioning"]["spec"])
    assert "continue_from" not in spec and by["ClapperSceneConditioning"]["width"] == 704
    assert "ref2va" in by["UNETLoader"]["unet_name"]
    assert by["ClapperSaveVideo"]["audio_path"].endswith("audio.wav")


def test_finish_and_export_use_upscaled(project):
    c = FakeComfy()
    t = takes(project)[0]
    job = upscale.queue_upscale(project, c, t["id"], method="sharpen", size="720p")
    out = project.root / job["out"]
    out.parent.mkdir(parents=True)
    out.write_bytes(b"mp4")
    c.finish(job["prompt_id"])
    tools.collect(project, c)
    t = project.data["takes"][t["id"]]
    assert t["upscaled"] == "sharpen-720p" and t["upscales"]["sharpen-720p"]["width"] == 720
    plan = export.export_plan(project)
    assert plan[0]["file"].endswith("sharpen-720p.mp4") and plan[0]["upscaled"] == "sharpen 720p" and plan[1]["upscaled"] is None
    assert export.export_size(plan, "auto") == [720, 1274]
    assert export.export_size(plan, "1080p") == [1080, 1910]
    assert export.export_plan(project, use_upscaled=False)[0]["file"].endswith("take.mp4")
    check = export.export_check(project)
    assert check["upscaled"] == [1] and check["not_upscaled"] == [2] and check["size"] == [720, 1274]
    tools.dismiss(project, job["id"])
    assert out.exists()   # an upscale's result stays when its job is dismissed


def test_redetail_needs_latents(project):
    t = takes(project)[0]
    (project.root / t["dir"] / "latent.safetensors").unlink()
    with pytest.raises(ProjectError, match="saved latents"):
        upscale.queue_upscale(project, FakeComfy(), t["id"], method="redetail")


def test_export_size_can_scale_down():
    segs = [{"width": 1080, "height": 1910}, {"width": 1080, "height": 1910}]
    assert export.export_size(segs, "720p") == [720, 1272]
    assert export.export_size(segs, "1080p") == [1080, 1910]


def test_delete_version(project):
    t = takes(project)[0]
    f = project.root / t["dir"] / "upscaled" / "sharpen-720p.mp4"
    f.parent.mkdir(parents=True); f.write_bytes(b"x")
    t["upscales"] = {"sharpen-720p": {"file": str(f.relative_to(project.root)), "method": "sharpen", "size": "720p", "width": 720, "height": 1274}}
    t["upscaled"] = "sharpen-720p"
    upscale.delete_version(project, t["id"], "sharpen-720p")
    assert not f.exists() and t["upscaled"] is None and t["upscales"] == {}
    with pytest.raises(ProjectError):
        upscale.delete_version(project, t["id"], "sharpen-720p")


def test_list_and_delete_exports(project):
    folder = project.root / "exports"; folder.mkdir(exist_ok=True)
    (folder / "short-1.mp4").write_bytes(b"v")
    (folder / "short-1.json").write_text(json.dumps({"created": "20261031-1", "total_seconds": 10.0, "width": 720, "height": 1274,
                                                     "scenes_exported": 2, "scenes_total": 2, "segments": [{"scene_number": 1, "upscaled": "sharpen 720p"}, {"scene_number": 2, "upscaled": None}]}))
    items = export.list_exports(project)
    assert items[0]["file"] == "exports/short-1.mp4" and items[0]["upscaled"] == [1] and items[0]["width"] == 720
    with pytest.raises(ProjectError):
        export.delete_export(project, "../project.json")
    export.delete_export(project, "exports/short-1.mp4")
    assert export.list_exports(project) == [] and not (folder / "short-1.json").exists()


def test_a_second_run_is_a_new_version_and_the_choice_stays(project, monkeypatch):
    import types
    t = next(iter(project.data["takes"].values()))
    client = types.SimpleNamespace(submit=lambda g: "pid")
    j1 = upscale.queue_upscale(project, client, t["id"], method="sharpen", size="720p")
    project.data["jobs"][j1["id"]]["status"] = "done"
    j2 = upscale.queue_upscale(project, client, t["id"], method="sharpen", size="720p")
    assert j1["out"] != j2["out"] and j2["out"].endswith("sharpen-720p-2.mp4")
    for j in (j1, j2):
        (project.root / j["out"]).parent.mkdir(parents=True, exist_ok=True); (project.root / j["out"]).write_bytes(b"x")
    upscale.finish_job(project, j1); upscale.finish_job(project, j2)
    assert t["upscaled"] == "sharpen-720p"           # the first one stays in use; the new one waits to be chosen
    assert upscale.delete_version(project, t["id"], "sharpen-720p") == "sharpen-720p-2"   # falls back to the other version

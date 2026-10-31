import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from comfyui_clapper.engine import director  # noqa: E402
from comfyui_clapper.engine.project import Project, ProjectError, asset_kind_for  # noqa: E402
from comfyui_clapper.engine.settings import apply_video_model, DEFAULT_SETTINGS  # noqa: E402

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x10\x00\x00\x00\x10\x08\x02\x00\x00\x00\x90\x91h6"
       b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82")


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "promo", "Promo")
    for name in ("opening", "hero", "style", "street"):
        (tmp_path / f"{name}.png").write_bytes(PNG)
        p.add_reference(name, tmp_path / f"{name}.png", "image", default=name in ("hero", "style"))
    p.data["start_image"] = "opening"
    p.data["style"] = "Flat 2D cartoon in the look of @style."
    (tmp_path / "line.mp3").write_bytes(b"ID3fake")
    p.save()
    return p


def test_references_are_library_assets(project):
    items = project.data["library"]["items"]
    assert all(items[n]["asset"] in project.assets for n in items)
    assert project.assets[items["opening"]["asset"]]["file"] == items["opening"]["file"]


def test_old_projects_get_assets_on_open(project):
    data = json.loads((project.root / "project.json").read_text())
    data.pop("assets")
    for item in data["library"]["items"].values():
        item.pop("asset")
    (project.root / "project.json").write_text(json.dumps(data))
    reopened = Project.open(project.root)
    assert len(reopened.assets) == 4
    opening = reopened.data["library"]["items"]["opening"]
    assert reopened.assets[opening["asset"]]["role"] == "start"


def test_kind_from_extension():
    assert asset_kind_for("a.MP3") == "audio" and asset_kind_for("b.m4a") == "audio"
    assert asset_kind_for("c.webp") == "image" and asset_kind_for("d.mov") == "video"
    with pytest.raises(ProjectError, match="unsupported"):
        asset_kind_for("notes.txt")


def test_add_name_and_remove_asset(project, tmp_path):
    a = project.add_asset(tmp_path / "line.mp3", role="speech", title="Line 1")
    assert a["kind"] == "audio" and (project.root / a["file"]).is_file()
    item = project.name_asset(a["id"], "narrator", definition="@narrator is the off-screen voice.", default=True)
    assert item["asset"] == a["id"] and "narrator" in project.data["library"]["defaults"]
    with pytest.raises(ProjectError, match="@narrator"):
        project.remove_asset(a["id"])
    project.remove_reference("narrator")
    project.remove_asset(a["id"])
    assert a["id"] not in project.assets and (project.root / "trash").is_dir()


def test_remove_reference_refused_while_mentioned(project):
    project.add_scene(text="@street at night.", refs=["street", "style"])
    with pytest.raises(ProjectError, match="scene 1"):
        project.remove_reference("street")


def test_name_conflict(project, tmp_path):
    a = project.add_asset(tmp_path / "hero.png")
    with pytest.raises(ProjectError, match="already used"):
        project.name_asset(a["id"], "hero")


def test_scene_start_modes(project):
    s1 = project.add_scene(text="@opening.")
    s2 = project.add_scene(text="@hero walks.")
    s3 = project.add_scene(text="@street. @hero arrives.", join="cut", start_image="street", refs=["hero", "style", "street"])
    s4 = project.add_scene(text="@hero alone.", join="cut", start_image="")
    assert [project.scene_mode(s) for s in (s1, s2, s3, s4)] == ["image", "continue", "image", "fresh"]
    c3 = project.compile_prompt(s3)
    assert c3["start"] == "street" and c3["labels"]["street"] == "<Picture 1>" and c3["labels"]["hero"] == "<Picture 2>"
    c4 = project.compile_prompt(s4)
    assert c4["start"] is None and c4["labels"]["hero"] == "<Picture 1>"
    with pytest.raises(ProjectError, match="image reference"):
        project.update_scene(s4["id"], start_image="nope")


def test_old_scene_hash_unchanged_by_new_fields(project):
    s = project.add_scene(text="@opening.")
    before = project.compile_prompt(s)["sha256"]
    assert "start_image" not in s and "audio_clips" not in s
    import hashlib
    assert before == hashlib.sha256(project.compile_prompt(s)["prompt"].encode()).hexdigest()


def test_audio_clips_become_guides(project, tmp_path):
    clip = project.add_asset(tmp_path / "line.mp3", role="speech")
    s1 = project.add_scene(text="@opening.", audio_clips=[{"asset": clip["id"], "at": 0.5}])
    plan = director.plan_take(project, s1["id"], seed=1)
    assert plan["spec"]["audio_guides"] == [{"path": str(project.root.resolve() / clip["file"]), "frame": 12}]
    assert plan["spec"]["start_image"]["pin"] is True
    with pytest.raises(ProjectError, match="used as an audio clip"):
        project.remove_asset(clip["id"])
    project.update_scene(s1["id"], audio_clips=[{"asset": clip["id"], "at": 9}])
    with pytest.raises(ProjectError, match="after the end"):
        director.plan_take(project, s1["id"], seed=1)
    with pytest.raises(ProjectError, match="audio item"):
        project.update_scene(s1["id"], audio_clips=[{"asset": project.data["library"]["items"]["hero"]["asset"], "at": 0}])


def test_clip_time_counts_after_carried_frames(project, tmp_path):
    clip = project.add_asset(tmp_path / "line.mp3")
    s1 = project.add_scene(text="@opening.")
    s2 = project.add_scene(text="@hero.", audio_clips=[{"asset": clip["id"], "at": 1.0}])
    t = project.new_take(s1["id"], seed=1)
    project.update_take(t["id"], status="done")
    project.select_take(s1["id"], t["id"])
    plan = director.plan_take(project, s2["id"], seed=2)
    assert plan["context"] == 22 and plan["spec"]["audio_guides"][0]["frame"] == 22 + 24


def test_video_model_modes():
    s = DEFAULT_SETTINGS
    assert apply_video_model(s, fresh=True)["models"]["unet"] == s["models"]["unet"]  # default: FL2VA everywhere
    r = apply_video_model({**s, "video_model": {**s["video_model"], "mode": "ref2va"}}, fresh=False)
    assert "ref2va" in r["models"]["unet"] and r["sampling"]["steps"] == 4 and "ref2v" in r["loras"][0]["name"]
    first = {**s, "video_model": {**s["video_model"], "mode": "ref2va_fresh"}}
    assert apply_video_model(first, fresh=True)["model_used"] == "ref2va"
    assert apply_video_model(first, fresh=False)["model_used"] == "fl2va"


def test_take_steps_override_beats_video_model(project):
    project.data["settings"] = {"video_model": {"mode": "ref2va"}}
    s1 = project.add_scene(text="@opening.")
    plan = director.plan_take(project, s1["id"], seed=1, overrides={"sampling": {"steps": 8}})
    assert plan["settings"]["sampling"]["steps"] == 8 and "ref2va" in plan["settings"]["models"]["unet"]


def test_restore_brings_back_start_and_clips(project, tmp_path):
    clip = project.add_asset(tmp_path / "line.mp3")
    s = project.add_scene(text="@hero.", join="cut", start_image="street", refs=["hero", "style", "street"],
                          audio_clips=[{"asset": clip["id"], "at": 0.2}])
    t = project.new_take(s["id"], seed=1, scene_text=s["text"], scene_refs=s["refs"], scene_start_image="street",
                         scene_audio_clips=s["audio_clips"])
    project.update_scene(s["id"], start_image="", audio_clips=[])
    project.restore_take_prompt(t["id"])
    assert project.scene(s["id"])["start_image"] == "street" and project.scene(s["id"])["audio_clips"][0]["at"] == 0.2


def test_grab_frame_and_probe(project, tmp_path):
    import shutil
    import subprocess
    from comfyui_clapper.engine import media
    if not Path(media.FFMPEG).exists() and not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    s = project.add_scene(text="@opening.")
    t = project.new_take(s["id"], seed=1)
    take_dir = project.root / t["dir"]
    take_dir.mkdir(parents=True)
    subprocess.run([media.FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=64x112:d=1", "-f", "lavfi",
                    "-i", "sine=d=1", "-shortest", str(take_dir / "take.mp4")], check=True)
    project.update_take(t["id"], status="done")
    frame = media.grab_frame(project, t["id"], 0.5)
    assert frame["kind"] == "image" and frame["role"] == "frame" and frame["origin"] == {"type": "frame", "take": t["id"], "seconds": 0.5}
    assert (frame["width"], frame["height"]) == (64, 112)
    clip = media.add_with_info(project, take_dir / "take.mp4", role="clip")
    assert clip["kind"] == "video" and 0.9 < clip["duration"] < 1.2

"""The assistant's Studio tools: characters, images, speech, scene starts and sound clips."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from comfyui_clapper.engine import assistant  # noqa: E402
from comfyui_clapper.engine.project import Project, ProjectError  # noqa: E402
from test_tools import PNG, FakeComfy, nodes_of  # noqa: E402

TEXT = ("integrated_multimodal_description: [Shot 1] @aoi waits in the rain.\\noverall_soundscape: rain.\\n"
        "non_diegetic_music: N/A").replace("\\\\n", "\\n")


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "a", "A")
    for n in ("aoi", "stop", "frame"):
        (tmp_path / f"{n}.png").write_bytes(PNG)
    (tmp_path / "v.wav").write_bytes(b"RIFF")
    p.aoi_img = p.add_asset(tmp_path / "aoi.png", role="character", title="Aoi")
    p.stop_img = p.add_asset(tmp_path / "stop.png", role="place", title="Stop")
    p.frame = p.add_asset(tmp_path / "frame.png", role="start", title="Frame")
    p.voice = p.add_asset(tmp_path / "v.wav", role="voice", title="Girl voice")
    p.name_asset(p.frame["id"], "opening")
    return p


def plan_apply(project, tool, args, client=None):
    plan = assistant.plan_proposal(project, tool, args)
    return plan, assistant.apply_proposal(project, {"id": "p1", **plan}, client=client)


def test_create_character_designs(project):
    c = FakeComfy()
    plan, result = plan_apply(project, "create_character", {"kind": "character", "name": "Ren", "description": "a tall boy with messy black hair and a grey hoodie"}, c)
    assert plan["summary"] == "Create character Ren and design 2 image(s)" and "queued" in result and len(c.graphs) == 2
    with pytest.raises(ProjectError, match="already exists"):
        assistant.plan_proposal(project, "create_character", {"kind": "character", "name": "ren", "description": "x" * 20})
    with pytest.raises(ProjectError, match="too short"):
        assistant.plan_proposal(project, "create_character", {"kind": "place", "name": "Park", "description": "a park"})


def test_create_from_image_and_edit_voice(project):
    plan, result = plan_apply(project, "create_character", {"kind": "character", "name": "Aoi", "description": "a girl with a short indigo bob",
                                                            "from_image": project.aoi_img["id"]})
    assert "@aoi" in result and plan["args"]["designs"] == 0
    plan_apply(project, "edit_character", {"character": "@aoi", "voice": project.voice["id"], "description": "a girl in a yellow raincoat"})
    e = assistant.find_entity(project, "Aoi")
    assert e["voice"] == project.voice["id"] and project.data["library"]["items"]["aoi"]["definition"].endswith("yellow raincoat")
    snap = assistant.snapshot(project)
    assert "Cast & world" in snap and "Aoi (character) · @aoi" in snap and "voice " + project.voice["id"] in snap


def test_make_images_and_speak(project):
    aoi = project.create_entity("character", "Aoi", description="indigo bob", from_asset=project.aoi_img["id"])
    project.create_entity("place", "Bus stop", description="a rural bus stop", from_asset=project.stop_img["id"])
    c = FakeComfy()
    plan, _ = plan_apply(project, "make_images", {"character": "Aoi", "action": "look", "change": "wearing a winter coat", "count": 1}, c)
    assert "The same character as image 1" in nodes_of(c.graphs[0], "CLIPTextEncode")[0]["inputs"]["text"]
    plan, _ = plan_apply(project, "make_images", {"character": "Aoi", "action": "place", "change": "sitting on the bench", "place": "@bus_stop", "count": 1}, c)
    assert len(nodes_of(c.graphs[1], "ReferenceLatent")) == 2
    with pytest.raises(ProjectError, match="no voice"):
        assistant.plan_proposal(project, "speak_line", {"text": "Hello", "voice": "Aoi"})
    project.update_entity(aoi["id"], voice=project.voice["id"])
    plan, result = plan_apply(project, "speak_line", {"text": "Three days of rain...", "voice": "Aoi", "language": "English"}, c)
    assert "Girl voice" not in plan["summary"] and "Aoi's voice" in plan["summary"] and "2 take(s)" in result
    assert nodes_of(c.graphs[2], "FB_Qwen3TTSVoiceClone")[0]["inputs"]["target_text"] == "Three days of rain..."
    with pytest.raises(ProjectError, match="generating is not available"):
        assistant.apply_proposal(project, {"id": "x", **assistant.plan_proposal(project, "speak_line", {"text": "Hi", "voice": project.voice["id"]})})


def test_scene_start_and_sound_clips(project):
    project.create_entity("character", "Aoi", description="indigo bob", from_asset=project.aoi_img["id"])
    plan, _ = plan_apply(project, "add_scenes", {"scenes": [{"title": "Rain", "text": TEXT, "start": "image", "start_image": "@opening",
                                                             "refs": ["aoi"], "sound_clips": [{"clip": project.voice["id"], "at": 1}]}]})
    s = project.scenes[0]
    assert s["join"] == "cut" and s["start_image"] == "opening" and s["audio_clips"] == [{"asset": project.voice["id"], "at": 1.0}]
    assert project.compile_prompt(s)["start"] == "opening"
    plan_apply(project, "edit_scene", {"scene": 1, "start": "fresh"})
    assert project.scenes[0]["start_image"] == "" and project.scene_mode(project.scenes[0]) == "fresh"
    with pytest.raises(ProjectError, match="not an audio"):
        assistant.plan_proposal(project, "edit_scene", {"scene": 1, "sound_clips": [{"clip": project.frame["id"]}]})
    with pytest.raises(ProjectError, match="needs start_image"):
        assistant.plan_proposal(project, "edit_scene", {"scene": 1, "start": "image"})


def test_settings_shape_and_model(project):
    plan, _ = plan_apply(project, "change_settings", {"video_shape": "16:9", "video_model": "ref2va"})
    assert project.data["settings"]["resolution"]["aspect"] == "16:9" and project.data["settings"]["video_model"]["mode"] == "ref2va"
    assert "video shape 16:9" in assistant.snapshot(project)

"""Harness checks found with a real run: leaked tool calls, characters outside the references, action density,
what changed outside the chat, and frames attached when a scene is named."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from comfyui_clapper.engine import assistant  # noqa: E402
from comfyui_clapper.engine.project import Project, ProjectError  # noqa: E402
from test_tools import PNG  # noqa: E402

FIVE_BEATS = ("integrated_multimodal_description: [Shot 1] The bus is stopped at the shelter, its door is open. @aoi walks to the door, "
              "steps up the small steps into the bus, and stands on the aisle. Looking back down the aisle, she sees @ren sitting at the back "
              "with a small, shy smile. The rain is still falling outside the open door. Medium shot, a slow dolly follow of Aoi walking to the "
              "bus and stepping inside, one continuous shot.\noverall_soundscape: rain.\nnon_diegetic_music: N/A")
STAGED = ("integrated_multimodal_description: [Shot 1] The bus stops and its folding front door hisses open beside @aoi. The camera stays "
          "outside on the wet platform, behind her. She lowers her clear umbrella and climbs the two steps into the bus, her back to the camera. "
          "Medium shot from behind, the camera follows her to the doorway, one continuous shot.\noverall_soundscape: rain.\nnon_diegetic_music: N/A")


def test_leaked_qwen_calls_are_split():
    leaked = '{"scene":2,"part":"end\n</function>\n</tool_call>\n<tool_call>\n<function=view_take>\n<parameter=scene>\n3","part":"all"}'
    calls, _ = assistant.repair_tool_calls([{"id": "c1", "name": "view_take", "arguments": leaked}], "")
    assert [(c["name"], json.loads(c["arguments"])) for c in calls] == [("view_take", {"scene": 2, "part": "end"}),
                                                                       ("view_take", {"scene": 3, "part": "all"})]
    text = "I'll look.\n<tool_call>\n<function=get_scene>\n<parameter=scene>\n2\n</parameter>\n</function>\n</tool_call>"
    calls, content = assistant.repair_tool_calls([], text)
    assert calls[0]["name"] == "get_scene" and json.loads(calls[0]["arguments"]) == {"scene": 2} and content == "I'll look."
    ok = [{"id": "x", "name": "get_scene", "arguments": '{"scene": 1}'}]
    assert assistant.repair_tool_calls(ok, "fine") == (ok, "fine")


def test_density():
    assert "actions in 5 seconds" in assistant.density_warning(FIVE_BEATS, 5)
    assert assistant.density_warning(STAGED, 5) == ""
    assert assistant.density_warning(FIVE_BEATS, 10) == ""   # the same beats fit in 10 seconds


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "a", "A")
    for n in ("aoi", "ren"):
        (tmp_path / f"{n}.png").write_bytes(PNG)
        a = p.add_asset(tmp_path / f"{n}.png", role="character", title=n)
        p.create_entity("character", n.capitalize(), description=f"{n} description", from_asset=a["id"])
    return p


def test_character_named_but_not_in_refs(project):
    s = project.add_scene(text=FIVE_BEATS.replace("@ren", "Ren"), refs=["aoi"])
    with pytest.raises(ProjectError, match="names Ren, but @ren is not in this scene's references"):
        assistant._check_prompts(project, [s["id"]])
    project.update_scene(s["id"], refs=["aoi", "ren"], text=FIVE_BEATS)
    assistant._check_prompts(project, [s["id"]])
    plan = assistant.plan_proposal(project, "edit_scene", {"scene": 1, "text": FIVE_BEATS})
    assert "actions in 5 seconds" in plan["args"]["warning"]


def test_changes_since_last_message(project):
    s1 = project.add_scene(text=STAGED, refs=["aoi"])
    before = assistant.scene_fingerprints(project)
    project.update_scene(s1["id"], text=STAGED.replace("lowers", "closes"))
    project.add_scene(text=STAGED, refs=["aoi"], title="Inside")
    changes = assistant.changes_since(before, assistant.scene_fingerprints(project))
    assert changes == ["scene 1: its text was edited (read it again below)", 'scene 2 "Inside" was added']
    assert assistant.changes_since(None, before) == []


def test_frames_attached_when_a_scene_is_named(project, monkeypatch):
    s1 = project.add_scene(text=STAGED, refs=["aoi"])
    s2 = project.add_scene(text=STAGED, refs=["aoi"])
    t1 = project.new_take(s1["id"], seed=1); project.update_take(t1["id"], status="done"); project.select_take(s1["id"], t1["id"])
    calls = []
    monkeypatch.setattr(assistant, "extract_frames", lambda p, take, count=4, end=False: calls.append((take["id"], count, end)) or [f"f{len(calls)}.jpg"] * count)
    images, note = assistant.scene_frames_for(project, "Fix scene 2 please")
    assert calls == [(t1["id"], 1, True)] and images == ["f1.jpg"] and "LAST frame of scene 1" in note
    assert assistant.scene_frames_for(project, "Thanks!") == ([], "")


def test_continuity_echo_quotes_the_first_sentence(project):
    s1 = project.add_scene(text=STAGED, refs=["aoi"])
    s2 = project.add_scene(text=STAGED, refs=["aoi"])
    assert assistant.continuity_echo(project, s2["id"], FIVE_BEATS) == ""        # scene 1 has no finished take yet
    t = project.new_take(s1["id"], seed=1); project.update_take(t["id"], status="done"); project.select_take(s1["id"], t["id"])
    echo = assistant.continuity_echo(project, s2["id"], FIVE_BEATS)
    assert 'Your text begins: "The bus is stopped at the shelter, its door is open."' in echo and "LAST frame of scene 1" in echo
    assert assistant.continuity_echo(project, s1["id"], FIVE_BEATS) == ""        # the first scene continues nothing


def test_takes_and_their_own_text(project):
    s1 = project.add_scene(text=STAGED, refs=["aoi"])
    s2 = project.add_scene(text=STAGED, refs=["aoi"])
    t1 = project.new_take(s1["id"], seed=1, scene_text="an older text"); project.update_take(t1["id"], status="done"); project.select_take(s1["id"], t1["id"])
    t2 = project.new_take(s1["id"], seed=2); project.update_take(t2["id"], status="done")   # adopted: no scene_text, only meta.json
    d = project.root / t2["dir"]; d.mkdir(parents=True)
    labels = project.compile_prompt(s1)["labels"]
    (d / "meta.json").write_text(json.dumps({"prompt": "Style.\n\nContinuation of Chunk 1. " + STAGED.replace("@aoi", labels["aoi"])}))
    assert assistant.take_text(project, t2) == STAGED
    out, _ = assistant.run_read_tool(project, "get_scene", {"scene": 1}, {"kind": "local"})
    assert "Take 1: done (SELECTED" in out and "made from this text:\nan older text" in out and "Take 2: done, seed 2 — made from the scene's current text" in out
    assert "continues from scene 1 Take 1" in assistant.snapshot(project)
    plan = assistant.plan_proposal(project, "select_take", {"scene": 1, "take": 2})
    assert plan["summary"] == "Use Take 2 for scene 1 (the next scene then continues from it)"
    assistant.apply_proposal(project, {"id": "x", **plan})
    assert project.scene(s1["id"])["selected_take"] == t2["id"]
    with pytest.raises(ProjectError, match="already the selected take"):
        assistant.plan_proposal(project, "select_take", {"scene": 1, "take": 2})


def test_frames_for_a_named_take(project, monkeypatch):
    s1 = project.add_scene(text=STAGED, refs=["aoi"])
    t1 = project.new_take(s1["id"], seed=1); project.update_take(t1["id"], status="done"); project.select_take(s1["id"], t1["id"])
    t2 = project.new_take(s1["id"], seed=2); project.update_take(t2["id"], status="done")
    monkeypatch.setattr(assistant, "extract_frames", lambda p, take, count=4, end=False: [f"{take['number']}-{'end' if end else i}" for i in range(count)])
    images, note = assistant.scene_frames_for(project, "Take 2 of scene 1 is the one I like, write the next scene from it")
    assert images == ["2-1", "2-2", "2-end"] and "NOT the selected take" in note and "LAST frame" in note

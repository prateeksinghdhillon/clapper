"""Things that must not go quietly wrong: badges after renumbering or a new picture, export warnings, removals."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from comfyui_clapper.engine import export, finish  # noqa: E402
from comfyui_clapper.engine.project import Project, ProjectError  # noqa: E402
from test_tools import PNG  # noqa: E402


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "p", "P")
    (tmp_path / "a.png").write_bytes(PNG)
    p.add_reference("hero", tmp_path / "a.png", "image", default=True, definition="@hero is a hero.")
    return p


def _rendered(project, scene, **extra):
    c = project.compile_prompt(scene)
    t = project.new_take(scene["id"], prompt_sha256=c["sha256"], refs=project.scene_refs(scene), pictures=c["pictures"], **extra)
    d = project.root / t["dir"]; d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps({"frames": 121, "width": 416, "height": 736}))
    project.update_take(t["id"], status="done", frames=121, width=416, height=736)
    project.select_take(scene["id"], t["id"])
    return t


def test_inserting_a_scene_is_not_an_edit(project):
    s1 = project.add_scene(text="@hero waits.", join="cut"); s2 = project.add_scene(text="@hero waves.")
    t1 = _rendered(project, s1); _rendered(project, s2, parent_take=t1["id"])
    assert project.scene_status(s2["id"]) == []
    project.add_scene(text="@hero sits.", after=s1["id"], join="cut")   # s2 is now scene 3: "Continuation of Chunk 1." becomes 2
    assert "prompt_changed" not in project.scene_status(s2["id"])
    project.update_scene(s2["id"], text="@hero waves twice.")
    assert "prompt_changed" in project.scene_status(s2["id"])


def test_a_new_picture_under_the_same_name_is_flagged(project, tmp_path):
    s1 = project.add_scene(text="@hero waits.", join="cut")
    _rendered(project, s1)
    project.data["library"]["items"]["hero"]["sha256"] = "another"; project.save()
    assert project.scene_status(s1["id"]) == ["pictures_changed"]
    assert any(w["badge"] == "pictures_changed" for w in export.export_check(project)["warnings"])


def test_export_check_names_mix_problems(project, tmp_path):
    s1 = project.add_scene(text="@hero waits.", join="cut"); _rendered(project, s1)
    (tmp_path / "logo.png").write_bytes(PNG)
    logo = project.add_asset(tmp_path / "logo.png", role="overlay")
    finish.set_finish(project, {"overlays": [{"asset": logo["id"], "start": 30}, {"asset": logo["id"], "start": 2, "end": 1}]})
    msgs = [w["message"] for w in export.export_check(project)["warnings"]]
    assert any("starts at 30s, after the video ends" in m for m in msgs) and any("ends (1s) before it starts" in m for m in msgs)
    with pytest.raises(ProjectError, match="used in the Finish"):
        project.remove_asset(logo["id"])


def test_restoring_a_take_made_with_old_defaults_keeps_its_references(project, tmp_path):
    (tmp_path / "b.png").write_bytes(PNG)
    project.add_reference("ren", tmp_path / "b.png", "image")
    s1 = project.add_scene(text="@hero waits.", join="cut")
    t = _rendered(project, s1, scene_text="@hero waits.", scene_refs=None)
    project.data["library"]["defaults"] = ["hero", "ren"]; project.save()
    project.restore_take_prompt(t["id"])
    assert project.scene(s1["id"])["refs"] == ["hero"]


# ------------------------------------------------------------------ jobs and takes

class Client:
    def __init__(self, history=None):
        self.n, self.hist = 0, history or {}

    def submit(self, graph):
        self.n += 1
        return f"pid{self.n}"

    def queue_state(self):
        return {"queue_running": [], "queue_pending": []}

    def history(self, pid):
        return self.hist.get(pid)


def test_history_outcomes():
    from comfyui_clapper.engine.director import history_outcome
    assert history_outcome(None) == ("lost", None)
    assert history_outcome({"status": {"status_str": "success"}}) == ("ok", None)
    assert history_outcome({"status": {"status_str": "error", "messages": [["execution_interrupted", {}]]}})[0] == "stopped"
    assert history_outcome({"status": {"status_str": "error", "messages": [["execution_error", {"exception_message": "OOM"}]]}}) == ("error", "OOM")


def test_requeue_after_the_scene_before_was_rendered_again(project):
    from comfyui_clapper.engine import director
    s1 = project.add_scene(text="@hero waits.", join="cut"); s2 = project.add_scene(text="@hero waves.")
    old1 = project.new_take(s1["id"], seed=1); project.update_take(old1["id"], status="interrupted")
    old2 = project.new_take(s2["id"], seed=2, parent_take=old1["id"], steps=12); project.update_take(old2["id"], status="interrupted")
    new1 = _rendered(project, s1)
    t = director.requeue(project, old2["id"], Client())
    assert t["parent_take"] == new1["id"] and t["seed"] == 2 and t["steps"] == 12


def test_render_rest_checks_every_scene_first_and_selects_the_new_chain(project):
    from comfyui_clapper.engine import director
    s1 = project.add_scene(text="@hero waits.", join="cut"); s2 = project.add_scene(text="@hero waves.")
    s3 = project.add_scene(text="@ghost appears.")
    with pytest.raises(ProjectError, match="scene 3 cannot render.*nothing was queued"):
        director.queue_rest(project, Client())
    assert not project.data["takes"]
    project.update_scene(s3["id"], text="@hero leaves.")
    queued = director.queue_rest(project, Client())
    assert len(queued) == 3 and all(project.data["takes"][t["id"]].get("select_when_done") for t in queued)


def test_images_finished_before_a_restart_are_collected_not_deleted(project):
    from comfyui_clapper.engine import tools
    job = {"id": "j_x", "tool": "image", "tool_title": "Image", "kind": "image", "inputs": {"prompt": "a"}, "seed": 1, "role": "other",
           "title": "x", "status": "running", "created": "now", "dir": "jobs/j_x", "outputs": [], "prompt_id": "gone"}
    project.data["jobs"] = {"j_x": job}; project.save()
    (project.root / "jobs/j_x").mkdir(parents=True); (project.root / "jobs/j_x/out_1.png").write_bytes(PNG)
    with pytest.raises(ProjectError, match="results on disk"):
        job["status"] = "interrupted"; tools.dismiss(project, "j_x")
    job["status"] = "running"
    tools.collect(project, Client())     # ComfyUI has no history of it (restarted)
    assert project.data["jobs"]["j_x"]["status"] == "done" and len(project.data["jobs"]["j_x"]["outputs"]) == 1


def test_a_removed_scene_cannot_hide_a_running_take(project):
    s1 = project.add_scene(text="@hero waits.", join="cut")
    project.new_take(s1["id"], seed=1)
    with pytest.raises(ProjectError, match="rendering — stop it first"):
        project.remove_scene(s1["id"])


def test_a_stale_copy_does_not_overwrite_another_programs_save(project):
    other = Project.open(project.root)
    other.data["style"] = "Changed elsewhere."; other.save()
    project.data["notes"] = "mine"
    with pytest.raises(ProjectError, match="changed elsewhere after it was opened"):
        project.save()
    assert Project.open(project.root).data["style"] == "Changed elsewhere."


# ------------------------------------------------------------------ references and characters

def test_removing_a_used_name_asks_first(project):
    s1 = project.add_scene(text="waits.", join="cut", refs=["hero"])
    with pytest.raises(ProjectError, match="used as scene 1's references; removing it takes it out of them"):
        project.remove_reference("hero")
    project.remove_reference("hero", force=True)
    assert project.scene(s1["id"])["refs"] == []


def test_character_definitions_written_by_hand_stay(project, tmp_path):
    (tmp_path / "c.png").write_bytes(PNG)
    a = project.add_asset(tmp_path / "c.png", role="character", title="c")
    e = project.create_entity("character", "Aoi", description="a girl", from_asset=a["id"])
    project.update_reference(e["ref"], definition="@aoi is Aoi, always in a yellow raincoat.")
    project.update_entity(e["id"], description="a tall girl")
    assert project.data["library"]["items"][e["ref"]]["definition"] == "@aoi is Aoi, always in a yellow raincoat."
    project.update_reference(e["ref"], definition="")     # empty: written from the description again
    assert project.data["library"]["items"][e["ref"]]["definition"] == "@aoi is Aoi: a tall girl"
    # an image with its own @name and definition keeps that definition when it becomes a character
    project.add_reference("opening", tmp_path / "c.png", "image", definition="@opening is the first frame, a rainy street.")
    o = project.data["library"]["items"]["opening"]["asset"]
    e2 = project.create_entity("place", "Street", description="a street", from_asset=o)
    assert e2["ref"] == "opening" and project.data["library"]["items"]["opening"]["definition"].startswith("@opening is the first frame")


def test_the_only_main_look_cannot_be_taken_out(project, tmp_path):
    (tmp_path / "c.png").write_bytes(PNG)
    a = project.add_asset(tmp_path / "c.png", role="character", title="c")
    e = project.create_entity("character", "Ren", from_asset=a["id"], default=True)
    with pytest.raises(ProjectError, match="only image and its main look"):
        project.add_to_entity(a["id"], None)
    project.update_entity(e["id"], default=False)
    assert e["ref"] not in project.data["library"]["defaults"]


def test_restoring_a_take_made_outside_a_scene(project):
    s1 = project.add_scene(text="@hero waits.", join="cut")
    t = _rendered(project, s1)                      # no scene_text: only its prompt in meta.json
    meta = project.root / t["dir"] / "meta.json"
    labels = project.compile_prompt(s1)["labels"]
    meta.write_text(json.dumps({"frames": 121, "prompt": "integrated_multimodal_description: " + labels["hero"] + " runs."}))
    project.update_scene(s1["id"], text="@hero sleeps.")
    project.restore_take_prompt(t["id"])
    assert project.scene(s1["id"])["text"] == "integrated_multimodal_description: @hero runs."

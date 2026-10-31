import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from comfyui_clapper.engine.project import Project, ProjectError  # noqa: E402
from comfyui_clapper.h3_temporal import scene_frame_count  # noqa: E402


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "ad", "Test ad")
    for name, kind in (("opening", "image"), ("hero", "image"), ("style", "image"), ("logo", "image"), ("narrator", "audio")):
        src = tmp_path / f"{name}.{'wav' if kind == 'audio' else 'png'}"
        src.write_bytes(name.encode())
        p.add_reference(name, src, kind, default=name in ("hero", "style", "narrator"))
    p.data["start_image"] = "opening"
    p.data["style"] = "Flat 2D cartoon. @style defines the look."
    return p


def test_compile_numbers_follow_scene_refs(project):
    s = project.add_scene(text="@opening shop. @hero waves. (S1) voice from @narrator.")
    c = project.compile_prompt(s)
    assert c["labels"] == {"opening": "<Picture 1>", "hero": "<Picture 2>", "style": "<Picture 3>", "narrator": "<Audio 1>"}
    assert "<Picture 2> waves" in c["prompt"] and "<Picture 3> defines" in c["prompt"]
    # a scene with its own list renumbers
    s2 = project.add_scene(text="@logo appears.", refs=["style", "logo"])
    c2 = project.compile_prompt(s2)
    assert c2["labels"]["logo"] == "<Picture 3>" and c2["labels"]["style"] == "<Picture 2>"


def test_inactive_reference_is_an_error(project):
    s = project.add_scene(text="@logo appears")  # logo not in defaults
    with pytest.raises(ProjectError, match="@logo"):
        project.compile_prompt(s)


def test_autosave_reopen_and_history(project):
    s = project.add_scene(text="one")
    project.update_scene(s["id"], text="two")
    again = Project.open(project.root)
    assert again.scene(s["id"])["text"] == "two"
    assert len(list((project.root / "history").glob("*.json"))) >= 2
    assert json.loads((project.root / "project.json").read_text())["scenes"][0]["text"] == "two"


def test_badges_lineage(project):
    a = project.add_scene(text="a")
    b = project.add_scene(text="b")
    ta = project.new_take(a["id"], status="done", prompt_sha256=project.compile_prompt(a)["sha256"], refs=project.scene_refs(a))
    project.select_take(a["id"], ta["id"])
    tb = project.new_take(b["id"], status="done", parent_take=ta["id"],
                          prompt_sha256=project.compile_prompt(b)["sha256"], refs=project.scene_refs(b))
    project.select_take(b["id"], tb["id"])
    assert project.scene_status(b["id"]) == []
    ta2 = project.new_take(a["id"], status="done", prompt_sha256=project.compile_prompt(a)["sha256"], refs=project.scene_refs(a))
    project.select_take(a["id"], ta2["id"])
    assert "continuity_changed" in project.scene_status(b["id"])
    project.update_scene(b["id"], text="b edited")
    assert "prompt_changed" in project.scene_status(b["id"])


def test_insert_between(project):
    a = project.add_scene(text="a")
    c = project.add_scene(text="c")
    b = project.add_scene(text="b", after=a["id"], join="bridge")
    assert [s["text"] for s in project.scenes] == ["a", "b", "c"]


def test_frame_counts():
    assert scene_frame_count(5) == 124
    assert scene_frame_count(5, context_frames=22) == 141
    assert scene_frame_count(8, context_frames=22) == 209


def test_definitions_follow_active_refs(project):
    project.data["style"] = "Intro.\n\nsubject_definitions:\n(S1) narrator.\n\nStyle: flat."
    project.update_reference("hero", definition="@hero is the main character.")
    project.update_reference("logo", definition="@logo is the logo.")
    project.data["library"]["defaults"] = ["hero", "style", "logo"]
    a = project.add_scene(text="@hero waves.")
    b = project.add_scene(text="End card.", refs=["style", "logo"], join="cut")
    pa, pb = project.compile_prompt(a)["prompt"], project.compile_prompt(b)["prompt"]
    assert "subject_definitions:\n<Picture 2> is the main character.\n<Picture 4> is the logo.\n(S1) narrator." in pa
    assert "mascot" not in pb and "<Picture 3> is the logo." in pb


class FakeClient:
    def __init__(self):
        self.graphs = []

    def submit(self, graph):
        self.graphs.append(graph)
        return f"pid{len(self.graphs)}"


def test_queue_rest_chains_pending_parents(project):
    from comfyui_clapper.engine import director

    project.data["start_image"] = None
    a = project.add_scene(text="a", join="cut")
    project.add_scene(text="b")
    project.add_scene(text="c")
    ta = project.new_take(a["id"], status="done", width=416, height=736)
    project.select_take(a["id"], ta["id"])
    client = FakeClient()
    queued = director.queue_rest(project, client)
    assert len(queued) == 2 and len(client.graphs) == 2
    assert queued[0]["parent_take"] == ta["id"] and queued[1]["parent_take"] == queued[0]["id"]
    spec = json.loads(next(n for n in client.graphs[1].values() if n["class_type"] == "ClapperSceneConditioning")["inputs"]["spec"])
    assert spec["continue_from"]["take_dir"].endswith(queued[0]["dir"])
    with pytest.raises(ProjectError, match="already rendering"):
        director.queue_rest(project, client)


def test_export_check_and_restore_prompt(project, monkeypatch):
    from comfyui_clapper.engine import export

    a = project.add_scene(text="a", join="cut")
    b = project.add_scene(text="b")
    project.add_scene(text="c")
    for scene, parent in ((a, None), (b, "x")):
        t = project.new_take(scene["id"], status="done", context_frames=22 if parent else 0, join=scene["join"],
                             parent_take=parent, scene_text=scene["text"], scene_refs=None, scene_join=scene["join"],
                             prompt_sha256=project.compile_prompt(scene)["sha256"], refs=project.scene_refs(scene))
        (project.root / t["dir"]).mkdir(parents=True)
        (project.root / t["dir"] / "meta.json").write_text('{"frames": 141}')
        project.select_take(scene["id"], t["id"])
    check = export.export_check(project)
    assert check["ready"] == 2 and check["missing"] == [3] and not check["complete"]
    assert any(w["badge"] == "continuity_changed" and w["scene"] == 2 for w in check["warnings"])
    with pytest.raises(ProjectError):
        export.export_plan(project, partial=False)
    project.update_scene(b["id"], text="b edited")
    project.restore_take_prompt(project.scene(b["id"])["selected_take"])
    assert project.scene(b["id"])["text"] == "b"


def test_takes_are_numbered_per_scene(project):
    a = project.add_scene(text="a", join="cut")
    b = project.add_scene(text="b")
    t1 = project.new_take(a["id"]); t2 = project.new_take(a["id"]); u1 = project.new_take(b["id"])
    assert (t1["number"], t2["number"], u1["number"]) == (1, 2, 1)
    assert [t["number"] for t in project.takes_for(a["id"])] == [1, 2]


def test_disk_usage_and_safe_delete(project, tmp_path):
    from comfyui_clapper.engine.project import delete_project_folder, disk_usage

    (project.root / "exports" / "a.mp4").write_bytes(b"x" * 1000)
    u = disk_usage(project.root)
    assert u["parts"]["exports"] == 1000 and u["total"] >= 1000 and u["parts"]["refs"] > 0
    with pytest.raises(ProjectError):  # not a project folder
        delete_project_folder(tmp_path, home=tmp_path.parent)
    with pytest.raises(ProjectError):  # too close to home
        delete_project_folder(project.root, home=project.root.parent)
    delete_project_folder(project.root, home=tmp_path.parent)
    assert not project.root.exists()


def test_folder_name():
    from comfyui_clapper.engine.project import folder_name

    assert folder_name("Café promo 2!") == "cafe-promo-2"
    assert folder_name("  Café — Morning Rush ") == "cafe-morning-rush"
    assert folder_name("???") == "project"

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

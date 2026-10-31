"""Characters as references in image making, character sheets, and descriptions written from an image."""
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from comfyui_clapper.engine import assistant, tools  # noqa: E402
from comfyui_clapper.engine.project import Project, ProjectError  # noqa: E402
from test_tools import PNG  # noqa: E402


class Client:
    def __init__(self):
        self.graphs = []

    def submit(self, graph):
        self.graphs.append(graph)
        return f"pid{len(self.graphs)}"


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "s", "S")
    for n in ("aoi", "stop", "coat"):
        (tmp_path / f"{n}.png").write_bytes(PNG)
    p.aoi = p.create_entity("character", "Aoi", description="a girl in a yellow raincoat",
                            from_asset=p.add_asset(tmp_path / "aoi.png", role="character", title="aoi")["id"])
    p.stop = p.create_entity("place", "Bus stop", description="a wooden shelter in the rain",
                             from_asset=p.add_asset(tmp_path / "stop.png", role="place", title="stop")["id"])
    p.coat = p.add_asset(tmp_path / "coat.png", role="character", title="coat")
    p.add_to_entity(p.coat["id"], p.aoi["id"], look="winter coat")
    return p


def test_cast_brings_images_and_names_after_the_users_own(project):
    own = project.coat["id"]
    run = tools.with_cast(project, {"prompt": "Aoi at the Bus stop", "images": [own], "cast": [project.aoi["id"], project.stop["id"]]})
    assert run["images"] == [own, project.aoi["main"], project.stop["main"]]
    assert run["prompt"].startswith("Image 2 is Aoi: a girl in a yellow raincoat. Image 3 is Bus stop: a wooden shelter in the rain. Aoi at")
    jobs = tools.queue_tool(project, Client(), "image", {"prompt": "Aoi waves", "cast": [project.aoi["id"]]}, entity=project.aoi["id"], look="waving")
    assert jobs[0]["inputs"]["cast"] == [project.aoi["id"]] and not jobs[0]["inputs"]["images"]   # the recipe keeps the choice
    assert jobs[0]["entity"] == project.aoi["id"] and jobs[0]["look"] == "waving"


def test_character_sheet_from_a_chosen_look(project):
    inputs, extra = tools.entity_inputs(project, project.aoi["id"], "sheet", "", source=project.coat["id"])
    assert inputs["images"] == [project.coat["id"]] and inputs["aspect"] == "16:9" and "front view, three-quarter view, side view, back view" in inputs["prompt"]
    assert extra == {"entity": project.aoi["id"], "look": "sheet · winter coat", "sheet": True}
    with pytest.raises(ProjectError, match="for characters and props"):
        tools.entity_inputs(project, project.stop["id"], "sheet", "")


def test_the_sheet_as_the_characters_reference(project):
    e = project.aoi
    with pytest.raises(ProjectError, match="no character sheet yet"):
        project.update_entity(e["id"], ref_use="sheet")
    project.set_entity_sheet(e["id"], project.coat["id"])
    project.update_entity(e["id"], ref_use="sheet")
    item = project.data["library"]["items"][e["ref"]]
    assert item["asset"] == project.coat["id"] and "(shown from the front, side and back)" in item["definition"]
    project.add_to_entity(project.coat["id"], None)      # it leaves the character: scenes use the main look again
    assert project.entities[e["id"]]["sheet"] is None and project.data["library"]["items"][e["ref"]]["asset"] == e["main"]


def test_describe_image_reads_json_or_prose(project, monkeypatch):
    answers = iter(['Sure! {"name": "Mika", "description": "A girl  with a red scarf."}', "A tall lighthouse on a cliff."])

    class Fake:
        sees_images = True

        def __init__(self, provider):
            pass

        def chat(self, messages, **kw):
            assert messages[0]["content"][0]["type"] == "image_url"
            return {"content": next(answers)}
    monkeypatch.setattr(assistant, "Client", Fake)
    from PIL import Image
    Image.new("RGB", (32, 32), "red").save(project.root / project.coat["file"], format="PNG")
    prov = {"vision": True}
    assert assistant.describe_image(prov, project, project.coat["id"], "character") == {"name": "Mika", "description": "A girl with a red scarf."}
    out = assistant.describe_image(prov, project, project.coat["id"])
    assert out["description"] == "A tall lighthouse on a cliff." and out["kind"] == "other"
    with pytest.raises(Exception, match="cannot see images"):
        assistant.describe_image({"vision": False}, project, project.coat["id"])


def test_a_hand_written_definition_is_told_about_the_sheet(project):
    e = project.aoi
    project.update_reference(e["ref"], definition="@aoi is Aoi, always in a yellow raincoat.")
    project.set_entity_sheet(e["id"], project.coat["id"]); project.update_entity(e["id"], ref_use="sheet")
    assert project.data["library"]["items"][e["ref"]]["definition"] == "@aoi is Aoi (shown from the front, side and back), always in a yellow raincoat."
    project.update_entity(e["id"], ref_use="main")
    assert project.data["library"]["items"][e["ref"]]["definition"] == "@aoi is Aoi, always in a yellow raincoat."

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
    assert extra == {"entity": project.aoi["id"], "look": "sheet · winter coat", "sheet": True, "sheet_of": project.coat["id"]}
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


def test_references_take_only_what_they_are_for(project, tmp_path):
    (tmp_path / "jacket.png").write_bytes(PNG)
    jacket = project.add_asset(tmp_path / "jacket.png", role="character", title="jacket")
    inputs, extra = tools.entity_inputs(project, project.aoi["id"], "look", "", refs=[{"asset": jacket["id"], "use": "outfit"}])
    assert inputs["images"] == [project.aoi["main"], jacket["id"]]
    assert "From image 2 take the outfit and clothes only (not the face, hair or body). Keep image 1's face, hair and body." in inputs["prompt"]
    assert extra["look"] == "outfit from jacket"
    # a place variation with a lighting reference; free words work too
    inputs, _ = tools.entity_inputs(project, project.stop["id"], "look", "at night", refs=[{"asset": jacket["id"]}, {"asset": project.coat["id"], "use": "the puddles"}])
    assert "From image 2 take the lighting, time of day and weather only." in inputs["prompt"] and "From image 3 take only the puddles." in inputs["prompt"]
    # put in a place uses two images already: at most two references
    (tmp_path / "pose.png").write_bytes(PNG)
    pose = project.add_asset(tmp_path / "pose.png", role="character", title="pose")
    three = [{"asset": a} for a in (jacket["id"], project.coat["id"], pose["id"])]
    with pytest.raises(ProjectError, match="already uses 2, so add at most 2"):
        tools.entity_inputs(project, project.aoi["id"], "place", "", place_id=project.stop["id"], refs=three)
    # an image that is already in use is not counted twice (the place's own image as a "reference")
    inputs, _ = tools.entity_inputs(project, project.aoi["id"], "place", "", place_id=project.stop["id"], refs=three[:2] + [{"asset": project.stop["main"]}])
    assert len(inputs["images"]) == 4
    with pytest.raises(ProjectError, match="describe what changes, or add a reference"):
        tools.entity_inputs(project, project.aoi["id"], "angle", "")


def test_several_sheets_and_the_one_in_use_follows_the_main_look(project, tmp_path):
    e = project.aoi
    sheets = []
    for n, of in (("s1", e["main"]), ("s2", project.coat["id"])):
        (tmp_path / f"{n}.png").write_bytes(PNG)
        a = project.add_asset(tmp_path / f"{n}.png", role="character", title=n)
        project.add_to_entity(a["id"], e["id"], look="sheet")
        a["sheet"], a["sheet_of"] = True, of
        sheets.append(a)
    project.set_entity_sheet(e["id"], sheets[0]["id"]); project.update_entity(e["id"], ref_use="sheet")
    assert [a["id"] for a in project.entity_sheets(e["id"])] == [s["id"] for s in sheets]
    project.set_entity_main(e["id"], project.coat["id"])       # the winter coat becomes the main look …
    assert project.entities[e["id"]]["sheet"] == sheets[1]["id"]   # … and scenes get the winter coat's sheet
    assert project.data["library"]["items"][e["ref"]]["asset"] == sheets[1]["id"]


def test_the_description_goes_in_only_when_it_matches_the_picture(project):
    angle, _ = tools.entity_inputs(project, project.aoi["id"], "angle", "from the side")
    assert "(Aoi: a girl in a yellow raincoat)" in angle["prompt"]                    # the main look: it matches
    sheet, _ = tools.entity_inputs(project, project.aoi["id"], "sheet", "", source=project.coat["id"])
    assert "(Aoi, winter coat)" in sheet["prompt"] and "raincoat" not in sheet["prompt"]   # another look: name and label
    swap, _ = tools.entity_inputs(project, project.aoi["id"], "angle", "sitting", refs=[{"asset": project.coat["id"], "use": "outfit"}])
    assert "raincoat" not in swap["prompt"]                                            # its clothes come from the reference


def test_the_assistant_writes_a_generation_from_plain_words(project, monkeypatch):
    seen = {}

    class Fake:
        sees_images = True

        def __init__(self, provider):
            pass

        def chat(self, messages, **kw):
            seen["content"] = messages[0]["content"]
            return {"content": 'Here: {"change": "wearing a red wool jacket", "name": "red jacket", '
                               '"prompt": "The same character as image 1 (Aoi), now wearing the jacket from image 2 in red wool.  Keep the face.", '
                               '"explain": "Took the jacket shape from image 2 and made it red."}'}
    monkeypatch.setattr(assistant, "Client", Fake)
    from PIL import Image
    for a in (project.aoi["main"], project.coat["id"]):
        Image.new("RGB", (32, 32), "red").save(project.root / project.assets[a]["file"], format="PNG")
    out = assistant.write_image_prompt({"vision": True}, project, project.aoi["id"], "a jacket like that one but red",
                                       refs=[{"asset": project.coat["id"], "use": "outfit"}])
    assert out == {"change": "wearing a red wool jacket", "name": "red jacket", "explain": "Took the jacket shape from image 2 and made it red.",
                   "prompt": "The same character as image 1 (Aoi), now wearing the jacket from image 2 in red wool. Keep the face.", "images_seen": True}
    texts = [p["text"] for p in seen["content"] if p["type"] == "text"]
    assert texts[0] == "Image 1 (Aoi, default):" and texts[1] == "Image 2 (Aoi, winter coat):"   # it sees the images, numbered as sent
    assert "do NOT repeat the old description" in texts[-1] and "a jacket like that one but red" in texts[-1]
    with pytest.raises(ProjectError, match="own words"):
        assistant.write_image_prompt({"vision": True}, project, project.aoi["id"], "  ")


def test_the_assistant_writes_a_description_from_plain_words(project, monkeypatch):
    class Fake:
        sees_images = True

        def __init__(self, provider):
            pass

        def chat(self, messages, **kw):
            assert "a shy boy with a camera" in messages[0]["content"]
            return {"content": '{"name": "Kenta", "description": "An 18-year-old boy,  slim, messy dark hair, round glasses, a vintage film camera on a strap."}'}
    monkeypatch.setattr(assistant, "Client", Fake)
    assert assistant.write_description({"vision": False}, project, "character", "a shy boy with a camera") == {
        "name": "Kenta", "description": "An 18-year-old boy, slim, messy dark hair, round glasses, a vintage film camera on a strap."}


def test_echoed_rules_are_taken_out_of_answers():
    assert assistant._clean_prompt("Same layout. No rain, no puddles. Plain light grey background. 40-110 words.", "place") == "Same layout. No rain, no puddles."
    assert assistant._look_name("foggy dawn bus stop", "Bus stop") == "foggy dawn"

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from comfyui_clapper.engine import tools  # noqa: E402
from comfyui_clapper.engine.project import Project, ProjectError  # noqa: E402
from test_tools import PNG, FakeComfy, nodes_of  # noqa: E402


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "anime", "Anime")
    for n in ("aoi", "stop", "loose"):
        (tmp_path / f"{n}.png").write_bytes(PNG)
    p.aoi_img = p.add_asset(tmp_path / "aoi.png", role="character", title="Aoi design")
    p.stop_img = p.add_asset(tmp_path / "stop.png", role="place", title="Bus stop")
    p.loose = p.add_asset(tmp_path / "loose.png", title="Loose")
    return p


def finish(project, c, job):
    out = project.root / job["dir"]; out.mkdir(parents=True); (out / "image_01.png").write_bytes(PNG)
    c.finish(job["prompt_id"]); tools.collect(project, c)
    return project.assets[project.data["jobs"][job["id"]]["outputs"][0]]


def test_entity_from_image_gets_ref_and_definition(project):
    e = project.create_entity("character", "Aoi", description="a 17-year-old girl with a short indigo bob", from_asset=project.aoi_img["id"])
    item = project.data["library"]["items"]["aoi"]
    assert e["main"] == project.aoi_img["id"] and item["asset"] == e["main"] and item["entity"] == e["id"]
    assert item["definition"] == "@aoi is Aoi: a 17-year-old girl with a short indigo bob"
    assert "aoi" in project.data["library"]["defaults"]
    project.update_entity(e["id"], description="a girl in a yellow raincoat")
    assert project.data["library"]["items"]["aoi"]["definition"].endswith("yellow raincoat")
    p2 = project.create_entity("place", "Bus stop", from_asset=project.stop_img["id"])
    assert p2["ref"] == "bus_stop" and "bus_stop" not in project.data["library"]["defaults"]   # places are not defaults


def test_ref_name_does_not_steal_an_existing_name(project):
    project.name_asset(project.loose["id"], "aoi")
    e = project.create_entity("character", "Aoi", from_asset=project.aoi_img["id"])
    assert e["ref"] == "aoi_2" and project.data["library"]["items"]["aoi"]["asset"] == project.loose["id"]


def test_new_look_uses_the_main_image_and_only_the_name(project):
    e = project.create_entity("character", "Aoi", description="indigo bob, amber eyes", from_asset=project.aoi_img["id"])
    c = FakeComfy()
    jobs = tools.queue_entity(project, c, e["id"], "look", "wearing a winter coat and a red scarf", count=2, seed=4)
    g = c.graphs[0]
    prompt = nodes_of(g, "CLIPTextEncode")[0]["inputs"]["text"]
    # the description describes the old look; with it, the change lost (tested): the picture carries who it is
    assert "The same character as image 1 (Aoi), now wearing a winter coat" in prompt and "indigo bob" not in prompt
    assert nodes_of(g, "ClapperLoadImage")[0]["inputs"]["path"].endswith(project.aoi_img["file"])
    a = finish(project, c, jobs[0])
    assert a["entity"] == e["id"] and a["look"] == "wearing a winter coat and a red scarf" and a["role"] == "character"
    assert [x["id"] for x in project.entity_looks(e["id"])] == [project.aoi_img["id"], a["id"]]
    project.set_entity_main(e["id"], a["id"])
    assert project.data["library"]["items"]["aoi"]["asset"] == a["id"]   # scenes now use the new look
    with pytest.raises(ProjectError, match="describe"):
        tools.queue_entity(project, c, e["id"], "look", "")


def test_first_design_then_main(project):
    e = project.create_entity("character", "Ren", description="a tall boy with messy black hair")
    c = FakeComfy()
    with pytest.raises(ProjectError, match="no main image"):
        tools.queue_entity(project, c, e["id"], "look", "in a suit")
    job = tools.queue_entity(project, c, e["id"], "first", "", count=1)[0]
    assert "a tall boy with messy black hair" in nodes_of(c.graphs[0], "CLIPTextEncode")[0]["inputs"]["text"]
    a = finish(project, c, job)
    assert project.entity(e["id"])["main"] == a["id"] and project.data["library"]["items"]["ren"]["asset"] == a["id"]


def test_character_in_a_place(project):
    e = project.create_entity("character", "Aoi", from_asset=project.aoi_img["id"])
    pl = project.create_entity("place", "Bus stop", description="a rural bus stop at dusk", from_asset=project.stop_img["id"])
    c = FakeComfy()
    job = tools.queue_entity(project, c, e["id"], "place", "sitting on the bench in the rain", place_id=pl["id"], count=1)[0]
    g = c.graphs[0]
    assert len(nodes_of(g, "ReferenceLatent")) == 2 and "place from image 2 (Bus stop: a rural bus stop at dusk)" in nodes_of(g, "CLIPTextEncode")[0]["inputs"]["text"]
    a = finish(project, c, job)
    assert a["role"] == "start" and a["uses"] == [e["id"], pl["id"]] and "entity" not in a
    with pytest.raises(ProjectError, match="place"):
        tools.queue_entity(project, c, e["id"], "place", "x", place_id=e["id"])


def test_voice_and_removal(project, tmp_path):
    (tmp_path / "v.wav").write_bytes(b"RIFF")
    v = project.add_asset(tmp_path / "v.wav", role="voice")
    e = project.create_entity("character", "Aoi", from_asset=project.aoi_img["id"])
    project.update_entity(e["id"], voice=v["id"])
    with pytest.raises(ProjectError, match="sound"):
        project.update_entity(e["id"], voice=project.loose["id"])
    project.remove_entity(e["id"])
    assert "aoi" not in project.data["library"]["items"] and "entity" not in project.aoi_img and project.aoi_img["id"] in project.assets


def test_taking_the_main_look_out_moves_main(project, tmp_path):
    e = project.create_entity("character", "Aoi", from_asset=project.aoi_img["id"])
    project.add_to_entity(project.loose["id"], e["id"], look="side view")
    project.add_to_entity(project.aoi_img["id"], None)
    assert project.entity(e["id"])["main"] == project.loose["id"] and project.data["library"]["items"]["aoi"]["asset"] == project.loose["id"]


def test_existing_name_is_adopted(project):
    project.name_asset(project.aoi_img["id"], "aoi")
    e = project.create_entity("character", "Aoi Sato", description="indigo bob", from_asset=project.aoi_img["id"])
    assert e["ref"] == "aoi" and project.data["library"]["items"]["aoi"]["entity"] == e["id"]
    assert project.data["library"]["items"]["aoi"]["definition"] == "@aoi is Aoi Sato: indigo bob"


def test_look_label_cuts_at_words():
    assert tools._label("wearing a long winter coat and a red knitted scarf") == "wearing a long winter coat and a red knitted…"
    assert tools._label("in a suit") == "in a suit"


def test_name_is_not_repeated_in_the_definition(project):
    e = project.create_entity("character", "Ren", description="Ren, a quiet 18-year-old boy", from_asset=project.aoi_img["id"])
    assert e["description"] == "a quiet 18-year-old boy"
    assert project.data["library"]["items"]["ren"]["definition"] == "@ren is Ren: a quiet 18-year-old boy"
    project.update_entity(e["id"], description="Ren: a tall boy")
    assert e["description"] == "a tall boy"
    assert project.create_entity("character", "Rena", description="Renaissance-style painter")["description"] == "Renaissance-style painter"


def test_entity_action_with_another_image_model_and_negative(project):
    e = project.create_entity("character", "Aoi", description="indigo bob, amber eyes", from_asset=project.aoi_img["id"])
    c = FakeComfy()
    job = tools.queue_entity(project, c, e["id"], "look", "wearing a winter coat", count=1, seed=4,
                             model="qwen_image_21_anime", negative="text, watermark")[0]
    enc = nodes_of(c.graphs[0], "TextEncodeQwenImage21")[0]["inputs"]
    assert "<image1>" in enc["prompt"] and enc["negative_prompt"] == "text, watermark" and "images.image_1" in enc
    assert job["inputs"]["model"] == "qwen_image_21_anime" and job["entity"] == e["id"]
    with pytest.raises(ProjectError, match="unknown image model"):
        tools.queue_entity(project, c, e["id"], "look", "x", model="nope")

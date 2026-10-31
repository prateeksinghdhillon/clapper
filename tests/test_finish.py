import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from comfyui_clapper.engine import finish  # noqa: E402
from comfyui_clapper.engine.project import Project, ProjectError  # noqa: E402
from test_tools import PNG  # noqa: E402


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "f", "F")
    (tmp_path / "logo.png").write_bytes(PNG)
    (tmp_path / "m.wav").write_bytes(b"RIFF")
    p.logo = p.add_asset(tmp_path / "logo.png", role="overlay")
    p.music = p.add_asset(tmp_path / "m.wav", role="music")
    return p


def test_normalise_clamps_and_checks(project):
    f = finish.set_finish(project, {"scene_volume": 5, "tracks": [{"asset": project.music["id"], "volume": -1, "start": 2}],
                                    "overlays": [{"asset": project.logo["id"], "width": 3, "end": ""}]})
    assert f["scene_volume"] == 2.0 and f["tracks"][0]["volume"] == 0 and f["tracks"][0]["duck"] is True
    assert f["overlays"][0]["width"] == 1 and f["overlays"][0]["end"] is None and f["overlays"][0]["position"] == "bottom-right"
    with pytest.raises(ProjectError, match="not an image"):
        finish.set_finish(project, {"overlays": [{"asset": project.music["id"]}]})
    with pytest.raises(ProjectError, match="no sound"):
        finish.set_finish(project, {"tracks": [{"asset": project.logo["id"]}]})
    with pytest.raises(ProjectError, match="position"):
        finish.set_finish(project, {"overlays": [{"asset": project.logo["id"], "position": "middle"}]})


def test_filters(project):
    f = finish.normalise(project, {"tracks": [{"asset": project.music["id"], "start": 1.5, "loop": True, "duck": True}],
                                   "overlays": [{"asset": project.logo["id"], "start": 1, "end": 4, "position": "top-left", "opacity": 0.5}]})
    inputs, parts, v, a = finish.filters(project, f, first_input=2, width=720, height=1280, total=10.0)
    graph = ";".join(parts)
    assert "-stream_loop" in inputs and inputs.count("-i") == 2 and v == "vo0" and a == "afin"
    assert "[2:v]format=rgba" in graph and "colorchannelmixer=aa=0.500" in graph and "enable='between(t,1.000,4.000)'" in graph
    assert "x='min(main_w,main_h)*0.04'" in graph   # quoted: the expression has a comma
    assert "adelay=delays=1500:all=1" in graph and "sidechaincompress" in graph and "amix=inputs=2:duration=first" in graph
    assert finish.is_empty(finish.finish_of(project))


def test_title_card(project):
    a = finish.title_card(project, "Rain Stop\nएक छोटी कहानी", size=64)
    assert a["role"] == "overlay" and a["kind"] == "image" and a["width"] > 100 and a["origin"]["type"] == "title"
    with pytest.raises(ProjectError, match="type the text"):
        finish.title_card(project, "  ")
    with pytest.raises(ProjectError, match="colour"):
        finish.title_card(project, "x", color="red")


def test_runs_keep_words_together():
    assert finish._runs("Rain Stop एक") == [(None, "Rain Stop "), ("hi", "एक")]

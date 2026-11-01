"""The assistant fills in the Image and Speech tools' forms (✨ Ask the assistant on Studio › New)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from comfyui_clapper.engine import assistant  # noqa: E402
from comfyui_clapper.engine.project import Project, ProjectError  # noqa: E402
from test_tools import PNG  # noqa: E402


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "s", "S")
    (tmp_path / "aoi.png").write_bytes(PNG)
    (tmp_path / "v.wav").write_bytes(b"RIFF0000WAVE")
    p.aoi = p.create_entity("character", "Aoi", description="a girl in a yellow raincoat",
                            from_asset=p.add_asset(tmp_path / "aoi.png", role="character", title="aoi")["id"])
    p.voice = p.add_asset(tmp_path / "v.wav", role="voice", title="Aoi voice")
    p.update_asset(p.voice["id"], transcript="Hi, I'm Aoi.")
    p.update_entity(p.aoi["id"], voice=p.voice["id"])
    return p


def fake(monkeypatch, answer, seen):
    class Fake:
        sees_images = False

        def __init__(self, provider):
            pass

        def chat(self, messages, **kw):
            c = messages[0]["content"]
            seen.append(c if isinstance(c, str) else c[-1]["text"])
            return {"content": answer}
    monkeypatch.setattr(assistant, "Client", Fake)


def test_image_prompt_for_the_chosen_model_with_negative(project, monkeypatch):
    seen = []
    fake(monkeypatch, '{"prompt": "An anime illustration of Aoi  waving .", "negative": "blurry,  text", "explain": "Done."}', seen)
    out = assistant.write_tool_image_prompt({}, project, "Aoi waving at the bus", model="qwen_image_21_anime", cast=[project.aoi["id"]])
    assert out == {"prompt": "An anime illustration of Aoi waving.", "negative": "blurry, text", "explain": "Done.",
                   "images_seen": False, "model": "qwen_image_21_anime"}
    assert "An anime illustration of" in seen[0] and "Image 1 is Aoi" in seen[0] and "negative" in seen[0]


def test_image_prompt_klein_has_no_negative(project, monkeypatch):
    seen = []
    fake(monkeypatch, '{"prompt": "A café at night, no text.", "negative": "x", "explain": ""}', seen)
    out = assistant.write_tool_image_prompt({}, project, "", prompt="cafe night", model="flux2_klein_9b")
    assert "negative" not in out and out["prompt"] == "A café at night, no text."
    assert "takes no negative prompt" in seen[0] and '"negative"' not in seen[0].split("Answer with only this JSON")[1]
    with pytest.raises(ProjectError):
        assistant.write_tool_image_prompt({}, project, "  ", prompt="")


def test_speech_line_carries_emotion_in_the_words(project, monkeypatch):
    seen = []
    fake(monkeypatch, '{"text": "(softly) \\"I— I missed you… more than I can say.\\"", "emotion": "shy, teary", '
                      '"temperature": 1.05, "lines": [], "explain": "Pauses and a stammer carry it."}', seen)
    out = assistant.write_speech_text({}, project, "she admits she missed him, shy", text="I missed you", voice=project.voice["id"])
    assert out["text"] == "I— I missed you… more than I can say." and out["temperature"] == 1.05 and out["emotion"] == "shy, teary"
    assert "no emotion setting" in seen[0] and "The speaker is Aoi" in seen[0] and "Hi, I'm Aoi." in seen[0] and "I missed you" in seen[0]


def test_speech_dialogue_lines_and_bounds(project, monkeypatch):
    fake(monkeypatch, '{"text": "", "temperature": 9, "lines": [{"speaker": "Aoi", "text": "Aoi: Hey! 😊"}, '
                      '{"speaker": "Ren", "text": "[laughs] Hi."}], "explain": ""}', [])
    out = assistant.write_speech_text({}, project, "a short greeting between Aoi and Ren")
    assert out["text"] == "Hey!" and out["temperature"] == 1.3
    assert out["lines"] == [{"speaker": "Aoi", "text": "Hey!"}, {"speaker": "Ren", "text": "Hi."}]
    with pytest.raises(ProjectError):
        assistant.write_speech_text({}, project, "", text="")


def test_negative_never_rules_out_what_the_prompt_asks_for():
    assert assistant._negative_without("no rain, blurry, plain light grey background, No text, blurry",
                                       "Lena in a café, rain streaking on the window, plain light grey background") == "blurry, text"

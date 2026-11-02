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
    assert out.pop("checks") == {"corrected": [], "remaining": [], "tips": []}
    assert out == {"prompt": "An anime illustration of Aoi waving.", "negative": "blurry, text", "explain": "Done.",
                   "images_seen": False, "model": "qwen_image_21_anime", "unknown_images": []}
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


def test_assistant_keeps_the_start_look_and_marks_the_pose(project, monkeypatch, tmp_path):
    from comfyui_clapper.engine import tools
    (tmp_path / "look.png").write_bytes(PNG)
    (tmp_path / "pose.png").write_bytes(PNG)
    look = project.add_asset(tmp_path / "look.png", role="character", title="red dress")
    project.update_asset(look["id"], look="red dress")
    project.data["assets"][look["id"]]["entity"] = project.aoi["id"]
    pose = project.add_asset(tmp_path / "pose.png", role="character", title="pose")
    seen = []
    fake(monkeypatch, '{"change": "sitting", "name": "sitting", "prompt": "Aoi from image 1, sitting on a chair.", "explain": ""}', seen)
    out = assistant.write_image_prompt({}, project, project.aoi["id"], "sitting like the pose", action="angle", source=look["id"],
                                       refs=[{"asset": pose["id"], "use": "pose"}])
    assert 'NOT its main look: it is its look "red dress"' in seen[0] and "do NOT write the clothes from the description" in seen[0]
    assert "pose skeleton" in seen[0] and out["prompt"].endswith(tools.POSE_LINE)


def test_assistant_is_corrected_when_it_names_a_picture_that_is_not_sent(project, monkeypatch):
    answers = iter(['{"change": "x", "name": "x", "prompt": "Aoi from image 1 wearing the coat from image 3.", "explain": ""}',
                    '{"change": "x", "name": "x", "prompt": "Aoi from the first image, now in a green coat.", "explain": ""}'])
    seen = []

    class Fake:
        sees_images = False

        def __init__(self, provider):
            pass

        def chat(self, messages, **kw):
            seen.append(messages)
            return {"content": next(answers)}
    monkeypatch.setattr(assistant, "Client", Fake)
    out = assistant.write_image_prompt({}, project, project.aoi["id"], "a green coat", action="look", model="qwen_image_21_realism")
    assert len(seen) == 2 and "mentions image 3" in seen[1][-1]["content"]
    assert out["prompt"] == "Aoi from image 1, now in a green coat." and out["unknown_images"] == [] and out["model"] == "qwen_image_21_realism"
    assert "Qwen-Image-2.1, photoreal" in seen[0][0]["content"] and "What this action makes: Image 1 is the subject" in seen[0][0]["content"]


def test_safeguards_on_what_the_assistant_writes():
    assert assistant._drop_stance("Kai stands in the black jacket, standing alone on a roof.", "Kai on a roof") == "Kai in the black jacket, on a roof."
    assert assistant._drop_stance("Lena sitting cross-legged.", "sitting like that") == "Lena sitting cross-legged."
    assert not assistant._real_prompt({"prompt": "the complete prompt for the image model"}) and not assistant._real_prompt({"prompt": "short"})
    assert assistant._real_prompt({"prompt": "A photo of Lena from image 1 in a navy suit, full body."})
    assert assistant._negative_for("qwen_image_21_anime", "blurry, outlines, flat style, cartoon, extra fingers", "x") == "blurry, extra fingers"
    assert assistant._drop_missing_images("Lena from image 1. Wearing the coat from image 3. Grey background.", 2) == "Lena from image 1. Grey background."
    assert assistant._json_answer('{"prompt": "A photo of Lena cut off before the end of the sentence because the') ["prompt"].startswith("A photo")


def test_the_prompt_checker_finds_what_was_seen_failing():
    L = assistant.lint_image_prompt
    base = dict(model_id="qwen_image_21_realism", n_images=1, has_pose=False, request="a navy suit", name="Lena",
                description="a woman with short black hair, wearing a mustard yellow raincoat and jeans")
    p, _ = L("The same character as image 1 (Lena), now a navy suit.", action="look", **base)
    assert any("start the prompt with the operation" in x for x in p)
    p, _ = L("Replace Lena's raincoat from image 1 with a navy suit.", action="look", **base)
    assert p == []
    p, _ = L("Lena from image 1 in her yellow raincoat, sitting.", action="angle", other_look=True, start_look="overalls",
             **{**base, "request": "like the pose picture", "has_pose": True})
    assert any("'raincoat'" in x for x in p) and any("no stance" in x for x in p)
    p, _ = L("Lena, keep the same face and hair, wearing the jacket from image 1.", action="first", **{**base, "request": "that jacket"})
    assert any("no picture of Lena" in x for x in p) and not any("hair as the description" in x for x in p)
    p, _ = L("Lena wearing the jacket from image 2, plain grey background.", action="place", **base)
    assert any("mentions image 2" in x for x in p) and any("no plain or grey background" in x for x in p)
    _, tips = L("Turn the café from image 1 into a rainy night.", action="look", **{**base, "request": "at night in the rain"})
    assert tips and "Klein" in tips[0]


def test_the_checker_on_limbs_and_mid_sentence_operations():
    L = assistant.lint_image_prompt
    p, _ = L("Kai on a rooftop, with arms down, looking at the city.", action="tool", model_id="qwen_image_21_anime", n_images=1,
             has_pose=True, request="Kai on a rooftop")
    assert any("arms down" in x for x in p)
    p, _ = L("A photo of the same café as image 1, turn the scene into a rainy night.", action="look", model_id="qwen_image_21_realism",
             n_images=1, has_pose=False, request="night")
    assert not any("operation" in x for x in p)


def test_limb_positions_are_removed_with_a_pose():
    assert assistant._drop_limbs("Kai (arms raised) on a roof, with arms down, smiling.", "Kai on a roof") == "Kai on a roof, smiling."
    assert assistant._drop_limbs("Kai with arms raised.", "arms raised like that") == "Kai with arms raised."

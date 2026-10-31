import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from comfyui_clapper.engine import assistant, llm  # noqa: E402
from comfyui_clapper.engine.project import Project, ProjectError  # noqa: E402

def S(action):
    """A scene text with the three H3 fields."""
    return f"integrated_multimodal_description: [Shot 1] {action}\noverall_soundscape: birds.\nnon_diegetic_music: N/A"


LOCAL = {"kind": "local", "model": "m", "base_url": "http://x/v1", "context": 1000, "vision": True}


@pytest.fixture
def project(tmp_path):
    from PIL import Image

    p = Project.create(tmp_path / "ad", "Café promo")
    for name in ("opening", "hero", "logo"):
        src = tmp_path / f"{name}.png"
        Image.new("RGB", (64, 48), "orange").save(src)
        p.add_reference(name, src, "image", default=name == "hero", definition=f"@{name} is the {name}.")
    p.data["start_image"] = "opening"
    p.save()
    p.add_scene(text="@opening café. @hero waves.", title="Open", join="cut")
    p.add_scene(text="@hero serves coffee.", title="Serve")
    return p


class FakeClient:
    """Plays back scripted model replies and records the requests."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.tools = []

    def chat(self, messages, **kw):
        self.requests.append(messages)
        self.tools.append(kw.get("tools"))
        reply = self.replies.pop(0)
        self.thinking = getattr(self, "thinking", []) + [kw.get("thinking")]
        return {"content": reply.get("content", ""), "reasoning": "", "finish_reason": reply.get("finish", "stop"),
                "tool_calls": [{"id": f"c{i}", "name": n, "arguments": json.dumps(a)} for i, (n, a) in enumerate(reply.get("tools", []))],
                "usage": {"prompt_tokens": reply.get("prompt_tokens", 100), "completion_tokens": 10}}


def make_agent(project, replies, provider=LOCAL):
    written = []

    def write(fn):
        p = Project.open(project.root)
        fn(p)
        written.append(True)

    agent = assistant.Assistant(provider, open=lambda: Project.open(project.root), write=write)
    agent.client = FakeClient(replies)
    return agent, written


def test_add_scenes_proposal_validates_and_applies(project):
    plan = assistant.plan_proposal(project, "add_scenes", {"after": 1, "scenes": [
        {"title": "Mid", "text": S("@hero dances."), "seconds": 5},
        {"title": "Logo", "text": S("@logo appears."), "refs": ["logo"], "join": "cut"}]})
    assert plan["summary"] == "Add 2 scenes after scene 1"
    assert len(project.scenes) == 2  # nothing written yet
    assistant.apply_proposal(project, plan)
    assert [s["title"] for s in project.scenes] == ["Open", "Mid", "Logo", "Serve"]
    assert project.scenes[2]["refs"] == ["logo"]


def test_proposals_refuse_what_would_not_render(project):
    with pytest.raises(ProjectError, match="not active in this scene: @logo"):
        assistant.plan_proposal(project, "edit_scene", {"scene": 2, "text": S("@logo spins.")})
    with pytest.raises(ProjectError, match="there is no scene 9"):
        assistant.plan_proposal(project, "remove_scene", {"scene": 9})
    with pytest.raises(ProjectError, match="unknown references: @ghost"):
        assistant.plan_proposal(project, "add_scenes", {"scenes": [{"title": "x", "text": S("y"), "refs": ["ghost"]}]})
    with pytest.raises(ProjectError, match="between 1 and 15"):
        assistant.plan_proposal(project, "edit_scene", {"scene": 1, "seconds": 40})


def test_edit_scene_resolves_ids_so_later_inserts_do_not_misdirect(project):
    plan = assistant.plan_proposal(project, "edit_scene", {"scene": 2, "text": S("@hero pours milk.")})
    project.add_scene(text="inserted", after=None, join="cut")
    project.move_scene(project.scenes[-1]["id"], 0)
    assistant.apply_proposal(project, plan)
    assert next(s for s in project.scenes if s["title"] == "Serve")["text"] == S("@hero pours milk.")
    assert plan["details"][0] == {"label": "text", "before": "@hero serves coffee.", "after": S("@hero pours milk.")}


def test_leaked_markup_is_removed_and_fields_get_their_own_lines(project):
    args = assistant.clean_args({"scenes": [{"title": "A", "text": "integrated_multimodal_description: [Shot 1] @hero waves. "
                                             "overall_soundscape: birds. non_diegetic_music: N/A</parameter>, "}]})
    plan = assistant.plan_proposal(project, "add_scenes", args)
    assert plan["details"][0]["after"] == ("integrated_multimodal_description: [Shot 1] @hero waves.\n"
                                           "overall_soundscape: birds.\nnon_diegetic_music: N/A")


def test_identical_new_scenes_are_refused_and_short_ones_flagged(project):
    with pytest.raises(ProjectError, match="new scenes 3 and 2 have the same text"):
        assistant.plan_proposal(project, "add_scenes", {"scenes": [{"title": "a", "text": S("@hero waves.")},
                                                                  {"title": "b", "text": S("Hold.")}, {"title": "c", "text": S("hold.")}]})
    plan = assistant.plan_proposal(project, "add_scenes", {"scenes": [{"title": "a", "text": S("@hero bows."), "seconds": 1}]})
    assert "shorter than 3 seconds" in plan["args"]["warning"]


def test_scene_text_needs_the_h3_fields_and_native_script(project):
    with pytest.raises(ProjectError, match="missing: overall_soundscape:, non_diegetic_music:"):
        assistant.plan_proposal(project, "edit_scene", {"scene": 2, "text": "integrated_multimodal_description: [Shot 1] @hero waves."})
    with pytest.raises(ProjectError, match="must start with \\[Shot 1\\]"):
        assistant.plan_proposal(project, "edit_scene", {"scene": 2, "text": S("x").replace("[Shot 1] ", "")})
    with pytest.raises(ProjectError, match="Hindi's own script"):
        assistant.plan_proposal(project, "edit_scene", {"scene": 2, "text": S("The narrator (S1) says: <d>[Hindi] Namaste dosto</d>")})
    assistant.plan_proposal(project, "edit_scene", {"scene": 2, "text": S("The narrator (S1) says: <d>[Hindi] नमस्ते दोस्तो</d>")})


def test_proposals_are_checked_together(project):
    style = assistant.plan_proposal(project, "edit_project", {"style": "Drawn like @hero."})
    style.update(status="pending")
    new = {"scenes": [{"title": "x", "text": S("@logo spins."), "refs": ["logo"]}]}
    assert assistant.plan_proposal(project, "add_scenes", new)["args"]["scenes"][0]["refs"] == ["logo"]  # alone: as given …
    together = assistant.plan_proposal(project, "add_scenes", new, [style])  # … with the waiting style text, which names @hero
    assert together["args"]["scenes"][0]["refs"] == ["logo", "hero"]
    assert "added @hero to the refs of new scene 1, because the style text names it" in together["args"]["warning"]
    scenes = assistant.plan_proposal(project, "add_scenes", new)
    scenes.update(status="pending")
    with pytest.raises(ProjectError, match="@hero comes from the style text, which is sent with every scene"):  # the other order
        assistant.plan_proposal(project, "edit_project", {"style": "Drawn like @hero."}, [scenes])
    add = assistant.plan_proposal(project, "add_scenes", {"scenes": [{"title": "x", "text": S("@hero jumps.")}]})
    assert add["args"]["total"] == 3
    with pytest.raises(ProjectError, match='repeats the new scene "x" in a proposal waiting for the user'):
        assistant.plan_proposal(project, "add_scenes", {"scenes": [{"title": "y", "text": S("@hero jumps.")}]}, [add])


def test_scene_structure_is_checked():
    check = assistant.check_scene_text
    check("The camera is on the counter.\n" + S("@hero waves."))  # one free line before the fields is fine
    for text, error in [
        ("Line one.\nLine two.\n" + S("x"), "only one short line"),
        (S("x") + "\noverall_soundscape: more birds.", "exactly once; repeated: overall_soundscape:"),
        ("overall_soundscape: birds.\nintegrated_multimodal_description: [Shot 1] x\nnon_diegetic_music: N/A", "in this order"),
        (S("x").replace("N/A", ""), "non_diegetic_music: is empty .write N/A"),
        (S("x").replace("[Shot 1]", "[Shot 2]"), r"must start with \[Shot 1\]"),
        (S("The narrator (S1) says: <d>[English] Hello there. The owner waves."), "not closed"),
        (S("The narrator (S1) says: <d>[English] Hi <d>[English] again</d>"), "not closed"),
        (S("The narrator (S1) says: Hi</d>"), "without an opening"),
        (S("The narrator (S1) says: <d>Hello</d>"), r"without a \[Language\] tag"),
    ]:
        with pytest.raises(ProjectError, match=error):
            check(text)


def test_slips_are_repaired_without_changing_words():
    norm = assistant.normalize_scene_text
    closing = S("The narrator (S1) says: <d>[Hindi] नमस्ते<d> The owner waves. Then: <d>[Hindi] चलो<d>")
    assert norm(closing) == S("The narrator (S1) says: <d>[Hindi] नमस्ते</d> The owner waves. Then: <d>[Hindi] चलो</d>")
    assert norm(S("(S1) says: <d>[English] Hi</ d> and <D>[English] bye<\\d>")) == S("(S1) says: <d>[English] Hi</d> and <d>[English] bye</d>")
    ambiguous = S("(S1) says: <d>[English] Hi <d>[English] again<d>")  # a new line starts before the first one closed
    assert norm(ambiguous) == ambiguous
    messy = "The camera is on the door. integrated_multimodal_description: [shot 1] x\n**Overall Soundscape:** birds.\nNon-diegetic music: N/A"
    assert norm(messy) == "The camera is on the door.\n" + S("x")
    assistant.check_scene_text(norm(messy))
    assert norm("Birds sing; the overall soundscape: calm.") == "Birds sing; the overall soundscape: calm."  # only at a line start


def test_scene_copies_are_refused(project):
    project.update_scene(project.scenes[1]["id"], text=S("@hero serves coffee to a smiling customer at the window table."))
    near = S("@hero serves coffee to a smiling customer at the window  table!")
    with pytest.raises(ProjectError, match='new scene 2 is nearly the same as scene 2 "Serve"'):
        assistant.plan_proposal(project, "add_scenes", {"scenes": [{"title": "a", "text": S("@hero bows.")}, {"title": "b", "text": near}]})
    with pytest.raises(ProjectError, match='repeats scene 2 "Serve"'):
        assistant.plan_proposal(project, "add_scenes", {"scenes": [{"title": "b", "text": S("@HERO serves coffee to a smiling customer at the window table.")}]})
    assistant.plan_proposal(project, "add_scenes", {"scenes": [{"title": "c", "text": S("@hero serves tea to a tired old man by the door.")}]})
    assert assistant.similarity("A b  C", "a B c") == 1.0 and assistant.similarity("a", "") == 0.0


def test_apply_refuses_what_would_break_the_project(project):
    add = assistant.plan_proposal(project, "add_scenes", {"scenes": [{"title": "x", "text": S("@logo spins."), "refs": ["logo"]}]})
    style = assistant.plan_proposal(project, "edit_project", {"style": "Drawn like @hero."})  # checked alone: fine
    assistant.apply_proposal(project, add)
    with pytest.raises(ProjectError, match='not applied, because afterwards scene 3 "x": not active in this scene: @hero'):
        assistant.apply_proposal(project, style)
    assert Project.open(project.root).data["style"] == ""  # nothing written
    project.data["scenes"][0]["text"] = "@ghost café."  # a scene that was broken already does not block other changes
    project.save()
    assistant.apply_proposal(project, assistant.plan_proposal(project, "edit_project", {"style": "Flat 2D."}))
    assert Project.open(project.root).data["style"] == "Flat 2D."


def test_pending_proposals_are_chained_and_listed(project):
    agent, _ = make_agent(project, [{"tools": [("add_scenes", {"scenes": [{"title": "Logo", "text": S("@logo spins."), "refs": ["logo"]}]}),
                                               ("edit_project", {"style": "Drawn like @hero."}),
                                               ("edit_project", {"style": "Flat 2D."})]},
                                    {"content": "done"}])
    chat = assistant.new_chat(project)
    agent.run_turn(chat, "Add a logo scene")
    tools = [m["text"] for m in chat["messages"] if m["role"] == "tool"]
    assert "not active in this scene: @hero" in tools[1]  # checked together with the waiting scene
    assert tools[2].startswith("Proposed")
    assert sorted(p["status"] for p in chat["proposals"].values()) == ["pending", "pending"]
    text = assistant.snapshot(project, chat)
    assert 'waiting for the user' in text and '- Add 1 scene at the end: "Logo"' in text and "- Change the style text" in text


def test_join_slips_are_accepted(project):
    plan = assistant.plan_proposal(project, "edit_scene", {"scene": 2, "join": "Continuous"})
    assert plan["args"]["fields"]["join"] == "continue"
    with pytest.raises(ProjectError, match="got 'smooth'"):
        assistant.plan_proposal(project, "edit_scene", {"scene": 2, "join": "smooth"})


def test_corrected_add_scenes_in_the_same_answer_replaces_the_first(project):
    first = {"scenes": [{"title": "a", "text": S("@hero bows.")}, {"title": "b", "text": S("@hero leaves.")}]}
    fixed = {"scenes": [{"title": "a", "text": S("@hero bows.")}, {"title": "b", "text": S("@hero leaves, waving.")}]}
    agent, _ = make_agent(project, [{"tools": [("add_scenes", first), ("add_scenes", fixed)]}, {"content": "done"},
                                    {"tools": [("add_scenes", first)]}, {"content": "done"}])
    chat = assistant.new_chat(project)
    agent.run_turn(chat, "Add two scenes")
    statuses = [p["status"] for p in chat["proposals"].values()]
    assert statuses == ["replaced", "pending"]
    assert "It replaces your earlier proposal" in [m for m in chat["messages"] if m["role"] == "tool"][1]["text"]
    agent.run_turn(chat, "And again")  # a later answer does not replace: the copy is refused
    assert "ERROR: new scene 1 repeats the new scene \"a\"" in [m for m in chat["messages"] if m["role"] == "tool"][-1]["text"]


def test_edits_of_one_scene_in_one_answer_become_one_card(project):
    agent, _ = make_agent(project, [{"tools": [("edit_scene", {"scene": 2, "title": "Coffee"})]},
                                    {"tools": [("edit_scene", {"scene": 2, "text": S("@hero pours milk.")})]},
                                    {"content": "done"}])
    chat = assistant.new_chat(project)
    agent.run_turn(chat, "Rename scene 2 and make it about milk")
    first, second = chat["proposals"].values()
    assert first["status"] == "replaced" and second["status"] == "pending"
    assert second["args"]["fields"] == {"title": "Coffee", "text": S("@hero pours milk.")}
    assert [d["label"] for d in second["details"]] == ["title", "text"] and second["details"][0]["before"] == "Serve"


def test_only_repeats_end_the_tool_loop(project):
    agent, _ = make_agent(project, [{"tools": [("edit_scene", {"scene": 1, "title": "A"})]},
                                    {"tools": [("edit_scene", {"scene": 1, "title": "A"})]},
                                    {"tools": [("edit_scene", {"scene": 1, "title": "A"})]},
                                    {"content": "Proposed a new title."}])
    chat = assistant.new_chat(project)
    agent.run_turn(chat, "rename")   # two rounds of only repeats (one more chance to correct), then answer without tools
    assert agent.client.tools[:3] == [assistant.TOOLS] * 3 and agent.client.tools[3] is None
    assert chat["messages"][-1]["text"] == "Proposed a new title."


def test_reasoning_that_runs_out_is_retried_without_thinking(project):
    agent, _ = make_agent(project, [{"content": "Wait, let me count the beats again…", "finish": "length"},
                                    {"tools": [("edit_scene", {"scene": 1, "title": "A"})]}, {"content": "done"}])
    chat = assistant.new_chat(project)
    agent.run_turn(chat, "Plan it", thinking=1024)
    assert agent.client.thinking == [1024, False, 1024]
    assert [m["role"] for m in chat["messages"]] == ["user", "assistant", "tool", "assistant"]  # the cut-off reply is dropped


def test_numbers_of_waiting_scenes_get_an_explanation(project):
    agent, _ = make_agent(project, [{"tools": [("add_scenes", {"scenes": [{"title": "a", "text": S("@hero bows.")}]}),
                                               ("edit_scene", {"scene": 3, "title": "b"})]}, {"content": "done"}])
    chat = assistant.new_chat(project)
    agent.run_turn(chat, "Add a bow")
    error = [m["text"] for m in chat["messages"] if m["role"] == "tool"][1]
    assert error.startswith("ERROR: there is no scene 3") and "have no number yet" in error


def test_auto_thinking_only_for_planning(project):
    prov = {"thinking_budget": 1024}
    assert assistant.resolve_thinking("auto", project, "What does scene 2 show?", prov) is False
    assert assistant.resolve_thinking("auto", project, "Make scene 2 slower", prov) == 1024   # writing a scene: think
    assert assistant.resolve_thinking("auto", project, "Plan a 30-second video", prov) == 1024
    assert assistant.resolve_thinking("auto", project, "Rewrite all scenes in French", prov) == 1024
    project.data["scenes"] = []
    assert assistant.resolve_thinking("auto", project, "A café promo please", prov) == 1024
    assert assistant.resolve_thinking(512, project, "x", prov) == 512 and assistant.resolve_thinking(False, project, "Plan", prov) is False


def test_narration_and_style_warnings(project):
    plan = assistant.plan_proposal(project, "add_scenes", {"scenes": [{"title": "x", "text": S("@hero jumps.")}]})
    assert "no style text yet" in plan["args"]["warning"]
    project.data["style"] = "(S1) is the narrator."
    plan = assistant.plan_proposal(project, "add_scenes", {"scenes": [{"title": "x", "text": S("@hero jumps.")}]})
    assert "none of the new scenes has a spoken line" in plan["args"]["warning"] and "no style text yet" not in plan["args"]["warning"]


def test_missing_music_field_becomes_na():
    text = assistant.normalize_scene_text("integrated_multimodal_description: [Shot 1] a\noverall_soundscape: b")
    assert text.endswith("\nnon_diegetic_music: N/A")
    assistant.check_scene_text(text)


def test_repeated_fields_are_kept_once():
    text = "integrated_multimodal_description: a\noverall_soundscape: b\nnon_diegetic_music: c\n\noverall_soundscape: b again\nnon_diegetic_music: c again"
    assert assistant.normalize_scene_text(text) == "integrated_multimodal_description: a\noverall_soundscape: b\nnon_diegetic_music: c"


def test_edit_project_checks_names_and_accepts_null_like_start(project):
    with pytest.raises(ProjectError, match="references that do not exist: @style"):
        assistant.plan_proposal(project, "edit_project", {"style": "Drawn like @style."})
    with pytest.raises(ProjectError, match="not active in this scene: @opening"):  # "@null" means none, which breaks scene 1
        assistant.plan_proposal(project, "edit_project", {"style": "Flat 2D.", "start_image": "@null"})
    project.update_scene(project.scenes[0]["id"], text="A café.")
    plan = assistant.plan_proposal(project, "edit_project", {"style": "Flat 2D.", "start_image": "none"})
    assert plan["args"] == {"style": "Flat 2D.", "start_image": None}
    with pytest.raises(ProjectError, match="would change nothing"):
        assistant.plan_proposal(project, "edit_project", {"start_image": "opening", "default_refs": ["hero"]})


def test_identical_proposal_is_not_duplicated(project):
    agent, _ = make_agent(project, [{"tools": [("edit_scene", {"scene": 1, "title": "A"}), ("edit_scene", {"scene": 1, "title": "A"})]},
                                    {"content": "done"}])
    chat = assistant.new_chat(project)
    agent.run_turn(chat, "rename")
    assert len(chat["proposals"]) == 1
    assert "already proposed exactly this" in [m for m in chat["messages"] if m["role"] == "tool"][1]["text"]


def test_inactive_reference_error_explains_the_fix(project):
    with pytest.raises(ProjectError, match=r"its refs must list every @name .* defaults: @hero"):
        assistant.plan_proposal(project, "add_scenes", {"scenes": [{"title": "x", "text": S("@hero jumps."), "refs": []}]})


def test_render_proposal_calls_the_renderer(project):
    plan = assistant.plan_proposal(project, "render_scenes", {"scenes": [2, 1]})
    seen = []
    assistant.apply_proposal(project, plan, render=lambda p, ids: seen.append(ids) or "ok")
    assert seen == [[project.scenes[1]["id"], project.scenes[0]["id"]]]


def test_snapshot_rides_only_on_the_newest_user_message(project):
    chat = assistant.new_chat(project)
    chat["messages"] += [{"role": "user", "text": "first", "snapshot": "<project>A</project>"},
                         {"role": "assistant", "text": "ok", "tool_calls": []},
                         {"role": "event", "text": "The user applied: X."},
                         {"role": "user", "text": "second", "snapshot": "<project>B</project>"}]
    msgs = assistant.build_messages(project, chat, LOCAL)
    assert msgs[0]["role"] == "system" and "Prompt guide" in msgs[0]["content"]
    assert msgs[1]["content"] == "first"
    assert msgs[3]["content"].startswith("[Since your last message: The user applied: X.]") and msgs[3]["content"].endswith("# The user's message\nsecond")
    assert "<project>B</project>\n\n# The user's message" in msgs[3]["content"]   # project first, request last
    stable = assistant.build_messages(project, chat, LOCAL, with_snapshot=False)
    assert "<project>B" not in json.dumps(stable)
    assert stable[:3] == msgs[:3]  # everything before the newest message is identical → prompt cache reuse


def test_snapshot_describes_the_project(project):
    project.data["notes"] = "The fox never speaks."
    text = assistant.snapshot(project)
    assert "@opening (image) ▶" in text and "@hero (image) ★" in text
    assert "The fox never speaks." in text
    assert '1. "Open" · 5.0s · opens on the image @opening' in text and "2. \"Serve\"" in text


def test_turn_with_proposal_notes_and_reference_view(project):
    agent, written = make_agent(project, [
        {"tools": [("view_reference", {"name": "hero"}), ("update_notes", {"notes": "Fox mascot, playful."}),
                   ("edit_scene", {"scene": 2, "title": "Coffee"})]},
        {"content": "I proposed a new title for scene 2."}])
    chat = assistant.new_chat(project)
    agent.run_turn(chat, "Rename scene 2 to Coffee")
    roles = [m["role"] for m in chat["messages"]]
    assert roles == ["user", "assistant", "tool", "attachment", "tool", "tool", "assistant"]
    assert Project.open(project.root).data["notes"] == "Fox mascot, playful."  # notes apply at once
    assert Project.open(project.root).scenes[1]["title"] == "Serve"             # proposals do not
    prop = next(iter(chat["proposals"].values()))
    assert prop["status"] == "pending" and prop["summary"] == "Edit scene 2 (title)"
    second = agent.client.requests[1]
    assert any(isinstance(m["content"], list) and m["content"][0]["type"] == "image_url" for m in second)
    assert chat["title"] == "Rename scene 2 to Coffee"
    assert assistant.load_chat(project, chat["id"])["messages"][-1]["text"] == "I proposed a new title for scene 2."


def test_bad_tool_arguments_go_back_to_the_model(project):
    agent, _ = make_agent(project, [{"tools": [("edit_scene", {"scene": 7, "title": "x"})]}, {"content": "Sorry."}])
    chat = assistant.new_chat(project)
    agent.run_turn(chat, "edit")
    tool = next(m for m in chat["messages"] if m["role"] == "tool")
    assert tool["text"].startswith("ERROR: there is no scene 7")
    assert not chat["proposals"]


def test_compaction_summarises_old_turns_but_keeps_them(project):
    agent, _ = make_agent(project, [{"content": "- user wants a 30 s café promo"}])
    chat = assistant.new_chat(project)
    for i in range(12):
        chat["messages"] += [{"role": "user", "text": f"q{i}"}, {"role": "assistant", "text": f"a{i}", "tool_calls": []}]
    chat["context_tokens"] = 700  # over 60% of the 1000-token context
    assert agent.needs_compaction(chat)
    assert agent.compact(project, chat)
    assert chat["summary"] == "- user wants a 30 s café promo"
    assert chat["messages"][chat["summarized_upto"]]["role"] == "user"
    assert len(chat["messages"]) == 24  # nothing deleted
    msgs = assistant.build_messages(project, chat, LOCAL)
    assert "Summary of the earlier conversation" in msgs[0]["content"]
    assert len(msgs) == 1 + assistant.KEEP_RECENT


def test_settings_keep_keys_on_the_server(tmp_path):
    path = tmp_path / "llm.json"
    llm.update_settings({"providers": {"cloud": {"kind": "openai", "name": "Cloud", "base_url": "https://api.example/v1",
                                                 "model": "big", "api_key": "sk-secret-1234"}}, "active": "cloud"}, path)
    data = llm.load_settings(path)
    assert data["active"] == "cloud" and data["providers"]["local"]["model"] == "clapper-128k"
    public = llm.public_settings(data)
    assert public["providers"]["cloud"]["api_key"] == "" and public["providers"]["cloud"]["api_key_hint"] == "…1234"
    llm.update_settings({"providers": {"cloud": {"model": "bigger", "api_key": ""}}}, path)  # empty key keeps the old one
    assert llm.load_settings(path)["providers"]["cloud"]["api_key"] == "sk-secret-1234"
    with pytest.raises(llm.LLMError):
        llm.update_settings({"providers": {"local": {"remove": True}}}, path)


def test_thinking_and_sampling_settings(tmp_path):
    path = tmp_path / "llm.json"
    path.write_text(json.dumps({"providers": {"local": {"kind": "local", "base_url": "http://x/v1", "model": "m"}}}))
    assert llm.load_settings(path)["providers"]["local"]["thinking_budget"] == 2048  # defaults fill older files
    data = llm.update_settings({"providers": {"local": {"thinking": "auto", "thinking_budget": "1024",
                                                        "sampling": {"temperature": "0.7", "top_k": "20", "min_p": ""}}}}, path)
    local = data["providers"]["local"]
    assert local["thinking"] == "auto" and local["thinking_budget"] == 1024 and local["sampling"] == {"temperature": 0.7, "top_k": 20}
    with pytest.raises(llm.LLMError, match="unknown sampling parameter"):
        llm.update_settings({"providers": {"local": {"sampling": {"model": "evil"}}}}, path)
    client = llm.Client({**LOCAL, "sampling": {"temperature": 0.7}, "sampling_thinking": {"temperature": 0.6, "model": "x"}})
    body = client._body([], None, 100, 512, stream=False)
    assert body["temperature"] == 0.6 and body["model"] == "m" and body["thinking_budget_tokens"] == 512
    assert client._body([], None, 100, False, stream=False)["temperature"] == 0.7


def test_client_parses_streamed_tool_calls(monkeypatch):
    lines = [
        {"choices": [{"delta": {"reasoning_content": "hmm"}}]},
        {"choices": [{"delta": {"content": "Sure. "}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "a1", "function": {"name": "edit_scene", "arguments": "{\"scene\""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ": 2}"}}]}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 7}},
    ]
    body = [f"data: {json.dumps(x)}\n".encode() for x in lines] + [b"data: [DONE]\n"]

    class Resp(list):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(llm, "_request", lambda *a, **k: Resp(body))
    deltas = []
    out = llm.Client(LOCAL).chat([{"role": "user", "content": "hi"}], on_delta=lambda k, t: deltas.append(k))
    assert out["content"] == "Sure. " and out["reasoning"] == "hmm" and out["finish_reason"] == "tool_calls"
    assert out["tool_calls"] == [{"id": "a1", "name": "edit_scene", "arguments": "{\"scene\": 2}"}]
    assert out["usage"]["prompt_tokens"] == 50 and deltas == ["reasoning", "content"]


def test_continuing_a_named_take_also_proposes_selecting_it(project):
    s1 = project.scenes[0]["id"]
    t1 = project.new_take(s1, seed=1); project.update_take(t1["id"], status="done"); project.select_take(s1, t1["id"])
    t2 = project.new_take(s1, seed=2); project.update_take(t2["id"], status="done")
    agent, _ = make_agent(project, [{"tools": [("add_scenes", {"after": 1, "scenes": [{"title": "Next", "text": S("@hero sips.")}]})]},
                                    {"content": "done"}])
    chat = assistant.new_chat(project)
    agent.run_turn(chat, "Take 2 of scene 1 is the one I like; write the next scene continuing from it")
    cards = sorted((p["tool"], p["summary"]) for p in chat["proposals"].values())
    assert cards == [("add_scenes", "Add 1 scene after scene 1"), ("select_take", "Use Take 2 for scene 1 (the next scene then continues from it)")]
    first = [m for m in chat["messages"] if m["role"] == "tool"][0]["text"]
    assert "Clapper also proposed" in first and "scene 1 Take 2" in first   # the continuity check already uses Take 2
    # a cut after it, or a take that is already selected, needs no card
    assert assistant.named_take_to_select(project, "take 2 of scene 1", "add_scenes",
                                          {"after": s1, "scenes": [{"join": "cut"}]}) is None
    assert assistant.named_take_to_select(project, "take 1 of scene 1", "add_scenes", {"after": s1, "scenes": [{}]}) is None


def test_one_scene_written_flat_or_as_a_string_is_repaired(project):
    flat = {"after": 1, "title": "Next", "text": S("@hero sips."), "seconds": 5, "scenes": "[{\"title\": \"Ne"}
    plan = assistant.plan_proposal(project, "add_scenes", flat)
    assert plan["summary"] == "Add 1 scene after scene 1" and plan["args"]["scenes"][0]["title"] == "Next"
    as_text = {"after": 1, "scenes": json.dumps([{"title": "Next", "text": S("@hero sips.")}])}
    assert assistant.plan_proposal(project, "add_scenes", as_text)["args"]["scenes"][0]["title"] == "Next"
    assert assistant.asks_for_one_scene("write a continuation, scene number 3") and not assistant.asks_for_one_scene("add two scenes")


def test_a_tool_call_in_the_last_round_still_runs_and_extra_scenes_are_noted(project):
    three = {"after": 2, "scenes": [{"title": t, "text": S(f"@hero {t}.")} for t in ("sips", "smiles", "leaves")]}
    one = {"after": 2, "scenes": [{"title": "sips", "text": S("@hero sips.")}]}
    agent, _ = make_agent(project, [{"tools": [("add_scenes", three)]}, {"tools": [("add_scenes", three)]},
                                    {"tools": [("add_scenes", three)]}, {"tools": [("add_scenes", one)]}, {"content": "done"}])
    chat = assistant.new_chat(project)
    agent.run_turn(chat, "Write scene 3: the hero sips the coffee")
    tool_texts = [m["text"] for m in chat["messages"] if m["role"] == "tool"]
    assert "the user asked for one scene, and you proposed 3" in tool_texts[0]
    assert agent.client.tools[3] is None and len(tool_texts) == 4   # asked without tools, called anyway: it ran
    pending = [p for p in chat["proposals"].values() if p["status"] == "pending"]
    assert [p["summary"] for p in pending] == ["Add 1 scene after scene 2"]


def test_an_identical_continuing_scene_is_a_repeat(project):
    s1 = project.scenes[0]["id"]
    t1 = project.new_take(s1, seed=1); project.update_take(t1["id"], status="done"); project.select_take(s1, t1["id"])
    one = {"after": 1, "scenes": [{"title": "Next", "text": S("@hero sips.")}]}
    agent, _ = make_agent(project, [{"tools": [("add_scenes", one)]}, {"tools": [("add_scenes", one)]}, {"content": "done"}])
    chat = assistant.new_chat(project)
    agent.run_turn(chat, "add a scene")
    assert assistant.REPEAT in [m for m in chat["messages"] if m["role"] == "tool"][1]["text"]

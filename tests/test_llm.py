"""The OpenAI-compatible client against a small fake server, and the message order strict APIs require."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from comfyui_clapper.engine import assistant, llm  # noqa: E402
from comfyui_clapper.engine.project import Project  # noqa: E402


class Fake:
    """Answers /v1/chat/completions with scripted responses: (status, headers, body or [SSE chunks])."""

    def __init__(self):
        self.script, self.requests = [], []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append({"body": body, "headers": dict(self.headers)})
                status, headers, payload = fake.script.pop(0)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                if isinstance(payload, list):
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    for item in payload:
                        line = item if isinstance(item, str) else "data: " + json.dumps(item)
                        self.wfile.write((line + "\n\n").encode())
                else:
                    data = json.dumps(payload).encode()
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

        self.server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.close()


def delta(**d):
    return {"choices": [{"delta": d, "finish_reason": None}]}


DONE = [{"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 2}}, "data: [DONE]"]


def client(fake, **extra):
    return llm.Client({"kind": "openai", "base_url": fake.url + "/chat/completions", "model": "m", "api_key": "sk-test", **extra})


def test_base_url_forms():
    for u in ("https://x.ai/v1", "https://x.ai/v1/", "https://x.ai/v1/chat/completions", " https://x.ai/v1/completions "):
        assert llm.normalise_base_url(u) == "https://x.ai/v1"


def test_openai_fields_only_and_headers(fake):
    fake.script.append((200, {}, [delta(content="O"), delta(content="K"), *DONE]))
    c = client(fake, sampling={"temperature": 0.5, "top_k": 20, "min_p": 0.05})
    r = c.chat([{"role": "user", "content": "hi"}], max_tokens=50)
    body = fake.requests[0]["body"]
    assert r["content"] == "OK" and r["usage"]["prompt_tokens"] == 10
    assert body["temperature"] == 0.5 and "top_k" not in body and "min_p" not in body and "cache_prompt" not in body
    assert fake.requests[0]["headers"]["Authorization"] == "Bearer sk-test" and "Clapper" in fake.requests[0]["headers"]["User-Agent"]


def test_learns_max_completion_tokens(fake):
    fake.script += [(400, {}, {"error": {"message": "Unsupported parameter: 'max_tokens' is not supported with this model. Use 'max_completion_tokens' instead."}}),
                    (200, {}, [delta(content="ok"), *DONE])]
    r = client(fake).chat([{"role": "user", "content": "hi"}], max_tokens=50)
    assert r["content"] == "ok" and "max_completion_tokens" in fake.requests[1]["body"] and "max_tokens" not in fake.requests[1]["body"]


def test_retries_429_then_explains(fake, monkeypatch):
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    fake.script += [(429, {"Retry-After": "1"}, {"error": "slow down"}), (200, {}, [delta(content="ok"), *DONE])]
    assert client(fake).chat([{"role": "user", "content": "x"}])["content"] == "ok"
    fake.script += [(401, {}, {"error": {"message": "bad key"}})]
    with pytest.raises(llm.LLMError, match="API key was not accepted"):
        client(fake).chat([{"role": "user", "content": "x"}])


def test_reasoning_and_list_content(fake):
    fake.script.append((200, {}, [delta(reasoning="hmm "), delta(content=[{"type": "thinking", "thinking": [{"type": "text", "text": "more"}]},
                                                                            {"type": "text", "text": "Answer"}]), *DONE]))
    seen = []
    r = client(fake).chat([{"role": "user", "content": "x"}], on_delta=lambda k, t: seen.append(k))
    assert r["reasoning"] == "hmm more" and r["content"] == "Answer" and "reasoning" in seen


def test_cut_off_stream_is_an_error(fake):
    fake.script.append((200, {}, [delta(content="half an ans")]))
    with pytest.raises(llm.LLMError, match="stopped in the middle"):
        client(fake).chat([{"role": "user", "content": "x"}])


def test_stop_drops_half_written_tool_calls(fake):
    fake.script.append((200, {}, [delta(tool_calls=[{"index": 0, "id": "c1", "function": {"name": "add_scenes", "arguments": '{"sce'}}]),
                                  delta(content="x"), *DONE]))
    n = {"i": 0}

    def stop():
        n["i"] += 1
        return n["i"] > 1
    r = client(fake).chat([{"role": "user", "content": "x"}], should_stop=stop)
    assert r["finish_reason"] == "stopped" and r["tool_calls"] == []


def test_key_is_not_sent_to_a_new_host(tmp_path):
    path = tmp_path / "llm.json"
    llm.update_settings({"providers": {"p1": {"kind": "openai", "base_url": "https://api.openai.com/v1", "model": "m", "api_key": "sk-secret"}}}, path)
    data = llm.update_settings({"providers": {"p1": {"base_url": "http://192.168.1.5:1234/v1"}}}, path)
    assert data["providers"]["p1"]["api_key"] == ""
    with pytest.raises(llm.LLMError, match="context"):
        llm.update_settings({"providers": {"p1": {"context": "lots"}}}, path)
    data = llm.update_settings({"providers": {"local": {"kind": "openai"}}}, path)
    assert data["providers"]["local"]["kind"] == "local"


# ------------------------------------------------------------------ message order for strict APIs

@pytest.fixture
def project(tmp_path):
    return Project.create(tmp_path / "p", "P")


def _chat(*messages):
    return {"id": "c", "messages": list(messages), "summarized_upto": 0}


def test_images_from_tools_come_after_all_replies(project):
    chat = _chat({"role": "user", "text": "look"},
                 {"role": "assistant", "text": "", "tool_calls": [{"id": "a", "name": "view_reference", "arguments": "{}"},
                                                                  {"id": "b", "name": "view_reference", "arguments": "{}"}]},
                 {"role": "tool", "tool_call_id": "a", "text": "ok"}, {"role": "attachment", "images": [], "text": "(images from a)"},
                 {"role": "tool", "tool_call_id": "b", "text": "ok"}, {"role": "attachment", "images": [], "text": "(images from b)"},
                 {"role": "assistant", "text": "I see them"})
    roles = [m["role"] for m in assistant.build_messages(project, chat, {"kind": "openai"})]
    assert roles == ["system", "user", "assistant", "tool", "tool", "user", "user", "assistant"]


def test_unanswered_tool_calls_get_a_reply_and_empty_answers_are_skipped(project):
    chat = _chat({"role": "user", "text": "plan"},
                 {"role": "assistant", "text": "", "tool_calls": [{"id": "x", "name": "add_scenes", "arguments": '{"scenes": []}'}]},
                 {"role": "assistant", "text": ""},
                 {"role": "user", "text": "again"})
    msgs = assistant.build_messages(project, chat, {"kind": "openai"})
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool", "user"]
    assert msgs[2]["content"] is None and msgs[3]["tool_call_id"] == "x"
    local = assistant.build_messages(project, chat, {"kind": "local"})
    assert local[2]["content"] == ""   # llama.cpp chat templates expect a string


def test_estimate_tokens():
    assert assistant.estimate_tokens([{"role": "user", "content": "x" * 350}]) == 100

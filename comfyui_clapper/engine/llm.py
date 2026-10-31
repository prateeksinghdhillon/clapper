"""Language-model providers for the assistant. Pure Python (no torch), standard library only.

Every provider speaks the OpenAI chat-completions API (llama.cpp / llama-swap, LM Studio, Ollama,
vLLM, OpenAI, OpenRouter, Groq, …). A provider of kind "local" is a model served on this machine
through llama-swap: it shares the GPU with ComfyUI, so it can be loaded, ejected, and its
conversation cache saved to disk and restored (LocalRunner).

Settings live in ~/.clapper/llm.json; API keys never leave the server.
"""
from __future__ import annotations

import copy
import json
import os
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path

SETTINGS = Path.home() / ".clapper" / "llm.json"

DEFAULT_SETTINGS = {
    "active": "local",
    "gpu_mode": "auto",   # auto: load/eject the local model as needed · manual: only the buttons do
    "providers": {
        "local": {
            "kind": "local", "name": "Local model (llama-swap)",
            "base_url": "http://127.0.0.1:8080/v1", "swap_url": "http://127.0.0.1:8080",
            "model": "clapper-128k", "api_key": "", "context": 131072, "vision": True,
            "slot_file": "clapper-assistant.bin",
            "thinking": "auto", "thinking_budget": 2048, "sampling": {}, "sampling_thinking": {},
        },
    },
}
KINDS = ("local", "openai")
SAMPLING_KEYS = ("temperature", "top_p", "top_k", "min_p", "presence_penalty", "repeat_penalty")


class LLMError(RuntimeError):
    pass


def load_settings(path: Path = SETTINGS) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    out = copy.deepcopy(DEFAULT_SETTINGS)
    out.update({k: v for k, v in data.items() if k != "providers"})
    for pid, prov in (data.get("providers") or {}).items():  # new default keys reach providers saved earlier
        out["providers"][pid] = {**DEFAULT_SETTINGS["providers"].get(pid, {}), **prov}
    return out


def save_settings(data: dict, path: Path = SETTINGS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(3)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # holds API keys
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def public_settings(data: dict) -> dict:
    """Settings for the browser: API keys replaced by a hint."""
    out = copy.deepcopy(data)
    for prov in out["providers"].values():
        key = prov.get("api_key") or ""
        prov["api_key"] = ""
        prov["api_key_set"] = bool(key)
        prov["api_key_hint"] = f"…{key[-4:]}" if len(key) > 8 else ("set" if key else "")
    return out


def _thinking_mode(value):
    """False (answer directly), "auto" (think only when planning a video) or a token budget for every turn."""
    if value in (False, None, "", "off", 0, "0"):
        return False
    if value == "auto":
        return "auto"
    try:
        return max(64, int(value))
    except (TypeError, ValueError):
        raise LLMError("thinking must be off, auto or a token budget") from None


def _sampling(value) -> dict:
    """Sampling parameters sent with every request; empty ones are left to the server."""
    out = {}
    for key, v in (value or {}).items():
        if key not in SAMPLING_KEYS:
            raise LLMError(f"unknown sampling parameter {key!r} (known: {', '.join(SAMPLING_KEYS)})")
        if v not in (None, ""):
            try:
                out[key] = int(v) if key == "top_k" else float(v)
            except (TypeError, ValueError):
                raise LLMError(f"{key} must be a number") from None
    return out


def update_settings(changes: dict, path: Path = SETTINGS) -> dict:
    """Merge browser changes. An empty api_key keeps the stored key; `remove: true` deletes a provider."""
    data = load_settings(path)
    if changes.get("gpu_mode") in ("auto", "manual"):
        data["gpu_mode"] = changes["gpu_mode"]
    for pid, prov in (changes.get("providers") or {}).items():
        if prov.get("remove"):
            if pid == "local":
                raise LLMError("the local provider cannot be removed")
            data["providers"].pop(pid, None)
            continue
        cur = data["providers"].get(pid, {"kind": "openai", "api_key": ""})
        for key in ("name", "base_url", "swap_url", "model", "context", "vision", "kind", "slot_file"):
            if key in prov:
                cur[key] = prov[key]
        if prov.get("api_key"):
            cur["api_key"] = prov["api_key"]
        if "thinking" in prov:
            cur["thinking"] = _thinking_mode(prov["thinking"])
        if prov.get("thinking_budget"):
            cur["thinking_budget"] = max(64, int(prov["thinking_budget"]))
        for key in ("sampling", "sampling_thinking"):
            if key in prov:
                cur[key] = _sampling(prov[key])
        if cur.get("kind") not in KINDS:
            raise LLMError(f"provider kind must be one of {KINDS}")
        if not cur.get("base_url") or not cur.get("model"):
            raise LLMError("a provider needs a base URL and a model")
        data["providers"][pid] = cur
    if changes.get("active"):
        if changes["active"] not in data["providers"]:
            raise LLMError(f"unknown provider {changes['active']}")
        data["active"] = changes["active"]
    save_settings(data, path)
    return data


# ------------------------------------------------------------------ HTTP

def _request(url: str, body: dict | None = None, *, key: str = "", timeout: float = 600, method: str | None = None):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data, headers, method=method or ("POST" if body is not None else "GET"))
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:600]
        raise LLMError(f"{url} answered HTTP {exc.code}: {detail}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise LLMError(f"cannot reach {url}: {getattr(exc, 'reason', exc)}") from None


def _json(url: str, body: dict | None = None, **kw):
    with _request(url, body, **kw) as resp:
        raw = resp.read()
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace")


class Client:
    """OpenAI-compatible chat client for one provider."""

    def __init__(self, provider: dict):
        self.p = provider
        self.base = provider["base_url"].rstrip("/")

    @property
    def local(self) -> bool:
        return self.p.get("kind") == "local"

    def _body(self, messages, tools, max_tokens, thinking, stream) -> dict:
        body = {"model": self.p["model"], "messages": messages, "max_tokens": max_tokens, "stream": stream}
        if tools:
            body["tools"] = tools
        if stream:
            body["stream_options"] = {"include_usage": True}
        for key in ("sampling", "sampling_thinking") if thinking else ("sampling",):
            body.update({k: v for k, v in (self.p.get(key) or {}).items() if k in SAMPLING_KEYS and v is not None})
        if self.local:  # llama.cpp: thinking on/off per request, optional budget
            body["chat_template_kwargs"] = {"enable_thinking": bool(thinking)}
            if isinstance(thinking, int) and not isinstance(thinking, bool):
                body["thinking_budget_tokens"] = thinking
            body["cache_prompt"] = True
        return body

    def chat(self, messages: list, *, tools: list | None = None, max_tokens: int = 4096, thinking=False,
             on_delta=None, should_stop=None, timeout: float = 900) -> dict:
        """One completion, streamed. Returns {content, reasoning, tool_calls, finish_reason, usage}.
        on_delta(kind, text) is called with kind "content" / "reasoning" as text arrives."""
        body = self._body(messages, tools, max_tokens, thinking, stream=True)
        out = {"content": "", "reasoning": "", "tool_calls": [], "finish_reason": None, "usage": None}
        calls: dict[int, dict] = {}
        with _request(self.base + "/chat/completions", body, key=self.p.get("api_key", ""), timeout=timeout) as resp:
            for raw in resp:
                if should_stop and should_stop():
                    out["finish_reason"] = "stopped"
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                if chunk.get("error"):
                    raise LLMError(str(chunk["error"])[:600])
                if chunk.get("usage"):
                    out["usage"] = chunk["usage"]
                if chunk.get("timings"):
                    out["timings"] = chunk["timings"]
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    for kind, key in (("content", "content"), ("reasoning", "reasoning_content")):
                        if delta.get(key):
                            out[kind] += delta[key]
                            if on_delta:
                                on_delta(kind, delta[key])
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", len(calls)), {"id": "", "name": "", "arguments": ""})
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function") or {}
                        slot["name"] += fn.get("name") or ""
                        slot["arguments"] += fn.get("arguments") or ""
                    if choice.get("finish_reason"):
                        out["finish_reason"] = choice["finish_reason"]
        out["tool_calls"] = [{"id": c["id"] or f"call_{secrets.token_hex(4)}", "name": c["name"], "arguments": c["arguments"]}
                             for _, c in sorted(calls.items())]
        return out


# ------------------------------------------------------------------ local model through llama-swap

class LocalRunner:
    """Load / eject a llama-swap model and save / restore its conversation cache (slot 0)."""

    def __init__(self, provider: dict):
        self.p = provider
        self.swap = provider.get("swap_url", "").rstrip("/")
        self.model = provider["model"]
        self.up = f"{self.swap}/upstream/{self.model}"

    def running(self) -> list[str]:
        try:
            data = _json(self.swap + "/running", timeout=5)
        except LLMError:
            return []
        return [m.get("model") for m in (data.get("running") or []) if isinstance(m, dict)]

    def reachable(self) -> bool:
        try:
            _json(self.swap + "/v1/models", timeout=5)
            return True
        except LLMError:
            return False

    def loaded(self) -> bool:
        return self.model in self.running()

    def load(self, timeout: float = 600) -> float:
        """Start the model (llama-swap starts it on the first upstream request). Returns seconds taken."""
        started = time.time()
        _json(self.up + "/health", timeout=timeout)
        return round(time.time() - started, 1)

    def unload(self) -> None:
        _json(self.swap + "/api/models/unload", {}, timeout=60)

    def apply_template(self, messages: list, tools: list | None = None, thinking=False) -> str:
        body = {"messages": messages, "chat_template_kwargs": {"enable_thinking": bool(thinking)}}
        if tools:
            body["tools"] = tools
        data = _json(self.up + "/apply-template", body, timeout=60)
        return data["prompt"] if isinstance(data, dict) else ""

    def prime(self, messages: list, tools: list | None = None) -> int:
        """Fill the cache with exactly the text the next request will start with, so a saved cache can be
        reused after a reload (this hybrid-attention model cannot rewind even one token). The next request
        adds a user message, so the shared part is where two different next user messages diverge."""
        a = self.apply_template(messages + [{"role": "user", "content": "A"}], tools)
        b = self.apply_template(messages + [{"role": "user", "content": "B"}], tools)
        n = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
        prefix = a[: a.rfind("\n", 0, n) + 1]  # end on a line boundary, never inside a token
        if not prefix:
            return 0
        data = _json(self.up + "/completion", {"prompt": prefix, "n_predict": 0, "cache_prompt": True}, timeout=900)
        return int((data.get("timings") or {}).get("prompt_n", 0)) if isinstance(data, dict) else 0

    def save(self, filename: str) -> int:
        data = _json(self.up + "/slots/0?action=save", {"filename": filename}, timeout=300)
        return int(data.get("n_saved", 0)) if isinstance(data, dict) else 0

    def restore(self, filename: str) -> int:
        data = _json(self.up + "/slots/0?action=restore", {"filename": filename}, timeout=300)
        return int(data.get("n_restored", 0)) if isinstance(data, dict) else 0

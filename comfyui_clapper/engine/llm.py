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
import re
import secrets
import time
import urllib.error
import urllib.parse
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
OPENAI_SAMPLING = ("temperature", "top_p", "presence_penalty")   # the ones every OpenAI-compatible API accepts
USER_AGENT = "Clapper (+https://github.com/prateeksinghdhillon/clapper)"
RETRIES = 3                 # for 429 / 5xx / dropped connections, with backoff


def normalise_base_url(url: str) -> str:
    """'https://host/v1/', 'https://host/v1/chat/completions' and 'https://host/v1' all become 'https://host/v1'."""
    url = (url or "").strip().rstrip("/")
    for tail in ("/chat/completions", "/completions"):
        if url.endswith(tail):
            url = url[: -len(tail)]
    return url.rstrip("/")


def _host(url: str) -> str:
    return urllib.parse.urlparse(normalise_base_url(url)).netloc.lower()


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
        cur = data["providers"].get(pid, {"kind": "openai", "api_key": "", "vision": False})
        old_host = _host(cur.get("base_url", "")) if cur.get("base_url") else None
        for key in ("name", "base_url", "swap_url", "model", "vision", "slot_file", "max_tokens_field"):
            if key in prov:
                cur[key] = prov[key]
        if "kind" in prov and pid != "local":   # the local provider stays local
            cur["kind"] = prov["kind"]
        if "base_url" in prov:
            cur["base_url"] = normalise_base_url(prov["base_url"])
            # a stored key is never sent to another host: changing the address needs the key again
            if old_host and _host(cur["base_url"]) != old_host and not prov.get("api_key"):
                cur["api_key"] = ""
        if "context" in prov:
            try:
                cur["context"] = max(2048, int(prov["context"]))
            except (TypeError, ValueError):
                raise LLMError("context must be a number of tokens, e.g. 131072") from None
        if prov.get("api_key"):
            cur["api_key"] = prov["api_key"]
        if "thinking" in prov:
            cur["thinking"] = _thinking_mode(prov["thinking"])
        if prov.get("thinking_budget"):
            try:
                cur["thinking_budget"] = max(64, int(prov["thinking_budget"]))
            except (TypeError, ValueError):
                raise LLMError("the thinking budget must be a number of tokens") from None
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

class HTTPError(LLMError):
    def __init__(self, message: str, status: int, detail: str):
        super().__init__(message)
        self.status, self.detail = status, detail


_HINTS = {
    401: "the API key was not accepted — check it in Assistant settings",
    402: "the account has no credit left — add credit with the provider, or use another provider",
    403: "the API key is not allowed to use this model (or this account has no access)",
    404: "not found — check the base URL (most providers end in /v1) and the model name",
    413: "the request is too large for this provider or plan — use a provider with a larger limit",
    429: "the provider's rate limit was reached — wait a minute, or use another provider",
}
CONTEXT_ERROR = re.compile(r"context[_ ]length|maximum context|context window|too many tokens|prompt is too long|reduce the length", re.I)


def _request(url: str, body: dict | None = None, *, key: str = "", timeout: float = 600, method: str | None = None,
             retries: int = 0, should_stop=None):
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data, headers, method=method or ("POST" if body is not None else "GET"))
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:800]
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            if (exc.code == 429 or exc.code >= 500) and attempt < retries and not (should_stop and should_stop()):
                time.sleep(min(30.0, float(retry_after) if retry_after and retry_after.isdigit() else 2.0 * 2 ** attempt))
                continue
            hint = _HINTS.get(exc.code) or ("the conversation is longer than the model's context window"
                                            if CONTEXT_ERROR.search(detail) else "")
            raise HTTPError(f"{url} answered HTTP {exc.code}" + (f" — {hint}" if hint else "") + f": {detail[:500]}",
                            exc.code, detail) from None
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            if attempt < retries and not (should_stop and should_stop()) and not isinstance(reason, (ConnectionRefusedError,)):
                time.sleep(2.0 * 2 ** attempt)
                continue
            raise LLMError(f"cannot reach {url}: {reason}") from None


def _json(url: str, body: dict | None = None, **kw):
    with _request(url, body, **kw) as resp:
        raw = resp.read()
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace")


class Client:
    """OpenAI-compatible chat client for one provider (llama.cpp, OpenAI, OpenRouter, Groq, Mistral, DeepSeek,
    Together, Gemini's and Anthropic's OpenAI-compatible endpoints, LM Studio, Ollama, vLLM, …)."""

    def __init__(self, provider: dict):
        self.p = provider
        self.base = normalise_base_url(provider["base_url"])
        self._dropped: set[str] = set()   # request fields this provider rejected (learned per client)
        field = provider.get("max_tokens_field") or ("max_completion_tokens" if "api.openai.com" in self.base else "max_tokens")
        self._max_field = field

    @property
    def local(self) -> bool:
        return self.p.get("kind") == "local"

    def _body(self, messages, tools, max_tokens, thinking, stream) -> dict:
        body = {"model": self.p["model"], "messages": messages, self._max_field: max_tokens, "stream": stream}
        if tools:
            body["tools"] = tools
        if stream:
            body["stream_options"] = {"include_usage": True}
        allowed = SAMPLING_KEYS if self.local else OPENAI_SAMPLING   # top_k, min_p, … are llama.cpp-only
        for key in ("sampling", "sampling_thinking") if thinking else ("sampling",):
            body.update({k: v for k, v in (self.p.get(key) or {}).items() if k in allowed and v is not None})
        if self.local:  # llama.cpp: thinking on/off per request, optional budget
            body["chat_template_kwargs"] = {"enable_thinking": bool(thinking)}
            if isinstance(thinking, int) and not isinstance(thinking, bool):
                body["thinking_budget_tokens"] = thinking
            body["cache_prompt"] = True
        elif thinking and self.p.get("reasoning_effort"):   # opt-in: only reasoning models accept it
            body["reasoning_effort"] = self.p["reasoning_effort"]
        for key in self._dropped:
            body.pop(key, None)
        return body

    def _adapt(self, err: HTTPError, body: dict) -> bool:
        """Learn from a 400 about one request field and say whether a retry makes sense."""
        if err.status not in (400, 422):
            return False
        d = err.detail.lower()
        if "max_completion_tokens" in d and self._max_field == "max_tokens" and "max_tokens" in body:
            self._max_field = "max_completion_tokens"
            return True
        if "max_tokens" in d and "max_completion_tokens" not in d and self._max_field == "max_completion_tokens":
            self._max_field = "max_tokens"
            return True
        for key in ("stream_options", "temperature", "top_p", "presence_penalty", "reasoning_effort"):
            if key in body and key in d and key not in self._dropped:
                self._dropped.add(key)
                return True
        return False

    def chat(self, messages: list, *, tools: list | None = None, max_tokens: int = 4096, thinking=False,
             on_delta=None, should_stop=None, timeout: float = 900) -> dict:
        """One completion, streamed. Returns {content, reasoning, tool_calls, finish_reason, usage}.
        on_delta(kind, text) is called with kind "content" / "reasoning" as text arrives.
        A stopped request returns finish_reason "stopped" and no tool calls (they would be incomplete)."""
        for _ in range(4):
            body = self._body(messages, tools, max_tokens, thinking, stream=True)
            try:
                return self._stream(body, on_delta, should_stop, timeout)
            except HTTPError as err:
                if not self._adapt(err, body):
                    raise
        raise LLMError("the provider kept rejecting the request")

    def _stream(self, body, on_delta, should_stop, timeout) -> dict:
        out = {"content": "", "reasoning": "", "tool_calls": [], "finish_reason": None, "usage": None}
        calls: dict[int, dict] = {}
        done = False

        def emit(kind, text):
            if text:
                out[kind] += text
                if on_delta:
                    on_delta(kind, text)

        with _request(self.base + "/chat/completions", body, key=self.p.get("api_key", ""), timeout=timeout,
                      retries=0 if self.local else RETRIES, should_stop=should_stop) as resp:
            for raw in resp:
                if should_stop and should_stop():
                    out["finish_reason"] = "stopped"
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    done = True
                    break
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                if chunk.get("error"):
                    err = chunk["error"]
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    raise LLMError(f"the provider reported an error: {str(msg)[:600]}")
                if chunk.get("usage"):
                    out["usage"] = chunk["usage"]
                if chunk.get("timings"):
                    out["timings"] = chunk["timings"]
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if isinstance(content, list):   # e.g. Mistral: [{type: thinking|text, …}]
                        for part in content:
                            if not isinstance(part, dict):
                                continue
                            if part.get("type") == "thinking":
                                inner = part.get("thinking")
                                emit("reasoning", "".join(x.get("text", "") for x in inner) if isinstance(inner, list) else str(inner or ""))
                            else:
                                emit("content", part.get("text") or "")
                    elif isinstance(content, str):
                        emit("content", content)
                    for key in ("reasoning_content", "reasoning"):   # llama.cpp / DeepSeek, and OpenRouter / Groq / Ollama
                        if isinstance(delta.get(key), str):
                            emit("reasoning", delta[key])
                            break
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", len(calls)), {"id": "", "name": "", "arguments": ""})
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function") or {}
                        slot["name"] += fn.get("name") or ""
                        args = fn.get("arguments")
                        slot["arguments"] += args if isinstance(args, str) else (json.dumps(args) if args else "")
                    if choice.get("finish_reason"):
                        out["finish_reason"] = choice["finish_reason"]
        if out["finish_reason"] == "stopped":
            return out   # half-written tool calls are dropped: a chat must never keep a call without its reply
        if not done and out["finish_reason"] is None:
            raise LLMError("the answer stopped in the middle (the connection closed before it finished) — try again")
        out["tool_calls"] = [{"id": c["id"] or f"call_{secrets.token_hex(4)}", "name": c["name"], "arguments": c["arguments"]}
                             for _, c in sorted(calls.items()) if c["name"]]
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
        data = _json(self.up + "/completion", {"prompt": prefix, "n_predict": 0, "cache_prompt": True}, timeout=60)
        return int((data.get("timings") or {}).get("prompt_n", 0)) if isinstance(data, dict) else 0

    def save(self, filename: str) -> int:
        data = _json(self.up + "/slots/0?action=save", {"filename": filename}, timeout=300)
        return int(data.get("n_saved", 0)) if isinstance(data, dict) else 0

    def restore(self, filename: str) -> int:
        data = _json(self.up + "/slots/0?action=restore", {"filename": filename}, timeout=300)
        return int(data.get("n_restored", 0)) if isinstance(data, dict) else 0

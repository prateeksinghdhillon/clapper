"""Minimal ComfyUI HTTP client (standard library only)."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid

DEFAULT_URL = "http://127.0.0.1:8188"


class ComfyError(RuntimeError):
    pass


class ComfyClient:
    def __init__(self, url: str = DEFAULT_URL):
        self.url = url.rstrip("/")
        self.client_id = f"clapper-{uuid.uuid4().hex[:8]}"

    def _request(self, path: str, payload: dict | None = None) -> dict:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.url + path, data=data,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise ComfyError(f"{path}: HTTP {exc.code}: {exc.read().decode(errors='replace')[:2000]}") from exc

    def submit(self, graph: dict) -> str:
        result = self._request("/prompt", {"prompt": graph, "client_id": self.client_id})
        if result.get("node_errors"):
            raise ComfyError(f"graph rejected: {json.dumps(result['node_errors'])[:2000]}")
        return result["prompt_id"]

    def history(self, prompt_id: str) -> dict | None:
        return self._request(f"/history/{prompt_id}").get(prompt_id)

    def queue_state(self) -> dict:
        return self._request("/queue")

    def wait(self, prompt_id: str, *, poll: float = 3.0, timeout: float = 7200.0, on_poll=None) -> dict:
        started = time.time()
        while time.time() - started < timeout:
            entry = self.history(prompt_id)
            if entry is not None:
                status = entry.get("status", {})
                if status.get("status_str") == "error" or not status.get("completed", True):
                    msgs = [m for m in status.get("messages", []) if m[0] == "execution_error"]
                    detail = msgs[-1][1] if msgs else status
                    raise ComfyError(f"execution failed: {json.dumps(detail)[:3000]}")
                return entry
            if on_poll:
                on_poll(time.time() - started)
            time.sleep(poll)
        raise ComfyError(f"timed out after {timeout:.0f}s waiting for {prompt_id}")

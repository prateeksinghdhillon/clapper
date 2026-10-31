"""Live render progress, captured inside the ComfyUI process.

ComfyUI sends step progress only to the websocket of whoever queued the prompt. The Director
queues from the server side, so we observe PromptServer.send_sync and keep a small state that
the page polls (/clapper/api/live).
"""
from __future__ import annotations

import threading
import time
from collections import deque

PHASES = {
    "UNETLoader": "Loading models", "LoraLoaderModelOnly": "Loading models", "CLIPLoader": "Loading models",
    "VAELoader": "Loading models", "MiniMaxChunkFeedForward": "Loading models", "SpectrumApplyMiniMaxH3": "Loading models",
    "ClapperSceneConditioning": "Encoding prompt & references", "RandomNoise": "Preparing", "KSamplerSelect": "Preparing",
    "BasicScheduler": "Preparing", "BasicGuider": "Preparing", "SamplerCustomAdvanced": "Sampling",
    "ClapperSaveTake": "Decoding & saving",
}


class LiveState:
    def __init__(self):
        self._lock = threading.Lock()
        self.current: dict | None = None
        self.history: deque = deque(maxlen=50)

    def _class_type(self, prompt_id: str, node: str | None) -> str:
        try:
            from server import PromptServer

            for item in PromptServer.instance.prompt_queue.currently_running.values():
                if item[1] == prompt_id:
                    return item[2].get(str(node), {}).get("class_type", "")
        except Exception:  # noqa: BLE001 — progress display must never break execution
            pass
        return ""

    def observe(self, event: str, data) -> None:
        if not isinstance(data, dict):
            return
        now = time.time()
        pid = data.get("prompt_id")
        with self._lock:
            if event == "execution_start":
                self.current = {"prompt_id": pid, "started": now, "node": None, "class_type": "", "phase": "Starting",
                                "node_started": now, "step": 0, "total": 0, "s_per_it": None, "sampling": None}
            cur = self.current
            if cur is None or (pid and cur["prompt_id"] != pid):
                finished = any(h["prompt_id"] == pid for h in self.history)
                if event == "progress" or (event == "executing" and data.get("node") is not None and not finished):
                    self.current = cur = {"prompt_id": pid, "started": now, "node": None, "class_type": "", "phase": "Starting",
                                          "node_started": now, "step": 0, "total": 0, "s_per_it": None, "sampling": None}
                else:
                    return
            if event == "executing":
                node = data.get("node")
                if node is None:
                    self._finish(cur, now, "done")
                    return
                if node != cur["node"]:
                    ct = self._class_type(pid, node)
                    cur.update(node=node, class_type=ct, phase=PHASES.get(ct, ct or "Working"), node_started=now,
                               step=0, total=0, s_per_it=None, _t0=None, _v0=None)
            elif event == "progress":
                value, total = int(data.get("value", 0)), int(data.get("max", 0))
                if data.get("node") and data.get("node") != cur["node"]:
                    ct = self._class_type(pid, data["node"])
                    cur.update(node=data["node"], class_type=ct, phase=PHASES.get(ct, ct or "Working"), node_started=now,
                               _t0=None, _v0=None, s_per_it=None)
                if cur.get("_t0") is None or value < (cur.get("_v0") or 0):
                    cur["_t0"], cur["_v0"] = now, value
                elif value > cur["_v0"]:
                    cur["s_per_it"] = (now - cur["_t0"]) / (value - cur["_v0"])
                cur["step"], cur["total"] = value, total
                if cur["class_type"] == "SamplerCustomAdvanced":
                    cur["sampling"] = {"steps": total, "s_per_it": cur["s_per_it"], "started": cur["node_started"]}
            elif event in ("execution_success",):
                self._finish(cur, now, "done")
            elif event == "execution_error":
                self._finish(cur, now, "error")
            elif event == "execution_interrupted":
                self._finish(cur, now, "interrupted")

    def _finish(self, cur: dict, now: float, status: str) -> None:
        if cur is None:
            return
        sampling = cur.get("sampling") or {}
        self.history.append({"prompt_id": cur["prompt_id"], "status": status, "seconds": round(now - cur["started"], 1),
                             "steps": sampling.get("steps"), "s_per_it": sampling.get("s_per_it"), "finished": now})
        self.current = None

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            cur = dict(self.current) if self.current else None
            history = list(self.history)[-10:]
        if cur:
            for key in ("_t0", "_v0"):
                cur.pop(key, None)
            cur["elapsed"] = round(now - cur["started"], 1)
            cur["phase_elapsed"] = round(now - cur["node_started"], 1)
            if cur.get("s_per_it") and cur.get("total"):
                cur["eta"] = round(cur["s_per_it"] * max(0, cur["total"] - cur["step"]), 1)
        return {"current": cur, "history": history}

    def result_for(self, prompt_id: str) -> dict | None:
        with self._lock:
            found = [h for h in self.history if h["prompt_id"] == prompt_id]
        return max(found, key=lambda h: h["seconds"]) if found else None


LIVE = LiveState()


def install() -> None:
    from server import PromptServer

    server = PromptServer.instance
    if getattr(server.send_sync, "_clapper", False):
        return
    original = server.send_sync

    def send_sync(event, data, sid=None):
        try:
            LIVE.observe(event, data)
        except Exception:  # noqa: BLE001
            pass
        return original(event, data, sid)

    send_sync._clapper = True
    server.send_sync = send_sync

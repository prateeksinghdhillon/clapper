"""Who holds the GPU: ComfyUI (H3) or the local assistant model. Runs inside the ComfyUI process.

H3 and a local LLM do not fit in VRAM together, so one is unloaded before the other loads.
Auto mode: the assistant loads when it is asked something (ComfyUI's models are unloaded first,
only while its queue is empty), and is ejected whenever any job is queued in ComfyUI — also jobs
queued from ComfyUI's own page. Manual mode: only the Load / Eject buttons change anything, and
Clapper refuses to queue a render while the assistant is loaded (jobs from ComfyUI's own page
still eject it, since they would otherwise run out of memory).
"""
from __future__ import annotations

import logging
import threading
import time

from .engine import llm

log = logging.getLogger("clapper")


class GPUError(RuntimeError):
    pass


class GPU:
    def __init__(self):
        self.lock = threading.RLock()
        self.before_eject = []  # callbacks(runner): save the conversation cache before the model goes
        self.state = "idle"     # idle · freeing · loading · ejecting
        self.last: dict = {}

    # ------------------------------------------------------------ state

    @staticmethod
    def settings() -> dict:
        return llm.load_settings()

    def runner(self) -> llm.LocalRunner:
        return llm.LocalRunner(self.settings()["providers"]["local"])

    @staticmethod
    def comfy() -> dict:
        import comfy.model_management as mm
        from server import PromptServer

        running, pending = PromptServer.instance.prompt_queue.get_current_queue()
        return {"running": len(running), "pending": len(pending), "models": len(mm.current_loaded_models)}

    def status(self) -> dict:
        s = self.settings()
        prov = s["providers"]["local"]
        runner = llm.LocalRunner(prov)
        reachable = runner.reachable()
        loaded = reachable and runner.loaded()
        c = self.comfy()
        holder = "assistant" if loaded else "h3" if (c["models"] or c["running"]) else "free"
        return {"mode": s["gpu_mode"], "state": self.state, "holder": holder, "comfy": c, "last": self.last,
                "llm": {"reachable": reachable, "loaded": loaded, "model": prov["model"], "name": prov.get("name", "")},
                "active_is_local": s["providers"].get(s["active"], {}).get("kind") == "local"}

    # ------------------------------------------------------------ actions

    def free_comfy(self, timeout: float = 30) -> int:
        """Unload ComfyUI's models (only when its queue is empty). Returns how many were loaded."""
        import comfy.model_management as mm
        from server import PromptServer

        c = self.comfy()
        if c["running"] or c["pending"]:
            raise GPUError("ComfyUI is rendering — the local assistant can load when the queue is empty")
        count = c["models"]
        if not count:
            return 0
        queue = PromptServer.instance.prompt_queue
        queue.set_flag("unload_models", True)
        queue.set_flag("free_memory", True)
        deadline = time.time() + timeout
        while mm.current_loaded_models and time.time() < deadline:
            time.sleep(0.25)
        time.sleep(1.0)  # the worker empties the CUDA cache right after unloading
        if mm.current_loaded_models:
            raise GPUError("ComfyUI did not unload its models in time")
        return count

    def load_llm(self) -> dict:
        with self.lock:
            runner = self.runner()
            if not runner.reachable():
                try:
                    started = runner.start_server()
                except llm.LLMError as exc:
                    raise GPUError(str(exc)) from None
                if not started:
                    raise GPUError(f"the local model server is not running at {runner.swap} — start llama-swap, or set a "
                                   "start command in Assistant settings so Clapper can start it")
                log.info("Clapper: started the local model server with its start command")
            if runner.loaded():
                return {"already": True}
            try:
                self.state = "freeing"
                freed = self.free_comfy()
                self.state = "loading"
                seconds = runner.load()
            finally:
                self.state = "idle"
            self.last = {"action": "loaded", "seconds": seconds, "comfy_models_unloaded": freed, "at": time.time()}
            log.info("Clapper: assistant model loaded in %ss (%s ComfyUI model(s) unloaded)", seconds, freed)
            return self.last

    def eject_llm(self, reason: str = "") -> bool:
        with self.lock:
            runner = self.runner()
            if not runner.loaded():
                return False
            self.state = "ejecting"
            try:
                for callback in self.before_eject:
                    try:
                        callback(runner)
                    except Exception as exc:  # noqa: BLE001 — saving the cache is best effort
                        log.warning("Clapper: could not save the assistant's cache: %s", exc)
                runner.unload()
            finally:
                self.state = "idle"
            self.last = {"action": "ejected", "reason": reason, "at": time.time()}
            log.info("Clapper: assistant model ejected (%s)", reason or "asked")
            return True

    def ensure_llm(self) -> None:
        """Before the assistant talks to the local model."""
        runner = self.runner()
        if runner.loaded():
            return
        if self.settings()["gpu_mode"] == "manual":
            raise GPUError("The assistant model is not loaded. Load it from the GPU menu, or set GPU mode to Auto.")
        self.load_llm()

    def check_render(self) -> None:
        """Before Clapper queues a render."""
        if self.settings()["gpu_mode"] == "manual" and self.runner().loaded():
            raise GPUError("The assistant model is loaded. Eject it from the GPU menu first, or set GPU mode to Auto.")

    def on_prompt(self, json_data):
        """ComfyUI calls this for every queued job (also from its own page)."""
        try:
            self.eject_llm("a ComfyUI job was queued")
        except Exception as exc:  # noqa: BLE001 — never block ComfyUI's queue
            log.warning("Clapper: could not eject the assistant model: %s", exc)
        return json_data


GPU_MANAGER = GPU()


def install() -> None:
    from server import PromptServer

    server = PromptServer.instance
    if not getattr(server, "_clapper_gpu", False):
        server.add_on_prompt_handler(GPU_MANAGER.on_prompt)
        server._clapper_gpu = True

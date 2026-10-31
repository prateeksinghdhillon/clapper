"""Run Clapper's web interface and HTTP API outside ComfyUI's process, for interface development and tests.

    HOME=/tmp/clapper-dev python tools/dev_server.py [--port 8190] [--comfy http://127.0.0.1:8188]

The page is at http://127.0.0.1:PORT/clapper/. Jobs (takes, tools, upscales) are sent to the real ComfyUI given
with --comfy, so they really run; ComfyUI's own queue controls (Stop) act only on that ComfyUI's HTTP API here.
Set HOME to a scratch folder to get a separate ~/.clapper (profiles, project list) — projects must live inside it.
Run it with a Python that has aiohttp, Pillow and psutil (for example ComfyUI's).
"""
from __future__ import annotations

import argparse
import sys
import types
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]


class _Queue:
    """Just enough of ComfyUI's PromptQueue for Clapper's routes (asks the real ComfyUI over HTTP)."""

    def __init__(self, url: str):
        self.url = url
        self.currently_running: dict = {}

    def get_current_queue(self):
        import json
        import urllib.request
        try:
            with urllib.request.urlopen(self.url + "/queue", timeout=10) as r:
                q = json.loads(r.read())
            return q.get("queue_running", []), q.get("queue_pending", [])
        except OSError:
            return [], []

    def delete_queue_item(self, fn):
        import json
        import urllib.request
        _, pending = self.get_current_queue()
        ids = [item[1] for item in pending if fn(item)]
        if ids:
            req = urllib.request.Request(self.url + "/queue", data=json.dumps({"delete": ids}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8190)
    ap.add_argument("--comfy", default="http://127.0.0.1:8188")
    args = ap.parse_args()

    app = web.Application(client_max_size=1024 ** 3)
    server = types.SimpleNamespace(routes=web.RouteTableDef(), app=app, prompt_queue=_Queue(args.comfy),
                                   send_sync=lambda *a, **k: None, add_on_prompt_handler=lambda fn: None)
    sys.modules["server"] = types.SimpleNamespace(PromptServer=types.SimpleNamespace(instance=server))
    port = int(args.comfy.rsplit(":", 1)[-1])
    sys.path.insert(0, str(ROOT))
    import comfyui_clapper  # noqa: F401  (the package itself, before the stubs below: no graph nodes outside ComfyUI)
    comfy = types.ModuleType("comfy")
    comfy.__path__ = []
    sys.modules["comfy"] = comfy
    sys.modules["comfy.model_management"] = comfy.model_management = types.SimpleNamespace(current_loaded_models=[])
    sys.modules["comfy.cli_args"] = types.SimpleNamespace(args=types.SimpleNamespace(port=port))
    sys.modules["nodes"] = types.SimpleNamespace(interrupt_processing=lambda: __import__("urllib.request").request.urlopen(
        __import__("urllib.request").request.Request(args.comfy + "/interrupt", data=b"{}", headers={"Content-Type": "application/json"}), timeout=10))
    import comfyui_clapper.api  # noqa: F401  (registers routes)
    import comfyui_clapper.assistant_api  # noqa: F401

    app.add_routes(server.routes)
    print(f"Clapper dev server on http://127.0.0.1:{args.port}/clapper/  (ComfyUI: {args.comfy})", flush=True)
    web.run_app(app, host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    main()

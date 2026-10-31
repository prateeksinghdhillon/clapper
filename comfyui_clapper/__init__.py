"""Clapper — scene/take workflow for MiniMax H3 in ComfyUI.

Inside ComfyUI this registers the graph nodes, the /clapper/ page + API, and a
sidebar button (./js). Outside ComfyUI (CLI/tests) only the pure-Python engine is used.
"""
import importlib.util

WEB_DIRECTORY = "./js"

if importlib.util.find_spec("comfy") is not None:  # loaded by ComfyUI
    from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
    from . import api  # noqa: F401  (registers routes on PromptServer)
    from . import assistant_api  # noqa: F401
else:
    NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS = {}, {}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

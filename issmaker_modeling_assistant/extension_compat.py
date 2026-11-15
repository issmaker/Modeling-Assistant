"""Find the installed add-on module ID in classic or Blender Extension mode.

The extension uses bl_ext.user_default.issmaker_modeling_assistant.
The classic add-on uses issmaker_modeling_assistant.
Derive the package name from this module itself, not an absolute string.
"""
from __future__ import annotations

ADDON_MODULE = __package__ or "issmaker_modeling_assistant"

def addon_key() -> str:
    """Module identifier for Blender add-on preferences and import paths."""
    return ADDON_MODULE

def addon_module(relative: str = "") -> str:
    if not relative:
        return ADDON_MODULE
    if not isinstance(relative, str) or not relative.strip("."):
        raise ValueError("Invalid relative submodule")
    return ADDON_MODULE + "." + relative.lstrip(".")

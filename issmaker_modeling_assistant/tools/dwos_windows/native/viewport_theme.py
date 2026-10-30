"""DWOS 3.5 retained Viewport visual tokens.

One source of truth for the Composer visual language.  Keeping these values out
of event/draw code prevents the old accumulation of slightly different debug
colors and makes the HUD behave like one professional tool.
"""
# DWOS references the independent ISSMAKER token module, never the other
# way around.  This guarantees the core can be imported before DWOS initializes.
from ....core.design_tokens import (
    PANEL_BG, PANEL_SURFACE, PANEL_HOVER, PANEL_BORDER,
    TEXT, MUTED, MINT, ORANGE, RED, BLUE, SHADOW,
)

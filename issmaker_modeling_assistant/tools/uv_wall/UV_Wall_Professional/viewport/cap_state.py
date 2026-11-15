"""Pure, project-level cap master-switch state policy.

Independent lower/upper settings remain individually configurable in editors.
The single contextual master control enables top-only from OFF, but switching
OFF from any active configuration must disable both sides atomically.
"""
from __future__ import annotations

def cap_master_enabled(top_enabled, bottom_enabled):
    return bool(top_enabled or bottom_enabled)

def toggled_master_caps(top_enabled, bottom_enabled):
    if cap_master_enabled(top_enabled,bottom_enabled):
        return False,False
    return True,False

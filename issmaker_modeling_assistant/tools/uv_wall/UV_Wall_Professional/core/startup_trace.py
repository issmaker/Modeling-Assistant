"""Write actionable startup diagnostics before UV Wall logging/RNA exists.

This is deliberately stdlib-only and does not register Blender classes.
"""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import traceback


def report_failure(stage, exc, *, blender=None, addon_version='unknown'):
    """Persist *original* traceback. Never raise while handling an exception."""
    header = (
        f"UV WALL STARTUP FAILURE\nversion={addon_version}\nstage={stage}\n"
        f"exception_type={type(exc).__name__}\nexception={exc}\n"
        f"blender={getattr(getattr(blender, 'app', None), 'version_string', 'unknown')}\n"
        f"traceback:\n{''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))}\n"
    )
    destinations = []
    if blender is not None:
        try:
            folder = blender.utils.user_resource(
                'CONFIG', path='UV_Wall_Professional/logs', create=True)
            if folder:
                destinations.append(Path(folder))
        except Exception:
            pass
    destinations.extend([Path.home()/'UV_Wall_Professional'/'logs',
                         Path(tempfile.gettempdir())/'UV_Wall_Professional'])
    for dest in destinations:
        try:
            dest.mkdir(parents=True, exist_ok=True)
            filename = dest / 'startup-failure-latest.log'
            filename.write_text(header, encoding='utf-8')
            return str(filename)
        except (OSError, UnicodeError):
            continue
    try:
        print(header, flush=True)
    except Exception:
        pass
    return 'startup log unavailable; consult Blender System Console'

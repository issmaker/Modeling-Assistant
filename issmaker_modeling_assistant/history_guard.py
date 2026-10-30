"""Bounded read-only action trace for native Blender Undo/Redo lifecycle.

Only records events. It never calls bpy.ops.ed.undo or modifies scene data.
"""
from collections import deque
import time

_EVENTS = deque(maxlen=40)
_GENERATION = 0
_PENDING = None


def begin(kind):
    global _PENDING
    if kind not in {'UNDO', 'REDO'}:
        raise ValueError(kind)
    _PENDING = kind
    _EVENTS.append({'event': kind + '_PRE', 'at': round(time.time(), 3)})


def complete(kind, success=True, reason=''):
    global _PENDING, _GENERATION
    if kind not in {'UNDO', 'REDO'}:
        raise ValueError(kind)
    _GENERATION += 1
    _EVENTS.append({'event': kind + '_POST', 'at': round(time.time(), 3),
                    'success': bool(success), 'reason': str(reason)[:160],
                    'generation': _GENERATION,
                    'matching_pre': _PENDING == kind})
    _PENDING = None


def status():
    return {'generation': _GENERATION, 'pending': _PENDING,
            'recent': list(_EVENTS)}


def reset():
    global _PENDING
    _PENDING = None

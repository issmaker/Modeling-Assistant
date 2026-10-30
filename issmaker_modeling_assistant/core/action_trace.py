"""Bounded diagnostics events for integrated tool launch and lifecycle."""
from collections import deque
from datetime import datetime, timezone

_EVENTS = deque(maxlen=300)


def record(action, tool='CORE', detail=''):
    _EVENTS.append(dict(utc=datetime.now(timezone.utc).isoformat(),
                        action=str(action)[:80], tool=str(tool)[:60],
                        detail=str(detail)[:500]))


def snapshot():
    return list(_EVENTS)


def clear():
    _EVENTS.clear()

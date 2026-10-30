"""Bounded runtime ledger for reliability decisions; no Blender dependency."""
from __future__ import annotations
from collections import deque
from datetime import datetime, timezone

_ROWS=deque(maxlen=256)


def record(decision, *, source='', extra=None):
    try:payload=decision.as_dict()
    except AttributeError:payload={'kind':'INTERNAL_ERROR','code':'BAD_DECISION','message':str(decision)}
    payload['source']=str(source)
    payload['timestamp_utc']=datetime.now(timezone.utc).isoformat()
    if extra:payload['extra']=dict(extra)
    _ROWS.append(payload)
    return payload


def snapshot():return list(_ROWS)
def clear():_ROWS.clear()

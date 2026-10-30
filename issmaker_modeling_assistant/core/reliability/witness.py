"""Bounded witness payloads for hard blocks and uncertainty reports."""
from __future__ import annotations
import math

MAX_COORDS=32
MAX_IDS=64


def _safe_number(value):
    try:v=float(value)
    except (TypeError,ValueError,OverflowError):return None
    return v if math.isfinite(v) else None


def make_witness(*, entity_ids=(), coordinates=(), metrics=None, context=None):
    ids=[]
    for value in entity_ids:
        try:ids.append(int(value))
        except (TypeError,ValueError,OverflowError):continue
        if len(ids)>=MAX_IDS:break
    coords=[]
    for point in coordinates:
        row=tuple(_safe_number(v) for v in point)
        if row and all(v is not None for v in row):coords.append(row)
        if len(coords)>=MAX_COORDS:break
    clean_metrics={}
    for key,value in (metrics or {}).items():
        if isinstance(value,(str,bool,int)) or value is None:clean_metrics[str(key)]=value
        else:
            number=_safe_number(value)
            clean_metrics[str(key)]=number if number is not None else str(value)[:160]
    return {'entity_ids':ids,'coordinates':coords,'metrics':clean_metrics,'context':dict(context or {})}

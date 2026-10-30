"""ISSMAKER Pass 11C shared deterministic Motion Engine.

Pure Python. Animation only changes paint properties; placement, visible hit
rectangles, modal lifecycle, tool operators and geometry remain authoritative.
Reduced motion or OFF returns final values immediately. No bpy imports.
"""
from __future__ import annotations
from dataclasses import dataclass
from math import isfinite, cos, pi


def clamp(value, low=0., high=1.):
    try:
        v=float(value)
        return min(high,max(low,v)) if isfinite(v) else low
    except (ValueError,TypeError,OverflowError):
        return low


def ease_out_cubic(t):
    t=clamp(t)
    return 1.-(1.-t)**3


def ease_in_out_cubic(t):
    t=clamp(t)
    return 4*t*t*t if t<.5 else 1.-(-2*t+2)**3/2


def interpolate(a,b,t,easing='out'):
    f=ease_in_out_cubic(t) if easing=='in_out' else ease_out_cubic(t)
    return a+(b-a)*f


@dataclass(frozen=True)
class MotionSpec:
    mode: str='BALANCED'
    reduced: bool=False
    speed: float=1.

    @property
    def enabled(self): return (self.mode.upper() in ('BALANCED','RICH') and not self.reduced)

    def duration(self,kind):
        if not self.enabled:return 0.
        base={'enter':.26,'exit':.17,'minimize':.20,'tab':.23,
              'hover':.13,'press':.19,'restore':.26,'feedback':.30}.get(kind,.20)
        return base*(1.20 if self.mode.upper()=='RICH' else 1.)/max(.3,min(3.,clamp(self.speed,0.,3.)))

    def fps_interval(self):
        if not self.enabled:return None
        return 1. / 60.  # 60 Hz target; actual Blender draw rate is measured separately


def progress(start,now,duration):
    return 1. if duration<=0 else clamp((now-start)/duration)


def envelope(start,now,duration):
    if duration<=0:return 0.
    t=progress(start,now,duration)
    return (1.-t)**2*(.5-.5*cos(2*pi*t))


class MotionBank:
    """Bounded keyed hover faders; no periodic timer required for hidden UI."""
    def __init__(self,limit=128):
        self._items={}
        self.limit=max(8,int(limit))

    def sample(self,key,target,now,duration):
        target=1. if target else 0.
        if duration<=0:
            self._items.pop(key,None)
            return target
        entry=self._items.get(key)
        if entry is None:
            start_value=0.
            start=now
            old_target=target
        else:
            start_value,start,old_target=entry
        if old_target!=target:
            start_value=interpolate(start_value,old_target,progress(start,now,duration))
            start=now
        value=interpolate(start_value,target,progress(start,now,duration))
        if abs(value-target)<.0001:
            # Preserve settled nonzero targets so hover-out starts at 1,
            # not zero. Prune settled zero to avoid retaining inactive keys.
            if target>0.:self._items[key]=(target,now,target)
            else:self._items.pop(key,None)
        else:
            self._items[key]=(start_value,start,target)
        if len(self._items)>self.limit:
            # Drop oldest animated entries; they can reconstruct on next draw.
            oldest=min(self._items,key=lambda k:self._items[k][1])
            self._items.pop(oldest,None)
        return value

    def clear(self):self._items.clear()


def from_theme(theme):
    return MotionSpec(getattr(theme,'motion','BALANCED'),False,getattr(theme,'pattern_speed',1.))

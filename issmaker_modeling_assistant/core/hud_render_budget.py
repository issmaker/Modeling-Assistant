"""Pure Python bounded viewport render pools and decorative pacing.

No bpy/gpu dependencies. All GPU references live in capped per-region scopes,
are removed by explicit reset on addon unload and by LRU/idle eviction.
Controls, hitboxes and modal input do NOT use decorative pacing.
"""
from __future__ import annotations
from collections import OrderedDict
from math import isfinite
from time import monotonic

HZ={'ULTRA':48,'HIGH':36,'BALANCED':30,'PERFORMANCE':24}

def decorative_time(now,quality='HIGH',animate=True):
    now=float(now)
    if not isfinite(now):raise ValueError('Nonfinite time')
    if not animate:return 0.0
    hz=HZ.get(str(quality).upper(),36)
    # Incremental 30/36/48Hz patterns; Blender hit tests and hover stay live.
    return int(max(0.,now)*hz)/hz

class RegionPool:
    def __init__(self,max_regions=6,idle_seconds=100.,clock=monotonic):
        self.max_regions=max(1,min(32,int(max_regions)))
        self.idle_seconds=max(1.,float(idle_seconds))
        self.clock=clock
        self.entries=OrderedDict()
        self.hits=0;self.misses=0;self.evictions=0
        self.dispose_failures=0;self.last_dispose_error=None

    def _dispose(self,entry):
        obj=entry[0]
        try:
            obj.clear()
            self.evictions+=1
        except Exception as exc:
            self.dispose_failures+=1
            self.last_dispose_error=f'{type(exc).__name__}: {exc}'
            # Best effort: never prevent other GPU regions from being released.

    def get(self,key,create):
        if not isinstance(key,tuple) or not key or not all(isinstance(x,int) and x>0 for x in key):
            raise ValueError('Invalid GPU region scope')
        now=float(self.clock())
        if not isfinite(now):raise ValueError('Nonfinite time')
        for old,(item,seen) in list(self.entries.items()):
            if now-seen>self.idle_seconds:
                self.entries.pop(old);self._dispose((item,seen))
        if key in self.entries:
            obj,_=self.entries.pop(key)
            self.entries[key]=(obj,now)
            self.hits+=1
            return obj
        obj=create();self.entries[key]=(obj,now);self.misses+=1
        while len(self.entries)>self.max_regions:
            _,entry=self.entries.popitem(last=False)
            self._dispose(entry)
        return obj

    def clear(self):
        while self.entries:
            _,entry=self.entries.popitem(last=False)
            self._dispose(entry)

    def snapshot(self):
        return {'regions':len(self.entries),'region_hits':self.hits,
                'region_misses':self.misses,'regions_evicted':self.evictions, 'dispose_failures':self.dispose_failures, 'last_dispose_error':self.last_dispose_error}

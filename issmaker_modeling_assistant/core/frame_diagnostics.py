"""Blender-independent performance telemetry and render-budget hints.

`draw_fps` is measured from *actual* draw callbacks; not the requested timer
frequency. CPU timing does not measure GPU completion. All bounded and opt-in.
"""
from __future__ import annotations
from collections import deque
from math import isfinite


class FrameDiagnostics:
    def __init__(self,limit=120):
        self.draw_times=deque(maxlen=max(10,min(600,int(limit))))
        self.cpu_ms=deque(maxlen=max(10,min(600,int(limit))))
        self.dropped_steps=0

    def sample(self,start,end):
        if not (isfinite(start) and isfinite(end) and end>=start):
            return
        self.draw_times.append(end)
        self.cpu_ms.append((end-start)*1000.)

    @property
    def draw_fps(self):
        if len(self.draw_times)<2:return 0.
        elapsed=self.draw_times[-1]-self.draw_times[0]
        return (len(self.draw_times)-1)/elapsed if elapsed>0 else 0.

    @property
    def avg_cpu_ms(self):
        return sum(self.cpu_ms)/len(self.cpu_ms) if self.cpu_ms else 0.

    def decoration_hz(self):
        """Coarsen only procedural geometry when CPU draw is costly.

        Button hitboxes and alpha easing must still run at every draw tick.
        """
        return 12 if self.avg_cpu_ms>21 else 20 if self.avg_cpu_ms>12 else 30

    @staticmethod
    def quantized_seconds(seconds,rate):
        if not isfinite(seconds) or seconds<0:return 0.
        rate=max(1.,min(120.,float(rate)))
        return int(seconds*rate)/rate

    def snapshot(self):
        return {'draw_fps':round(self.draw_fps,1),
                'cpu_draw_ms':round(self.avg_cpu_ms,2),
                'pattern_hz':self.decoration_hz(),
                'frames':len(self.draw_times)}

    def clear(self):
        self.draw_times.clear();self.cpu_ms.clear();self.dropped_steps=0

"""ISSMAKER Pass 07 — shared, Blender-free viewport window layout coordinator.

Coordinates are *WINDOW-region local*, bottom-left origin, and use ``xyxy``.
A frame owns one coordinator. Only committed layouts may draw or own hitboxes.
User preferences survive reflows inside the session, not Blender-area recreation.
Blender drag/close/toggle events remain the responsibility of the HUD controller.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import math

from .overlay_placement import StrictOverlayAllocator, rect_xyxy


@dataclass
class WindowState:
    key: str
    preferred: tuple
    size: tuple
    priority: int = 0
    compact_size: Optional[tuple] = None
    collapsed: bool = False
    hidden: bool = False
    pinned: bool = False
    rect: Optional[tuple] = None
    status: str = 'PENDING'


class ViewportWindowManager:
    """Single-region deterministic layout with stable key and explicit lifecycle.

    Fixed reservations include toolbars/model silhouettes; floating windows are
    allocated by descending priority. If a panel cannot fit, first try an
    explicitly supplied compact size; otherwise fail closed (rect is None).
    A pinned window gets priority and keeps its preferred origin *when free*;
    pinning never authorizes overlap with a higher-priority reservation.
    """

    def __init__(self, width, height, *, margin=8., gap=6.):
        self.width = float(width)
        self.height = float(height)
        if not all(math.isfinite(x) and x > 0 for x in (self.width, self.height)):
            raise ValueError('Invalid viewport')
        self.margin = max(0.,float(margin))
        self.gap = max(0.,float(gap))
        if not all(math.isfinite(x) for x in (self.margin,self.gap)):
            raise ValueError('Invalid layout spacing')
        self.windows = {}
        self._fixed = {}
        self.failures = []
        self._last_layout = {}

    def reserve(self, key, bounds):
        key = str(key)
        if key in self.windows:
            raise ValueError('Window and fixed reservation share a key')
        bounds = rect_xyxy(bounds)
        self._fixed[key] = bounds

    def register(self, key, size, preferred, *, priority=0,
                 compact_size=None, pinned=False, hidden=False, collapsed=False):
        key=str(key)
        if not key or key in self.windows or key in self._fixed:
            raise ValueError('Duplicate or empty window key')
        size=self._size(size)
        preferred=self._position(preferred)
        compact_size=self._size(compact_size) if compact_size is not None else None
        self.windows[key]=WindowState(key=key,size=size,preferred=preferred,
                                      priority=int(priority), compact_size=compact_size,
                                      pinned=bool(pinned),hidden=bool(hidden),collapsed=bool(collapsed))
        return self.windows[key]

    @staticmethod
    def _size(value):
        try: result=tuple(float(x) for x in value)
        except (TypeError,ValueError,OverflowError) as exc: raise ValueError('Invalid size') from exc
        if len(result)!=2 or not all(math.isfinite(x) and x>0 for x in result):
            raise ValueError('Invalid size')
        return result

    @staticmethod
    def _position(value):
        try: result=tuple(float(x) for x in value)
        except (TypeError,ValueError,OverflowError) as exc: raise ValueError('Invalid position') from exc
        if len(result)!=2 or not all(math.isfinite(x) for x in result):
            raise ValueError('Invalid position')
        return result

    def _invalidate(self, key):
        """Remove an old clickable rectangle *before* the next draw tick.

        Controllers must only use the committed state.rect when hit testing.
        This avoids ghost buttons during hide, resize and transient reflows.
        """
        state = self.windows[key]
        state.rect = None
        state.status = 'PENDING' if not state.hidden else 'HIDDEN'
        self._last_layout.pop(key, None)

    def move(self,key,preferred):
        self.windows[key].preferred=self._position(preferred)
        self._invalidate(key)

    def hide(self,key,hidden=True):
        state = self.windows[key]
        state.hidden=bool(hidden)
        self._invalidate(key)

    def restore(self,key):
        self.hide(key,False)

    def restore_all(self):
        """Restore intentionally hidden windows, without bypassing placement.

        A restored panel remains noninteractive until layout() reserves space.
        """
        restored = []
        for key,state in self.windows.items():
            if state.hidden:
                self.restore(key)
                restored.append(key)
        return tuple(restored)

    def hidden_keys(self):
        return tuple(sorted(key for key,state in self.windows.items() if state.hidden))

    def unavailable_keys(self):
        return tuple(sorted(key for key,state in self.windows.items()
                            if state.hidden or state.status == 'NO_ROOM'))

    def remove(self,key):
        self._last_layout.pop(key,None)
        return self.windows.pop(key)

    def set_viewport_size(self,width,height):
        w,h = self._size((width,height))
        self.width,self.height=w,h
        for key in self.windows:
            self._invalidate(key)

    def collapse(self,key,collapsed=True):
        state=self.windows[key]
        if collapsed and state.compact_size is None:
            raise ValueError('Window has no compact layout')
        state.collapsed=bool(collapsed)
        self._invalidate(key)

    def pin(self,key,pinned=True):
        self.windows[key].pinned=bool(pinned)
        self._invalidate(key)

    def layout(self):
        allocator=StrictOverlayAllocator(self.width,self.height,
                                         margin=self.margin,gap=self.gap)
        for key,bounds in self._fixed.items():
            allocator.reserve('fixed:'+key,bounds)
        self.failures=[]
        self._last_layout={}
        ordered=sorted(self.windows.values(),key=lambda s:(-int(s.pinned),-s.priority,s.key))
        for state in ordered:
            state.rect=None
            state.status='HIDDEN' if state.hidden else 'PENDING'
            if state.hidden:
                continue
            options=[('COLLAPSED' if state.collapsed else 'FULL',
                      state.compact_size if state.collapsed else state.size)]
            if not state.collapsed and state.compact_size:
                options.append(('AUTO_COMPACT',state.compact_size))
            for name,size in options:
                result=allocator.place('window:'+state.key,size,state.preferred)
                if result is not None:
                    state.rect=result
                    state.status=name
                    self._last_layout[state.key]=result
                    break
            if state.rect is None:
                state.status='NO_ROOM'
                self.failures.append({'key':state.key,'reason':allocator.last_failure,
                                      'priority':state.priority})
        return dict(self._last_layout)

    def snapshot(self):
        return {'viewport':(self.width,self.height),'fixed':dict(self._fixed),
                'windows':{key:{'rect':state.rect,'status':state.status,
                                'priority':state.priority,'preferred':state.preferred,
                                'hidden':state.hidden,'pinned':state.pinned,
                                'collapsed':state.collapsed} for key,state in self.windows.items()},
                'failures':list(self.failures)}


def place_unobstructed(viewport, key, size, preferred, blockers=(), *, margin=8.,gap=6., scope=None):
    """Adapter for legacy controllers. Returns xyxy or None; no interactive ghost.

    The controller must use this *same* returned rect for draw and input, and
    must clear stale hitboxes when None. Do not guess replacement positions.
    """
    # Pass 09: optional long-lived visibility, scoped to a single Blender WINDOW
    # region. Never allocate or hit-test a user-hidden window. Existing clients
    # without a scope retain the Pass 08 stateless behavior.
    from . import window_visibility
    if scope is not None and window_visibility.is_hidden(scope,key):
        return window_visibility.observe(scope,key,None)
    manager=ViewportWindowManager(*viewport,margin=margin,gap=gap)
    # Pass 15.9: previously each HUD only blocked its own controls. A scoped
    # caller also reserves committed peers (including fixed HUD chrome) from
    # the very same WINDOW region. Expired/hidden peer hitboxes never block.
    if scope is not None:
        for peer_key, peer_rect in window_visibility.peer_rectangles(scope, key):
            try: manager.reserve('peer:'+peer_key,rect_xyxy(peer_rect))
            except ValueError: continue
    for index,rect in enumerate(blockers):
        if rect is not None:
            try: manager.reserve('blocker:'+str(index),rect_xyxy(rect))
            except ValueError: continue  # Offscreen/invalid silhouette is not UI.
    manager.register(key,size,preferred)
    result = manager.layout().get(str(key))
    return window_visibility.observe(scope,key,result) if scope is not None else result

"""ISSMAKER shared HUD GPU caches, lazily scoped to a live Blender region.

Do not import `bpy`, `gpu` or tool packages at module import time. The pool
is intentionally bounded: no process-global unbounded GPU/driver objects.

A shader/batch belongs to its original window+area+region scope. It may not
be reused in another editor/window. `release()` runs on addon unregister.
"""
from __future__ import annotations
from .core.hud_render_budget import RegionPool
from collections import deque
from time import perf_counter

_BG_CPU_MS=deque(maxlen=120)

def background_sample(started):
    elapsed=(perf_counter()-started)*1000.
    if 0.<=elapsed<10000.:
        _BG_CPU_MS.append(elapsed)

_POOL=RegionPool(max_regions=6,idle_seconds=100.)

def active_scope():
    try:
        import bpy
        ctx=bpy.context
        if ctx.window is None or ctx.area is None or ctx.region is None:return None
        return (int(ctx.window.as_pointer()),int(ctx.area.as_pointer()),int(ctx.region.as_pointer()))
    except (ImportError,AttributeError,ReferenceError,RuntimeError,ValueError):return None

def cache():
    key=active_scope()
    if key is None:return None
    # Only the currently drawing Blender region can instantiate a GPU batch.
    def create():
        from .appearance_gpu import RenderCache
        return RenderCache(max_boxes=144,max_patterns=24)
    return _POOL.get(key,create)

def snapshot():
    s=_POOL.snapshot()
    s['background_avg_cpu_ms']=(round(sum(_BG_CPU_MS)/len(_BG_CPU_MS),3)
                                if _BG_CPU_MS else 0.)
    s['background_samples']=len(_BG_CPU_MS)
    key=active_scope()
    current=_POOL.entries.get(key) if key is not None else None
    if current:
        obj=current[0]
        s.update(cache_hits=obj.hits,cache_misses=obj.misses,
                 pattern_batches=len(obj.patterns)+len(obj.aa_patterns),
                 chrome_batches=len(obj.chrome),box_batches=len(obj.boxes))
    return s

def release():
    _POOL.clear()
    _BG_CPU_MS.clear()

def _on_file_load(_dummy):
    # A new .blend can invalidate viewport/region GPU handles.
    release()

def _remove_load_hooks(hooks):
    """Delete old Python-module incarnations after in-place addon reload."""
    for fn in tuple(hooks):
        if (getattr(fn, '__module__', None) == __name__
                and getattr(fn, '__name__', None) == '_on_file_load'):
            try:
                hooks.remove(fn)
            except (ValueError, RuntimeError, ReferenceError):
                pass


def install_hooks():
    try:
        import bpy
        from bpy.app.handlers import persistent
        hooks = bpy.app.handlers.load_post
        _remove_load_hooks(hooks)
        hooks.append(persistent(_on_file_load))
    except (ImportError, AttributeError, RuntimeError, ReferenceError):
        pass


def remove_hooks():
    try:
        import bpy
        _remove_load_hooks(bpy.app.handlers.load_post)
    except (ImportError, AttributeError, RuntimeError, ReferenceError):
        pass

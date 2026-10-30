"""Performance primitives for UV Wall NEXTGEN.

This module is Blender-light on purpose: it owns deterministic cache keys,
bounded pure-geometry caches and profiling counters, but never touches Blender
Objects. Production generators remain authoritative; cached values are used only
when every geometry/UV input in the key is identical.
"""
from __future__ import annotations

from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from hashlib import blake2b
from struct import pack
from time import perf_counter


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    stores: int = 0
    evictions: int = 0


class BoundedGeometryCache:
    """Small LRU for immutable geometry payloads.

    Values are stored as tuples of plain Python numbers so callers cannot mutate
    a cached Vector/UV object accidentally. The cache is intentionally bounded:
    it accelerates interactive rebuilds of the currently edited objects rather
    than becoming a scene-wide geometry database.
    """

    def __init__(self, max_entries=16, max_payload_items=600_000):
        self.max_entries = max(1, int(max_entries))
        self.max_payload_items = max(1, int(max_payload_items))
        self._items = OrderedDict()
        self.stats = CacheStats()

    def clear(self):
        self._items.clear()
        self.stats = CacheStats()

    def get(self, key):
        value = self._items.get(key)
        if value is None:
            self.stats.misses += 1
            return None
        self._items.move_to_end(key)
        self.stats.hits += 1
        return value

    def put(self, key, value, payload_items=0):
        if int(payload_items) > self.max_payload_items:
            return False
        self._items[key] = value
        self._items.move_to_end(key)
        self.stats.stores += 1
        while len(self._items) > self.max_entries:
            self._items.popitem(last=False)
            self.stats.evictions += 1
        return True

    def snapshot(self):
        return {
            "entries": len(self._items),
            "hits": self.stats.hits,
            "misses": self.stats.misses,
            "stores": self.stats.stores,
            "evictions": self.stats.evictions,
        }


SURFACE_GEOMETRY_CACHE = BoundedGeometryCache(max_entries=18, max_payload_items=750_000)


def clear_performance_caches():
    SURFACE_GEOMETRY_CACHE.clear()


def _float_bytes(value):
    return pack("!d", round(float(value), 12))


def _vector_triplet(value):
    if value is None:
        return (0.0, 0.0, 0.0)
    return (float(value[0]), float(value[1]), float(value[2]))


def surface_geometry_key(kind, path_points, source, tile_length, angle_rad, z_offset, preferred_normal):
    """Hash every input that can affect generated cap/surface geometry or UVs."""
    digest = blake2b(digest_size=20)
    digest.update(str(kind).encode("utf-8"))
    digest.update(_float_bytes(tile_length))
    digest.update(_float_bytes(angle_rad))
    digest.update(_float_bytes(z_offset))
    for component in _vector_triplet(preferred_normal):
        digest.update(_float_bytes(component))
    for point in path_points:
        digest.update(_float_bytes(point.x))
        digest.update(_float_bytes(point.y))
        digest.update(_float_bytes(point.z))
    # UV corners determine every emitted face UV. Texture resolution/material
    # identity is deliberately excluded: texel validation remains authoritative
    # and geometry/UV coordinates do not change when only the image datablock does.
    for name in ("bottom_left_uv", "bottom_right_uv", "top_left_uv", "top_right_uv"):
        uv = source.get(name)
        digest.update(_float_bytes(uv.x if uv is not None else 0.0))
        digest.update(_float_bytes(uv.y if uv is not None else 0.0))
    return digest.digest()


def freeze_surface_payload(vertices, faces, face_uvs):
    return (
        tuple(tuple(float(v) for v in vertex) for vertex in vertices),
        tuple(tuple(int(i) for i in face) for face in faces),
        tuple(tuple((float(uv[0]), float(uv[1])) for uv in uvs) for uvs in face_uvs),
    )


def thaw_surface_payload(payload):
    vertices, faces, face_uvs = payload
    # Return fresh outer lists. Inner tuples are immutable and safe for
    # mesh.from_pydata / UV assignment.
    return list(vertices), list(faces), list(face_uvs)


class PerformanceRegistry:
    """Rolling in-memory timings used by diagnostics/field tests."""

    def __init__(self, max_samples=64):
        self.max_samples = max(4, int(max_samples))
        self.samples = defaultdict(list)

    def add(self, stage, elapsed_seconds):
        values = self.samples[str(stage)]
        values.append(float(elapsed_seconds))
        if len(values) > self.max_samples:
            del values[:-self.max_samples]

    def summary(self, stage):
        values = self.samples.get(str(stage), ())
        if not values:
            return None
        ordered = sorted(values)
        return {
            "count": len(values),
            "last_ms": values[-1] * 1000.0,
            "min_ms": ordered[0] * 1000.0,
            "median_ms": ordered[len(ordered) // 2] * 1000.0,
            "max_ms": ordered[-1] * 1000.0,
        }


PERFORMANCE_REGISTRY = PerformanceRegistry()


class StageTimer:
    def __init__(self, stage):
        self.stage = str(stage)
        self.started = None
        self.elapsed = 0.0

    def __enter__(self):
        self.started = perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.elapsed = perf_counter() - self.started
        PERFORMANCE_REGISTRY.add(self.stage, self.elapsed)
        return False


def cached_surface_call(kind, builder, path_points, source, tile_length,
                        angle_rad=0.0, z_offset=0.0, preferred_normal=None,
                        log_fn=None):
    """Cache a pure surface/cap builder without bypassing later validation."""
    key = surface_geometry_key(
        kind, path_points, source, tile_length, angle_rad, z_offset, preferred_normal
    )
    lookup_started = perf_counter()
    cached = SURFACE_GEOMETRY_CACHE.get(key)
    lookup_elapsed = perf_counter() - lookup_started
    if cached is not None:
        PERFORMANCE_REGISTRY.add(f"{kind}_CACHE_LOOKUP", lookup_elapsed)
        vertices, faces, face_uvs = thaw_surface_payload(cached)
        if log_fn is not None:
            log_fn(
                f"GEOMETRY_CACHE kind={kind}; status=HIT; faces={len(faces)}; "
                f"lookup_ms={lookup_elapsed * 1000.0:.3f}; "
                f"entries={SURFACE_GEOMETRY_CACHE.snapshot()['entries']}",
                "PERF_CACHE",
            )
        return vertices, faces, face_uvs

    with StageTimer(f"{kind}_GEOMETRY") as timer:
        vertices, faces, face_uvs = builder(
            path_points, source, tile_length,
            angle_rad=angle_rad, z_offset=z_offset, preferred_normal=preferred_normal,
        )
    frozen = freeze_surface_payload(vertices, faces, face_uvs)
    payload_items = len(vertices) + len(faces) + sum(len(face) for face in faces)
    stored = SURFACE_GEOMETRY_CACHE.put(key, frozen, payload_items=payload_items)
    if log_fn is not None:
        log_fn(
            f"GEOMETRY_CACHE kind={kind}; status=MISS; build_ms={timer.elapsed * 1000.0:.3f}; "
            f"faces={len(faces)}; stored={stored}; entries={SURFACE_GEOMETRY_CACHE.snapshot()['entries']}",
            "PERF_CACHE",
        )
    return thaw_surface_payload(frozen)


def record_stage(log_fn, stage, started, category="PERF"):
    elapsed = perf_counter() - float(started)
    PERFORMANCE_REGISTRY.add(stage, elapsed)
    if log_fn is not None:
        log_fn(f"STAGE_TIME stage={stage}; elapsed={elapsed:.6f}s", category)
    return elapsed

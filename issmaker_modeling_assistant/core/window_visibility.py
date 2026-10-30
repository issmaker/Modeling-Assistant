"""Pass 09: runtime-only per-Viewport visibility registry for legacy GPU HUDs.

No Blender imports: renderer must call observed() and use the SAME placed rect
for rendering and hit-testing. Hiding cannot close an active modeling tool or
mutate geometry. State dies with this add-on module; this is intentional.
"""
from __future__ import annotations

from dataclasses import dataclass
from time import monotonic


@dataclass
class Entry:
    key: str
    title: str
    hidden: bool = False
    status: str = 'PENDING'
    rect: tuple | None = None
    last_seen: float = 0.0


_ENTRIES: dict[str, dict[str, Entry]] = {}
# Transient, non-interactive reservations for fixed HUD chrome such as Collision footer.
# These are NOT user-hideable windows. Store only coordinates and monotonic timestamps.
_FIXED: dict[str, dict[str, tuple]] = {}
_LIMIT = 128
_PEER_LEASE_SECONDS = 2.5

TITLES = {
    'DWOS_COMPOSER': 'DWOS · Studio',
    'DWOS_ZONE_POPUP': 'DWOS · Разделение зон',
    'DWOS_SPLIT_POPUP': 'DWOS · Редактор разделения',
    'DWOS_MULLION_EDIT': 'DWOS · Импост',
    'DWOS_ALIGN_SPLITS': 'DWOS · Выравнивание',
    'BRICK_HOLE_PREVIEW': 'Brick Hole · Интерактивный HUD',
    'BRICK_FIXTURE_NAV': 'Brick Hole · Навигатор',
    'BRICK_ISSUE_CARD': 'Brick Hole · Пересечения',
    'UV_WALL_RESOURCE_PICKER': 'UV Wall · Выбор ресурсов',
    'COLLISION_GROUP_MENU': 'Collision · Фильтр проблем',
    'COLLISION_INFO_CARD': 'Collision · Информационная карточка',
    'IMA_OT_geometry_quality_popup': 'ISSMAKER · Геометрия',
}


def editor_key(kind):
    return 'UV_WALL_EDITOR:' + str(kind)


def viewport_key(window, area, region):
    """Stable for the lifetime of one Blender WINDOW region, not persisted."""
    return f'{int(window)}:{int(area)}:{int(region)}'


def observe(scope, key, rect, *, now=None):
    """Observe a requested HUD; hidden windows always return None (no hits)."""
    if not scope:
        return rect
    scope = str(scope); key = str(key)
    if not key:
        raise ValueError('Window key is required')
    entries = _ENTRIES.setdefault(scope, {})
    stamp = monotonic() if now is None else float(now)
    if key not in entries:
        if len(entries) >= _LIMIT:
            # Evict the oldest non-hidden stale record first, never a hidden one.
            candidates = [e for e in entries.values() if not e.hidden]
            if candidates:
                entries.pop(min(candidates, key=lambda e: e.last_seen).key)
            else:
                raise RuntimeError('Too many hidden windows in viewport')
        entries[key] = Entry(key=key, title=TITLES.get(key, key))
    entry = entries[key]
    entry.last_seen = stamp
    entry.rect = None if entry.hidden or rect is None else tuple(rect)
    entry.status = 'HIDDEN' if entry.hidden else ('VISIBLE' if rect is not None else 'NO_ROOM')
    return entry.rect


def reserve_fixed(scope, key, rect, *, now=None):
    """Update a non-hideable fixed HUD reservation, or clear it immediately.

    Does not retain Blender objects. Fixed surfaces are used as obstacles, not
    as interactive windows; their drawing/click ownership remains with caller.
    """
    if not scope:
        return
    scope, key = str(scope), str(key)
    entries = _FIXED.get(scope)
    if rect is None:
        if entries is not None:
            entries.pop(key, None)
            if not entries:
                _FIXED.pop(scope, None)
        return
    from .overlay_placement import rect_xyxy
    bounds = rect_xyxy(rect)
    if entries is None:
        entries = _FIXED.setdefault(scope, {})
    stamp = monotonic() if now is None else float(now)
    if key not in entries and len(entries) >= _LIMIT:
        oldest = min(entries, key=lambda name: entries[name][1])
        entries.pop(oldest, None)
    entries[key] = (bounds, stamp)


def peer_rectangles(scope, key, *, active_within=_PEER_LEASE_SECONDS, now=None):
    """Committed live peer windows and fixed chrome within a WINDOW region.

    Never include our own key, hidden, pending or expired windows. If a modal
    handler disappeared without calling cleanup, its lease expires rather than
    permanently obstructing the next tool. No Blender references are retained.
    """
    if not scope:
        return ()
    scope, key = str(scope), str(key)
    current = monotonic() if now is None else float(now)
    max_age = max(0., float(active_within))
    peers = []
    for name, entry in _ENTRIES.get(scope, {}).items():
        if (name != key and not entry.hidden and entry.rect is not None and
                0 <= current - entry.last_seen <= max_age):
            peers.append((name, entry.rect))
    for name, (bounds, stamp) in _FIXED.get(scope, {}).items():
        if name != key and 0 <= current - stamp <= max_age:
            peers.append((name, bounds))
    return tuple(sorted(peers, key=lambda row: row[0]))


def invalidate_matching(*prefixes):
    """Fail-closed cleanup across viewport regions after a tool stops.

    Preserves user-hidden preferences, but releases obsolete draw/hit rectangles
    immediately rather than waiting for their temporary lease to expire.
    """
    names = tuple(str(prefix) for prefix in prefixes if str(prefix))
    if not names:
        return 0
    count = 0
    for entries in _ENTRIES.values():
        for key, entry in entries.items():
            if key.startswith(names):
                entry.rect = None
                entry.status = 'HIDDEN' if entry.hidden else 'PENDING'
                count += 1
    for scope, entries in tuple(_FIXED.items()):
        for key in tuple(entries):
            if key.startswith(names):
                entries.pop(key, None)
                count += 1
        if not entries:
            _FIXED.pop(scope, None)
    return count


def fixed_snapshot(*, active_within=900., now=None):
    """Read-only bounded diagnostics without RNA or GPU handles."""
    current = monotonic() if now is None else float(now)
    return {scope: [{"key": key, "rect": rect, "seconds_ago": max(0., current - stamp)}
                    for key, (rect, stamp) in sorted(items.items())
                    if 0 <= current-stamp <= active_within]
            for scope, items in sorted(_FIXED.items())
            if any(0 <= current-stamp <= active_within for rect, stamp in items.values())}


def is_hidden(scope, key):
    entry = _ENTRIES.get(str(scope), {}).get(str(key))
    return bool(entry and entry.hidden)


def set_hidden(scope, key, hidden):
    entry = _ENTRIES.get(str(scope), {}).get(str(key))
    if entry is None:
        return False
    entry.hidden = bool(hidden)
    entry.rect = None   # invalidate immediately, before next redraw
    entry.status = 'HIDDEN' if entry.hidden else 'PENDING'
    return True


def restore_all(scope):
    keys=[]
    for entry in _ENTRIES.get(str(scope), {}).values():
        if entry.hidden:
            set_hidden(scope,entry.key,False)
            keys.append(entry.key)
    return tuple(sorted(keys))


def snapshot(scope, *, active_within=900., now=None):
    t = monotonic() if now is None else float(now)
    return tuple(
        dict(key=e.key,title=e.title,hidden=e.hidden,status=e.status,rect=e.rect,
             seconds_ago=max(0.,t-e.last_seen))
        for e in sorted(_ENTRIES.get(str(scope), {}).values(), key=lambda e:e.key)
        if t-e.last_seen <= float(active_within)
    )


def forget(scope, key=None):
    scope = str(scope)
    if key is None:
        _ENTRIES.pop(scope,None)
        _FIXED.pop(scope,None)
    else:
        entries = _ENTRIES.get(scope)
        if entries:
            entries.pop(str(key), None)
            if not entries:
                _ENTRIES.pop(scope,None)
        fixed = _FIXED.get(scope)
        if fixed:
            fixed.pop(str(key), None)
            if not fixed:
                _FIXED.pop(scope, None)


def reset():
    _ENTRIES.clear()
    _FIXED.clear()


def scope_from_context(context, *, area=None, region=None):
    """Resolve a VIEW_3D WINDOW region even when called from its UI sidebar.

    Blender objects are supplied by caller to keep this core module import-free.
    Missing/stale contexts use the old stateless layout behavior.
    """
    try:
        window = context.window
        area = area if area is not None else context.area
        if window is None or area is None or area.type != 'VIEW_3D':
            return None
        if region is None:
            region = next((r for r in area.regions if r.type == 'WINDOW'),None)
        if region is None or region.type != 'WINDOW':
            return None
        return viewport_key(window.as_pointer(),area.as_pointer(),region.as_pointer())
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return None


def snapshot_all(*, active_within=900.):
    """Bounded state for one-click diagnostics, no Blender data references."""
    return {scope:list(snapshot(scope,active_within=active_within))
            for scope in sorted(_ENTRIES) if snapshot(scope,active_within=active_within)}

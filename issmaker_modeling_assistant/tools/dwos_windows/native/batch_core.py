from __future__ import annotations

"""Pure helpers for DWOS Window Core V1.4 multi-window editing."""

from dataclasses import dataclass


@dataclass(frozen=True)
class WindowRecord:
    window_id: str
    host_name: str
    width: float
    height: float


def same_size(a: WindowRecord, b: WindowRecord, tolerance: float = 0.01) -> bool:
    return abs(a.width - b.width) <= tolerance and abs(a.height - b.height) <= tolerance


def scope_ids(records, active_id: str, selected_ids, scope: str, tolerance: float = 0.01):
    rows = list(records)
    active = next((r for r in rows if r.window_id == active_id), None)
    if active is None:
        return []
    if scope == 'ACTIVE':
        return [active.window_id]
    if scope == 'SELECTED':
        wanted = set(selected_ids or [])
        wanted.add(active.window_id)
        return [r.window_id for r in rows if r.window_id in wanted and r.host_name == active.host_name]
    if scope == 'SAME_SIZE':
        return [r.window_id for r in rows if r.host_name == active.host_name and same_size(r, active, tolerance)]
    if scope == 'ALL_HOST':
        return [r.window_id for r in rows if r.host_name == active.host_name]
    return [active.window_id]


def summarize_scope(records, ids):
    wanted = set(ids)
    rows = [r for r in records if r.window_id in wanted]
    sizes = {(round(r.width, 6), round(r.height, 6)) for r in rows}
    return {'count': len(rows), 'size_groups': len(sizes)}

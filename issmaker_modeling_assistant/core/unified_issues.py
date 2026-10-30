"""ISSMAKER PPA Wave 8: finite, bounded, immutable read-only issue presentation.

No bpy/bmesh imports; geometry solvers remain authoritative. In particular a
Collision event name or GPU proximity marker never upgrades an issue to proof.
"""
from __future__ import annotations

from hashlib import blake2b
import json
import math

MAX_ISSUES = 256
MAX_TEXT = 260
MAX_POINTS = 12
KINDS = frozenset(('PROVEN_INVALID', 'UNCERTAIN', 'RESOURCE_LIMIT',
                   'INTERNAL_ERROR', 'SETUP_REQUIRED', 'WARNING', 'REVIEW'))


def _text(value, limit=MAX_TEXT):
    try:
        return str(value or '')[:limit]
    except Exception:
        return '<unreadable>'


def _point(value):
    try:
        if value is None or len(value) != 3:
            return None
        coords = tuple(float(axis) for axis in value)
        return coords if all(math.isfinite(axis) for axis in coords) else None
    except (TypeError, ValueError, OverflowError, RuntimeError):
        return None


def _points(value):
    if not isinstance(value, (list, tuple)):
        return ()
    clean = []
    for item in value[:MAX_POINTS]:
        point = _point(item)
        if point is not None:
            clean.append(point)
    return tuple(clean)


def normalize(entry):
    """Produce one immutable, JSON-safe issue; rejects malformed records.

    PROVEN_INVALID is accepted only when both proof and a finite witness exist.
    Merely reporting SELF_INTERSECTION in a legacy subsystem is not proof.
    """
    if not isinstance(entry, dict):
        return None
    source = _text(entry.get('source', 'UNKNOWN'), 48) or 'UNKNOWN'
    source_kind = _text(entry.get('kind', 'REVIEW'), 48) or 'REVIEW'
    kind = source_kind if source_kind in KINDS else 'REVIEW'
    proof = _text(entry.get('proof', ''), 500)
    coords = _points(entry.get('coordinates', ()))
    code = _text(entry.get('code', 'UNKNOWN'), 96) or 'UNKNOWN'
    entity = _text(entry.get('entity', ''), 160)
    if kind == 'PROVEN_INVALID' and not (proof and coords):
        kind = 'UNCERTAIN'
    if kind == 'REVIEW' and source_kind in ('ERROR', 'CRITICAL'):
        kind = 'WARNING'
    key_payload = (source, code, entity, coords, proof)
    issue_id = blake2b(repr(key_payload).encode('utf-8', 'replace'), digest_size=12).hexdigest()
    return {'id': issue_id, 'source': source, 'source_kind': source_kind,
            'kind': kind, 'code': code, 'message': _text(entry.get('message', '')),
            'entity': entity, 'coordinates': coords, 'proof': proof, 'count': 1}


def collect(groups, max_issues=MAX_ISSUES):
    """Combine source sequences without mutating any input.

    Returns bounded, flat, JSON-compatible snapshots, sources' raw count and
    source failures.  A broken adapter must not hide other sources.
    """
    limit = max(1, min(MAX_ISSUES, int(max_issues)))
    results, seen, failures, source_counts = [], {}, {}, {}
    total = 0
    for source, payload in groups:
        source = _text(source, 48)
        try:
            for item in payload:
                total += 1
                source_counts[source] = source_counts.get(source, 0) + 1
                if not isinstance(item, dict):
                    continue
                candidate = normalize(dict(item, source=source))
                if candidate is None:
                    continue
                previous = seen.get(candidate['id'])
                if previous is not None:
                    previous['count'] += 1
                elif len(results) < limit:
                    seen[candidate['id']] = candidate
                    results.append(candidate)
        except Exception as exc:
            failures[source] = _text('%s: %s' % (type(exc).__name__, exc), 200)
    # Source total includes duplicates; omitted counts reflect cap, not valid errors.
    return {'issues': tuple(results), 'total': total,
            'omitted': max(0, total - sum(row['count'] for row in results)),
            'sources': source_counts, 'source_errors': failures,
            'truncated': total > sum(row['count'] for row in results)}


def page(count, index, direction):
    if count <= 0:
        return -1
    return (int(index) + (1 if direction >= 0 else -1)) % count


def focus_point(issue):
    """Only real finite coordinates permit viewport focus."""
    coords = issue.get('coordinates', ()) if isinstance(issue, dict) else ()
    finite = _points(coords)
    if not finite:
        return None
    # Subtraction relative to first point is more stable than summing huge CAD XYZ.
    origin = finite[0]
    return tuple(origin[i] + math.fsum(point[i] - origin[i] for point in finite) / len(finite)
                 for i in range(3))


def export_json(snapshot):
    return json.dumps(snapshot, ensure_ascii=False, allow_nan=False, default=list)

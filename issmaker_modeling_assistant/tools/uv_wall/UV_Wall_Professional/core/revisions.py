"""ISSMAKER parametric revision ledger v1 (pure Python, bpy-independent).

Revision entries hold *parameters only*, never live Blender RNA pointers or meshes.
A build result remains managed by the original transactional Build/Undo system.
"""
from __future__ import annotations
import hashlib
import json

SCHEMA = 1
PROPERTY = 'uvwall_revisions_v1'
MAX_ENTRIES = 32
MAX_PAYLOAD_BYTES = 262144
MAX_LEDGER_BYTES = 8 * 1024 * 1024


def _validated_payload(payload):
    if not isinstance(payload, str) or len(payload.encode('utf-8')) > MAX_PAYLOAD_BYTES:
        raise ValueError('REVISION_PAYLOAD_LIMIT')
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError('REVISION_PAYLOAD_JSON') from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get('values'), dict):
        raise ValueError('REVISION_PAYLOAD_SCHEMA')
    return parsed


def parse_ledger(raw, owner_uid):
    if not owner_uid or not isinstance(owner_uid, str):
        raise ValueError('REVISION_OWNER_MISSING')
    if raw and (not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_LEDGER_BYTES):
        raise ValueError('REVISION_LEDGER_LIMIT')
    if not raw:
        return {'schema': SCHEMA, 'owner_uid': owner_uid, 'next_id': 1, 'entries': []}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError('REVISION_LEDGER_JSON') from exc
    if not isinstance(data, dict) or data.get('schema') != SCHEMA:
        raise ValueError('REVISION_LEDGER_SCHEMA')
    if data.get('owner_uid') != owner_uid:
        raise ValueError('REVISION_OWNER_MISMATCH')
    entries = data.get('entries')
    if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
        raise ValueError('REVISION_ENTRIES_INVALID')
    if type(data.get('next_id')) is not int or data['next_id'] < 1:
        raise ValueError('REVISION_COUNTER_INVALID')
    previous = 0
    for entry in entries:
        if not isinstance(entry, dict) or type(entry.get('id')) is not int or entry['id'] <= previous:
            raise ValueError('REVISION_ORDER_INVALID')
        parent = entry.get('parent_id')
        if parent is not None and (type(parent) is not int or parent < 1 or parent >= entry['id']):
            raise ValueError('REVISION_PARENT_INVALID')
        previous = entry['id']
        _validated_payload(entry.get('definition'))
        digest = hashlib.sha256(entry['definition'].encode('utf-8')).hexdigest()
        if digest != entry.get('sha256'):
            raise ValueError('REVISION_HASH_MISMATCH')
    if data['next_id'] <= previous:
        raise ValueError('REVISION_COUNTER_STALE')
    return data


def record_revision(raw, owner_uid, definition_payload, *, label='Успешная сборка', limit=MAX_ENTRIES, parent_id=None):
    """Return (JSON ledger, changed, entry). Refuse silent corruption."""
    payload = _validated_payload(definition_payload)
    ledger = parse_ledger(raw, owner_uid)
    digest = hashlib.sha256(definition_payload.encode('utf-8')).hexdigest()
    entries = ledger['entries']
    # Exact duplicate is not a new accepted state, even after revisiting history.
    # Callers must clear the pending parent marker after a successful Build.
    if entries and entries[-1]['sha256'] == digest:
        return json.dumps(ledger, ensure_ascii=False, separators=(',', ':')), False, entries[-1]
    previous = _validated_payload(entries[-1]['definition'])['values'] if entries else {}
    if parent_id is not None:
        parent_entry = next((e for e in entries if e['id'] == int(parent_id)), None)
        if parent_entry is None: raise ValueError('REVISION_PARENT_NOT_FOUND')
        previous = _validated_payload(parent_entry['definition'])['values']
    current = payload['values']
    changed = sorted(k for k in set(previous) | set(current) if previous.get(k) != current.get(k))
    expected_parent = entries[-1]['id'] if entries else None
    if parent_id is not None:
        try: parent_id = int(parent_id)
        except (ValueError, TypeError) as exc: raise ValueError('REVISION_PARENT_INVALID') from exc
        if parent_id not in {e['id'] for e in entries}:
            raise ValueError('REVISION_PARENT_NOT_FOUND')
        expected_parent = parent_id
    field_values = [
        {'field': key, 'before': _compact_value(previous.get(key)),
         'after': _compact_value(current.get(key))}
        for key in changed[:24]
    ]
    entry = {
        'parent_id': expected_parent,
        'id': ledger['next_id'], 'sha256': digest, 'mode': str(current.get('generator_type', 'WALL')),
        'label': str(label)[:100], 'fields_changed': changed[:80],
        'change_count': len(changed), 'field_values': field_values,
        'definition': definition_payload,
    }
    entries.append(entry)
    ledger['entries'] = entries[-min(MAX_ENTRIES, max(1, int(limit))):]
    ledger['next_id'] += 1
    encoded = json.dumps(ledger, ensure_ascii=False, separators=(',', ':'))
    if len(encoded.encode('utf-8')) > MAX_LEDGER_BYTES:
        raise ValueError('REVISION_LEDGER_LIMIT')
    return encoded, True, entry


def revision_payload(raw, owner_uid, revision_id):
    ledger = parse_ledger(raw, owner_uid)
    for entry in ledger['entries']:
        if entry['id'] == int(revision_id):
            return entry['definition']
    raise ValueError('REVISION_NOT_FOUND')


# UI-only semantics. Existing ledger v1 entries without field_values are supported.
_FIELD_LABELS = {
    'inset_distance':'Толщина', 'wall_offset':'Смещение',
    'stories':'Этажи', 'first_story_height':'Высота первого этажа',
    'inner_wall_height_delta':'Высота доп. стены',
    'create_inner':'Дополнительная стена', 'create_top_cap':'Верхняя крышка',
    'create_bottom_cap':'Нижняя крышка', 'wall_alignment':'Сторона',
    'generator_type':'Тип инструмента', 'surface_height_offset':'Смещение поверхности',
    'cornice_profile_rotation':'Поворот карниза',
}

def _compact_value(value):
    if value is None: return '—'
    if isinstance(value, bool): return 'Вкл' if value else 'Выкл'
    if isinstance(value, float): return f'{value:.5g}'
    if isinstance(value, (dict, list)): return 'параметры'
    text = str(value)
    return text if len(text) <= 34 else text[:31] + '…'

def describe_revision(entry, ledger=None, max_fields=2):
    """Return a short human-readable diff; no Blender dependency."""
    changes = entry.get('field_values')
    if changes is None and ledger is not None:
        parent = next((e for e in ledger.get('entries', []) if e.get('id') == entry.get('parent_id')), None)
        if parent is not None:
            old = _validated_payload(parent['definition'])['values']
            new = _validated_payload(entry['definition'])['values']
            keys = sorted(k for k in set(old)|set(new) if old.get(k) != new.get(k))
            changes = [{'field':k,'before':_compact_value(old.get(k)), 'after':_compact_value(new.get(k))} for k in keys]
    if not changes:
        fields = entry.get('fields_changed') or []
        return ', '.join(_FIELD_LABELS.get(f, f) for f in fields[:max_fields]) or 'Первое принятие'
    parts=[]
    for item in changes[:max_fields]:
        key = str(item.get('field', ''))
        parts.append(f"{_FIELD_LABELS.get(key, key)}: {item.get('before','?')} → {item.get('after','?')}")
    return ' · '.join(parts)[:180]

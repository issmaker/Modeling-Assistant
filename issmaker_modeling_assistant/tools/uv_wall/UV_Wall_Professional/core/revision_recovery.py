"""Verified, all-or-rollback restoration for accepted UV Wall definitions.

This module cannot mutate generated Mesh. The builder is the sole commit path.
"""
from .revisions import _validated_payload
from .definitions import (SNAPSHOT_FIELDS, _POINTER_COLLECTIONS, snapshot_definition,
                          restore_definition, apply_serialized_definition)

PROTECTED = ('source_uid', 'last_result_name', 'last_committed_revision',
             'last_successful_settings_json')

def _equal(expected, actual):
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is type(actual) and expected == actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        # Blender RNA can store float32; rounding is not a failed assignment.
        return abs(float(expected)-float(actual)) <= max(2e-7, abs(float(expected))*2e-6)
    return expected == actual

def atomic_restore_definition(definition, payload, bpy_data, sync=None):
    """Apply a historical parameter state or restore every RNA field.

    sync is a scene sync callback. The caller manages the path parent marker,
    installing it only if the entire operation succeeds.
    """
    if definition is None:
        raise ValueError('REVISION_NO_DEFINITION')
    data = _validated_payload(payload)
    values = data['values']
    allowed = set(SNAPSHOT_FIELDS) - set(PROTECTED)
    if any(name not in allowed for name in values):
        raise ValueError('REVISION_UNKNOWN_OR_PROTECTED_FIELD')
    # A missing asset is not silently replaced with None: the committed
    # mesh and current definition must survive until user fixes dependencies.
    for name, value in values.items():
        if name in _POINTER_COLLECTIONS and value:
            collection = getattr(bpy_data, _POINTER_COLLECTIONS[name])
            if collection.get(value) is None:
                raise ValueError(f'REVISION_MISSING_ASSET:{name}:{value}')
    before = snapshot_definition(definition)
    if before is None:
        raise ValueError('REVISION_SNAPSHOT_FAILED')
    protected_before = {name: getattr(definition, name, None) for name in PROTECTED}
    try:
        if not apply_serialized_definition(definition, payload):
            raise ValueError('REVISION_APPLY_FAILED')
        for name, value in values.items():
            actual = getattr(definition, name)
            if name in _POINTER_COLLECTIONS:
                actual = getattr(actual, 'name', None) if actual is not None else None
            if not _equal(value, actual):
                raise ValueError(f'REVISION_FIELD_NOT_APPLIED:{name}')
        for name in PROTECTED:
            if getattr(definition, name, None) != protected_before[name]:
                raise ValueError(f'REVISION_PROTECTED_FIELD_CHANGED:{name}')
        if sync is not None:
            sync()
        return before
    except Exception:
        restore_definition(definition, before)
        if sync is not None:
            try: sync()
            except Exception: pass
        raise

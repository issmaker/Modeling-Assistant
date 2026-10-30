"""Undo-aware parameter commit adapter. Build is the ONLY operator with UNDO.

Floating/native parameter editors submit a serialized Definition target. Nothing
in the saved scene changes before Blender enters the Build operator's execute().
An unchanged payload never creates an Undo step. This module does not mutate
geometry and cannot push Blender history itself.
"""
from __future__ import annotations
import json
from math import isfinite
from contextlib import contextmanager


def height_patch(module, total_height):
    """Same decomposition as core.definitions.set_total_wall_height; no RNA writes."""
    height = float(total_height)
    width = float(module)
    if not isfinite(height) or not isfinite(width) or width <= 0:
        raise ValueError('Некорректная высота или размер модуля (NaN/Infinity запрещены)')
    height = max(0.001, height)
    stories = max(1, int((height + width - 1.0e-9) // width))
    if stories * width < height - 1.0e-9:
        stories += 1
    first = height - width * (stories - 1)
    if first <= 1.0e-9:
        first = width
    custom = abs(first - width) > 1.0e-8
    return {'stories': stories, 'custom_first_story': custom,
            'first_story_height': first if custom else 0.0}


def definition_patch_payload(definition, changes, *, height=None):
    """Build a target Definition without applying changes to real Blender data."""
    from .definitions import serialize_definition, SNAPSHOT_FIELDS
    content = json.loads(serialize_definition(definition))
    values = content['values']
    before = dict(values)
    patch = dict(changes)
    if height is not None:
        patch.update(height_patch(values['module_standard'], height))
    for name, value in patch.items():
        if name not in SNAPSHOT_FIELDS or name in {'last_result_name', 'last_error', 'last_committed_revision'}:
            raise ValueError(f'Нельзя изменить параметр: {name}')
        if name in {'cornice_profile', 'inner_surface_material'} and value is not None and not isinstance(value, str):
            value = value.name
        values[name] = value
    if all(values.get(key) == before.get(key) for key in patch):
        return ''
    return json.dumps(content, ensure_ascii=False, separators=(',', ':'))


def _clear_pending_commit(wm):
    """Best-effort runtime cleanup, including an RNA setter failure.

    Never replace the original Build exception with a temporary flag cleanup
    error.  These properties are ephemeral (SKIP_SAVE), not authored geometry.
    """
    for name, value in (
        ('uvwall_pending_definition_json', ''),
        ('uvwall_pending_surface_edits_json', ''),
        ('uvwall_viewport_commit_active', False),
    ):
        try:
            setattr(wm, name, value)
        except (AttributeError, ReferenceError, RuntimeError, TypeError):
            pass


@contextmanager
def pending_commit(wm, payload):
    """Guard pending payload with rollback, even if initial RNA writes fail."""
    if wm is None:
        raise ValueError('UV Wall: WindowManager недоступен для транзакции')
    if getattr(wm, 'uvwall_viewport_commit_active', False):
        raise ValueError('UV Wall: другая транзакция ещё выполняется')
    try:
        wm.uvwall_viewport_commit_active = True
        wm.uvwall_pending_definition_json = payload
        wm.uvwall_pending_surface_edits_json = ''
    except Exception:
        _clear_pending_commit(wm)
        raise
    try:
        yield
    finally:
        _clear_pending_commit(wm)


def commit_definition_patch(context, definition, changes, *, height=None):
    """Dispatch one nested *Build* UNDO operator; outer dialog itself has NO UNDO."""
    from ..operators.build_context import object_mode_error, execute_build
    reason = object_mode_error(context)
    if reason:
        raise ValueError(reason)
    payload = definition_patch_payload(definition, changes, height=height)
    if not payload:
        return False
    def audit(event, reason=''):
        try:
            settings = context.scene.uv_wall_settings
            if getattr(settings, 'diagnostic_detail', 'NORMAL') != 'FORENSIC':
                return
            from ..production.validation import log_event
            log_event(f"TX_{event} source_uid={getattr(definition,'source_uid','')}; "
                      f"fields={','.join(changes)}; {reason}", 'TRANSACTION')
        except Exception:
            pass
    audit('BEGIN')
    try:
        with pending_commit(context.window_manager, payload):
            result = execute_build(context)
        if 'FINISHED' not in result:
            raise ValueError('Построение отменено — предыдущие параметры сохранены')
    except Exception as exc:
        audit('ROLLBACK', type(exc).__name__)
        raise
    audit('COMMIT')
    return True

"""ISSMAKER strict exclusive handoff (Blender-free coordination).

The coordinator never launches tools or touches Mesh data. Caller invokes target
preflight only after this function verifies every other registered owner stopped.
"""
from collections import deque
from . import action_trace

_BUSY = False
_EVENTS = deque(maxlen=100)
_SEQUENCE = 0


def _trace(action, owner, detail=''):
    row = {'phase': str(action), 'tool': str(owner), 'detail': str(detail)[:480]}
    _EVENTS.append(row)
    try:
        action_trace.record('HANDOFF_' + action, owner, detail)
    except Exception:
        # Failure of diagnostics cannot prevent genuine resource release.
        pass


def snapshot():
    """No bpy or mesh references; bounded, read-only lifecycle evidence."""
    return {'schema': 'issmaker.handoff.2', 'in_progress': _BUSY,
            'completed_sequence': _SEQUENCE, 'events': list(_EVENTS)}


def stop_others(target, context, adapters):
    """Stop all non-target owners, then recheck every owner. Never preflight target.

    Backward-compatible result (success, explanation). No target launch, no
    geometry edits, and no previous-tool resurrection on failure.
    """
    global _BUSY, _SEQUENCE
    target = str(target)
    if _BUSY:
        _trace('REENTRANT_BLOCKED', target, 'previous handoff is still active')
        return False, 'Переключение уже выполняется; повторная команда отменена'
    _BUSY = True
    failures = []
    rows = tuple(adapters)
    try:
        names = [str(row[0]) for row in rows]
        if len(names) != len(set(names)):
            _trace('BAD_REGISTRY', target, 'duplicate adapter IDs')
            return False, 'Внутренняя ошибка переключения: повторяющиеся владельцы инструментов'
        _trace('REQUEST', target, 'registered=' + ','.join(names))
        for name, active, stop in rows:
            if name == target:
                continue
            try:
                is_running = bool(active())
            except Exception as exc:
                detail = f'{name}: проверка активности: {type(exc).__name__}: {exc}'
                failures.append(detail)
                _trace('CHECK_FAILED', name, detail)
                continue
            if not is_running:
                continue
            _trace('STOP_REQUEST', name, 'next=' + target)
            try:
                if stop(context) is False:
                    raise RuntimeError('очистка возвратила False')
                _trace('STOP_OK', name, 'next=' + target)
            except Exception as exc:
                detail = f'{name}: ошибка закрытия: {type(exc).__name__}: {exc}'
                failures.append(detail)
                _trace('STOP_FAILED', name, detail)
        # A final verification is necessary even if an adapter reported success.
        # Never infer 'closed' just because the UI rectangle disappeared.
        for name, active, _stop in rows:
            if name == target:
                continue
            try:
                if bool(active()):
                    detail = f'{name}: инструмент остаётся активным после закрытия'
                    failures.append(detail)
                    _trace('VERIFY_FAILED', name, detail)
            except Exception as exc:
                detail = f'{name}: проверка после закрытия: {type(exc).__name__}: {exc}'
                failures.append(detail)
                _trace('VERIFY_FAILED', name, detail)
        if failures:
            _trace('BLOCKED', target, ' | '.join(failures)[:480])
            return False, ' | '.join(failures)
        _trace('VERIFIED', target, 'other tool owners inactive')
        return True, ''
    finally:
        _SEQUENCE += 1
        _BUSY = False

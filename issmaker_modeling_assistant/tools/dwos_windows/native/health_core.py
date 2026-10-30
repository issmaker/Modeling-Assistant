"""Pure, no-Blender DWOS Health classifier (no geometry or redraw side effects)."""
from __future__ import annotations

_LEVELS = {'OK':0, 'FINALIZED':0, 'CONTACT':1, 'PENDING':2, 'ERROR':3}


def classify(*, is_window, finalized=False, pending=False, reason_code='',
             validation_error='', contact_count=0, classification='PLANAR',
             glass_layers=None, frame_geometry_locked=None, smart_enabled=True):
    """Return a durable small state dictionary for UI, support ZIP and CLI QA.

    Never infer geometry validity from absent optional metadata. More expensive
    exact validation belongs to explicit geometry checks, not the draw callback.
    """
    if not is_window:
        return {'code':'NO_WINDOW','severity':0,'label':'Нет окна DWOS','detail':'','needs_attention':False}
    reason = str(validation_error or '').strip()
    if reason and not (pending and reason.lower().startswith('smart interior:')):
        code='ERROR';detail=reason
    elif finalized:
        code='FINALIZED';detail='Геометрия финализирована'
    elif pending:
        code='PENDING';detail=f'Interior ожидает согласования · {reason_code or "CONTACT_UNRESOLVED"}'
    elif classification in {'CURVED','CORNER'} and (
        (glass_layers is not None and str(glass_layers) != '2') or frame_geometry_locked is False):
        code='ERROR';detail='Нарушен FULL FRAME / 2 GLASS'
    elif smart_enabled and contact_count > 0:
        code='CONTACT';detail=f'Связанные Interior: {int(contact_count)}'
    else:
        code='OK';detail='Нарушения не зарегистрированы'
    return {'code':code,'severity':_LEVELS[code],
            'label':{'OK':'Готово','FINALIZED':'Финализировано','CONTACT':'Контакт согласован',
                     'PENDING':'Ожидает Interior','ERROR':'Нужна проверка'}[code],
            'detail':detail,'needs_attention':code in {'PENDING','ERROR'}}

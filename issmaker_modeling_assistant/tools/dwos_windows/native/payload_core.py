"""Pure serialization guards for DWOS opening payloads (3.5.3)."""
from __future__ import annotations
import json


class OpeningPayloadError(ValueError):
    pass


def decode_opening_payload(raw):
    text='{}' if raw is None or not str(raw).strip() else str(raw)
    try:
        payload=json.loads(text)
    except Exception as exc:
        raise OpeningPayloadError('Повреждены данные проёма DWOS (opening payload). Выполните «Найти проёмы» повторно; автоматический legacy fallback запрещён.') from exc
    if not isinstance(payload,dict):
        raise OpeningPayloadError('Данные проёма DWOS имеют неверный формат. Выполните «Найти проёмы» повторно.')
    return payload


def adaptive_topology_from_payload(payload):
    if not isinstance(payload,dict):
        raise OpeningPayloadError('Opening payload должен быть объектом JSON.')
    declared=payload.get('coordinate_system')=='SVD_RIBBON' or 'adaptive_topology' in payload
    topo=payload.get('adaptive_topology')
    if not declared:
        return None
    if not isinstance(topo,dict):
        raise OpeningPayloadError('Adaptive Opening payload повреждён: отсутствует topology. Выполните «Найти проёмы» повторно.')
    if topo.get('coordinate_system')!='SVD_RIBBON':
        raise OpeningPayloadError('Adaptive Opening payload имеет неподдерживаемую систему координат. Выполните «Найти проёмы» повторно.')
    return topo

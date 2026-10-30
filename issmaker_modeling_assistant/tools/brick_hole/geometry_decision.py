"""Brick Hole compatibility facade for the shared decision taxonomy."""
from __future__ import annotations
from ...core.reliability.decision import (
    PROVEN_INVALID, RESOURCE_LIMIT, SETUP_REQUIRED, INTERNAL_ERROR,
    UNCERTAIN, prefix as _prefix,
)

# Historical name kept for existing callers/tests.
INTERNAL_FAILURE=INTERNAL_ERROR


def classify_failure(value):
    text=str(value or '')
    upper=text.upper()
    if any(token in upper for token in ('PRECISION_UNCERTAIN','NUMERICAL_AMBIGUITY','UNCERTAIN')):
        return UNCERTAIN
    if any(token in upper for token in ('ЛИМИТ','СЛИШКОМ БОЛЬШ','СЛИШКОМ МНОГО','ОПЕРАЦИЯ СЛИШКОМ ТЯЖ',
                                        'RESOURCE','БЮДЖЕТ','РАЗДЕЛИТЕ ОПЕРАЦИЮ','УМЕНЬШИТЕ ВЫДЕЛЕНИЕ')):
        return RESOURCE_LIMIT
    if any(token in upper for token in ('НУЖЕН MESH','EDIT MODE','РЕЖИМ ВЫДЕЛЕНИЯ','UV-КАРТ',
                                        'APPLY SCALE','CTRL+A','UNIT SCALE','SHAPE KEYS','МАТЕРИАЛА НЕТ',
                                        'ВЫБЕРИТЕ РЁБРА','ВЫДЕЛИТЕ ПОЛИГОНЫ','ЗАДАЙТЕ НОРМАЛ','BRICK_HOLE_UNSUPPORTED_MULTIPLANE')):
        return SETUP_REQUIRED
    if any(token in upper for token in ('SELF_INTERSECTION','САМОПЕРЕСЕЧ','ДОКАЗАНН','ZERO_EDGE','НУЛЕВО',
                                        'NONMANIFOLD','NON-MANIFOLD','NAN','БЕСКОНЕЧ','COLLISION:',
                                        'ВЫРОЖДЕН','НЕПЛОСК','NONPLANAR','AREA_MISMATCH','BOUNDARY_MISMATCH',
                                        'TOPOLOGY:','UV_NONLINEAR','ИСКАЖЕНИЕ UV')):
        return PROVEN_INVALID
    return INTERNAL_ERROR


def user_prefix(kind):
    return _prefix(kind)


def format_failure(value):
    kind=classify_failure(value)
    message=f'[{user_prefix(kind)}] {value}'
    try:
        from ...core.reliability.ledger import record
        from ...core.reliability.decision import Decision
        record(Decision(kind,'BRICK_HOLE_LEGACY',str(value),{},'BRICK_HOLE','legacy-classified' if kind==PROVEN_INVALID else ''),source='brick_hole.geometry_decision')
    except Exception:
        pass
    return kind, message

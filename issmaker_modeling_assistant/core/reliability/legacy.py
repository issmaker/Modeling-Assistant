"""Conservative classification for validators not yet migrated to Decision objects.

Legacy text is never promoted to PROVEN_INVALID merely because it contains a
word like 'intersection'.  Only explicit proof markers may claim hard geometry
proof; this prevents old heuristic validators from blaming user geometry.
"""
from __future__ import annotations
from .decision import (PROVEN_INVALID,UNCERTAIN,RESOURCE_LIMIT,SETUP_REQUIRED,INTERNAL_ERROR,prefix)


def classify_text(value):
    text=str(value or '');u=text.upper()
    if any(x in u for x in ('PRECISION_UNCERTAIN','NUMERICAL_AMBIGUITY','NEAR_MISS','UNCERTAIN')):
        return UNCERTAIN
    if any(x in u for x in ('RESOURCE_LIMIT','БЮДЖЕТ','СЛИШКОМ МНОГО','СЛИШКОМ БОЛЬШ','ЛИМИТ','MAX_GENERATED','MAX_ROOF')):
        return RESOURCE_LIMIT
    if any(x in u for x in ('ВЫБЕРИТЕ','НЕ ВЫБРАН','APPLY SCALE','CTRL+A','EDIT MODE','OBJECT MODE','UV-КАРТ','MISSING','REQUIRED')):
        return SETUP_REQUIRED
    if any(x in u for x in ('PROVEN_INVALID','ДОКАЗАННО','EXACT_PROOF')):
        return PROVEN_INVALID
    # A legacy geometry-sounding exception is not sufficient proof by itself.
    if any(x in u for x in ('INTERSECTION','ПЕРЕСЕЧ','NONPLANAR','НЕПЛОСК','DEGENERATE','ВЫРОЖД','OVERLAP','ПЕРЕКРЫ')):
        return UNCERTAIN
    return INTERNAL_ERROR


def format_legacy(tool,value):
    kind=classify_text(value)
    return kind,f'[{prefix(kind)}] {value}'

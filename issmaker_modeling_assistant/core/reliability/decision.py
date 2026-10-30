"""Common decision taxonomy for ISSMAKER geometry tools.

Hard blocks are represented only by ``PROVEN_INVALID`` or explicit setup
requirements.  Numerical uncertainty and resource exhaustion are not proof of
bad geometry and therefore must remain distinguishable in UI and diagnostics.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

VALID = 'VALID'
PROVEN_INVALID = 'PROVEN_INVALID'
UNCERTAIN = 'UNCERTAIN'
RESOURCE_LIMIT = 'RESOURCE_LIMIT'
SETUP_REQUIRED = 'SETUP_REQUIRED'
INTERNAL_ERROR = 'INTERNAL_ERROR'

BLOCKING_KINDS = frozenset({PROVEN_INVALID, SETUP_REQUIRED, INTERNAL_ERROR})
NON_PROOF_KINDS = frozenset({UNCERTAIN, RESOURCE_LIMIT})

@dataclass(frozen=True)
class Decision:
    kind: str
    code: str
    message: str
    witness: Mapping[str, Any] = field(default_factory=dict)
    tool: str = ''
    proof: str = ''

    @property
    def blocking(self) -> bool:
        return self.kind in BLOCKING_KINDS

    @property
    def is_geometry_proof(self) -> bool:
        return self.kind == PROVEN_INVALID and bool(self.proof)

    def as_dict(self):
        return {
            'kind': self.kind, 'code': self.code, 'message': self.message,
            'tool': self.tool, 'proof': self.proof,
            'blocking': self.blocking, 'witness': dict(self.witness),
        }


def _decision(kind, code, message, *, witness=None, tool='', proof=''):
    if kind == PROVEN_INVALID and not proof:
        raise ValueError('PROVEN_INVALID requires an explicit proof description')
    return Decision(kind, str(code), str(message), dict(witness or {}), str(tool), str(proof))


def valid(code='OK', message='OK', **kwargs):
    return _decision(VALID, code, message, **kwargs)

def proven_invalid(code, message, *, proof, **kwargs):
    return _decision(PROVEN_INVALID, code, message, proof=proof, **kwargs)

def uncertain(code, message, **kwargs):
    return _decision(UNCERTAIN, code, message, **kwargs)

def resource_limit(code, message, **kwargs):
    return _decision(RESOURCE_LIMIT, code, message, **kwargs)

def setup_required(code, message, **kwargs):
    return _decision(SETUP_REQUIRED, code, message, **kwargs)

def internal_error(code, message, **kwargs):
    return _decision(INTERNAL_ERROR, code, message, **kwargs)


_PREFIX = {
    VALID: 'ГОТОВО',
    PROVEN_INVALID: 'ГЕОМЕТРИЯ ДОКАЗАННО НЕКОРРЕКТНА',
    UNCERTAIN: 'МАТЕМАТИКА НЕ ДАЛА ДОКАЗАТЕЛЬСТВА',
    RESOURCE_LIMIT: 'ОГРАНИЧЕНИЕ РЕСУРСОВ',
    SETUP_REQUIRED: 'НУЖНА НАСТРОЙКА',
    INTERNAL_ERROR: 'ВНУТРЕННЯЯ ОШИБКА ISSMAKER',
}

def prefix(kind):
    return _PREFIX.get(kind, 'ВНУТРЕННЯЯ ОШИБКА ISSMAKER')

def format_decision(decision: Decision) -> str:
    return f'[{prefix(decision.kind)}] {decision.message}'

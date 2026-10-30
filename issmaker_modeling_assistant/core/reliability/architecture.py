"""Machine-readable architecture contract for ISSMAKER Reliability Foundation."""
from __future__ import annotations
from . import KERNEL_VERSION

FOUNDATION_ID='ISSMAKER_RELIABILITY_FOUNDATION_1_0_20261026'

INVARIANTS=(
    'Hard geometry block requires reproducible proof, not proximity epsilon alone.',
    'UNCERTAIN and RESOURCE_LIMIT are never presented as proven bad geometry.',
    'Broad-phase tolerances nominate candidates; refined/exact predicates decide hard contact.',
    'Geometry decisions must be translation-stable for large CAD coordinates.',
    'Source XYZ/UV/materials are not mutated by read-only validation.',
    'Every hard block should carry bounded witness data when the caller has entity coordinates.',
)

ADAPTERS={
    'BRICK_HOLE':{
        'shared_predicates':True,'shared_decision_taxonomy':True,
        'coverage':'robust segment predicates, area vector, failure classification',
    },
    'COLLISION_INSPECTOR':{
        'shared_predicates':True,'shared_decision_taxonomy':True,
        'coverage':'shared-edge proof, exact segment/triangle crossing, exact coplanar positive-area overlap',
    },
    'DWOS':{
        'shared_predicates':True,'shared_decision_taxonomy':True,
        'coverage':'window scalar validation and spatial safety; Smart Interior inherits spatial kernel',
    },
    'UV_WALL':{
        'shared_predicates':True,'shared_decision_taxonomy':True,
        'coverage':'planar integrity, cap/ribbon boundary intersections and translation-stable area',
    },
    'CORNICE':{
        'shared_predicates':True,'shared_decision_taxonomy':True,
        'coverage':'inherits UV Wall planar/cap integrity; reach heuristic remains advisory',
    },
    'SMART_INTERIOR':{
        'shared_predicates':True,'shared_decision_taxonomy':True,
        'coverage':'inherits DWOS spatial safety hard-contact kernel',
    },
}


def snapshot():
    return {'foundation_id':FOUNDATION_ID,'kernel_version':KERNEL_VERSION,
            'invariants':list(INVARIANTS),'adapters':ADAPTERS}

"""BH0473: Blender-native, BMesh-independent fail-closed collision budget regression.

Run in Blender:
    from issmaker_modeling_assistant.tools.brick_hole.bh0473_native_cases import (
        run_native_collision_budget_cases)
    rows = run_native_collision_budget_cases()

No Blender data or user scene is modified by these checks.
"""
from .swept_collision_guard import validate_swept_edges


def run_native_collision_budget_cases():
    """Check evidence, error taxonomy and exact-limit behavior for both broad phases."""
    a, b, c, d = object(), object(), object(), object()
    strip = {
        'edge': (a, b),
        'quad': ((0., 0., 0.), (2., 0., 0.), (2., 0., .4), (0., 0., .4)),
    }
    wall = [
        {
            'face_index': 0,
            'tri': ((0., 0., 0.), (2., 0., 0.), (2., 2., 0.)),
            'verts': (a, b, c),
            'linked_edges': ((a, b), (b, c)),
        },
        {
            'face_index': 0,
            'tri': ((0., 0., 0.), (2., 2., 0.), (0., 2., 0.)),
            'verts': (a, c, d),
            'linked_edges': ((a, b), (b, c), (c, d), (d, a)),
        },
    ]
    intruder = {
        'face_index': 1,
        'tri': ((1., -.5, .15), (1., .5, .15), (1., 0., .55)),
        'verts': (object(), object(), object()),
        'linked_edges': (),
    }
    rows = []

    def record(name, obstacle_faces, limit, method, expected, *, use_statistics=True):
        statistics = {} if use_statistics else None
        witnesses = []
        tested = None
        failure = None
        try:
            tested = validate_swept_edges(
                [strip], obstacle_faces, broadphase=method,
                max_candidate_pairs=limit, statistics=statistics,
                witness=witnesses.append)
        except ValueError as exc:
            failure = str(exc)
        if expected == 'VALID':
            ok = failure is None and (statistics is None or
                                      statistics.get('validation_state') == 'VALID')
        elif expected == 'RESOURCE_LIMIT':
            ok = bool(failure and failure.startswith('RESOURCE_LIMIT:'))
            if statistics is not None:
                ok = ok and statistics.get('validation_state') == 'UNCERTAIN'
                ok = ok and statistics.get('budget_exhausted') is True
            ok = ok and not any(w.get('kind') in ('source_contact', 'source_overlap')
                                for w in witnesses)
        elif expected == 'COLLISION':
            ok = bool(failure and failure.startswith('COLLISION:'))
            ok = ok and any(w.get('kind') in ('source_contact', 'source_overlap')
                            for w in witnesses)
        else:
            raise AssertionError('Unexpected expected outcome')
        rows.append({
            'case': name, 'status': 'PASS' if ok else 'FAIL',
            'evidence': {
                'outcome': expected, 'error': failure, 'checked': tested,
                'state': statistics.get('validation_state') if statistics is not None else None,
                'budget_exhausted': statistics.get('budget_exhausted') if statistics is not None else None,
                'witness_kinds': [w.get('kind') for w in witnesses],
                'broadphase': method, 'limit': limit,
            },
        })

    for method in ('X_SWEEP', 'BVH'):
        record('BH0473_LEGAL_SHARED_SEAM_' + method, wall, 50000, method, 'VALID')
        record('BH0473_PROVEN_INTERSECTION_' + method, wall + [intruder],
               50000, method, 'COLLISION')
        # The allowed shared seam consumes the first pair. The real intrusion
        # occurs later: budget=1 MUST NOT silently pass the untested contact.
        record('BH0473_HIDDEN_INTERSECTION_BUDGET_' + method,
               wall + [intruder], 1, method, 'RESOURCE_LIMIT')
        record('BH0473_INCOMPLETE_LEGAL_GEOMETRY_' + method,
               wall, 1, method, 'RESOURCE_LIMIT')
        record('BH0473_EMPTY_OBSTACLES_' + method, [], 1, method, 'VALID')
        record('BH0473_NO_STATS_STILL_FAILS_CLOSED_' + method,
               wall + [intruder], 1, method, 'RESOURCE_LIMIT',
               use_statistics=False)
        # Exact budget without an additional candidate must be accepted.
        count = validate_swept_edges([strip], wall, broadphase=method,
                                     max_candidate_pairs=50000)
        record('BH0473_EXACT_BUDGET_' + method, wall, count, method, 'VALID')
    return rows

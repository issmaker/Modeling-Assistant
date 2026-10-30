"""Pass 15.49.3 — bounded, non-mutating, role-aware triangle diagnostics.

The caller provides actual *render triangulation* loop triangles.  This module
never changes winding, UVs, normals or materials, never infers unknown roles,
and never claims complete verification from a FAST sample.  Face roles mean
local authored construction orientation; object mirroring is reported separately.
"""
from __future__ import annotations

import math


WALL_ROLES = {
    0: 'UNKNOWN', 10: 'WALL_OUTER', 11: 'WALL_INNER',
    12: 'END_CAP', 20: 'TOP_CAP', 21: 'BOTTOM_CAP',
    30: 'SURFACE', 40: 'CORNICE',
}


def role_anchor(role, family='UNKNOWN', interior_sign=-1, adaptive=False):
    """Return an expected LOCAL +Z/-Z *hint* only when the role contract says so.

    Walls and end caps depend on path tangent: cannot be inferred from a role
    alone. Adaptive DWOS uses a varying local tangent, never a flat Z anchor.
    """
    role = str(role or 'UNKNOWN').upper()
    family = str(family).upper()
    if family == 'WALL':
        if role in {'TOP_CAP', 'SURFACE'}:
            return (0., 0., 1.), 0.0  # upward, not necessarily horizontal
        if role == 'BOTTOM_CAP':
            return (0., 0., -1.), 0.0
    elif family == 'DWOS' and not adaptive:
        if interior_sign not in (-1, 1):
            raise ValueError('ROLE_INVALID_INTERIOR_SIGN')
        if role in {'FRAME_EXTERIOR', 'GLASS_EXTERIOR', 'GLASS_SINGLE', 'INTERIOR_BACK'}:
            return (0., 0., float(-interior_sign)), .75
        if role in {'FRAME_INTERIOR_BACK', 'GLASS_INTERIOR', 'INTERIOR_V_BACK_RETURN'}:
            return (0., 0., float(interior_sign)), .75
    return None


def _finite_point(p, ndim):
    try:
        v = tuple(float(x) for x in p)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError('ROLE_INVALID_COORDINATES') from exc
    if len(v) != ndim or not all(math.isfinite(x) for x in v):
        raise ValueError('ROLE_NONFINITE_COORDINATES')
    return v


def _triangle_normal(a, b, c):
    # Scaling before multiply avoids overflow, and non-finite subtraction fails
    # rather than producing a bogus orientation verdict.
    v = tuple(b[i] - a[i] for i in range(3))
    w = tuple(c[i] - a[i] for i in range(3))
    span = max(abs(x) for x in v+w)
    if not math.isfinite(span):
        raise ValueError('ROLE_GEOMETRY_OVERFLOW')
    if span == 0.:
        return None
    x, y, z = (q/span for q in v)
    u, v, w = (q/span for q in w)
    n = (y*w-z*v, z*u-x*w, x*v-y*u)
    size = math.hypot(*n)
    if not math.isfinite(size) or size <= 1.e-13:
        return None
    return tuple(q/size for q in n)


def _uv_sign(uvs):
    if uvs is None:
        return None
    a,b,c=(_finite_point(p,2) for p in uvs)
    v=tuple(b[i]-a[i] for i in range(2))
    w=tuple(c[i]-a[i] for i in range(2))
    span=max(abs(x) for x in v+w)
    if not math.isfinite(span):
        raise ValueError('ROLE_UV_OVERFLOW')
    if span == 0.:return 0
    det=(v[0]/span)*(w[1]/span)-(v[1]/span)*(w[0]/span)
    if not math.isfinite(det) or abs(det) <= 1.e-13:return 0
    return 1 if det>0 else -1


def _sample_indices(n, mode, budget):
    if mode == 'FULL':
        return range(n)
    if mode != 'FAST':
        raise ValueError('ROLE_INVALID_MODE')
    if not isinstance(budget, int) or not 1 <= budget <= 4096:
        raise ValueError('ROLE_INVALID_SAMPLE_BUDGET')
    count = min(n, budget)
    if count == n:return range(n)
    if count == 1:return (0,)
    return (i*(n-1)//(count-1) for i in range(count))


def scan_triangles(triangle_count, triangle_at, vertex_at, role_at, *,
                   family='UNKNOWN', interior_sign=-1, adaptive=False,
                   mode='FAST', sample_budget=256, full_limit=350000):
    """Scan real triangles by random-access callbacks; does not copy full mesh.

    triangle_at(i) -> (face_index, (v0,v1,v2), (uv0,uv1,uv2)|None)
    role_at(face_index) -> role name, else 'UNKNOWN'.
    FULL limit is a genuine block, never substituted with FAST without consent.
    """
    if not isinstance(triangle_count, int) or triangle_count < 0:
        raise ValueError('ROLE_INVALID_TRIANGLE_COUNT')
    if mode == 'FULL' and triangle_count > full_limit:
        raise ValueError('ROLE_FULL_LIMIT_EXCEEDED')
    indices=_sample_indices(triangle_count, mode, sample_budget)
    report = dict(mode=mode, total_triangles=triangle_count, scanned_triangles=0,
                  anchored_triangles=0, unanchored_triangles=0, role_conflicts=0,
                  geometry_degenerate=0, uv_degenerate=0, uv_negative=0,
                  uv_positive=0, uv_missing=0, uv_mixed_faces=0,
                  issue_samples=[], confidence='PARTIAL' if mode == 'FAST' else 'TRIANGULATED')
    face_signs={}
    for ti in indices:
        fi, vertex_ids, uvs=triangle_at(ti)
        if len(vertex_ids)!=3 or (uvs is not None and len(uvs)!=3):
            raise ValueError('ROLE_INVALID_TRIANGLE')
        p=tuple(_finite_point(vertex_at(i),3) for i in vertex_ids)
        normal=_triangle_normal(*p)
        role=role_at(fi)
        anchor=role_anchor(role,family,interior_sign,adaptive)
        report['scanned_triangles']+=1
        if normal is None:
            report['geometry_degenerate']+=1
            if len(report['issue_samples'])<16:
                report['issue_samples'].append((int(fi),'DEGENERATE_GEOMETRY'))
        if anchor is None:
            report['unanchored_triangles']+=1
        else:
            report['anchored_triangles']+=1
            if normal is not None and sum(n*t for n,t in zip(normal,anchor[0])) <= anchor[1]:
                report['role_conflicts']+=1
                if len(report['issue_samples'])<16:
                    report['issue_samples'].append((int(fi),'ROLE_NORMAL_CONFLICT'))
        uv_state=_uv_sign(uvs)
        if uv_state is None:
            report['uv_missing']+=1
        elif uv_state==0:
            report['uv_degenerate']+=1
            if len(report['issue_samples'])<16:
                report['issue_samples'].append((int(fi),'UV_DEGENERATE'))
        else:
            report['uv_positive' if uv_state>0 else 'uv_negative']+=1
            prior=face_signs.get(fi)
            if prior is None:
                face_signs[fi]=uv_state
            elif prior != uv_state and prior != 0:
                report['uv_mixed_faces']+=1
                face_signs[fi]=0
                if len(report['issue_samples'])<16:
                    report['issue_samples'].append((int(fi),'UV_FOLDED_FACE'))
    issues = (report['geometry_degenerate']+report['uv_degenerate']+
              report['role_conflicts']+report['uv_mixed_faces'])
    report['status'] = ('EMPTY_MESH' if triangle_count == 0 else 'ISSUES' if issues else 'PARTIAL_SAMPLE' if mode=='FAST'
                        else 'PARTIAL_ROLES' if report['unanchored_triangles']
                        else 'ROLE_ANCHORS_CHECKED')
    report['complete_geometry_scan'] = mode=='FULL' and report['scanned_triangles']==triangle_count
    report['complete_role_coverage'] = (triangle_count > 0 and report['complete_geometry_scan']
                                        and not report['unanchored_triangles'])
    report['uv_mirror_is_error'] = False
    return report

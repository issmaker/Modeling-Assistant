"""Blender-free mirror-aware local validity tests for a swept architectural profile.

A point on the authored rail is immutable.  At every generated station the
profile is translated along a side frame; the *complete* signed X section,
not just the maximum positive offset, must remain a valid lateral cross-section.
These tests reject folded XY strips before the Blender Mesh is committed.

This validator is deliberately not a Boolean/3D Trim solver.  Complex joints
whose geometry cannot be proven valid must be rejected, not silently replaced
with a different Sharp mode or a corrupted Result.
"""
from math import hypot, isfinite


def _xy(p):
    return float(p[0]), float(p[1])


def _dot(a, b):
    return a[0]*b[0] + a[1]*b[1]


def _cross(a, b):
    return a[0]*b[1] - a[1]*b[0]


def _sub(a, b):
    return a[0]-b[0], a[1]-b[1]


def _section_point(p, frame, scale, x):
    return p[0]+frame[0]*scale*x, p[1]+frame[1]*scale*x


def audit_sweep_strips(stations, profile_x, *, side_sign, mirror_side=False,
                       mirror_x=False, mirror_z=False, reverse_path=False,
                       tolerance=1.0e-8, max_records=16):
    """Test profile strip Jacobian, tangent reach and segment degeneracy in XY.

    `stations`: [(position xyz, side frame xyz, scale, distance), ...].
    `profile_x`: signed *transformed* section X values (post flip/rotation).
    Accepts adjacent equal X (vertical faces), including closed profiles.

    Reflections are explicitly reported, never applied twice to the already
    transformed frames/section.  The physical strip orientation depends on the
    sign of side_sign and signed section X intervals; reversal is tracked.
    """
    if side_sign not in (-1, 1, -1.0, 1.0):
        raise ValueError('MIRROR_FRAME_INVALID_SIDE')
    if not isfinite(tolerance) or tolerance <= 0:
        raise ValueError('MIRROR_FRAME_INVALID_TOLERANCE')
    xs = [float(x) for x in profile_x]
    if len(xs) < 2 or any(not isfinite(x) for x in xs):
        raise ValueError('MIRROR_PROFILE_OFFSETS_INVALID')
    if len(stations) < 2:
        raise ValueError('MIRROR_SWEEP_STATIONS_INVALID')
    report = dict(intervals=0, longitudinal_rails=0, lateral_triangles=0,
                  minimum_forward_m=None, minimum_triangle_ratio=None,
                  signed_x_min=min(xs), signed_x_max=max(xs),
                  effective_mirror_parity=(-1 if bool(mirror_side) != bool(mirror_x) else 1),
                  full_orientation_parity=(-1 if sum(map(bool,(mirror_side,mirror_x,mirror_z,reverse_path)))%2 else 1),
                  mirror_side=bool(mirror_side), mirror_x=bool(mirror_x),
                  mirror_z=bool(mirror_z), reverse_path=bool(reverse_path),
                  issue_count=0, issues=[])
    for i, (a, b) in enumerate(zip(stations, stations[1:])):
        p0 = _xy(a[0]); p1 = _xy(b[0]); n0 = _xy(a[1]); n1 = _xy(b[1])
        s0 = float(a[2]); s1 = float(b[2]); base = _sub(p1,p0)
        length = hypot(*base)
        values = (*p0,*p1,*n0,*n1,s0,s1)
        if not all(map(isfinite,values)) or length <= tolerance or s0 <= 0 or s1 <= 0:
            raise ValueError(f'MIRROR_SWEEP_DEGENERATE_STATION interval={i}')
        tangent = (base[0]/length,base[1]/length)
        report['intervals']+=1
        rails = [_section_point(p0,n0,s0,x) for x in xs]
        rails_next = [_section_point(p1,n1,s1,x) for x in xs]
        for j, (v0,v1) in enumerate(zip(rails,rails_next)):
            forward=_dot(_sub(v1,v0),tangent)
            report['longitudinal_rails']+=1
            curr=report['minimum_forward_m']
            report['minimum_forward_m'] = forward if curr is None else min(curr,forward)
            if forward <= tolerance:
                report['issue_count']+=1
                if len(report['issues'])<max_records:
                    report['issues'].append({'code':'RAIL_REVERSED_OR_COLLAPSED',
                        'interval':i,'profile_index':j,'signed_x':xs[j],
                        'forward_m':round(forward,9),'center_span_m':round(length,9)})
        # Detect a bow-tie/fold in profile bands which have XY width. All
        # triangles must preserve the expected 2D orientation for this
        # particular signed section interval, regardless of left/right mirror.
        for j, (xa,xb) in enumerate(zip(xs,xs[1:])):
            dx = xb-xa
            if abs(dx) <= tolerance:
                continue  # vertical profile face: no XY width, not a fold
            a0,a1=rails[j],rails[j+1]
            b0,b1=rails_next[j],rails_next[j+1]
            triangle0=_cross(_sub(b0,a0),_sub(b1,a0))
            triangle1=_cross(_sub(b1,a0),_sub(a1,a0))
            # 180-degree reflection must invert the projected Jacobian sign.
            expected_sign=float(side_sign)*(1 if dx>0 else -1)
            scale_area=max(length*abs(dx),tolerance*tolerance)
            for tri_index,triangle in enumerate((triangle0,triangle1)):
                ratio=triangle*expected_sign/scale_area
                report['lateral_triangles']+=1
                curr=report['minimum_triangle_ratio']
                report['minimum_triangle_ratio']=ratio if curr is None else min(curr,ratio)
                if ratio <= tolerance:
                    report['issue_count']+=1
                    if len(report['issues'])<max_records:
                        report['issues'].append({'code':'STRIP_FOLDED_OR_COLLAPSED',
                            'interval':i,'profile_band':j,'triangle':tri_index,
                            'signed_x_pair':[round(xa,8),round(xb,8)],
                            'jacobian_ratio':round(ratio,9)})
    report['status']='OK' if not report['issue_count'] else 'UNSAFE'
    return report


def require_valid_sweep(report, *, sample_limit=3):
    """Fail closed. Called before Mesh publication and expensive UV meshing."""
    if report['status']!='OK':
        raise ValueError('MIRROR_CORNER_UNSAFE: ' + str(report['issues'][:sample_limit]))
    return report

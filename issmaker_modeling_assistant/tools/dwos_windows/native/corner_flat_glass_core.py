"""DWOS RC18: rigid-branch, planar postless CORNER glazing.

Keep each two-station facade branch in its own physical plane and obtain
both layer miter tips from the original SVD junction station.  No host
boundary/authorial contour points are moved. The ON variant with a physical
central post requires separate frame glazing-stop geometry and is deliberately
not routed through this mapper.
"""
from __future__ import annotations
from math import isfinite, sqrt
try:
    from .adaptive_opening_core import map_svd_local
except ImportError:
    from adaptive_opening_core import map_svd_local


def postless_corner_point(topology, branch, s, v, depth, *, tolerance=1e-7):
    if topology.get('classification') != 'CORNER':
        raise ValueError('Rigid branch glazing applies only to CORNER')
    stations = topology.get('stations') or ()
    joints = topology.get('corner_junctions') or ()
    if len(stations) < 3 or len(joints) != 1 or branch not in (0, 1):
        raise ValueError('Planar glass requires exactly two straight CORNER branches')
    ji=int(joints[0].get('station_index',-1))
    normals=topology.get('segment_exterior_normals') or ()
    if not (0 < ji < len(stations)-1 and len(normals)==len(stations)-1):
        raise ValueError('Incomplete CORNER junction / station correspondence')
    # Actual host edges can contain arbitrary collinear intermediate vertices.
    # Keep those stations without blending physically different facade planes.
    ref=normals[0 if branch==0 else -1]
    ref_len=sqrt(sum(float(x)*float(x) for x in ref))
    if ref_len<1.e-10:raise ValueError('Invalid facade normal')
    for normal in normals[:ji] if branch==0 else normals[ji:]:
        nn=sqrt(sum(float(x)*float(x) for x in normal))
        if nn<1.e-10 or sum(float(a)*float(b) for a,b in zip(ref,normal)) / (ref_len*nn) < .999998:
            raise ValueError('Corner branch not physically straight; cannot guarantee planar glass')
    half = float(topology['width'])*.5
    js = float(joints[0]['s'])-half
    if branch == 0 and float(s)>js+tolerance or branch==1 and float(s)<js-tolerance:
        raise ValueError('Glass station crosses another CORNER branch')
    if abs(float(s)-js)<=tolerance:
        # True miter point is the intersection of both translated facade lines.
        return tuple(float(x) for x in map_svd_local(topology,js,v,depth))
    ref_s = float(stations[0 if branch==0 else -1]['s'])-half
    origin = map_svd_local(topology,s,v,0.0)
    face0 = map_svd_local(topology,ref_s,0.0,0.0)
    face1 = map_svd_local(topology,ref_s,0.0,1.0)
    depth_axis = tuple(float(b)-float(a) for a,b in zip(face0,face1))
    p = tuple(float(a)+float(depth)*float(n) for a,n in zip(origin,depth_axis))
    if not all(isfinite(q) for q in p):
        raise ValueError('Nonfinite CORNER glazing coordinate')
    return p

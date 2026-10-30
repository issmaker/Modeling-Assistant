"""Final shell-consistent outward normal pass for WALL and CORNICE.

Only polygon loop order is repaired. Mesh coordinates, authored Path, material
assignment and UV values attached to loops are not intentionally transformed.
"""
from __future__ import annotations

import bmesh
import bpy
from math import sqrt
from ..core.semantic_geometry import FACE_ROLE_CODES, face_role_codes
from ..core.nearest_segments import NearestSegments


def _signed_volume(faces):
    # Oriented polyhedral volume: sum of tetrahedra against an arbitrary origin.
    # Fan triangulation is algebraically valid for planar concave faces as well.
    volume = 0.0
    # Translate to a local origin; world-space coordinates in CAD projects
    # may be kilometres from (0,0,0), which amplifies cancellation.
    origin = faces[0].verts[0].co.copy()
    for face in faces:
        vs = list(face.verts)
        for i in range(1, len(vs) - 1):
            a, b, c = vs[0].co - origin, vs[i].co - origin, vs[i + 1].co - origin
            volume += a.dot(b.cross(c)) / 6.0
    return volume


def _left_segments(points, tolerance=1.e-10):
    result=[]
    for a,b in zip(points, points[1:]):
        dx=float(b.x-a.x);dy=float(b.y-a.y);length2=dx*dx+dy*dy
        if length2 > tolerance:
            inv=length2**-.5
            result.append((float(a.x),float(a.y),dx,dy,length2,-dy*inv,dx*inv))
    return result


def _role_outside_vote(face, role, segments, inset_side, create_inner, outer_normal_side, path_points, segment_index=None):
    normal=face.normal
    if role==FACE_ROLE_CODES['TOP_CAP']:
        return float(normal.z)
    if role==FACE_ROLE_CODES['BOTTOM_CAP']:
        return -float(normal.z)
    if role==FACE_ROLE_CODES['END_CAP'] and len(path_points)>1:
        c=face.calc_center_median()
        start=path_points[0];end=path_points[-1]
        start_distance=(c.x-start.x)**2+(c.y-start.y)**2
        end_distance=(c.x-end.x)**2+(c.y-end.y)**2
        dx=float(path_points[1].x-start.x) if start_distance<end_distance else float(end.x-path_points[-2].x)
        dy=float(path_points[1].y-start.y) if start_distance<end_distance else float(end.y-path_points[-2].y)
        length=sqrt(dx*dx+dy*dy)
        if length<1.e-10:return 0.0
        factor=-1.0 if start_distance<end_distance else 1.0
        return factor*(normal.x*dx+normal.y*dy)/length
    if role not in {FACE_ROLE_CODES['WALL_OUTER'], FACE_ROLE_CODES['WALL_INNER']} or not segments:
        return 0.0
    c=face.calc_center_median()
    # For standalone diagnostics this fallback remains valid; production
    # creates one index for the complete WALL pass, not one per face.
    if segment_index is None:
        segment_index = NearestSegments(segments)
    nearest = segment_index.nearest_index(float(c.x), float(c.y))
    if nearest is None:
        return 0.0
    best = segments[nearest][5:7]
    if create_inner:
        side=inset_side if role==FACE_ROLE_CODES['WALL_INNER'] else ('RIGHT' if inset_side=='LEFT' else 'LEFT')
    else:
        side=outer_normal_side or ('RIGHT' if inset_side=='LEFT' else 'LEFT')
    sign=1.0 if side=='LEFT' else -1.0
    return sign*(float(normal.x)*best[0]+float(normal.y)*best[1])


def orient_connected_shells(mesh, *, kind, path_points=(), inset_side='LEFT',
                            create_inner=True, outer_normal_side=None):
    """Winding-connectivity pass, then whole-component outside orientation.

    No local independent face flips after this pass: those create contradictory
    winding on shared edges. A non-orientable/non-manifold mesh fails closed.
    """
    if kind not in {'WALL','CORNICE'}:
        raise ValueError('Unknown normal integrity geometry kind')
    roles=face_role_codes(mesh)
    bm=bmesh.new()
    report={'kind':kind,'components':0,'closed':0,'open':0,
            'parity_flips':0,'outward_flips':0,'checked_faces':0,'unresolved':0}
    try:
        bm.from_mesh(mesh)
        bm.faces.ensure_lookup_table();bm.faces.index_update()
        faces=list(bm.faces)
        if not faces:return report
        for edge in bm.edges:
            if len(edge.link_faces)>2:
                raise ValueError('NORMAL_INTEGRITY: non-manifold edge with >2 faces')
        parity={};components=[]
        for root in faces:
            if root in parity:continue
            parity[root]=0; queue=[root];part=[]
            for face in queue:
                part.append(face)
                for loop in face.loops:
                    edge=loop.edge
                    if len(edge.link_loops)!=2:continue
                    other=next(item for item in edge.link_loops if item.face != face)
                    required=parity[face] ^ int(loop.vert == other.vert)
                    if other.face in parity:
                        if parity[other.face]!=required:
                            raise ValueError('NORMAL_INTEGRITY: contradictory winding cycle')
                    else:
                        parity[other.face]=required;queue.append(other.face)
            components.append(part)
        for face,flag in parity.items():
            if flag:
                face.normal_flip();report['parity_flips']+=1
        bm.normal_update()
        segments=_left_segments(path_points) if kind=='WALL' else []
        segment_index=NearestSegments(segments) if segments else None
        if segment_index is not None and bool(bpy.context.scene.get('uvwall_geometry_truth_full', False)):
            segment_index.set_truth_mode('FULL')
        for part in components:
            report['components']+=1;report['checked_faces']+=len(part)
            links={edge for face in part for edge in face.edges}
            closed=all(len(edge.link_faces)==2 for edge in links)
            if closed:
                report['closed']+=1
                signed_volume=_signed_volume(part)
                if abs(signed_volume) < 1.e-12:
                    raise ValueError('NORMAL_INTEGRITY: zero-volume closed shell; cannot identify exterior')
                if signed_volume < 0:
                    for face in part: face.normal_flip();report['outward_flips']+=1
            else:
                report['open']+=1
                if kind=='WALL':
                    positive=negative=0.0
                    for face in part:
                        role=roles[face.index] if face.index<len(roles) else 0
                        vote=_role_outside_vote(face,role,segments,inset_side,create_inner,outer_normal_side,path_points,segment_index)
                        weight=float(face.calc_area())*abs(vote)
                        if vote>0.12:positive+=weight
                        elif vote<-.12:negative+=weight
                    if max(positive,negative)<1.e-10 or abs(positive-negative)<(positive+negative)*.02:
                        raise ValueError('NORMAL_INTEGRITY: ambiguous open wall shell exterior')
                    if negative>positive:
                        for face in part:face.normal_flip();report['outward_flips']+=1
                    # Mixed high-confidence votes reveal folded or intersecting geometry.
                    conflict=min(positive,negative)
                    if conflict > (positive+negative)*.10:
                        raise ValueError('NORMAL_INTEGRITY: conflicting wall normal directions within one shell')
                # CORNICE: for an open shell, globally preserve the exterior
                # direction seeded from its authored cross-section quads.
                # An open section itself has no mathematically unique inside.
        bm.normal_update()
        report['geometry_truth'] = (segment_index.truth_report() if segment_index is not None
                                    else {'mode':'SAMPLED','queries':0,'reference_checks':0,'mismatches':0})
        # Explicit parity re-audit after all shell flips.
        for edge in bm.edges:
            loops=list(edge.link_loops)
            if len(loops)==2 and loops[0].vert == loops[1].vert:
                raise ValueError('NORMAL_INTEGRITY: shared edge traversal not opposite')
        bm.to_mesh(mesh)
    finally:
        bm.free()
    mesh.update()
    return report

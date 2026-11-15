"""Construct a real closed frame shell from a certified S/V face ribbon.

Pure Python data structure transform: no Blender, no Mesh publication.
The outer sides are *intentional wall-contact surfaces*, and the inner sides
are glazing-facing reveals. Assembly clearances require separate validation.
"""
from __future__ import annotations
from collections import Counter
from math import isfinite

SCHEMA='ISSMAKER_SVD_VOLUME_SHELL_V1'


def stage(front_vertices,back_vertices,front_faces,front_roles,source_vertex_count):
    """Duplicate ribbon to front/back and stitch both existing rim cycles.

    Source contact vertices preserve front IDs [0, source_vertex_count).
    Each front boundary edge is used exactly once by the appropriate sidewall.
    A valid annular ribbon becomes a closed genus-1 frame envelope.
    """
    front=[tuple(float(x) for x in pt) for pt in front_vertices]
    back=[tuple(float(x) for x in pt) for pt in back_vertices]
    n=len(front)
    if n<6 or n>450 or n!=len(back) or len(front_faces)>600:
        raise ValueError('SVD_VOLUME_VERTEX_OR_FACE_BUDGET')
    if not 3<=int(source_vertex_count)<n:
        raise ValueError('SVD_VOLUME_INVALID_CONTACT_COUNT')
    if any(len(pt)!=3 or not all(isfinite(c) for c in pt) for pt in front+back):
        raise ValueError('SVD_VOLUME_NONFINITE_VERTEX')
    if len(front_roles)!=len(front_faces) or not front_faces:
        raise ValueError('SVD_VOLUME_ROLE_OR_FACE_COUNT')
    from collections import defaultdict
    incidence=defaultdict(list)
    faces=[];roles=[];seen_faces=set()
    for k,ring in enumerate(front_faces):
        face=tuple(int(i) for i in ring)
        if len(face)<3 or len(set(face))!=len(face) or any(i<0 or i>=n for i in face):
            raise ValueError('SVD_VOLUME_INVALID_FRONT_FACE')
        canonical=tuple(sorted(face))
        if canonical in seen_faces:raise ValueError('SVD_VOLUME_DUPLICATE_FRONT_FACE')
        seen_faces.add(canonical)
        faces.append(face);roles.append('FRONT_'+str(front_roles[k]))
        for a,b in zip(face,face[1:]+face[:1]):
            incidence[tuple(sorted((a,b)))].append((a,b))
    if any(len(rows)>2 for rows in incidence.values()):
        raise ValueError('SVD_VOLUME_NONMANIFOLD_RIBBON_EDGE')
    for edge,rows in incidence.items():
        if len(rows)==2 and rows[0]==rows[1]:
            raise ValueError('SVD_VOLUME_INCONSISTENT_FRONT_WINDING')
    sides=[]
    count_outer=count_inner=0
    for edge,rows in sorted(incidence.items()):
        if len(rows)!=1:continue
        a,b=rows[0]
        if a<int(source_vertex_count) and b<int(source_vertex_count):
            role='OUTER_WALL_CONTACT';count_outer+=1
        elif a>=int(source_vertex_count) and b>=int(source_vertex_count):
            role='INNER_GLAZING_REVEAL';count_inner+=1
        else:raise ValueError('SVD_VOLUME_MIXED_BOUNDARY_CONTACT')
        sides.append(((b,a,a+n,b+n),role))
    if count_outer!=int(source_vertex_count) or count_inner<3:
        raise ValueError('SVD_VOLUME_INVALID_RIM_CYCLES')
    for face,role in sides:
        faces.append(face);roles.append(role)
    for k,ring in enumerate(front_faces):
        faces.append(tuple(n+int(i) for i in reversed(ring)))
        roles.append('BACK_'+str(front_roles[k]))
    if len(faces)!=2*len(front_faces)+count_outer+count_inner:
        raise ValueError('SVD_VOLUME_INVALID_SHELL_COUNTS')
    # A physical bend can make a sidewall quad non-planar when depth vectors
    # differ at its endpoints. Keep the exact same vertex positions and split
    # ONLY the offending quads into two oriented triangles, before UV staging.
    # The physical shape is explicitly represented by its two planar facets.
    from math import sqrt
    new_faces=[];new_roles=[];triangulated=0
    coordinates=front+back
    for face,role in zip(faces,roles):
        if len(face)==4:
            a,b,c,d=(coordinates[i] for i in face)
            ab=tuple(b[k]-a[k] for k in range(3))
            ac=tuple(c[k]-a[k] for k in range(3))
            ad=tuple(d[k]-a[k] for k in range(3))
            normal=(ab[1]*ac[2]-ab[2]*ac[1],
                    ab[2]*ac[0]-ab[0]*ac[2],
                    ab[0]*ac[1]-ab[1]*ac[0])
            length=sqrt(sum(x*x for x in normal))
            # Degenerate faces are intentionally NOT repaired here; geometry
            # validators must reject them rather than hide a topology error.
            if length>1e-12:
                departure=abs(sum(ad[k]*normal[k] for k in range(3)))/length
                if departure>1e-6:
                    new_faces.extend(((face[0],face[1],face[2]),
                                      (face[0],face[2],face[3])))
                    new_roles.extend((role,role))
                    triangulated+=1
                    continue
        new_faces.append(face);new_roles.append(role)
    faces=new_faces;roles=new_roles
    edge_counts=Counter();directed=Counter()
    for face in faces:
        for a,b in zip(face,face[1:]+face[:1]):
            edge_counts[tuple(sorted((a,b)))]+=1
            directed[(a,b)]+=1
    if any(count!=2 for count in edge_counts.values()):
        raise ValueError('SVD_VOLUME_OPEN_OR_NONMANIFOLD_SHELL')
    if any(directed[(a,b)]!=1 or directed[(b,a)]!=1 for a,b in edge_counts):
        raise ValueError('SVD_VOLUME_SHELL_ORIENTATION_FAILURE')
    return {'schema':SCHEMA,'vertices':front+back,'faces':faces,'roles':roles,
            'source_contact_vertex_count':int(source_vertex_count),
            'front_vertex_count':n,'front_face_count':len(front_faces),
            'back_face_count':len(front_faces),
            'outer_wall_contact_face_count':count_outer,
            'inner_glazing_reveal_face_count':count_inner,
            'edge_count':len(edge_counts),'open_boundary_edges':0,
            'triangulated_nonplanar_quads':triangulated,
            'topology_status':'CLOSED_ORIENTED_SHELL',
            'source_geometry_modified':False,
            'production_geometry_replaced':False}

"""Read-only proximity audit for possible unstitched geometric boundaries.

Never weld a 2 mm gap by proximity alone: an authored corner, cap grid cut,
and UV seam are distinct semantics. Run only on explicit FORENSIC diagnostics.
"""
from math import sqrt
from .anchor_neighbors import AnchorNeighbors


def boundary_gap_report(vertices, polygons, *, tolerance=0.002,
                        micro_tolerance=0.000020, limit=12,
                        max_boundary_vertices=150000):
    # All positions are 3D coordinates; caller supplies polygons as indices.
    if not 0 < micro_tolerance < tolerance <= 0.002:
        raise ValueError('Invalid conservative gap-audit tolerance')
    edge_faces = {}
    vertex_faces = {}
    for fid, face in enumerate(polygons):
        for vertex in face:
            vertex_faces.setdefault(int(vertex),set()).add(fid)
        for a,b in zip(face,face[1:]+face[:1]):
            key=(min(int(a),int(b)),max(int(a),int(b)))
            edge_faces[key]=edge_faces.get(key,0)+1
    boundary_edges=[e for e,count in edge_faces.items() if count==1]
    boundary_vertices=sorted({v for e in boundary_edges for v in e})
    if len(boundary_vertices)>max_boundary_vertices:
        return {'status':'SKIPPED_BUDGET','boundary_vertices':len(boundary_vertices),
                'suspects':0,'examples':[]}
    index=AnchorNeighbors([vertices[i] for i in boundary_vertices],tolerance)
    count=0;minimum=None;maximum=0.; examples=[]
    for local_i,a in enumerate(boundary_vertices):
        for local_j in index.matches(vertices[a]):
            if local_j<=local_i:continue
            b=boundary_vertices[local_j]
            if vertex_faces.get(a,set()) & vertex_faces.get(b,set()):continue
            if (a,b) in edge_faces:continue
            pa,pb=vertices[a],vertices[b]
            distance=sqrt(sum((float(x)-float(y))**2 for x,y in zip(pa,pb)))
            if distance<=micro_tolerance or distance>tolerance:continue
            count+=1
            minimum=distance if minimum is None else min(minimum,distance)
            maximum=max(maximum,distance)
            if len(examples)<limit:
                examples.append({'vertex_a':a,'vertex_b':b,
                                 'gap_m':round(distance,9)})
    return {'status':'DIAGNOSTIC_ONLY','boundary_vertices':len(boundary_vertices),
            'suspects':count,'min_gap_m':minimum,'max_gap_m':maximum,
            'examples':examples}

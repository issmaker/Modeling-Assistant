"""BH0476 read-only native BMesh regression for FILLED self-intersections."""
import bmesh
from .filled_intersection_guard import validate_filled_result_intersections


def run_native_filled_intersection_cases():
    rows=[]
    for label,expected in (
        ('REAL_INTERIOR_CROSSING','COLLISION'),
        ('WELDED_SHARED_SEAM','VALID'),
        ('COPLANAR_AREA_OVERLAP','COLLISION'),
        ('DISJOINT','VALID'),
        ('BUDGET_FAIL_CLOSED','RESOURCE_LIMIT'),
    ):
        bm=bmesh.new()
        try:
            tag=bm.faces.layers.int.new('brick_hole_result')
            def quad(points):
                vs=[bm.verts.new(pt) for pt in points]
                return bm.faces.new(vs)
            if label=='REAL_INTERIOR_CROSSING':
                face1=quad(((-1.,0.,0.),(1.,0.,0.),(1.,0.,3.),(-1.,0.,3.)))
                face2=quad(((0.,-1.,0.),(0.,1.,0.),(0.,1.,3.),(0.,-1.,3.)))
            elif label in ('WELDED_SHARED_SEAM','BUDGET_FAIL_CLOSED'):
                p=[bm.verts.new(v) for v in ((0,0,0),(1,0,0),(1,0,2),
                     (0,0,2),(1,1,0),(1,1,2))]
                face1=bm.faces.new((p[0],p[1],p[2],p[3]))
                face2=bm.faces.new((p[1],p[4],p[5],p[2]))
            elif label=='COPLANAR_AREA_OVERLAP':
                face1=quad(((0.,0.,0.),(2.,0.,0.),(2.,2.,0.),(0.,2.,0.)))
                face2=quad(((1.,1.,0.),(3.,1.,0.),(3.,3.,0.),(1.,3.,0.)))
            else:
                face1=quad(((0.,0.,0.),(1.,0.,0.),(1.,1.,0.),(0.,1.,0.)))
                face2=quad(((2.,0.,0.),(3.,0.,0.),(3.,1.,0.),(2.,1.,0.)))
            face1[tag]=1
            bm.faces.index_update()
            before=(len(bm.verts),len(bm.edges),len(bm.faces))
            status='VALID';stats={}
            try:
                validate_filled_result_intersections(
                    bm,tag,1,statistics=stats,
                    max_candidate_pairs=1 if label=='BUDGET_FAIL_CLOSED' else 50000)
            except ValueError as exc:
                if str(exc).startswith('RESOURCE_LIMIT:'):
                    status='RESOURCE_LIMIT'
                elif str(exc).startswith('COLLISION:'):
                    status='COLLISION'
                else:
                    status='ERROR: '+str(exc)
            same=before==(len(bm.verts),len(bm.edges),len(bm.faces))
            rows.append({'case':'BH0476_'+label,
                         'status':'PASS' if status==expected and same else 'FAIL',
                         'evidence':{'expected':expected,'actual':status,
                                     'read_only':same,'stats':stats}})
        finally:
            bm.free()
    return rows

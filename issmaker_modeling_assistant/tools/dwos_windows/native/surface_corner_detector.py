"""W55: explainable geometric corner detection from authored wall path.

Never infer angle from an arbitrary window name or a screen projection.
Each window is projected to a UNIQUE path segment, then consecutive segments
are classified by signed XY turn and normal orientation. Designed for 45/90
and unusual turn angles, strictly read-only before optional operator tagging.
"""
from __future__ import annotations
import bpy,json,math
from mathutils import Vector
from .surface_steps import SESSION_KEY

def classify(hosts,path):
    if not path or path.type!='MESH' or path.data.polygons or not hosts:
        raise ValueError('W55_VALID_EDGE_PATH_AND_WINDOWS_REQUIRED')
    # W50 path remains an ordered open polyline with edge index k -> (k,k+1).
    verts=[path.matrix_world@v.co for v in path.data.vertices]
    edges=[tuple(e.vertices) for e in path.data.edges]
    if len(edges)!=len(verts)-1 or any(set(edge)!={i,i+1} for i,edge in enumerate(edges)):
        raise ValueError('W55_PATH_IS_NOT_ORDERED_OPEN_POLYLINE')
    assigned={}
    for host in hosts:
        if SESSION_KEY not in host:raise ValueError('W55_HOST_MISSING_DWOS_SESSION:'+host.name)
        data=json.loads(host[SESSION_KEY])
        src=data['source'];o=Vector(src['origin'])
        x=Vector(src['x_axis']);y=Vector(src['y_axis']);n=Vector(src['normal'])
        px=sum(p[0] for p in src['points'])/len(src['points'])
        py=sum(p[1] for p in src['points'])/len(src['points'])
        center=o+x*px+y*py
        scored=[]
        for i,(a,b) in enumerate(edges):
            aa=verts[a];bb=verts[b]
            v=bb-aa;dx=Vector((v.x,v.y));length=dx.length
            if length<1e-8:raise ValueError('W55_DEGENERATE_PATH_EDGE')
            relative=Vector((center.x-aa.x,center.y-aa.y))
            t=relative.dot(dx)/(length*length)
            distance=abs(dx.x*relative.y-dx.y*relative.x)/length
            if -.001<=t<=1.001 and distance<1.75:
                candidate_normal=Vector((dx.y,-dx.x,0)).normalized()
                alignment=n.dot(candidate_normal)
                # The Brick Hole caps sit .5m behind the authored facade.
                # Match by orientation too, or an adjacent right-angle wall
                # could win purely by XY distance.
                if abs(alignment)>.9:scored.append((distance,i,t,alignment))
        scored.sort()
        if not scored or (len(scored)>1 and abs(scored[0][0]-scored[1][0])<1e-5):
            raise ValueError('W55_AMBIGUOUS_OR_MISSING_WINDOW_PATH_SEGMENT:'+host.name)
        distance,idx,t,alignment=scored[0]
        if abs(alignment)<.9:raise ValueError('W55_WINDOW_NORMAL_NOT_ALIGNED_TO_FACADE:'+host.name)
        if idx in assigned:raise ValueError('W55_MULTIPLE_WINDOWS_IN_ONE_PATH_SEGMENT_UNSUPPORTED')
        assigned[idx]={'host':host.name,'segment':idx,'parameter_t':t,'offset_m':distance,'alignment':alignment,
                       'normal':tuple(n)}
    pairrows=[]
    for i in range(len(edges)-1):
        if i not in assigned or i+1 not in assigned:continue
        va=verts[i+1]-verts[i];vb=verts[i+2]-verts[i+1]
        a=Vector((va.x,va.y)).normalized();b=Vector((vb.x,vb.y)).normalized()
        signed=math.degrees(math.atan2(a.x*b.y-a.y*b.x,a.dot(b)))
        if abs(signed)<1.:cls='STRAIGHT'
        else:
            side=assigned[i]['alignment']
            interior_angle=signed if side>0 else -signed
            kind='INTERIOR_CORNER' if interior_angle>0 else 'EXTERIOR_CORNER'
            cls=kind+'_'+str(int(round(abs(signed))))+'DEG'
        pairrows.append({'a':assigned[i]['host'],'b':assigned[i+1]['host'],
            'segments':[i,i+1],'signed_turn_deg':round(signed,6),'class':cls,
            'angle_deg':round(abs(signed),6),
            'merge_candidate':abs(signed)>=20. and abs(signed)<160.,
            'source':'AUTHORED_PATH_TANGENTS_AND_WINDOW_NORMALS'})
    return {'path':path.name,'windows':[assigned[k] for k in sorted(assigned)],
            'adjacent_pairs':pairrows,
            'corner_pairs':[p for p in pairrows if p['merge_candidate']],
            'errors':[]}

class DWOS_OT_surface_detect_corner_neighbors(bpy.types.Operator):
    bl_idname='dwos.surface_detect_corner_neighbors'
    bl_label='DWOS W55 · Detect Architectural Corner Windows'
    bl_options={'REGISTER','UNDO'}
    path_name:bpy.props.StringProperty(default="ISSMAKER_W50_AUTHORING_PATH_4M")
    collection_name:bpy.props.StringProperty(default="ISSMAKER | W53 NINE LP NO MULLIONS")
    def execute(self,context):
        try:
            path=bpy.data.objects.get(self.path_name)
            coll=bpy.data.collections.get(self.collection_name)
            if coll is None:raise ValueError('W55_WINDOW_COLLECTION_NOT_FOUND')
            hosts=sorted([o for o in coll.objects if o.type=='MESH' and SESSION_KEY in o],key=lambda o:o.name)
            result=classify(hosts,path)
            # Classification is metadata only. Never trigger unsafe interior merge.
            context.scene['DWOS_W55_corner_detection']=json.dumps(result,ensure_ascii=False)
            for row in result['windows']:
                host=bpy.data.objects[row['host']]
                neighbor=[x for x in result['adjacent_pairs'] if host.name in (x['a'],x['b'])]
                host['DWOS_W55_corner_neighbors']=json.dumps(neighbor,ensure_ascii=False)
                host['DWOS_W55_auto_corner_candidate']=any(x['merge_candidate'] for x in neighbor)
            self.report({'INFO'},f'DWOS: {len(result["windows"])} окон, {len(result["corner_pairs"])} угловых пар')
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'},str(exc)[:180]);return {'CANCELLED'}
CLASSES=(DWOS_OT_surface_detect_corner_neighbors,)

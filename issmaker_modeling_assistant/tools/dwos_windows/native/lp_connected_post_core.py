"""W58.23 LP-driven connected corner post, design research only.

The corner join starts at the LP V-junction. Its own depth axis is the
inward bisector of LP's two local tangent directions, not an HP/front normal.
The generated frame is a single shared-vertex mesh and no detached 4-quad
island is introduced. This first 2-branch solver is intentionally limited
to the validated 24-vertex/30-face DWOS branch-frame template.
"""
from math import acos, sin, sqrt, hypot, degrees, isfinite
from collections import Counter,defaultdict

def _distance(v,w):
    return sqrt(sum((a-b)*(a-b) for a,b in zip(v,w)))

def _dot(a,b):return sum(x*y for x,y in zip(a,b))
def _unit(a):
    n=sqrt(_dot(a,a))
    if n<1e-9:raise ValueError("LP_ZERO_LENGTH_BRANCH")
    return tuple(x/n for x in a)

def _sub(a,b):return tuple(x-y for x,y in zip(a,b))
def _add(a,b):return tuple(x+y for x,y in zip(a,b))
def _scale(a,t):return tuple(x*t for x in a)

def lp_axes(lp_points):
    """One LP V-junction defined by center and two points on its branches, XZ."""
    c,left,right=lp_points
    d_left=_unit(_sub(left,c));d_right=_unit(_sub(right,c))
    cosine=max(-1.,min(1.,_dot(d_left,d_right)))
    angle=degrees(acos(cosine))
    axis=_unit(_add(d_left,d_right))
    if not 0<angle<180:raise ValueError("LP_JUNCTION_DEGENERATE")
    return {"lp_apex":c,"left":d_left,"right":d_right,
            "own_depth_axis":axis,"interior_angle_deg":angle}

def lp_local_from_mesh(frame,lp):
    """Read the real LP mesh, expressed in the HP frame's local XZ plane."""
    matrix=frame.matrix_world.inverted() @ lp.matrix_world
    coords=[tuple((matrix@v.co)[i] for i in (0,2)) for v in lp.data.vertices]
    # All y-levels are represented; identify center at the two-branch apex.
    # Unique xz points have mirrored left and right positions.
    unique=[]
    for v in coords:
        if not any(_distance(v,w)<1e-5 for w in unique):unique.append(v)
    if len(unique)<3:raise ValueError("LP_NOT_TWO_BRANCHES")
    pivot=max(unique,key=lambda v:v[1])
    left=min((v for v in unique if v[0]<pivot[0]-1e-5),key=lambda v:v[0],default=None)
    right=max((v for v in unique if v[0]>pivot[0]+1e-5),key=lambda v:v[0],default=None)
    if left is None or right is None:raise ValueError("LP_MISSING_BOTH_SIDE_BRANCHES")
    return lp_axes((pivot,left,right))

def _plan(front_verts,lp_profile,threshold=65.,clearance=2e-5):
    if len(front_verts)<24:raise ValueError("HP_BASE_VERTS_UNSUPPORTED")
    angle=lp_profile["interior_angle_deg"]
    if angle<threshold-clearance:
        return {"status":"NO_POST_ALLOWED","angle":angle,
                "reason":"CENTRAL_CORNER_POST_MIN_INTERIOR_65_DEG"}
    if angle>175.:raise ValueError("LP_NEAR_FLAT_JUNCTION_NO_POST")
    verts=front_verts[:24]
    # Structural layout: 2 corner panes, front apex at 4/10,
    # inset apex at 16/22, adjacent front direction anchors at 3/5 and 9/11.
    center_b=(float(verts[4][0]),float(verts[4][2]))
    center_t=(float(verts[10][0]),float(verts[10][2]))
    inner_b=(float(verts[16][0]),float(verts[16][2]))
    inner_t=(float(verts[22][0]),float(verts[22][2]))
    for i,j in ((4,10),(16,22),(3,9),(5,11)):
        if _distance((verts[i][0],verts[i][2]),(verts[j][0],verts[j][2]))>1e-5:
            raise ValueError("HP_NOT_SHARED_TWO_BRANCH_CROSS_SECTION")
    p=lp_profile["lp_apex"]
    axis=lp_profile["own_depth_axis"]
    # The HP source front corner must coincide with the LP-authored pivot.
    if _distance(p,center_b)>3e-4:
        raise ValueError("HP_FRONT_CUSP_NOT_AT_LP")
    back_vector=_sub(inner_b,p)
    along=_dot(back_vector,axis)
    if along<.001 or abs(_dot(back_vector,(-axis[1],axis[0])))>.001:
        raise ValueError("HP_BACK_CUSP_OUTSIDE_LP_DEPTH_AXIS")
    n_left=lp_profile["left"];n_right=lp_profile["right"]
    feet=[_add(p,_scale(n_left,_dot(back_vector,n_left))),
          _add(p,_scale(n_right,_dot(back_vector,n_right)))]
    # The shoulder intersection is obtained by LP side orthogonal projection,
    # NOT by a fixed impost rectangle or a guessed centerline.
    foot_l,foot_r=feet
    for foot in feet:
        if not all(isfinite(x) for x in foot):raise ValueError("LP_FOOT_NONFINITE")
        if _distance(foot,p)<1e-5:raise ValueError("LP_FOOT_COLLAPSED")
    for id,side in ((3,foot_l),(5,foot_r),(9,foot_l),(11,foot_r)):
        base=(verts[id][0],verts[id][2])
        if _dot(_sub(side,p),_sub(base,p))<=0:raise ValueError("LP_FOOT_ON_WRONG_BRANCH")
        if _distance(side,p)>_distance(base,p):raise ValueError("LP_FOOT_OUTSIDE_WINDOW_FRAME")
    return {"status":"POST_OK","angle":angle,"own_depth_axis":axis,
            "lp_inset_m":along*sin(angle*3.141592653589793/360.),
            "post_depth_m":along,"feet":(foot_l,foot_r)}

# The stable two-branch DWOS frame template is validated before modification.
_TEMPLATES=[
 (0,4,3), (0,1,4), (1,2,4), (2,5,4), (9,10,6),(10,7,6),
 (10,8,7),(10,11,8), (3,6,0),(3,9,6),(2,8,5),(8,11,5),
 (15,16,12),(16,13,12),(16,14,13),(16,17,14),
 (18,22,21),(18,19,22),(19,20,22),(20,23,22),
 (12,18,15),(18,21,15),(17,20,14),(17,23,20),
 (15,21,9,3),(3,4,16,15),(22,10,9,21),(4,5,17,16),
 (11,10,22,23),(11,23,17,5)
]

def connected_topology(hp_verts,hp_faces,lp_profile,threshold=65.):
    """Return a native connected HP mesh with LP-authored junction basis."""
    if len(hp_verts)!=24 or len(hp_faces)!=30:
        raise ValueError("LP_JOIN_TEMPLATE_REQUIRES_BASE_24V_30F")
    # The native core may triangulate the outer belt in either direction.
    # Both are equivalent, but the re-patch must normalize them to the
    # semantically same connected topology to prevent diagonal T-junctions.
    for start, required in ((0,{0,1,3,4}),(4,{6,7,9,10})):
        actual=set(int(v) for face in hp_faces[start:start+2] for v in face)
        if actual!=required:
            raise ValueError(f"LP_JOIN_BELT_FACE_SET_MISMATCH_{start}: {actual}")
    # Native face winding/triangulation differs by corner angle. Those
    # interior frame faces remain untouched; the native stage has already
    # validated their connectivity and winding. Never require a chosen
    # arbitrary diagonal on portions we do not actually modify.
    plan=_plan(hp_verts,lp_profile,threshold)
    if plan["status"]=="NO_POST_ALLOWED":
        return {"verts":list(hp_verts),"faces":list(hp_faces),"post":False,"plan":plan}
    left,right=plan["feet"]
    lo=float(hp_verts[4][1]);hi=float(hp_verts[10][1])
    if not hi>lo:raise ValueError("HP_INNER_BELT_LEVEL_ORDER")
    v=list(hp_verts)+[(right[0],hi,right[1]),(left[0],hi,left[1]),
                      (right[0],lo,right[1]),(left[0],lo,left[1])]
    f=list(hp_faces)
    replacements={
      0:(0,4,27,3),1:(0,1,4),2:(1,2,4),3:(2,5,26,4),
      4:(9,25,10,6),5:(10,7,6),6:(10,8,7),7:(10,24,11,8),
      25:(10,25,27,4),26:(24,10,4,26),
      27:(22,24,26,16),28:(25,22,16,27)
    }
    for k,row in replacements.items():f[k]=row
    f.extend([(11,24,22,23),(21,22,25,9),(17,16,26,5),(3,27,16,15)])
    assert len(v)==28 and len(f)==34
    return {"verts":v,"faces":f,"post":True,"plan":plan}

def validate_connected(verts,faces,post):
    # Reject disjoint islands and nonmanifold T-junctions.
    parents=list(range(len(verts)))
    def root(x):
        while parents[x]!=x:
            parents[x]=parents[parents[x]];x=parents[x]
        return x
    for fi,face in enumerate(faces):
        if len(face)<3 or len(set(face))!=len(face):
            raise ValueError(f"POST_DEGENERATE_FACE_{fi}")
        a=face[0]
        for b in face[1:]:
            aa,bb=root(a),root(b)
            if aa!=bb:parents[bb]=aa
    components=len({root(i) for i in range(len(verts))})
    if components!=1:raise ValueError(f"POST_DISCONNECTED_{components}")
    edges=defaultdict(list)
    for fi,face in enumerate(faces):
        for a,b in zip(face,face[1:]+face[:1]):
            if a==b:raise ValueError("POST_ZERO_EDGE")
            edges[tuple(sorted((a,b)))].append(fi)
    over=[e for e,v in edges.items() if len(v)>2]
    if over:raise ValueError(f"POST_NONMANIFOLD_EDGE_{over[:3]}")
    return {"components":1,"verts":len(verts),"faces":len(faces),
            "open_edges":sum(len(x)==1 for x in edges.values()),
            "overconnected_edges":0,"post":post}

def _project_uv_metric(mesh):
    """Research-only UV atlas. Each HP face is metrically mapped in one tile.
    Geometry is authoritative; source wall and LP UV are never touched.
    Continuity of texture across adjacent face charts is NOT yet certified.
    """
    import math
    layer=mesh.uv_layers.active or mesh.uv_layers.new(name="UVMap")
    inv=1000./4096.
    for face in mesh.polygons:
        ids=list(face.vertices)
        pts=[mesh.vertices[i].co for i in ids]
        min_y=min(p.y for p in pts); max_y=max(p.y for p in pts)
        # In the 2D profile chart, x and z form the facade footprint.
        # Choose a true planar local coordinate along the longest footprint
        # vector. It is orthogonal to mesh local y.
        best=None;length=0.
        for i,a in enumerate(pts):
            for b in pts[i+1:]:
                delta=(b.x-a.x,b.z-a.z)
                d=math.hypot(*delta)
                if d>length:length=d;best=delta
        if length<1e-8:raise ValueError("LP_JOIN_UV_ZERO_SPAN_F"+str(face.index))
        unit=(best[0]/length,best[1]/length)
        projected=[p.x*unit[0]+p.z*unit[1] for p in pts]
        if max_y-min_y>1e-6:
            vals=[(v,min_y-pt.y) for v,pt in zip(projected,pts)]
        else:
            # horizontal (top/bottom) belts live in XZ; normal UV axes x and z
            vals=[(p.x,p.z) for p in pts]
        umin=min(x for x,y in vals);vmin=min(y for x,y in vals)
        urange=max(x for x,y in vals)-umin
        vrange=max(y for x,y in vals)-vmin
        if max(urange,vrange)>3.9:raise ValueError("LP_JOIN_FACE_NEEDS_TILE_SPLIT")
        for li,(u,v) in zip(face.loop_indices,vals):
            layer.data[li].uv=((u-umin)*inv+.015,(v-vmin)*inv+.015)
        # Check per-face metric map preserves every physical edge.
        for j in range(len(pts)):
            k=(j+1)%len(pts)
            physical=(pts[k]-pts[j]).length
            if physical<=1e-7:raise ValueError("LP_JOIN_DEGENERATE_PHYSICAL_EDGE")
            a=layer.data[face.loop_indices[j]].uv
            b=layer.data[face.loop_indices[k]].uv
            measured=(a-b).length*4096./physical
            if abs(measured-1000.)>1.5:
                raise ValueError(f"LP_JOIN_UV_EDGE_NONMETRIC_{face.index}_{measured:.4f}")
    return layer

def apply_lp_frame(frame, lp, min_angle=65.):
    """Migrate native OFF window frame to a connected LP-origin corner node.
    Original LP, Wall, Glass and Interior objects are never modified.
    Only use inside a saved, isolated W58.23 test scene.
    """
    import bpy,json
    if frame.get("DWOS_W58_topology_class")!="CORNER":
        raise ValueError("LP_JOIN_ONLY_FOR_SINGLE_CORNER")
    profile=lp_local_from_mesh(frame,lp)
    prior=frame.data
    hp_verts=[tuple(v.co) for v in prior.vertices]
    hp_faces=[tuple(face.vertices) for face in prior.polygons]
    outcome=connected_topology(hp_verts,hp_faces,profile,min_angle)
    check=validate_connected(outcome["verts"],outcome["faces"],outcome["post"])
    angle=float(outcome["plan"]["angle"])
    if not outcome["post"]:
        message=(f"Окно построено. Центральный угловой импост недоступен при "
                 f"{angle:.1f}°: минимальный угол 65°. Остальные инструменты DWOS доступны.")
        frame["DWOS_W5823_post_status"]="NO_CORNER_POST"
        frame["DWOS_W5823_friendly_hint"]=message
        frame["DWOS_W5823_lp_reference"]=lp.name
        frame["DWOS_W5823_lp_angle_deg"]=angle
        return {"status":"NO_POST","angle_deg":angle,"message":message,"audit":check}
    mesh=bpy.data.meshes.new(prior.name+"_W5823_LP_CONNECTED")
    mesh.from_pydata(outcome["verts"],[],outcome["faces"])
    mesh.update()
    for mat in prior.materials:mesh.materials.append(mat)
    roles=json.loads(frame.get("DWOS_face_roles","[]"))
    if len(roles)!=30:
        bpy.data.meshes.remove(mesh);raise ValueError("LP_JOIN_ROLE_MAP_NEEDS_30")
    # Preserve staged native roles for unchanged slots, extra new surfaces
    # belong to the LP corner profile (not a separate independent shell).
    roles[25]="FRAME_LP_CORNER_JOIN"
    roles[26]="FRAME_LP_CORNER_JOIN"
    roles[27]="FRAME_LP_CORNER_JOIN"
    roles[28]="FRAME_LP_CORNER_JOIN"
    roles+=["FRAME_LP_CORNER_JOIN"]*4
    _project_uv_metric(mesh)
    # Commit only after geometry and UV metrics have passed preflight.
    frame.data=mesh
    frame["DWOS_face_roles"]=json.dumps(roles)
    frame["DWOS_W5823_lp_reference"]=lp.name
    frame["DWOS_W5823_lp_angle_deg"]=angle
    frame["DWOS_W5823_own_depth_xz"]=list(outcome["plan"]["own_depth_axis"])
    frame["DWOS_W5823_lp_offset_m"]=float(outcome["plan"]["lp_inset_m"])
    frame["DWOS_W5823_post_depth_m"]=float(outcome["plan"]["post_depth_m"])
    frame["DWOS_W5823_post_status"]="LP_CONNECTED"
    frame["DWOS_W5823_post_island_count"]=0
    frame["DWOS_W5823_post_topology"]="SINGLE_CONNECTED_28V_34F"
    frame["DWOS_W5823_friendly_hint"]="Центральный угловой импост построен от LP; собственная ось глубины рассчитана."
    frame["DWOS_W5823_post_requires_live_rebuild_integration"]=True
    # This research stage doesn't modify an installed production addon.
    try:bpy.data.meshes.remove(prior)
    except Exception:pass
    return {"status":"LP_CONNECTED","angle_deg":angle,"audit":check,
            "post_depth_m":outcome["plan"]["post_depth_m"],
            "lp_offset_m":outcome["plan"]["lp_inset_m"],
            "own_depth_axis":outcome["plan"]["own_depth_axis"]}

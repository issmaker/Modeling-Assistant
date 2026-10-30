"""W58.28 User-reference-based CORNER solver: tangent-normal intersection.
EXPERIMENTAL TEST ONLY; do not install into production.
"""
import math, json, collections
from mathutils import Vector
import bpy
from . import frame_locked_post_core as fc

def v2(v):return Vector((float(v.x),float(v.z)))
def cross(a,b):return a.x*b.y-a.y*b.x

def section_from_lp(frame):
    n=int(frame.get("W5825_post_vertex_count",0))
    if n!=12:raise ValueError("USER_POST_EXPECTS_12_VERT_QUAD")
    me=frame.data
    st=len(me.vertices)-12
    F=v2(me.vertices[st+1].co)
    R=v2(me.vertices[st+3].co)
    tangent=(v2(me.vertices[2].co)-v2(me.vertices[1].co))
    if tangent.length<1e-5:raise ValueError("NATIVE_LP_TANGENT_DEGENERATE")
    tangent.normalize()
    normal=Vector((tangent.y,-tangent.x))
    run=(R-F).dot(tangent)
    depth=(R-F).dot(normal)
    if run<=1e-4 or depth<=1e-4:raise ValueError("NO_POSITIVE_NORMAL_INTERSECTION")
    S=F+run*tangent
    p=[Vector((me.vertices[st+i].co.x,me.vertices[st+i].co.z)) for i in range(6)]
    if (S.x<=float((p[0].x+p[1].x)*.5) or
            (tangent.y<0 and S.y>=F.y+1e-6)):
        raise ValueError("NONLOCAL_POST_SHOULDER")
    p[2]=S
    center_x=float((p[0].x+p[1].x)*.5)
    p[5]=Vector((2*center_x-S.x,S.y))
    turns=[cross(p[(i+1)%6]-p[i],p[(i+2)%6]-p[(i+1)%6]) for i in range(6)]
    # Revit-like architectural profiles can be slightly concave while still
    # being a valid non-self-intersecting 6-sided prismatic section.
    if (not fc.no_self_intersection(p) or
            abs(sum(cross(p[i],p[(i+1)%6]) for i in range(6)))*.5<.0002):
        raise ValueError("POST_SELF_INTERSECTION_OR_ZERO_FOOTPRINT")
    return {"p":p,"s":S,"run_m":run,"normal_depth_m":depth,
            "tangent":tangent,"normal":normal,"src_front":F,"src_rear":R,
            "start":st,"origin_frame_vertices":st}

def project_post_uv(frame,firstface):
    mesh=frame.data
    layer=mesh.uv_layers.active
    if layer is None:raise ValueError("NO_NATIVE_UV")
    old_first=mesh.polygons[firstface].loop_start
    old_uv=[tuple(q.uv) for q in list(layer.data)[:old_first]]
    for face in list(mesh.polygons)[firstface:]:
        vertices=[mesh.vertices[i].co for i in face.vertices]
        a=vertices[0]
        longest=max((vertices[i]-vertices[j] for i in range(len(vertices)) for j in range(i)),key=lambda v:v.length)
        if longest.length<1e-8:raise ValueError("POST_DEGENERATE_UV_EDGE")
        u=longest.normalized()
        if face.normal.length<.95:raise ValueError("POST_ZERO_FACE_NORMAL")
        v=face.normal.cross(u).normalized()
        coords=[((q-a).dot(u),(q-a).dot(v)) for q in vertices]
        minU=min(c[0] for c in coords);minV=min(c[1] for c in coords)
        for li,(x,y) in zip(face.loop_indices,coords):
            layer.data[li].uv=(.012+(x-minU)/4.096,.012+(y-minV)/4.096)
    if any((layer.data[k].uv-Vector(uv)).length>1e-7 for k,uv in enumerate(old_uv)):
        raise ValueError("ORIGINAL_FRAME_UV_CHANGED")

def point_inside(p,poly):
    inside=False
    for j in range(len(poly)):
        a=poly[j];b=poly[(j+1)%len(poly)]
        if (a.y>p.y)!=(b.y>p.y):
            if p.x<(b.x-a.x)*(p.y-a.y)/(b.y-a.y)+a.x:inside=not inside
    return inside

def collision(a,b):
    def strict(p,q,r,s):
        return cross(q-p,r-p)*cross(q-p,s-p)<-1e-12 and cross(s-r,p-r)*cross(s-r,q-r)<-1e-12
    return (any(strict(a[i],a[(i+1)%len(a)],b[j],b[(j+1)%len(b)])
                for i in range(len(a)) for j in range(len(b))) or
            any(point_inside(p,b) for p in a) or any(point_inside(p,a) for p in b))

def glass_clear(frame,glass,post):
    matrix=frame.matrix_world.inverted()@glass.matrix_world
    pts=[matrix@v.co for v in glass.data.vertices]
    if len(pts)!=16:raise ValueError("NEED_TWO_REAL_GLASS_BOXES")
    for inds in ((0,2,6,4),(8,10,14,12)):
        section=[v2(pts[i]) for i in inds]
        if collision(post,section):return False
    return True

def frame_sha_prefix(frame,vertex_count,face_count):
    import hashlib
    h=hashlib.sha256()
    for v in list(frame.data.vertices)[:vertex_count]:
        h.update(repr(tuple(round(float(x),8) for x in v.co)).encode())
    for f in list(frame.data.polygons)[:face_count]:
        h.update(repr(tuple(int(x) for x in f.vertices)).encode())
    return h.hexdigest()

def expand_rear_if_outward(F,R,t):
    """For mixed ribbons with outward-corner local tangent tilt, increase
    only the independent post rear shoulder as needed to keep a positive run.
    The authored frame/LP never moves. Do not exceed architecture limits.
    """
    t=t.normalized()
    v=R.copy()
    run=(v-F).dot(t)
    if t.y>0 and run<.012:
        if t.x<.2:raise ValueError("OUTWARD_TANGENT_TOO_SHARP")
        v.x=F.x+(.012-(v.y-F.y)*t.y)/t.x
        if abs(v.x-F.x)>.09:raise ValueError("OUTWARD_POST_WIDTH_UNSUPPORTED")
    return v

def preflight_section(F,R,t,center_x=0.0):
    """Pure predicate: optional post may be rejected, but NEVER the window."""
    t=t.normalized()
    R=expand_rear_if_outward(F,R,t)
    n=Vector((t.y,-t.x))
    run=(R-F).dot(t)
    depth=(R-F).dot(n)
    S=F+run*t
    p=[Vector((2*center_x-F.x,F.y)),F,S,R,Vector((2*center_x-R.x,R.y)),Vector((2*center_x-S.x,S.y))]
    turns=[cross(p[(i+1)%6]-p[i],p[(i+2)%6]-p[(i+1)%6]) for i in range(6)]
    allowed=bool(run>1e-4 and depth>1e-4 and S.x>center_x
       and fc.no_self_intersection(p)
       and abs(sum(cross(p[i],p[(i+1)%6]) for i in range(6)))*.5>.0002)
    return {"allowed":allowed,"points":p,"run_m":run,"normal_depth_m":depth}

def solve_native(frame):
    """Native DWOS window MUST exist. Modifies only that experimental frame and its glass."""
    lp=next((o for o in frame.children if o.get("DWOS_W58_auto_role")=="LP"),None)
    gl=next((o for o in frame.children if o.get("DWOS_role")=="GLASS"),None)
    it=next((o for o in frame.children if o.get("DWOS_role")=="INTERIOR"),None)
    if not lp or not gl or not it:raise ValueError("MISSING_NATIVE_COMPONENT")
    ref=fc.get_geometry_basis(frame,lp,gl)
    sourcev=len(frame.data.vertices);sourcef=len(frame.data.polygons)
    source_sha=frame_sha_prefix(frame,sourcev,sourcef)
    # Perform the optional feature predicate BEFORE touching the native HP.
    try:
        proposed=fc.plan_profiles(ref,"QUAD_ADAPTER")
        if proposed["status"]=="NO_POST":
            frame["W5828_user_post_warning"]="ANGLE_LT_65_DEG"
            return {"status":"NO_POST","angle_deg":ref["angle_deg"],
                    "source_sha":source_sha,"reason":"ANGLE_LT_65_DEG"}
        p=proposed["cross_section"]
        tangent=v2(frame.data.vertices[2].co)-v2(frame.data.vertices[1].co)
        pre=preflight_section(p[1],p[3],tangent,float(frame.data.vertices[1].co.x))
        if not pre["allowed"]:
            frame["W5828_user_post_warning"]="OPTIONAL_POST_CONVEXITY_UNSUPPORTED"
            return {"status":"NO_POST_WARNING","angle_deg":ref["angle_deg"],
                    "source_sha":source_sha,"reason":"OPTIONAL_POST_CONVEXITY_UNSUPPORTED"}
    except Exception as ex:
        frame["W5828_user_post_warning"]="POST_PREFLIGHT_UNCERTAIN"
        return {"status":"NO_POST_WARNING","angle_deg":ref["angle_deg"],
                "source_sha":source_sha,"reason":"POST_PREFLIGHT_UNCERTAIN",
                "details":repr(ex)}
    solution=fc.do_frame_locked(frame,"QUAD_ADAPTER")
    if solution["status"]=="NO_POST":
        return {"status":"NO_POST","angle_deg":ref["angle_deg"],"source_sha":source_sha,
                "reason":"ANGLE_LT_65_DEG"}
    # Same pure correction is applied to the staged independent post.
    st=len(frame.data.vertices)-12
    m=frame.data
    front=v2(m.vertices[st+1].co)
    rear=v2(m.vertices[st+3].co)
    tan=v2(m.vertices[2].co)-v2(m.vertices[1].co)
    new_rear=expand_rear_if_outward(front,rear,tan)
    if abs(new_rear.x-rear.x)>1e-8:
        center_x=float((m.vertices[st+0].co.x+m.vertices[st+1].co.x)*.5)
        for level in (0,6):
            m.vertices[st+level+3].co.x=new_rear.x
            m.vertices[st+level+4].co.x=2*center_x-new_rear.x
        m.update()
    shp=section_from_lp(frame)
    st=shp["start"]
    me=frame.data;firstface=sourcef
    for level in (0,6):
        me.vertices[st+level+2].co.x=shp["s"].x
        me.vertices[st+level+2].co.z=shp["s"].y
        me.vertices[st+level+5].co.x=shp["p"][5].x
        me.vertices[st+level+5].co.z=shp["s"].y
    me.update()
    # Split actual post mesh rings above 4m instead of overflowing 0-1 UV.
    hpost=abs(float(me.vertices[st+6].co.y-me.vertices[st].co.y))
    if hpost>4.000001:
        from . import w5829_post_uv_core
        frame["W5829_long_post_uv_result"]=str(w5829_post_uv_core.replace_post_island(frame))
    else:
        project_post_uv(frame,firstface)
    poly=shp["p"]
    if frame_sha_prefix(frame,sourcev,sourcef)!=source_sha:
        raise ValueError("RELEASE_FRAME_DRAG")
    # Native glass was initially fitted for legacy 55mm post.
    # Find smallest safe distance for THIS user's real tangent-normal post.
    glass_m=gl.data
    valid=None
    for offset_mm in range(55,240,3):
        seat=offset_mm/1000.0
        try:
            candidate,roles=fc.split_glass_at_seats(frame,gl,ref,requested_seat=seat)
        except ValueError:continue
        if len(candidate.polygons)!=12:continue
        old=gl.data;gl.data=candidate
        ok=glass_clear(frame,gl,poly)
        gl.data=old
        if ok:
            valid=(candidate,roles,seat)
            break
    if valid is None:raise ValueError("GLASS_CLEARANCE_NOT_FOUND")
    newglass,roles,seat=valid
    gl.data=newglass
    gl["DWOS_glass_roles"]=json.dumps(roles)
    if not glass_clear(frame,gl,poly):raise ValueError("POST_GLASS_COLLISION")
    frame["W5828_user_reference_post_mode"]="TANGENT_NORMAL_LP_INTERSECTION"
    frame["W5828_user_reference_angles"]="120/160"
    frame["W5828_shoulder_run_m"]=float(shp["run_m"])
    frame["W5828_normal_depth_m"]=float(shp["normal_depth_m"])
    frame["W5828_shoulders_xz"]=[float(shp["s"].x),float(shp["s"].y)]
    frame["W5828_selected_glass_seat_m"]=float(seat)
    frame["W5828_frame_invariant"]=True
    frame["W5828_glass_clear_2d"]=True
    frame["W5828_release_gate"]="EXPERIMENTAL_ONLY"
    return {"status":"SOLVED_USER_REFERENCE","angle_deg":ref["angle_deg"],
            "post_shell_separate_island":True,"source_sha":source_sha,
            "frame_sha_after":frame_sha_prefix(frame,sourcev,sourcef),
            "shoulder_xz":list(shp["s"]),"run_m":float(shp["run_m"]),
            "normal_m":float(shp["normal_depth_m"]),"glass_seat_m":seat,
            "source_verts":sourcev,"post_verts":len(me.vertices)-sourcev,
            "glass_clear_2d":True}

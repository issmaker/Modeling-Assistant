"""ISSMAKER BH-04B: conservative, dependency-free two-edge miter junction.

The only authorized Apply is a *planar convex miter* of exactly two
connected boundary edges. Real overlapping/warped surfaces are NOT trimmed.
This is an explicit conservative geometric subset, not a Boolean solver.
"""
import math
from dataclasses import dataclass


def xyz(v):
    q=tuple(float(x) for x in v)
    if len(q)!=3 or not all(map(math.isfinite,q)):
        raise ValueError('BH04B: недопустимые XYZ')
    return q


def uv2(v):
    q=tuple(float(x) for x in v)
    if len(q)!=2 or not all(map(math.isfinite,q)):
        raise ValueError('BH04B: недопустимые UV')
    return q


def add(a,b):return tuple(x+y for x,y in zip(a,b))
def sub(a,b):return tuple(x-y for x,y in zip(a,b))
def mul(a,k):return tuple(x*k for x in a)
def dot(a,b):return sum(x*y for x,y in zip(a,b))
def cross(a,b):return (a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])
def norm(a):return math.sqrt(dot(a,a))
def unit(a):
    n=norm(a)
    if n<=1e-10:raise ValueError('BH04B: нулевой вектор')
    return mul(a,1/n)

def signed_area3(a,b,c,normal):
    return dot(cross(sub(b,a),sub(c,a)),normal)*.5

@dataclass(frozen=True)
class MiterPlan:
    seam_start:tuple
    seam_mid:tuple
    seam_end:tuple
    outer_points:tuple
    polygons:tuple  # 2 quads: (source_common, source_far, moved_far, miter)
    triangle_names:tuple  # exact triangles for each new face (source,moved_far,..)
    max_miter_factor:float
    miter_shifts_m:tuple
    clearance_m:float
    uv_faces:tuple  # vertex-name -> uv per side; authored UV not modified
    source_ppm:tuple
    seam_gap_px:float
    seam_warning:bool
    triangle_areas:tuple


def plan_two_edge_miter(shared,far_a,far_b,normal_a,normal_b,
                        signed_depth,uv_shared_a,uv_far_a,uv_out_a,
                        uv_shared_b,uv_far_b,uv_out_b,
                        *,resolution=4096, target_ppm=1000.,tol=1e-7,
                        max_miter_factor=4.,max_overshoot_ratio=.85,
                        uv_safe_min=0.,uv_safe_max=1.):
    """Produce real candidate positions + 6 triangles with one shared seam.

    Requires two nonparallel, individually planar convex swept quads.
    Produces exact metric UV on NEW triangles. Source UV remains untouched,
    so mismatch of authored source density is reported as a possible seam.
    No source-edge split or arbitrary polygon Boolean is attempted.
    """
    s,a,b,na,nb=map(xyz,(shared,far_a,far_b,normal_a,normal_b))
    uas,uaa,uva,ubs,ubb,uvb=map(uv2,(uv_shared_a,uv_far_a,uv_out_a,
                                        uv_shared_b,uv_far_b,uv_out_b))
    d=float(signed_depth); res=float(resolution);ppm=float(target_ppm)
    if not all(map(math.isfinite,(d,res,ppm,tol,uv_safe_min,uv_safe_max))) or abs(d)<=1e-6 or res<=0 or ppm<=0 or not 1e-10<=tol<=1e-4 or not 0<=uv_safe_min<uv_safe_max<=1:
        raise ValueError('BH04B: неверная глубина, UV-разрешение или допуск')
    ea=sub(a,s);eb=sub(b,s);la=norm(ea);lb=norm(eb)
    if min(la,lb)<=1e-5:raise ValueError('BH04B: слишком короткое исходное ребро')
    ua=unit(ea); ub=unit(eb); na=unit(na); nb=unit(nb)
    if max(abs(dot(ua,na)),abs(dot(ub,nb)))>1e-5:
        raise ValueError('BH04B: направление не перпендикулярно ребру')
    cosine=dot(ua,ub)
    if abs(cosine)>math.cos(math.radians(8)):
        raise ValueError('BH04B: почти параллельные или противоположные рёбра; miter неустойчив')
    # Intersection of offset source lines in 3D; rejects skew lines.
    p1=add(s,mul(na,d));p2=add(s,mul(nb,d));w=sub(p2,p1)
    denom=1-cosine*cosine
    ta=(dot(w,ua)-cosine*dot(w,ub))/denom
    tb=(cosine*dot(w,ua)-dot(w,ub))/denom
    ma=add(p1,mul(ua,ta));mb=add(p2,mul(ub,tb))
    err=norm(sub(ma,mb));clearance=max(tol,1e-9*max(la,lb,abs(d)))
    if err>clearance:
        raise ValueError('BH04B: смещённые линии в 3D не пересекаются — нельзя сваривать угол')
    m=mul(add(ma,mb),.5);delta=sub(m,s)
    factor=norm(delta)/abs(d)
    if factor>max_miter_factor or not math.isfinite(factor):
        raise ValueError('BH04B: чрезмерно длинный miter')
    if abs(ta)>=max_overshoot_ratio*la or abs(tb)>=max_overshoot_ratio*lb:
        raise ValueError('BH04B: срез заходит слишком далеко на короткое ребро')
    if max(abs(dot(delta,na)-d),abs(dot(delta,nb)-d))>max(clearance*4,abs(d)*1e-6):
        raise ValueError('BH04B: нет одинаковой физической глубины обоих откосов')
    aprime=add(a,mul(na,d)); bprime=add(b,mul(nb,d))
    quads=((s,a,aprime,m),(s,b,bprime,m))
    # Strict convexity + planarity; reject self-folding (including near-zero faces).
    areas=[]
    for quad,nn in zip(quads,(na,nb)):
        ref=unit(cross(sub(quad[1],quad[0]),sub(quad[3],quad[0])))
        scale=max(la,lb,abs(d))
        if max(abs(dot(sub(pt,quad[0]),ref)) for pt in quad)>max(clearance*4,scale*1e-6):
            raise ValueError('BH04B: неплоская поверхность')
        turns=[dot(cross(sub(quad[(i+1)%4],quad[i]),sub(quad[(i+2)%4],quad[(i+1)%4])),ref) for i in range(4)]
        if any(t<=max(1e-12,1e-9*min(la,lb,abs(d))**2) for t in turns):
            raise ValueError('BH04B: вогнутая/складывающаяся или вырожденная поверхность')
    q=add(s,mul(delta,.5))
    # Each quad is triangulated identically; new shared seam is subdivided at Q.
    tri_names=(('S','A','AP'),('S','AP','Q'),('Q','AP','M'))
    tri_names_b=(('S','B','BP'),('S','BP','Q'),('Q','BP','M'))
    coords={'S':s,'A':a,'AP':aprime,'B':b,'BP':bprime,'Q':q,'M':m}
    triangles=[]
    for names in (tri_names,tri_names_b):
        for x,y,z in names:
            ar=norm(cross(sub(coords[y],coords[x]),sub(coords[z],coords[x])))/2
            if ar<=1e-10:raise ValueError('BH04B: нулевая площадь новой грани')
            triangles.append(ar)
    # UV mappings are per-side affine isometries at TARGET 1000 px/m.
    # The original authored UV of the wall is deliberately NOT rewritten.
    seam_gap=0.;source_ppm=[];side_uv=[]
    for idx,(out_edge,norm_edge,src_uv,far_uv,out_uv,names,shift) in enumerate((
        (ea,na,uas,uaa,uva,('S','A','AP','M','Q'),ta),
        (eb,nb,ubs,ubb,uvb,('S','B','BP','M','Q'),tb))):
        eunit=unit(out_edge)
        src_rate=norm(sub(far_uv,src_uv))*res/norm(out_edge)
        if src_rate<=1e-6:raise ValueError('BH04B: UV источника вырождено')
        tangent=unit(sub(far_uv,src_uv))
        uv_out=unit(out_uv)
        if abs(dot(tangent,uv_out))>1e-5:
            raise ValueError('BH04B: поперечный UV-вектор не ортогонален исходному')
        # Existing UV seam at original wall is not sacrificed to exact texel.
        # New UV begins with corrected metric endpoints centred on original UV.
        src_center=mul(add(src_uv,far_uv),.5)
        metric_s=sub(src_center,mul(tangent,norm(out_edge)*ppm/(2*res)))
        projected={}
        for name in names:
            rel=sub(coords[name],s)
            along=dot(rel,eunit)
            across=dot(rel,norm_edge)*(1 if d>=0 else -1)
            projected[name]=add(metric_s,add(mul(tangent,along*ppm/res),mul(uv_out,across*ppm/res)))
            if any(x<uv_safe_min-1e-8 or x>uv_safe_max+1e-8 for x in projected[name]):
                raise ValueError('BH04B: новый UV выходит за один UDIM; нужен многотайловый режим')
        base_seam=max(norm(sub(metric_s,src_uv)),norm(sub(projected[names[1]],far_uv)))*res
        seam_gap=max(seam_gap,base_seam)
        source_ppm.append(src_rate)
        side_uv.append(projected)
    uv_mismatch=max(norm(sub(side_uv[0]['M'],side_uv[1]['M'])),
                    norm(sub(side_uv[0]['Q'],side_uv[1]['Q'])))*res
    # A UV seam along the joined 3D seam is allowed and reported; do not
    # force welding UV coordinates when source wall islands are distinct.
    seam_gap=max(seam_gap,uv_mismatch)
    return MiterPlan(s,q,m,(aprime,bprime),quads,(tri_names,tri_names_b),
                     factor,(ta,tb),err,tuple(side_uv),tuple(source_ppm),
                     seam_gap,seam_gap>0.25,tuple(triangles))

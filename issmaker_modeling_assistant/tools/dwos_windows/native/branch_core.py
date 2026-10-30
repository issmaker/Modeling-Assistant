"""DWOS 3.4 pure Corner Branch Architecture.

A CORNER window is one architectural assembly but each facade side owns an
independent Zone/Split tree.  This module has no bpy dependency and is designed
for deterministic serialization, regression testing and transactional editing.
"""
from __future__ import annotations
import copy, json, math, hashlib
try:
    from .zone_core import root_node, decode as zone_decode, encode as zone_encode, layout as zone_layout, add as zone_add, update as zone_update, remove as zone_remove
except ImportError:
    from zone_core import root_node, decode as zone_decode, encode as zone_encode, layout as zone_layout, add as zone_add, update as zone_update, remove as zone_remove

EPS=1e-7
SCHEMA='DWOS_corner_branches_v1'


def _stable_id(index, left_station, right_station):
    key=f'{int(index)}:{int(left_station)}:{int(right_station)}'.encode('utf8')
    return 'branch_'+hashlib.sha1(key).hexdigest()[:10]


def branch_descriptors(topology):
    """Return deterministic branch ranges in centred S coordinates.

    Hard junctions are logical boundaries even when Corner Post mode is OFF.
    """
    if not isinstance(topology,dict) or topology.get('classification')!='CORNER':
        return []
    stations=list(topology.get('stations') or [])
    if len(stations)<2:return []
    width=float(topology.get('width',0.0)); half=width*.5
    cuts=[0]+[int(i) for i in topology.get('hard_junction_station_indices') or []]+[len(stations)-1]
    cuts=sorted(set(i for i in cuts if 0<=i<len(stations)))
    out=[]
    for bi,(a,b) in enumerate(zip(cuts,cuts[1:])):
        if b<=a:continue
        s0=float(stations[a]['s'])-half; s1=float(stations[b]['s'])-half
        if s1-s0<=EPS:continue
        out.append({
            'id':_stable_id(bi,a,b),'index':bi,'station_start':a,'station_end':b,
            's0':s0,'s1':s1,'junction_left':(a if a not in (0,len(stations)-1) else None),
            'junction_right':(b if b not in (0,len(stations)-1) else None),
        })
    return out


def new_state(topology, previous=None):
    desc=branch_descriptors(topology)
    prev={str(b.get('id')):b for b in ((previous or {}).get('branches') or []) if isinstance(b,dict)}
    branches=[]
    for d in desc:
        old=prev.get(d['id'],{})
        branches.append({**d,'tree':old.get('tree') or root_node()})
    return {'schema':SCHEMA,'branches':branches,'migration_status':(previous or {}).get('migration_status','NATIVE_3_4')}


def decode_state(raw, topology):
    if isinstance(raw,dict):data=copy.deepcopy(raw)
    elif raw:
        try:data=json.loads(raw)
        except Exception:data={}
    else:data={}
    if data.get('schema')!=SCHEMA:
        data={}
    return new_state(topology,data)


def encode_state(state):
    return json.dumps(state,ensure_ascii=False,separators=(',',':'),sort_keys=True)


def _post_boundary_reserve(s, posts):
    for p in posts or ():
        if abs(float(p.get('s',0.0))-float(s))<=1e-5:
            legacy=max(0.0,float(p.get('thickness',0.0))*.5)
            extent=max(0.0,float(p.get('physical_branch_extent',legacy)))
            integrated=(str(p.get('junction_contract','')) in {'INTEGRATED_FRAME_DEPTH_V1','INTEGRATED_ANGLE_V2'})
            return extent, integrated
    return 0.0, False


def branch_clear_span(branch, topology, frame_width, posts=(), clearance=0.003):
    """Usable S span for branch-owned zones/splits.

    Outer opening ends reserve main-frame width. Internal junctions reserve only
    the physical junction envelope. For the integrated CONVEX junction this is
    frame_depth along each branch and H-mullions butt-join that plane exactly.
    Legacy posts keep the historical clearance rule.
    """
    width=float(topology['width']); half=width*.5; fw=float(frame_width)
    lo=float(branch['s0']); hi=float(branch['s1'])
    if abs(lo+half)<=1e-5:lo+=fw
    else:
        reserve,integrated=_post_boundary_reserve(lo,posts)
        lo+=reserve + (0.0 if integrated else (float(clearance) if reserve>0 else 0.0))
    if abs(hi-half)<=1e-5:hi-=fw
    else:
        reserve,integrated=_post_boundary_reserve(hi,posts)
        hi-=reserve + (0.0 if integrated else (float(clearance) if reserve>0 else 0.0))
    if hi-lo<=max(EPS,fw*.2):
        raise ValueError('Corner branch has no usable span after frame/post envelopes')
    return lo,hi


def _branch_layout(branch, topology, height, frame_width, posts=()):
    lo,hi=branch_clear_span(branch,topology,frame_width,posts)
    span=hi-lo; center=(lo+hi)*.5
    # zone_core subtracts frame_width at each X side. Add those margins back to
    # the synthetic branch width so its root clear region is exactly [lo,hi].
    fake_width=span+2*float(frame_width)
    leaves,bands=zone_layout(zone_decode(branch.get('tree') or root_node()),fake_width,float(height),float(frame_width))
    gleaves=[];gbands=[]
    for zid,(x0,x1,y0,y1) in leaves:
        gleaves.append((f"{branch['id']}|{zid}",(x0+center,x1+center,y0,y1),branch['id']))
    for sid,axis,pos,thick,rect,band in bands:
        rx0,rx1,ry0,ry1=rect; bx0,bx1,by0,by1=band
        gle=(rx0+center,rx1+center,ry0,ry1)
        gbe=(bx0+center,bx1+center,by0,by1)
        gpos=pos+center if axis=='V' else pos
        gbands.append((f"{branch['id']}|{sid}",axis,gpos,thick,gle,gbe,branch['id']))
    return gleaves,gbands


def layout_state(state, topology, height, frame_width, posts=()):
    leaves=[];bands=[]
    for b in state.get('branches') or []:
        l,r=_branch_layout(b,topology,height,frame_width,posts);leaves.extend(l);bands.extend(r)
    return leaves,bands


def validate_post_clearance(state, topology, height, frame_width, posts=()):
    """Precommit/physics gate for integrated CORNER vertical mullion placement.

    Keep layout_state itself readable for diagnostics of existing, previously
    accepted invalid windows. All mutations / publishing must run this gate.
    """
    if not posts:
        return True
    try:
        from .corner_rules import validate_split_rows
    except ImportError:
        from corner_rules import validate_split_rows
    _leaves,bands=layout_state(state,topology,height,frame_width,posts)
    conflicts=validate_split_rows(bands,posts,clearance=0.003)
    if conflicts:
        first=conflicts[0]
        raise ValueError('Вертикальный импост пересекает центральный профиль CORNER '
                         f'({first["split_id"]}); отодвиньте его от угла')
    return True


def split_zone_ref(ref):
    text=str(ref or '')
    if '|' not in text:raise ValueError('Zone does not identify a branch')
    return text.split('|',1)


def find_branch(state, branch_id):
    return next((b for b in state.get('branches') or [] if b.get('id')==branch_id),None)


def add_split(state, topology, height, frame_width, zone_ref, axis, thickness, posts=()):
    staged=copy.deepcopy(state); bid,zid=split_zone_ref(zone_ref);b=find_branch(staged,bid)
    if b is None:raise ValueError('Corner branch no longer exists')
    lo,hi=branch_clear_span(b,topology,frame_width,posts);span=hi-lo
    fake_width=span+2*float(frame_width)
    tree,new_id=zone_add(zone_decode(b.get('tree') or root_node()),zid,axis,float(thickness),fake_width,float(height),float(frame_width))
    b['tree']=tree
    validate_post_clearance(staged,topology,height,frame_width,posts)
    return staged,f'{bid}|{new_id}'


def _split_ref(state, split_id):
    text=str(split_id or '')
    if '|' in text:
        bid,local=text.split('|',1)
        b=find_branch(state,bid)
        if b is not None:return b,local
    # Compatibility with early in-memory 3.4 drafts: search a unique local ID.
    hits=[]
    for b in state.get('branches') or []:
        def walk(n):
            if n.get('id')==text:return True
            return any(walk(c) for c in n.get('children',[]) or [])
        if walk(zone_decode(b.get('tree') or root_node())):hits.append(b)
    if len(hits)==1:return hits[0],text
    return None,None


def update_split(state, topology, height, frame_width, split_id, *, center=None, thickness=None, posts=()):
    staged=copy.deepcopy(state); b,local_id=_split_ref(staged,split_id)
    if b is None:raise ValueError('Branch split not found')
    lo,hi=branch_clear_span(b,topology,frame_width,posts);span=hi-lo;mid=(lo+hi)*.5
    fake_width=span+2*float(frame_width)
    local_center=None
    if center is not None:
        rows=rows_as_dicts(staged,topology,height,frame_width,posts)
        axis=next((r['axis'] for r in rows if r['id']==split_id),None)
        if axis is None:raise ValueError('Branch split not found')
        local_center=float(center)-mid if axis=='V' else float(center)
    b['tree']=zone_update(zone_decode(b.get('tree') or root_node()),local_id,fake_width,float(height),float(frame_width),center=local_center,thickness=thickness)
    validate_post_clearance(staged,topology,height,frame_width,posts)
    return staged


def remove_split(state, topology, height, frame_width, split_id, posts=()):
    staged=copy.deepcopy(state);b,local_id=_split_ref(staged,split_id)
    if b is None:raise ValueError('Branch split not found')
    lo,hi=branch_clear_span(b,topology,frame_width,posts);span=hi-lo
    fake_width=span+2*float(frame_width)
    b['tree']=zone_remove(zone_decode(b.get('tree') or root_node()),local_id,fake_width,float(height),float(frame_width))
    validate_post_clearance(staged,topology,height,frame_width,posts)
    return staged


def branch_for_s(state, s, topology, frame_width, posts=()):
    ss=float(s)
    best=None
    for b in state.get('branches') or []:
        lo,hi=branch_clear_span(b,topology,frame_width,posts)
        if lo-EPS<=ss<=hi+EPS:return b['id']
        d=min(abs(ss-lo),abs(ss-hi))
        if best is None or d<best[0]:best=(d,b['id'])
    return best[1] if best else ''


def branch_for_zone(state, zone_ref):
    try:return split_zone_ref(zone_ref)[0]
    except Exception:return ''


def rows_as_dicts(state, topology, height, frame_width, posts=()):
    _leaves,bands=layout_state(state,topology,height,frame_width,posts)
    return [dict(id=r[0],axis=r[1],position=r[2],thickness=r[3],rect=r[4],band=r[5],branch_id=r[6]) for r in bands]


def apply_positions(state, topology, height, frame_width, positions, posts=()):
    """Transactional multi-split update for branch trees.

    Splits in different branches are independent by construction. Within one
    branch, apply in movement-safe order so a common translation/alignment does
    not transiently cross a neighbour before final validation.
    """
    staged=copy.deepcopy(state)
    rows=rows_as_dicts(staged,topology,height,frame_width,posts)
    lookup={r['id']:r for r in rows}
    unknown=[sid for sid in positions if sid not in lookup]
    if unknown:raise ValueError('Branch split no longer exists')
    grouped={}
    for sid,target in positions.items():
        r=lookup[sid];grouped.setdefault(r['branch_id'],[]).append((sid,float(target),float(r['position'])))
    for bid,items in grouped.items():
        # Positive movement: right/top first. Negative: left/bottom first.
        avg_delta=sum(t-o for _sid,t,o in items)/max(1,len(items))
        items=sorted(items,key=lambda x:x[2],reverse=avg_delta>0)
        for sid,target,_old in items:
            staged=update_split(staged,topology,height,frame_width,sid,center=target,posts=posts)
    validate_post_clearance(staged,topology,height,frame_width,posts)
    return staged

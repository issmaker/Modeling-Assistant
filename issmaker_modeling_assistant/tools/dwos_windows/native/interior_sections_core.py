"""DWOS RC28: deterministic full-height mullion partition plans (no bpy).

A visible group can comprise several disjoint cavity shells, because every
physical mullion band must stay free of Interior faces. No CORNER fallback.
"""
from __future__ import annotations
import math

TOL=1e-5

def full_height_bands(splits,zone_bands,width,height,frame_width):
    """Physical vertical V bands crossing the *whole clear window height*.

    Zone bands are (id,axis,center,thickness,parent_rect,band). Global splits
    have .axis/.position/.thickness. Nested short V sections are excluded.
    """
    ymin=-float(height)*.5+float(frame_width)
    ymax=float(height)*.5-float(frame_width)
    found=[]
    for row in (zone_bands or ()):
        if isinstance(row,dict):
            axis=row['axis'];center=row['position'];thick=row['thickness'];rect=row['rect'];sid=row['id']
        else:
            sid,axis,center,thick,rect=row[:5]
        if axis=='V' and float(rect[2])<=ymin+TOL and float(rect[3])>=ymax-TOL:
            found.append((str(sid),float(center),float(thick)))
    if not zone_bands:
        for sp in splits or ():
            if sp.axis=='V':found.append((str(getattr(sp,'split_id','')),float(sp.position),float(sp.thickness)))
    result=[]
    for sid,c,t in sorted(found,key=lambda e:e[1]):
        if not math.isfinite(c) or not math.isfinite(t) or t<=TOL:
            raise ValueError('Interior: некорректная толщина или позиция V-импоста')
        if result and c-t*.5<result[-1][1]+result[-1][2]*.5-TOL:
            raise ValueError('Interior: пересечение сквозных вертикальных импостов')
        result.append((sid,c,t))
    return result


def partition_plan(rect, bands, requested=0, min_span=.003):
    """Physical cavity intervals + balanced consecutive logical group IDs.

    0 = maximum groups, 2..N+1 = requested. Each full V takes out its actual
    occupied thickness; grouping does not cause a cavity to cross that band.
    """
    x0,x1,y0,y1=map(float,rect)
    n=len(bands)
    if not n:
        if int(requested)>1:raise ValueError('Нет сквозных V-импостов для разделения Interior')
        return {'intervals':[(x0,x1)],'groups':[0],'group_count':1,'eligible':0,'cuts':[]}
    if int(requested)<0:raise ValueError('Interior: отрицательное количество групп')
    count=n+1 if int(requested)==0 else int(requested)
    if count<2 or count>n+1:
        raise ValueError(f'Interior: можно выбрать от 2 до {n+1} групп для {n} сквозных V')
    starts=[x0];ends=[];cuts=[]
    for sid,c,t in bands:
        a=c-t*.5;b=c+t*.5
        if a<=starts[-1]+min_span or b>=x1-min_span:
            raise ValueError('Interior: V-импост не оставляет достаточного проёма')
        ends.append(a);starts.append(b);cuts.append({'id':sid,'center':c,'thickness':t})
    ends.append(x1)
    intervals=list(zip(starts,ends))
    if any(hi-lo<=min_span for lo,hi in intervals):raise ValueError('Interior: нулевая секция')
    # Balanced contiguous grouping based on physical clear section lengths.
    # Deterministic dynamic programming, no arbitrary fixed split index.
    lengths=[b-a for a,b in intervals];m=len(lengths);mean=sum(lengths)/count
    dp={(0,0):(0.0,())}
    prefix=[0.0]
    for value in lengths:prefix.append(prefix[-1]+value)
    for k in range(1,count+1):
        for j in range(k,m+1):
            candidates=[]
            for i in range(k-1,j):
                if (k-1,i) not in dp:continue
                previous,edges=dp[(k-1,i)]
                cost=previous+(prefix[j]-prefix[i]-mean)**2
                candidates.append((cost,edges+(j,)))
            if candidates:dp[(k,j)]=min(candidates,key=lambda v:(round(v[0],12),v[1]))
    boundaries=dp[(count,m)][1];groups=[];start=0
    for gid,end in enumerate(boundaries):
        groups.extend([gid]*(end-start));start=end
    return {'intervals':intervals,'groups':groups,'group_count':count,
            'eligible':n,'cuts':cuts}


def join_meshes(items):
    verts=[];faces=[];roles=[];face_groups=[]
    for group,mesh in items:
        v,f,r=mesh
        if len(f)!=len(r):raise ValueError('Interior: damaged role mapping')
        off=len(verts);verts.extend(v)
        faces.extend(tuple(off+i for i in face) for face in f)
        roles.extend(r);face_groups.extend([int(group)]*len(f))
    return verts,faces,roles,face_groups

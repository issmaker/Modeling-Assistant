"""RC29: user-authored physical Interior sections, isolated from Blender runtime.

A vertical mullion is a candidate separator, never an automatic compulsory cut.
A selected separator splits two physically distinct cavity shells near its
centreline. Front contours of external frame are unchanged.
"""
from __future__ import annotations
import json
import math


def read_ids(raw):
    try: ids=json.loads(raw or '[]') if isinstance(raw,str) else raw
    except (TypeError,ValueError) as exc: raise ValueError('Interior: повреждён список границ') from exc
    if not isinstance(ids,list) or len(ids)>256 or any(not isinstance(v,str) or not v for v in ids):
        raise ValueError('Interior: список границ должен содержать ID импостов')
    if len(ids)!=len(set(ids)): raise ValueError('Interior: повторяющиеся ID границ')
    return ids


def choose_balanced(bands,rect,count):
    """Pick count-1 real posts giving approximately equal *physical* spans."""
    n=len(bands)
    if count==1:return []
    if count<1 or count>n+1:raise ValueError(f'Interior: допустимо от 1 до {n+1} секций')
    if count==n+1:return [str(b[0]) for b in bands]
    x0,x1=float(rect[0]),float(rect[1]);length=x1-x0
    # Optimal partition of consecutive post centres by least squared span error.
    pos=[x0]+[float(b[1]) for b in bands]+[x1]
    target=length/count
    dp={(0,0):(0.,())}
    for k in range(1,count+1):
        for j in range(k,n+2):
            if k<count and j==n+1:continue
            if k==count and j!=n+1:continue
            opt=[]
            for i in range(k-1,j):
                if (k-1,i) not in dp:continue
                score,indices=dp[k-1,i]
                opt.append((score+(pos[j]-pos[i]-target)**2,indices+(j,)))
            if opt:dp[k,j]=min(opt,key=lambda e:(round(e[0],12),e[1]))
    return [str(bands[j-1][0]) for j in dp[count,n+1][1][:-1]]



def effective_cut_ids(bands, rect, mode='CUSTOM', requested=1, selected='[]'):
    """One authoritative list of actually selected, full-height V cut IDs.

    Previously Composer rendered AUTO as ALL while click handling treated
    AUTO as NONE. The first click therefore created a cut rather than
    removing the one the user saw. COUNT and CUSTOM must agree as well.
    """
    posts=sorted([(str(sid),float(center),float(thickness)) for sid,center,thickness in bands],key=lambda x:x[1])
    ids=[row[0] for row in posts]
    if len(ids)!=len(set(ids)) or any(not sid for sid in ids):
        raise ValueError('Interior: некорректные ID сквозных импостов')
    if mode=='AUTO':
        result=ids
    elif mode=='COUNT':
        result=choose_balanced(posts,rect,int(requested))
    elif mode=='CUSTOM':
        result=read_ids(selected)
    else:
        raise ValueError('Interior: неизвестный режим разделения')
    invalid=set(result)-set(ids)
    if invalid:
        raise ValueError('Interior: сохранённые разделители не являются сквозными V: '+', '.join(sorted(invalid)))
    chosen=set(result)
    return [sid for sid in ids if sid in chosen]


def toggled_cut_ids(bands,rect,mode,requested,selected,cut_id):
    """Toggle precisely once; never silently reinterpret AUTO/COUNT as empty."""
    original=effective_cut_ids(bands,rect,mode,requested,selected)
    valid={str(row[0]) for row in bands}
    sid=str(cut_id)
    if sid not in valid:
        raise ValueError('Импост больше не является сквозным V')
    after=set(original)
    if sid in after:after.remove(sid)
    else:after.add(sid)
    ordered=[str(row[0]) for row in sorted(bands,key=lambda p:float(p[1]))]
    return original,[name for name in ordered if name in after]


def authored_plan(rect,bands,mode='AUTO',requested=0,selected=(),joint_gap=.01,min_span=.003):
    """Exactly one independent physical shell per authored Interior group.

    Stop at c +- seam/2, not at full mullion band outer edges. Both ends
    occupy the central portion of a mullion width, allowing separate reveals.
    """
    x0,x1,y0,y1=map(float,rect)
    if not (x1>x0 and y1>y0):raise ValueError('Interior: некорректные границы рамы')
    if mode not in ('AUTO','COUNT','CUSTOM'):raise ValueError('Interior: неизвестный способ деления')
    if not math.isfinite(float(joint_gap)) or float(joint_gap)<0:
        raise ValueError('Interior: зазор секций не может быть отрицательным')
    posts=[];seen=set()
    for sid,c,t in bands:
        sid=str(sid);c=float(c);t=float(t)
        if not sid or sid in seen or not all(math.isfinite(v) for v in (c,t)) or t<=0:
            raise ValueError('Interior: недействительный ID/размер сквозного V')
        seen.add(sid);posts.append((sid,c,t))
    posts.sort(key=lambda e:e[1])
    for a,b in zip(posts,posts[1:]):
        if a[1]+a[2]/2>b[1]-b[2]/2+1e-6:
            raise ValueError('Interior: импосты пересекаются')
    selected_ids=effective_cut_ids(posts,rect,mode,requested,selected)
    missing=set(selected_ids)-seen
    if missing:raise ValueError('Interior: выбранный импост не является сквозным V: '+', '.join(sorted(missing)))
    selected_set=set(selected_ids)
    active=[p for p in posts if p[0] in selected_set]
    left=x0;intervals=[];cuts=[]
    for sid,c,t in active:
        # RC38: selected Interior end faces must react to the actual V width.
        # The user's joint_gap is the *minimum* desired separation, while the
        # centreline-relative fraction follows the mullion as it is resized.
        # Never extend either face beyond 40% of the post half-width span:
        # both terminal reveals stay well INSIDE the physical profile.
        actual_gap=min(t*.8,max(float(joint_gap),t*.24))
        lo=c-actual_gap/2;hi=c+actual_gap/2
        if lo-left<=min_span or x1-hi<=min_span:
            raise ValueError('Interior: выбранный V слишком близко к границе или другому V')
        intervals.append((left,lo));left=hi
        cuts.append({'id':sid,'center':c,'thickness':t,'gap_m':actual_gap,
                     'left_end':lo,'right_start':hi})
    intervals.append((left,x1))
    if any(b-a<=min_span for a,b in intervals):raise ValueError('Interior: пустая секция')
    return {'intervals':intervals,'groups':list(range(len(intervals))),
            'group_count':len(intervals),'eligible':len(posts),
            'cuts':cuts,'active_ids':[x['id'] for x in cuts],
            'policy':'USER_SELECTED_CENTERLINE_V_RC29','mode':mode,
            'requested':int(requested),'joint_gap_requested_m':float(joint_gap)}

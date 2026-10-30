"""BH-03.2H.1: deterministic per-signed-axis decision model, Blender-free.

A preliminary angle pass is not a full Preflight. An unknown or incomplete
probe is NEVER reported as OK. One candidate failure never terminates others.
"""
import math
import time

VECTORS={'X':(1.,0.,0.),'Y':(0.,1.,0.),'Z':(0.,0.,1.)}


def candidate_specs(include_auto=True):
    items=[(name+('+' if sign>0 else '-'),'GLOBAL',axis,sign)
           for axis in 'XYZ' for name,sign in ((axis,1),(axis,-1))]
    if include_auto:
        items += [('AUTO'+('+' if sign>0 else '-'),'AUTO',None,sign)
                  for sign in (1,-1)]
    return items


def evaluate_candidates(geometry_gate, full_probe, depth, *, max_probes=8,
                        max_seconds=4., clock=None):
    """Evaluate every signed candidate independently in fixed order.

    max_seconds is a scheduling budget checked between calls, not a hard
    interruption of Blender's synchronous preflight. Results are immutable
    dictionaries that preserve errors; no suggestion without full success.
    """
    value=float(depth)
    if not math.isfinite(value) or not 0.001<=abs(value)<=50.:
        raise ValueError('Некорректная глубина Direction Alternatives')
    if not isinstance(max_probes,int) or max_probes<1:
        raise ValueError('Некорректный бюджет Preflight')
    if not math.isfinite(max_seconds) or max_seconds<=0:
        raise ValueError('Некорректный временной бюджет')
    clock=clock or time.perf_counter
    started=clock()
    calls=0
    answers=[]
    for key,mode,axis,sign in candidate_specs():
        if mode=='GLOBAL':
            try:
                valid,reason=geometry_gate(axis)
            except Exception as exc:
                valid,reason=False,'Предварительная проверка не выполнена: '+str(exc)
            if not valid:
                answers.append(dict(key=key,mode=mode,axis=axis,sign=sign,
                                    status='GEOMETRY_BLOCKED',reason=str(reason)[:170]));continue
        if calls>=max_probes or clock()-started>=max_seconds:
            answers.append(dict(key=key,mode=mode,axis=axis,sign=sign,
                                status='NOT_CHECKED',reason='Бюджет проверки исчерпан'));continue
        calls+=1
        try:
            full_probe(mode,axis,sign*abs(value))
        except Exception as exc:
            answers.append(dict(key=key,mode=mode,axis=axis,sign=sign,
                                status='PREFLIGHT_BLOCKED',reason=str(exc)[:170]))
        else:
            answers.append(dict(key=key,mode=mode,axis=axis,sign=sign,
                                status='VERIFIED_CANDIDATE',reason='Полный Preflight пройден'))
    return dict(items=answers, probes=calls, elapsed_s=max(0.,clock()-started),
                complete=all(a['status']!='NOT_CHECKED' for a in answers))


def rank_verified(result, preferred=None):
    permitted=[item for item in result['items'] if item['status']=='VERIFIED_CANDIDATE']
    return sorted(permitted,key=lambda item:(item['key']!=preferred,
                                              item['mode']=='AUTO',item['key']))

"""Pure state and layout policy for direct CORNICE controls (Blender independent).

Authored corner UIDs live in Definition.corner_uid_json. A corner click may
produce one and only one undoable build, never a direct Scene RNA mutation.
"""
import json

DIRECT_CONTROLS = ("C_PROFILE", "C_ROT", "C_SIDE", "C_PATH")


def corner_patch(serialized, vertex_uid, sharp):
    uid = int(vertex_uid)
    if uid <= 0:
        raise ValueError("Неизвестный UID вершины карниза")
    try:
        current = json.loads(serialized or "[]")
        if not isinstance(current, list):
            raise ValueError("Expected UID list")
        values = {int(item) for item in current if int(item) > 0}
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("Некорректные данные углов карниза") from exc
    if sharp:
        values.add(uid)
    else:
        values.discard(uid)
    normalized = sorted(values)
    return (json.dumps(normalized, separators=(",", ":")),
            "MAX_CLIPPED" if normalized else "MAX_REAL_WORLD")


def sharp_indices(serialized, uid_to_index):
    try:
        raw=json.loads(serialized or "[]")
        if not isinstance(raw,list):return set()
    except (TypeError,ValueError):
        return set()
    resolved=set()
    for item in raw:
        try:
            uid=int(item)
        except (TypeError,ValueError,OverflowError):
            continue
        if uid in uid_to_index:
            resolved.add(int(uid_to_index[uid]))
    return resolved


def corner_popup_position(point, region, size=(230,136)):
    width,height=map(float,region)
    w=min(float(size[0]),max(80.,width-12.))
    h=min(float(size[1]),max(40.,height-12.))
    x=max(6.,min(float(point[0])+18.,width-w-6.))
    y=max(6.,min(float(point[1])+14.,height-h-6.))
    return x,y,w,h


from ..production.corner_methods import MODES, MODE_IDS, MODE_LABELS

def method_for_uid(serialized, uid):
    try:
        raw=json.loads(serialized or '{}')
        mode=raw.get(str(int(uid)), 'CLASSIC') if isinstance(raw,dict) else 'CLASSIC'
        return mode if mode in MODE_IDS else 'CLASSIC'
    except (ValueError,TypeError,OverflowError):
        return 'CLASSIC'

def method_patch(serialized, uid, mode):
    if not int(uid)>0 or mode not in MODE_IDS:
        raise ValueError('Некорректный способ соединения угла')
    try:
        raw=json.loads(serialized or '{}')
        if not isinstance(raw,dict): raise ValueError('Invalid corner methods map')
        for k,v in raw.items():
            if int(k)<=0 or v not in MODE_IDS:raise ValueError('Invalid saved corner mode')
    except (ValueError,TypeError,OverflowError) as exc:
        raise ValueError('Поврежденные настройки методов углов') from exc
    raw[str(int(uid))]=mode
    return json.dumps(raw,sort_keys=True,separators=(',',':'))

def method_remove(serialized,uid):
    try:
        raw=json.loads(serialized or '{}')
        if not isinstance(raw,dict):return '{}'
        raw.pop(str(int(uid)),None)
        return json.dumps(raw,sort_keys=True,separators=(',',':'))
    except (ValueError,TypeError,OverflowError):
        return '{}'

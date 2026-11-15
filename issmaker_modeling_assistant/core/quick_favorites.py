"""ISSMAKER Quick Favorites — stable per-user tool shortcuts, no scene state.

Never persist favorites in a .blend; independently versioned user config.
All changes use atomic replacement. Prior settings are retained if a write
fails. A corrupt file is quarantined on first user-initiated write.
"""
from __future__ import annotations
import json
import math
import os
import shutil
from datetime import datetime
from pathlib import Path

SCHEMA="ISSMAKER_FAVORITES_V1"
TOOLS=("COLLISION","UV_WALL")
SHADE="SHADING_LAST"
ALLOWED=TOOLS+(SHADE,)
ICONS={
       "COLLISION":"FAV_COLLISION","UV_WALL":"FAV_WALL",
       SHADE:"FAV_SHADE"}
LABELS={
        "COLLISION":"Коллизии","UV_WALL":"Стены",
        SHADE:"Повторить шейдинг"}
_CACHE=None
_CACHE_PATH=None
_INVALID_PATH=None


def _config_path():
    import bpy
    folder=Path(bpy.utils.user_resource('CONFIG'))/'issmaker_modeling_assistant'
    return folder/'quick_favorites_v1.json'


def _default():
    return {"schema":SCHEMA,"favorites":[],"last_shading":None}


def validate(data):
    if not isinstance(data,dict) or data.get("schema")!=SCHEMA:
        raise ValueError("INVALID_FAVORITES_SCHEMA")
    items=data.get("favorites",[])
    if not isinstance(items,list) or any(not isinstance(x,str) for x in items):
        raise ValueError("INVALID_FAVORITE_IDS")
    # Never silently drop unrecognized values. An unreadable legacy config
    # is retained for explicit recovery before any user-initiated overwrite.
    if any(x not in ALLOWED for x in items):
        raise ValueError("INVALID_FAVORITE_IDS")
    if len(items)!=len(set(items)) or len(items)>len(ALLOWED):
        raise ValueError("DUPLICATE_OR_TOO_MANY_FAVORITES")
    shading=data.get("last_shading")
    if shading is not None:
        if not isinstance(shading,dict) or shading.get("mode") not in ("FLAT","SMOOTH"):
            raise ValueError("INVALID_LAST_SHADING")
        angle=shading.get("angle_degrees",None)
        if shading["mode"]=="SMOOTH":
            if not isinstance(angle,(int,float)) or not math.isfinite(angle) or not (0<=angle<=180):
                raise ValueError("INVALID_LAST_SHADING_ANGLE")
    return {"schema":SCHEMA,"favorites":list(items),
            "last_shading":dict(shading) if shading else None}


def load():
    global _CACHE,_CACHE_PATH,_INVALID_PATH
    path=_config_path()
    if _CACHE is not None and _CACHE_PATH==path:
        return validate(_CACHE)
    if not path.exists():
        _CACHE=_default()
    else:
        try:
            _CACHE=validate(json.loads(path.read_text(encoding='utf-8')))
        except (ValueError,TypeError,OSError) as exc:
            _CACHE=_default()
            _INVALID_PATH=path
            try:
                from .. import log
                log.write("FAVORITES_CONFIG_UNREADABLE: "+repr(exc))
            except (ImportError,AttributeError):
                pass
    _CACHE_PATH=path
    return validate(_CACHE)


def _save(data):
    global _CACHE,_CACHE_PATH,_INVALID_PATH
    updated=validate(data)
    path=_config_path()
    path.parent.mkdir(parents=True,exist_ok=True)
    if _INVALID_PATH==path and path.exists():
        # Never overwrite a malformed previous user config without saving it.
        stamp=datetime.now().strftime('%Y%m%d_%H%M%S')
        backup=path.with_name(path.stem+'.invalid_'+stamp+path.suffix)
        counter=1
        while backup.exists():
            backup=path.with_name(path.stem+'.invalid_'+stamp+'_'+str(counter)+path.suffix)
            counter+=1
        shutil.copy2(path,backup)
    tmp=path.with_name(path.name+'.tmp')
    try:
        tmp.write_text(json.dumps(updated,ensure_ascii=False,indent=2)+'\n',
                       encoding='utf-8')
        os.replace(tmp,path)
    finally:
        if tmp.exists():tmp.unlink()
    _CACHE=updated
    _CACHE_PATH=path
    if _INVALID_PATH==path:
        _INVALID_PATH=None
    try:
        import bpy
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type=="VIEW_3D":area.tag_redraw()
    except (ImportError,AttributeError,ReferenceError,RuntimeError):
        pass
    return validate(updated)


def favorites():
    return tuple(load()["favorites"])


def contains(identifier):
    return identifier in favorites()


def toggle(identifier):
    if identifier not in ALLOWED:
        raise ValueError("UNKNOWN_FAVORITE:"+str(identifier))
    data=load()
    items=data["favorites"]
    if identifier in items:
        items.remove(identifier)
        selected=False
    else:
        items.append(identifier)
        selected=True
    _save(data)
    return selected


def last_shading():
    return load()["last_shading"]


def record_shading(mode,angle_degrees=None):
    if mode not in ("SMOOTH","FLAT"):
        raise ValueError("UNKNOWN_SHADING_MODE")
    data=load()
    if mode=="SMOOTH":
        angle=float(angle_degrees)
        if not math.isfinite(angle) or not (0<=angle<=180):
            raise ValueError("INVALID_SHADING_ANGLE")
        data["last_shading"]={"mode":"SMOOTH","angle_degrees":round(angle,6)}
    else:
        data["last_shading"]={"mode":"FLAT"}
    return _save(data)


def tooltip(identifier):
    if identifier not in ALLOWED:return "Неизвестный инструмент"
    label=LABELS[identifier]
    if identifier==SHADE:
        last=last_shading()
        if last is None:return "Повторить последний шейдинг · сначала примени Smooth или Flat"
        if last["mode"]=="SMOOTH":
            return f"Повторить Smooth by Angle · {last['angle_degrees']:g}°"
        return "Повторить плоское затенение (Flat)"
    return "Быстрый запуск · "+label


def reset_cache():
    global _CACHE,_CACHE_PATH,_INVALID_PATH
    _CACHE=None
    _CACHE_PATH=None
    _INVALID_PATH=None


def quick_positions(anchor,number,height,scale=1.):
    """Vertical compact dock directly above the native Tools Gizmo.

    If the viewport is small, never create hidden click targets or overlap
    the radial opener. Painter and Blender Gizmos use this exact same list.
    """
    if anchor is None or number<=0:return ()
    x,y=map(float,anchor)
    step=42.*max(.75,min(1.3,float(scale)))
    size=32.*max(.75,min(1.3,float(scale)))
    result=[]
    for slot in range(min(number,len(ALLOWED))):
        py=y+(slot+1)*step
        if py+size*.5+10.>float(height):
            break
        result.append((round(x,3),round(py,3)))
    return tuple(result)


def icon(identifier):
    if identifier==SHADE:
        last=last_shading()
        if last is not None and last["mode"]=="FLAT":
            return "FAV_FLAT"
    return ICONS.get(identifier,"FAV_UNKNOWN")

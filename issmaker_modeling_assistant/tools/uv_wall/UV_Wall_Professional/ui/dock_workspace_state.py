"""Persistent user display-state and conservative settings presets.

This module intentionally contains no bpy imports: pure tests can exercise
all preference parsing, migration, filtering and preset transactions.
"""
import json
import math

MAX_PRESETS = 32
MODE_KEYS=('WALL','SURFACE','CORNICE')
# Only scene-independent primitive generation properties. Never store Path,
# Result, material/image/object pointers, per-corner UID, or geometry.
PRESET_FIELDS={
 'WALL':('module_standard','stories','custom_first_story','first_story_height',
         'story_direction','create_inner','inset_distance','wall_offset','wall_alignment',
         'create_top_cap','cap_material_override','create_bottom_cap','bottom_cap_material_override',
         'cap_rotation','wall_module_rotation','create_inner_surface','inner_surface_height_offset'),
 'SURFACE':('module_standard','surface_height_offset','surface_module_rotation','roof_normal'),
 'CORNICE':('module_standard','cornice_profile_rotation','cornice_flip_side',
           'cornice_profile_flip_x','cornice_profile_flip_y','cornice_reverse_path',
           'cornice_uv_mode','cornice_linear_step'),
}
BOOL_FIELDS={'custom_first_story','create_inner','create_top_cap','cap_material_override',
             'create_bottom_cap','bottom_cap_material_override','create_inner_surface',
             'cornice_flip_side','cornice_profile_flip_x','cornice_profile_flip_y','cornice_reverse_path'}
ENUMS={'module_standard':('2','4'),'story_direction':('UP','DOWN'),
       'wall_alignment':('INSIDE','CENTER','OUTSIDE','LEGACY','LEFT','RIGHT'),
       'roof_normal':('UP','DOWN'),
       'cornice_uv_mode':('MAX_CLIPPED','MAX_REAL_WORLD')}


def read_map(raw):
    try:
        data=json.loads(raw or '{}')
        return data if isinstance(data,dict) else {}
    except (ValueError,TypeError):
        return {}


def write_map(data):
    return json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def safe_strings(data,mode,allowed=None):
    values=data.get(mode,[]) if isinstance(data,dict) else []
    if not isinstance(values,list):return ()
    result=[]
    for item in values[:128]:
        if isinstance(item,str) and item not in result and (allowed is None or item in allowed):
            result.append(item)
    return tuple(result)


def collect_preset(definition, mode):
    return {name:getattr(definition,name) for name in PRESET_FIELDS.get(mode,())}


def validate_preset(values,mode):
    if mode not in MODE_KEYS or not isinstance(values,dict):
        raise ValueError('Неизвестный режим или повреждённый пресет')
    approved={}
    for name,val in values.items():
        if name not in PRESET_FIELDS[mode]:
            raise ValueError('Недопустимое поле: '+str(name))
        if name in BOOL_FIELDS:
            if not isinstance(val,bool):raise ValueError('Некорректный переключатель '+name)
        elif name in ENUMS:
            if not isinstance(val,str) or val not in ENUMS[name]:
                raise ValueError('Неизвестное значение '+name)
        elif name=='stories':
            if isinstance(val,bool) or not isinstance(val,int) or not 1<=val<=256:
                raise ValueError('Количество секций вне диапазона')
        elif name in ('first_story_height','inset_distance','cornice_linear_step'):
            if isinstance(val,bool) or not isinstance(val,(float,int)) or not math.isfinite(val) or val<(0 if name=='first_story_height' else 0.0000001) or val>10000:
                raise ValueError('Положительное значение недопустимо: '+name)
        else:
            if isinstance(val,bool) or not isinstance(val,(float,int)) or not math.isfinite(val) or abs(val)>100000:
                raise ValueError('Некорректное числовое значение '+name)
        approved[name]=val
    if approved.get('custom_first_story',False) and 'first_story_height' in approved and approved['first_story_height']<=0:
        raise ValueError('Высота первого модуля должна быть положительной')
    if not approved:raise ValueError('Пустой пресет')
    return approved


def apply_transaction(definition, values, mode):
    checked=validate_preset(values,mode)
    original={k:getattr(definition,k) for k in checked}
    old_rev=getattr(definition,'revision',0)
    try:
        for k,v in checked.items():setattr(definition,k,v)
        definition.revision=old_rev+1
    except Exception:
        for k,v in original.items():
            try:setattr(definition,k,v)
            except Exception:pass
        definition.revision=old_rev
        raise
    return len(checked)


def preset_entries(raw,mode):
    data=read_map(raw)
    values=data.get(mode,{})
    return values if isinstance(values,dict) else {}


def store_preset(raw,mode,name,definition):
    name=str(name).strip()
    if not name or len(name)>56 or any(ord(c)<32 for c in name):
        raise ValueError('Имя пресета: 1–56 символов без управляющих знаков')
    if mode not in MODE_KEYS:raise ValueError('Неизвестный режим')
    data=read_map(raw); group=preset_entries(raw,mode).copy()
    if name not in group and len(group)>=MAX_PRESETS:raise ValueError('Лимит пресетов достигнут')
    group[name]=validate_preset(collect_preset(definition,mode),mode)
    data[mode]=group
    return write_map(data)


def remove_preset(raw,mode,name):
    data=read_map(raw);group=preset_entries(raw,mode).copy()
    if name not in group:raise ValueError('Пресет не найден')
    del group[name];data[mode]=group
    return write_map(data)

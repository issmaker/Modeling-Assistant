"""DWOS 3.5.6 internal parametric history.

This history is intentionally separate from Blender's global scene Undo stack.
It stores only DWOS window/controller settings and logical mullion/zone state,
then reconstructs generated Mesh from those settings on undo/redo.

Object creation/finalize/weld operations remain Blender-scene operations and are
not represented here.
"""
from __future__ import annotations

import copy
import json

import bpy

from .dwos_log import event as log_event, exception as log_exception

MAX_HISTORY = 80
_UNDO = []
_REDO = []
_RESTORING = False
_HISTORY_SCENE = None

# Controller properties that define rebuildable DWOS geometry/interaction state.
_FIELDS = (
    'frame_width_text','internal_width_text','frame_depth_text','frame_offset_text',
    'optimization','interior_mode','interior_depth_text','interior_groups_count','interior_partition_mode','interior_dividers_json','interior_joint_gap_text','interior_clearance_text',
    'interior_offset_text','glass_offset_text','glass_edge_offset_text','glass_gap_text',
    'flip_interior','corner_post_mode','corner_post_variant','corner_post_width_text','corner_interior_variant',
    'zone_tree_json','branch_state_json','active_branch_id','active_zone_id',
    'active_zone_split_id','active_split_index','mullion_selection_json',
    'mullion_groups_json','mullion_group_name','mullion_offset_text','mullion_step_text',
)


def _window_objects(scene):
    out=[]
    for obj in scene.objects:
        s=getattr(obj,'dwos_window',None)
        if s and s.is_window and not s.finalized:
            out.append(obj)
    return sorted(out,key=lambda o:o.name)


def snapshot_window(obj):
    s=obj.dwos_window
    data={key:copy.deepcopy(getattr(s,key)) for key in _FIELDS}
    data['frame_material_name']=s.frame_material.name if s.frame_material else ''
    data['splits']=[{
        'split_id':sp.split_id,
        'axis':sp.axis,
        'position_text':sp.position_text,
        'thickness_text':sp.thickness_text,
        'override_thickness':bool(sp.override_thickness),
    } for sp in s.splits]
    return data


def capture_scene(scene):
    return {'schema':'DWOS_HISTORY_V1','windows':{obj.name:snapshot_window(obj) for obj in _window_objects(scene)}}


def _fingerprint(state):
    return json.dumps(state,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def _scene_token(scene):
    try:return scene.as_pointer()
    except (AttributeError,ReferenceError):return id(scene)


def _guard_scene(scene):
    global _HISTORY_SCENE
    current=_scene_token(scene)
    if _HISTORY_SCENE is None:
        _HISTORY_SCENE=current
    elif _HISTORY_SCENE != current:
        clear()
        _HISTORY_SCENE=current
    return current


def push(context,label='DWOS_EDIT'):
    """Record the state *before* a DWOS parametric mutation."""
    global _UNDO,_REDO
    if _RESTORING or context is None or context.scene is None:
        return False
    _guard_scene(context.scene)
    state=capture_scene(context.scene)
    fp=_fingerprint(state)
    if _UNDO and _UNDO[-1]['fingerprint']==fp:
        return False
    _UNDO.append({'label':str(label),'state':state,'fingerprint':fp})
    if len(_UNDO)>MAX_HISTORY:
        del _UNDO[:-MAX_HISTORY]
    _REDO=[]
    log_event('DWOS_HISTORY_PUSH',label=str(label),undo_depth=len(_UNDO),redo_depth=0)
    return True


def _apply_window(obj,target):
    s=obj.dwos_window
    for key in _FIELDS:
        if key in target:
            setattr(s,key,copy.deepcopy(target[key]))
    s.frame_material=bpy.data.materials.get(target.get('frame_material_name','')) if target.get('frame_material_name') else None
    s.splits.clear()
    for row in target.get('splits') or []:
        sp=s.splits.add()
        sp.split_id=str(row.get('split_id',''))
        sp.axis=str(row.get('axis','V'))
        sp.position_text=str(row.get('position_text','0'))
        sp.thickness_text=str(row.get('thickness_text','0,06'))
        sp.override_thickness=bool(row.get('override_thickness',False))


def restore_scene(context,state):
    """Transactional best-effort multi-window restore.

    A failing rebuild on window N must not leave windows 1..N-1 restored
    while the history entry is returned to the Undo stack as if nothing changed.
    """
    global _RESTORING
    from .window_builder import rebuild_window
    if not state or state.get('schema')!='DWOS_HISTORY_V1':
        raise ValueError('Неверный снимок истории DWOS')
    current={obj.name:snapshot_window(obj) for obj in _window_objects(context.scene)}
    changed=[]; touched=[]
    _RESTORING=True
    try:
        for name,target in (state.get('windows') or {}).items():
            obj=bpy.data.objects.get(name)
            if obj is None or not getattr(obj,'dwos_window',None) or not obj.dwos_window.is_window or obj.dwos_window.finalized:
                continue
            if current.get(name)==target:
                continue
            # Include failing target in compensation even when _apply_window or
            # rebuild_window throws before the successful-change bookkeeping.
            touched.append(name)
            _apply_window(obj,target)
            rebuild_window(obj)
            changed.append(name)
    except Exception:
        failed_rollbacks=[]
        for name in reversed(touched):
            obj=bpy.data.objects.get(name)
            previous=current.get(name)
            if obj is None or previous is None:
                continue
            try:
                _apply_window(obj,previous)
                rebuild_window(obj)
            except Exception as rollback_exc:
                failed_rollbacks.append(name)
                log_exception('DWOS_HISTORY_COMPENSATE_FAILED',rollback_exc,window=name)
        log_event('DWOS_HISTORY_RESTORE_ABORT',touched=len(touched),
                  rolled_back=len(touched)-len(failed_rollbacks),
                  rollback_failures=','.join(failed_rollbacks))
        raise
    finally:
        _RESTORING=False
    return changed

def undo(context):
    global _UNDO,_REDO
    if context is None or getattr(context,'scene',None) is None:
        return {'ok':False,'reason':'NO_SCENE','label':'','changed':[]}
    _guard_scene(context.scene)
    if not _UNDO:
        return {'ok':False,'reason':'EMPTY','label':'','changed':[]}
    current=capture_scene(context.scene)
    entry=_UNDO.pop()
    _REDO.append({'label':entry['label'],'state':current,'fingerprint':_fingerprint(current)})
    try:
        changed=restore_scene(context,entry['state'])
    except Exception as exc:
        # Put the entry back so a failed restore never consumes user history.
        _UNDO.append(entry);_REDO.pop()
        log_exception('DWOS_HISTORY_UNDO_FAILED',exc,label=entry['label'])
        raise
    log_event('DWOS_HISTORY_UNDO',label=entry['label'],changed=len(changed),undo_depth=len(_UNDO),redo_depth=len(_REDO))
    return {'ok':True,'label':entry['label'],'changed':changed}


def redo(context):
    global _UNDO,_REDO
    if context is None or getattr(context,'scene',None) is None:
        return {'ok':False,'reason':'NO_SCENE','label':'','changed':[]}
    _guard_scene(context.scene)
    if not _REDO:
        return {'ok':False,'reason':'EMPTY','label':'','changed':[]}
    current=capture_scene(context.scene)
    entry=_REDO.pop()
    _UNDO.append({'label':entry['label'],'state':current,'fingerprint':_fingerprint(current)})
    try:
        changed=restore_scene(context,entry['state'])
    except Exception as exc:
        _REDO.append(entry);_UNDO.pop()
        log_exception('DWOS_HISTORY_REDO_FAILED',exc,label=entry['label'])
        raise
    log_event('DWOS_HISTORY_REDO',label=entry['label'],changed=len(changed),undo_depth=len(_UNDO),redo_depth=len(_REDO))
    return {'ok':True,'label':entry['label'],'changed':changed}


def depths():
    scene=getattr(bpy.context,'scene',None)
    if scene is not None:_guard_scene(scene)
    return len(_UNDO),len(_REDO)


def clear():
    global _UNDO,_REDO,_HISTORY_SCENE
    _UNDO=[];_REDO=[];_HISTORY_SCENE=None
